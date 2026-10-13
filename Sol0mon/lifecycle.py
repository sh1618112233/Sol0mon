"""
Project Sol0mon - Formal Build Lifecycle State Machine (Final Architecture
Hardening - Phase A).

Replaces the ~30 ad-hoc status string literals scattered across
``build_pipeline`` / ``implementation_executor`` / ``execution_decision`` with
a single state machine that defines the legal build states and the transitions
between them. ``write_build_state`` is left byte-for-byte unchanged (existing
tests pin its output); the lifecycle writes a PARALLEL ``lifecycle_stage``
field into ``build_state.json``. The two coexist:

    build_state.json:
      "status":          "spec_generated"   # legacy ad-hoc string (back-compat)
      "lifecycle_stage": "SPEC_READY"        # new authoritative state

``lifecycle_stage`` is the new authority for every NEW module (resume,
validation, the execution planner's stage tagging). ``lifecycle_stage_from_status``
maps any legacy ``status`` string into the nearest lifecycle stage, so a
workspace created before this layer — which has no ``lifecycle_stage`` field —
still resumes correctly. Nothing bypasses the lifecycle: a legacy ``status``
IS its lifecycle stage, just spelled differently; the mapper bridges the two.

# Why not rewrite the status strings in place?

Existing tests (``test_phase2_pipeline_integrity``, ``test_phase3_build_spec_artifact``,
``test_repair_loop``) pin specific ``status`` values byte-for-byte. Replacing
them is a breaking change across the codebase for zero behavioral gain; the
mapper gives resume the same information. Wiring ``advance_lifecycle`` into
``run_build_pipeline`` is a future, opt-in step (see ponytail: comment below).

# The state machine

    NEW
      |
      v
    VERIFIED
      |
      v
    SPEC_READY
      |
      v
    EXECUTION_ALLOWED
      |
      v
    EXECUTING
      |
      v
    ARTIFACTS_VALIDATED
      |
      v
    TESTS_VALIDATED
      |
      v
    COMPLETE

Side / terminal states reached from many points: FAILED, REPAIRING, BLOCKED,
ABORTED. The full transition table is ``BUILD_LIFECYCLE`` below. Each state
defines: owner, entry conditions, exit conditions, allowed transitions,
telemetry event, recovery strategy (in ``STATES``).
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from typing import Any

from errors import BuildError, illegal_transition


class BuildLifecycle(str, enum.Enum):
    """Authoritative build states. ``str`` enum so JSON-serialization,
    comparison against existing string literals, and ``status in (...)``
    checks all work without conversion."""

    NEW = "NEW"
    VERIFIED = "VERIFIED"
    SPEC_READY = "SPEC_READY"
    EXECUTION_ALLOWED = "EXECUTION_ALLOWED"
    EXECUTING = "EXECUTING"
    ARTIFACTS_VALIDATED = "ARTIFACTS_VALIDATED"
    TESTS_VALIDATED = "TESTS_VALIDATED"
    # PUBLISHED — the Build Finalization stage. Owner: build_finalizer.
    # Reached AFTER TESTS_VALIDATED (build itself complete on disk) and
    # BEFORE COMPLETE (every persistence store reconciled with the build).
    # The Build Finalizer is the SOLE code path that enters this stage and
    # the SOLE path that advances out of it (to COMPLETE on success, or to
    # BLOCKED/ABORTED on a terminal finalizer failure). No other module may
    # call advance_lifecycle(_, PUBLISHED).
    PUBLISHED = "PUBLISHED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    REPAIRING = "REPAIRING"
    BLOCKED = "BLOCKED"
    ABORTED = "ABORTED"


# Allowed forward transitions. ``BUILD_LIFECYCLE[src]`` is the set of states
# ``src`` may move to. Anything else raises ``BuildError`` via
# ``illegal_transition``. ``ABORTED`` is terminal: empty set. ``COMPLETE`` has
# the single outgoing edge to ``PUBLISHED`` — the Build Finalizer's
# re-publication edge (it is the SOLE code path that takes this transition
# in production; the build pipeline reaches lifecycle=COMPLETE via the legacy
# ``status='executed'`` map BEFORE the finalizer enters PUBLISHED).
BUILD_LIFECYCLE: dict[BuildLifecycle, frozenset[BuildLifecycle]] = {
    BuildLifecycle.NEW: frozenset({BuildLifecycle.VERIFIED, BuildLifecycle.BLOCKED, BuildLifecycle.ABORTED}),
    BuildLifecycle.VERIFIED: frozenset({BuildLifecycle.SPEC_READY, BuildLifecycle.BLOCKED, BuildLifecycle.ABORTED}),
    BuildLifecycle.SPEC_READY: frozenset({
        BuildLifecycle.EXECUTION_ALLOWED, BuildLifecycle.FAILED,
        BuildLifecycle.BLOCKED, BuildLifecycle.ABORTED,
    }),
    BuildLifecycle.EXECUTION_ALLOWED: frozenset({
        # FAILED is reached when an execution attempt cannot proceed (e.g. the
        # spec file is missing despite build_state claiming 'spec_generated' —
        # a data-integrity failure, not a gate block).
        BuildLifecycle.EXECUTING, BuildLifecycle.BLOCKED,
        BuildLifecycle.FAILED, BuildLifecycle.ABORTED,
    }),
    BuildLifecycle.EXECUTING: frozenset({
        BuildLifecycle.ARTIFACTS_VALIDATED, BuildLifecycle.REPAIRING,
        BuildLifecycle.FAILED, BuildLifecycle.BLOCKED, BuildLifecycle.ABORTED,
    }),
    BuildLifecycle.ARTIFACTS_VALIDATED: frozenset({
        BuildLifecycle.TESTS_VALIDATED, BuildLifecycle.REPAIRING,
        BuildLifecycle.FAILED, BuildLifecycle.ABORTED,
    }),
    BuildLifecycle.TESTS_VALIDATED: frozenset({
        # PUBLISHED is the canonical next stage (Build Finalizer reconciliation).
        # COMPLETE remains reachable for backward compatibility with any
        # legacy caller that jumps directly TESTS_VALIDATED -> COMPLETE
        # (resume.py keeps that path; the finalizer routes through PUBLISHED).
        BuildLifecycle.PUBLISHED, BuildLifecycle.COMPLETE,
        BuildLifecycle.ABORTED,
    }),
    BuildLifecycle.PUBLISHED: frozenset({
        BuildLifecycle.COMPLETE, BuildLifecycle.ABORTED,
    }),
    # COMPLETE -> PUBLISHED is the re-publication edge. The build pipeline
    # reaches lifecycle=COMPLETE via the legacy ``status="executed"`` map
    # BEFORE the Build Finalizer runs; the finalizer (its sole owner)
    # re-enters PUBLISHED to perform reconciliation, then advances back to
    # COMPLETE on success. This makes Publication observable in the
    # lifecycle audit trail without breaking legacy COMPLETE-terminus
    # assumptions in resume / validation.
    BuildLifecycle.COMPLETE: frozenset({BuildLifecycle.PUBLISHED}),
    BuildLifecycle.REPAIRING: frozenset({BuildLifecycle.EXECUTING, BuildLifecycle.FAILED, BuildLifecycle.ABORTED}),
    BuildLifecycle.BLOCKED: frozenset({
        BuildLifecycle.SPEC_READY, BuildLifecycle.EXECUTION_ALLOWED,
        BuildLifecycle.NEW, BuildLifecycle.ABORTED,
    }),
    # FAILED is *mostly* terminal. The SPEC_READY edge is the recovery edge:
    # a force-retry resume (resume_build(force=True)) regenerates the spec
    # deterministically from the (immutable) canonical decisions, so it can
    # restart at SPEC_READY without first bouncing through NEW. REPAIRING
    # also reaches FAILED on repair exhaustion (see implementation_executor).
    BuildLifecycle.FAILED: frozenset({BuildLifecycle.NEW, BuildLifecycle.SPEC_READY, BuildLifecycle.ABORTED}),
    BuildLifecycle.ABORTED: frozenset(),
}


# Per-state metadata: owner / entry / exit / recovery. Telemetry event is the
# ``Lifecycle.<STATE>`` record emitted on every transition into the state.
STATES: dict[BuildLifecycle, dict[str, str]] = {
    BuildLifecycle.NEW: {
        "owner": "build_pipeline",
        "entry": "workspace created (create_workspace / resurrect_answer)",
        "exit": "verification_result consumed via decision_model.decision_records",
        "telemetry": "Lifecycle.NEW",
        "recovery": "fresh start; nothing to recover",
    },
    BuildLifecycle.VERIFIED: {
        "owner": "decision_model",
        "entry": "verification_result present; canonical records produced",
        "exit": "build_spec generated from the canonical records",
        "telemetry": "Lifecycle.VERIFIED",
        "recovery": "re-run verification with prior_research_evidence reuse",
    },
    BuildLifecycle.SPEC_READY: {
        "owner": "build_spec_engine",
        "entry": "build_spec() status='completed' AND missing_sections=[]",
        "exit": "execution_decision == IMPLEMENTATION_REQUIRED; no pending humans",
        "telemetry": "Lifecycle.SPEC_READY",
        "recovery": "regenerate spec deterministically (debate_fn=None Path 3)",
    },
    BuildLifecycle.EXECUTION_ALLOWED: {
        "owner": "execution_decision + human_decision gate",
        "entry": "exec decision not blocked; human_decision_manager.load_pending empty",
        "exit": "coding agent dispatched",
        "telemetry": "Lifecycle.EXECUTION_ALLOWED",
        "recovery": "resolve pending human decisions or override mode",
    },
    BuildLifecycle.EXECUTING: {
        "owner": "implementation_executor",
        "entry": "agent_fn invoked (coding_agent_router.get_agent)",
        "exit": "agent produced at least one new file (artefact-presence gate)",
        "telemetry": "Lifecycle.EXECUTING",
        "recovery": "resume from repair_attempts checkpoint in build_state.json",
    },
    BuildLifecycle.ARTIFACTS_VALIDATED: {
        "owner": "implementation_executor._list_workspace_files",
        "entry": "files_after - files_before is non-empty",
        "exit": "test_fn ran and _tests_passed == True",
        "telemetry": "Lifecycle.ARTIFACTS_VALIDATED",
        "recovery": "re-invoke agent (repair loop continues)",
    },
    BuildLifecycle.TESTS_VALIDATED: {
        "owner": "implementation_executor._tests_passed",
        "entry": "runner != 'none' AND passed>=1 AND failed==0 AND exit_code==0",
        "exit": "lifecycle advances to PUBLISHED (Build Finalizer) or COMPLETE",
        "telemetry": "Lifecycle.TESTS_VALIDATED",
        "recovery": "enter REPAIRING on test failure",
    },
    BuildLifecycle.PUBLISHED: {
        "owner": "build_finalizer",
        "entry": "build manifest written (lifecycle=TESTS_VALIDATED); "
                 "finalize_build entered",
        "exit": "build_finalizer reconciled answer_memory + workspace DB + "
                "telemetry + artifact catalog -> advance to COMPLETE",
        "telemetry": "Lifecycle.PUBLISHED",
        "recovery": "re-run finalize_build; finalizer is idempotent",
    },
    BuildLifecycle.COMPLETE: {
        "owner": "build_finalizer",
        "entry": "PUBLISHED reached AND finalize_build returned "
                 "finalized=True (all 5 reconciliation steps ok or skipped)",
        "exit": "(terminal)",
        "telemetry": "Lifecycle.COMPLETE",
        "recovery": "no recovery; start a NEW build",
    },
    BuildLifecycle.REPAIRING: {
        "owner": "implementation_executor._execute_with_repair_loop",
        "entry": "tests failed and repair_attempts < MAX_REPAIR_ATTEMPTS",
        "exit": "re-invoke agent (-> EXECUTING) or exhaust (-> FAILED)",
        "telemetry": "Lifecycle.REPAIRING",
        "recovery": "advance lifecycle to EXECUTING with the next repair prompt",
    },
    BuildLifecycle.FAILED: {
        "owner": "build_pipeline / implementation_executor",
        "entry": "spec incomplete / agent failed / repair exhausted / execution error",
        "exit": "(terminal unless restart)",
        "telemetry": "Lifecycle.FAILED",
        "recovery": "start NEW; inspect build_state.json failure reason",
    },
    BuildLifecycle.BLOCKED: {
        "owner": "human_decision_manager / execution_decision gate",
        "entry": "pending human decisions OR execution decision != IMPLEMENTATION_REQUIRED",
        "exit": "human resolves / mode override / aborts",
        "telemetry": "Lifecycle.BLOCKED",
        "recovery": "resolve_human_requirement then resume_build",
    },
    BuildLifecycle.ABORTED: {
        "owner": "operator",
        "entry": "explicit cancel",
        "exit": "(terminal)",
        "telemetry": "Lifecycle.ABORTED",
        "recovery": "no recovery; start a NEW build",
    },
}


TERMINAL: frozenset[BuildLifecycle] = frozenset({BuildLifecycle.COMPLETE, BuildLifecycle.ABORTED})


def is_legal_transition(current: BuildLifecycle, target: BuildLifecycle) -> bool:
    return target in BUILD_LIFECYCLE.get(current, frozenset())


def transition(current: BuildLifecycle | str, target: BuildLifecycle | str) -> BuildLifecycle:
    """Validate a lifecycle transition. Returns the target state if legal,
    raises ``BuildError`` (LifecycleError, non-recoverable) if not.

    Accepts raw strings too (coerced via ``BuildLifecycle``) so callers can
    pass values read from ``build_state.json`` without an explicit coercion step."""
    cur = current if isinstance(current, BuildLifecycle) else BuildLifecycle(str(current))
    tgt = target if isinstance(target, BuildLifecycle) else BuildLifecycle(str(target))
    if not is_legal_transition(cur, tgt):
        raise illegal_transition(cur.value, tgt.value)
    return tgt


# ponytail: advance_lifecycle writes a PARALLEL lifecycle_stage field into
# build_state.json. It deliberately does NOT replace the legacy ``status``
# string (existing tests pin it byte-for-byte). Wiring this into
# run_build_pipeline is a future opt-in step; resume/validation read
# lifecycle_stage, falling back to mapping legacy ``status`` when absent.
def advance_lifecycle(workspace_id: str, target: BuildLifecycle | str, **extra: Any) -> dict:
    """Validate + persist a lifecycle transition for a workspace. Writes
    ``lifecycle_stage`` (the new authority) plus any ``extra`` fields into
    ``build_state.json``, leaving ``status`` untouched. Returns the new state."""
    from build_workspace import _workspace_dir  # local import: reuse the conftest-snapshotted root

    tgt = target if isinstance(target, BuildLifecycle) else BuildLifecycle(str(target))
    path = _workspace_dir(workspace_id) / "build_state.json"
    state: dict = {}
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            state = {}
    prior_str = state.get("lifecycle_stage") or state.get("status") or "NEW"
    try:
        prior = BuildLifecycle(prior_str) if prior_str in _LEGACY_OK else _map_legacy_status(prior_str)
    except ValueError:
        prior = BuildLifecycle.NEW
    # The FIRST advance on a legacy workspace has no lifecycle_stage yet; the
    # prior stage comes from the legacy status mapper. A re-advance of the
    # SAME stage (idempotent) is allowed for resume convenience.
    if prior != tgt:
        transition(prior, tgt)  # raises BuildError on illegal
    state["lifecycle_stage"] = tgt.value
    state["lifecycle_updated_at"] = datetime.now(timezone.utc).isoformat()
    state.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


_LEGACY_OK = {s.value for s in BuildLifecycle}


def current_lifecycle(workspace_id: str) -> BuildLifecycle:
    """Read the authoritative lifecycle stage for a workspace. Prefers the new
    ``lifecycle_stage`` field; falls back to mapping the legacy ``status``
    string (so pre-hardening workspaces resume cleanly). Returns NEW for a
    fresh / corrupt / missing workspace."""
    from build_workspace import _workspace_dir

    if not workspace_id:
        return BuildLifecycle.NEW
    path = _workspace_dir(workspace_id) / "build_state.json"
    if not path.is_file():
        return BuildLifecycle.NEW
    try:
        state = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return BuildLifecycle.NEW
    stage = state.get("lifecycle_stage")
    if stage and stage in _LEGACY_OK:
        return BuildLifecycle(stage)
    legacy = state.get("status") or ""
    return _map_legacy_status(legacy)


def lifecycle_stage_from_status(status: str) -> BuildLifecycle:
    """Map any legacy ad-hoc ``status`` string into the nearest lifecycle
    stage. Public so callers can ask 'what lifecycle stage does this old
    workspace's status imply?' without reading build_state.json."""
    return _map_legacy_status(status or "")


# Legacy ad-hoc status string -> lifecycle stage. ``""`` and unknown -> NEW
# (the safe start state for resume). The mapping is one-way and conservative:
# a string that could be blocked OR failed is mapped to BLOCKED only when it
# is unambiguously a pending-human shape, else FAILED.
_LEGACY_STATUS_MAP: dict[str, BuildLifecycle] = {
    "": BuildLifecycle.NEW,
    "created": BuildLifecycle.NEW,
    "resurrected": BuildLifecycle.VERIFIED,
    "workspace_created": BuildLifecycle.NEW,
    "human_decisions_aggregated": BuildLifecycle.BLOCKED,
    "execution_decision_loaded": BuildLifecycle.VERIFIED,
    "execution_decision_resolved": BuildLifecycle.SPEC_READY,
    "research_complete": BuildLifecycle.BLOCKED,
    "architecture_complete": BuildLifecycle.BLOCKED,
    "unresolved_disputes": BuildLifecycle.BLOCKED,
    "human_required": BuildLifecycle.BLOCKED,
    "blocked_pending_human_decisions": BuildLifecycle.BLOCKED,
    "blocked_incomplete_spec": BuildLifecycle.FAILED,
    "blocked_execution_decision": BuildLifecycle.BLOCKED,
    "blocked": BuildLifecycle.BLOCKED,
    "spec_generated": BuildLifecycle.SPEC_READY,
    "execution_error": BuildLifecycle.FAILED,
    "executed": BuildLifecycle.COMPLETE,
    "implementing": BuildLifecycle.EXECUTING,
    "agent_failed": BuildLifecycle.FAILED,
    "agent_no_artefact": BuildLifecycle.FAILED,
    "repair_agent_failed": BuildLifecycle.REPAIRING,
    "repair_attempt": BuildLifecycle.REPAIRING,
    "repair_exhausted": BuildLifecycle.FAILED,
    "spec_status_completed": BuildLifecycle.SPEC_READY,
}


def _map_legacy_status(status: str) -> BuildLifecycle:
    if status in _LEGACY_STATUS_MAP:
        return _LEGACY_STATUS_MAP[status]
    # ``blocked_<reason>`` was generated by run_build_pipeline as
    # f"blocked_{block_reason}"; the prefix alone is enough to place it.
    if status.startswith("blocked_"):
        return BuildLifecycle.BLOCKED
    return BuildLifecycle.NEW


def resume_point(workspace_id: str) -> BuildLifecycle:
    """Alias for ``current_lifecycle``; reads as intent at resume call sites."""
    return current_lifecycle(workspace_id)


if __name__ == "__main__":
    # ponytail self-check: the smallest thing that fails if the state machine
    # regresses. No I/O — exercises the transition table + the legacy mapper.
    assert is_legal_transition(BuildLifecycle.NEW, BuildLifecycle.VERIFIED)
    assert not is_legal_transition(BuildLifecycle.COMPLETE, BuildLifecycle.EXECUTING)
    assert transition("NEW", "VERIFIED") is BuildLifecycle.VERIFIED
    raised = False
    try:
        transition(BuildLifecycle.COMPLETE, BuildLifecycle.EXECUTING)
    except BuildError as e:
        raised = True
        assert e.recoverable is False and e.kind.value == "LifecycleError"
    assert raised, "illegal transition did not raise"

    # Legacy status mapping (resume of pre-hardening workspaces).
    assert lifecycle_stage_from_status("spec_generated") is BuildLifecycle.SPEC_READY
    assert lifecycle_stage_from_status("executed") is BuildLifecycle.COMPLETE
    assert lifecycle_stage_from_status("repair_exhausted") is BuildLifecycle.FAILED
    assert lifecycle_stage_from_status("blocked_pending_human_decisions") is BuildLifecycle.BLOCKED
    assert lifecycle_stage_from_status("blocked_anything_new") is BuildLifecycle.BLOCKED
    assert lifecycle_stage_from_status("") is BuildLifecycle.NEW
    assert lifecycle_stage_from_status("never_seen_string") is BuildLifecycle.NEW

    # Every state has metadata (owner/entry/exit/telemetry/recovery).
    for s in BuildLifecycle:
        meta = STATES[s]
        assert {"owner", "entry", "exit", "telemetry", "recovery"} <= set(meta), s

    # Round-trip str(enum) -> enum for JSON consumers.
    for s in BuildLifecycle:
        assert BuildLifecycle(s.value) is s

    print("lifecycle smoke test PASSED")
