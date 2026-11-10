"""
test_execution_mode_gate.py — Execution Mode Gate V1 (Phases 1-7).

Offline only. No model calls. No browser. No external APIs.

Mirrors the seam strategy of test_execution_decision.py: answer_memory points
at a tmp DB, build_workspace.WORKSPACE_ROOT at a tmp dir, build_pipeline's
debate_fn / executor_fn / mode_select_fn seams are injected. The
deterministic execution_decision engine and gates are exercised directly.

Verifies the 10 contracts (Phase 6):

  1. Recommendation accepted — Enter keeps the recommended mode; the
     pipeline records selected == recommended and override_used == False.
  2. Override works — a different menu choice overrides; override_used ==
     True and the gate enforces the selected (not recommended) depth.
  3. Research-only stops correctly — forced RESEARCH_ONLY stops after
     synthesis (status research_complete, no spec, no executor).
  4. Research-architecture stops correctly — forced RESEARCH_ARCHITECTURE
     stops at architecture_complete (no build specs, no executor).
  5. Full-build reaches build stage — forced FULL_BUILD reaches spec
     generation regardless of the recommended mode (the Debate #145 fix).
  6. Persistence survives save/load — recommended/selected/override_used
     round-trip through build_state.json.
  7. Resume preserves mode — a fresh _resolve_execution_decision re-applies
     the persisted selected_mode without re-prompting (no mode_select_fn).
  8. Existing execution_decision tests remain green — classify_execution_decision
     is unchanged and the gate is a pure wrapper (no behavior drift).
  9. No OpenCode invocation in non-build modes — RESEARCH_ONLY and
     RESEARCH_ARCHITECTURE never invoke the executor (and skip the spec
     debate too).
 10. Full-build mode reaches the implementation executor — FULL_BUILD invokes
     the executor seam exactly once.

Also: the gate module imports no LLM/browser/network client (purity parity with
execution_decision), so the build pipeline stays offline in every test.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import answer_memory
import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
import execution_decision as ed
import execution_mode_gate as emg
from execution_decision import (
    RESEARCH_ONLY as ED_RESEARCH_ONLY,
    ARCHITECTURE_ONLY as ED_ARCHITECTURE_ONLY,
    IMPLEMENTATION_REQUIRED as ED_IMPLEMENTATION_REQUIRED,
    classify_execution_decision, attach_to_verification_result,
    from_verification_result,
)
from execution_mode_gate import (
    RESEARCH_ONLY, RESEARCH_ARCHITECTURE, FULL_BUILD,
    decision_to_mode, recommend_mode_from_prompt, resolve_mode,
    prompt_execution_mode, fixed_selection_fn, mode_to_decision,
    mode_allows_build, mode_allows_architecture, apply_mode_override,
    decision_from_mode_selection, save_mode_selection, load_mode_selection,
    KEY_RECOMMENDED_MODE, KEY_SELECTED_MODE, KEY_OVERRIDE_USED,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _with_tmp_db():
    """Point answer_memory at a fresh temp DB. Returns path for cleanup."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    if os.path.exists(tmp.name):
        os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _set_workspace_root():
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    return Path(tmp)


def _good_spec_text():
    """Hybrid spec: contains the legacy 7 audit-debate sections (so
    validate_spec_sections → [] on the legacy path) AND the 6 executable-
    spec sections (so validate_executable_spec_sections → [] on the
    defence-in-depth gate at execute_build_spec). After the artefact-
    selection fix the production default emits the executable-spec shape;
    test seams that pin the legacy audit-debate path keep using a hybrid
    shape so BOTH validators pass — the spec reaches the executor."""
    return (
        # Legacy 7 sections:
        "# Architecture\nLayered.\n\n"
        "# Components\nAPI, worker, DB.\n\n"
        "# File Structure\nsrc/api src/worker\n\n"
        "# Dependencies\nfastapi\n\n"
        "# Constraints\nMUST use PostgreSQL.\n\n"
        "# Testing Requirements\npytest\n\n"
        # Executable-spec sections:
        "# Implementation Objective\nBuild the layered service per the "
        "verified decisions.\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement main.\n\n"
        "# Acceptance Criteria\ntests green\n\n"
        "# Output Contract\nStop after tests pass.\n"
    )


