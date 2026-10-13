"""
test_phase2_pipeline_integrity.py - Phase 2: pin the elimination of the
answer-156 false-success class.

The runtime observed in the live workspace f334026c-… was:

    build_state.json:
      "status":             "executed"
      "spec_status":        "incomplete"
      "spec_missing_sections": [
          "components", "file structure", "dependencies", "constraints",
          "testing requirements", "acceptance criteria"
      ]
      "repair_log":         ["[attempt 0] agent success=True;
                              output=What would you like me to do with this?
                              It's a decision record (sqlite3 + repository
                              pattern + versioned SQL migrations) with no
                              actionable task stated."]
      "execution_success":  true   <- FALSE POSITIVE

The synthesis path produced a decision record (only "# Verified Decisions" /
"# Architecture") instead of an Implementation Spec, then shipped it to the
coding agent as the plan. The agent correctly said "no actionable task", the
test runner found no tests to run (so `failed == 0`), and the existing
``_tests_passed`` returned True. ``execution_success`` was bound to "agent
didn't crash" rather than to spec completeness + artefact presence + tests
executed + tests passed.

Phase 2 fixes this class via three independent gates (each closes a separate
hole, so a regression in any one still leaves the build correctly halted):

  1. SPEC-COMPLETENESS GATE in build_pipeline.execute_build_spec. If the
     workspace's build_state.json carries `spec_status` set to anything other
     than "completed", the agent is never invoked. Returns
     ``{"blocked": True, "blocked_reason": "incomplete_spec", ...}``.
  2. STRENGTHENED `_tests_passed` in implementation_executor. Requires
     ``runner != "none"``, ``passed >= 1``, ``failed == 0`` and
     ``exit_code == 0``. The no-tests-ran shape (passed=0, failed=0,
     exit_code=5) is now a hard failure rather than a false pass.
  3. ARTEFACT-PRESENCE CHECK in implementation_executor._execute_with_repair_loop.
     An agent that returned success=True without creating any new file in
     the working directory is treated as a non-answer (the "What would you
     like me to do?" case). Failure is reported before the test runner is
     invoked, so the repair loop is never wasted on a non-answer.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_pipeline as bp
import build_workspace as bw
import build_spec_engine as bse
import implementation_executor as ie
from coding_agents.base_agent import AgentResult
from implementation_executor import (
    MAX_REPAIR_ATTEMPTS,
    _list_workspace_files,
    _tests_passed,
    execute_implementation,
)


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #

def _fresh_workspace_root():
    """Point WORKSPACE_ROOT at a tmp dir and return (tmp, root)."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    return Path(tmp), tmp


def _good_spec_text():
    """A spec with all 6 EXECUTABLE_SPEC_SECTIONS present
    (validate_executable_spec_sections → []). After the artefact-selection
    fix the production default emits this shape — the defence-in-depth gate
    in execute_build_spec re-validates the on-disk spec text against
    validate_executable_spec_sections and blocks any spec missing any of
    these. The legacy 7-section "decision record" shape is now blocked."""
    return (
        "# Implementation Objective\nBuild the auth module with PostgreSQL.\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement main.\n\n"
        "# Acceptance Criteria\npytest exits 0 with >=1 pass.\n\n"
        "# Output Contract\nStop after tests pass; do not commit.\n"
    )


def _incomplete_spec_text():
    """The answer-156-spec shape: only "# Verified Decisions" / "# Architecture"
    sections. The build_state is set to spec_status=incomplete (legacy 6
    legacy SPEC_SECTIONS missing); the existing ``spec_status != "completed"``
    gate in execute_build_spec fires first and blocks. The defence-in-depth
    ``validate_executable_spec_sections`` gate would also flag all 6
    EXECUTABLE_SPEC_SECTIONS missing for this spec text — both gates catch
    the same regression class independently."""
    return (
        "# Verified Decisions\n\nDecision Count: 1\n\n"
        "### Database Access Strategy: sqlite3 with a repository pattern\n\n"
        "Winner:\nPython's built-in sqlite3 with a repository pattern and "
        "versioned SQL migrations.\n\n"
        "# Architecture\n\nNo verified architectural decisions recorded.\n\n"
        "# Implementation Plan\n\nNo implementation steps; nothing verified.\n"
    )


def _agent_should_not_be_called(spec_text):
    """A mock executor that fails the test if it is ever invoked. Used so a
    gate-bypass regression in execute_build_spec produces a clear failure
    rather than a silent execution success."""
    raise AssertionError(
        "execute_build_spec dispatched to the executor despite the spec-"
        f"completeness gate; spec was {spec_text!r}")


