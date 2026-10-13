"""
Project Sol0mon — AI-Powered Project Planner (Stage 1 of every workflow).

Responsibility: convert a user's objective into a structured execution graph
of work packages. NOT responsible for research, architecture, technology
selection, implementation, or planning implementation steps — only for
decomposition of the objective into typed, dependency-ordered work packages
that downstream stages (the scheduler + the existing debate pipeline)
execute.

Each work package contains:
    id           — stable string identifier (unique within the graph)
    title        — short human-readable label
    type         — one of: research | architecture | implementation |
                         validation | documentation
    description  — what the package must accomplish
    deliverable  — the concrete artefact the package produces
    dependencies — list of package ids that must complete first

Pipeline position:

    Objective → Planner → Scheduler → Existing Pipeline

Provider: Cerebras GPT-OSS-120B via the existing provider router
(``cerebras_client.generate`` → Cerebras → Groq → OpenRouter → Ollama). No
duplicate provider implementation is introduced.

Persistence: written inside the workspace next to the existing build files::

    workspaces/workspace_<id>/
        project.json             # objective + planner metadata
        planner/
            execution_graph.json # the validated graph

Resume: ``plan_objective`` does NOT regenerate the graph if a valid one is
already persisted, unless ``force=True``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Ponytail: reuse the existing provider router entry point and JSON extractor
# rather than reimplementing either. Both are already battle-tested across
# conflict_extractor / verification_engine / synthesis_engine.
from cerebras_client import generate as _default_generate
from conflict_extractor import extract_json as _extract_json
from validation import _find_cycle as _find_cycle

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Preferred model for planning (the spec names GPT-OSS-120B). The provider
# router already falls back through Groq → OpenRouter → Ollama when Cerebras
# is unavailable, so this is a preference, not a hard dependency.
DEFAULT_MODEL = os.getenv("SOL0MON_PLANNER_MODEL", "gpt-oss-120b")

DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

WORK_PACKAGE_TYPES = (
    "research",
    "architecture",
    "implementation",
    "validation",
    "documentation",
)

REQUIRED_PACKAGE_FIELDS = (
    "id",
    "title",
    "type",
    "description",
    "deliverable",
    "dependencies",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PlannerError(Exception):
    """Raised when the planner cannot produce a valid execution graph.

    Carries a human-actionable message; callers (the scheduler / pipeline)
    surface it directly. Ponytail: one class — no seven-subclass tax for a
    failure path nobody catches individually."""

    def __init__(self, message: str, *, retryable: bool = False,
                 cause: Exception | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_execution_graph(graph: Any) -> list[str]:
    """Validate the structure of an execution graph.

    Returns a list of human-readable issue strings; an empty list means the
    graph is valid. Pure; no I/O; no model calls. The checks are the five
    guarantees the spec demands:

      1. duplicate ids
      2. circular dependencies
      3. missing deliverables
      4. invalid dependency references (deps must point to real package ids)
      5. unknown work package types

    Plus a structural floor: the graph is a dict with a ``work_packages`` list
    and every package has all of ``REQUIRED_PACKAGE_FIELDS``.
    """
    issues: list[str] = []
    if not isinstance(graph, dict):
        return ["execution graph must be a JSON object"]
    packages = graph.get("work_packages")
    if not isinstance(packages, list) or not packages:
        return ["execution graph must have a non-empty 'work_packages' list"]

    # Per-package structural + type + deliverable + dep-ref checks (single
    # pass; O(n)). Dup-id packages are still checked for their OTHER defects
    # so a malformed-but-duped package surfaces every problem at once.
    ids: list[str] = []
    seen: set[str] = set()
    by_id: dict[str, dict] = {}
    for i, pkg in enumerate(packages):
        if not isinstance(pkg, dict):
            issues.append(f"work_package[{i}] is not an object")
            continue
        for field in REQUIRED_PACKAGE_FIELDS:
            if field not in pkg:
                issues.append(f"work_package[{i}] missing required field '{field}'")
        pid = pkg.get("id")
        if not isinstance(pid, str) or not pid.strip():
            issues.append(f"work_package[{i}] has a non-empty string 'id'")
            continue
        is_dup = pid in seen
        if is_dup:
            issues.append(f"duplicate work package id: '{pid}'")
        else:
            seen.add(pid)
            ids.append(pid)
            by_id[pid] = pkg
        ptype = pkg.get("type")
        if ptype not in WORK_PACKAGE_TYPES:
            issues.append(
                f"work_package[{i}] ('{pid}') has unknown type '{ptype}'; "
                f"expected one of {list(WORK_PACKAGE_TYPES)}"
            )
        deliverable = pkg.get("deliverable")
        if not isinstance(deliverable, str) or not deliverable.strip():
            issues.append(f"work_package[{i}] ('{pid}') is missing a non-empty 'deliverable'")
        deps = pkg.get("dependencies")
        if deps is None:
            issues.append(f"work_package[{i}] ('{pid}') is missing 'dependencies' (use [] for none)")
        elif not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            issues.append(f"work_package[{i}] ('{pid}') dependencies must be a list of strings")

    # Dependency reference check (deps must point to real package ids). Run
    # AFTER building the full id set so forward references resolve. Iterates
    # every package so even dup-id packages surface bad dep refs.
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        pid = pkg.get("id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        deps = pkg.get("dependencies")
        if not isinstance(deps, list):
            continue
        for d in deps:
            if d not in seen:
                issues.append(
                    f"work package '{pid}' depends on unknown package '{d}'"
                )

    # Cycle check: reuse the existing DFS cycle detector from the validation
    # framework so there is one implementation of this concern, not two.
    # Expected shape: {tid: {"dependencies": [...]}}.
    tasks_shape = {
        pid: {"dependencies": list((by_id.get(pid) or {}).get("dependencies") or [])}
        for pid in ids
    }
    cycle = _find_cycle(tasks_shape)
    if cycle:
        issues.append(f"circular dependency: {' -> '.join(cycle)}")

    return issues


# ---------------------------------------------------------------------------
# Topological order (Kahn's algorithm, insertion-stable)
# ---------------------------------------------------------------------------

def topological_order(graph: dict) -> list[str]:
    """Return package ids in dependency-first order. Raises PlannerError on
    cycles (defensive; ``validate_execution_graph`` flags them too)."""
    packages = (graph or {}).get("work_packages") or []
    by_id = {pkg["id"]: pkg for pkg in packages
             if isinstance(pkg, dict) and isinstance(pkg.get("id"), str)}
    indeg: dict[str, int] = {pid: 0 for pid in by_id}
    children: dict[str, list[str]] = {pid: [] for pid in by_id}
    for pid, pkg in by_id.items():
        for d in (pkg.get("dependencies") or []):
            if d in by_id:
                indeg[pid] += 1
                children[d].append(pid)
    queue = [pid for pid in by_id if indeg[pid] == 0]
    out: list[str] = []
    while queue:
        n = queue.pop(0)
        out.append(n)
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
        queue.sort(key=lambda x: list(by_id).index(x))
    if len(out) != len(by_id):
        raise PlannerError("execution graph has a cycle; cannot topologically order")
    return out


# ---------------------------------------------------------------------------
# LLM prompt construction
# ---------------------------------------------------------------------------

def build_planner_prompt(objective: str) -> str:
    """Build the prompt sent to the planner LLM.

    Strictly decomposes the objective into work packages. Explicitly forbids
    the planner from performing research, choosing technologies, or writing
    implementation plans — it must only structure the objective.
    """
    return f"""You are the Project Planner for Project Sol0mon.

