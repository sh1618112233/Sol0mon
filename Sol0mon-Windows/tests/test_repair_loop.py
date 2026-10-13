"""
test_repair_loop.py - Autonomous Repair Loop (Phase 1).

Offline only. No model calls. No subprocess. No browser. No external APIs.

The repair loop wraps the existing Build Spec -> OpenCode Agent -> Run Tests
path. When tests fail, the executor captures the failure output, builds a
repair prompt, re-invokes the OpenCode agent, and re-tests. It repeats until
the tests pass or MAX_REPAIR_ATTEMPTS is exhausted. On exhaustion it emits a
HUMAN_REQUIRED decision via human_decision_manager (no silent failure) and
records the audit trail in build_state.json.

Verified contracts:
  1. successful repair after one retry (repair_attempts == 1, success == True).
  2. multiple repair attempts then pass (repair_attempts == 2).
  3. exhausted repairs -> HUMAN_REQUIRED persisted; no silent failure.
  4. repair history persisted in build_state.json (repair_log + repair_attempts).
  5. failed test output is fed into the repair prompt sent to the agent.
  6. no repair when tests pass on the first run (agent invoked exactly once).
  7. workspace resume retains repair history (does not silently re-run).

The agent invocation and the test runner are both injected via seams
(``agent_fn`` and ``test_fn``), so nothing reaches the network, a subprocess,
or an LLM.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import human_decision_manager as hdm
from coding_agents.base_agent import AgentResult
from implementation_executor import (
    MAX_REPAIR_ATTEMPTS,
    ImplementationResult,
    execute_implementation,
)


# --------------------------------------------------------------------------- #
# Fake seams                                                                  #
# --------------------------------------------------------------------------- #

class FakeAgent:
    """Programmable agent.run seam. Returns a scripted sequence of
    AgentResults and records every plan string it is handed."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, plan, working_dir=None, timeout=None):
        self.calls.append(plan)
        if not self.results:
            raise AssertionError("FakeAgent ran out of scripted results")
        return self.results.pop(0)


class FakeTests:
    """Programmable test runner seam. Returns a scripted sequence of test
    result dicts (the same shape _run_tests returns)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, working_dir=None):
        self.calls.append(working_dir)
        if not self.results:
            raise AssertionError("FakeTests ran out of scripted results")
        return self.results.pop(0)


def _ok_output(marker="initial"):
    return f"[{marker}] agent produced code"


def _tests(failed, output="", passed=0, exit_code=None):
    return {
        "runner": "pytest",
        "passed": passed,
        "failed": failed,
        "exit_code": 0 if exit_code is None and failed == 0 else (exit_code if exit_code is not None else 1),
        "output": output,
    }


def _setup_workspace():
    """Create an isolated build workspace root + a fresh workspace. Returns
    (tmpdir_path, workspace_id)."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    wid = bw.create_workspace(source_question="repair loop test")
    return Path(tmp), wid


def _build_state(tmp, wid):
    return json.loads((tmp / f"workspace_{wid}" / "build_state.json").read_text())


# --------------------------------------------------------------------------- #
# 1. successful repair after one retry                                        #
# --------------------------------------------------------------------------- #

def test_successful_repair_after_one_retry():
    """Initial agent run succeeds, tests fail, one repair, tests pass.
    repair_attempts == 1, success == True."""
    tmp, wid = _setup_workspace()

    agent = FakeAgent([
        AgentResult(success=True, output=_ok_output("initial")),
        AgentResult(success=True, output=_ok_output("repair-1")),
    ])
    tests = FakeTests([
        _tests(failed=2, output="FAILED test_x\nFAILED test_y"),
        _tests(failed=0, passed=5),
    ])

    result = execute_implementation(
        plan="Build a hello module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent,
        test_fn=tests,
    )

    assert result["success"] is True
    assert result["repair_attempts"] == 1
    assert len(agent.calls) == 2  # initial + one repair
    assert len(tests.calls) == 2
    state = _build_state(tmp, wid)
    assert state["status"] == "executed"
    assert state["repair_attempts"] == 1


# --------------------------------------------------------------------------- #
# 2. multiple repair attempts then pass                                       #
# --------------------------------------------------------------------------- #

