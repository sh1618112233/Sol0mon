"""
test_resume.py - Phase G regression: the resume engine.

Pins:
  - resume_build on an empty NEW workspace -> "no_source_to_resume_from".
  - resume_build on a workspace with planted decisions.json generates the
    spec deterministically, validates + plans it, and skips execution (no
    model): the build sits at EXECUTION_ALLOWED (ready and waiting).
  - A second resume reads the now-populated lifecycle_stage directly (the new
    authority flows across runs).
  - Deterministic lifecycle recovery: the same decisions.json -> the same spec
    -> the same plan id.
  - resume_validation returns all three validator reports; all pass for the
    156 fixture.
  - Terminal states (COMPLETE / ABORTED) are no-ops.
  - BLOCKED with pending humans surfaces the pending count instead of
    auto-running.
  - FAILED without force returns an advisory; FAILED with force re-runs from
    SPEC_READY.
  - Back-compat workspace: ONLY the legacy ad-hoc status string present (no
    lifecycle_stage) resumes correctly via lifecycle_stage_from_status.
  - resume_execution refuses to run before the spec is ready (no inferring
    execution on an unplanned workspace).
  - The lifecycle guard prevents illegal retreat (skipped execution never
    moves the build backwards).
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
import resume
from lifecycle import BuildLifecycle


VR_156 = {
    "resolved_claims": [{
        "normalized_key": "database_technology_selection",
        "dispute": ("Database Technology: GPT selects PostgreSQL, Gemini "
                    "selects SQLite."),
        "verification": {
            "status": "RESOLVED", "confidence": 95,
            "finding": ("Both evidence strips conclude SQLite is the appropriate "
                        "persistence engine; the text argues against PostgreSQL."),
        },
    }],
    "equivalent_claims": [], "unresolved_claims": [],
    "verification_score": 100, "truncated_count": 0,
}


def _setup_workspace(decisions=None, answer_text="Use SQLite"):
    """Create an isolated workspace + (optionally) plant decisions.json +
    answer.json. Returns the workspace_id."""
    bw.WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="sol0mon_res_"))
    wid = bw.create_workspace(source_question="Build Sol0mon MVP")
    if decisions is not None:
        (bw._workspace_dir(wid) / "decisions.json").write_text(
            json.dumps(decisions, indent=2), encoding="utf-8")
    if answer_text is not None:
        (bw._workspace_dir(wid) / "answer.json").write_text(
            json.dumps({"synthesized_answer": answer_text,
                        "answer_id": 1, "question": "q"}, indent=2),
            encoding="utf-8")
    return wid


def _decisions_156():
    return {
        "answer_id": 1,
        "verification_result": VR_156,
        "verified_decisions": [{"outcome_type": "VERIFIED_DECISION",
                                "dispute_key": "database_technology_selection",
                                "decision": "SQLite persistence",
                                "reasoning": "low capital"}],
        "resolved_decisions": [],
    }


# --- Empty NEW workspace ---

def test_empty_workspace_resume_no_source():
    _setup_workspace(decisions=None, answer_text=None)
    wid = _setup_workspace(decisions=None, answer_text=None)
    rep = resume.resume_build(wid)
    assert rep["resumed"] is False
    assert rep["reason"] == "no_source_to_resume_from"


# --- Spec-generation resume ---

def test_resume_generates_spec_validates_and_plans():
    wid = _setup_workspace(_decisions_156())
    rep = resume.resume_build(wid)
    assert rep["resumed"] is True, rep
    # Skipped execution (no model) -> build sits at EXECUTION_ALLOWED.
    assert rep["lifecycle_stage"] == "EXECUTION_ALLOWED", rep
    assert rep["execution"]["skipped"] is True, rep
    # Spec was generated + validated + planned.
    assert rep["spec"]["status"] == "completed", rep["spec"]
    assert rep["validation"]["plan_tasks"] >= 1
    assert rep["validation"]["plan_validation"]["passed"] is True
    # The implementation_plan.md was written to the workspace. After the
    # artefact-selection fix the deterministic renderer emits an executable
    # spec (# Implementation Objective / # Files to Create / ...) — not the
    # legacy "# Architecture" audit shape.
    spec = (bw._workspace_dir(wid) / "implementation_plan.md").read_text()
    assert "# Implementation Objective" in spec
    assert "# Files to Create" in spec


def test_second_resume_reads_lifecycle_stage_directly():
    wid = _setup_workspace(_decisions_156())
    resume.resume_build(wid)
    assert lifecycle.current_lifecycle(wid) is BuildLifecycle.EXECUTION_ALLOWED
    rep2 = resume.resume_build(wid)
    assert rep2["resumed"] is True
    assert rep2["lifecycle_stage"] == "EXECUTION_ALLOWED"


def test_deterministic_lifecycle_recovery_same_spec_same_plan():
    wid1 = _setup_workspace(_decisions_156())
    r1 = resume.resume_build(wid1)
    # Read wid1's spec text NOW — the next _setup_workspace reassigns
    # WORKSPACE_ROOT to a fresh tmp dir, after which wid1's dir is no longer
    # reachable through the module global.
    s1 = (bw._workspace_dir(wid1) / "implementation_plan.md").read_text()
    wid2 = _setup_workspace(_decisions_156())
    r2 = resume.resume_build(wid2)
    s2 = (bw._workspace_dir(wid2) / "implementation_plan.md").read_text()
    # Same canonical decisions -> same plan id (deterministic recovery).
    assert r1["validation"]["plan_id"] == r2["validation"]["plan_id"]
    assert r1["validation"]["plan_order"] == r2["validation"]["plan_order"]
    # ... and the same spec bytes.
    assert s1 == s2


# --- resume_validation ---

def test_resume_validation_returns_three_reports_all_pass_for_156():
    wid = _setup_workspace(_decisions_156())
    resume.resume_build(wid)  # generates the spec
    rv = resume.resume_validation(wid)
    assert rv["resumed"] is True
    assert rv["decision_model"]["passed"] is True
    assert rv["build_spec"] is not None and rv["build_spec"]["passed"] is True
    # final_build is not yet COMPLETE (execution skipped) -> fails (honest).
    assert rv["final_build"]["passed"] is False
    assert rv["final_build"]["stage"] == "final_build"


# --- Terminal states ---

def test_complete_workspace_resume_is_noop():
    wid = _setup_workspace(_decisions_156())
    lifecycle.advance_lifecycle(wid, BuildLifecycle.VERIFIED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.SPEC_READY)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.EXECUTION_ALLOWED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.EXECUTING)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.ARTIFACTS_VALIDATED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.TESTS_VALIDATED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.COMPLETE)
    rep = resume.resume_build(wid)
    assert rep["resumed"] is False and rep["reason"] == "already_complete"


def test_aborted_workspace_resume_is_noop():
    wid = _setup_workspace(_decisions_156())
    # ABORTED is terminal; reachable from many states, e.g. EXECUTION_ALLOWED.
    lifecycle.advance_lifecycle(wid, BuildLifecycle.NEW)
    # We need a legal path to ABORTED: NEW -> ABORTED is legal.
    lifecycle.advance_lifecycle(wid, BuildLifecycle.ABORTED)
    rep = resume.resume_build(wid)
    assert rep["resumed"] is False and rep["reason"] == "aborted"


# --- BLOCKED with pending humans ---

def test_blocked_with_pending_humans_surfaces_count():
    wid = _setup_workspace(_decisions_156())
    # Put a pending human decision on the queue.
    pending = [{"decision_id": "h1", "dispute_key": "k",
                "dispute_text": "needs human", "option_a": "yes",
                "option_b": "no", "reasoning": "", "outcome_type": "HUMAN_REQUIRED"}]
    from human_decision_manager import save_pending, load_pending
    save_pending(wid, pending)
    assert len(load_pending(wid)) == 1
    # Move the lifecycle into BLOCKED, then resume -> should stay blocked.
    lifecycle.advance_lifecycle(wid, BuildLifecycle.VERIFIED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.SPEC_READY)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.EXECUTION_ALLOWED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.BLOCKED,
                                blocked_reason="pending_human_decisions")
    rep = resume.resume_build(wid)
    assert rep["resumed"] is False
    assert rep["reason"] == "blocked_pending_humans"
    assert rep.get("pending_count") == 1


# --- FAILED ---

def test_failed_without_force_is_advisory():
    wid = _setup_workspace(_decisions_156())
    lifecycle.advance_lifecycle(wid, BuildLifecycle.VERIFIED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.SPEC_READY)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.FAILED,
                                status="blocked_incomplete_spec")
    rep = resume.resume_build(wid)
    assert rep["resumed"] is False and rep["reason"] == "failed_no_force"
    assert "force=True" in rep["suggested_action"]


def test_failed_with_force_reruns_from_spec_ready():
    wid = _setup_workspace(_decisions_156())
    lifecycle.advance_lifecycle(wid, BuildLifecycle.VERIFIED)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.SPEC_READY)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.FAILED)
    rep = resume.resume_build(wid, force=True)
    assert rep["resumed"] is True
    # After recovery the spec is regenerated; execution skipped -> EXECUTION_ALLOWED.
    assert rep["lifecycle_stage"] == "EXECUTION_ALLOWED"


# --- Back-compat workspace (legacy status only, no lifecycle_stage) ---

def test_legacy_only_workspace_resumes_via_status_mapper():
    """A workspace created BEFORE this layer has only the legacy ad-hoc
    ``status`` string and no ``lifecycle_stage``. resume must still work,
    mapping the status into a lifecycle stage. The realistic shape includes
    an implementation_plan.md (the pipeline writes it before status='spec_generated')."""
    bw.WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="sol0mon_legacy_"))
    wid = bw.create_workspace(source_question="Build Sol0mon MVP")
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(_decisions_156(), indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "Use SQLite"}, indent=2),
        encoding="utf-8")
    # A realistic legacy workspace has the spec on disk (the pipeline wrote
    # it). After the artefact-selection fix the spec on disk MUST carry the
    # executable-spec sections (the defence-in-depth gate enforces them on
    # any execution path; without executable sections the resume would block
    # at EXECUTION_ALLOWED).
    complete_spec = (
        "# Implementation Objective\nA\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement main.\n\n"
        "# Acceptance Criteria\nG\n\n"
        "# Output Contract\nStop after tests pass.\n"
    )
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        complete_spec, encoding="utf-8")
    # Write ONLY the legacy status (no lifecycle_stage).
    bw.write_build_state(wid, "spec_generated", spec_status="completed")
    assert lifecycle.current_lifecycle(wid) is BuildLifecycle.SPEC_READY
    # Resume proceeds from SPEC_READY -> execution skipped (no model).
    rep = resume.resume_build(wid)
    assert rep["resumed"] is True
    assert rep["lifecycle_stage"] == "EXECUTION_ALLOWED", rep


# --- resume_execution refuses before spec ready ---

def test_resume_execution_refuses_before_spec_ready():
    wid = _setup_workspace(_decisions_156())
    rep = resume.resume_execution(wid)
    assert rep["resumed"] is False
    assert rep["reason"] == "spec_not_ready"
    # Now make the spec ready (via resume_build), then resume_execution works.
    resume.resume_build(wid)
    rep2 = resume.resume_execution(wid)
    assert rep2["resumed"] is True
    assert rep2["lifecycle_stage"] == "EXECUTION_ALLOWED"


def test_resume_execution_on_terminal_is_noop():
    wid = _setup_workspace(_decisions_156())
    lifecycle.advance_lifecycle(wid, BuildLifecycle.NEW)
    lifecycle.advance_lifecycle(wid, BuildLifecycle.ABORTED)
    rep = resume.resume_execution(wid)
    assert rep["resumed"] is False and rep["reason"] == "terminal"


# --- The lifecycle guard prevents illegal retreat ---

def test_skipped_execution_never_moves_lifecycle_backwards():
    wid = _setup_workspace(_decisions_156())
    rep = resume.resume_build(wid)
    # Skipped execution holds at EXECUTION_ALLOWED; not SPEC_READY (no retreat)
    # and not EXECUTING (no executor ran).
    assert rep["lifecycle_stage"] == "EXECUTION_ALLOWED"
    stage = lifecycle.current_lifecycle(wid)
    assert stage is BuildLifecycle.EXECUTION_ALLOWED
    # The legacy status field (if the planner wrote one) is independent; the
    # lifecycle guard never moved backwards. Assert no BuildError raised: a
    # second resume reaches the same state.
    rep2 = resume.resume_build(wid)
    assert rep2["resumed"] is True
    assert lifecycle.current_lifecycle(wid) is BuildLifecycle.EXECUTION_ALLOWED


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