# --------------------------------------------------------------------------- #
# 1. Spec-completeness gate                                                   #
# --------------------------------------------------------------------------- #

def test_incomplete_spec_blocks_execution_at_execute_build_spec() -> None:
    """The Phase 2 gate: build_state.json carries spec_status=incomplete, so
    execute_build_spec returns blocked without dispatching to the executor."""
    tmp, _ = _fresh_workspace_root()
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _incomplete_spec_text())
    bw.write_build_state(
        wid, "spec_generated",
        spec_status="incomplete",
        spec_missing_sections=[
            "components", "file structure", "dependencies", "constraints",
            "testing requirements", "acceptance criteria",
        ],
    )

    result = bp.execute_build_spec(wid, executor_fn=_agent_should_not_be_called)
    assert result.get("blocked") is True, result
    assert result["blocked_reason"] == "incomplete_spec", result
    assert result["success"] is False, result
    assert result["spec_status"] == "incomplete", result
    assert set(result["spec_missing_sections"]) == {
        "components", "file structure", "dependencies", "constraints",
        "testing requirements", "acceptance criteria",
    }
    assert result["workspace_id"] == wid


def test_complete_spec_passes_gate_and_dispatches() -> None:
    """When spec_status is "completed" the gate does NOT block — the existing
    success path runs. After the artefact-selection fix the mock executor
    MUST also create the files declared in ``# Files to Create`` so the
    post-execution QualityGate confirms required files exist (the new
    success contract)."""
    tmp, _ = _fresh_workspace_root()
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _good_spec_text())
    bw.write_build_state(wid, "spec_generated", spec_status="completed")

    # Pre-create the files the spec declares; the mock executor simulates
    # OpenCode writing them, then the post-execution QualityGate verifies
    # they exist on disk.
    ws = bw._workspace_dir(wid)
    (ws / "src" / "app").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "app" / "main.py").write_text("VALUE = 1\n")
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "tests" / "test_app.py").write_text("def test_value(): assert True\n")

    def _mock_executor(spec_text):
        return {"success": True, "output": "ok", "test_results": {},
                "repair_attempts": 0, "error": ""}

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)
    assert not result.get("blocked"), result
    assert result["success"] is True, result
    assert result.get("error", "") == "", result
    # The post-execution QualityGate passed.
    assert result["quality_gate"]["passed"] is True, result["quality_gate"]


def test_absent_spec_status_keeps_existing_behaviour() -> None:
    """Back-compat: workspaces created by older callers have no spec_status
    field on disk. The existing gate skips — but the new defence-in-depth
    executable-spec gate STILL runs (it does not depend on spec_status), so
    the spec text MUST carry the executable-spec sections to dispatch. The
    mock executor also MUST write the declared files so the post-execution
    QualityGate passes."""
    tmp, _ = _fresh_workspace_root()
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _good_spec_text())
    # NOTE: deliberately do NOT call write_build_state — no spec_status field.

    ws = bw._workspace_dir(wid)
    (ws / "src" / "app").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "app" / "main.py").write_text("VALUE = 1\n")
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "tests" / "test_app.py").write_text("def test_value(): assert True\n")

    def _mock_executor(spec_text):
        return {"success": True, "output": "ok", "test_results": {},
                "repair_attempts": 0, "error": ""}

    result = bp.execute_build_spec(wid, executor_fn=_mock_executor)
    assert not result.get("blocked"), result
    assert result["success"] is True, result