def _fake_debate_factory():
    """Injected build-spec debate seam — returns the canonical spec text."""
    def _debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
        }
    return _debate


def _counting_executor(schema="success"):
    """Executor seam that records invocations; the spec it received is kept.
    After the artefact-selection fix the executor must ALSO create the files
    declared in the spec's ``# Files to Create`` so the post-execution
    QualityGate's "required files exist" check passes (a real executor
    writes them before returning). The mock writes the declared files to
    the single workspace_ directory under WORKSPACE_ROOT."""
    state = {"count": 0, "specs": []}

    def _fn(spec_text):
        state["count"] += 1
        state["specs"].append(spec_text)
        # Write the declared files so the QualityGate passes. Resolves the
        # single workspace_<id>/ directory under WORKSPACE_ROOT.
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
            "output": "[MOCK] executed",
            "test_results": {"passed": 5, "failed": 0, "exit_code": 0},
            "repair_attempts": 0,
            "error": "",
        }
    return _fn, state


def _counting_debate():
    """Build-spec debate seam that counts how many times spec generation ran."""
    state = {"count": 0}

    def _debate(prompt, memory_ids=None):
        state["count"] += 1
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
        }
    return _debate, state


def _seed_answer(prompt, execution_decision_dict, body="seed body"):
    """Save a debate row carrying an execution_decision and return its id.
    The decision rides inside verification_result (the immutable round-trip
    channel execution_decision uses)."""
    vr = {"verification_score": 100, "resolved_claims": [],
          "unresolved_claims": [], "equivalent_claims": [],
          "truncated_count": 0}
    attach_to_verification_result(vr, execution_decision_dict)
    return answer_memory.memory.save_debate(
        question=prompt, gpt_final="gpt", gemini_final="gem",
        synthesized_answer=body, consensus_score=90,
        conflict_severity="LOW", conflict_points=[],
        validated_disputes=[], verification_result=vr,
    )


def _read_build_state(ws_root, workspace_id):
    return json.loads(
        (ws_root / f"workspace_{workspace_id}" / "build_state.json").read_text()
    )


# --------------------------------------------------------------------------- #
# 1. Recommendation accepted                                                   #
# --------------------------------------------------------------------------- #

def test_recommendation_accepted():
    """Enter accepts the recommendation: selected == recommended and
    override_used is False, both at the selection helper and through the
    pipeline (persisted to build_state.json)."""
    assert resolve_mode(FULL_BUILD, "") == (FULL_BUILD, False)
    assert resolve_mode(RESEARCH_ARCHITECTURE, None) == (RESEARCH_ARCHITECTURE, False)
    assert resolve_mode(RESEARCH_ONLY, "   ") == (RESEARCH_ONLY, False)
    # injected input_fn (Enter) accepts; no deadlock in a non-interactive shell.
    assert prompt_execution_mode(FULL_BUILD, input_fn=lambda _p: "") == (FULL_BUILD, False)

    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        # IMPLEMENTATION_REQUIRED answer -> recommended FULL_BUILD; accept it.
        aid = _seed_answer(
            "Implement the user authentication module",
            classify_execution_decision(question="Implement the user authentication module"),
        )
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=_fake_debate_factory(),
            executor_fn=_counting_executor()[0],
            mode_select_fn=lambda rec: (rec, False),  # accept recommendation
        )
        assert result["recommended_mode"] == FULL_BUILD
        assert result["selected_mode"] == FULL_BUILD
        assert result["override_used"] is False
        # The accepted recommendation proceeds to build (no block).
        assert result.get("execution_blocked") is not True
        assert result["spec_status"] == "completed"

        st = _read_build_state(ws_root, result["workspace_id"])
        assert st[KEY_RECOMMENDED_MODE] == FULL_BUILD
        assert st[KEY_SELECTED_MODE] == FULL_BUILD
        assert st[KEY_OVERRIDE_USED] is False
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 2. Override works                                                            #
# --------------------------------------------------------------------------- #

