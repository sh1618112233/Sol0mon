"""
Project Sol0mon - Execution Planner + Task Graph (Final Architecture Hardening -
Phases D + E).

Inserts a deterministic planning stage BETWEEN build-spec generation and the
coding agent, replacing the previous direct hand-off:

    Build Spec  ->  Execution Planner  ->  Task Graph (DAG)  ->  Coding Agent

The planner is DETERMINISTIC (no LLM): it derives the task set from the
canonical decision model already captured in the BuildSpec (the same
``_architecture_components`` matcher ``build_spec_engine`` uses) plus the spec's
verified_decisions. "Do not add new AI capabilities" — the planner reuses the
model the verifier already produced, it does not invent new structure.

Each task records: inputs, outputs, dependencies, owner, status, retries,
validation stage (matching the validator names in ``validation.py``). The graph
supports partial completion, resume (a task is ``ready()`` when all deps are
DONE), and future parallel execution (any subset of ``ready()`` can run
concurrently — the DAG is the only ordering constraint).

Outputs:
    ExecutionPlan (dataclass)
        .tasks: dict[str, Task]   # the DAG
        .order: list[str]         # a valid topological order
    ExecutionPlan.to_dict() is the JSON-able payload wrapped by
    ``execution_plan_artifact(...)`` (an ArtifactKind.EXECUTION_PLAN instance).

Pure stdlib. Pure functions. No I/O.
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class TaskKind(str, enum.Enum):
    COMPONENT = "component"
    DECISION = "decision"
    TESTS = "tests"
    INTEGRATION = "integration"
    VALIDATION = "validation"
    ACCEPTANCE = "acceptance"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Task:
    id: str
    kind: TaskKind
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    owner: str = ""
    status: TaskStatus = TaskStatus.PENDING
    retries: int = 0
    acceptance_criteria: tuple[str, ...] = ()
    validation: str = ""  # validator stage name (matches validation.VALIDATORS)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind.value,
            "inputs": list(self.inputs), "outputs": list(self.outputs),
            "dependencies": list(self.dependencies),
            "owner": self.owner, "status": self.status.value,
            "retries": self.retries,
            "acceptance_criteria": list(self.acceptance_criteria),
            "validation": self.validation,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """A BuildSpec decomposed into a task DAG. ``order`` is a deterministic
    topological order (stable on insertion order for equal-rank tasks)."""

    tasks: dict[str, Task]
    order: list[str]
    build_spec_artifact_id: str = ""
    spec_checksum: str = ""
    id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at",
                               datetime.now(timezone.utc).isoformat())
        if not self.id:
            digest = hashlib.sha256(
                (self.spec_checksum + "|" +
                 "|".join(self.order) + "|" +
                 "|".join(f"{t.id}:{t.kind.value}" for t in
                          (self.tasks[k] for k in self.order))
                 ).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "id", f"plan_{digest[:16]}")

    def to_dict(self) -> dict:
        # NOTE: created_at is intentionally EXCLUDED so the payload (and thus
        # the Artifact checksum / id) is deterministic over the plan's
        # structure. Provenance time is stamped by the Artifact wrapping it.
        return {
            "id": self.id,
            "build_spec_artifact_id": self.build_spec_artifact_id,
            "spec_checksum": self.spec_checksum,
            "order": self.order,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
        }

    def ready(self) -> list[str]:
        """Task ids whose dependencies are all DONE — eligible to run now.
        Supports partial completion + resume + future parallel execution."""
        done = {tid for tid, t in self.tasks.items()
                if t.status is TaskStatus.DONE}
        out = []
        for tid, t in self.tasks.items():
            if t.status is not TaskStatus.PENDING:
                continue
            if all(d in done for d in t.dependencies):
                out.append(tid)
        return out

    def dependents(self, task_id: str) -> list[str]:
        return [tid for tid, t in self.tasks.items() if task_id in t.dependencies]

    def is_complete(self) -> bool:
        return all(t.status is TaskStatus.DONE for t in self.tasks.values())


# --- Deterministic topological order (Kahn's algorithm, insertion-stable) ---

def _topo_order(tasks: dict[str, Task]) -> list[str]:
    indeg: dict[str, int] = {tid: 0 for tid in tasks}
    children: dict[str, list[str]] = {tid: [] for tid in tasks}
    for tid, t in tasks.items():
        for d in t.dependencies:
            if d in tasks:  # ignore unknown deps here; the validator flags them
                indeg[tid] += 1
                children[d].append(tid)
    # insertion-stable queue: process in dict insertion order
    queue = [tid for tid in tasks if indeg[tid] == 0]
    out: list[str] = []
    while queue:
        n = queue.pop(0)
        out.append(n)
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
        # re-stabilize: keep queue ordered by task insertion order so equal-rank
        # tasks emit in a deterministic sequence (Tests / Integration / etc.)
        queue.sort(key=lambda x: list(tasks).index(x))
    return out


_SLUG_RE = __import__("re").compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    s = _SLUG_RE.sub("_", (name or "").lower()).strip("_")
    return s or "component"


# The acceptance criteria carried onto the validation / acceptance tasks —
# matches the deterministic contract emitted by build_spec_engine's
# # Acceptance Criteria section.
_ACCEPTANCE_CRITERIA: tuple[str, ...] = (
    "implementation_plan.md complete (validate_spec_sections returns [])",
    "every MUST constraint honoured (decision_gaps empty)",
    "pytest suite exits 0 with >=1 passing test",
    "agent produced >=1 new file (artefact-presence gate)",
)


def plan(
    build_spec: dict | Any,
    build_spec_artifact_id: str = "",
) -> ExecutionPlan:
    """Decompose a BuildSpec (the dict returned by ``build_spec_engine.build_spec``,
    or an ``Artifact`` whose payload is one) into a task DAG.

    Derivation (deterministic — same spec -> identical DAG, byte-for-byte):
      1. One COMPONENT task per platform component matched by the canonical
         ``decision_renderer._architecture_components`` (the same matcher the
         spec renderer uses — single source of truth).
      2. One DECISION task per verified decision, depending on ALL component
         tasks (the decision is realized by the components as a whole; refined
         per-decision matching would re-implement the component matcher).
      3. A TESTS task depending on every component + every decision task.
      4. An INTEGRATION task depending on every component task.
      5. A VALIDATION task depending on (tests, integration) -> runs the
         ``final_build`` validator.
      6. An ACCEPTANCE task depending on validation (terminal tasks of the DAG).

    Falls back to a single IMPLEMENTATION task when no components / decisions
    are derived (the spec still carries # Constraints the agent honors). The
    DAG is never empty.
    """
    if hasattr(build_spec, "payload") and not isinstance(build_spec, dict):
        spec = build_spec.payload
        if not build_spec_artifact_id:
            build_spec_artifact_id = getattr(build_spec, "id", "")
    else:
        spec = build_spec or {}

    verification_result = spec.get("verification_result") or {}
    verified_decisions = spec.get("verified_decisions") or []

    # Canonical component list (same call build_spec_engine._render_spec_sections
    # makes — single source of truth; no re-parsing of the markdown).
    try:
        from decision_renderer import _architecture_components
        components = _architecture_components(verification_result) or []
    except Exception:
        components = []

    tasks: dict[str, Task] = {}

    comp_ids: list[str] = []
    for i, c in enumerate(components):
        tid = f"comp_{i:02d}_{_slug(c.get('name', 'component'))}"
        loc = f"src/{_slug(c.get('name', 'component'))}/"
        tasks[tid] = Task(
            id=tid, kind=TaskKind.COMPONENT,
            inputs=(c.get("name", "component"),),
            outputs=(loc,), dependencies=(), owner="coding_agent",
            acceptance_criteria=(f"produce files under {loc}",),
        )
        comp_ids.append(tid)

    # No components -> synthesize one so the DAG is non-empty and the agent has
    # at least one task to point at. The spec's # Constraints still bind.
    if not comp_ids:
        tid = "comp_00_implementation"
        tasks[tid] = Task(
            id=tid, kind=TaskKind.COMPONENT,
            inputs=("derived from # Constraints",),
            outputs=("src/",), dependencies=(), owner="coding_agent",
            acceptance_criteria=("produce at least one source file",),
        )
        comp_ids.append(tid)

    dec_ids: list[str] = []
    for i, d in enumerate(verified_decisions):
        if not isinstance(d, dict):
            continue
        key = (d.get("dispute_key") or d.get("normalized_key")
               or d.get("key") or f"dec{i}")
        tid = f"decision_{i:02d}_{_slug(str(key))}"
        tasks[tid] = Task(
            id=tid, kind=TaskKind.DECISION,
            inputs=(str(d.get("decision", "")),),
            outputs=(f"honor:{key}",),
            dependencies=tuple(comp_ids),
            owner="coding_agent",
            acceptance_criteria=(f"honor verified decision {key} in # Constraints",),
            validation="build_spec",
        )
        dec_ids.append(tid)

    tests_id = "tests"
    tasks[tests_id] = Task(
        id=tests_id, kind=TaskKind.TESTS,
        inputs=("pytest",), outputs=("tests/",),
        dependencies=tuple(comp_ids + dec_ids),
        owner="coding_agent",
        acceptance_criteria=("one tests_*.py per source module; suite exits 0",),
        validation="tests",
    )

    integ_id = "integration"
    tasks[integ_id] = Task(
        id=integ_id, kind=TaskKind.INTEGRATION,
        inputs=("all components",), outputs=("integration_report",),
        dependencies=tuple(comp_ids),
        owner="coding_agent",
        acceptance_criteria=("components combine without import / wiring errors",),
    )

    val_id = "validation"
    tasks[val_id] = Task(
        id=val_id, kind=TaskKind.VALIDATION,
        inputs=("tests", "integration"),
        outputs=("validation_report",),
        dependencies=(tests_id, integ_id),
        owner="validation_framework",
        acceptance_criteria=_ACCEPTANCE_CRITERIA,
        validation="final_build",
    )

    acc_id = "acceptance"
    tasks[acc_id] = Task(
        id=acc_id, kind=TaskKind.ACCEPTANCE,
        inputs=("validation_report",), outputs=("build_decision",),
        dependencies=(val_id,),
        owner="lifecycle",
        acceptance_criteria=("lifecycle advances to COMPLETE",),
        validation="final_build",
    )

    order = _topo_order(tasks)
    # spec_checksum: same canonical form the manifest records for ``build_spec``
    # (sha256 of the spec_text UTF-8 bytes) so manifest.plan_matches_spec can
    # compare plan.spec_checksum to manifest.checksum_of("build_spec") directly.
    spec_checksum = ""
    if isinstance(spec, dict):
        spec_text_for_ck = spec.get("spec_text", "") or ""
        spec_checksum = hashlib.sha256(
            spec_text_for_ck.encode("utf-8")).hexdigest()
    return ExecutionPlan(
        tasks=tasks, order=order,
        build_spec_artifact_id=build_spec_artifact_id,
        spec_checksum=spec_checksum,
    )


def plan_from_workspace(workspace_id: str) -> ExecutionPlan:
    """Load the stored implementation_plan.md-derived spec for a workspace and
    plan it. Reads the resurrected ``decisions.json`` so the canonical model is
    the source of truth (never re-parses the markdown)."""
    import json
    from build_workspace import _workspace_dir

    p = _workspace_dir(workspace_id) / "decisions.json"
    spec: dict = {}
    if p.is_file():
        try:
            spec = json.loads(p.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            spec = {}
    # Shape the dict the way build_spec_engine.build_spec returns it so the
    # planner's component matcher sees verification_result + verified_decisions.
    return plan({
        "verification_result": spec.get("verification_result") or {},
        "verified_decisions": spec.get("verified_decisions") or [],
        "spec_text": "",  # not needed for decomposition
    })


# ---------------------------------------------------------------------------
# Part 2: ExecutionPlanArtifact persistence (Final Architecture Hardening).
#
# Writes execution_plan.json to the workspace root, beside implementation_plan.md
# / build_state.json / decisions.json. The persisted object is the artifact
# envelope (id / checksum / version / producer / lifecycle_stage / dependencies /
# validation_status / payload). The payload is plan.to_dict() (the structured
# task graph + topological order). The id is deterministic over (kind, checksum),
# so re-deriving an identical plan yields the same id — resume reuses the
# stored file rather than regenerating.
# ---------------------------------------------------------------------------

ARTIFACT_KIND = "ExecutionPlanArtifact"
MANIFEST_LIFECYCLE_STAGE = "EXECUTION_ALLOWED"


def _artifact_envelope(plan_obj: "ExecutionPlan | dict", *,
                       producer: str = "execution_planner",
                       build_spec_artifact_id: str | None = None) -> dict:
    """Build the ExecutionPlanArtifact envelope around the plan payload."""
    payload = plan_obj.to_dict() if hasattr(plan_obj, "to_dict") else dict(plan_obj or {})
    plan_id = payload.get("id") or ""
    plan_checksum = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    artifact_id = str(uuid.uuid5(_PLAN_NAMESPACE, f"{ARTIFACT_KIND}:{plan_checksum}"))
    deps = []
    if build_spec_artifact_id:
        deps.append(build_spec_artifact_id)
    return {
        "kind": ARTIFACT_KIND,
        "id": artifact_id,
        "plan_id": plan_id,
        "checksum": plan_checksum,
        "version": 1,
        "producer": producer,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lifecycle_stage": MANIFEST_LIFECYCLE_STAGE,
        "validation_status": "unvalidated",
        "dependencies": deps,
        "payload": payload,
    }


_PLAN_NAMESPACE = uuid.UUID("b3c4d5e6-f7a8-4b90-8abc-def012345679")


def store_execution_plan(workspace_id: str, plan_obj: "ExecutionPlan | dict", *,
                         producer: str = "execution_planner",
                         build_spec_artifact_id: str | None = None) -> dict:
    """Persist the ExecutionPlanArtifact to <workspace>/execution_plan.json.

    The file is the artifact envelope (id/checksum/version/producer/lifecycle_stage/
    dependencies/validation_status) wrapping the structured payload (plan.to_dict()).
    Returns the envelope. Idempotent: re-storing the same plan produces the same
    file content (id + checksum deterministic over the payload).
    """
    from build_workspace import _workspace_dir
    envelope = _artifact_envelope(plan_obj, producer=producer,
                                  build_spec_artifact_id=build_spec_artifact_id)
    p = _workspace_dir(workspace_id) / "execution_plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(envelope, indent=2, default=str, ensure_ascii=False),
                 encoding="utf-8")
    return envelope


def load_execution_plan(workspace_id: str) -> dict | None:
    """Read the persisted ExecutionPlanArtifact envelope. None if absent/corrupt."""
    from build_workspace import _workspace_dir
    if not workspace_id:
        return None
    p = _workspace_dir(workspace_id) / "execution_plan.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")) or None
    except (json.JSONDecodeError, OSError):
        return None


def execution_plan_artifact_id(workspace_id: str) -> str | None:
    """The artifact id of the persisted ExecutionPlanArtifact, or None."""
    env = load_execution_plan(workspace_id) or {}
    return env.get("id")


def verify_execution_plan_checksum(envelope: dict) -> bool:
    """True iff the persisted envelope's checksum matches a re-derived checksum
    of its payload. Used by validate_manifest to detect tampering / drift."""
    if not isinstance(envelope, dict):
        return False
    payload = envelope.get("payload")
    if payload is None:
        return False
    expected = envelope.get("checksum", "")
    actual = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return bool(expected) and expected == actual


# ---------------------------------------------------------------------------
# Part 4: Task graph export — JSON / Mermaid / DOT (diagnostic only).
#
# Each export is a pure function turning an ExecutionPlan (or plan.to_dict())
# into a string. No runtime depends on these; they exist for debugging,
# visualization, and future scheduling. Determinism is preserved: same plan
# -> byte-identical export text.
# ---------------------------------------------------------------------------

def _plan_dict(plan_obj: "ExecutionPlan | dict") -> dict:
    if isinstance(plan_obj, dict):
        return plan_obj
    return plan_obj.to_dict()


def export_json(plan_obj: "ExecutionPlan | dict") -> str:
    """JSON export of the task graph. Indented, sorted keys for stability."""
    return json.dumps(_plan_dict(plan_obj), indent=2, sort_keys=True,
                      default=str, ensure_ascii=False)


def export_mermaid(plan_obj: "ExecutionPlan | dict") -> str:
    """Mermaid graph diagram for the task graph. Output:
        graph TD
          task_id1[task_id1]
          task_id2[task_id2]
          task_id1 --> task_id2
    Pure; deterministic. No special edge labels (the downstream node carries
    the validation stage; the edge carries the dependency direction only)."""
    d = _plan_dict(plan_obj)
    tasks = d.get("tasks") or {}
    if not tasks:
        return "graph TD\n  (empty)\n"
    out = ["graph TD"]
    for tid in tasks:
        safe = tid.replace("[", "_").replace("]", "_")
        out.append(f"  {safe}[{safe}]")
    for tid, t in tasks.items():
        tid_safe = tid.replace("[", "_").replace("]", "_")
        for dep in (t.get("dependencies") or []):
            dep_safe = dep.replace("[", "_").replace("]", "_")
            out.append(f"  {dep_safe} --> {tid_safe}")
    return "\n".join(out) + "\n"


def export_dot(plan_obj: "ExecutionPlan | dict") -> str:
    """Graphviz DOT graph for the task graph."""
    d = _plan_dict(plan_obj)
    tasks = d.get("tasks") or {}
    out = ["digraph G {"]
    if not tasks:
        out.append("  // empty plan")
        out.append("}")
        return "\n".join(out) + "\n"
    for tid, t in tasks.items():
        kind = t.get("kind", "task")
        label = tid.replace('"', '\\"')
        out.append(f'  "{label}" [label="{label}\\n({kind})"];')
    for tid, t in tasks.items():
        tid_safe = tid.replace('"', '\\"')
        for dep in (t.get("dependencies") or []):
            dep_safe = dep.replace('"', '\\"')
            out.append(f'  "{dep_safe}" -> "{tid_safe}";')
    out.append("}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    # ponytail self-check: determinism, DAG validity, resume readiness.
    vr_156 = {
        "resolved_claims": [{
            "normalized_key": "database_technology_selection",
            "dispute": "Database Technology: GPT selects PostgreSQL, Gemini selects SQLite",
            "verification": {"status": "RESOLVED", "confidence": 95,
                             "finding": ("Both evidence strips conclude SQLite is the "
                                         "appropriate persistence engine; the text "
                                         "argues against PostgreSQL.")},
        }],
    }
    spec = {
        "verification_result": vr_156,
        "verified_decisions": [{
            "outcome_type": "VERIFIED_DECISION",
            "dispute_key": "database_technology_selection",
            "decision": "SQLite as primary persistence engine",
        }],
        "spec_text": "# Architecture\n...\n# Components\n...\n",
    }
    p1 = plan(spec)
    p2 = plan(spec)
    assert p1.to_dict() == p2.to_dict(), "planner not deterministic"
    assert p1.order == p2.order and p1.id == p2.id

    # The plan is a valid DAG (passes the validator).
    from validation import validate_execution_plan
    rep = validate_execution_plan(p1.to_dict())
    assert rep.passed, rep.to_dict()

    # Order respects dependencies: components before decisions before tests/
    # integration, both before validation, validation before acceptance.
    pos = {tid: i for i, tid in enumerate(p1.order)}
    for tid, t in p1.tasks.items():
        for d in t.dependencies:
            assert pos[d] < pos[tid], f"order violates {d} -> {tid}: {p1.order}"
    assert p1.order[-1] == "acceptance", p1.order

    # Resume readiness: nothing ready until the first component completes; then
    # dependents become ready progressively.
    assert p1.ready() != []
    # Marking all components DONE makes decisions + integration + (eventually)
    # tests ready.
    done_comp = {tid: __import__("dataclasses").replace(t, status=TaskStatus.DONE)
                 for tid, t in p1.tasks.items() if t.kind is TaskKind.COMPONENT}
    tasks2 = {**p1.tasks, **done_comp}
    p1b = __import__("dataclasses").replace(p1, tasks=tasks2)
    ready_after = set(p1b.ready())
    assert "integration" in ready_after, ready_after

    # Empty spec still yields a non-empty plan (fallback IMPLEMENTATION task).
    p_empty = plan({})
    assert p_empty.tasks and validate_execution_plan(p_empty.to_dict()).passed

    # Persistence (Part 2): store/load round-trip; deterministic id; checksum
    # verification.
    import tempfile
    from build_workspace import WORKSPACE_ROOT as _R0
    import build_workspace as _bw
    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = __import__("pathlib").Path(tmp)
        wid = _bw.create_workspace(source_question="x")
        env = store_execution_plan(wid, p1, producer="execution_planner")
        assert env["kind"] == ARTIFACT_KIND
        assert env["lifecycle_stage"] == "EXECUTION_ALLOWED"
        assert env["validation_status"] == "unvalidated"
        loaded = load_execution_plan(wid)
        assert loaded is not None
        assert loaded["id"] == env["id"]
        assert loaded["payload"]["order"] == env["payload"]["order"]
        assert execution_plan_artifact_id(wid) == env["id"]
        assert verify_execution_plan_checksum(loaded) is True
        # Re-store with the same plan -> deterministic id (resume reuse).
        env2 = store_execution_plan(wid, p1)
        assert env2["id"] == env["id"] and env2["checksum"] == env["checksum"]
        # load returns None for unknown workspace.
        assert load_execution_plan("nope-id") is None

        # Exports (Part 4): all three formats non-empty + deterministic.
        json_text = export_json(p1)
        mermaid_text = export_mermaid(p1)
        dot_text = export_dot(p1)
        assert _plan_dict(p1)["order"] == json.loads(json_text)["order"]
        assert "graph TD" in mermaid_text and "-->" in mermaid_text
        assert "digraph G" in dot_text and "->" in dot_text
        assert export_json(p1) == export_json(p1)
        assert export_mermaid(p1) == export_mermaid(p1)
        # Exports handle empty plans.
        assert "graph TD" in export_mermaid({})
        assert "digraph G" in export_dot({})
        _bw.WORKSPACE_ROOT = _R0
    print("execution_planner smoke test PASSED")
