"""
test_execution_decision.py - Debate-Governed Execution Decisions (Phase 1-6).

Offline only. No model calls. No browser. No external APIs.

Verifies the 10 contracts:

  1. research prompts become RESEARCH_ONLY.
  2. architecture prompts become ARCHITECTURE_ONLY.
  3. implementation prompts become IMPLEMENTATION_REQUIRED.
  4. execution decision persisted (lives inside verification_result +
     answer_memory JSON-TEXT column round-trip + workspace build_state.json).
  5. resurrection retains the decision (decisions.json/answer.json carry it
     back through answer_loader.get_answer_by_id).
  6. workspace resume retains the decision (build_state.json carries
     execution_decision/reason/confidence; reloading the workspace state
     reports the same values and never reclassifies).
  7. build pipeline blocked for research (status="research_complete",
     execution_blocked=True, no spec generator, no coding agent).
  8. build pipeline blocked for architecture (status="architecture_complete").
  9. implementation path still works (IMPLEMENTATION_REQUIRED proceeds through
     spec generation as before).
 10. no model calls — execution_decision imports no LLM/browser/network module.

The seam strategy mirrors the other offline test files:
  - answer_memory is pointed at a tmp DB (`answer_memory.DB_FILE` swap) and
    the lazy-init flag is reset so the schema re-creates on the new path.
  - build_workspace.WORKSPACE_ROOT is pointed at a tmp dir.
  - build_pipeline's debate_fn is injected so no browser/engine runs.
  - classify_execution_decision is exercised directly so the deterministic
    rules are pinned without touching the network.
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
from execution_decision import (
    RESEARCH_ONLY, ARCHITECTURE_ONLY, IMPLEMENTATION_REQUIRED,
    classify_execution_decision, attach_to_verification_result,
    from_verification_result, is_blocked_decision, is_implementation_required,
    block_status,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

def _with_tmp_db():
    """Point answer_memory at a fresh temp DB. Returns path for cleanup."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _set_workspace_root():
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    return Path(tmp)


def _good_spec_text():
    return (
        "# Architecture\nLayered.\n\n"
        "# Components\nAPI, worker, DB.\n\n"
        "# File Structure\nsrc/api src/worker\n\n"
        "# Dependencies\nfastapi\n\n"
        "# Constraints\nMUST use PostgreSQL.\n\n"
        "# Testing Requirements\npytest\n\n"
        "# Acceptance Criteria\ntests green\n"
    )


def _fake_debate_factory(marker="spec-OK"):
    """Build a fake debate_fn that returns the canonical spec text."""
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


def _seed_answer(question, synthesized_answer, *, execution_decision=None,
                  extra_vr=None):
    """Save a debate row in the tmp answer_memory DB. Optionally attach an
    execution_decision payload to the verification_result so the round-trip
    tests exercise the persistence layer.

    Returns the debate_id.
    """
    vr = {"verification_score": 100, "resolved_claims": [],
          "unresolved_claims": [], "equivalent_claims": [],
          "truncated_count": 0}
    if extra_vr:
        vr.update(extra_vr)
    if execution_decision is not None:
        attach_to_verification_result(vr, execution_decision)
    return answer_memory.memory.save_debate(
        question=question, gpt_final="gpt", gemini_final="gem",
        synthesized_answer=synthesized_answer, consensus_score=90,
        conflict_severity="LOW", conflict_points=[],
        validated_disputes=[], verification_result=vr,
    )


# --------------------------------------------------------------------------- #
# 1 + 2 + 3. Deterministic classification rules                                #
# --------------------------------------------------------------------------- #

def test_research_prompts_become_research_only():
    """Phase 2: research-keyword prompts classify as RESEARCH_ONLY."""
    prompts = [
        "Research the best ML frameworks for 2025",
        "Compare PostgreSQL vs MongoDB for a multi-tenant SaaS",
        "Evaluate the trade-offs between Kafka and RabbitMQ",
        "Analyze the consensus algorithms used in modern databases",
        "Survey the state of the art in vector databases",
        "Benchmark in-memory queues for low-latency trading",
    ]
    for p in prompts:
        out = classify_execution_decision(question=p)
        assert out["execution_decision"] == RESEARCH_ONLY, (
            f"expected RESEARCH_ONLY for {p!r}, got {out['execution_decision']}"
        )
        assert out["execution_confidence"] > 0
        assert out["execution_reason"]