def test_override_works():
    """A different menu choice overrides the recommendation: selected !=
    recommended, override_used is True, and the gate enforces the selected
    depth (the recommended decision is replaced at the gate)."""
    assert resolve_mode(FULL_BUILD, "1") == (RESEARCH_ONLY, True)
    assert resolve_mode(RESEARCH_ONLY, "3") == (FULL_BUILD, True)
    assert resolve_mode(RESEARCH_ARCHITECTURE, "1") == (RESEARCH_ONLY, True)
    # invalid choice falls back to the recommendation (override_used False).
    assert resolve_mode(FULL_BUILD, "9") == (FULL_BUILD, False)
    assert resolve_mode(FULL_BUILD, "abc") == (FULL_BUILD, False)

    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        # IMPLEMENTATION_REQUIRED answer (recommended FULL_BUILD), override to
        # RESEARCH_ONLY: the gate enforces RESEARCH_ONLY even though the
        # resurrected decision was IMPLEMENTATION_REQUIRED.
        aid = _seed_answer(
            "Implement the user authentication module",
            classify_execution_decision(question="Implement the user authentication module"),
        )
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=_fake_debate_factory(),
            executor_fn=_counting_executor()[0],
            mode_select_fn=fixed_selection_fn(RESEARCH_ONLY, True),
        )
        assert result["recommended_mode"] == FULL_BUILD
        assert result["selected_mode"] == RESEARCH_ONLY
        assert result["override_used"] is True
        # The override replaced the decision: the auto IMPLEMENTATION_REQUIRED
        # is overridden to RESEARCH_ONLY.
        assert result["execution_decision"] == ED_RESEARCH_ONLY
        assert result["execution_blocked"] is True
        assert result["status"] == "research_complete"

        assert result["workspace_id"] is None
        assert not list(ws_root.glob("workspace_*"))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 3. Research-only stops correctly                                             #
# --------------------------------------------------------------------------- #

def test_research_only_stops_correctly():
    """Forced RESEARCH_ONLY stops after synthesis: status research_complete,
    no spec debate, no spec file, no executor invocation (OpenCode)."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    debate, d_state = _counting_debate()
    executor, e_state = _counting_executor()
    try:
        # Recommended FULL_BUILD (implementation answer) -> force RESEARCH_ONLY.
        aid = _seed_answer(
            "Implement the user authentication module",
            classify_execution_decision(question="Implement the user authentication module"),
        )
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=debate, executor_fn=executor,
            mode_select_fn=fixed_selection_fn(RESEARCH_ONLY, True),
        )
        assert result["selected_mode"] == RESEARCH_ONLY
        assert result["execution_blocked"] is True
        assert result["status"] == "research_complete"
        assert result["spec_status"] == "blocked"
        assert result["execution_result"]["blocked"] is True
        assert result["execution_result"]["blocked_reason"] == ED_RESEARCH_ONLY

        # No spec debate ran (Step 3.5 returned before Step 4).
        assert d_state["count"] == 0, "spec debate must NOT run for RESEARCH_ONLY"
        # No executor reached.
        assert e_state["count"] == 0, "executor must NOT run for RESEARCH_ONLY"
        # No implementation workspace is created for forced RESEARCH_ONLY.
        assert result["workspace_id"] is None
        assert not list(ws_root.glob("workspace_*"))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 4. Research-architecture stops correctly                                     #
# --------------------------------------------------------------------------- #

def test_research_architecture_stops_correctly():
    """Forced RESEARCH_ARCHITECTURE stops at architecture_complete: no build
    specs, no OpenCode. Architecture itself rides in the synthesized answer
    (deterministic decision_renderer handles that; the gate just halts before
    build-spec generation)."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    debate, d_state = _counting_debate()
    executor, e_state = _counting_executor()
    try:
        # Recommended FULL_BUILD (implementation answer) -> force RESEARCH +
        # Architecture. The gate maps it to ARCHITECTURE_ONLY and stops.
        aid = _seed_answer(
            "Implement the user authentication module",
            classify_execution_decision(question="Implement the user authentication module"),
        )
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=debate, executor_fn=executor,
            mode_select_fn=fixed_selection_fn(RESEARCH_ARCHITECTURE, True),
        )
        assert result["selected_mode"] == RESEARCH_ARCHITECTURE
        assert result["execution_blocked"] is True
        assert result["status"] == "architecture_complete"
        assert result["execution_decision"] == ED_ARCHITECTURE_ONLY

        assert d_state["count"] == 0, "spec debate must NOT run for RESEARCH_ARCHITECTURE"
        assert e_state["count"] == 0, "executor must NOT run for RESEARCH_ARCHITECTURE"
        assert result["workspace_id"] is None
        assert not list(ws_root.glob("workspace_*"))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 5. Full-build reaches build stage                                            #
