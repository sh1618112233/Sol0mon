"""
Project Sol0mon - Build Planner (staged FULL_BUILD decomposition).

Replaces the monolithic "hand the whole spec to one coding-agent call" step
with a dependency-aware staged plan. Stages are NOT hardcoded — they are
derived from the architecture via the EXISTING ``execution_planner.plan``
task DAG (single source of truth). ``execution_planner`` already decomposes
a build spec into a DAG of Component / Decision / Tests / Integration /
Validation / Acceptance tasks with deterministic dependencies; this module
groups those tasks into ENGINEERING STAGES along logical software
boundaries, then writes ``build_plan.json`` and seeds
``stage_status.json``.

# Why reuse execution_planner?

Re-implementing the DAG decomposer here would duplicate the canonical
component-matching logic (``decision_renderer._architecture_components``)
that ``build_spec_engine`` and ``execution_planner`` already agree on. The
single source of truth is the architecture (the verification result); both
the build spec and the task DAG are derivable from it deterministically.
This module therefore DERIVES, not re-implements.

# Stage composition (deterministic)

Given the task DAG, the planner groups tasks into stages using a fixed,
architecture-driven policy:

    Stage 1 .. N           : one COMPONENT task per stage (each component
                              is its own stage; isolation > parallelism in
                              the current model — ponytail: a parallel
                              scheduler can re-group these freely since the
                              DAG is the sole ordering constraint)
    Stage "decisions"      : all DECISION tasks (each may depend on multiple
                              components; grouped because each decision is
                              a constraint applied across components)
    Stage "tests"          : the TESTS task (depends on all components +
                              decisions; produces the test suite)
    Stage "integration"    : the INTEGRATION task (depends on all components;
                              cross-component wiring verification)
    Stage "validation"    : the VALIDATION task (depends on tests + integration
                              — overall build-quality gate, runs the central
                              validators; this is the per-stage-GLOBAL gate,
                              distinct from the per-stage validation each
                              implementation stage runs locally)
    Stage "acceptance"     : the ACCEPTANCE task (terminal — lifecycle
                              advance to COMPLETE on pass)

Stages respect the DAG: a stage's ``dependencies`` are the union of its
tasks' dependencies mapped to the containing stage ids. The executor
honors these dependencies (``stage_status.next_pending_stage`` only returns
a stage whose dependencies are all DONE).

# Fallback

If ``execution_planner.plan`` returns the empty-component fallback (a
single ``comp_00_implementation`` task), the planner produces a
SINGLE-STAGE plan that wraps the full spec — the staged executor then
degenerates to the legacy single-shot path. This is the bridge that keeps
backward-compat with non-component-driven specs while still emitting
build_plan.json so resume + the stage-level files are populated
consistently.

Public API:

    plan_staged_build(workspace_id, spec_result=None, plan_obj=None) -> dict
    stage_manifest_for(workspace_id) -> dict   # diagnostic / for the finalizer

Pure stdlib. ``execution_planner.plan`` is reused (no LLM); ``build_spec_engine``
is reused to read the persisted spec when needed. No I/O beyond reading
``decisions.json`` / ``implementation_plan.md`` / ``execution_plan.json``
(whichever is present) and writing ``build_plan.json`` through
``stage_status.write_build_plan`` (sole writer).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import stage_status
from stage_status import StageStatus


# ---------------------------------------------------------------------------
# Helpers (deterministic)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _load_spec_result(workspace_id: str) -> dict:
    """Read the canonical decision model + synthesized answer from the
    workspace. The build_spec_engine consumes deterministically from
    ``decisions.json`` + ``answer.json`` + the persisted spec; we mirror
    that so the planner can re-run ``execution_planner.plan`` without
    forcing a re-debate. Piggybacks on the same pattern ``resume.py`` uses
    (``_shape_source_result``) but inlined here so the planner has no
    dependency on resume's private helpers."""
    from build_workspace import _workspace_dir

    ws = _workspace_dir(workspace_id)
    decisions: dict = {}
    p = ws / "decisions.json"
    if p.is_file():
        try:
            decisions = json.loads(p.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            decisions = {}
    answer_text = ""
    p = ws / "answer.json"
    if p.is_file():
        try:
            answer_text = (json.loads(p.read_text(encoding="utf-8"))
                           or {}).get("synthesized_answer", "") or ""
        except (json.JSONDecodeError, OSError):
            answer_text = ""
    if not answer_text:
        p = ws / "synthesized_answer.md"
        if p.is_file():
            try:
                answer_text = p.read_text(encoding="utf-8")
            except OSError:
                answer_text = ""
    return {
        "verified_decisions": decisions.get("verified_decisions") or [],
        "verified_tradeoffs": [],
        "human_required": [],
        "verification_result": decisions.get("verification_result") or {},
        "resolved_decisions": decisions.get("resolved_decisions") or [],
        "synthesized_answer": answer_text,
    }


def _load_spec_text(workspace_id: str) -> str:
    """Read the persisted ``implementation_plan.md``. Empty string when absent."""
    from build_spec_engine import load_implementation_spec
    return load_implementation_spec(workspace_id) or ""


# ---------------------------------------------------------------------------
# Stage composition
# ---------------------------------------------------------------------------


def _stage_prefix(plan_obj, default_prefix: str) -> str:
    """A short stable prefix for stage ids (used in stage ids and
    checkpoints/<id>.json file names). Defaults to the plan id when
    available (the deterministic uuid5-derived plan id), else 'sbuild'."""
    plan_id = ""
    try:
        plan_id = (plan_obj.to_dict() if hasattr(plan_obj, "to_dict")
                   else dict(plan_obj or {})).get("id", "")
    except Exception:
        pass
    return plan_id[:12] or default_prefix


def _slice_files_to_create_for(spec_text: str, files: list[str], stage_id: str = "") -> str:
    """Build a per-stage spec by SCOPING the original ``# Files to Create``
    section to only ``files``. The other executable sections (# Implementation
    Objective, # Implementation Steps, # Acceptance Criteria, # Output
    Contract, # Files to Modify) are PRESERVED VERBATIM — the agent keeps
    full context; we only narrow the file-creation directive.

    Implementation choice (ponytail): a real parser would be more robust but
    fragile if the spec structure shifts. The deterministic ``-x IN <files>``
    directive at the top is the smallest thing that works AND is observable
    (the agent reads the directive and the file list). Belt-and-braces: we
    also keep the full ``# Files to Create`` section as a reference, so the
    agent sees both the full list and the explicit "implement ONLY" directive.
    The ``stage_id`` is stamped into the directive header so the agent can
    name stage-specific files deterministically — the production coding agent
    honors this without a parser (the directive text names the stage).
    """
    file_block = "\n".join(f"- `{f}`" for f in files) or "- (no files declared for this stage)"
    stage_header = f" (stage_id={stage_id})" if stage_id else ""
    return (
        f"# STAGED IMPLEMENTATION DIRECTIVE{stage_header}\n\n"
        f"This is stage {len(files)} file(s) of an N-stage build. "
        f"Implement **ONLY** the files below. Files from prior stages are "
        f"already complete; do NOT regenerate or modify them. Tests for "
        f"this stage write to the ``tests/`` directory if applicable.\n\n"
        f"## Files for this stage\n{file_block}\n\n"
        f"---\n\n"
        f"# ORIGINAL FULL SPEC (context — for the agent's reference)\n\n"
        f"{spec_text}"
    )


def _stage_id(prefix: str, idx: int, title: str) -> str:
    """Deterministic stage id: ``stage_<NN>_<sanitized-title>``."""
    import re
    safe = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_") or "stage"
    return f"stage_{idx:02d}_{safe[:40]}"


def compose_stages(plan_obj, spec_text: str) -> list[dict]:
    """Given an ``execution_planner.ExecutionPlan`` (or plan-dict) and the
    full spec text, produce the staged-plan's ``stages`` array. Each stage::

        {
          "stage_id":       str (deterministic),
          "title":          str,
          "kind":           "component" | "decisions" | "tests" | ...,
          "task_ids":       [str, ...],
          "dependencies":   [stage_id, ...]   # of OTHER stages, not of tasks,
                                              # so the executor's "deps done"
                                              # check is O(stages) not O(tasks)
          "files":          [str, ...]        # the # Files to Create entries
                                              # this stage is allowed to produce
          "stage_spec_excerpt": str          # the directive + relevant slice
          "validation":     [str, ...]        # validator stage names per the
                                              # stage's task `validation` fields
        }

    Stage composition (fixed policy — deterministic):

      * Each COMPONENT task -> its own stage. The stage's ``files`` is the
        task's ``outputs`` (paths under ``src/<slug>/``).
      * All DECISION tasks -> grouped into one ``decisions`` stage (one
        cross-cutting constraint-application step). Deps: every component
        stage.
      * TESTS task -> a ``tests`` stage. Deps: every component + the decisions stage.
      * INTEGRATION task -> an ``integration`` stage. Deps: every component.
      * VALIDATION task -> a ``validation`` stage. Deps: tests + integration.
      * ACCEPTANCE task -> an ``acceptance`` stage. Deps: validation.

    Stage IDs are assigned in a single linear pass so cross-stage
    dependencies can reference the assigned ids directly (no placeholder
    fix-up step). When a grouping is empty (e.g. no decisions), no stage
    is allocated for it AND downstream stages' deps don't reference it.
    """
    plan_dict = plan_obj.to_dict() if hasattr(plan_obj, "to_dict") else dict(plan_obj or {})
    tasks = plan_dict.get("tasks") or {}
    prefix = _stage_prefix(plan_obj, "sbuild")

    # Bucket tasks by kind, preserving dict (insertion) order.
    components: list[tuple[str, dict]] = []
    decisions: list[tuple[str, dict]] = []
    tests_tasks: list[tuple[str, dict]] = []
    integration_tasks: list[tuple[str, dict]] = []
    validation_tasks: list[tuple[str, dict]] = []
    acceptance_tasks: list[tuple[str, dict]] = []

    def _kind(t):
        if isinstance(t, dict):
            return t.get("kind") or ""
        return (t.kind.value if hasattr(getattr(t, "kind", None), "value")
                else str(getattr(t, "kind", "")))

    def _field(t, name):
        if isinstance(t, dict):
            return t.get(name) or []
        return list(getattr(t, name, ()) or [])

    for tid, task in tasks.items():
        k = _kind(task)
        if k == "component":
            components.append((tid, task))
        elif k == "decision":
            decisions.append((tid, task))
        elif k == "tests":
            tests_tasks.append((tid, task))
        elif k == "integration":
            integration_tasks.append((tid, task))
        elif k == "validation":
            validation_tasks.append((tid, task))
        elif k == "acceptance":
            acceptance_tasks.append((tid, task))

    stages: list[dict] = []
    counter = 0

    def _next_stage_id(label: str) -> str:
        nonlocal counter
        sid = _stage_id(prefix, counter, label)
        counter += 1
        return sid

    # 1. Each component -> its own stage (no deps).
    comp_stage_ids: list[str] = []
    for tid, task in components:
        outputs = _field(task, "outputs")
        files = [o for o in outputs if o and not o.endswith("/")] or list(outputs)
        title_raw = tid.split("_", 2)[-1] if "_" in tid else tid
        title = title_raw.replace("_", " ").title() or f"Component"
        sid = _next_stage_id(title)
        comp_stage_ids.append(sid)
        validation = _field(task, "validation") or []
        stages.append({
            "stage_id": sid,
            "title": title,
            "kind": "component",
            "task_ids": [tid],
            "dependencies": [],
            "files": files,
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, files, sid),
            "validation": [v for v in validation if v],
        })

    # 2. Decisions (grouped). Deps: every component stage.
    decisions_id = ""
    if decisions:
        decisions_id = _next_stage_id("Decisions")
        files = ["tests/test_decisions.py"]
        stages.append({
            "stage_id": decisions_id,
            "title": "Decisions",
            "kind": "decisions",
            "task_ids": [tid for tid, _ in decisions],
            "dependencies": list(comp_stage_ids),
            "files": files,
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, files, decisions_id),
            "validation": ["build_spec"],
        })

    # 3. Tests. Deps: components + decisions.
    tests_id = ""
    if tests_tasks:
        tests_id = _next_stage_id("Tests")
        files = ["tests/conftest.py", "tests/test_components.py"]
        deps = list(comp_stage_ids) + ([decisions_id] if decisions_id else [])
        stages.append({
            "stage_id": tests_id,
            "title": "Tests",
            "kind": "tests",
            "task_ids": [tid for tid, _ in tests_tasks],
            "dependencies": deps,
            "files": files,
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, files, tests_id),
            "validation": ["tests"],
        })

    # 4. Integration. Deps: components.
    integration_id = ""
    if integration_tasks:
        integration_id = _next_stage_id("Integration")
        files = ["tests/test_integration.py"]
        stages.append({
            "stage_id": integration_id,
            "title": "Integration",
            "kind": "integration",
            "task_ids": [tid for tid, _ in integration_tasks],
            "dependencies": list(comp_stage_ids),
            "files": files,
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, files, integration_id),
            "validation": [],
        })

    # 5. Validation. Deps: tests + integration (whichever exist).
    validation_id = ""
    if validation_tasks:
        validation_id = _next_stage_id("Validation")
        deps: list[str] = []
        if tests_id:
            deps.append(tests_id)
        if integration_id:
            deps.append(integration_id)
        files = ["reports/validation_report.json"]
        stages.append({
            "stage_id": validation_id,
            "title": "Validation",
            "kind": "validation",
            "task_ids": [tid for tid, _ in validation_tasks],
            "dependencies": deps,
            "files": files,
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, files, validation_id),
            "validation": ["final_build"],
        })

    # 6. Acceptance. Deps: validation.
    if acceptance_tasks:
        deps = [validation_id] if validation_id else []
        files = ["reports/acceptance_report.json"]
        sid = _next_stage_id("Acceptance")
        stages.append({
            "stage_id": sid,
            "title": "Acceptance",
            "kind": "acceptance",
            "task_ids": [tid for tid, _ in acceptance_tasks],
            "dependencies": deps,
            "files": files,
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, files, sid),
            "validation": ["final_build"],
        })

    # Edge case: empty task DAG -> single fallback stage wrapping the full
    # spec. The staged executor then runs ``implementation_executor`` once
    # over the whole spec — behavior matches the legacy single-shot build
    # for greenfield specs the architecture matcher couldn't decompose.
    if not stages:
        sid = _stage_id(prefix, 0, "Implementation")
        stages = [{
            "stage_id": sid,
            "title": "Implementation",
            "kind": "implementation",
            "task_ids": list(tasks.keys()) or ["comp_00_implementation"],
            "dependencies": [],
            "files": [],
            "stage_spec_excerpt": _slice_files_to_create_for(spec_text, [], sid),
            "validation": ["final_build"],
        }]
    return stages


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def plan_staged_build(
    workspace_id: str,
    spec_result: dict | None = None,
    plan_obj=None,
) -> dict:
    """Decompose the workspace's build spec into engineering stages and
    persist the staged plan + an empty per-stage status map.

    Args:
        workspace_id:   The canonical workspace id (UUID).
        spec_result:    Optional pre-loaded spec_result (the build pipeline
                        already has one). When None, loads from
                        ``decisions.json`` + ``answer.json`` (canonical
                        source of truth).
        plan_obj:        Optional pre-computed ``execution_planner.plan``
                        result. When None, computed from ``spec_result``.

    Returns:
        The staged plan dict (also persisted to ``<workspace>/build_plan.json``).
        Shape::

            {
              "plan_id":   "<sha256 over stages>",
              "generated_at": "<iso>",
              "workspace_id": "...",
              "source_spec_checksum": "<sha256 of implementation_plan.md>",
              "execution_plan_id": "<the underlying execution_plan.json id>",
              "stage_count": N,
              "stages": [ {stage dict...}, ... ]
            }

    The staged plan is NOT executed here — the ``staged_executor`` owns that.
    Persisting here ONLY:
      * ``build_plan.json``  (via ``stage_status.write_build_plan``)
      * ``stage_status.json`` (empty seed: one PENDING entry per stage)
    """
    spec_text = _load_spec_text(workspace_id)
    if spec_result is None:
        spec_result = _load_spec_result(workspace_id)
    if plan_obj is None:
        from execution_planner import plan as _plan
        plan_obj = _plan(spec_result)

    stages = compose_stages(plan_obj, spec_text)
    spec_checksum = _sha256_text(spec_text)
    plan_dict = plan_obj.to_dict() if hasattr(plan_obj, "to_dict") else dict(plan_obj or {})
    execution_plan_id = plan_dict.get("id", "")

    # Deterministic plan_id: hash of stage identities (stage_id + kind +
    # dependencies + files). Stable across re-runs so resume can detect
    # "the staged plan matches the spec on disk" exactly.
    plan_id_input = "".join(
        f"{s['stage_id']}|{s['kind']}|{','.join(s.get('dependencies') or [])}|"
        f"{','.join(s.get('files') or [])}"
        for s in stages
    ) + f"|{spec_checksum}|{execution_plan_id}"
    plan_id = f"stage_{hashlib.sha256(plan_id_input.encode('utf-8')).hexdigest()[:16]}"

    manifest_payload = {
        "plan_id": plan_id,
        "generated_at": _now_iso(),
        "workspace_id": workspace_id,
        "source_spec_checksum": spec_checksum,
        "execution_plan_id": execution_plan_id,
        "stage_count": len(stages),
        "stages": stages,
    }

    stage_status.write_build_plan(workspace_id, manifest_payload)

    # Seed stage_status.json with one PENDING record per stage. Only writes
    # the records that don't already exist — preserves resume state for
    # already-started stages.
    current_status = stage_status.read_stage_status(workspace_id)
    existing = (current_status.get("stages") or {}) if isinstance(current_status, dict) else {}
    for stage in stages:
        sid = stage["stage_id"]
        if sid not in existing:
            stage_status.write_stage_status(
                workspace_id, sid, StageStatus.PENDING,
                title=stage.get("title", ""),
                kind=stage.get("kind", ""),
                attempts=0,
            )
    return manifest_payload