def test_architecture_prompts_become_architecture_only():
    """Phase 2: architecture-keyword prompts classify as ARCHITECTURE_ONLY."""
    prompts = [
        "Design a planet-scale consensus platform architecture",
        "Blueprint the data layer for the analytics platform",
        "Architecture for a multi-tenant SaaS",
        "System design for a real-time notification service",
        "Platform design for a managed Kafka offering",
    ]
    for p in prompts:
        out = classify_execution_decision(question=p)
        assert out["execution_decision"] == ARCHITECTURE_ONLY, (
            f"expected ARCHITECTURE_ONLY for {p!r}, got {out['execution_decision']}"
        )


def test_implementation_prompts_become_implementation_required():
    """Phase 2: implementation-keyword prompts classify as IMPLEMENTATION_REQUIRED."""
    prompts = [
        "Implement the user authentication module for the consensus platform",
        "Build the rate limiter service",
        "Develop the session token refresh flow",
        "Generate code for the analytics ingest pipeline",
        "Create code for the audit log service",
        "Execute the migration to the new event bus",
    ]
    for p in prompts:
        out = classify_execution_decision(question=p)
        assert out["execution_decision"] == IMPLEMENTATION_REQUIRED, (
            f"expected IMPLEMENTATION_REQUIRED for {p!r}, "
            f"got {out['execution_decision']}"
        )

    # Verified decisions can upgrade a research-leaning prompt to
    # IMPLEMENTATION_REQUIRED when the debate itself directed implementation.
    overridden = classify_execution_decision(
        question="Compare Kafka vs RabbitMQ",
        verified_decisions=[
            {"decision": "Implement the user authentication module now."},
        ],
    )
    assert overridden["execution_decision"] == IMPLEMENTATION_REQUIRED

    # Pure-stdlib guarantee: the deterministic rules NEVER call a model, so an
    # upgrade from RESEARCH_ONLY to IMPLEMENTATION_REQUIRED must come from the
    # override path, not from the prompt being reinterpreted.
    no_override = classify_execution_decision(
        question="Compare Kafka vs RabbitMQ",
        verified_decisions=[],
    )
    assert no_override["execution_decision"] == RESEARCH_ONLY


# --------------------------------------------------------------------------- #
# 4. execution decision persisted                                             #
# --------------------------------------------------------------------------- #

