"""
test_implementation_engine.py — Native Implementation Engine integration tests.

Offline only. No model calls. No browser. No external APIs.

Covers the spec's test scenarios:
  - implementation specification loading
  - missing specification
  - empty specification
  - successful OpenCode invocation
  - prompt submission
  - acknowledgement verification
  - generated file validation
  - execution interruption
  - resume
  - checkpoint recovery
  - provider timeout
  - workspace recovery
  - LegacyImplementationEngine
  - NativeImplementationEngine
  - configuration switching
  - node transitions
  - state recovery
  - ExecutionRecorder replay
  - ExecutionRecorder integrity
  - regression comparison
  - timeline reconstruction

The seams (coding_agent_fn / testing_agent_fn / repair_agent_fn) replace
production providers so tests never touch OpenCode or the network.
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
from implementation_engine import (
    # Components
    AgentManager, BaseNode, BuildResult, CheckpointManager, CodingAgent,
    CodingAgentError, EventBus, ExecutionRecorder, ExecutionContext,
    ImplementationEngine, LegacyImplementationEngine, NativeImplementationEngine,
    NodeRegistry, ResultCollector, RetryPolicy, StateManager, TransitionEngine,
    # Constants
    NODE_GENERATE_CODE, NODE_RUN_TESTS, NODE_ANALYZE_FAILURE, NODE_REPAIR,
    NODE_FINISH, STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING,
    EV_EXECUTION_STARTED, EV_EXECUTION_RESUMED, EV_EXECUTION_COMPLETED,
    EV_EXECUTION_FAILED, EV_NODE_STARTED, EV_NODE_COMPLETED, EV_NODE_FAILED,
    EV_CHECKPOINT_SAVED,
    # Factory
    get_implementation_engine, default_registry,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_workspace_root(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(bw, "WORKSPACE_ROOT", Path(tmp))
    return Path(tmp)


def _make_workspace(q="test", *, root=None):
    return bw.create_workspace(source_question=q)


def _good_agent(prompt: str, working_dir: str) -> dict:
    """Fake coding agent ── success with a generated file."""
    return {"success": True, "output": "ok", "files": ["src/main.py"]}


def _good_tests(workspace_id: str) -> dict:
    return {"passed": True, "failed": 0, "errors": 0}


def _failing_tests(workspace_id: str) -> dict:
    return {"passed": False, "failed": 2, "errors": 0,
            "failure_summary": "test_main.py::test_x failed",
            "stderr": "AssertionError: expected 5 got 4"}


class FlakyTests:
    """Returns failure on attempt N, success afterwards."""
    def __init__(self, fail_until: int = 1):
        self.n = 0
        self.fail_until = fail_until

    def __call__(self, workspace_id: str) -> dict:
        self.n += 1
        return ({"passed": False, "failed": 1} if self.n <= self.fail_until
                else {"passed": True, "failed": 0})


# --------------------------------------------------------------------------- #
# 1. Implementation specification loading                                     #
# --------------------------------------------------------------------------- #

def test_implementation_spec_loaded_into_context(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests,
        max_attempts=2)
    r = eng.execute(workspace_id=wid, implementation_spec="# Spec\nbuild it")
    assert r.success is True
    # State persisted with the spec
    ctx = StateManager.load(wid)
    assert ctx is not None
    assert "# Spec" in ctx.implementation_spec


# --------------------------------------------------------------------------- #
# 2. Missing specification                                                     #
# --------------------------------------------------------------------------- #

def test_missing_spec_returns_structured_error(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="")
    assert r.success is False
    assert "implementation_spec" in r.error
    # Never silent ── error is non-empty
    assert r.error


def test_missing_spec_none_value(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec=None)  # type: ignore[arg-type]
    assert r.success is False


# --------------------------------------------------------------------------- #
# 3. Empty (whitespace) specification                                          #
# --------------------------------------------------------------------------- #

def test_empty_whitespace_spec_rejected(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="   \n\n   ")
    assert r.success is False
    assert "implementation_spec" in r.error


# --------------------------------------------------------------------------- #
# 4. Successful OpenCode invocation                                            #
# --------------------------------------------------------------------------- #

def test_successful_opencode_invocation(tmp_workspace_root):
    wid = _make_workspace()
    captured = {"prompts": []}

    def capturing_agent(prompt, working_dir):
        captured["prompts"].append(prompt)
        return {"success": True, "output": "generated", "files": ["src/x.py"]}

    eng = NativeImplementationEngine(
        coding_agent_fn=capturing_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="Build the thing")
    assert r.success is True
    assert r.generated_files == ["src/x.py"]


# --------------------------------------------------------------------------- #
# 5. Prompt submission                                                         #
# --------------------------------------------------------------------------- #

def test_prompt_submitted_to_agent(tmp_workspace_root):
    wid = _make_workspace()
    received = {"prompts": []}

    def agent(prompt, working_dir):
        received["prompts"].append(prompt)
        return {"success": True, "output": "ok"}

    eng = NativeImplementationEngine(
        coding_agent_fn=agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="MY_UNIQUE_SPEC")
    assert r.success is True
    assert received["prompts"], "no prompt was submitted to the agent"
    # The spec is the prompt for the first generate-code run
    assert any("MY_UNIQUE_SPEC" in p for p in received["prompts"])


# --------------------------------------------------------------------------- #
# 6. Acknowledgement verification (CodingAgent post-validation)               #
# --------------------------------------------------------------------------- #

def test_acknowledgement_verification_rejects_silent_failure(tmp_workspace_root):
    """An agent that returns an empty dict is REJECTED — silent failures
    are forbidden by the CodingAgent's post-validation."""
    wid = _make_workspace()

    def silent_agent(prompt, working_dir):
        return {}  # no success, no output, no files ── silent failure

    eng = NativeImplementationEngine(
        coding_agent_fn=silent_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is False
    assert "post-validation" in r.error or "silent failure" in r.error


def test_acknowledgement_verification_accepts_dict_with_success_key(
    tmp_workspace_root):
    wid = _make_workspace()

    def ok_agent(prompt, working_dir):
        return {"success": True, "message": "done"}

    eng = NativeImplementationEngine(
        coding_agent_fn=ok_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is True


# --------------------------------------------------------------------------- #
# 7. Generated file validation                                                 #
# --------------------------------------------------------------------------- #

def test_generated_files_collected_on_result(tmp_workspace_root):
    wid = _make_workspace()
    files = ["src/a.py", "src/b.py", "tests/test_a.py"]

    def agent(prompt, working_dir):
        return {"success": True, "files": files, "output": "ok"}

    eng = NativeImplementationEngine(
        coding_agent_fn=agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is True
    assert sorted(r.generated_files) == sorted(files)


# --------------------------------------------------------------------------- #
# 8. Execution interruption (simulate crash between nodes)                     #
# --------------------------------------------------------------------------- #

def test_execution_interruption_checkpoint_survives(tmp_workspace_root):
    wid = _make_workspace()
    # Manually seed a context at RunTestsNode ── simulating a crash after
    # GenerateCodeNode completed.
    ctx = ExecutionContext(
        workspace_id=wid, implementation_spec="spec",
        current_node=NODE_RUN_TESTS, current_attempt=1,
        execution_state=STATUS_RUNNING,
        generated_files=["src/main.py"],
    )
    StateManager.save(ctx)
    # The checkpoint exists on disk
    impl = Path(tmp_workspace_root) / f"workspace_{wid}" / "implementation"
    assert (impl / "execution_context.json").is_file()


# --------------------------------------------------------------------------- #
# 9. Resume from interruption                                                 #
# --------------------------------------------------------------------------- #

def test_resume_continues_from_checkpoint(tmp_workspace_root):
    wid = _make_workspace()
    StateManager.save(ExecutionContext(
        workspace_id=wid, implementation_spec="spec",
        current_node=NODE_RUN_TESTS, current_attempt=1,
        execution_state=STATUS_RUNNING,
    ))
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests,
        max_attempts=2)
    r = eng.resume(workspace_id=wid)
    assert r.success is True, r
    assert r.workspace_id == wid


def test_resume_no_checkpoint_returns_structured_error(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    # No checkpoint was seeded
    r = eng.resume(workspace_id=wid)
    assert r.success is False
    assert "checkpoint" in r.error.lower() or "absent" in r.error.lower()


# --------------------------------------------------------------------------- #
# 10. Checkpoint recovery                                                      #
# --------------------------------------------------------------------------- #

def test_checkpoint_after_every_node(tmp_workspace_root):
    """Each completed node writes a fresh checkpoint.json — resume sees
    the LAST one, and it should reflect the last node that ran."""
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    impl = Path(tmp_workspace_root) / f"workspace_{wid}" / "implementation"
    checkpoints = json.loads((impl / "checkpoint.json").read_text())
    assert checkpoints["node"] == NODE_FINISH  # last node
    # engine_state also reflects the final node
    state = json.loads((impl / "engine_state.json").read_text())
    assert state["current_node"] == NODE_FINISH


# --------------------------------------------------------------------------- #
# 11. Provider timeout (modeled as an agent exception)                        #
# --------------------------------------------------------------------------- #

def test_provider_timeout_surfaced_not_silent(tmp_workspace_root):
    wid = _make_workspace()

    def timeout_agent(prompt, working_dir):
        raise TimeoutError("agent timed out after 60s")

    eng = NativeImplementationEngine(
        coding_agent_fn=timeout_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is False
    assert "timed out" in r.error or "TimeoutError" in r.error


# --------------------------------------------------------------------------- #
# 12. Workspace recovery                                                       #
# --------------------------------------------------------------------------- #

def test_workspace_recovery_diagnostic(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is True
    rec = eng.recover(workspace_id=wid)
    assert rec["recoverable"] is True


def test_workspace_recovery_no_state(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    rec = eng.recover(workspace_id=wid)
    assert rec["recoverable"] is False
    assert "no execution_context.json" in rec["reason"]


# --------------------------------------------------------------------------- #
# 13. LegacyImplementationEngine surface                                      #
# --------------------------------------------------------------------------- #

def test_legacy_engine_resume_reports_unsupported(tmp_workspace_root):
    wid = _make_workspace()
    leg = LegacyImplementationEngine()
    r = leg.resume(workspace_id=wid)
    assert r.success is False
    assert r.engine == "Legacy"
    assert "resume" in r.error.lower()


def test_legacy_engine_status_surface(tmp_workspace_root):
    wid = _make_workspace()
    leg = LegacyImplementationEngine()
    st = leg.status(workspace_id=wid)
    assert st["engine"] == "Legacy"


def test_legacy_engine_recover_reports_no_checkpoint(tmp_workspace_root):
    wid = _make_workspace()
    leg = LegacyImplementationEngine()
    rec = leg.recover(workspace_id=wid)
    assert rec["recoverable"] is False
    assert "checkpoint" in rec["reason"].lower()


# --------------------------------------------------------------------------- #
# 14. NativeImplementationEngine surface                                      #
# --------------------------------------------------------------------------- #

def test_native_engine_build_result_shape(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.engine == "Native"
    assert r.success is True
    assert r.execution_id
    assert r.executed_at
    assert r.workspace_id == wid
    # BuildResult dict shape (consumers use .to_dict())
    d = r.to_dict()
    for key in ("success", "output", "test_results", "repair_attempts",
                "repair_log", "error", "executed_at", "workspace_id",
                "engine", "final_node", "execution_id", "node_timings",
                "generated_files", "modified_files", "execution_log"):
        assert key in d


# --------------------------------------------------------------------------- #
# 15. Configuration switching                                                  #
# --------------------------------------------------------------------------- #

def test_get_implementation_engine_native_default():
    eng = get_implementation_engine()
    assert isinstance(eng, NativeImplementationEngine)


def test_get_implementation_engine_native_by_name():
    eng = get_implementation_engine("Native")
    assert isinstance(eng, NativeImplementationEngine)


def test_get_implementation_engine_legacy_by_name():
    eng = get_implementation_engine("Legacy")
    assert isinstance(eng, LegacyImplementationEngine)


def test_get_implementation_engine_unknown_name_raises():
    with pytest.raises(ValueError):
        get_implementation_engine("DoesNotExist")


# --------------------------------------------------------------------------- #
# 16. Node transitions                                                          #
# --------------------------------------------------------------------------- #

def test_node_transition_success_path(tmp_workspace_root):
    """GenerateCode -> RunTests (passed) -> Finish."""
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is True
    timeline = eng.recorder.timeline()
    transitions = [(t["from"], t["to"], t["reason"]) for t in timeline]
    # The success path generates these transitions
    transition_strings = [f"{a}->{b}" for a, b, _ in transitions]
    assert any("RunTestsNode->FinishNode" in s for s in transition_strings)


def test_node_transition_failure_path_with_retry(tmp_workspace_root):
    """GenerateCode -> RunTests (failed) -> AnalyzeFailure -> Repair ->
    retry -> GenerateCode -> RunTests (passed) -> Finish."""
    wid = _make_workspace()
    flaky = FlakyTests(fail_until=1)
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=flaky,
        max_attempts=3)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is True
    assert r.repair_attempts >= 1
    timeline = eng.recorder.timeline()
    # We see AnalyzeFailure and Repair transitions
    transition_strings = [f"{t['from']}->{t['to']}" for t in timeline]
    assert any("RepairNode" in t for t in transition_strings)


def test_node_transition_retry_exhausted_to_finish(tmp_workspace_root):
    """When retry budget is exhausted, FinishNode is reached with
    test_passed=False."""
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_failing_tests,
        max_attempts=2)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is False
    assert r.final_node == NODE_FINISH
    timeline = eng.recorder.timeline()
    # The final transition is to Finish
    final = timeline[-1]
    assert final["to"] == NODE_FINISH


# --------------------------------------------------------------------------- #
# 17. State recovery                                                           #
# --------------------------------------------------------------------------- #

def test_state_save_load_round_trip(tmp_workspace_root):
    wid = _make_workspace()
    ctx = ExecutionContext(workspace_id=wid, implementation_spec="round trip")
    StateManager.save(ctx)
    loaded = StateManager.load(wid)
    assert loaded is not None
    assert loaded.implementation_spec == "round trip"
    assert loaded.execution_id == ctx.execution_id


def test_state_validate_detects_bad_state(tmp_workspace_root):
    ctx = ExecutionContext(execution_state="BOGUS", current_node="Nope")
    issues = StateManager.validate(ctx)
    assert any("execution_state" in i for i in issues)
    assert any("current_node" in i for i in issues)


def test_state_recover_returns_none_on_corrupt_state(tmp_workspace_root):
    wid = _make_workspace()
    impl = Path(tmp_workspace_root) / f"workspace_{wid}" / "implementation"
    impl.mkdir(parents=True, exist_ok=True)
    # Corrupt execution_context.json
    (impl / "execution_context.json").write_text("{ not json }", encoding="utf-8")
    assert StateManager.recover(wid) is None


# --------------------------------------------------------------------------- #
# 18. ExecutionRecorder replay                                                 #
# --------------------------------------------------------------------------- #

def test_recorder_replay_matches_event_count(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    recorded = eng.recorder.to_dict()
    replay = eng.recorder.replay()
    assert len(replay) == recorded["event_count"]
    assert len(replay) > 0


def test_recorder_timeline_is_node_transitions_only(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    timeline = eng.recorder.timeline()
    for e in timeline:
        assert e["kind"] == "node_transition"


# --------------------------------------------------------------------------- #
# 19. ExecutionRecorder integrity (persists to disk; is reloadable)            #
# --------------------------------------------------------------------------- #

def test_recorder_persists_to_disk(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    record_path = (Path(tmp_workspace_root) / f"workspace_{wid}"
                   / "implementation" / "execution_record.json")
    assert record_path.is_file()
    d = json.loads(record_path.read_text())
    assert d["event_count"] > 0
    assert d["plan"]["execution_id"]
    assert d["execution_mode"] == "Native"


def test_recorder_observer_never_affects_workflow(tmp_workspace_root):
    """Observer-only contract: removing the recorder subscriber should not
    change the result."""
    wid = _make_workspace()
    # Engine WITHOUT the recorder subscriber attached to the bus.
    eng_no_rec = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng_no_rec.event_bus._subs = []  # detach all observers
    r_no = eng_no_rec.execute(workspace_id=wid, implementation_spec="spec")

    wid2 = _make_workspace()  # need a fresh ws since the prev has state
    eng_with_rec = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r_with = eng_with_rec.execute(workspace_id=wid2, implementation_spec="spec")
    assert r_no.success == r_with.success
    assert r_no.final_node == r_with.final_node


# --------------------------------------------------------------------------- #
# 20. Regression comparison (Legacy vs Native BuildResult shape)              #
# --------------------------------------------------------------------------- #

def test_native_and_legacy_produce_same_buildresult_shape(tmp_workspace_root):
    """Both engines return identical BuildResult field shapes."""
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r_n = eng.execute(workspace_id=wid, implementation_spec="spec")
    leg = LegacyImplementationEngine()
    # Legacy resume surface (avoids the real OpenCode agent)
    r_l = leg.resume(workspace_id=wid)
    # Field set matches (regression comparison)
    assert set(r_n.to_dict().keys()) == set(r_l.to_dict().keys())
    # Both are BuildResult instances
    assert isinstance(r_n, BuildResult)
    assert isinstance(r_l, BuildResult)


# --------------------------------------------------------------------------- #
# 21. Timeline reconstruction                                                  #
# --------------------------------------------------------------------------- #

def test_timeline_reconstructs_full_success_path(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    timeline = eng.recorder.timeline()
    # The success path must include START -> GenerateCode and RunTests->Finish
    transitions = [(t["from"], t["to"]) for t in timeline]
    assert (NODE_GENERATE_CODE, NODE_RUN_TESTS) in transitions or \
           any(NODE_GENERATE_CODE in a or NODE_GENERATE_CODE in b
               for a, b in transitions)
    assert any(t[1] == NODE_FINISH for t in transitions)


def test_failure_reconstruction_returns_first_failure(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_failing_tests,
        max_attempts=1)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    assert r.success is False
    fr = eng.recorder.failure_reconstruction()
    # failure_reconstruction returns SOME event relevant to the failure
    assert fr  # non-empty


# --------------------------------------------------------------------------- #
# 22. Workspace artifacts persisted (the spec's seven files)                  #
# --------------------------------------------------------------------------- #

def test_seven_workspace_artifacts_persisted(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    impl = Path(tmp_workspace_root) / f"workspace_{wid}" / "implementation"
    required = [
        "execution_context.json",
        "engine_state.json",
        "checkpoint.json",
        "node_history.json",
        "event_log.json",
        "execution_record.json",
        "build_result.json",
    ]
    for f in required:
        assert (impl / f).is_file(), f"missing: {f}"


def test_build_result_artifact_matches_returned_result(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r = eng.execute(workspace_id=wid, implementation_spec="spec")
    impl = Path(tmp_workspace_root) / f"workspace_{wid}" / "implementation"
    persisted = json.loads((impl / "build_result.json").read_text())
    assert persisted["execution_id"] == r.execution_id
    assert persisted["success"] == r.success


# --------------------------------------------------------------------------- #
# 23. Cancel is honored at the next node boundary                             #
# --------------------------------------------------------------------------- #

def test_cancel_marks_context_cancelled(tmp_workspace_root):
    wid = _make_workspace()
    StateManager.save(ExecutionContext(
        workspace_id=wid, implementation_spec="spec",
        current_node=NODE_GENERATE_CODE))
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    assert eng.cancel(workspace_id=wid) is True
    ctx = StateManager.load(wid)
    assert ctx is not None and ctx.cancelled is True


def test_cancel_returns_false_when_no_state(tmp_workspace_root):
    wid = _make_workspace()  # no checkpoint
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    assert eng.cancel(workspace_id=wid) is False


# --------------------------------------------------------------------------- #
# 24. Status surface                                                          #
# --------------------------------------------------------------------------- #

def test_status_returns_unknown_when_no_state(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    st = eng.status(workspace_id=wid)
    assert st["execution_state"] == "unknown"


def test_status_returns_current_node(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    eng.execute(workspace_id=wid, implementation_spec="spec")
    st = eng.status(workspace_id=wid)
    assert st["current_node"] == NODE_FINISH
    assert st["test_passed"] is True


# --------------------------------------------------------------------------- #
# 25. NodeRegistry extension                                                  #
# --------------------------------------------------------------------------- #

def test_registry_registers_new_node_without_engine_modification():
    class ExtraNode(BaseNode):
        name = "ExtraNode"
        def execute(self, ctx): return {"ok": True}
        def validate(self, ctx): return []
    reg = NodeRegistry()
    reg.register(ExtraNode())
    assert reg.get("ExtraNode") is not None


def test_registry_rejects_unnamed_node():
    class NoName(BaseNode):
        pass
    reg = NodeRegistry()
    with pytest.raises(ValueError):
        reg.register(NoName())


def test_default_registry_has_five_required_nodes():
    agents = AgentManager(coding_agent_fn=_good_agent,
                          testing_agent_fn=_good_tests)
    reg = default_registry(agents, RetryPolicy())
    for name in (NODE_GENERATE_CODE, NODE_RUN_TESTS, NODE_ANALYZE_FAILURE,
                 NODE_REPAIR, NODE_FINISH):
        assert reg.get(name) is not None


# --------------------------------------------------------------------------- #
# 26. CodingAgent mandatory validation                                         #
# --------------------------------------------------------------------------- #

def test_coding_agent_rejects_empty_prompt():
    agent = CodingAgent(lambda p, wd: {})
    with pytest.raises(CodingAgentError):
        agent.run("ws-id", "", ctx=None)


def test_coding_agent_rejects_missing_input():
    agent = CodingAgent(lambda p, wd: {})
    with pytest.raises(CodingAgentError):
        agent.run("", "prompt", ctx=None)


def test_coding_agent_rejects_non_dict_response():
    agent = CodingAgent(lambda p, wd: "not a dict")  # type: ignore[arg-type]
    with pytest.raises(CodingAgentError):
        agent.run("ws-id", "prompt", ctx=None)


def test_coding_agent_rejects_silent_success_no_output():
    agent = CodingAgent(lambda p, wd: {})
    with pytest.raises(CodingAgentError):
        agent.run("ws-id", "prompt", ctx=None)


# --------------------------------------------------------------------------- #
# 27. RetryPolicy                                                              #
# --------------------------------------------------------------------------- #

def test_retry_policy_is_eligible_under_cap(tmp_workspace_root):
    p = RetryPolicy(max_attempts=3)
    ctx = ExecutionContext(failure_analysis="something broke")
    ctx.current_attempt = 1
    assert p.is_eligible(ctx) is True
    ctx.current_attempt = 3
    assert p.is_eligible(ctx) is False


def test_retry_policy_not_eligible_without_analysis():
    p = RetryPolicy(max_attempts=3)
    ctx = ExecutionContext(failure_analysis="")
    assert p.is_eligible(ctx) is False


def test_retry_policy_should_terminate_at_cap():
    p = RetryPolicy(max_attempts=2)
    ctx = ExecutionContext()
    ctx.current_attempt = 2
    assert p.should_terminate(ctx) is True
    ctx.current_attempt = 1
    assert p.should_terminate(ctx) is False


# --------------------------------------------------------------------------- #
# 28. EventBus observer contract                                              #
# --------------------------------------------------------------------------- #

def test_event_bus_observers_do_not_break_on_exception():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: seen.append(e))
    bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.emit(__import__("implementation_engine").Event(
        "Test", "now", {"x": 1}))
    assert len(seen) == 1


def test_event_bus_emits_lifecycle_events(tmp_workspace_root):
    wid = _make_workspace()
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    events_seen = {"types": set()}
    eng.event_bus.subscribe(
        lambda e: events_seen["types"].add(e.type))
    eng.execute(workspace_id=wid, implementation_spec="spec")
    assert EV_EXECUTION_STARTED in events_seen["types"]
    assert EV_NODE_STARTED in events_seen["types"]
    assert EV_NODE_COMPLETED in events_seen["types"]
    assert EV_CHECKPOINT_SAVED in events_seen["types"]
    assert EV_EXECUTION_COMPLETED in events_seen["types"]


# --------------------------------------------------------------------------- #
# 29. Pure-stdlib at import (no provider pulled in transitively)             #
# --------------------------------------------------------------------------- #

def test_no_forbidden_modules_transitively_imported():
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_set = (
        "{'cerebras_client','ollama_client','openrouter_client','groq_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium',"
        "'requests','httpx','urllib3'}"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import implementation_engine; "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: stdout={probe.stdout!r} "
        f"stderr={probe.stderr!r}"
    )
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "implementation_engine transitively imports forbidden modules"
    )


# --------------------------------------------------------------------------- #
# 30. Determinism: same inputs → same final_node                              #
# --------------------------------------------------------------------------- #

def test_deterministic_final_node_for_same_inputs(tmp_workspace_root):
    w1 = _make_workspace("determinism")
    w2 = _make_workspace("determinism")
    eng = NativeImplementationEngine(
        coding_agent_fn=_good_agent, testing_agent_fn=_good_tests)
    r1 = eng.execute(workspace_id=w1, implementation_spec="same spec")
    r2 = eng.execute(workspace_id=w2, implementation_spec="same spec")
    assert r1.success == r2.success
    assert r1.final_node == r2.final_node
    assert r1.test_results == r2.test_results


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_implementation_engine.py -q")