# --------------------------------------------------------------------------- #

def test_full_build_reaches_build_stage():
    """Forced FULL_BUILD reaches the build stage even when the recommended
    mode was RESEARCH_ONLY — the Debate #145 failure mode is fixed: a debate
    that auto-classified RESEARCH_ONLY no longer prevents the build."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        rd = classify_execution_decision(
            question="Survey the state of the art in vector databases",
        )
        assert rd["execution_decision"] == ED_RESEARCH_ONLY  # recommended RESEARCH_ONLY
        aid = _seed_answer(
            "Survey the state of the art in vector databases", rd,
        )
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=_fake_debate_factory(),
            executor_fn=_counting_executor()[0],
            mode_select_fn=fixed_selection_fn(FULL_BUILD, True),
        )
        assert result["recommended_mode"] == RESEARCH_ONLY
        assert result["selected_mode"] == FULL_BUILD
        assert result["override_used"] is True
        # The auto RESEARCH_ONLY was overridden to IMPLEMENTATION_REQUIRED.
        assert result["execution_decision"] == ED_IMPLEMENTATION_REQUIRED
        assert result.get("execution_blocked") is not True
        assert result["spec_status"] == "completed"

        wid = result["workspace_id"]
        # Spec was generated and stored.
        spec = bse.load_implementation_spec(wid)
        assert spec
        for section in ("# architecture", "# components", "# testing requirements"):
            assert section in spec.lower()

        st = _read_build_state(ws_root, wid)
        assert st["execution_decision"] == ED_IMPLEMENTATION_REQUIRED
        assert st["execution_blocked"] is False
        assert st[KEY_SELECTED_MODE] == FULL_BUILD
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 6. Persistence survives save/load                                            #
# --------------------------------------------------------------------------- #

def test_persistence_survives_save_load():
    """recommended_mode / selected_mode / override_used round-trip through
    build_state.json via save_mode_selection / load_mode_selection, and the
    exact Phase-4 example object is reproduced."""
    ws_root = _set_workspace_root()
    wid = bw.create_workspace(source_question="persistence probe")

    # The Phase-4 example: recommended FULL_BUILD, selected RESEARCH_ONLY,
    # override_used True.
    save_mode_selection(wid, FULL_BUILD, RESEARCH_ONLY, True)
    loaded = load_mode_selection(wid)
    assert loaded == {
        KEY_RECOMMENDED_MODE: FULL_BUILD,
        KEY_SELECTED_MODE: RESEARCH_ONLY,
        KEY_OVERRIDE_USED: True,
    }
    # build_state.json on disk carries the persisted trio.
    st = _read_build_state(ws_root, wid)
    assert st[KEY_RECOMMENDED_MODE] == FULL_BUILD
    assert st[KEY_SELECTED_MODE] == RESEARCH_ONLY
    assert st[KEY_OVERRIDE_USED] is True

    # Legacy / fresh workspace with no selection returns None (no spurious mode).
    wid2 = bw.create_workspace(source_question="no mode")
    assert load_mode_selection(wid2) is None

    # Overwriting replaces the prior selection.
    save_mode_selection(wid, RESEARCH_ARCHITECTURE, FULL_BUILD, True)
    loaded2 = load_mode_selection(wid)
    assert loaded2[KEY_SELECTED_MODE] == FULL_BUILD
    assert loaded2[KEY_RECOMMENDED_MODE] == RESEARCH_ARCHITECTURE


# --------------------------------------------------------------------------- #
# 7. Resume preserves mode                                                     #
# --------------------------------------------------------------------------- #

def test_resume_preserves_mode():
    """After a run with an override, the persisted selected_mode is honored by
    a fresh _resolve_execution_decision WITHOUT mode_select_fn (resume), and
    load_mode_selection returns the persisted trio."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        # RESEARCH_ONLY answer -> override to FULL_BUILD (Debate #145 fix).
        rd = classify_execution_decision(
            question="Survey the state of the art in vector databases",
        )
        aid = _seed_answer(
            "Survey the state of the art in vector databases", rd,
        )
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=_fake_debate_factory(),
            executor_fn=_counting_executor()[0],
            mode_select_fn=fixed_selection_fn(FULL_BUILD, True),
        )
        wid = result["workspace_id"]

        # Selection persisted (Phase 4).
        sel = load_mode_selection(wid)
        assert sel is not None
        assert sel[KEY_SELECTED_MODE] == FULL_BUILD
        assert sel[KEY_RECOMMENDED_MODE] == RESEARCH_ONLY
        assert sel[KEY_OVERRIDE_USED] is True

        # Resume: re-resolve with NO mode_select_fn. The persisted mode is the
        # authority — it returns IMPLEMENTATION_REQUIRED (FULL_BUILD's mapping)
        # and never re-prompts or recomputes the auto RESEARCH_ONLY.
        resumed = bp._resolve_execution_decision(wid, None, f"Build answer {aid}")
        assert resumed["execution_decision"] == ED_IMPLEMENTATION_REQUIRED
        assert resumed["blocked"] is False
        assert resumed[KEY_SELECTED_MODE] == FULL_BUILD
        assert resumed[ed.KEY_SOURCE] == "execution_mode_gate_persisted"

        # build_state.json still carries the trio after a separate state write
        # (merge semantics must not wipe the mode keys).
        bw.write_build_state(wid, "plan_running", unrelated="x")
        sel2 = load_mode_selection(wid)
        assert sel2[KEY_SELECTED_MODE] == FULL_BUILD
        assert _read_build_state(ws_root, wid)["unrelated"] == "x"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 8. Existing execution_decision tests remain green (no behavior drift)      #