def test_execution_decision_persisted_through_answer_memory_and_workspace():
    """The decision survives (a) the answer_memory JSON-TEXT column round-trip
    (save_debate -> get_debate) and (b) a workspace build_state.json write."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        decision = classify_execution_decision(
            question="Design the data layer architecture for the platform",
        )
        assert decision["execution_decision"] == ARCHITECTURE_ONLY

        # (a) answer_memory round-trip.
        debate_id = _seed_answer(
            "Design the data layer architecture for the platform",
            "synth body", execution_decision=decision,
        )
        row = answer_memory.memory.get_debate(debate_id)
        vr = row["verification_result"]
        assert vr["execution_decision"] == ARCHITECTURE_ONLY
        assert vr["execution_reason"] == decision["execution_reason"]
        assert vr["execution_confidence"] == decision["execution_confidence"]

        # answer_loader._shape preserves the keys on the shaped answer.
        import answer_loader as al
        shaped = al.get_answer_by_id(debate_id)
        assert shaped is not None
        loaded = from_verification_result(shaped["verification_result"])
        assert loaded is not None
        assert loaded["execution_decision"] == ARCHITECTURE_ONLY
        assert loaded["execution_reason"] == decision["execution_reason"]

        # (b) workspace build_state.json round-trip. Resurrect the answer; the
        # decision is loaded into build_state.json (Phase 3).
        res = bp.resurrect_answer(f"build answer {debate_id}")
        assert "error" not in res
        wid = res["workspace_id"]
        state = json.loads(
            (ws_root / f"workspace_{wid}" / "build_state.json").read_text()
        )
        assert state["execution_decision"] == ARCHITECTURE_ONLY
        assert state["execution_reason"] == decision["execution_reason"]
        assert state["execution_confidence"] == decision["execution_confidence"]
        assert state["execution_blocked"] is True
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 5. resurrection retains decision                                            #
# --------------------------------------------------------------------------- #

def test_resurrection_retains_decision():
    """resurrect_answer reads the persisted execution_decision from the
    answer's verification_result and surfaces it on source_result. No new
    database column, no reclassification — the stored decision is honored."""
    db_path = _with_tmp_db()
    _set_workspace_root()
    try:
        decision = classify_execution_decision(
            question="Benchmark in-memory queues for low-latency trading",
        )
        assert decision["execution_decision"] == RESEARCH_ONLY
        aid = _seed_answer(
            "Benchmark in-memory queues for low-latency trading",
            "research findings body", execution_decision=decision,
        )

        res = bp.resurrect_answer(f"build answer {aid}")
        assert "error" not in res
        source_result = res["source_result"]
        vr = source_result["verification_result"]
        loaded = from_verification_result(vr)
        assert loaded is not None
        # Immutable — the resurrected decision matches what was stored exactly.
        assert loaded["execution_decision"] == RESEARCH_ONLY
        assert loaded["execution_reason"] == decision["execution_reason"]
        assert loaded["execution_confidence"] == decision["execution_confidence"]

        # decisions.json also carries verification_result verbatim, so the
        # decision reaches the build spec extractor's input file unchanged.
        ws_dir = bw._workspace_dir(res["workspace_id"])
        decisions_blob = json.loads((ws_dir / "decisions.json").read_text())
        vr_from_file = decisions_blob.get("verification_result", {})
        assert vr_from_file.get("execution_decision") == RESEARCH_ONLY
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 6. workspace resume retains decision                                        #
# --------------------------------------------------------------------------- #

def test_workspace_resume_retains_decision():
    """build_state.json carries the decision; loading the workspace state
    (resume path) reports the same values. Phase 6 — never reclassify."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        decision = classify_execution_decision(
            question="Design a planet-scale consensus platform architecture",
        )
        assert decision["execution_decision"] == ARCHITECTURE_ONLY
        aid = _seed_answer(
            "Design a planet-scale consensus platform architecture",
            "arch body", execution_decision=decision,
        )

        res = bp.resurrect_answer(f"build answer {aid}")
        wid = res["workspace_id"]
        state_path = ws_root / f"workspace_{wid}" / "build_state.json"

        # --- First persist (already written by resurrect_answer) ---
        s1 = json.loads(state_path.read_text())
        assert s1["execution_decision"] == ARCHITECTURE_ONLY
        assert s1["execution_blocked"] is True

        # --- Simulate a resume: another process reads the workspace state
        # directly. It MUST see the same immutable decision and NOT recompute.
        # The defense-in-depth loader reproduces exactly the resume read path.
        block = bp._load_execution_decision_block(wid)
        assert block is not None
        assert block["execution_decision"] == ARCHITECTURE_ONLY
        assert block["blocked"] is True
        assert block["block_status"] == "architecture_complete"

        # --- Resolve through run_build_pipeline's resolver: persisted rule
        # wins over compute-from-prompt — the architecture-question prompt
        # would compute ARCHITECTURE_ONLY too, but the test pins that the
        # PERSISTED decision (not a freshly-computed one) is used. We confirm
        # by source == "persisted".
        resolved = bp._resolve_execution_decision(
            wid, res["source_result"], prompt="Build answer %d" % aid,
        )
        assert resolved["execution_decision"] == ARCHITECTURE_ONLY
        assert resolved["source"] == "persisted"

        # --- Immutability: rewriting build_state with other unrelated fields
        # (e.g. a fresh stage update) must NOT wipe the execution_decision.
        bw.write_build_state(wid, "plan_running", some_other_field="x")
        s2 = json.loads(state_path.read_text())
        # The execution_decision keys are preserved across the merge-update.
        assert s2["execution_decision"] == ARCHITECTURE_ONLY
        assert s2["execution_reason"] == decision["execution_reason"]
        assert s2["execution_blocked"] is True
        assert s2["some_other_field"] == "x"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 7. build pipeline blocked for research                                      #
# --------------------------------------------------------------------------- #

