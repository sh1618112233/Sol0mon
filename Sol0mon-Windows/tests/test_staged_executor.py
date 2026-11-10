"""
test_staged_executor.py - pins the staged execution loop end-to-end.

Pins:
  - execute_stages drives every PENDING stage to DONE with an injected
    agent + test seam, then runs integration validation.
  - Idempotent: a second execute_stages invocation on an already-DONE
    build returns success=True with stage_results=[].
  - Stage failure escalates to HUMAN_REQUIRED (the single escalation channel,
    reusing human_decision_manager.save_pending).
  - repair_history.json accumulates a record per stage repair attempt
    (fingerprinted so the next strategy chain is anti-oscillation aware).
  - test_results.json records the last test outcome per stage.
  - checkpoints/<stage_id>.json records the artifact delta + test results +
    repair history + manifest_delta per completed stage.
  - Legacy (no staged plan) workspace: execute_stages returns the
    ``no_staged_layout`` safety-net (does NOT crash).
  - Integration-only failure drives the integration pseudo-stage; bounded by
    MAX_INTEGRATION_ITERATIONS; HUMAN_REQUIRED on exhaustion.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_planner as bp
import staged_executor
import stage_status
from stage_status import StageStatus
from coding_agents.base_agent import AgentResult


def _tmp_root():
    tmp = tempfile.mkdtemp(prefix="sol0mon_se_")
    bw.WORKSPACE_ROOT = Path(tmp)
    return tmp


def _plant_two_components(wid):
    """Plant a workspace whose decisions.json + answer.json +
    implementation_plan.md make decision_renderer._architecture_components
    emit 2 components (Temporal + PostgreSQL)."""
    decisions = {
        "answer_id": 1,
        "verification_result": {
            "resolved_claims": [
                {"normalized_key": "orchestration",
                 "dispute": "Airflow vs Temporal",
                 "verification": {"status": "RESOLVED", "confidence": 95,
                                  "finding": "Verification concludes Temporal as orchestration engine; Airflow rejected."}},
                {"normalized_key": "persistence",
                 "dispute": "MySQL vs PostgreSQL",
                 "verification": {"status": "RESOLVED", "confidence": 92,
                                  "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
            ],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "orchestration",
             "decision": "Temporal"},
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
             "decision": "PostgreSQL"},
        ],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "Temporal+PostgreSQL"}),
        encoding="utf-8")
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        "# Implementation Objective\nBuild X\n\n"
        "# Files to Create\n- `src/orchestration/main.py`\n"
        "- `src/persistence/store.py`\n- `tests/test_app.py`\n\n"
        "# Files to Modify\n(none)\n\n"
        "# Implementation Steps\n1. orchestration\n2. persistence\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n",
        encoding="utf-8")


def _passing_agent(plan, working_dir=None, timeout=None):
    """Agent seam that writes a stage-specific file (each stage produces a
    NEW file so the artefact-presence gate passes per stage) + a sentinel
    test file."""
    token = "default"
    for line in plan.splitlines():
        if line.startswith("# STAGED IMPLEMENTATION DIRECTIVE (stage_id="):
            token = line.split("(stage_id=", 1)[1].rstrip(")")
            break
        if line.startswith("# STAGED REPAIR — Stage"):
            parts = line.split()
            if len(parts) >= 5:
                token = parts[4].split("_", 1)[-1]
            break
        if line.startswith("# INTEGRATION REPAIR"):
            token = "integration"
            break
    rel = f"src/staged_exec_{token}.py"
    p = Path(working_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# stub (stage={token})\nVALUE=42\n", encoding="utf-8")
    test_p = Path(working_dir) / "tests" / "test_app.py"
    test_p.parent.mkdir(parents=True, exist_ok=True)
    test_p.write_text("def test_app():\n  assert True\n", encoding="utf-8")
    return AgentResult(success=True, output=f"wrote {rel}", error="")


def _passing_tests(working_dir=None):
    return {"runner": "pytest", "passed": 1, "failed": 0, "exit_code": 0,
            "output": "1 passed"}


def test_execute_stages_drives_all_to_done():
    _tmp_root()
    wid = bw.create_workspace(source_question="Build X")
    _plant_two_components(wid)
    bp.plan_staged_build(wid)
    r = staged_executor.execute_stages(wid,
                                         agent_fn=_passing_agent,
                                         test_fn=_passing_tests)
    assert r["success"] is True, r
    assert r["stages_failed"] == 0, r
    # The persisted stage_status.json says every declared stage is DONE.
    plan = stage_status.read_build_plan(wid)
    statuses = stage_status.read_stage_status(wid).get("stages") or {}
    for s in plan["stages"]:
        assert (statuses.get(s["stage_id"], {}) or {}).get("status") == "DONE"
    # Integration pseudo-stage is also DONE.
    integ = (statuses.get(staged_executor.INTEGRATION_STAGE_ID, {}) or {})
    assert integ.get("status") == "DONE"


def test_idempotent_resume_returns_success_with_no_new_work():
    _tmp_root()
    wid = bw.create_workspace(source_question="Build X")
    _plant_two_components(wid)
    bp.plan_staged_build(wid)
    staged_executor.execute_stages(wid,
                                     agent_fn=_passing_agent,
                                     test_fn=_passing_tests)
    r2 = staged_executor.execute_stages(wid,
                                          agent_fn=_passing_agent,
                                          test_fn=_passing_tests)
    assert r2["success"] is True, r2
    assert r2["stages_run_this_invocation"] == 0, r2
    assert r2["stage_results"] == [], r2


def test_checkpoints_persisted_per_stage():
    _tmp_root()
    wid = bw.create_workspace(source_question="Build X")
    _plant_two_components(wid)
    manifest = bp.plan_staged_build(wid)
    staged_executor.execute_stages(wid,
                                     agent_fn=_passing_agent,
                                     test_fn=_passing_tests)
    for s in manifest["stages"]:
        cp = stage_status.read_stage_checkpoint(wid, s["stage_id"])
        assert cp.get("stage_id") == s["stage_id"], cp
        # Each successful stage has a test_results entry.
        tr = stage_status.read_stage_test_results(wid, s["stage_id"])
        assert tr.get("passed") == 1, tr


def test_legacy_workspace_returns_no_staged_layout():
    _tmp_root()
    wid = bw.create_workspace(source_question="legacy")
    r = staged_executor.execute_stages(wid)
    assert r["skipped"] is True
    assert r["reason"] == "no_staged_layout"


def test_stage_failure_escalates_to_human_required():
    _tmp_root()
    wid = bw.create_workspace(source_question="Build Failing")
    # Single component (PostgreSQL) so the plan has a single component stage.
    decisions = {
        "answer_id": 1,
        "verification_result": {
            "resolved_claims": [
                {"normalized_key": "persistence",
                 "dispute": "MySQL vs PostgreSQL",
                 "verification": {"status": "RESOLVED", "confidence": 92,
                                  "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
            ],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
             "decision": "PostgreSQL"},
        ],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "PostgreSQL"}), encoding="utf-8")
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        "# Implementation Objective\nX\n\n"
        "# Files to Create\n- `src/persistence/store.py`\n\n"
        "# Implementation Steps\n1. build\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n",
        encoding="utf-8")
    bp.plan_staged_build(wid)

    def failing_agent(plan, working_dir=None, timeout=None):
        # Write a file so artefact-presence passes; tests will fail.
        p = Path(working_dir) / "src" / "persistence" / "store.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("syntax error here", encoding="utf-8")
        return AgentResult(success=True, output="written a broken file",
                            error="")

    def failing_tests(working_dir=None):
        return {"runner": "pytest", "passed": 0, "failed": 2, "exit_code": 1,
                "output": "FAILED test_load\nFAILED test_save"}

    # Bound the outer loop to 0 to fast-fail (inner executor still runs its
    # own 3-attempt loop, after which the staged executor escalates).
    r = staged_executor.execute_stages(wid, agent_fn=failing_agent,
                                         test_fn=failing_tests,
                                         max_stage_repair_iterations=0)
    assert r["success"] is False, r
    assert r["blocked"] is True, r
    # First (and only) stage is FAILED; HUMAN_REQUIRED emitted.
    sr = r["stage_results"][0]
    assert sr["status"] == "FAILED"
    assert sr["human_required"] is True
    # human_decision_manager has a pending decision for this ws.
    from human_decision_manager import load_pending
    pending = load_pending(wid)
    assert pending, "expected a pending HUMAN_REQUIRED decision"
    assert any(sr["stage_id"] in (d.get("stage_id","") or "") for d in pending)


def test_repair_history_accumulates_per_attempt():
    _tmp_root()
    wid = bw.create_workspace(source_question="Build Failing")
    decisions = {
        "answer_id": 1,
        "verification_result": {
            "resolved_claims": [
                {"normalized_key": "persistence",
                 "dispute": "MySQL vs PostgreSQL",
                 "verification": {"status": "RESOLVED", "confidence": 92,
                                  "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
            ],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
             "decision": "PostgreSQL"},
        ],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "PostgreSQL"}), encoding="utf-8")
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        "# Implementation Objective\nX\n\n"
        "# Files to Create\n- `src/persistence/store.py`\n\n"
        "# Implementation Steps\n1. build\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n",
        encoding="utf-8")
    bp.plan_staged_build(wid)

    def failing_agent(plan, working_dir=None, timeout=None):
        p = Path(working_dir) / "src" / "persistence" / "store.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("syntax error", encoding="utf-8")
        return AgentResult(success=True, output="broken file", error="")

    def failing_tests(working_dir=None):
        return {"runner": "pytest", "passed": 0, "failed": 1, "exit_code": 1,
                "output": "FAILED test_load"}

    r = staged_executor.execute_stages(wid, agent_fn=failing_agent,
                                         test_fn=failing_tests,
                                         max_stage_repair_iterations=1)
    # The single component stage should have failed; repair_history.json has
    # at least one entry (the inner-loop completion record).
    plan = stage_status.read_build_plan(wid)
    failed_sids = [s["stage_id"] for s in r["stage_results"]
                    if s.get("status") == StageStatus.FAILED.value]
    assert failed_sids, r
    history = stage_status.read_repair_history(wid, failed_sids[0])
    assert history, history
    # Each entry has a fingerprint (the anti-oscillation hook).
    assert all("fingerprint" in h for h in history), history


def test_integration_validation_failure_blocks_build():
    """Integration failure: every per-stage implementation passes, but the
    integration-validation test run fails. The integration pseudo-stage
    falls into the bounded repair loop, exhausts it, and escalates to
    HUMAN_REQUIRED on the integration stage.

    Detection strategy: count test_fn invocations. With
    ``max_stage_repair_iterations=0`` and a stage_count N, the first N
    test_fn calls are per-stage (each stage's inner executor runs one
    attempt); the (N+1)th call is the integration validation run. The
    test_fn returns passing for the first N calls and failing from there,
    so the FIRST integration-validation call fails (the agent enters the
    bounded integration-repair loop, which also fails, and HUMAN_REQUIRED
    is emitted)."""
    _tmp_root()
    wid = bw.create_workspace(source_question="Integration fail")
    decisions = {
        "answer_id": 1,
        "verification_result": {
            "resolved_claims": [
                {"normalized_key": "persistence",
                 "dispute": "MySQL vs PostgreSQL",
                 "verification": {"status": "RESOLVED", "confidence": 92,
                                  "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
            ],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
             "decision": "PostgreSQL"},
        ],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "PostgreSQL"}), encoding="utf-8")
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        "# Implementation Objective\nX\n\n"
        "# Files to Create\n- `src/persistence/store.py`\n\n"
        "# Implementation Steps\n1. build\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n",
        encoding="utf-8")
    plan_manifest = bp.plan_staged_build(wid)
    n_stages = plan_manifest["stage_count"]

    # Counter of test_fn calls — flips to "fail" after N per-stage calls.
    calls = {"n": 0}

    def stage_pass_agent(plan, working_dir=None, timeout=None):
        # Per-stage pass: write a stage-specific file.
        token = "default"
        for line in plan.splitlines():
            if line.startswith("# STAGED IMPLEMENTATION DIRECTIVE (stage_id="):
                token = line.split("(stage_id=", 1)[1].rstrip(")")
                break
            if line.startswith("# STAGED REPAIR — Stage"):
                parts = line.split()
                if len(parts) >= 5:
                    token = parts[4].split("_", 1)[-1]
                break
            if line.startswith("# INTEGRATION REPAIR"):
                token = "integration"
                break
        rel = f"src/staged_exec_{token}.py"
        p = Path(working_dir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# ok\nVALUE=42\n", encoding="utf-8")
        test_p = Path(working_dir) / "tests" / "test_app.py"
        test_p.parent.mkdir(parents=True, exist_ok=True)
        test_p.write_text("def test_app():\n  assert True\n", encoding="utf-8")
        return AgentResult(success=True, output=f"wrote {rel}", error="")

    def fail_on_integration_tests(working_dir=None):
        calls["n"] += 1
        # The first N calls are per-stage; from N+1 onward = integration.
        if calls["n"] <= n_stages:
            return {"runner": "pytest", "passed": 1, "failed": 0,
                    "exit_code": 0, "output": "1 passed"}
        # Integration validation (and any subsequent integration-repair
        # retries): always fail.
        return {"runner": "pytest", "passed": 0, "failed": 1, "exit_code": 1,
                "output": "FAILED integration tests"}

    r = staged_executor.execute_stages(wid,
                                         agent_fn=stage_pass_agent,
                                         test_fn=fail_on_integration_tests,
                                         max_stage_repair_iterations=0,
                                         max_integration_iterations=1)
    # All per-stage work passed; integration failed and was bounded by
    # ``max_integration_iterations``. The build is blocked pending human.
    assert r["success"] is False, r
    assert r["blocked"] is True, r
    assert r["integration"] is not None, r
    assert r["integration"].get("passed") is False
    intf_status = stage_status.read_stage_status(
        wid, staged_executor.INTEGRATION_STAGE_ID).get("status")
    assert intf_status == StageStatus.FAILED.value, intf_status
    # HUMAN_REQUIRED emitted for the integration stage.
    from human_decision_manager import load_pending
    pending = load_pending(wid)
    assert pending, "pending HUMAN_REQUIRED expected"
    assert any(staged_executor.INTEGRATION_STAGE_ID in
                (d.get("stage_id","") or "") for d in pending)



def test_run_build_pipeline_dispatches_to_staged_executor_and_finalizer():
    _tmp_root()
    source_result = {
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "orchestration",
             "decision": "Temporal"},
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
             "decision": "PostgreSQL"},
        ],
        "verified_tradeoffs": [],
        "human_required": [],
        "resolved_decisions": [],
        "synthesized_answer": "Temporal+PostgreSQL",
        "verification_result": {
            "resolved_claims": [
                {"normalized_key": "orchestration",
                 "dispute": "Airflow vs Temporal",
                 "verification": {"status": "RESOLVED", "confidence": 95,
                                  "finding": "Verification concludes Temporal as orchestration engine; Airflow rejected."}},
                {"normalized_key": "persistence",
                 "dispute": "MySQL vs PostgreSQL",
                 "verification": {"status": "RESOLVED", "confidence": 92,
                                  "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
            ],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
    }
    spec_text = (
        "# Architecture\nTemporal + PostgreSQL.\n\n"
        "# Components\norchestration; persistence\n\n"
        "# File Structure\nsrc/orchestration src/persistence tests\n\n"
        "# Dependencies\npytest\n\n"
        "# Constraints\nUse Temporal and PostgreSQL.\n\n"
        "# Testing Requirements\npytest\n\n"
        "# Implementation Objective\nBuild X\n\n"
        "# Files to Create\n- `src/orchestration/main.py`\n"
        "- `src/persistence/store.py`\n- `tests/test_app.py`\n\n"
        "# Files to Modify\n(none)\n\n"
        "# Implementation Steps\n1. orchestration\n2. persistence\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n"
    )

    def fake_debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": spec_text,
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": source_result["verified_decisions"],
            "verified_tradeoffs": [], "human_required": [],
            "final_consensus_score": 90,
        }

    import types
    sys.modules.setdefault(
        "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
    )
    import build_pipeline as pipeline
    from lifecycle import current_lifecycle, BuildLifecycle

    result = pipeline.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=fake_debate,
        source_result=source_result,
        use_claude_code=True,
        staged_agent_fn=_passing_agent,
        staged_test_fn=_passing_tests,
    )

    assert result["build_plan"] and result["build_plan"]["stages"], result
    assert result["staged_execution"] and result["staged_execution"]["success"] is True, result
    assert result["execution_result"].get("staged") is True, result
    assert result["execution_result"]["success"] is True, result
    assert not any(
        step.get("step") == "stage_entry" and step.get("status") == "failed"
        for step in result["finalization"].get("steps", [])
    ), result["finalization"]
    assert current_lifecycle(result["workspace_id"]) in {
        BuildLifecycle.PUBLISHED, BuildLifecycle.COMPLETE,
    }


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
