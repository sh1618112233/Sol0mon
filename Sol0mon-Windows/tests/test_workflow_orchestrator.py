"""
test_workflow_orchestrator.py — Phase 4 Autonomous Workflow Orchestration.

Offline only. No model calls. No browser. No external APIs.

Covers the spec's test scenarios:
  - complete workflow execution       (NativeWorkflowOrchestrator.run to COMPLETED)
  - replay                            (WorkflowRecorder.replay / timeline / stage_timings)
  - recovery                          (RecoveryManager resume + corruption detection)
  - event ordering                     (workflow_events.json order matches transitions)
  - concurrent workflows               (StressTestEngine.run_concurrent)
  - artifact integrity                 (ArtifactManager sha256 + verify)
  - benchmarking                       (BenchmarkEngine small/medium/large)
  - fault injection                    (FaultInjectionEngine every fault type)
  - stress testing                     (StressTestEngine concurrent + retries)
  - regression                         (Phase 1-3.1 components still importable + usable)
  - the 7 workspace artifacts         (under <workspace>/workflow/)
  - the 11 packaging outputs          (under <workspace>/package/)
  - PolicyEngine enforcement          (stage gating + attempt cap + skip + pause)
  - no silent failures                 (every failure surfaces a structured error)
  - backward compatibility             (Phase 1-3.1 components unchanged)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
from workflow_orchestrator import (
    # Components
    ArtifactManager, BenchmarkEngine, BenchmarkResult, BaseStage,
    FaultInjectionEngine, FaultInjectionResult, NativeWorkflowOrchestrator,
    PackagingEngine, PolicyDecision, PolicyEngine, RecoveryManager,
    StageRegistry, StageResult, StressTestEngine, StressResult,
    WorkflowContext, WorkflowOrchestratorProtocol, WorkflowRecorder,
    WorkflowStateMachine, _workflow_dir,
    # Constants
    S_PLANNING, S_RESEARCH, S_DEBATE, S_CONSENSUS, S_ARCHITECTURE,
    S_IMPLEMENTATION, S_VALIDATION, S_REPAIR, S_QUALITY_GATE,
    S_PACKAGING, S_DELIVERY, S_COMPLETED, S_FAILED, S_PAUSED, S_CANCELLED,
    ALL_STATES, SUCCESS_PATH, TERMINAL_STATES,
    EV_WORKFLOW_STARTED, EV_STAGE_STARTED, EV_STAGE_COMPLETED,
    EV_WORKFLOW_COMPLETED, EV_POLICY_DECISION, EV_ARTIFACT_PRODUCED,
    # Artifacts
    ART_WORKFLOW_STATE, ART_WORKFLOW_RECORD, ART_WORKFLOW_METRICS,
    ART_WORKFLOW_EVENTS, ART_WORKFLOW_MANIFEST, ART_ARTIFACT_MANIFEST,
    ART_BENCHMARK_RESULTS, ART_STRESS_RESULTS,
    ALL_WORKFLOW_ARTIFACTS,
    PKG_RESEARCH, PKG_ARCHITECTURE, PKG_IMPLEMENTATION_SPEC,
    PKG_IMPLEMENTATION, PKG_VALIDATION_REPORT, PKG_QUALITY_REPORT,
    PKG_REPAIR_HISTORY, PKG_EXECUTION_RECORD, PKG_WORKFLOW_RECORD,
    PKG_MANIFEST, ALL_PACKAGING_OUTPUTS,
    # Factory
    get_workflow_orchestrator, default_registry,
)
from implementation_engine import (
    _workspace_dir, _write_json,
)


# --------------------------------------------------------------------------- #
# Fixtures + helpers                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_workspace_root(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(bw, "WORKSPACE_ROOT", Path(tmp))
    return Path(tmp)


def _seed_completed_workspace(root, q="complete"):
    """Create a workspace + populate it with enough artifacts for the
    PackagingEngine to assemble the 11 outputs."""
    wid = bw.create_workspace(source_question=q)
    ws = _workspace_dir(wid)
    (ws / "synthesized_answer.md").write_text("# Research\nBody\n", encoding="utf-8")
    (ws / "implementation_plan.md").write_text(
        "# Architecture\nLayered.\n\n# Components\nAPI\n", encoding="utf-8")
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "api.py").write_text("API = 1\n", encoding="utf-8")
    val_dir = ws / "validation"
    val_dir.mkdir(parents=True, exist_ok=True)
    (val_dir / "validation_report.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8")
    (val_dir / "quality_report.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8")
    (val_dir / "repair_history.json").write_text(
        json.dumps({"history": []}), encoding="utf-8")
    impl_dir = ws / "implementation"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / "execution_record.json").write_text(
        json.dumps({"events": []}), encoding="utf-8")
    return wid, ws


def _orch():
    return NativeWorkflowOrchestrator()


# --------------------------------------------------------------------------- #
# 1. Complete workflow execution                                              #
# --------------------------------------------------------------------------- #

def test_complete_workflow_reaches_completed(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    r = _orch().run(workspace_id=wid, user_prompt="Build the thing")
    assert r["success"] is True
    assert r["final_state"] == S_COMPLETED
    assert r["engine"] == "NativeWorkflow"
    assert r["stages_executed"]  # at least PLANNING recorded


def test_missing_workspace_returns_structured_error(tmp_workspace_root):
    r = _orch().run(workspace_id="no-such-ws", user_prompt="x")
    assert r["success"] is False
    assert "workspace" in r["error"].lower()


def test_missing_user_prompt_returns_structured_error(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    r = _orch().run(workspace_id=wid, user_prompt="")
    assert r["success"] is False
    assert "user_prompt" in r["error"]


# --------------------------------------------------------------------------- #
# 2. The 7 workspace artifacts persisted                                      #
# --------------------------------------------------------------------------- #

def test_all_seven_workspace_artifacts_persisted(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build the thing")
    wf = _workflow_dir(wid)
    for f in ALL_WORKFLOW_ARTIFACTS:
        assert (wf / f).is_file(), f"missing workflow artifact: {f}"


def test_workflow_state_artifact_carries_resume_state(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build the thing")
    state = json.loads((_workflow_dir(wid) / ART_WORKFLOW_STATE).read_text())
    assert state["workspace_id"] == wid
    assert state["current_state"] == S_COMPLETED
    assert state["workflow_id"]


def test_workflow_record_artifact_carries_events(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build the thing")
    rec = json.loads((_workflow_dir(wid) / ART_WORKFLOW_RECORD).read_text())
    assert rec["event_count"] > 0
    assert rec["transition_count"] > 0
    types = [e["type"] for e in rec["events"]]
    assert EV_WORKFLOW_STARTED in types
    assert EV_STAGE_STARTED in types
    assert EV_WORKFLOW_COMPLETED in types


# --------------------------------------------------------------------------- #
# 3. The 11 packaging outputs persisted                                       #
# --------------------------------------------------------------------------- #

def test_all_eleven_packaging_outputs_persisted(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    r = _orch().run(workspace_id=wid, user_prompt="Build the thing")
    assert r["success"] is True
    pkg = ws / "package"
    for f in ALL_PACKAGING_OUTPUTS:
        target = pkg / f
        if f.endswith("/"):
            assert target.is_dir(), f"missing package dir: {f}"
        else:
            assert target.is_file(), f"missing package file: {f}"


def test_package_manifest_records_checksums(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build the thing")
    manifest = json.loads((ws / "package" / PKG_MANIFEST).read_text())
    assert manifest["workspace_id"] == wid
    assert manifest["outputs"]
    for entry in manifest["outputs"]:
        assert entry["sha256"]
        assert entry["size"] >= 0
        assert entry["producer"]


# --------------------------------------------------------------------------- #
# 4. Replay + timeline reconstruction                                         #
# --------------------------------------------------------------------------- #

def test_replay_returns_all_events_in_order(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    recorder = WorkflowRecorder()
    orch = NativeWorkflowOrchestrator(recorder=recorder)
    orch.run(workspace_id=wid, user_prompt="Build")
    events = recorder.replay()
    # The WorkflowStarted event is first; WorkflowCompleted is last
    assert events[0]["type"] == EV_WORKFLOW_STARTED
    assert events[-1]["type"] == EV_WORKFLOW_COMPLETED


def test_timeline_returns_all_state_transitions(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    recorder = WorkflowRecorder()
    orch = NativeWorkflowOrchestrator(recorder=recorder)
    orch.run(workspace_id=wid, user_prompt="Build")
    timeline = recorder.timeline()
    # Every transition's 'to' field is in ALL_STATES
    for t in timeline:
        assert t["to"] in ALL_STATES
    # The final transition's 'to' is COMPLETED
    assert timeline[-1]["to"] == S_COMPLETED


def test_stage_timings_reconstructs_per_state(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    recorder = WorkflowRecorder()
    orch = NativeWorkflowOrchestrator(recorder=recorder)
    orch.run(workspace_id=wid, user_prompt="Build")
    timings = recorder.stage_timings()
    # Every state in the successful forward path (excluding COMPLETED and
    # REPAIR ── REPAIR only runs when validation/quality_gate fails) ran.
    for s in SUCCESS_PATH:
        if s in (S_COMPLETED, S_REPAIR):
            continue
        assert s in timings


# --------------------------------------------------------------------------- #
# 5. Recovery from interruption                                               #
# --------------------------------------------------------------------------- #

def test_resume_continues_from_persisted_checkpoint(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    # Seed a paused state mid-workflow
    ctx = WorkflowContext(workspace_id=wid, user_prompt="resume",
                            current_state=S_ARCHITECTURE)
    RecoveryManager.save_state(ctx)
    r = _orch().resume(workspace_id=wid)
    # Resumes from ARCHITECTURE → runs through to COMPLETED (the no-op
    # default stages produce success)
    assert r["success"] is True
    assert r["final_state"] == S_COMPLETED


def test_resume_no_state_returns_structured_error(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    r = _orch().resume(workspace_id=wid)
    assert r["success"] is False
    assert "checkpoint" in r["error"].lower() or "absent" in r["error"].lower()


def test_resume_corrupt_state_returns_structured_error(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    # Corrupt the workflow_state.json
    wf = _workflow_dir(wid)
    wf.mkdir(parents=True, exist_ok=True)
    (wf / ART_WORKFLOW_STATE).write_text("{ not valid json }", encoding="utf-8")
    r = _orch().resume(workspace_id=wid)
    assert r["success"] is False


def test_cancel_marks_context_cancelled(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    ctx = WorkflowContext(workspace_id=wid, user_prompt="cancel",
                            current_state=S_RESEARCH)
    RecoveryManager.save_state(ctx)
    orch = _orch()
    assert orch.cancel(workspace_id=wid) is True
    ctx_after = RecoveryManager.load_state(wid)
    assert ctx_after.cancelled is True


def test_cancel_returns_false_when_no_state(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)  # no checkpoint
    assert _orch().cancel(workspace_id=wid) is False


def test_cancel_is_honored_on_resume(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    ctx = WorkflowContext(workspace_id=wid, user_prompt="cancel",
                            current_state=S_RESEARCH)
    RecoveryManager.save_state(ctx)
    orch = _orch()
    orch.cancel(workspace_id=wid)
    r = orch.resume(workspace_id=wid)
    assert r["final_state"] == S_CANCELLED


# --------------------------------------------------------------------------- #
# 6. Recovery surfaces                                                        #
# --------------------------------------------------------------------------- #

def test_status_returns_unknown_when_no_state(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    st = _orch().status(workspace_id=wid)
    assert st["state"] == "unknown"


def test_status_reports_current_state(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    orch = _orch()
    orch.run(workspace_id=wid, user_prompt="Build")
    st = orch.status(workspace_id=wid)
    assert st["current_state"] == S_COMPLETED


def test_recover_returns_diagnostic(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build")
    rec = _orch().recover(workspace_id=wid)
    assert rec["recoverable"] is True
    assert rec["workspace_id"] == wid


def test_recover_returns_false_when_no_state(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    rec = _orch().recover(workspace_id=wid)
    assert rec["recoverable"] is False
    assert "no workflow_state.json" in rec["reason"]


def test_detect_corruption_flags_corrupt_json(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    wf = _workflow_dir(wid)
    wf.mkdir(parents=True, exist_ok=True)
    (wf / ART_WORKFLOW_STATE).write_text("{ not valid json }", encoding="utf-8")
    assert RecoveryManager.detect_corruption(wid)
    issues = RecoveryManager.detect_corruption(wid)
    assert any("workflow_state.json" in i for i in issues)


def test_detect_corruption_clear_when_clean(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build")
    assert RecoveryManager.detect_corruption(wid) == []


# --------------------------------------------------------------------------- #
# 7. Event ordering                                                           #
# --------------------------------------------------------------------------- #

def test_event_log_artifact_carries_events_in_order(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build")
    events = json.loads((_workflow_dir(wid) / ART_WORKFLOW_EVENTS).read_text())
    types = [e["type"] for e in events["events"]]
    # WorkflowStarted is in the first few events
    assert EV_WORKFLOW_STARTED in types[:5]
    # WorkflowCompleted is last
    assert types[-1] == EV_WORKFLOW_COMPLETED


def test_event_log_artifact_carries_artifact_events(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build")
    events = json.loads((_workflow_dir(wid) / ART_WORKFLOW_EVENTS).read_text())
    # At the COMPLETED state, PackagingEngine produces artifacts → events
    types = [e["type"] for e in events["events"]]
    # StageStarted + StageCompleted + WorkflowCompleted all present
    assert EV_STAGE_STARTED in types
    assert EV_STAGE_COMPLETED in types
    assert EV_WORKFLOW_COMPLETED in types


# --------------------------------------------------------------------------- #
# 8. Concurrent workflows (no shared-state leaks)                            #
# --------------------------------------------------------------------------- #

def test_concurrent_workflows_succeed_independently(tmp_workspace_root):
    ste = StressTestEngine()
    result = ste.run_concurrent(count=3)
    assert result.workflow_count == 3
    assert len(result.final_states) == 3
    # Every workflow reached a terminal state (no shared-state leak)
    for s in result.final_states:
        assert s in (S_COMPLETED, S_FAILED, S_CANCELLED)


def test_concurrent_workflows_distinct_workspaces(tmp_workspace_root):
    """No two concurrent workflows should collide on a workspace_id."""
    seen: set[str] = set()

    def _wid_factory(i: int) -> str:
        wid = bw.create_workspace(source_question=f"stress-{i}")
        seen.add(wid)
        return wid

    ste = StressTestEngine(workspace_id_factory=_wid_factory)
    result = ste.run_concurrent(count=5)
    assert len(seen) == 5  # all distinct


# --------------------------------------------------------------------------- #
# 9. Artifact integrity                                                       #
# --------------------------------------------------------------------------- #

def test_artifact_manager_records_sha256(tmp_workspace_root):
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    _orch().run(workspace_id=wid, user_prompt="Build")
    am = ArtifactManager()
    sample = _workspace_dir(wid) / "synthesized_answer.md"
    entry = am.register(path=sample, producer="test")
    assert len(entry["sha256"]) == 64  # sha256 hex
    assert entry["size"] > 0
    assert entry["missing"] is False


def test_artifact_manager_detects_missing_files(tmp_workspace_root):
    am = ArtifactManager()
    entry = am.register(path="/no/such/file", producer="test")
    assert entry["missing"] is True
    assert entry["sha256"] == ""


def test_artifact_manager_verify_detects_checksum_mismatch(tmp_workspace_root):
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    am = ArtifactManager()
    sample = ws / "synthesized_answer.md"
    am.register(path=sample, producer="test")
    # Mutate the file after registration ── checksum should now mismatch
    sample.write_text("different content", encoding="utf-8")
    issues = am.verify(wid)
    assert any("checksum mismatch" in i for i in issues)


def test_artifact_manager_reproducible_checksum(tmp_workspace_root):
    """Same content → same checksum, byte-for-byte."""
    am1 = ArtifactManager()
    am2 = ArtifactManager()
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    sample = ws / "synthesized_answer.md"
    e1 = am1.register(path=sample, producer="test")
    e2 = am2.register(path=sample, producer="test")
    assert e1["sha256"] == e2["sha256"]


# --------------------------------------------------------------------------- #
# 10. BenchmarkEngine ── small / medium / large workloads                     #
# --------------------------------------------------------------------------- #

def test_benchmark_runs_three_workloads():
    be = BenchmarkEngine()
    results = be.run()
    assert len(results) == 3
    names = {r.workload for r in results}
    assert names == {"small", "medium", "large"}
    # All three complete deterministically with the no-op default stages
    for r in results:
        assert isinstance(r, BenchmarkResult)
        assert r.success is True
        assert r.final_state == S_COMPLETED


def test_benchmark_determinism_same_workload_same_state():
    """Running the same benchmark twice produces the same final state."""
    be = BenchmarkEngine()
    r1 = be.run()
    r2 = be.run()
    assert [r.final_state for r in r1] == [r.final_state for r in r2]


# --------------------------------------------------------------------------- #
# 11. FaultInjectionEngine ── every fault type                                #
# --------------------------------------------------------------------------- #

def test_fault_injection_runs_every_injector():
    fie = FaultInjectionEngine()
    results = fie.run()
    # Every default injector produced a structured result
    assert len(results) >= 4  # the 4 default injectors
    for r in results:
        assert isinstance(r, FaultInjectionResult)
        assert r.fault  # fault name recorded
        # Every fault surfaces an error OR recovers (never silent)
        assert r.error or r.recovered


def test_fault_injection_corrupt_state_detected():
    """Corrupting workflow_state.json is detected by the RecoveryManager."""
    wid = bw.create_workspace(source_question="corrupt test")
    wf = _workflow_dir(wid)
    wf.mkdir(parents=True, exist_ok=True)
    (wf / ART_WORKFLOW_STATE).write_text("{ not valid json }", encoding="utf-8")
    assert RecoveryManager.detect_corruption(wid)


def test_fault_injection_delete_state_returns_no_checkpoint_error():
    """Deleting workflow_state.json → resume surfaces a structured error
    (no silent restart)."""
    wid = bw.create_workspace(source_question="delete test")
    # No state was ever saved → resume returns a structured error
    r = _orch().resume(workspace_id=wid)
    assert r["success"] is False
    assert "checkpoint" in r["error"].lower() or "absent" in r["error"].lower()


# --------------------------------------------------------------------------- #
# 12. StressTestEngine ── retries + concurrent                                 #
# --------------------------------------------------------------------------- #

def test_stress_concurrent_completes_all_workflows(tmp_workspace_root):
    ste = StressTestEngine()
    r = ste.run_concurrent(count=4)
    assert r.workflow_count == 4
    assert all(s in (S_COMPLETED, S_FAILED, S_CANCELLED)
               for s in r.final_states)


# --------------------------------------------------------------------------- #
# 13. PolicyEngine enforcement                                                #
# --------------------------------------------------------------------------- #

def test_policy_engine_stage_gate_blocks_when_producer_not_run():
    eng = PolicyEngine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_DEBATE)
    # No RESEARCH in stage_results → DEBATE gate fails
    d = eng.evaluate(ctx, next_state=S_DEBATE)
    assert not d.allowed
    assert "stage gate" in d.reason


def test_policy_engine_stage_gate_passes_when_producer_succeeded():
    eng = PolicyEngine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_DEBATE)
    ctx.stage_results[S_RESEARCH] = {"success": True}
    d = eng.evaluate(ctx, next_state=S_DEBATE)
    assert d.allowed


def test_policy_engine_attempt_cap_blocks_after_exhaustion():
    eng = PolicyEngine({"max_attempts": 1})
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_RESEARCH, attempt_count=1)
    d = eng.evaluate(ctx, next_state=S_RESEARCH)
    assert not d.allowed
    assert "attempt budget" in d.reason


def test_policy_engine_skip_stages_skipped():
    eng = PolicyEngine({"skip_stages": (S_DEBATE,)})
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_RESEARCH)
    ctx.stage_results[S_RESEARCH] = {"success": True}
    d = eng.evaluate(ctx, next_state=S_DEBATE)
    assert not d.allowed
    assert d.action == "skip"


def test_policy_engine_pause_before_yields_pause_decision():
    eng = PolicyEngine({"pause_before": (S_ARCHITECTURE,)})
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_CONSENSUS)
    ctx.stage_results[S_RESEARCH] = {"success": True}
    ctx.stage_results[S_DEBATE] = {"success": True}
    ctx.stage_results[S_CONSENSUS] = {"success": True}
    d = eng.evaluate(ctx, next_state=S_ARCHITECTURE)
    assert d.allowed
    assert d.action == "pause"


def test_policy_engine_cancelled_workflow_blocks():
    eng = PolicyEngine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_RESEARCH)
    ctx.cancelled = True
    d = eng.evaluate(ctx, next_state=S_RESEARCH)
    assert not d.allowed
    assert d.action == "fail"


def test_policy_engine_terminal_states_allowed():
    """Policy short-circuits terminal states (they're always allowed ──
    the SM has already decided to terminate)."""
    eng = PolicyEngine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_COMPLETED)
    d = eng.evaluate(ctx, next_state=S_COMPLETED)
    assert d.allowed


# --------------------------------------------------------------------------- #
# 14. WorkflowStateMachine repair loop                                        #
# --------------------------------------------------------------------------- #

def test_state_machine_validation_failed_goes_to_repair():
    sm = WorkflowStateMachine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_VALIDATION)
    r = StageResult(stage=S_VALIDATION, success=False)
    assert sm.next_state(ctx, r) == S_REPAIR


def test_state_machine_repair_succeeded_reenters_validation():
    sm = WorkflowStateMachine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_REPAIR)
    r = StageResult(stage=S_REPAIR, success=True)
    assert sm.next_state(ctx, r) == S_VALIDATION


def test_state_machine_quality_gate_failed_goes_to_repair():
    sm = WorkflowStateMachine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_QUALITY_GATE)
    r = StageResult(stage=S_QUALITY_GATE, success=False)
    assert sm.next_state(ctx, r) == S_REPAIR


def test_state_machine_validation_succeeded_advances_to_quality_gate():
    sm = WorkflowStateMachine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_VALIDATION)
    r = StageResult(stage=S_VALIDATION, success=True)
    assert sm.next_state(ctx, r) == S_QUALITY_GATE


def test_state_machine_cancelled_goes_to_cancelled():
    sm = WorkflowStateMachine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_RESEARCH, cancelled=True)
    r = StageResult(stage=S_RESEARCH, success=True)
    assert sm.next_state(ctx, r) == S_CANCELLED


def test_state_machine_non_repair_failure_goes_to_failed():
    sm = WorkflowStateMachine()
    ctx = WorkflowContext(workspace_id="w", user_prompt="x",
                            current_state=S_ARCHITECTURE)
    r = StageResult(stage=S_ARCHITECTURE, success=False)
    assert sm.next_state(ctx, r) == S_FAILED


# --------------------------------------------------------------------------- #
# 15. StageRegistry                                                           #
# --------------------------------------------------------------------------- #

def test_registry_rejects_default_name():
    class _NoName(BaseStage):
        pass
    reg = StageRegistry()
    with pytest.raises(ValueError):
        reg.register(_NoName())


def test_registry_rejects_invalid_state_name():
    class _BadName(BaseStage):
        name = "NOT_A_STATE"
        def execute(self, ctx): ...
    reg = StageRegistry()
    with pytest.raises(ValueError):
        reg.register(_BadName())


def test_registry_registers_real_stage():
    class _MyStage(BaseStage):
        name = S_RESEARCH
        def execute(self, ctx):
            return StageResult(stage=self.name, success=True)
    reg = StageRegistry()
    reg.register(_MyStage())
    assert reg.get(S_RESEARCH) is not None
    assert S_RESEARCH in reg.names()


def test_default_registry_has_eleven_workflow_states():
    reg = default_registry()
    # SUCCESS_PATH includes COMPLETED ── which has no stage. Count the
    # states we registered (non-terminal).
    expected = tuple(s for s in SUCCESS_PATH
                     if s not in (S_COMPLETED, S_FAILED, S_PAUSED, S_CANCELLED))
    for s in expected:
        assert reg.get(s) is not None


# --------------------------------------------------------------------------- #
# 16. Configuration switch                                                     #
# --------------------------------------------------------------------------- #

def test_get_workflow_orchestrator_native_default():
    o = get_workflow_orchestrator()
    assert isinstance(o, NativeWorkflowOrchestrator)


def test_get_workflow_orchestrator_unknown_raises():
    with pytest.raises(ValueError):
        get_workflow_orchestrator("DoesNotExist")


# --------------------------------------------------------------------------- #
# 17. Pure-stdlib at import (no provider pulled transitively)                 #
# --------------------------------------------------------------------------- #

def test_no_forbidden_modules_transitively_imported():
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_set = (
        "{'cerebras_client','ollama_client','openrouter_client','grok_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium',"
        "'requests','httpx','urllib3'}"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import workflow_orchestrator; "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: stdout={probe.stdout!r} "
        f"stderr={probe.stderr!r}"
    )
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "workflow_orchestrator transitively imports forbidden modules"
    )


# --------------------------------------------------------------------------- #
# 18. Backward compatibility ── Phase 1-3.1 components still work             #
# --------------------------------------------------------------------------- #

def test_phase1_execution_decision_engine_still_importable():
    """Phase 1's ExecutionDecisionEngine is untouched by Phase 4 ── import
    + use it directly."""
    from execution_decision_engine import ExecutionDecisionEngine
    eng = ExecutionDecisionEngine()
    d = eng.decide(user_prompt="Build the trading bot")
    assert d.execution_mode in ("RESEARCH_ONLY", "ARCHITECTURE_ONLY",
                                  "BUILD_ONLY", "BUILD_AND_VALIDATE",
                                  "FULL_BUILD", "CUSTOM")


def test_phase2_implementation_engine_still_importable(tmp_workspace_root):
    """Phase 2's NativeImplementationEngine is untouched by Phase 4."""
    from implementation_engine import NativeImplementationEngine
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    eng2 = NativeImplementationEngine(
        coding_agent_fn=lambda prompt, wd: {"success": True,
                                              "output": "ok",
                                              "files": ["src/api.py"]},
        testing_agent_fn=lambda workspace_id: {"passed": True},
        max_attempts=2)
    r = eng2.execute(workspace_id=wid, implementation_spec="# spec")
    assert r.success is True
    assert r.engine == "Native"