def test_build_pipeline_blocked_for_research():
    """A resurrected RESEARCH_ONLY answer stops the build pipeline before any
    spec generation. status="research_complete", execution_blocked=True, no
    spec file generated, no coding agent run."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    debate_invocations = {"count": 0}

    def _debate_spy(prompt, memory_ids=None):
        debate_invocations["count"] += 1
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
        }

    try:
        decision = classify_execution_decision(
            question="Survey the state of the art in vector databases",
        )
        assert decision["execution_decision"] == RESEARCH_ONLY
        aid = _seed_answer(
            "Survey the state of the art in vector databases",
            "research body", execution_decision=decision,
        )

        result = bp.run_build_pipeline(
            f"Build answer {aid}", debate_fn=_debate_spy,
        )
        assert result["execution_decision"] == RESEARCH_ONLY
        assert result["execution_blocked"] is True
        assert result["status"] == "research_complete"
        assert result["spec_status"] == "blocked"
        assert result["execution_result"]["blocked"] is True
        assert result["execution_result"]["blocked_reason"] == RESEARCH_ONLY

        # No spec generation ran — the debate_fn seam was never invoked.
        assert debate_invocations["count"] == 0, (
            "spec generation must NOT run for RESEARCH_ONLY answers"
        )

        # No implementation workspace is created for RESEARCH_ONLY.
        assert result["workspace_id"] is None
        assert not list(ws_root.glob("workspace_*"))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 8. build pipeline blocked for architecture                                  #
# --------------------------------------------------------------------------- #

def test_build_pipeline_blocked_for_architecture():
    """A resurrected ARCHITECTURE_ONLY answer stops the build pipeline at
    architecture_complete. No spec debate, no spec file, no coding agent."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    debate_invocations = {"count": 0}

    def _debate_spy(prompt, memory_ids=None):
        debate_invocations["count"] += 1
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
        }

    try:
        decision = classify_execution_decision(
            question="Design a streaming data platform architecture",
        )
        assert decision["execution_decision"] == ARCHITECTURE_ONLY
        aid = _seed_answer(
            "Design a streaming data platform architecture",
            "architecture body", execution_decision=decision,
        )

        result = bp.run_build_pipeline(
            f"Build answer {aid}", debate_fn=_debate_spy,
        )
        assert result["execution_decision"] == ARCHITECTURE_ONLY
        assert result["execution_blocked"] is True
        assert result["status"] == "architecture_complete"

        # No spec generation ran.
        assert debate_invocations["count"] == 0, (
            "spec generation must NOT run for ARCHITECTURE_ONLY answers"
        )
        # No implementation workspace is created for ARCHITECTURE_ONLY.
        assert result["workspace_id"] is None
        assert not list(ws_root.glob("workspace_*"))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 9. implementation path still works                                           #
# --------------------------------------------------------------------------- #

