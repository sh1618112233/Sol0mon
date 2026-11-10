"""
Project Sol0mon - Per-Stage State Machine + Persistence.

The build-level lifecycle (``lifecycle.BuildLifecycle``) tracks the overall
build: NEW -> ... -> COMPLETE. This module is the STAGE-LEVEL overlay: each
engineering stage in a staged FULL_BUILD gets its own state, persisted to
``stage_status.json`` at the workspace root. Coexistence with the build
lifecycle:

    build_state.json        -> "lifecycle_stage": "EXECUTING"   (build-level)
                  +         -> "status":          "executed"     (legacy, kept)
    stage_status.json       -> "stages": {stage_id: {
                                  "status": "DONE",
                                  "attempts": 1,
                                  "last_checkpoint": "checkpoints/stage_01.json",
                                  ...
                              }}                               (stage-level)

The two are non-overlapping: a build at ``EXECUTING`` may have 5 stages of
which 3 are ``DONE`` and 2 are ``PENDING``. Resume reads ``stage_status.json``
first; if absent (legacy / single-stage workspace) it falls back to the
build-level state and the existing ``resume_build`` path takes over.

# State machine (per stage)

    PENDING -> IMPLEMENTING -> VALIDATING -> CHECKPOINTED -> DONE
                                 |               |
                                 v               v
                              REPAIRING        FAILED

    PENDING     : declared in the build plan, not yet started
    IMPLEMENTING: implementation_executor running the per-stage spec
    VALIDATING  : validation pipeline (static+compile+unit+integration) running
    REPAIRING   : a repair iteration in progress (continuous, bounded by the
                  stage's repair budget; each attempt is persisted to
                  ``repair_history.json`` with a fingerprint so the loop
                  makes progress, not just retries)
    CHECKPOINTED: per-stage checkpoint written to ``checkpoints/<stage_id>.json``
    DONE        : stage complete; never re-run by the staged executor
                  (only repair explicitly modifies existing stages; otherwise
                  they are immutably complete)
    FAILED      : autonomous repair could not make the stage pass; surfaced
                  through the build-level state (executable_error /
                  execution_error) AND a HUMAN_REQUIRED decision (the same
                  escalation contract ``implementation_executor`` uses)

# Files managed here

    <workspace>/build_plan.json              (the staged plan; written by
                                              build_planner, READ here)
    <workspace>/stage_status.json            (per-stage status; written here)
    <workspace>/repair_history.json         (per-stage repair attempts; written
                                              here by the staged executor)
    <workspace>/test_results.json           (per-stage test results; written
                                              here by the staged executor)
    <workspace>/checkpoints/<stage_id>.json (per-stage checkpoint; written here)

# Single source of truth

This module is the SOLE writer of ``stage_status.json``, ``repair_history.json``,
``test_results.json``, and ``checkpoints/<stage_id>.json``. The build
planner writes ``build_plan.json`` (not here) but the schema is owned jointly
so the planner and the executor agree on the stage identity. The build
finalizer is NOT bypassed; it reads the build-level lifecycle, not these
stage-level files, so PUBLISHED -> COMPLETE remains the finalizer's sole
ownership.

Pure stdlib. All I/O goes through ``build_workspace._workspace_dir`` so
test-time WORKSPACE_ROOT swaps are honored.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class StageStatus(str, enum.Enum):
    """Per-stage lifecycle. ``str`` enum so JSON-serialization and ``status in``
    checks against on-disk string values both work without conversion."""
    PENDING = "PENDING"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    CHECKPOINTED = "CHECKPOINTED"
    DONE = "DONE"
    FAILED = "FAILED"


# Legal forward transitions per stage. The executor consults this table
# before persisting a transition (defense-in-depth: an illegal stage
# transition is treated as a bug, not a runtime event — surfaced as a
# StageStatusError, caller writes a structured FAILED record).
STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.IMPLEMENTING, StageStatus.FAILED}),
    StageStatus.IMPLEMENTING: frozenset({
        StageStatus.VALIDATING, StageStatus.REPAIRING, StageStatus.FAILED,
    }),
    StageStatus.VALIDATING: frozenset({
        StageStatus.CHECKPOINTED, StageStatus.REPAIRING, StageStatus.FAILED,
    }),
    StageStatus.REPAIRING: frozenset({
        StageStatus.VALIDATING, StageStatus.FAILED,
    }),
    StageStatus.CHECKPOINTED: frozenset({StageStatus.DONE}),
    # DONE is terminal for the staged executor. Only an explicit repair
    # directive may re-enter a DONE stage — the executor itself never does.
    StageStatus.DONE: frozenset(),
    StageStatus.FAILED: frozenset({StageStatus.PENDING}),  # forced retry
}

TERMINAL_STAGE_STATUSES: frozenset[StageStatus] = frozenset({
    StageStatus.DONE, StageStatus.FAILED,
})


class StageStatusError(Exception):
    """Raised when an illegal stage transition is attempted. The caller may
    convert this into a structured FAILED record (no caller should crash on
    an unexpected stage transition — surfaces it instead)."""


def is_legal_stage_transition(current: StageStatus, target: StageStatus) -> bool:
    return target in STAGE_TRANSITIONS.get(current, frozenset())


def stage_transition(current: StageStatus | str, target: StageStatus | str) -> StageStatus:
    cur = current if isinstance(current, StageStatus) else StageStatus(str(current))
    tgt = target if isinstance(target, StageStatus) else StageStatus(str(target))
    if not is_legal_stage_transition(cur, tgt):
        raise StageStatusError(f"illegal stage transition: {cur.value} -> {tgt.value}")
    return tgt


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8")) or default
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str),
                    encoding="utf-8")


def _workspace_path(workspace_id: str, *parts: str):
    from build_workspace import _workspace_dir
    return _workspace_dir(workspace_id).joinpath(*parts)


# --- build_plan.json ------------------------------------------------------ #


def read_build_plan(workspace_id: str) -> dict:
    """Read the staged plan from ``<workspace>/build_plan.json``.

    Returns ``{"stages": []}`` when the file is absent OR corrupt OR the
    workspace is a legacy single-stage one (backward-compat: caller treats
    empty ``stages`` as "no staged plan -> fall back to single-shot executor").
    """
    return _read_json(_workspace_path(workspace_id, "build_plan.json"),
                      {"stages": [], "generated_at": None, "plan_id": ""})


def write_build_plan(workspace_id: str, plan: dict) -> bool:
    """Persist the staged plan. The build planner (sole producer) calls this
    once, after planning. Idempotent: re-writing the same plan produces
    byte-identical content (plan_id is deterministic over the plan payload)."""
    try:
        _write_json(_workspace_path(workspace_id, "build_plan.json"), plan)
        return True
    except OSError as e:
        print(f"[BUILD PLAN WRITE ERROR] {e}")
        return False


def has_staged_layout(workspace_id: str) -> bool:
    """True iff a staged build_plan.json exists with at least one stage.

    This is the bidirectional backward-compat gate: legacy workspaces (without
    the staged layout) return False, and callers (`staged_executor`,
    `resume`) fall back to the single-shot executor path. Workspaces created
    by the new FULL_BUILD path return True after `build_planner` has run.
    """
    plan = read_build_plan(workspace_id)
    return bool(plan.get("stages"))


# --- stage_status.json --------------------------------------------------- #


def read_stage_status(workspace_id: str, stage_id: str | None = None) -> dict:
    """Read all per-stage status records, or one stage's record when
    ``stage_id`` is provided. Returns ``{}`` when absent (legacy workspace)."""
    blob = _read_json(_workspace_path(workspace_id, "stage_status.json"),
                     {"stages": {}, "updated_at": None})
    stages = blob.get("stages") or {}
    if stage_id is None:
        return blob
    return stages.get(stage_id, {})


def write_stage_status(
    workspace_id: str,
    stage_id: str,
    status: StageStatus | str,
    **extra: Any,
) -> dict:
    """Merge ``status`` and any keyword fields into one stage's record.
    Preserves the other stages' records and existing fields across updates.

    The on-disk shape is::

        {
          "stages": {
            "<stage_id>": {
              "status": "DONE",
              "attempts": 1,
              "started_at": "...", "completed_at": "...",
              "last_checkpoint": "checkpoints/<stage_id>.json",
              <extra>
            },
            ...
          },
          "updated_at": "..."
        }

    The stage's ``status`` field is a StageStatus string. ``attempts`` is
    initialized to 0 on the first write and incremented by the staged
    executor via the ``attempts_increment`` keyword argument (NEVER reset to
    zero — that would erase the resume checkpoint).

    Returns the merge result for the single stage (so the caller can chain
    reads/writes without re-reading the whole blob).
    """
    status_str = status.value if isinstance(status, StageStatus) else str(status)
    path = _workspace_path(workspace_id, "stage_status.json")
    blob = _read_json(path, {"stages": {}, "updated_at": None})
    stages = blob.setdefault("stages", {})
    record = stages.get(stage_id, {})
    # First write initializes attempts to 0; subsequent writes preserve it
    # unless ``attempts`` is passed explicitly.
    if "attempts" not in record:
        record["attempts"] = 0
    record["status"] = status_str
    record["updated_at"] = _now_iso()
    if "started_at" not in record and status_str == StageStatus.IMPLEMENTING.value:
        record["started_at"] = record["updated_at"]
    if status_str in (StageStatus.DONE.value, StageStatus.FAILED.value) \
            and not record.get("completed_at"):
        record["completed_at"] = record["updated_at"]
    if "attempts_increment" in extra:
        record["attempts"] = int(record.get("attempts", 0)) + int(extra.pop("attempts_increment"))
    record.update(extra)
    stages[stage_id] = record
    blob["stages"] = stages
    blob["updated_at"] = _now_iso()
    _write_json(path, blob)
    return record


def next_pending_stage(workspace_id: str) -> dict | None:
    """Return the next stage to execute (or None if all stages are DONE /
    there are no stages). Honors dependencies: a stage is eligible only when
    every stage in its ``dependencies`` list is DONE.

    Returns the full stage dict (from ``build_plan.json``) so the caller
    (the staged executor) gets the per-stage spec / file list without an
    extra read. Two steady-state results:
      * All stages DONE -> None (the bundle is ready for integration
        validation; the executor owns the integration step).
      * One+ stages PENDING but each has at least one incomplete dep -> None
        with reason "dependency_blocked" (the executor surfaces this as a
        build-level FAILED — a build plan with an unsatisfiable dependency
        graph is a build-PLANNER bug, not a runtime event).
    """
    plan = read_build_plan(workspace_id)
    stages = plan.get("stages") or []
    if not stages:
        return None
    statuses = read_stage_status(workspace_id)
    by_id: dict[str, dict] = {s.get("stage_id", ""): s for s in stages}
    stage_status_map: dict[str, str] = (statuses.get("stages") or {}) \
        if isinstance(statuses, dict) and "stages" in statuses else statuses
    done_ids = {
        sid for sid, rec in stage_status_map.items()
        if rec.get("status") == StageStatus.DONE.value
    }
    for stage in stages:
        sid = stage.get("stage_id", "")
        rec_status = (stage_status_map.get(sid, {}) or {}).get("status")
        if rec_status and rec_status != StageStatus.PENDING.value:
            # Already started / finished; never re-run by the executor.
            continue
        if all(dep in done_ids for dep in (stage.get("dependencies") or [])):
            return stage
    return None


def all_stages_done(workspace_id: str) -> bool:
    """True iff every declared stage is DONE. False when: no stages declared
    (legacy), or any stage is not yet DONE."""
    plan = read_build_plan(workspace_id)
    stages = plan.get("stages") or []
    if not stages:
        return False
    statuses = (read_stage_status(workspace_id).get("stages") or {})
    for stage in stages:
        sid = stage.get("stage_id", "")
        if (statuses.get(sid, {}) or {}).get("status") != StageStatus.DONE.value:
            return False
    return True


# --- repair_history.json + test_results.json ------------------------------ #


def append_repair_history(
    workspace_id: str,
    stage_id: str,
    entry: dict,
) -> dict:
    """Append a single repair attempt for ``stage_id`` to ``repair_history.json``.
    The entry is shaped by the staged executor (attempt number, failure
    summary, strategy, repair fingerprint — the bytes the repair memory
    layer uses to detect oscillation).
    """
    path = _workspace_path(workspace_id, "repair_history.json")
    blob = _read_json(path, {"stages": {}})
    stages = blob.setdefault("stages", {})
    history = stages.setdefault(stage_id, [])
    entry = dict(entry)
    entry.setdefault("attempted_at", _now_iso())
    history.append(entry)
    stages[stage_id] = history
    blob["stages"] = stages
    _write_json(path, blob)
    return entry


def read_repair_history(workspace_id: str, stage_id: str | None = None) -> dict | list:
    blob = _read_json(_workspace_path(workspace_id, "repair_history.json"),
                      {"stages": {}})
    if stage_id is None:
        return blob
    return (blob.get("stages") or {}).get(stage_id, [])


def write_stage_test_results(
    workspace_id: str,
    stage_id: str,
    results: dict,
) -> dict:
    """Persist per-stage test results to ``test_results.json`` (workspace root).
    One stage's results do not overwrite others'. The staged executor writes
    the LATEST test outcome for each stage; the integration-validation stage
    is treated the same way (its ``stage_id`` is the conventional
    ``"_integration"``)."""
    path = _workspace_path(workspace_id, "test_results.json")
    blob = _read_json(path, {"stages": {}})
    stages = blob.setdefault("stages", {})
    stages[stage_id] = results
    blob["stages"] = stages
    _write_json(path, blob)
    return results


def read_stage_test_results(workspace_id: str, stage_id: str | None = None) -> dict:
    blob = _read_json(_workspace_path(workspace_id, "test_results.json"),
                      {"stages": {}})
    if stage_id is None:
        return blob
    return (blob.get("stages") or {}).get(stage_id, {})


# --- checkpoints/<stage_id>.json ----------------------------------------- #


def write_stage_checkpoint(
    workspace_id: str,
    stage_id: str,
    *,
    artifacts: list[dict] | None = None,
    test_results: dict | None = None,
    repair_history: list[dict] | None = None,
    manifest_delta: dict | None = None,
    **extra: Any,
) -> dict:
    """Write a per-stage checkpoint to ``checkpoints/<stage_id>.json`` and
    record its path on the stage's ``last_checkpoint`` field in
    ``stage_status.json``. Resume reads a stage's checkpoint to recover:

      * the artifacts the stage produced (relative paths + sha256)
      * the test results that passed
      * the repair history (so the next repair attempts don't repeat a
        strategy that already failed)
      * a manifest delta (new artifact ids produced by this stage, for the
        manifest module to fold in)

    The checkpoint is ADDITIVE: it is written exactly once per stage (when
    the stage transitions to CHECKPOINTED). A subsequent explicit RE-repair
    of a DONE stage writes a NEW checkpoint (versioned by ``attempt``) so
    the audit trail is preserved — the staged executor never overwrites a
    checkpoint in place (resume never loses a good checkpoint to a bad
    re-repair)."""
    rel_path = f"checkpoints/{stage_id}.json"
    payload = {
        "stage_id": stage_id,
        "workspace_id": workspace_id,
        "artifacts": artifacts or [],
        "test_results": test_results or {},
        "repair_history": repair_history or [],
        "manifest_delta": manifest_delta or {},
        "checkpoint_at": _now_iso(),
        **extra,
    }
    _write_json(_workspace_path(workspace_id, rel_path), payload)
    write_stage_status(workspace_id, stage_id, StageStatus.CHECKPOINTED,
                       last_checkpoint=rel_path)
    return payload


def read_stage_checkpoint(workspace_id: str, stage_id: str) -> dict:
    return _read_json(_workspace_path(workspace_id, "checkpoints", f"{stage_id}.json"), {})


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # ponytail self-check: the smallest thing that fails if the state machine
    # or the persistence regresses. Uses a tmp WORKSPACE_ROOT via the same
    # module-global swap pattern the rest of the codebase uses.
    import tempfile
    from pathlib import Path
    import build_workspace as _bw

    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)

        # No build_plan.json yet on a fresh workspace -> has_staged_layout False.
        wid = _bw.create_workspace(source_question="x")
        assert has_staged_layout(wid) is False
        # next_pending_stage on a plan-less workspace -> None (fall back to single-shot).
        assert next_pending_stage(wid) is None
        assert all_stages_done(wid) is False

        # Stage a fake plan: 3 stages, linear deps.
        plan = {
            "plan_id": "test_plan",
            "generated_at": _now_iso(),
            "stages": [
                {"stage_id": "stage_01", "title": "Core", "files": ["src/core.py"],
                 "dependencies": [], "stage_spec_excerpt": "...stage 01..."},
                {"stage_id": "stage_02", "title": "API", "files": ["src/api.py"],
                 "dependencies": ["stage_01"], "stage_spec_excerpt": "...stage 02..."},
                {"stage_id": "stage_03", "title": "Tests", "files": ["tests/test_core.py"],
                 "dependencies": ["stage_01", "stage_02"], "stage_spec_excerpt": "...stage 03..."},
            ],
        }
        assert write_build_plan(wid, plan) is True
        assert has_staged_layout(wid) is True

        # First pending stage is stage_01 (no deps).
        stage = next_pending_stage(wid)
        assert stage and stage["stage_id"] == "stage_01", stage
        # Mark stage_01 IMPLEMENTING then DONE.
        write_stage_status(wid, "stage_01", StageStatus.IMPLEMENTING)
        write_stage_status(wid, "stage_01", StageStatus.DONE)
        # Next pending is now stage_02 (deps satisfied).
        assert next_pending_stage(wid)["stage_id"] == "stage_02"
        write_stage_status(wid, "stage_02", StageStatus.DONE)
        # stage_03 deps still not satisfied? They are. -> stage_03.
        assert next_pending_stage(wid)["stage_id"] == "stage_03"
        write_stage_status(wid, "stage_03", StageStatus.DONE)
        assert next_pending_stage(wid) is None
        assert all_stages_done(wid) is True

        # Checkpoint write/read.
        cp = write_stage_checkpoint(
            wid, "stage_01",
            artifacts=[{"path": "src/core.py", "sha256": "deadbeef"}],
            test_results={"runner": "pytest", "passed": 3, "failed": 0, "exit_code": 0},
        )
        assert cp["stage_id"] == "stage_01"
        read = read_stage_checkpoint(wid, "stage_01")
        assert read["artifacts"][0]["path"] == "src/core.py"
        # stage_status.json now has last_checkpoint set.
        rec = read_stage_status(wid, "stage_01")
        assert rec["last_checkpoint"] == "checkpoints/stage_01.json"

        # Repair history append.
        append_repair_history(wid, "stage_02", {"attempt": 1, "reason": "tests fail"})
        append_repair_history(wid, "stage_02", {"attempt": 2, "reason": "still failing"})
        hist = read_repair_history(wid, "stage_02")
        assert len(hist) == 2 and hist[0]["attempt"] == 1, hist

        # Test results per stage.
        write_stage_test_results(wid, "stage_02",
                                 {"runner": "pytest", "passed": 5, "failed": 0})
        assert read_stage_test_results(wid, "stage_02")["passed"] == 5

        # Illegal transition raises.
        raised = False
        try:
            stage_transition(StageStatus.DONE, StageStatus.IMPLEMENTING)
        except StageStatusError:
            raised = True
        assert raised

    print("stage_status smoke test PASSED")