def test_incomplete_spec_blocks_through_run_build_pipeline() -> None:
    """End-to-end: run_build_pipeline with a debate that produces an
    incomplete spec (only "# Architecture" — validate_spec_sections returns 6
    missing) must halt before the agent runs. The pipeline returns with the
    full_uri block reason and build_state.json records block_incomplete_spec."""
    tmp, _ = _fresh_workspace_root()

    def _fake_debate_incomplete(prompt, memory_ids=None):
        # The synthesized_answer IS the spec text — only "# Architecture"
        # present, so validate_spec_sections returns 6 missing sections.
        return {
            "status": "completed",
            "synthesized_answer": "# Architecture\nOnly this section.\n",
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 80,
        }

    def _agent_should_not_be_called(spec_text):
        raise AssertionError(
            "run_build_pipeline dispatched to the executor despite the spec-"
            "completeness gate")

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate_incomplete,
        executor_fn=_agent_should_not_be_called,
    )
    assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
    assert result["spec_status"] == "incomplete", result
    assert set(result["spec_missing_sections"]) == {
        "components", "file structure", "dependencies", "constraints",
        "testing requirements", "acceptance criteria",
    }
    exec = result["execution_result"]
    assert exec.get("blocked") is True, exec
    assert exec.get("blocked_reason") == "incomplete_spec", exec
    assert exec.get("success") is False, exec

    # Build state recorded the block reason.
    state = bw._workspace_dir(result["workspace_id"]) / "build_state.json"
    import json
    saved = json.loads(state.read_text())
    # The exact status string recorded by run_build_pipeline:
    assert saved["status"].startswith("blocked_incomplete_spec"), saved
    assert saved.get("spec_status") == "incomplete", saved
    # The strengthened success contract must NOT have been written True here.
    assert saved.get("execution_success") is not True, saved


# --------------------------------------------------------------------------- #
# 2. Strengthened _tests_passed                                               #
# --------------------------------------------------------------------------- #

def test_tests_passed_requires_passing_count() -> None:
    """`passed == 0` must FAIL — a no-tests-ran workspace does not pass Phase 2.
    Before Phase 2 this returned True because failed == 0; that is how
    answer 156 reached execution_success=True."""
    assert _tests_passed({
        "runner": "pytest", "passed": 0, "failed": 0, "exit_code": 5,
        "output": "no tests ran in 0.02s",
    }) is False


def test_tests_passed_requires_clean_exit_code() -> None:
    """A non-zero exit_code must FAIL even if there were no parsing-visible
    failures. The runner aborted (e.g. collection error) — that is a failure."""
    assert _tests_passed({
        "runner": "pytest", "passed": 0, "failed": 0, "exit_code": 2,
        "output": "collection error",
    }) is False


def test_tests_passed_requires_real_runner() -> None:
    """`runner == 'none'` (no test runner found at all) must FAIL."""
    assert _tests_passed({
        "runner": "none", "passed": 0, "failed": 0, "exit_code": -1,
        "output": "",
    }) is False


def test_tests_passed_accepts_real_pass() -> None:
    """Sanity: a clean pytest run with multiple passing tests still passes."""
    assert _tests_passed({
        "runner": "pytest", "passed": 7, "failed": 0, "exit_code": 0,
        "output": "7 passed in 1.2s",
    }) is True


def test_tests_passed_rejects_failures_even_with_passes() -> None:
    """Mixed pass/fail is still a failure (the legacy scenario — repair loop
    feeds the failed-test output back)."""
    assert _tests_passed({
        "runner": "pytest", "passed": 5, "failed": 2, "exit_code": 1,
        "output": "5 passed, 2 failed",
    }) is False


# --------------------------------------------------------------------------- #
# 3. Artefact-presence check                                                  #
# --------------------------------------------------------------------------- #

def test_list_workspace_files_excludes_caches() -> None:
    """The walker must skip .pytest_cache and __pycache__ so they don't
    masquerade as agent-produced artefacts."""
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "answer.json").write_text("{}")
    (Path(tmp) / ".pytest_cache").mkdir()
    (Path(tmp) / ".pytest_cache" / "lastfailed").write_text("[]")
    (Path(tmp) / "subdir").mkdir()
    (Path(tmp) / "subdir" / "__pycache__").mkdir()
    (Path(tmp) / "subdir" / "__pycache__" / "x.pyc").write_text("")
    (Path(tmp) / "subdir" / "module.py").write_text("pass")

    files = _list_workspace_files(tmp)
    assert files == {"answer.json", os.path.join("subdir", "module.py")}, files


def test_list_workspace_files_returns_none_for_missing_dir() -> None:
    """A falsy or non-existent working_dir yields None so callers can skip
    the artefact check (test seams that bypass the file system pass no
    working_dir)."""
    assert _list_workspace_files(None) is None
    assert _list_workspace_files("") is None
    assert _list_workspace_files("/does/not/exist") is None