Your ONLY job is to decompose a user's objective into a structured execution
graph of work packages. You do NOT perform research, design architecture,
choose technologies, generate implementation plans, or debate alternatives.
You ONLY decide which work packages are needed, in what order, with what
dependencies, and what each package must deliver.

Work package types (use exactly these values):
- research        (gather evidence, survey approaches, evaluate options)
- architecture    (produce the design given research findings)
- implementation  (build the artifact described)
- validation      (test / verify the implementation against the deliverable)
- documentation   (write user / maintainer docs for the deliverable)

Output a VALID JSON object and NOTHING ELSE. No prose, no markdown fences,
no trailing commentary. The schema is exactly:

{{
  "objective": "<the user's objective, summarized in one sentence>",
  "work_packages": [
    {{
      "id": "wp_<short_slug>",
      "title": "<short title>",
      "type": "research" | "architecture" | "implementation" | "validation" | "documentation",
      "description": "<what this package must accomplish>",
      "deliverable": "<the concrete artefact this package produces>",
      "dependencies": ["<id of a package that must complete first>", ...]
    }}
  ]
}}

Rules:
- 3 to 8 work packages total.
- Every package MUST have all six fields.
- 'id' values are unique; 'dependencies' reference other package ids (use []
  when there are none).
- The graph must be acyclic.
- A 'validation' package depends on 'implementation'; 'documentation'
  depends on 'implementation'; 'architecture' depends on 'research'.