# --------------------------------------------------------------------------- #

def test_execution_decision_engine_unchanged():
    """The gate is a pure wrapper over classify_execution_decision; the V1/V2
    deterministic classification is byte-for-byte unchanged. (The full
    regression is run separately in Phase 7; this pins the no-drift contract
    in-band.)"""
    prompts = [
        ("Compare PostgreSQL vs MongoDB for a multi-tenant SaaS", ED_RESEARCH_ONLY),
        ("Design a planet-scale consensus platform architecture", ED_ARCHITECTURE_ONLY),
        ("Implement the user authentication module", ED_IMPLEMENTATION_REQUIRED),
    ]
    for prompt, expected in prompts:
        out = classify_execution_decision(question=prompt)
        assert out["execution_decision"] == expected, (
            f"{prompt!r} expected {expected}, got {out['execution_decision']} — "
            "execution_decision classification drifted."
        )
        # Determinism: identical across calls.
        assert classify_execution_decision(question=prompt) == out

    # The mapping is a pure wrapper: same input -> same recommended mode.
    assert decision_to_mode(ED_RESEARCH_ONLY) == RESEARCH_ONLY
    assert decision_to_mode(ED_ARCHITECTURE_ONLY) == RESEARCH_ARCHITECTURE
    assert decision_to_mode(ED_IMPLEMENTATION_REQUIRED) == FULL_BUILD
    for prompt, expected_ed in prompts:
        decision = classify_execution_decision(question=prompt)
        assert decision_to_mode(decision) == {
            ED_RESEARCH_ONLY: RESEARCH_ONLY,
            ED_ARCHITECTURE_ONLY: RESEARCH_ARCHITECTURE,
            ED_IMPLEMENTATION_REQUIRED: FULL_BUILD,
        }[expected_ed]

    # recommend_mode_from_prompt reuses the engine verbatim.
    assert recommend_mode_from_prompt(prompts[0][0]) == RESEARCH_ONLY
    assert recommend_mode_from_prompt(prompts[1][0]) == RESEARCH_ARCHITECTURE
    assert recommend_mode_from_prompt(prompts[2][0]) == FULL_BUILD

    # Round-trip persistence through verification_result is unchanged.
    vr = {"resolved_claims": []}
    d = classify_execution_decision(question="Design a system architecture")
    attach_to_verification_result(vr, d)
    assert from_verification_result(vr) == {
        "execution_decision": d["execution_decision"],
        "execution_reason": d["execution_reason"],
        "execution_confidence": d["execution_confidence"],
        "execution_decision_source": d["execution_decision_source"],
    }


