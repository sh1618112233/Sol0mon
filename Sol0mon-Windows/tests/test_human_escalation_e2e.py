"""
test_human_escalation_e2e.py — TEST 6 (Human Escalation Validation).

End-to-end proof of the human escalation path:

  1. Force an unresolved dispute (a stalemate the verifier cannot settle from
     evidence) -> produce HUMAN_REQUIRED.
  2. Verify persistence: the HUMAN_REQUIRED outcome is aggregated into one
     bundle and written to workspaces/<id>/human_decisions.json
     ({"pending": [decision], "resolved": []}).
  3. Verify workspace resume: re-loading the pending list yields the same
     decision (resume picks the held state up unchanged).
  4. Verify the build pipeline BLOCKS implementation until the human decision
     is resolved: execute_build_spec returns {blocked: True,
     blocked_reason: "pending_human_decisions"} and the executor is NOT
     invoked. run_build_pipeline records build_state.status =
     "blocked_pending_human_decisions".
  5. Verify unblock: after the human resolves the decision, the SAME
     execute_build_spec call proceeds (mock executor runs, success=True).

All offline. The governor is forced to HUMAN_REQUIRED by monkeypatching
governance.generate (the verifier's LLM call) to raise — exercising the
real verifier fallback path without a network call.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
import human_decision_manager as hdm
import governance


def _good_spec_text() -> str:
    """A spec that satisfies both validate_spec_sections (legacy 7) and
    validate_executable_spec_sections (executable 6, the defence-in-depth
    gate's contract)."""
    return (
        # Legacy 7 sections:
        "# Architecture\nLayered edge + docker.\n\n"
        "# Components\nAPI.\n\n"
        "# File Structure\nsrc/\n\n"
        "# Dependencies\ndocker\n\n"
        "# Constraints\nMUST honor verified decisions.\n\n"
        "# Testing Requirements\npytest\n\n"
        # Executable-spec sections:
        "# Implementation Objective\nBuild the orchestration module.\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement main.\n\n"
        "# Acceptance Criteria\nAll tests green.\n\n"
        "# Output Contract\nStop after tests pass.\n"
    )


def _write_declared_files(wid: str) -> None:
    """Helper: write the files declared in # Files to Create so the post-
    execution QualityGate's "required files exist" check passes for tests
    that use a mock executor (which doesn't write files)."""
    ws = bw._workspace_dir(wid)
    (ws / "src" / "app").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "app" / "main.py").write_text("VALUE = 1\n")
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")


def _dispute_text() -> str:
    return "Orchestration: K8s vs Nomad — ops preference, no evidence settles it."