def test_implementation_path_still_works():
    """IMPLEMENTATION_REQUIRED answers continue through build spec generation
    as before. The execution-decision gate does not regress the production
    path."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        decision = classify_execution_decision(
            question="Implement the user authentication module",
        )
        assert decision["execution_decision"] == IMPLEMENTATION_REQUIRED
        aid = _seed_answer(
            "Implement the user authentication module",
            "Use PostgreSQL + FastAPI.", execution_decision=decision,
        )

        result = bp.run_build_pipeline(
            f"Build answer {aid}", debate_fn=_fake_debate_factory(),
        )
        assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
        assert result["answer_resurrected"] is True
        # Not blocked: implementation proceeds to spec generation.
        assert result.get("execution_blocked") is not True
        assert result["spec_status"] == "completed"
        wid = result["workspace_id"]

        # Spec was generated and stored.
        spec = bse.load_implementation_spec(wid)
        assert spec
        for section in ("# architecture", "# components", "# testing requirements"):
            assert section in spec.lower()

        # build_state.json carries IMPLEMENTATION_REQUIRED and the
        # execution_blocked flag is False.
        state = json.loads(
            (ws_root / f"workspace_{wid}" / "build_state.json").read_text()
        )
        assert state["execution_decision"] == IMPLEMENTATION_REQUIRED
        assert state["execution_blocked"] is False
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_direct_prompt_implementation_build_still_runs_spec():
    """A direct implementation prompt (no answer ref) reaches spec generation
    exactly as before — the execution-decision resolver classifies it from
    the prompt, gets IMPLEMENTATION_REQUIRED, and lets the pipeline proceed.

    Pins the regression-safe behavior: the gate is a NO-OP for the canonical
    `run_build_pipeline("Build <thing>", debate_fn=...)` happy path that
    every existing test uses."""
    ws_root = _set_workspace_root()
    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP", debate_fn=_fake_debate_factory(),
    )
    assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
    assert result.get("execution_blocked") is not True
    assert result["spec_status"] == "completed"
    wid = result["workspace_id"]
    state = json.loads(
        (ws_root / f"workspace_{wid}" / "build_state.json").read_text()
    )
    assert state["execution_decision"] == IMPLEMENTATION_REQUIRED
    assert state["execution_blocked"] is False
    # The decision source for direct prompts is "computed" (no prior debate).
    assert state["decision_source"] == "computed"


# --------------------------------------------------------------------------- #
# 10. no model calls                                                           #
# --------------------------------------------------------------------------- #

def test_no_model_calls_in_execution_decision():
    """exec_decision module must not IMPORT any LLM / browser / network
    client, and classify_execution_decision must not touch the network. The
    forbidden set mirrors the production providers actually present in this
    repo.

    The import-isolation guarantee is checked in a fresh subprocess that
    imports ONLY execution_decision — so the forbidden set is the module's
    own transitive imports, not modules pulled in by the rest of the test
    suite (debate_engine/answer_memory/etc. legitimately import providers).
    """
    import subprocess

    # 1) Fresh subprocess: import execution_decision alone, report which
    #    forbidden modules ended up in sys.modules. The cwd is the repo root
    #    so the bare `import execution_decision` resolves.
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_set = (
        "{'cerebras_client','ollama_client','openrouter_client','grok_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium',"
        "'requests','httpx','urllib3'}"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import execution_decision; "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: stdout={probe.stdout!r} stderr={probe.stderr!r}"
    )
    forbidden_loaded = json.loads(probe.stdout.strip() or "[]")
    assert forbidden_loaded == [], (
        "execution_decision transitively imports forbidden modules: "
        f"{forbidden_loaded}"
    )

    # 2) Exercising the classifier on every flavor of input doesn't load any
    #    of them either (re-probe in a fresh subprocess after classification).
    probe2 = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; from execution_decision import classify_execution_decision; "
         "classify_execution_decision(question='Research the best database'); "
         "classify_execution_decision(question='Design a system architecture'); "
         "classify_execution_decision(question='Implement the auth module'); "
         "classify_execution_decision(question='Compare X vs Y', "
         "  verified_decisions=[{'decision': 'Implement Z now'}]); "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe2.returncode == 0, (
        f"isolated classify failed: stdout={probe2.stdout!r} stderr={probe2.stderr!r}"
    )
    forbidden_after = json.loads(probe2.stdout.strip() or "[]")
    assert forbidden_after == [], (
        "execution_decision: forbidden modules imported during classification: "
        f"{forbidden_after}"
    )

    # 3) Determinism: same inputs -> identical outputs across calls.
    a = classify_execution_decision(
        question="Benchmark in-memory queues for low-latency trading",
    )
    b_ = classify_execution_decision(
        question="Benchmark in-memory queues for low-latency trading",
    )
    assert a == b_

    # 4) Helpers are pure — round-trip + None handling + predicate covering.
    vr = {"resolved_claims": []}
    attach_to_verification_result(vr, a)
    assert from_verification_result(vr) == {
        "execution_decision": a["execution_decision"],
        "execution_reason": a["execution_reason"],
        "execution_confidence": a["execution_confidence"],
        "execution_decision_source": a["execution_decision_source"],
    }
    assert from_verification_result({"resolved_claims": []}) is None
    assert is_blocked_decision(a) is True
    assert is_blocked_decision({"execution_decision": IMPLEMENTATION_REQUIRED}) is False
    assert is_blocked_decision(None) is False
    assert is_implementation_required({"execution_decision": IMPLEMENTATION_REQUIRED})
    assert not is_implementation_required({"execution_decision": RESEARCH_ONLY})
    assert block_status(RESEARCH_ONLY) == "research_complete"
    assert block_status(ARCHITECTURE_ONLY) == "architecture_complete"
    assert block_status(IMPLEMENTATION_REQUIRED) == ""


# --------------------------------------------------------------------------- #
# Defense-in-depth: execute_build_spec gate (catches direct callers)          #
# --------------------------------------------------------------------------- #

def test_execute_build_spec_gates_blocked_decision_direct_caller():
    """A direct caller of execute_build_spec on a RESEARCH_ONLY workspace is
    blocked — defense-in-depth for callers that skip run_build_pipeline."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        decision = classify_execution_decision(
            question="Survey the state of the art in vector databases",
        )
        aid = _seed_answer(
            "Survey the state of the art in vector databases",
            "research body", execution_decision=decision,
        )
        res = bp.resurrect_answer(f"build answer {aid}")
        wid = res["workspace_id"]

        # Even with an executor_fn provided (which would normally run), the
        # gate fires first and blocks without invoking the executor.
        def _bomb_executor(spec):
            raise AssertionError(
                "executor must NOT run on a RESEARCH_ONLY workspace; the "
                "execution_decision gate should have blocked first."
            )

        out = bp.execute_build_spec(wid, executor_fn=_bomb_executor)
        assert out["blocked"] is True
        assert out["blocked_reason"] == "execution_decision"
        assert out["execution_decision"] == RESEARCH_ONLY
        assert out["status"] == "research_complete"
        assert out["success"] is False
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_execution_decision.py -q")