# --------------------------------------------------------------------------- #
# 9. No OpenCode invocation in non-build modes                                 #
# --------------------------------------------------------------------------- #

def test_no_opencode_invocation_in_non_build_modes():
    """RESEARCH_ONLY and RESEARCH_ARCHITECTURE never invoke the executor seam,
    and skip the spec debate entirely. (A bomb executor would raise if ever
    reached; the counter gives an explicit count to assert against.)"""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        aid = _seed_answer(
            "Implement the user authentication module",
            classify_execution_decision(question="Implement the user authentication module"),
        )
        for mode in (RESEARCH_ONLY, RESEARCH_ARCHITECTURE):
            debate, d_state = _counting_debate()
            executor, e_state = _counting_executor()
            result = bp.run_build_pipeline(
                f"Build answer {aid}",
                debate_fn=debate, executor_fn=executor,
                mode_select_fn=fixed_selection_fn(mode, True),
            )
            assert result["selected_mode"] == mode
            assert result["execution_blocked"] is True, (
                f"{mode} must block at Step 3.5 (got status={result.get('status')})"
            )
            assert result["spec_status"] == "blocked"
            assert d_state["count"] == 0, (
                f"spec debate must NOT run for {mode}")
            assert e_state["count"] == 0, (
                f"executor (OpenCode) must NOT be invoked for {mode}")
            assert mode_allows_build(mode) is False
            if mode == RESEARCH_ONLY:
                assert result["status"] == "research_complete"
            else:
                assert result["status"] == "architecture_complete"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 10. Full-build mode reaches the implementation executor                      #
# --------------------------------------------------------------------------- #