def test_agent_no_artefact_is_failure() -> None:
    """The answer-156 shape: agent returns success=True with a non-answer
    ("What would you like me to do?") and produces NO new files in the
    working directory. Phase 2 must treat this as failure -硬化 against the
    false-success that reached build_state in answer 156."""
    tmp, _ = _fresh_workspace_root()
    wid = bw.create_workspace()
    working_dir = str(bw._workspace_dir(wid))

    def fake_agent_plan_non_answer(plan, working_dir=None, timeout=None):
        # The exact 156 agent output.
        return AgentResult(
            success=True,
            output=("What would you like me to do with this? It's a decision "
                    "record (sqlite3 + repository pattern + versioned SQL "
                    "migrations) with no actionable task stated."),
        )

    def fake_tests_no_tests(working_dir=None):
        # The exact 156 test result — no tests ran.
        return {"runner": "pytest", "passed": 0, "failed": 0, "exit_code": 5,
                "output": "no tests ran in 0.02s"}

    result = execute_implementation(
        plan="# Verified Decisions\nno actionable task\n",
        workspace_id=wid,
        working_dir=working_dir,
        use_claude_code=True,
        agent_fn=fake_agent_plan_non_answer,
        test_fn=fake_tests_no_tests,
        max_repairs=3,
    )

    assert result["success"] is False, result
    assert "no artefact" in result["error"].lower(), result["error"]
    assert result["repair_attempts"] == 0, result
    # Build state recorded the no-artefact block (NOT "executed" with
    # execution_success=True as it did for answer 156).
    import json
    state = json.loads((bw._workspace_dir(wid) / "build_state.json").read_text())
    assert state["status"] == "agent_no_artefact", state
    assert state.get("execution_success") is False, state


def test_agent_producing_artefact_passes_artefact_gate() -> None:
    """Sanity: when the agent actually writes a new file to the working
    directory, the artefact-presence check passes and the test runner runs."""
    tmp, _ = _fresh_workspace_root()
    wid = bw.create_workspace()
    working_dir = str(bw._workspace_dir(wid))

    def fake_agent_writes_file(plan, working_dir=None, timeout=None):
        # A REAL implementation would write actual source code; for the test
        # we just write one .py file so the artefact-presence check sees a
        # delta and lets the flow proceed.
        (Path(working_dir) / "implementation.py").write_text("VALUE = 42\n")
        return AgentResult(success=True, output="Created implementation.py")

    def fake_tests_passing(working_dir=None):
        return {"runner": "pytest", "passed": 3, "failed": 0, "exit_code": 0,
                "output": "3 passed"}

    result = execute_implementation(
        plan="# Architecture\nLayered service.\n# Components\nAPI.\n",
        workspace_id=wid,
        working_dir=working_dir,
        use_claude_code=True,
        agent_fn=fake_agent_writes_file,
        test_fn=fake_tests_passing,
        max_repairs=3,
    )

    assert result["success"] is True, result
    assert result["repair_attempts"] == 0, result


# --------------------------------------------------------------------------- #
# 4. End-to-end: the answer-156 lineage cannot produce execution_success=True #
# --------------------------------------------------------------------------- #

def test_answer156_lineage_cannot_reach_execution_success_true() -> None:
    """End-to-end regression: with a debate that returns the answer-156 spec
    shape (the audit format instead of an Implementation Spec), the pipeline
   halts cleanly with execution_blocked and execution_success is never set
    True. Before Phase 1+2, the 156 lineage recorded
    ``execution_success=true`` while ``spec_status=incomplete`` — that
    contradiction is now impossible by construction."""
    tmp, _ = _fresh_workspace_root()

    def _fake_debate_156_spec(prompt, memory_ids=None):
        # Synthesize_answer's determinstic path returns the audit format,
        # which lacks every SPEC_SECTION — validate_spec_sections flags 6
        # missing. Before Phase 1+2, this exact shape reached the agent.
        return {
            "status": "completed",
            "synthesized_answer": _incomplete_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 100,
        }

    def _agent_should_not_be_called(spec_text):
        raise AssertionError(
            f"156-lineage regression: agent ran despite incomplete spec: "
            f"{spec_text!r}")

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate_156_spec,
        executor_fn=_agent_should_not_be_called,
    )
    # The contradiction class is closed at every level:
    assert result["spec_status"] == "incomplete", result
    exec = result["execution_result"]
    assert exec.get("blocked") is True, exec
    assert exec.get("blocked_reason") == "incomplete_spec", exec
    assert exec.get("success") is False, exec

    # build_state never records execution_success=True for an incomplete spec.
    import json
    state = json.loads((bw._workspace_dir(result["workspace_id"])
                        / "build_state.json").read_text())
    assert state.get("execution_success") is not True, state
    assert state["status"].startswith("blocked_incomplete_spec"), state


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
