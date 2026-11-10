"""
test_build_answer_55_opencode_live_e2e.py — Phase D3 real end-to-end test.

Proves the live chain from the audit:

    Build answer 55
        -> Resurrection        (build_pipeline.resurrect_answer + answer_loader)
        -> Build Prompt        (build_prompt_generator.generate_build_prompt)
        -> Build Spec          (build_spec_engine.build_spec + store_implementation_spec)
        -> OpenCodeAgent       (real subprocess backend in coding_agents/opencode_agent.py)
        -> Repository modification  (opencode writes files into the build workspace)
        -> Test execution      (implementation_executor._run_tests on the workspace)
        -> Success

Live opencode is required; the test skips cleanly when the `opencode` binary
is not on PATH (so it stays green on CI without the agent installed). Uses the
real read-only answer_memory.db (answer 55 is present). The debate step is a
stub that yields a spec instructing opencode to create exactly one test file.
No model calls beyond opencode itself; no LangGraph; no Claude/Codex/Gemini CLI.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
from coding_agent_router import get_agent
from build_prompt_generator import EXECUTABLE_SPEC_SECTIONS

OPCODE_BIN = shutil.which("opencode")


def _spec_that_writes_a_passing_test():
    """An executable implementation specification that:
      - passes BOTH ``validate_spec_sections`` (legacy 7 audit-debate
        sections — the test stubs via ``debate_fn``, so the legacy audit-
        debate path's diagnostic is bypassed) AND
        ``validate_executable_spec_sections`` (6 executable-spec sections
        — the defence-in-depth gate at execute_build_spec dispatches only
        when these are present);
      - declares exactly one file in ``# Files to Create`` (tests/test_hello.py)
        so the post-execution QualityGate's "required files exist" check
        passes the moment OpenCode writes it;
      - instructs opencode to create one passing pytest test.

    After opencode writes tests/test_hello.py, implementation_executor.
    _run_tests invokes ``python3 -m pytest`` in the workspace dir and
    should see >0 passed; the QualityGate then confirms the build is
    complete."""
    return (
        # Legacy 7 sections (the audit-debate path's validate_spec_sections
        # check; kept so build_spec.debate_fn seam path returns
        # status="completed" on this stubbed synthesized_answer):
        "# Architecture\nA trivial one-file Python project for opencode "
        "end-to-end verification. No external dependencies.\n\n"
        "# Components\nA single pytest test file.\n\n"
        "# File Structure\ntests/test_hello.py\n\n"
        "# Dependencies\npytest (already installed).\n\n"
        "# Constraints\nMUST create exactly one file named tests/test_hello.py. "
        "MUST NOT create any other file. MUST NOT run the tests yourself. "
        "MUST NOT modify files outside the current directory.\n\n"
        "# Testing Requirements\ntests/test_hello.py must contain a pytest "
        "test named test_hello that asserts True == True and therefore "
        "passes.\n\n"
        # Executable-spec sections (the defence-in-depth gate):
        "# Implementation Objective\nA trivial one-file Python project for "
        "opencode end-to-end verification.\n\n"
        "# Files to Create\n- tests/test_hello.py  # the single contract "
        "test\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Create exactly one file at the path "
        "tests/test_hello.py in the current directory.\n"
        "2. tests/test_hello.py contains a pytest test named test_hello "
        "that asserts True == True and therefore passes.\n\n"
        "# Acceptance Criteria\npython3 -m pytest tests/test_hello.py "
        "reports 1 passed.\n\n"
        "# Output Contract\nMUST create exactly one file named "
        "tests/test_hello.py. MUST NOT create any other file. MUST NOT run "
        "the tests yourself. MUST NOT modify files outside the current "
        "directory. Stop after creating the file.\n"
    )


def _stub_debate(prompt, memory_ids=None):
    """Offline debate_fn stub: returns the spec above as the consensus
    synthesized_answer. No browser, no model call."""
    return {
        "status": "completed",
        "synthesized_answer": _spec_that_writes_a_passing_test(),
        "gpt_final": "g", "gemini_final": "m", "validated_disputes": [],
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [], "final_consensus_score": 90,
    }


def test_build_answer_55_opencode_live_e2e() -> None:
    if OPCODE_BIN is None:
        print("SKIP test_build_answer_55_opencode_live_e2e: "
              "opencode CLI not on PATH")
        return

    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)  # ponytail: module-global swap, isolated run

    try:
        # 1) OpenCodeAgent is the registered real backend (smoke-checked first
        #    so a router/adapter bug fails cleanly before the long pipeline).
        agent = get_agent("opencode", "preload")
        assert agent is not None and agent.name == "opencode"

        # 2) Build answer 55 -> resurrection -> build prompt -> build spec ->
        #    OpenCodeAgent -> repo modification -> tests -> success.
        result = bp.run_build_pipeline(
            "Build answer 55",
            debate_fn=_stub_debate,
            use_claude_code=True,
        )

        # Classification + resurrection + spec generation.
        assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
        assert result["answer_resurrected"] is True
        assert result["spec_status"] == "completed", result["spec_status"]
        wid = result["workspace_id"]
        ws_dir = Path(tmp_ws) / f"workspace_{wid}"

        # build_state.json shows the executor ran and persisted final state.
        import json
        state = json.loads((ws_dir / "build_state.json").read_text())
        assert state["status"] == "executed", (
            f"build_state final status={state.get('status')!r}, "
            "expected 'executed' (audit D3 + earlier audit fix #3)")
        assert state.get("execution_success") is True

        # implementation_plan.md is the canonical spec filename and is non-empty.
        plan_text = (ws_dir / "implementation_plan.md").read_text()
        assert plan_text
        for s in EXECUTABLE_SPEC_SECTIONS:
            assert s in plan_text.lower(), (
                f"implementation_plan.md missing executable-spec section: {s}")

        # 3) The OpenCodeAgent was actually invoked (not the old stub): the
        #    spec instructed it to create tests/test_hello.py in the workspace dir.
        created = ws_dir / "tests" / "test_hello.py"
        assert created.is_file(), (
            f"opencode did not write {created}; contents of ws_dir: "
            f"{[p.name for p in ws_dir.iterdir()]}")
        src = created.read_text()
        assert "def test_hello" in src, f"unexpected test file content: {src!r}"
        assert "True" in src or "true" in src

        # 4) Test execution by implementation_executor._run_tests on the
        #    workspace dir reports >0 passed and 0 failed.
        exec_result = result["execution_result"]
        assert exec_result["success"] is True, (
            f"execution_result.success False: {exec_result}")
        tr = exec_result["test_results"]
        assert tr.get("failed", 1) == 0, (
            f"expected 0 failed, got {tr.get('failed')}: {tr}")
        assert tr.get("passed", 0) > 0, (
            f"expected >0 passed, got {tr.get('passed')}: {tr}")

        # 5) Coding-agent router was reached + attached to the result.
        assert exec_result.get("coding_agent") is not None
        assert exec_result["coding_agent"].name == "opencode"
        assert exec_result["coding_agent"].workspace_id == wid

        print(f"PASS test_build_answer_55_opencode_live_e2e "
              f"(ws={wid}, passed={tr.get('passed')}, "
              f"file={created.relative_to(ws_dir)})")
    finally:
        # tmp dir is intentionally small; leave for OS cleanup.
        pass


if __name__ == "__main__":
    test_build_answer_55_opencode_live_e2e()
