"""
test_implementation_pipeline.py - TEST 4.

Input:  Implementation Spec
Expected: Implementation Executor accepts spec (mock execution only)

Verifies Path D: build_spec -> implementation_executor.
Verifies Path E: implementation_executor -> coding_agent_router.

Does NOT invoke Claude Code. Mock execution only.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make the project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
from coding_agent_router import get_agent, available_agents
from coding_agents.base_agent import BaseAgent, AgentResult


def _good_spec_text():
    """Hybrid spec: contains the legacy 7 audit-debate sections AND the 6
    executable-spec sections. Both validators pass — the spec reaches the
    executor and the mock writes the declared files so the post-execution
    QualityGate confirms completion."""
    return (
        # Legacy 7 sections:
        "# Architecture\nLayered.\n\n"
        "# Components\nAPI.\n\n"
        "# File Structure\nsrc/\n\n"
        "# Dependencies\nfastapi\n\n"
        "# Constraints\nMUST use PG.\n\n"
        "# Testing Requirements\npytest\n\n"
        # Executable-spec sections:
        "# Implementation Objective\nBuild the API module per the verified "
        "decisions.\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement main.\n\n"
        "# Acceptance Criteria\npass\n\n"
        "# Output Contract\nStop after tests pass.\n"
    )


def _mock_executor(spec_text):
    """Mock executor: accepts a spec string, returns a success result.
    Writes the files declared in the spec's # Files to Create so the post-
    execution QualityGate's "required files exist" check passes (a real
    executor writes them before returning)."""
    assert spec_text, "executor received empty spec"
    assert "implementation objective" in spec_text.lower()
    ws_dirs = list(bw.WORKSPACE_ROOT.glob("workspace_*"))
    if ws_dirs:
        ws = ws_dirs[0]
        (ws / "src" / "app").mkdir(parents=True, exist_ok=True)
        (ws / "src" / "app" / "main.py").write_text("VALUE = 1\n")
        (ws / "tests").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "test_app.py").write_text("def test_value():\n"
                                                  "    assert True\n")
    return {
        "success": True,
        "output": f"[MOCK EXECUTION] Implemented {len(spec_text)} chars of spec.",
        "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
        "repair_attempts": 0,
        "error": "",
    }


def test_executor_accepts_implementation_spec() -> None:
    """TEST 4: Implementation executor accepts the spec text."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)

    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _good_spec_text())

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)

    assert result["success"] is True
    assert result["test_results"]["passed"] == 5
    assert result["test_results"]["failed"] == 0
    assert "[MOCK EXECUTION]" in result["output"]


def test_executor_no_spec_returns_error() -> None:
    """Execute without a stored spec -> error."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    wid = bw.create_workspace()

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)
    assert "error" in result
    assert result["success"] is False


def test_coding_agent_router_route() -> None:
    """Path E: execute_build_spec routes to coding_agent_router."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _good_spec_text())

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)
    assert "coding_agent" in result
    agent = result["coding_agent"]
    assert agent is not None
    assert agent.name == "opencode"
    assert agent.workspace_id == wid


def test_coding_agent_router_registered() -> None:
    """Router has the opencode agent registered.

    Audit D3: the agent now has a real subprocess backend, so this test no
    longer invokes `run` with a non-empty plan (which would spawn a live
    opencode process). It asserts registration + the empty-plan fast path,
    which returns ``success=False`` without launching any subprocess.
    """
    assert "opencode" in available_agents()
    agent = get_agent("opencode", "ws-1")
    assert agent is not None
    assert isinstance(agent, BaseAgent)
    # Empty plan: the adapter rejects it without spawning opencode.
    rejected = agent.run("")
    assert rejected.success is False
    assert "empty plan" in rejected.error.lower()


def test_coding_agent_router_unknown_returns_none() -> None:
    assert get_agent("nonexistent", "ws-1") is None


def test_full_pipeline_spec_to_execution() -> None:
    """Full path: classify -> resurrect/create -> build spec -> execute (mock)."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)

    def _fake_debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 80,
        }

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate,
        executor_fn=_mock_executor,
    )

    assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
    assert result["spec_status"] == "completed"
    exec = result["execution_result"]
    assert exec["success"] is True
    assert exec["coding_agent"] is not None
    assert exec["coding_agent"].name == "opencode"


if __name__ == "__main__":
    test_executor_accepts_implementation_spec()
    test_executor_no_spec_returns_error()
    test_coding_agent_router_route()
    test_coding_agent_router_registered()
    test_coding_agent_router_unknown_returns_none()
    test_full_pipeline_spec_to_execution()
    print("test_implementation_pipeline: ALL PASSED")
