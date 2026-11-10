"""
test_lifecycle.py - Phase A regression: the formal BuildLifecycle state machine.

Pins:
  - Legal forward transitions succeed; illegal ones raise BuildError
    (kind=LifecycleError, non-recoverable).
  - Terminal states (COMPLETE, ABORTED) have no outgoing transitions.
  - advance_lifecycle writes a PARALLEL lifecycle_stage field and leaves the
    legacy ad-hoc status string byte-for-byte untouched (back-compat).
  - current_lifecycle prefers lifecycle_stage, falls back to mapping legacy
    status (so pre-hardening workspaces resume).
  - lifecycle_stage_from_status maps every legacy ad-hoc string + arbitrary
    "blocked_*" prefixes.
  - Idempotent re-advance to the SAME stage is a no-op (resume convenience).
  - Every state carries owner/entry/exit/telemetry/recovery metadata.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import lifecycle
from errors import BuildError, ErrorKind


def _tmp_root():
    tmp = tempfile.mkdtemp(prefix="sol0mon_lc_")
    bw.WORKSPACE_ROOT = Path(tmp)
    return tmp


# --- Transition table ---

def test_legal_forward_transitions_succeed():
    assert lifecycle.is_legal_transition(lifecycle.BuildLifecycle.NEW,
                                          lifecycle.BuildLifecycle.VERIFIED)
    assert lifecycle.is_legal_transition(lifecycle.BuildLifecycle.SPEC_READY,
                                          lifecycle.BuildLifecycle.EXECUTION_ALLOWED)
    assert lifecycle.is_legal_transition(lifecycle.BuildLifecycle.TESTS_VALIDATED,
                                          lifecycle.BuildLifecycle.COMPLETE)
    assert lifecycle.transition("NEW", "VERIFIED") is lifecycle.BuildLifecycle.VERIFIED
    # raw strings coerce to enum
    assert lifecycle.transition("EXECUTING", "REPAIRING") is lifecycle.BuildLifecycle.REPAIRING


def test_illegal_transitions_raise_fatal_non_recoverable():
    for cur, tgt in [
        ("NEW", "COMPLETE"),          # skip ahead
        ("COMPLETE", "EXECUTING"),     # back from terminal
        ("ABORTED", "NEW"),            # back from terminal
        ("VERIFIED", "EXECUTING"),      # skip EXECUTION gate
        ("EXECUTION_ALLOWED", "TESTS_VALIDATED"),  # skip artefact+exec
    ]:
        raised = False
        try:
            lifecycle.transition(cur, tgt)
        except BuildError as e:
            raised = True
            assert e.kind is ErrorKind.LIFECYCLE, (cur, tgt, e.kind)
            assert e.recoverable is False, (cur, tgt)
            assert cur in e.message and tgt in e.message
        assert raised, f"illegal {cur}->{tgt} did not raise"


def test_terminal_states_have_no_outgoing():
    # ABORTED is fully terminal. COMPLETE has a single outgoing edge to
    # PUBLISHED — the Build Finalizer's re-publication edge (sole owner);
    # see lifecycle.BUILD_LIFECYCLE for the rationale. This test now pins
    # both halves of that contract: ABORTED has no outgoing edges, and
    # COMPLETE's only outgoing edge is exactly PUBLISHED.
    assert lifecycle.BUILD_LIFECYCLE[lifecycle.BuildLifecycle.ABORTED] == frozenset()
    assert lifecycle.BUILD_LIFECYCLE[lifecycle.BuildLifecycle.COMPLETE] == frozenset({
        lifecycle.BuildLifecycle.PUBLISHED,
    })


def test_every_state_has_metadata():
    for s in lifecycle.BuildLifecycle:
        meta = lifecycle.STATES[s]
        assert {"owner", "entry", "exit", "telemetry", "recovery"} <= set(meta), s
        assert meta["telemetry"].startswith("Lifecycle."), s


def test_str_enum_round_trip_for_json():
    for s in lifecycle.BuildLifecycle:
        assert lifecycle.BuildLifecycle(s.value) is s
        assert json.dumps(s.value)  # JSON-serializable as a string


# --- advance_lifecycle + back-compat with the legacy status string ---

def test_advance_writes_parallel_lifecycle_stage_leaves_status_untouched():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    # Simulate the legacy pipeline writing an ad-hoc status string.
    bw.write_build_state(wid, "spec_generated", spec_status="completed")
    # advance_lifecycle now sets the new authority on top.
    state = lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.SPEC_READY)

    # New field present.
    assert state["lifecycle_stage"] == "SPEC_READY"
    on_disk = json.loads((bw._workspace_dir(wid) / "build_state.json").read_text())
    assert on_disk["lifecycle_stage"] == "SPEC_READY"
    # Legacy status preserved VERBATIM (back-compat — existing tests pin it).
    assert on_disk["status"] == "spec_generated"
    assert on_disk["spec_status"] == "completed"


def test_current_lifecycle_prefers_new_field_then_maps_legacy_status():
    _tmp_root()
    wid = bw.create_workspace()
    # No lifecycle_stage yet, no status -> NEW.
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.NEW

    bw.write_build_state(wid, "spec_generated")
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.SPEC_READY

    bw.write_build_state(wid, "executed", execution_success=True)
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.COMPLETE

    bw.write_build_state(wid, "repair_exhausted", repair_attempts=3)
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.FAILED

    # New field WINS over the legacy status when both are present. Use a FRESH
    # workspace + a legal forward path to populate lifecycle_stage, then
    # overwrite the legacy status with a DIFFERENT lifecycle-implying string
    # and assert the new field still wins (no illegal jumps involved).
    wid2 = bw.create_workspace()
    for tgt in (lifecycle.BuildLifecycle.VERIFIED,
                lifecycle.BuildLifecycle.SPEC_READY,
                lifecycle.BuildLifecycle.EXECUTION_ALLOWED,
                lifecycle.BuildLifecycle.EXECUTING,
                lifecycle.BuildLifecycle.ARTIFACTS_VALIDATED,
                lifecycle.BuildLifecycle.TESTS_VALIDATED,
                lifecycle.BuildLifecycle.COMPLETE):
        lifecycle.advance_lifecycle(wid2, tgt)
    # Overwrite the legacy status with a string that maps to SPEC_READY.
    bw.write_build_state(wid2, "spec_generated")
    # The new lifecycle_stage (COMPLETE) must win over the legacy status.
    assert lifecycle.current_lifecycle(wid2) is lifecycle.BuildLifecycle.COMPLETE


def test_idempotent_re_advance_to_same_stage_is_noop():
    _tmp_root()
    wid = bw.create_workspace()
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.VERIFIED)
    # Re-advancing to the SAME stage must NOT raise (resume convenience).
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.VERIFIED)
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.VERIFIED


def test_advance_illegal_raises():
    _tmp_root()
    wid = bw.create_workspace()
    bw.write_build_state(wid, "spec_generated")
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.SPEC_READY)
    raised = False
    try:
        # SPEC_READY -> EXECUTING skips EXECUTION_ALLOWED (illegal).
        lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.EXECUTING)
    except BuildError as e:
        raised = True
        assert e.kind is ErrorKind.LIFECYCLE
    assert raised
    # No partial write: lifecycle_stage unchanged on the illegal attempt.
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.SPEC_READY


# --- Legacy status -> lifecycle stage mapping ---

def test_legacy_status_mapping_covers_all_ad_hoc_strings():
    cases = {
        "": lifecycle.BuildLifecycle.NEW,
        "created": lifecycle.BuildLifecycle.NEW,
        "resurrected": lifecycle.BuildLifecycle.VERIFIED,
        "workspace_created": lifecycle.BuildLifecycle.NEW,
        "execution_decision_loaded": lifecycle.BuildLifecycle.VERIFIED,
        "execution_decision_resolved": lifecycle.BuildLifecycle.SPEC_READY,
        "human_decisions_aggregated": lifecycle.BuildLifecycle.BLOCKED,
        "research_complete": lifecycle.BuildLifecycle.BLOCKED,
        "architecture_complete": lifecycle.BuildLifecycle.BLOCKED,
        "unresolved_disputes": lifecycle.BuildLifecycle.BLOCKED,
        "human_required": lifecycle.BuildLifecycle.BLOCKED,
        "blocked_pending_human_decisions": lifecycle.BuildLifecycle.BLOCKED,
        "blocked_execution_decision": lifecycle.BuildLifecycle.BLOCKED,
        "blocked": lifecycle.BuildLifecycle.BLOCKED,
        "spec_generated": lifecycle.BuildLifecycle.SPEC_READY,
        "execution_error": lifecycle.BuildLifecycle.FAILED,
        "executed": lifecycle.BuildLifecycle.COMPLETE,
        "implementing": lifecycle.BuildLifecycle.EXECUTING,
        "agent_failed": lifecycle.BuildLifecycle.FAILED,
        "agent_no_artefact": lifecycle.BuildLifecycle.FAILED,
        "repair_agent_failed": lifecycle.BuildLifecycle.REPAIRING,
        "repair_attempt": lifecycle.BuildLifecycle.REPAIRING,
        "repair_exhausted": lifecycle.BuildLifecycle.FAILED,
        "blocked_incomplete_spec": lifecycle.BuildLifecycle.FAILED,
    }
    for status, expected in cases.items():
        assert lifecycle.lifecycle_stage_from_status(status) is expected, status

    # And arbitrary blocked_* prefixes (run_build_pipeline builds the string
    # dynamically as f"blocked_{block_reason}").
    assert lifecycle.lifecycle_stage_from_status("blocked_some_new_reason") is lifecycle.BuildLifecycle.BLOCKED
    # Unknown strings -> NEW (the safe resume start).
    assert lifecycle.lifecycle_stage_from_status("never_seen_string_xyz") is lifecycle.BuildLifecycle.NEW


def test_resume_point_reads_current():
    _tmp_root()
    wid = bw.create_workspace()
    bw.write_build_state(wid, "spec_generated")
    assert lifecycle.resume_point(wid) is lifecycle.current_lifecycle(wid)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