def test_force_unresolved_dispute_produces_human_required() -> None:
    """Step 1 + 2: force an unresolved dispute; the verifier falls back to
    HUMAN_REQUIRED; the outcome is aggregated + persisted."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)

    # Force the governor's verifier inference to fail. verifier_review's
    # existing fallback returns a HUMAN_REQUIRED outcome when generate()
    # raises — exactly the path forced for an unresolved dispute.
    original_generate = governance.generate

    def _fail_generate(*_args, **_kwargs):
        raise RuntimeError("forced verifier failure — dispute unresolvable")

    governance.generate = _fail_generate
    try:
        outcome = governance.verifier_review(
            dispute_text=_dispute_text(),
            dispute_key="orchestration",
            gpt_position="Adopt K8s — familiar ops tooling.",
            gemini_position="Adopt Nomad — lighter, simpler scheduling.",
        )
    finally:
        governance.generate = original_generate

    assert outcome.outcome_type == governance.OutcomeType.HUMAN_REQUIRED, (
        f"expected HUMAN_REQUIRED, got {outcome.outcome_type}")
    assert outcome.dispute_key == "orchestration"

    # Aggregate into a GovernanceResult (the same shape the debate pipeline
    # hands the build phase) and persist via build_pipeline.
    gov = governance.GovernanceResult()
    gov.add(outcome)
    gov_dict = gov.to_dict()
    assert len(gov_dict["human_required"]) == 1

    wid = bw.create_workspace(source_question="Test workspace")
    bundle = bp.aggregate_human_decisions(wid, gov_dict)
    assert bundle["decision_count"] == 1
    assert len(bundle["decisions"]) == 1
    return wid, gov_dict


def test_persistence_shape_after_aggregation() -> None:
    """Step 2 (persistence): pending/resolved partition holds the
    HUMAN_REQUIRED decision (store-backed, no flat file)."""
    wid, _gov = test_force_unresolved_dispute_produces_human_required()
    pending = hdm.load_pending(wid)
    assert len(pending) == 1
    assert pending[0].get("dispute_key") == "orchestration"
    assert hdm.load_resolved(wid) == []


def test_workspace_resume_picks_up_pending_decisions() -> None:
    """Step 3 (workspace resume): a fresh load (simulating resume) returns the
    SAME pending decision — the held state survives a process boundary."""
    wid, _gov = test_force_unresolved_dispute_produces_human_required()
    first = hdm.load_pending(wid)
    # Simulate resume: re-load the pending state.
    resumed = hdm.load_pending(wid)
    assert resumed == first
    assert len(resumed) == 1


def test_build_pipeline_blocks_implementation_until_resolved() -> None:
    """Step 4 (build pipeline block): with a pending HUMAN_REQUIRED decision,
    execute_build_spec must BLOCK — the executor is NOT invoked and the
    structured result names the block reason."""
    wid, _gov = test_force_unresolved_dispute_produces_human_required()

    # A spec already exists in the workspace (would normally be generated
    # during the build phase). The gate fires BEFORE the spec is even read.
    bse.store_implementation_spec(wid, _good_spec_text())

    executor_called = {"count": 0}

    def _mock_executor(spec_text):
        executor_called["count"] += 1
        return {"success": True, "output": "[MOCK EXECUTION]",
                "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
                "repair_attempts": 0, "error": ""}

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)

    assert result.get("blocked") is True
    assert result.get("blocked_reason") == "pending_human_decisions"
    assert result.get("pending_count") == 1
    assert result.get("success") is False
    assert executor_called["count"] == 0, (
        "executor was invoked while a human decision was still pending")
    # The pending decision is surfaced so a UI/resume flow can render it.
    assert isinstance(result.get("pending_decisions"), list)
    assert len(result["pending_decisions"]) == 1
    return wid


def test_resolve_unblocks_build_pipeline() -> None:
    """Step 5 (unblock): after the human resolves the pending decision, the
    SAME execute_build_spec call proceeds — the mock executor is invoked and
    succeeds."""
    wid = test_build_pipeline_blocks_implementation_until_resolved()

    # Human chooses K8s for the orchestration dispute.
    assert hdm.resolve(wid, "orchestration", "K8s"), "resolve must succeed"
    assert hdm.load_pending(wid) == []
    resolved = hdm.load_resolved(wid)
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "K8s"

    executor_called = {"count": 0}

    def _mock_executor(spec_text):
        executor_called["count"] += 1
        # Simulate OpenCode writing the files declared in # Files to Create
        # so the post-execution QualityGate's "required files exist" check
        # passes (the mock otherwise returns without touching the disk).
        _write_declared_files(wid)
        return {"success": True, "output": "[MOCK EXECUTION]",
                "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
                "repair_attempts": 0, "error": ""}

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)
    assert not result.get("blocked"), "should not be blocked after resolve"
    assert result.get("success") is True, result
    assert executor_called["count"] == 1, "executor should run after unblock"
    assert "coding_agent" in result
    assert result["coding_agent"].name == "opencode"
def test_run_build_pipeline_records_blocked_state() -> None:
    """Task 2 daemon: full run_build_pipeline with a HUMAN_REQUIRED
    source_result records build_state.status = 'blocked_pending_human_decisions'
    so a workspace resume knows implementation is intentionally held."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)

    gov = {
        "verified_decisions": [],
        "verified_tradeoffs": [],
        "human_required": [
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "deploy",
             "dispute_text": "K8s vs Nomad", "option_a": "K8s",
             "option_b": "Nomad", "reasoning": "ops preference",
             "decision_id": "d1"},
        ],
    }

    def _fake_debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [],
            "verified_tradeoffs": [],
            "human_required": gov["human_required"],
            "final_consensus_score": 75,
        }

    executor_called = {"count": 0}

    def _mock_executor(spec_text):
        executor_called["count"] += 1
        return {"success": True, "output": "[MOCK EXECUTION]",
                "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
                "repair_attempts": 0, "error": ""}

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate,
        source_result=gov,
        executor_fn=_mock_executor,
    )

    assert result["spec_status"] == "completed"
    assert result["human_decisions"]["decision_count"] == 1
    exec_result = result["execution_result"]
    assert exec_result.get("blocked") is True
    assert exec_result.get("blocked_reason") == "pending_human_decisions"
    assert executor_called["count"] == 0

    wid = result["workspace_id"]
    state_path = bw._workspace_dir(wid) / "build_state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text())
    assert state.get("status") == "blocked_pending_human_decisions"
    assert state.get("pending_count") == 1
    assert state.get("blocked_reason") == "pending_human_decisions"