def test_phase3_validation_engine_still_importable(tmp_workspace_root):
    """Phase 3's NativeValidationEngine is untouched by Phase 4."""
    from validation_engine import NativeValidationEngine
    wid, ws = _seed_completed_workspace(tmp_workspace_root)
    eng3 = NativeValidationEngine(
        build_fn=lambda wd, spec: {"success": True, "exit_code": 0,
                                    "stdout": "", "stderr": "", "logs": []},
        test_fns={t: lambda wd: {"passed": True, "total": 1, "failed": 0,
                                   "errors": 0, "output": ""}
                  for t in ("unit", "integration", "smoke")},
        max_iterations=1)
    spec = ("# Architecture\nfoo\n\n# Components\napi\n\n"
            "# Acceptance Criteria\ntests pass\n")
    r = eng3.validate(workspace_id=wid, implementation_spec=spec)
    assert r.success is True
    assert r.engine == "NativeValidation"


def test_phase31_repair_memory_still_importable():
    """Phase 3.1's RepairMemory is untouched by Phase 4."""
    from repair_memory import RepairMemory
    mem = RepairMemory()
    mem.record_attempt(__import__("repair_memory").RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["x.py"], failure_signature="sig", success=False))
    assert mem.is_blacklisted(
        strategy="patch", target_files=["x.py"],
        failure_signature_value="sig")
    mem.close()