def test_full_build_reaches_implementation_executor():
    """FULL_BUILD reaches the implementation executor seam exactly once (and
    produces a completed spec). The forced build happens even when the debate
    auto-classified RESEARCH_ONLY."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        rd = classify_execution_decision(
            question="Survey the state of the art in vector databases",
        )
        assert rd["execution_decision"] == ED_RESEARCH_ONLY
        aid = _seed_answer(
            "Survey the state of the art in vector databases", rd,
        )
        executor, e_state = _counting_executor()
        result = bp.run_build_pipeline(
            f"Build answer {aid}",
            debate_fn=_fake_debate_factory(),
            executor_fn=executor,
            mode_select_fn=fixed_selection_fn(FULL_BUILD, True),
        )
        assert result["selected_mode"] == FULL_BUILD
        assert result.get("execution_blocked") is not True
        assert result["spec_status"] == "completed"
        assert mode_allows_build(FULL_BUILD) is True
        # The implementation executor (OpenCode seam) was reached exactly once.
        assert e_state["count"] == 1, (
            f"executor must run exactly once for FULL_BUILD (got {e_state['count']})"
        )
        assert e_state["specs"][0]  # received a non-empty spec
        assert result["execution_result"]["success"] is True
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# Enforcement + purity invariants (Phase-2/Phase-5 helpers + no model imports) #
# --------------------------------------------------------------------------- #

def test_enforcement_mapping_and_purity():
    """Phase-5 enforcement helpers, the override-stamping contract, and the
    gate module's import purity (no LLM/browser/network client pulled in,
    matching execution_decision's own guarantee)."""
    assert mode_to_decision(RESEARCH_ONLY) == ED_RESEARCH_ONLY
    assert mode_to_decision(RESEARCH_ARCHITECTURE) == ED_ARCHITECTURE_ONLY
    assert mode_to_decision(FULL_BUILD) == ED_IMPLEMENTATION_REQUIRED
    assert mode_to_decision(None) is None
    assert mode_to_decision("UNKNOWN") is None
    assert mode_allows_build(FULL_BUILD) and not mode_allows_build(RESEARCH_ONLY)
    assert not mode_allows_architecture(RESEARCH_ONLY)
    assert mode_allows_architecture(RESEARCH_ARCHITECTURE)
    assert mode_allows_architecture(FULL_BUILD)

    # apply_mode_override re-stamps when the depth changes.
    auto = {
        ed.KEY_DECISION: ED_RESEARCH_ONLY,
        ed.KEY_REASON: "auto",
        ed.KEY_CONFIDENCE: 80,
        ed.KEY_SOURCE: ed.SOURCE_RESEARCH_ANALYSIS,
        "blocked": True, "block_status": "research_complete",
        "source": "persisted",
    }
    over = apply_mode_override(auto, FULL_BUILD, RESEARCH_ONLY, True)
    assert over[ed.KEY_DECISION] == ED_IMPLEMENTATION_REQUIRED
    assert over["blocked"] is False
    assert over["block_status"] == ""
    assert over[ed.KEY_SOURCE] == "execution_mode_gate"
    assert over[KEY_SELECTED_MODE] == FULL_BUILD
    assert over[KEY_OVERRIDE_USED] is True
    # accepting the recommendation leaves the depth (and source) untouched.
    same = apply_mode_override(auto, RESEARCH_ONLY, RESEARCH_ONLY, False)
    assert same[ed.KEY_DECISION] == ED_RESEARCH_ONLY
    assert same[ed.KEY_SOURCE] == ed.SOURCE_RESEARCH_ANALYSIS
    assert same[KEY_SELECTED_MODE] == RESEARCH_ONLY

    # decision_from_mode_selection — resume authority.
    persisted = decision_from_mode_selection(
        {KEY_RECOMMENDED_MODE: RESEARCH_ONLY, KEY_SELECTED_MODE: FULL_BUILD,
         KEY_OVERRIDE_USED: True})
    assert persisted[ed.KEY_DECISION] == ED_IMPLEMENTATION_REQUIRED
    assert persisted[KEY_SELECTED_MODE] == FULL_BUILD
    assert persisted[ed.KEY_SOURCE] == "execution_mode_gate_persisted"
    assert decision_from_mode_selection(None) is None

    # Purity: importing the gate pulls no forbidden provider/browser/network
    # module. (execution_decision itself guarantees this; the gate only adds
    # stdlib + execution_decision at import time, persistence is lazy.)
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    forbidden = (
        "{'cerebras_client','ollama_client','openrouter_client','grok_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium','requests',"
        "'httpx','urllib3'}"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import execution_mode_gate; "
         f"forbidden = {forbidden}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: {probe.stdout!r} {probe.stderr!r}")
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "execution_mode_gate transitively imports forbidden modules")


if __name__ == "__main__":
    test_recommendation_accepted()
    test_override_works()
    test_research_only_stops_correctly()
    test_research_architecture_stops_correctly()
    test_full_build_reaches_build_stage()
    test_persistence_survives_save_load()
    test_resume_preserves_mode()
    test_execution_decision_engine_unchanged()
    test_no_opencode_invocation_in_non_build_modes()
    test_full_build_reaches_implementation_executor()
    test_enforcement_mapping_and_purity()
    print("test_execution_mode_gate: ALL PASSED")