def stage_manifest_for(workspace_id: str) -> dict:
    """Diagnostic: the persisted staged plan as written, plus the current
    per-stage status. Used by the build finalizer (read-only — the
    finalizer does NOT write back) and the resume UI."""
    return {
        "plan": stage_status.read_build_plan(workspace_id),
        "status": stage_status.read_stage_status(workspace_id),
    }


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    import build_workspace as _bw

    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)

        # Build a workspace with the canonical decision model planted. Two
        # platform components (Temporal / PostgreSQL) are matched from the
        # winner_text of the resolved claims; this is the realistic shape
        # the production pipeline produces via the verify + governance steps.
        wid = _bw.create_workspace(source_question="Build X")
        decisions = {
            "answer_id": 1,
            "verification_result": {
                "resolved_claims": [
                    {"normalized_key": "orchestration",
                     "dispute": "Orchestration: Airflow vs Temporal",
                     "verification": {"status": "RESOLVED", "confidence": 95,
                                      "finding": "The verification concludes Temporal as the orchestration engine; Airflow rejected."}},
                    {"normalized_key": "persistence",
                     "dispute": "Database: MySQL vs PostgreSQL",
                     "verification": {"status": "RESOLVED", "confidence": 92,
                                      "finding": "The verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
                ],
                "equivalent_claims": [],
                "unresolved_claims": [],
                "verification_score": 100,
                "truncated_count": 0,
            },
            "verified_decisions": [
                {"outcome_type": "VERIFIED_DECISION",
                 "dispute_key": "orchestration",
                 "decision": "Temporal orchestration",
                 "reasoning": ""},
                {"outcome_type": "VERIFIED_DECISION",
                 "dispute_key": "persistence",
                 "decision": "PostgreSQL primary datastore",
                 "reasoning": ""},
            ],
            "resolved_decisions": [],
        }
        (_bw._workspace_dir(wid) / "decisions.json").write_text(
            json.dumps(decisions, indent=2), encoding="utf-8")
        (_bw._workspace_dir(wid) / "answer.json").write_text(
            json.dumps({"synthesized_answer": "Use Temporal+PostgreSQL"}), encoding="utf-8")
        _bw._workspace_dir(wid).joinpath("implementation_plan.md").write_text(
            "# Implementation Objective\nBuild the platform\n\n"
            "# Files to Create\n- `src/orchestration/temporal.py`\n"
            "- `src/persistence/postgres.py`\n- `tests/test_app.py`\n\n"
            "# Files to Modify\n(none)\n\n"
            "# Implementation Steps\n1. Build orchestration\n2. Build persistence\n\n"
            "# Acceptance Criteria\n- pytest passes\n\n"
            "# Output Contract\nFiles written under src/.\n",
            encoding="utf-8")

        manifest = plan_staged_build(wid)
        stages = manifest["stages"]
        # 2 components + 1 decisions + 1 tests + 1 integration + 1
        # validation + 1 acceptance = 7 stages.
        assert manifest["stage_count"] == 7, manifest

        # components come first, each its own stage.
        comp_stages = [s for s in stages if s["kind"] == "component"]
        assert len(comp_stages) == 2, [s["kind"] for s in stages]
        assert all(s["dependencies"] == [] for s in comp_stages)
        # Followed by decisions (deps = both components); tests (deps = comp+
        # decisions); integration (deps = comp); validation (deps = tests+
        # integration); acceptance (deps = validation).
        by_kind = {s["kind"]: s for s in stages}
        assert set(by_kind["decisions"]["dependencies"]) == {s["stage_id"] for s in comp_stages}
        assert by_kind["acceptance"]["dependencies"] == [by_kind["validation"]["stage_id"]]

        # Deterministic: re-running yields byte-identical stage ids + plan id.
        manifest2 = plan_staged_build(wid)
        assert manifest2["plan_id"] == manifest["plan_id"]
        assert [s["stage_id"] for s in manifest2["stages"]] == \
               [s["stage_id"] for s in manifest["stages"]]

        # stage_status.json was seeded with PENDING records.
        for s in stages:
            rec = stage_status.read_stage_status(wid, s["stage_id"])
            assert rec.get("status") == "PENDING", (s["stage_id"], rec)
            assert rec.get("title") == s["title"]

        # Re-planning preserves already-started stages (only seeds PENDING for
        # stages that don't yet have a record).
        stage_status.write_stage_status(wid, comp_stages[0]["stage_id"],
                                         StageStatus.IMPLEMENTING)
        plan_staged_build(wid)  # idempotent re-plan
        rec = stage_status.read_stage_status(wid, comp_stages[0]["stage_id"])
        assert rec["status"] == StageStatus.IMPLEMENTING.value, rec

        # next_pending_stage respects deps.
        nxt = stage_status.next_pending_stage(wid)
        assert nxt["stage_id"] != comp_stages[0]["stage_id"], nxt
        # Mark first comp DONE; second comp still PENDING -> next is second comp.
        stage_status.write_stage_status(wid, comp_stages[0]["stage_id"], StageStatus.DONE)
        if len(comp_stages) > 1:
            nxt = stage_status.next_pending_stage(wid)
            assert nxt["stage_id"] == comp_stages[1]["stage_id"], nxt
            stage_status.write_stage_status(wid, comp_stages[1]["stage_id"], StageStatus.DONE)
        # All components done -> next is decisions stage (deps satisfied).
        nxt = stage_status.next_pending_stage(wid)
        assert nxt and nxt["kind"] == "decisions", nxt

        # Empty spec (no components, no decisions) — execution_planner
        # synthesizes a fallback component + the standard 4 cross-cutting
        # stages, so the greenfield case still produces a 5-stage plan
        # (impl -> tests -> integration -> validation -> acceptance) — that
        # IS correct staged behavior for a small project, not a single-stage
        # fallback. The true single-stage fallback only triggers when
        # ``compose_stages`` is called with NO tasks at all; verify that path
        # directly.
        wid2 = _bw.create_workspace(source_question="greenfield")
        _bw._workspace_dir(wid2).joinpath("decisions.json").write_text(
            json.dumps({"verification_result": {}, "verified_decisions": []}),
            encoding="utf-8")
        _bw._workspace_dir(wid2).joinpath("implementation_plan.md").write_text(
            "# Implementation Objective\nGreenfield build\n\n"
            "# Files to Create\n- `src/app.py`\n\n"
            "# Implementation Steps\n1. Build app\n\n"
            "# Acceptance Criteria\n- pytest passes\n\n"
            "# Output Contract\nFiles under src/.\n",
            encoding="utf-8")
        m2 = plan_staged_build(wid2)
        assert m2["stage_count"] == 5, m2  # fallback-comp + 4 cross-cutting stages

        # Direct call to compose_stages with an empty task DAG: must yield
        # the single fallback "implementation" stage (defensive path).
        empty_stages = compose_stages({"tasks": {}}, "spec text")
        assert len(empty_stages) == 1
        assert empty_stages[0]["kind"] == "implementation"

    print("build_planner smoke test PASSED")