- Do NOT pick technologies, name libraries, or write code. 'description'
  describes the SCOPE, not the solution.

USER OBJECTIVE:
{objective}
"""


# ---------------------------------------------------------------------------
# LLM call + retry loop
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, generate_fn: Callable[..., str] | None,
              module: str) -> str:
    """Call the provider router (or injected test seam). Raises on failure."""
    fn = generate_fn or _default_generate
    return fn(prompt, module=module) if "module" in fn.__code__.co_varnames \
        else fn(prompt)


def plan_objective(
    objective: str,
    *,
    generate_fn: Callable[..., str] | None = None,
    model: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """Call the planner LLM, validate the response, retry on a malformed
    response, and return the parsed execution graph.

    ``generate_fn`` is the injectable LLM seam: production callers omit it
    (the provider router is used); tests pass a fake. ``model`` defaults to
    ``gpt-oss-120b`` — passed only as metadata; the provider router already
    selects the model on the Cerebras side. Retries on:
      - malformed JSON
      - empty response
      - provider failure (network / timeout / 5xx)
      - schema validation failure (duplicate ids, cycles, missing fields, ...)

    Raises ``PlannerError`` after exhausting retries.
    """
    if not objective or not str(objective).strip():
        raise PlannerError("objective is empty", retryable=False)

    prompt = build_planner_prompt(str(objective))
    last_error: str = ""
    for attempt in range(1, max_retries + 1):
        try:
            raw = _call_llm(prompt, generate_fn, module="project_planner")
        except Exception as e:
            last_error = f"provider failure: {e}"
            print(f"[PROJECT PLANNER] attempt {attempt}/{max_retries} "
                  f"provider error: {last_error[:160]}", flush=True)
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if not raw or not str(raw).strip():
            last_error = "empty response from provider"
            print(f"[PROJECT PLANNER] attempt {attempt}/{max_retries} "
                  f"{last_error}", flush=True)
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        parsed = _extract_json(raw)
        if not isinstance(parsed, dict) or "work_packages" not in parsed:
            last_error = "malformed JSON: missing 'work_packages' object"
            print(f"[PROJECT PLANNER] attempt {attempt}/{max_retries} "
                  f"{last_error}", flush=True)
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        issues = validate_execution_graph(parsed)
        if issues:
            last_error = "schema validation failed: " + "; ".join(issues[:3])
            print(f"[PROJECT PLANNER] attempt {attempt}/{max_retries} "
                  f"{last_error}", flush=True)
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        parsed.setdefault("objective", str(objective))
        parsed.setdefault("model", model or DEFAULT_MODEL)
        parsed.setdefault("planner", "project_planner")
        parsed.setdefault("planned_at", datetime.now(timezone.utc).isoformat())
        print(f"[PROJECT PLANNER] valid graph with "
              f"{len(parsed['work_packages'])} work packages", flush=True)
        return parsed

    raise PlannerError(
        f"planner failed after {max_retries} attempt(s); last error: {last_error}",
        retryable=False,
    )


# ---------------------------------------------------------------------------
# Workspace persistence
# ---------------------------------------------------------------------------

def _workspace_dir(workspace_id: str) -> Path:
    """Resolve a planner workspace dir. Reuses build_workspace._workspace_dir
    when available so planner files live alongside the existing build
    workspace; otherwise falls back to a planner-specific subdir."""
    try:
        from build_workspace import _workspace_dir as _bw_dir
        return _bw_dir(workspace_id)
    except Exception:
        from build_workspace import WORKSPACE_ROOT
        return WORKSPACE_ROOT / f"workspace_{workspace_id}"


def _project_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "project.json"


def _graph_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "planner" / "execution_graph.json"


def save_execution_graph(workspace_id: str, graph: dict) -> dict:
    """Persist the execution graph + project metadata inside the workspace.
    Idempotent: re-saving the same graph produces the same files."""
    graph = dict(graph or {})
    graph.setdefault("workspace_id", workspace_id)
    graph.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    g_path = _graph_path(workspace_id)
    g_path.parent.mkdir(parents=True, exist_ok=True)
    g_path.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    project = {
        "workspace_id": workspace_id,
        "objective": graph.get("objective", ""),
        "model": graph.get("model", DEFAULT_MODEL),
        "planner": graph.get("planner", "project_planner"),
        "planned_at": graph.get("planned_at", ""),
        "package_count": len(graph.get("work_packages") or []),
    }
    p_path = _project_path(workspace_id)
    p_path.parent.mkdir(parents=True, exist_ok=True)
    p_path.write_text(
        json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return project


def load_execution_graph(workspace_id: str) -> dict | None:
    """Read the persisted execution graph. Returns None if absent or corrupt."""
    if not workspace_id:
        return None
    p = _graph_path(workspace_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")) or None
    except (json.JSONDecodeError, OSError):
        return None


def load_project(workspace_id: str) -> dict | None:
    """Read the persisted project metadata. None if absent/corrupt."""
    if not workspace_id:
        return None
    p = _project_path(workspace_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")) or None
    except (json.JSONDecodeError, OSError):
        return None


def has_execution_graph(workspace_id: str) -> bool:
    """True iff a persisted execution graph is present AND structurally valid.
    Used by the resume path to skip regeneration silently."""
    graph = load_execution_graph(workspace_id)
    if graph is None:
        return False
    return not validate_execution_graph(graph)


# ---------------------------------------------------------------------------
# High-level entry: plan, persist, resume
# ---------------------------------------------------------------------------

def project_planner(
    objective: str,
    *,
    workspace_id: str | None = None,
    force: bool = False,
    generate_fn: Callable[..., str] | None = None,
    model: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[str, dict]:
    """Plan an objective, persisting the graph inside a workspace.

    Resume semantics: if a valid execution graph already exists for the
    workspace and ``force`` is False, the existing graph is returned without
    calling the LLM. Pass ``force=True`` to regenerate.

    Creates a fresh build workspace when ``workspace_id`` is None so the
    planner is usable standalone (not only downstream of an existing
    workspace).

    Returns ``(workspace_id, graph)``. Raises ``PlannerError`` on failure.
    """
    if workspace_id is None:
        try:
            from build_workspace import create_workspace
            workspace_id = create_workspace(source_question=str(objective)[:500])
        except Exception:
            workspace_id = str(uuid.uuid4())

    if not force and has_execution_graph(workspace_id):
        graph = load_execution_graph(workspace_id)
        print(f"[PROJECT PLANNER] resumed existing graph for workspace "
              f"{workspace_id[:8]}...", flush=True)
        return workspace_id, graph  # type: ignore[return-value]

    graph = plan_objective(
        objective, generate_fn=generate_fn, model=model, max_retries=max_retries,
    )
    save_execution_graph(workspace_id, graph)
    print(f"[PROJECT PLANNER] persisted execution_graph.json for workspace "
          f"{workspace_id[:8]}...", flush=True)
    return workspace_id, graph


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ponytail self-check: the only thing that breaks if the contract regresses.
    good = {
        "objective": "ship X",
        "work_packages": [
            {"id": "wp_research", "title": "Research", "type": "research",
             "description": "survey", "deliverable": "research.md",
             "dependencies": []},
            {"id": "wp_arch", "title": "Architecture", "type": "architecture",
             "description": "design", "deliverable": "architecture.md",
             "dependencies": ["wp_research"]},
            {"id": "wp_impl", "title": "Implement", "type": "implementation",
             "description": "build", "deliverable": "src/",
             "dependencies": ["wp_arch"]},
            {"id": "wp_val", "title": "Validate", "type": "validation",
             "description": "test", "deliverable": "tests/",
             "dependencies": ["wp_impl"]},
            {"id": "wp_doc", "title": "Docs", "type": "documentation",
             "description": "document", "deliverable": "README.md",
             "dependencies": ["wp_impl"]},
        ],
    }
    assert validate_execution_graph(good) == [], validate_execution_graph(good)
    assert topological_order(good) == [
        "wp_research", "wp_arch", "wp_impl", "wp_val", "wp_doc",
    ]

    # Cycle
    cyclic = {"work_packages": [
        {"id": "a", "title": "a", "type": "research", "description": "d",
         "deliverable": "x", "dependencies": ["b"]},
        {"id": "b", "title": "b", "type": "research", "description": "d",
         "deliverable": "x", "dependencies": ["a"]},
    ]}
    assert any("circular" in i for i in validate_execution_graph(cyclic))

    # Dup id, unknown type, missing deliverable, bad dep ref
    bad = {"work_packages": [
        {"id": "x", "title": "t", "type": "research", "description": "d",
         "deliverable": "out", "dependencies": []},
        {"id": "x", "title": "t", "type": "exotic", "description": "d",
         "deliverable": "", "dependencies": ["zzz"]},
    ]}
    issues = validate_execution_graph(bad)
    assert any("duplicate" in i for i in issues)
    assert any("unknown type" in i for i in issues)
    assert any("deliverable" in i for i in issues)
    assert any("unknown package" in i for i in issues)

    # plan_objective with injected fake generate returns a valid graph and
    # retries once on a malformed response.
    calls = {"n": 0}
    good_json = json.dumps(good)
    def fake_gen(prompt, module=""):
        calls["n"] += 1
        return "not json at all" if calls["n"] == 1 else good_json
    g = plan_objective("ship X", generate_fn=fake_gen, max_retries=3)
    assert g["work_packages"] == good["work_packages"]
    assert calls["n"] == 2

    # Persistence + resume
    import tempfile
    import build_workspace as _bw
    saved_root = _bw.WORKSPACE_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)
        wid, g1 = project_planner("ship X", generate_fn=fake_gen)
        assert has_execution_graph(wid)
        # second call resumes without regenerating
        calls["n"] = 0
        wid2, g2 = project_planner("ship X", workspace_id=wid)
        assert wid2 == wid
        assert calls["n"] == 0  # no LLM call on resume
        # force regenerates
        calls["n"] = 0
        _bw.WORKSPACE_ROOT = Path(tmp)
        project_planner("ship X", workspace_id=wid, force=True,
                        generate_fn=fake_gen)
        assert calls["n"] >= 1
        _bw.WORKSPACE_ROOT = saved_root
    print("project_planner self-check PASSED")
