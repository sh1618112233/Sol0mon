"""
test_build_answer_55_e2e.py - End-to-end build pipeline audit test.

Proves the full chain the audit traced end-to-end:

    "Build answer 55"
        -> Resurrection      (build_pipeline.resurrect_answer + answer_loader)
        -> Build Prompt      (build_prompt_generator.generate_build_prompt)
        -> Build Spec        (build_spec_engine.build_spec + store_implementation_spec)
        -> Coding Agent Router (coding_agent_router.get_agent)
        -> Mock Executor     (injected via executor_fn — no model call)
        -> Success

Uses the real (read-only) answer_memory.db, which already carries answer 55.
The debate step and the executor are injected stubs, so the test runs offline —
no new model calls. Workspace writes are isolated to a tmp dir.

Fixes exercised:
    #1 implementation_plan.md / implementation_spec.md unification
    #2 executor gate removed — production execution reachable
    #3 build_state.json persisted through pipeline stages
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
from build_prompt_generator import EXECUTABLE_SPEC_SECTIONS, SPEC_SECTIONS
from coding_agent_router import available_agents


def _good_spec_text():
    """A spec that satisfies BOTH validators: the legacy 7-section shape
    (so build_spec's legacy audit-debate path reports ``missing=[]`` and
    ``status="completed"``) AND the executable-spec shape (so the
    defence-in-depth gate at execute_build_spec dispatches the executor).
    References the six agreed architectural points so the build_spec
    extractor honours the verified decisions from answer 55."""
    return (
        # Legacy 7 sections (validate_spec_sections audit-debate path):
        "# Architecture\nHybrid compute-leased: Cloudflare Workers edge + "
        "server-side Docker workers. BYOK. Server-side SQLite authoritative; "
        "PostgreSQL migration path. Reject IndexedDB, WebContainers, CRDTs, "
        "FastAPI, the Single VM monolith.\n\n"
        "# Components\ncontrol plane; execution workers; store; coordination\n\n"
        "# File Structure\nsrc/control src/worker src/store src/coord\n\n"
        "# Dependencies\nsqlite; cloudflare workers; docker; byok provider router\n\n"
        "# Constraints\nMUST honor byok; MUST honor cloudflare workers; "
        "MUST honor docker; MUST honor server-side authoritative storage; "
        "MUST honor sqlite; MUST honor postgresql migration.\n\n"
        "# Testing Requirements\npytest; contract tests per verified decision\n\n"
        # Executable-spec sections (validate_executable_spec_sections gate):
        "# Implementation Objective\nImplement the BYOK crypto trading "
        "bot per the six verified decisions above.\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement the trading bot.\n\n"
        "# Acceptance Criteria\nAll tests green; agreement on six "
        "architectural points.\n\n"
        "# Output Contract\nStop after tests pass; do not commit.\n"
    )


def test_build_answer_55_e2e_resurrect_to_executor_success() -> None:
    """E2E: Build answer 55 -> resurrect -> build prompt -> build spec ->
    coding agent router -> mock executor -> success."""
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)  # ponytail: module-global swap, isolated run

    captured = {"prompt": None, "spec_to_executor": None}

    def _fake_debate(prompt, memory_ids=None):
        captured["prompt"] = prompt
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m", "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [], "final_consensus_score": 90,
        }

    def _mock_executor(spec_text):
        captured["spec_to_executor"] = spec_text
        assert spec_text, "executor received empty spec"
        assert "implementation objective" in spec_text.lower()
        # Simulate OpenCode creating the files declared in # Files to Create
        # so the post-execution QualityGate's "required files exist" check
        # passes (a real executor writes the files before returning).
        # The workspace_id is not in scope yet at this point in the test
        # (it is assigned AFTER run_build_pipeline returns); resolve the
        # single workspace dir under WORKSPACE_ROOT.
        ws_dirs = list(Path(tmp_ws).glob("workspace_*"))
        assert ws_dirs, "no workspace dir created before executor invocation"
        ws_dir = ws_dirs[0]
        (ws_dir / "src" / "app").mkdir(parents=True, exist_ok=True)
        (ws_dir / "src" / "app" / "main.py").write_text("VALUE = 42\n")
        (ws_dir / "tests").mkdir(parents=True, exist_ok=True)
        (ws_dir / "tests" / "test_app.py").write_text(
            "from src.app.main import VALUE\n"
            "def test_value():\n    assert VALUE == 42\n")
        return {
            "success": True,
            "output": f"[MOCK EXECUTION] Implemented {len(spec_text)} chars of spec.",
            "test_results": {"passed": 6, "failed": 0, "exit_code": 0},
            "repair_attempts": 0,
            "error": "",
        }

    try:
        result = bp.run_build_pipeline(
            "Build answer 55",
            debate_fn=_fake_debate,
            executor_fn=_mock_executor,
        )

        # ---- Stage 1: classification -> IMPLEMENTATION ----
        assert result["classification"]["workflow_type"] == "IMPLEMENTATION"

        # ---- Stage 2: Resurrection ----
        assert result["answer_resurrected"] is True, "answer 55 must resurrect"
        wid = result["workspace_id"]
        assert wid
        ws_dir = Path(tmp_ws) / f"workspace_{wid}"

        # Authoritative decision files exist.
        assert (ws_dir / "answer.json").is_file()
        assert (ws_dir / "decisions.json").is_file()
        decisions = json.loads((ws_dir / "decisions.json").read_text())
        # Answer 55 has 7 verified decisions in its verification_result.
        assert len(decisions["verified_decisions"]) == 7, (
            f"expected 7 verified decisions, got "
            f"{len(decisions['verified_decisions'])}")

        # ---- Audit fix #3: build_state.json persisted across stages ----
        # After execution the state must reflect the 'executed' stage with
        # execution_success=True; earlier stages were merged into the same file.
        state = json.loads((ws_dir / "build_state.json").read_text())
        assert state["status"] == "executed", (
            f"build_state final status={state.get('status')!r}, "
            "expected 'executed' (audit fix #3)")
        assert state["workspace_id"] == wid
        assert state.get("execution_success") is True
        # Earlier-stage fields survive the merge (audit fix #3 preserves history):
        assert state.get("source_answer_id") == "55"
        assert state.get("spec_status") == "completed"

        # ---- Stage 3: Build Prompt (build_prompt_generator ran) ----
        assert captured["prompt"], "debate_fn never received a build prompt"
        prompt = captured["prompt"]
        assert "Build Prompt: Implementation Specification" in prompt
        # All 7 SPEC_SECTIONS are listed in the prompt's output contract.
        for s in SPEC_SECTIONS:
            assert s in prompt.lower(), f"build prompt missing section: {s}"
        # Constraints block is populated from decisions.json.verified_decisions:
        assert "## Constraints (immutable)" in prompt
        assert "(none)" not in prompt.lower().split(
            "## constraints (immutable)")[1].split("##")[0]

        # ---- Stage 4: Build Spec produced + stored ----
        assert result["spec_status"] == "completed", result["spec_status"]
        assert result["spec_missing_sections"] == []

        # Audit fix #1: implementation_plan.md is the canonical plan file and is
        # NON-EMPTY (previous bug: the engine wrote implementation_spec.md while
        # the manifest declared implementation_plan.md, so the latter stayed "").
        plan_path = ws_dir / "implementation_plan.md"
        assert plan_path.is_file(), "implementation_plan.md missing from workspace"
        plan_text = plan_path.read_text()
        assert plan_text, (
            "implementation_plan.md is empty — canonical filename was never "
            "populated (audit fix #1)")
        for s in SPEC_SECTIONS:
            assert s in plan_text.lower(), (
                f"implementation_plan.md missing legacy section: {s}")
        # The executable-spec sections (the defence-in-depth gate's contract)
        # are present too — the spec carries both sets after the artefact-
        # selection fix (the production default now emits executable-spec
        # sections; tests that pin the legacy audit-debate path's prompt
        # check SPEC_SECTIONS, both checks coexist).
        for s in EXECUTABLE_SPEC_SECTIONS:
            assert s in plan_text.lower(), (
                f"implementation_plan.md missing executable-spec section: {s}")
        # The engine's load API reads the same canonical file (round-trip).
        assert bse.load_implementation_spec(wid) == plan_text
        # No stray implementation_spec.md ghost file left from the old bug.
        assert not (ws_dir / "implementation_spec.md").is_file(), (
            "stale implementation_spec.md present after filename unification")

        # ---- Stage 5: Coding Agent Router reached ----
        exec_result = result["execution_result"]
        assert exec_result.get("coding_agent") is not None, (
            "coding_agent_router was not reached (audit fix #2 — executor gate)")
        agent = exec_result["coding_agent"]
        assert agent.name == "opencode"
        assert agent.workspace_id == wid
        assert "opencode" in available_agents()

        # ---- Stage 6: Mock Executor invoked with the spec text + success ----
        assert captured["spec_to_executor"] == plan_text, (
            "mock executor did not receive the stored implementation plan")
        assert exec_result["success"] is True
        assert exec_result["test_results"]["passed"] == 6
        assert exec_result["test_results"]["failed"] == 0
        assert "[MOCK EXECUTION]" in exec_result["output"]
    finally:
        pass  # tmp dir left for OS cleanup; WORKSPACE_ROOT is per-process


if __name__ == "__main__":
    test_build_answer_55_e2e_resurrect_to_executor_success()
    print("test_build_answer_55_e2e: ALL PASSED")
