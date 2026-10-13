"""
Project Sol0mon — Implementation Executor + Autonomous Repair Loop.

Handles the final stage of IMPLEMENTATION workflows:

    Consensus → Synthesis → Implementation Plan → User Approval →
    OpenCode Agent → Code Generation → Testing → Repair → Retesting →
    Completion

The repair loop:
    1. Generate code via the OpenCode agent (coding_agent_router.get_agent).
    2. Run tests (``_run_tests``).
    3. If tests fail: build a repair prompt embedding the failed-test output,
       feed it back to the agent.
    4. Retest.
    5. Repeat until tests pass or ``MAX_REPAIR_ATTEMPTS`` is exhausted.
    6. On exhaustion: persist a HUMAN_REQUIRED decision via
       ``human_decision_manager.save_pending`` (no silent failure) and record
       the audit trail in ``build_state.json``.

Supports checkpoint recovery — repair attempts and the repair log are
persisted to ``build_state.json`` after every attempt, so a crashed run
resumes from the last checkpoint rather than from zero. A prior exhausted
run is never silently re-run; the executor re-emits HUMAN_REQUIRED instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEST_TIMEOUT_SECONDS = 120
MAX_REPAIR_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Implementation result
# ---------------------------------------------------------------------------


@dataclass
class ImplementationResult:
    """Structured result of an implementation execution."""

    success: bool
    output: str = ""
    test_results: dict = field(default_factory=dict)
    repair_attempts: int = 0
    repair_log: list[str] = field(default_factory=list)
    error: str = ""
    executed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "test_results": self.test_results,
            "repair_attempts": self.repair_attempts,
            "repair_log": self.repair_log,
            "error": self.error,
            "executed_at": self.executed_at,
        }


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


def _run_tests(working_dir: str | None = None) -> dict:
    """Run the project test suite and return results.

    Returns:
        dict with keys: passed, failed, output, exit_code
    """
    try:
        # First try pytest
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--tb=short", "-x"],
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
            cwd=working_dir,
        )
        output = result.stdout + result.stderr
        # Parse test counts
        passed = output.count("PASSED") + output.count(" passed")
        failed = output.count("FAILED") + output.count(" failed")
        return {
            "runner": "pytest",
            "passed": passed,
            "failed": failed,
            "exit_code": result.returncode,
            "output": output[-3000:],  # Last 3k chars
        }
    except FileNotFoundError:
        # Fall back to test_suite.py
        try:
            result = subprocess.run(
                [sys.executable, "test_suite.py"],
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT_SECONDS,
                cwd=working_dir,
            )
            output = result.stdout + result.stderr
            # Parse from test_suite.py output format
            import re
            m = re.search(r'RESULTS:\s*(\d+)\s*passed,\s*(\d+)\s*failed', output)
            if m:
                passed = int(m.group(1))
                failed = int(m.group(2))
            else:
                passed = 0
                failed = 0
            return {
                "runner": "test_suite",
                "passed": passed,
                "failed": failed,
                "exit_code": result.returncode,
                "output": output[-3000:],
            }
        except Exception as e:
            return {
                "runner": "none",
                "passed": 0,
                "failed": 0,
                "exit_code": -1,
                "output": str(e),
            }


# ---------------------------------------------------------------------------
# Repair loop helpers
# ---------------------------------------------------------------------------


def _tests_passed(test_results: dict) -> bool:
    """True if the test result dict indicates a passing suite.

    Phase 2 strengthening: requires both that tests actually ran AND that
    at least one passed AND zero failed AND exit_code == 0. The legacy check
    (``failed == 0`` alone) false-passed on a workspace with no test files
    (pytest exits non-zero with no 'FAILED'/'failed' substring when it has
    nothing to collect), which is how answer 156 reached
    ``execution_success=True`` with an incomplete spec, no agent artefacts,
    and no tests. ``runner == "none"`` means no test runner was found at all;
    treat that as a hard failure too.
    """
    return (
        test_results.get("runner") != "none"
        and test_results.get("passed", 0) >= 1
        and test_results.get("failed", 1) == 0
        and test_results.get("exit_code", 1) == 0
    )


def _build_repair_prompt(
    plan: str, test_results: dict, attempt: int, max_repairs: int
) -> str:
    """Construct the repair prompt fed back to the coding agent.

    Embeds the original plan and the failed-test output so the agent has the
    exact context it needs to fix the failing tests without rewriting working
    code. """
    failed_output = (test_results or {}).get("output", "")
    return (
        f"REPAIR ATTEMPT {attempt}/{max_repairs}\n\n"
        "The previous implementation failed its test suite. Apply the minimal "
        "change that makes the failing tests below pass. Do not rewrite code "
        "that is already working.\n\n"
        f"ORIGINAL IMPLEMENTATION PLAN:\n{plan}\n\n"
        f"FAILED TEST OUTPUT:\n{failed_output}\n\n"
        "Repair the failing tests now."
    )


def _load_build_state(workspace_id: str) -> dict:
    """Read build_state.json for a workspace. Returns {} if absent/corrupt.

    ponytail: inlined here rather than added to build_workspace — keeps the
    repair-loop logic and its persistence concern in one file. """
    if not workspace_id:
        return {}
    from build_workspace import _workspace_dir
    p = _workspace_dir(workspace_id) / "build_state.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _list_workspace_files(working_dir: str | None) -> set[str] | None:
    """Return the set of relative file paths in working_dir, excluding
    pytest cache and pycache. Returns None when ``working_dir`` is falsy or
    not a directory, so callers can branch on "skip the artefact-presence
    check" by checking for None (test seams that bypass the file system
    pass no working_dir)."""
    if not working_dir or not os.path.isdir(working_dir):
        return None
    out: set[str] = set()
    for root, dirs, files in os.walk(working_dir):
        dirs[:] = [d for d in dirs if d not in (".pytest_cache", "__pycache__")]
        for f in files:
            out.add(os.path.relpath(os.path.join(root, f), working_dir))
    return out


def _emit_human_required(
    workspace_id: str,
    plan: str,
    test_results: dict,
    repair_attempts: int,
    max_repairs: int,
) -> dict:
    """Persist a HUMAN_REQUIRED decision + record exhaustion in build_state.

    This is the single point where the repair loop escalates to a human. It
    never silently returns failure. """
    from build_workspace import write_build_state
    from human_decision_manager import save_pending

    last_output = (test_results or {}).get("output", "")[:500]
    decision = {
        "decision_id": f"repair_exhausted_{workspace_id[:8] if workspace_id else 'no_ws'}",
        "dispute_key": "implementation_repair",
        "dispute_text": "Implementation repair loop exhausted — tests still failing",
        "option_a": "Manual code fix",
        "option_b": "Abort build",
        "reasoning": (
            f"OpenCode agent exhausted its repair budget ({repair_attempts}/"
            f"{max_repairs} attempts) and could not make the failing tests "
            f"pass. Manual intervention required."
        ),
        "outcome_type": "HUMAN_REQUIRED",
        "source": "implementation_executor.repair_loop",
        "repair_attempts": repair_attempts,
        "last_test_output": last_output,
    }
    if workspace_id:
        save_pending(workspace_id, [decision])
        write_build_state(
            workspace_id, "repair_exhausted",
            repair_attempts=repair_attempts,
            human_required=True,
            last_test_output=last_output,
        )
    return decision


def _execute_with_repair_loop(
    plan: str,
    workspace_id: str,
    working_dir: str | None,
    agent_fn: Callable[..., Any],
    test_fn: Callable[..., dict],
    max_repairs: int,
) -> ImplementationResult:
    """Initial implementation → test → repair loop → HUMAN_REQUIRED on exhaust.

    The agent invocation (``agent_fn``) and test runner (``test_fn``) are
    seams: production wires them to ``OpenCodeAgent.run`` and ``_run_tests``;
    tests inject fakes so nothing reaches a subprocess or a model.

    Every attempt persists ``repair_attempts`` and ``repair_log`` to
    ``build_state.json`` so a crashed run resumes from the last checkpoint.
    A prior exhausted run is never silently re-run. """
    from build_workspace import write_build_state

    now = lambda: datetime.now(timezone.utc).isoformat()

    # Resume: load prior repair state from build_state.json.
    prior = _load_build_state(workspace_id)
    repair_attempts = int(prior.get("repair_attempts", 0))
    repair_log: list[str] = list(prior.get("repair_log", []))

    # Already exhausted in a prior run: do NOT silently re-run. Re-emit.
    if (repair_attempts >= max_repairs
            and prior.get("status") == "repair_exhausted"):
        _emit_human_required(
            workspace_id, plan,
            {"output": prior.get("last_test_output", "")},
            repair_attempts, max_repairs,
        )
        return ImplementationResult(
            success=False,
            error=(f"Repair loop already exhausted ({repair_attempts}/"
                   f"{max_repairs}) — HUMAN_REQUIRED re-emitted"),
            repair_attempts=repair_attempts,
            repair_log=repair_log,
            executed_at=now(),
        )

    write_build_state(workspace_id, "implementing",
                      repair_attempts=repair_attempts, repair_log=repair_log)

    # Phase 2 — snapshot the workspace files BEFORE the agent runs so the
    # artefact-presence check (after the agent) can compute the delta. None
    # means working_dir is falsy / not a directory (test seams that bypass
    # the file system); in that case the artefact check is skipped.
    files_before = _list_workspace_files(working_dir)

    # 1) Initial implementation via the agent.
    agent_result = agent_fn(plan, working_dir=working_dir)
    repair_log.append(
        f"[attempt 0] agent success={agent_result.success}; "
        f"output={(agent_result.output or '')[:300]}"
    )
    if not agent_result.success:
        write_build_state(workspace_id, "agent_failed",
                          repair_attempts=repair_attempts, repair_log=repair_log)
        return ImplementationResult(
            success=False,
            output=agent_result.output,
            error=f"Agent failed: {agent_result.error}",
            repair_attempts=repair_attempts,
            repair_log=repair_log,
            executed_at=now(),
        )

    # Phase 2 — ARTEFACT PRESENCE CHECK. An agent that returned success=True
    # WITHOUT producing any new file in working_dir gave a non-answer (the
    # answer-156 case: "What would you like me to do?"). Without this check
    # the strengthened `_tests_passed` would fall through to the repair loop,
    # which would burn its full budget trying to repair tests that don't
    # exist. Skip when ``files_before`` is None (working_dir not set — test
    # seams) so existing repair-loop tests that don't touch the file system
    # keep working.
    if files_before is not None:
        files_after = _list_workspace_files(working_dir)
        new_files = files_after - files_before
        if not new_files:
            write_build_state(
                workspace_id, "agent_no_artefact",
                repair_attempts=repair_attempts, repair_log=repair_log,
                execution_success=False,
            )
            return ImplementationResult(
                success=False,
                output=agent_result.output,
                error=(
                    "Agent returned success but produced no artefact in "
                    f"{working_dir}. The spec was likely not an actionable "
                    "implementation plan — Phase 2 artefact-presence gate."
                ),
                repair_attempts=repair_attempts,
                repair_log=repair_log,
                executed_at=now(),
            )

    # 2) Initial test run.
    test_results = test_fn(working_dir)
    if _tests_passed(test_results):
        write_build_state(workspace_id, "executed",
                          repair_attempts=repair_attempts,
                          repair_log=repair_log, execution_success=True)
        return ImplementationResult(
            success=True,
            output=agent_result.output,
            test_results=test_results,
            repair_attempts=repair_attempts,
            repair_log=repair_log,
            executed_at=now(),
        )

    # 3) Repair loop: prompt the agent with the failed-test output, retest.
    while repair_attempts < max_repairs:
        repair_attempts += 1
        repair_prompt = _build_repair_prompt(
            plan, test_results, repair_attempts, max_repairs)
        repair_result = agent_fn(repair_prompt, working_dir=working_dir)
        repair_log.append(
            f"[repair {repair_attempts}] agent success={repair_result.success}; "
            f"output={(repair_result.output or '')[:300]}"
        )
        if not repair_result.success:
            # Agent could not produce a repair; try the next attempt if any.
            write_build_state(workspace_id, "repair_agent_failed",
                              repair_attempts=repair_attempts,
                              repair_log=repair_log)
            continue

        test_results = test_fn(working_dir)
        repair_log.append(
            f"[repair {repair_attempts}] tests failed={test_results.get('failed', '?')}"
        )
        if _tests_passed(test_results):
            write_build_state(workspace_id, "executed",
                              repair_attempts=repair_attempts,
                              repair_log=repair_log, execution_success=True)
            return ImplementationResult(
                success=True,
                output=repair_result.output,
                test_results=test_results,
                repair_attempts=repair_attempts,
                repair_log=repair_log,
                executed_at=now(),
            )
        # Repaired code still fails; persist and loop.
        write_build_state(workspace_id, "repair_attempt",
                          repair_attempts=repair_attempts,
                          repair_log=repair_log,
                          last_test_output=test_results.get("output", "")[:500])

    # 4) Budget exhausted: escalate to a human. No silent failure.
    _emit_human_required(workspace_id, plan, test_results,
                         repair_attempts, max_repairs)
    return ImplementationResult(
        success=False,
        output=agent_result.output,
        test_results=test_results,
        repair_attempts=repair_attempts,
        repair_log=repair_log,
        error=(f"Repair loop exhausted ({repair_attempts}/{max_repairs}) — "
               f"HUMAN_REQUIRED emitted to human_decision_manager"),
        executed_at=now(),
    )


# ---------------------------------------------------------------------------
# Direct implementation (synchronous, for non-Claude Code use)
# ---------------------------------------------------------------------------


def _direct_implementation(plan: str, working_dir: str | None = None) -> ImplementationResult:
    """Execute implementation plan directly via provided provider router.

    This is the fallback when Claude Code CLI is not available.
    The provider router generates the implementation code, then tests run.
    """
    from cerebras_client import generate as llm_generate

    # Use provider router to generate implementation
    instruction = (
        "You are an implementation engineer. Implement the following plan "
        "by producing actual code files. For each file, specify the filepath "
        "and complete file contents. Use markdown code blocks.\n\n"
        f"IMPLEMENTATION PLAN:\n{plan}\n\n"
        "Generate all code now. Be thorough."
    )

    try:
        code_output = llm_generate(instruction, module="implementation_executor")
        repair_log = [code_output[:500]]
    except Exception as e:
        return ImplementationResult(
            success=False,
            error=f"Provider generation failed: {e}",
            executed_at=datetime.now(timezone.utc).isoformat(),
        )

    # Run tests
    test_results = _run_tests(working_dir)

    success = test_results.get("failed", 1) == 0
    return ImplementationResult(
        success=success,
        output=code_output,
        test_results=test_results,
        repair_attempts=0,
        repair_log=repair_log,
        error="" if success else f"Tests failed: {test_results.get('failed', '?')} failures",
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_implementation(
    plan: str,
    workspace_id: str = "",
    working_dir: str | None = None,
    use_claude_code: bool = True,
    agent_fn: Callable[..., Any] | None = None,
    test_fn: Callable[..., dict] | None = None,
    max_repairs: int | None = None,
) -> dict:
    """Execute an implementation plan through the autonomous repair loop.

    Production path (``use_claude_code=True``):
        coding_agent_router.get_agent("opencode", workspace_id) →
        ``_execute_with_repair_loop`` → initial code generation →
        ``_run_tests`` → on failure, repair prompt → re-invoke agent →
        retest, up to ``max_repairs`` times. On exhaustion a HUMAN_REQUIRED
        decision is persisted via ``human_decision_manager.save_pending`` —
        no silent failure. Repair state is checkpointed to
        ``build_state.json`` after every attempt for crash recovery.

    Seams (for offline tests, no model/subprocess/browser):
        agent_fn: callable(plan, working_dir=None, timeout=None) -> AgentResult.
                   Replaces ``get_agent(...).run``. When provided,
                   ``use_claude_code=True`` is implied for routing.
        test_fn:  callable(working_dir=None) -> dict. Replaces ``_run_tests``.

    Args:
        plan: The implementation plan from synthesis.
        workspace_id: Associated workspace ID (for state + escalation).
        working_dir: Directory to execute in (defaults to CWD).
        use_claude_code: If True, dispatch via coding_agent_router -> OpenCodeAgent
                        (then the repair loop). If False, fall back to the
                        cerebras-based direct implementation (no repair loop).
        agent_fn: Optional injected agent seam (testing only).
        test_fn: Optional injected test runner seam (testing only).
        max_repairs: Repair budget; defaults to ``MAX_REPAIR_ATTEMPTS``.

    Returns:
        Dict with: success, output, test_results, repair_attempts, repair_log,
        error, executed_at.
    """
    max_repairs = MAX_REPAIR_ATTEMPTS if max_repairs is None else max_repairs
    print(f"[EXECUTOR] Starting implementation (workspace={workspace_id[:8] if workspace_id else 'none'})")
    print(f"[EXECUTOR] Plan length: {len(plan)} chars; max_repairs={max_repairs}")

    if use_claude_code:
        # Resolve the agent seam: an injected test seam wins; otherwise pull
        # the production OpenCodeAgent from the router.
        if agent_fn is None:
            from coding_agent_router import get_agent
            agent = get_agent("opencode", workspace_id)
            if agent is None:
                print("[EXECUTOR] no 'opencode' agent registered — "
                      "falling back to provider-based generation")
            else:
                agent_fn = agent.run
        runner = test_fn or _run_tests

        if agent_fn is not None:
            return _execute_with_repair_loop(
                plan, workspace_id, working_dir, agent_fn, runner, max_repairs,
            ).to_dict()

    # Direct implementation via provider router (cerebras fallback).
    result = _direct_implementation(plan, working_dir)
    return result.to_dict()


if __name__ == "__main__":
    # Smoke test
    test_plan = "Create a simple Python function that returns 'Hello Sol0mon'."
    result = execute_implementation(test_plan, use_claude_code=False)
    print(json.dumps(result, indent=2))