def test_multiple_repair_attempts_then_pass():
    """Tests fail twice, pass on the third agent run. repair_attempts == 2."""
    tmp, wid = _setup_workspace()

    agent = FakeAgent([
        AgentResult(success=True, output=_ok_output("initial")),
        AgentResult(success=True, output=_ok_output("repair-1")),
        AgentResult(success=True, output=_ok_output("repair-2")),
    ])
    tests = FakeTests([
        _tests(failed=1, output="FAILED test_a"),
        _tests(failed=1, output="FAILED test_b"),
        _tests(failed=0, passed=3),
    ])

    result = execute_implementation(
        plan="Build a counter module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent,
        test_fn=tests,
    )

    assert result["success"] is True
    assert result["repair_attempts"] == 2
    assert len(agent.calls) == 3
    assert len(tests.calls) == 3
    state = _build_state(tmp, wid)
    assert state["status"] == "executed"
    assert state["repair_attempts"] == 2


# --------------------------------------------------------------------------- #
# 3. exhausted repairs -> HUMAN_REQUIRED (no silent failure)                  #
# --------------------------------------------------------------------------- #

def test_exhausted_repairs_emits_human_required():
    """All MAX_REPAIR_ATTEMPTS repairs run, tests still fail. The executor
    must NOT silently return failure; it must persist a HUMAN_REQUIRED
    decision to human_decisions.json and mark build_state accordingly."""
    tmp, wid = _setup_workspace()

    agent = FakeAgent([
        AgentResult(success=True, output=_ok_output("initial")),
        AgentResult(success=True, output=_ok_output("repair-1")),
        AgentResult(success=True, output=_ok_output("repair-2")),
        AgentResult(success=True, output=_ok_output("repair-3")),
    ])
    tests = FakeTests([
        _tests(failed=4, output="FAILED test_1"),
        _tests(failed=4, output="FAILED test_1"),
        _tests(failed=4, output="FAILED test_1"),
        _tests(failed=4, output="FAILED test_1"),
    ])

    result = execute_implementation(
        plan="Build a broken module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent,
        test_fn=tests,
    )

    assert result["success"] is False
    assert result["repair_attempts"] == MAX_REPAIR_ATTEMPTS

    # HUMAN_REQUIRED persisted to human_decisions.json.
    pending = hdm.load_pending(wid)
    assert len(pending) == 1
    decision = pending[0]
    assert decision["outcome_type"] == "HUMAN_REQUIRED"
    assert decision["source"] == "implementation_executor.repair_loop"
    assert decision["repair_attempts"] == MAX_REPAIR_ATTEMPTS
    assert "exhausted" in decision["reasoning"].lower()

    # build_state.json reflects exhaustion.
    state = _build_state(tmp, wid)
    assert state["status"] == "repair_exhausted"
    assert state["repair_attempts"] == MAX_REPAIR_ATTEMPTS
    assert state.get("human_required") is True

    # Error message names HUMAN_REQUIRED explicitly (no silent failure).
    assert "HUMAN_REQUIRED" in result["error"]


# --------------------------------------------------------------------------- #
# 4. repair history persisted in build_state.json                             #
# --------------------------------------------------------------------------- #

def test_repair_history_persisted():
    """repair_log accumulates an entry per agent attempt and is persisted
    to build_state.json so a resume sees the full audit trail."""
    tmp, wid = _setup_workspace()

    agent = FakeAgent([
        AgentResult(success=True, output=_ok_output("initial")),
        AgentResult(success=True, output=_ok_output("repair-1")),
    ])
    tests = FakeTests([
        _tests(failed=2, output="FAILED test_x"),
        _tests(failed=0, passed=5),
    ])

    result = execute_implementation(
        plan="Build a fold module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent,
        test_fn=tests,
    )

    assert result["success"] is True
    assert result["repair_attempts"] == 1

    log = result["repair_log"]
    # Expect an entry for the initial attempt and an entry for the repair.
    assert any("attempt 0" in entry for entry in log)
    assert any("repair 1" in entry for entry in log)

    # Same audit trail persisted on disk.
    state = _build_state(tmp, wid)
    persisted_log = state.get("repair_log", [])
    assert persisted_log == log
    assert state["repair_attempts"] == 1


# --------------------------------------------------------------------------- #
# 5. failed test output is fed into the repair prompt                          #
# --------------------------------------------------------------------------- #