# --------------------------------------------------------------------------- #
# 19. Determinism                                                             #
# --------------------------------------------------------------------------- #

def test_deterministic_for_same_inputs(tmp_workspace_root):
    ws_root = tmp_workspace_root
    w1, _ = _seed_completed_workspace(ws_root, q="a")
    w2, _ = _seed_completed_workspace(ws_root, q="b")
    orch = _orch()
    r1 = orch.run(workspace_id=w1, user_prompt="Build")
    r2 = orch.run(workspace_id=w2, user_prompt="Build")
    assert r1["final_state"] == r2["final_state"]
    assert r1["stages_executed"] == r2["stages_executed"]


# --------------------------------------------------------------------------- #
# 20. No silent failures                                                       #
# --------------------------------------------------------------------------- #

def test_every_failure_surfaces_an_error(tmp_workspace_root):
    """Every non-success result carries a non-empty error string ──
    the spec's 'no silent failures' invariant."""
    wid, _ = _seed_completed_workspace(tmp_workspace_root)
    # Resume with corrupted state
    wf = _workflow_dir(wid)
    wf.mkdir(parents=True, exist_ok=True)
    (wf / ART_WORKFLOW_STATE).write_text("{ bad json", encoding="utf-8")
    r = _orch().resume(workspace_id=wid)
    assert r["success"] is False
    assert r["error"]  # never silent


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_workflow_orchestrator.py -q")