def test_full_escalation_cycle_end_to_end() -> None:
    """One end-to-end pass: force HUMAN_REQUIRED -> persist -> resume-load ->
    block -> resolve -> proceed. The complete escalation contract."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)

    # Force HUMAN_REQUIRED via the verifier fallback path.
    original_generate = governance.generate
    governance.generate = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("forced verifier failure"))
    try:
        outcome = governance.verifier_review(
            dispute_text=_dispute_text(),
            dispute_key="orchestration",
            gpt_position="K8s.", gemini_position="Nomad.")
    finally:
        governance.generate = original_generate
    assert outcome.outcome_type == governance.OutcomeType.HUMAN_REQUIRED

    gov = governance.GovernanceResult()
    gov.add(outcome)
    gov_dict = gov.to_dict()

    wid = bw.create_workspace(source_question="Cycle test")
    bp.aggregate_human_decisions(wid, gov_dict)

    # Persisted + resume-equivalent load.
    pending_before = hdm.load_pending(wid)
    assert len(pending_before) == 1
    assert hdm.load_pending(wid) == pending_before

    # Spec ready; gate blocks.
    bse.store_implementation_spec(wid, _good_spec_text())
    calls = {"count": 0}

    def _mock(spec_text):
        calls["count"] += 1
        return {"success": True, "output": "[MOCK EXECUTION]",
                "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
                "repair_attempts": 0, "error": ""}

    blocked = bp.execute_build_spec(wid, executor_fn=_mock)
    assert blocked.get("blocked") is True
    assert calls["count"] == 0

    # Resolve and proceed.
    assert hdm.resolve(wid, "orchestration", "K8s")
    assert hdm.load_pending(wid) == []

    def _mock(spec_text):
        calls["count"] += 1
        _write_declared_files(wid)
        return {"success": True, "output": "[MOCK EXECUTION]",
                "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
                "repair_attempts": 0, "error": ""}

    unblocked = bp.execute_build_spec(wid, executor_fn=_mock)
    assert not unblocked.get("blocked")
    assert unblocked.get("success") is True
    assert calls["count"] == 1
    assert unblocked["coding_agent"].name == "opencode"


if __name__ == "__main__":
    test_force_unresolved_dispute_produces_human_required()
    test_persistence_shape_after_aggregation()
    test_workspace_resume_picks_up_pending_decisions()
    test_build_pipeline_blocks_implementation_until_resolved()
    test_resolve_unblocks_build_pipeline()
    test_run_build_pipeline_records_blocked_state()
    test_full_escalation_cycle_end_to_end()
    print("test_human_escalation_e2e: ALL PASSED")