def test_failed_test_output_in_repair_prompt():
    """The repair prompt handed to the agent for the second call must contain
    the previous run's failed-test output, so the agent has the context it
    needs to fix the right thing."""
    tmp, wid = _setup_workspace()

    marker = "UNIQUE_FAILURE_MARKER_XYZ"
    agent = FakeAgent([
        AgentResult(success=True, output=_ok_output("initial")),
        AgentResult(success=True, output=_ok_output("repair-1")),
    ])
    tests = FakeTests([
        _tests(failed=1, output=marker),
        _tests(failed=0, passed=2),
    ])

    execute_implementation(
        plan="Build a marker module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent,
        test_fn=tests,
    )

    assert len(agent.calls) == 2
    repair_prompt = agent.calls[1]
    assert marker in repair_prompt
    assert "REPAIR ATTEMPT" in repair_prompt
    assert "Build a marker module" in repair_prompt  # original plan retained


# --------------------------------------------------------------------------- #
# 6. no repair when tests pass on the first run                                #
# --------------------------------------------------------------------------- #

def test_no_repair_when_tests_pass():
    """Tests pass on the first agent run. The agent is invoked exactly once,
    tests run exactly once, repair_attempts == 0, no HUMAN_REQUIRED."""
    tmp, wid = _setup_workspace()

    agent = FakeAgent([
        AgentResult(success=True, output=_ok_output("initial")),
    ])
    tests = FakeTests([
        _tests(failed=0, passed=7),
    ])

    result = execute_implementation(
        plan="Build a passing module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent,
        test_fn=tests,
    )

    assert result["success"] is True
    assert result["repair_attempts"] == 0
    assert len(agent.calls) == 1
    assert len(tests.calls) == 1
    # No human escalation when tests pass.
    assert hdm.load_pending(wid) == []
    state = _build_state(tmp, wid)
    assert state["status"] == "executed"
    assert state["repair_attempts"] == 0


# --------------------------------------------------------------------------- #
# 7. workspace resume retains repair history (does not silently re-run)        #
# --------------------------------------------------------------------------- #

def test_workspace_resume_retains_repair_history():
    """A prior exhausted run persisted repair_attempts=3 and status
    'repair_exhausted' to build_state.json. A fresh call to
    execute_implementation on the same workspace must NOT silently re-run the
    agent or tests (which would zero the counter and hide the prior
    escalation). It must re-emit HUMAN_REQUIRED and leave the history intact."""
    tmp, wid = _setup_workspace()

    # --- Run 1: exhaust the repair budget. ---
    agent1 = FakeAgent([
        AgentResult(success=True, output="initial"),
        AgentResult(success=True, output="repair-1"),
        AgentResult(success=True, output="repair-2"),
        AgentResult(success=True, output="repair-3"),
    ])
    tests1 = FakeTests([
        _tests(failed=1, output="FAILED test_z"),
        _tests(failed=1, output="FAILED test_z"),
        _tests(failed=1, output="FAILED test_z"),
        _tests(failed=1, output="FAILED test_z"),
    ])

    first = execute_implementation(
        plan="Build a persistent module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent1,
        test_fn=tests1,
    )
    assert first["success"] is False
    assert first["repair_attempts"] == MAX_REPAIR_ATTEMPTS

    # --- Run 2 (resume): fresh seams that WOULD pass immediately. ---
    agent2 = FakeAgent([AgentResult(success=True, output="would-succeed")])
    tests2 = FakeTests([_tests(failed=0, passed=99)])

    resumed = execute_implementation(
        plan="Build a persistent module",
        workspace_id=wid,
        use_claude_code=True,
        agent_fn=agent2,
        test_fn=tests2,
    )

    # The resume must NOT have invoked the fresh agent or tests at all —
    # otherwise it would silently erase the prior HUMAN_REQUIRED escalation.
    assert agent2.calls == []
    assert tests2.calls == []

    assert resumed["success"] is False
    assert resumed["repair_attempts"] == MAX_REPAIR_ATTEMPTS
    assert "HUMAN_REQUIRED" in resumed["error"]

    # HUMAN_REQUIRED is re-emitted on resume (not silently dropped).
    pending = hdm.load_pending(wid)
    assert len(pending) == 1
    assert pending[0]["outcome_type"] == "HUMAN_REQUIRED"
    assert pending[0]["repair_attempts"] == MAX_REPAIR_ATTEMPTS

    # History preserved on disk.
    state = _build_state(tmp, wid)
    assert state["repair_attempts"] == MAX_REPAIR_ATTEMPTS
    assert state["status"] == "repair_exhausted"


# --------------------------------------------------------------------------- #
# Default constant sanity                                                     #
# --------------------------------------------------------------------------- #

def test_max_repair_attempts_constant():
    """MAX_REPAIR_ATTEMPTS is 3, as agreed for Phase 1."""
    assert MAX_REPAIR_ATTEMPTS == 3


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_repair_loop.py -q")
