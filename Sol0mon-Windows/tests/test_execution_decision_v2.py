"""
test_execution_decision_v2.py - Debate-Governed Execution Decisions V2.

Offline only. No model calls. No browser. No external APIs.

V2 replaces the V1 keyword-driven classifier with a decision-model-driven
hierarchy. The verifier's output (human_required, unresolved_claims, verified
decisions) is the strongest signal; the prompt's wording is the weakest
(fallback rung 6).

Verifies the 10 contracts:

  1. human_required blocks   — rung 1: STOP_HUMAN_REQUIRED (deferred to the
     pending-decision gate, not the 3.5 hard-stop).
  2. unresolved disputes block — rung 2: STOP_UNRESOLVED_DISPUTES hard-stops
     the pipeline before spec generation.
  3. implementation artifacts override prompt — rung 3: the CRITICAL test.
     A "Design a planet scale consensus platform" prompt with verified
     decisions "Deploy PostgreSQL / Deploy Temporal / Deploy Worker Pool"
     classifies IMPLEMENTATION_REQUIRED (V1's keyword classifier failed this
     — it produced ARCHITECTURE_ONLY because "deploy" was not a V1
     implementation keyword; V2 fixes it via the implementation-artifacts rung).
  4. architecture stops      — rung 4: architecture signals in verified
     decisions (orchestration / deployment topology) stop the build. Word
     boundaries stop "deploy" from matching "deployment topology".
  5. research stops          — rung 5: research signals in verification_result
     (comparison / benchmark) stop the build.
  6. prompt fallback        — rung 6: no decision-model signal → V1 keyword
     classification of the prompt, source="prompt_fallback".
  7. persistence            — a V2 decision (with execution_decision_source)
     survives the answer_memory JSON-TEXT round-trip (save_debate/get_debate
     + from_verification_result returns all 4 keys).
  8. resurrection            — resurrect_answer surfaces the decision AND its
     source onto build_state.json.
  9. resume + pipeline block — a resurrected STOP_UNRESOLVED_DISPUTES answer
     hard-stops run_build_pipeline at Step 3.5 (status="unresolved_disputes",
     no spec, no debate); _resolve_execution_decision reports source=
     "persisted" and the classifier source is preserved across build_state
     rewrites.
 10. no model calls          — execution_decision imports no LLM/browser/
     network module; the classifier is deterministic; the helpers cover the
     new STOP_* types and execution_decision_source.
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
    STOP_HUMAN_REQUIRED, STOP_UNRESOLVED_DISPUTES,
    classify_execution_decision, attach_to_verification_result,
    from_verification_result, is_blocked_decision, is_implementation_required,
    block_status,
)
from execution_decision import (
    SOURCE_HUMAN_REQUIRED, SOURCE_UNRESOLVED_DISPUTES,
    SOURCE_IMPLEMENTATION_ARTIFACTS, SOURCE_ARCHITECTURE_DECISIONS,
    SOURCE_RESEARCH_ANALYSIS, SOURCE_PROMPT_FALLBACK,
)


# --------------------------------------------------------------------------- #
# Fixtures (mirror test_execution_decision.py)                                #
# --------------------------------------------------------------------------- #

def _with_tmp_db():
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


def _debate_spy_factory(counter):
    def _spy(prompt, memory_ids=None):
        counter["count"] += 1
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
        }
    return _spy


def _seed_answer(question, synthesized_answer, *, decision=None, extra_vr=None):
    vr = {"verification_score": 100, "resolved_claims": [],
          "unresolved_claims": [], "equivalent_claims": [],
          "truncated_count": 0}
    if extra_vr:
        vr.update(extra_vr)
    if decision is not None:
        attach_to_verification_result(vr, decision)
    return answer_memory.memory.save_debate(
        question=question, gpt_final="gpt", gemini_final="gem",
        synthesized_answer=synthesized_answer, consensus_score=90,
        conflict_severity="LOW", conflict_points=[],
        validated_disputes=[], verification_result=vr,
    )


# --------------------------------------------------------------------------- #
# 1. human_required blocks (rung 1)                                            #
# --------------------------------------------------------------------------- #

def test_human_required_blocks():
    """Rung 1: a non-empty human_required list yields STOP_HUMAN_REQUIRED
    with source="human_required". is_blocked_decision is False — the actual
    build block is DEFERRED to execute_build_spec's pending-decision gate so
    spec generation still runs and the resolve-then-proceed flow survives
    (the existing human-escalation contract)."""
    out = classify_execution_decision(
        question="Build the user authentication module",
        human_required=[
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "deploy",
             "dispute_text": "K8s vs Nomad", "option_a": "K8s",
             "option_b": "Nomad", "reasoning": "ops preference",
             "decision_id": "d1"},
        ],
    )
    assert out["execution_decision"] == STOP_HUMAN_REQUIRED, out
    assert out["execution_decision_source"] == SOURCE_HUMAN_REQUIRED
    # Deferred — the 3.5 gate lets it through to the pending gate.
    assert is_blocked_decision(out) is False
    assert block_status(out) == "human_required"

    # Full pipeline: a human_required source_result flows through spec
    # generation (NOT blocked at 3.5) and is held at the pending-decision
    # gate in execute_build_spec — exactly the pre-V2 behavior.
    _set_workspace_root()
    gov = {
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "deploy",
             "dispute_text": "K8s vs Nomad", "option_a": "K8s",
             "option_b": "Nomad", "reasoning": "ops preference",
             "decision_id": "d1"},
        ],
    }
    spy = {"count": 0}
    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_debate_spy_factory(spy),
        source_result=gov,
    )
    assert result["spec_status"] == "completed", (
        "STOP_HUMAN_REQUIRED must NOT hard-stop the pipeline; spec should run"
    )
    assert result.get("execution_blocked") is not True
    exec_result = result["execution_result"]
    assert exec_result.get("blocked") is True
    assert exec_result.get("blocked_reason") == "pending_human_decisions"
    # The spec debate ran (the hard-stop types RESEARCH/ARCH/UNRESOLVED would
    # NOT have run it); the pending gate is what held the build.
    assert spy["count"] > 0


# --------------------------------------------------------------------------- #
# 2. unresolved disputes block (rung 2)                                        #
# --------------------------------------------------------------------------- #

def test_unresolved_disputes_block():
    """Rung 2: unresolved_claims in the verification_result yields
    STOP_UNRESOLVED_DISPUTES — a HARD stop at the 3.5 gate (the debate outcome
    is incomplete; governance did not escalate to a human). Even an explicit
    implementation-intent prompt is overridden by the unresolved-disputes
    rung (the hierarchy never reverses)."""
    out = classify_execution_decision(
        question="Implement the user authentication module",
        verification_result={
            "unresolved_claims": [
                {"normalized_key": "db_choice",
                 "dispute": "PostgreSQL vs MongoDB",
                 "verification": {"status": "UNRESOLVED", "confidence": 40,
                                  "finding": "no evidence settles it"}},
            ],
        },
    )
    assert out["execution_decision"] == STOP_UNRESOLVED_DISPUTES, out
    assert out["execution_decision_source"] == SOURCE_UNRESOLVED_DISPUTES
    assert is_blocked_decision(out) is True
    assert block_status(out) == "unresolved_disputes"

    # hierarchy never reverses: human_required is checked BEFORE unresolved,
    # so an empty human_required list is required for rung 2 to win.
    assert STOP_UNRESOLVED_DISPUTES not in (
        RESEARCH_ONLY, ARCHITECTURE_ONLY, IMPLEMENTATION_REQUIRED,
        STOP_HUMAN_REQUIRED,
    )


# --------------------------------------------------------------------------- #
# 3. implementation artifacts override prompt (CRITICAL test, rung 3)         #
# --------------------------------------------------------------------------- #

def test_implementation_artifacts_override_prompt():
    """Rung 3 (the CRITICAL test): a "Design a planet scale consensus
    platform" prompt — which the V1 keyword classifier would classify
    ARCHITECTURE_ONLY (because "design" is an architecture keyword and
    "deploy" was not a V1 implementation keyword) — upgrades to
    IMPLEMENTATION_REQUIRED when the verified decision model contains
    explicit deployment decisions. Verifier output overrides prompt wording.
    """
    # The V1 classifier would produce ARCHITECTURE_ONLY here (prompt-only).
    prompt_only = classify_execution_decision(
        question="Design a planet scale consensus platform",
    )
    assert prompt_only["execution_decision"] == ARCHITECTURE_ONLY, (
        "sanity: prompt alone is architecture (V1 behavior preserved at "
        "rung 6); got %s" % prompt_only["execution_decision"]
    )

    # V2: the verified decisions say "Deploy X" three times → rung 3 fires.
    out = classify_execution_decision(
        question="Design a planet scale consensus platform",
        verified_decisions=[
            {"decision": "Deploy PostgreSQL"},
            {"decision": "Deploy Temporal"},
            {"decision": "Deploy Worker Pool"},
        ],
    )
    assert out["execution_decision"] == IMPLEMENTATION_REQUIRED, (
        "CRITICAL: verified 'Deploy …' decisions must override the "
        "architecture-leaning prompt → IMPLEMENTATION_REQUIRED; got %s"
        % out["execution_decision"]
    )
    assert out["execution_decision_source"] == SOURCE_IMPLEMENTATION_ARTIFACTS
    assert is_blocked_decision(out) is False

    # The override also fires for research-leaning prompts.
    research_override = classify_execution_decision(
        question="Compare Kafka vs RabbitMQ",
        verified_decisions=[{"decision": "Implement the auth module now."}],
    )
    assert research_override["execution_decision"] == IMPLEMENTATION_REQUIRED
    assert research_override["execution_decision_source"] == (
        SOURCE_IMPLEMENTATION_ARTIFACTS
    )


# --------------------------------------------------------------------------- #
# 4. architecture stops (rung 4)                                               #
# --------------------------------------------------------------------------- #

def test_architecture_decisions_stop():
    """Rung 4: architecture signals (orchestration / deployment topology /
    storage design / security model / provider routing / database selection)
    in verified decisions — with NO implementation signal on record — stop
    the build at architecture. Word boundaries stop "deploy" from matching
    "deployment topology" so an architecture decision is not misclassified
    as implementation."""
    out = classify_execution_decision(
        question="Research the trade-offs for the platform",
        verified_decisions=[
            {"decision": "Deployment topology: use Kubernetes for "
                         "orchestration"},
        ],
    )
    assert out["execution_decision"] == ARCHITECTURE_ONLY, out
    assert out["execution_decision_source"] == SOURCE_ARCHITECTURE_DECISIONS
    assert is_blocked_decision(out) is True
    assert block_status(out) == "architecture_complete"

    # storage design + security model also fire the architecture rung.
    arch2 = classify_execution_decision(
        question="Tell me about the system",
        verified_decisions=[
            {"decision": "Storage design: use object storage; security model "
                         "is RBAC + mTLS"},
        ],
    )
    assert arch2["execution_decision"] == ARCHITECTURE_ONLY, arch2
    assert arch2["execution_decision_source"] == SOURCE_ARCHITECTURE_DECISIONS


# --------------------------------------------------------------------------- #
# 5. research stops (rung 5)                                                  #
# --------------------------------------------------------------------------- #

def test_research_analysis_stops():
    """Rung 5: research signals (comparison / evaluation / benchmark / analysis)
    in the verification_result — with no implementation or architecture
    artifacts in the decision model — stop the build at research."""
    out = classify_execution_decision(
        question="Tell me about the platform",
        verification_result={
            "resolved_claims": [
                {"normalized_key": "queue_cmp", "dispute": "Kafka vs RabbitMQ",
                 "verification": {
                     "status": "RESOLVED", "confidence": 85,
                     "finding": "Benchmark shows Kafka has higher throughput; "
                                "comparison favors Kafka for streaming."},
                 "research_summary": "Evaluation of message queues."},
            ],
            "unresolved_claims": [],
        },
    )
    assert out["execution_decision"] == RESEARCH_ONLY, out
    assert out["execution_decision_source"] == SOURCE_RESEARCH_ANALYSIS
    assert is_blocked_decision(out) is True
    assert block_status(out) == "research_complete"


# --------------------------------------------------------------------------- #
# 6. prompt fallback (rung 6)                                                 #
# --------------------------------------------------------------------------- #

def test_prompt_fallback():
    """Rung 6: with no decision-model signal (no human_required, no
    unresolved claims, no verified decisions, no verification research
    signals), classification falls back to V1 prompt-keyword counting and
    sources from "prompt_fallback"."""
    # Implementation keyword in the prompt.
    impl = classify_execution_decision(question="Build the auth module")
    assert impl["execution_decision"] == IMPLEMENTATION_REQUIRED
    assert impl["execution_decision_source"] == SOURCE_PROMPT_FALLBACK

    # Architecture keyword in the prompt.
    arch = classify_execution_decision(
        question="Design a planet-scale consensus platform architecture")
    assert arch["execution_decision"] == ARCHITECTURE_ONLY
    assert arch["execution_decision_source"] == SOURCE_PROMPT_FALLBACK

    # Research keyword in the prompt.
    res = classify_execution_decision(
        question="Compare PostgreSQL vs MongoDB for a multi-tenant SaaS")
    assert res["execution_decision"] == RESEARCH_ONLY
    assert res["execution_decision_source"] == SOURCE_PROMPT_FALLBACK

    # No keyword at all → safest-stop default (RESEARCH_ONLY), confidence 60.
    blank = classify_execution_decision(question="Tell me about 2024")
    assert blank["execution_decision"] == RESEARCH_ONLY
    assert blank["execution_decision_source"] == SOURCE_PROMPT_FALLBACK
    assert blank["execution_confidence"] == 60

    # An explicit empty verification_result (unresolved_claims=[]) does not
    # trip rung 2, so a research-keyword prompt still falls to rung 6.
    res_clean = classify_execution_decision(
        question="Benchmark in-memory queues",
        verification_result={"unresolved_claims": []},
    )
    assert res_clean["execution_decision"] == RESEARCH_ONLY
    assert res_clean["execution_decision_source"] == SOURCE_PROMPT_FALLBACK


# --------------------------------------------------------------------------- #
# 7. persistence (answer_memory JSON round-trip carries the source)           #
# --------------------------------------------------------------------------- #

def test_decision_persistence_round_trip():
    """A V2 decision (with execution_decision_source) survives the
    answer_memory JSON-TEXT column round-trip: save_debate → get_debate →
    from_verification_result returns all 4 keys."""
    db_path = _with_tmp_db()
    try:
        decision = classify_execution_decision(
            question="Implement the auth module",
            verification_result={
                "unresolved_claims": [{"normalized_key": "k"}],
            },
        )
        assert decision["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert decision["execution_decision_source"] == (
            SOURCE_UNRESOLVED_DISPUTES)

        aid = _seed_answer(
            "Implement the auth module", "body", decision=decision,
        )
        row = answer_memory.memory.get_debate(aid)
        vr = row["verification_result"]
        assert vr["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert vr["execution_decision_source"] == SOURCE_UNRESOLVED_DISPUTES
        assert vr["execution_reason"] == decision["execution_reason"]
        assert vr["execution_confidence"] == decision["execution_confidence"]

        loaded = from_verification_result(vr)
        assert loaded == {
            "execution_decision": STOP_UNRESOLVED_DISPUTES,
            "execution_reason": decision["execution_reason"],
            "execution_confidence": decision["execution_confidence"],
            "execution_decision_source": SOURCE_UNRESOLVED_DISPUTES,
        }
        # No decision stored → None.
        assert from_verification_result({"resolved_claims": []}) is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 8. resurrection (build_state carries decision + source)                      #
# --------------------------------------------------------------------------- #

def test_resurrection_carries_decision_and_source():
    """resurrect_answer loads the immutable V2 decision (including
    execution_decision_source) into build_state.json."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    try:
        decision = classify_execution_decision(
            question="Implement the auth module",
            verification_result={
                "unresolved_claims": [{"normalized_key": "k"}],
            },
        )
        assert decision["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        aid = _seed_answer(
            "Implement the auth module", "body", decision=decision,
        )

        res = bp.resurrect_answer(f"build answer {aid}")
        assert "error" not in res
        wid = res["workspace_id"]
        state = json.loads(
            (ws_root / f"workspace_{wid}" / "build_state.json").read_text()
        )
        assert state["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert state["execution_decision_source"] == (
            SOURCE_UNRESOLVED_DISPUTES)
        assert state["execution_reason"] == decision["execution_reason"]
        assert state["execution_confidence"] == decision["execution_confidence"]
        assert state["execution_blocked"] is True

        # The resurrected source_result carries the decision on its
        # verification_result so the resolver honors it without reclassifying.
        loaded = from_verification_result(res["source_result"]
                                           ["verification_result"])
        assert loaded is not None
        assert loaded["execution_decision_source"] == (
            SOURCE_UNRESOLVED_DISPUTES)
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 9. resume + pipeline block for unresolved disputes                           #
# --------------------------------------------------------------------------- #

def test_resume_and_pipeline_block_for_unresolved():
    """A resurrected STOP_UNRESOLVED_DISPUTES answer hard-stops the whole
    pipeline at Step 3.5: status="unresolved_disputes", execution_blocked=True,
    no spec debate runs, no spec file is written. The resume read paths
    (_load_execution_decision_block, _resolve_execution_decision) report
    source="persisted" and preserve the classifier source across build_state
    rewrites."""
    db_path = _with_tmp_db()
    ws_root = _set_workspace_root()
    spy = {"count": 0}
    try:
        decision = classify_execution_decision(
            question="Implement the auth module",
            verification_result={
                "unresolved_claims": [{"normalized_key": "k"}],
            },
        )
        aid = _seed_answer(
            "Implement the auth module", "body", decision=decision,
        )

        # --- resume reads on a manually-resurrected workspace. ------------
        res = bp.resurrect_answer(f"build answer {aid}")
        wid = res["workspace_id"]
        state_path = ws_root / f"workspace_{wid}" / "build_state.json"

        # resume read #1: _load_execution_decision_block
        block = bp._load_execution_decision_block(wid)
        assert block is not None
        assert block["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert block["execution_decision_source"] == (
            SOURCE_UNRESOLVED_DISPUTES)
        assert block["blocked"] is True
        assert block["block_status"] == "unresolved_disputes"

        # resume read #2: _resolve_execution_decision → persisted wins
        resolved = bp._resolve_execution_decision(
            wid, res["source_result"], prompt="Build answer %d" % aid,
        )
        assert resolved["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert resolved["execution_decision_source"] == (
            SOURCE_UNRESOLVED_DISPUTES)
        assert resolved["source"] == "persisted"  # not recomputed
        assert resolved["blocked"] is True

        # immutability: an unrelated build_state merge-update preserves the
        # execution decision + source keys (resume never reclassifies).
        bw.write_build_state(wid, "plan_running", some_other_field="x")
        s_pre = json.loads(state_path.read_text())
        assert s_pre["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert s_pre["execution_decision_source"] == SOURCE_UNRESOLVED_DISPUTES
        assert s_pre["execution_blocked"] is True
        assert s_pre["some_other_field"] == "x"

        # --- full pipeline block (run_build_pipeline resurrects its own
        # workspace; use result["workspace_id"] for the block assertions). --
        result = bp.run_build_pipeline(
            f"Build answer {aid}", debate_fn=_debate_spy_factory(spy),
        )
        assert result["execution_decision"] == STOP_UNRESOLVED_DISPUTES
        assert result["execution_decision_source"] == (
            SOURCE_UNRESOLVED_DISPUTES)
        assert result["execution_blocked"] is True
        assert result["status"] == "unresolved_disputes"
        assert result["spec_status"] == "blocked"
        assert spy["count"] == 0, (
            "spec generation must NOT run for STOP_UNRESOLVED_DISPUTES"
        )

        # No implementation workspace is created for unresolved disputes.
        assert result["workspace_id"] is None
        # The earlier direct resurrection workspace still exists; the pipeline
        # must not create a second workspace for this blocked run.
        assert len(list(ws_root.glob("workspace_*"))) == 1
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 10. no model calls + determinism + helper coverage                           #
# --------------------------------------------------------------------------- #

def test_no_model_calls_and_determinism():
    """execution_decision imports no LLM/browser/network module (checked in a
    fresh subprocess so the forbidden set is the module's own transitive
    imports). The classifier is deterministic, and the helpers cover the new
    V2 decision types and execution_decision_source."""
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    forbidden_set = (
        "{'cerebras_client','ollama_client','openrouter_client','grok_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium',"
        "'requests','httpx','urllib3'}"
    )
    # 1) isolated import — no forbidden modules transitively loaded.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import execution_decision; "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: stdout={probe.stdout!r} "
        f"stderr={probe.stderr!r}"
    )
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "execution_decision transitively imports forbidden modules"
    )

    # 2) exercising every rung in a fresh subprocess loads nothing forbidden.
    probe2 = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; from execution_decision import classify_execution_decision; "
         "classify_execution_decision(question='Research the best database'); "
         "classify_execution_decision(question='Build the auth module', "
         "  human_required=[{'outcome_type':'HUMAN_REQUIRED'}]); "
         "classify_execution_decision(question='Build the auth module', "
         "  verification_result={'unresolved_claims':[{'normalized_key':'k'}]}); "
         "classify_execution_decision(question='Design a platform', "
         "  verified_decisions=[{'decision':'Deploy PostgreSQL'}]); "
         "classify_execution_decision(question='Research x', "
         "  verified_decisions=[{'decision':'Deployment topology: K8s'}]); "
         "classify_execution_decision(question='Tell me', "
         "  verification_result={'resolved_claims':[{'dispute':'Benchmark X',"
         "    'verification':{'finding':'benchmark comparison'}}]}); "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe2.returncode == 0, (
        f"isolated classify failed: stdout={probe2.stdout!r} "
        f"stderr={probe2.stderr!r}"
    )
    assert json.loads(probe2.stdout.strip() or "[]") == [], (
        "execution_decision: forbidden modules imported during classification"
    )

    # 3) determinism: same inputs → identical outputs across calls.
    vr = {"unresolved_claims": [{"normalized_key": "k"}]}
    a = classify_execution_decision(question="Implement the auth module",
                                     verification_result=vr)
    b_ = classify_execution_decision(question="Implement the auth module",
                                      verification_result=vr)
    assert a == b_

    # 4) helper coverage for the new V2 types.
    assert is_blocked_decision(
        {"execution_decision": STOP_UNRESOLVED_DISPUTES}) is True
    assert is_blocked_decision(
        {"execution_decision": STOP_HUMAN_REQUIRED}) is False  # deferred
    assert is_blocked_decision(
        {"execution_decision": RESEARCH_ONLY}) is True
    assert is_blocked_decision(
        {"execution_decision": ARCHITECTURE_ONLY}) is True
    assert is_blocked_decision(
        {"execution_decision": IMPLEMENTATION_REQUIRED}) is False
    assert is_blocked_decision(None) is False

    assert is_implementation_required(
        {"execution_decision": IMPLEMENTATION_REQUIRED}) is True
    assert not is_implementation_required(
        {"execution_decision": STOP_UNRESOLVED_DISPUTES})

    assert block_status(STOP_UNRESOLVED_DISPUTES) == "unresolved_disputes"
    assert block_status(STOP_HUMAN_REQUIRED) == "human_required"
    assert block_status(RESEARCH_ONLY) == "research_complete"
    assert block_status(ARCHITECTURE_ONLY) == "architecture_complete"
    assert block_status(IMPLEMENTATION_REQUIRED) == ""
    assert block_status(None) == ""

    # 5) round-trip persistence carries all 4 keys (incl. source).
    out = classify_execution_decision(question="Compare Kafka vs RabbitMQ",
                                       verified_decisions=[])
    vr2 = {"resolved_claims": []}
    attach_to_verification_result(vr2, out)
    assert from_verification_result(vr2) == {
        "execution_decision": out["execution_decision"],
        "execution_reason": out["execution_reason"],
        "execution_confidence": out["execution_confidence"],
        "execution_decision_source": out["execution_decision_source"],
    }


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_execution_decision_v2.py -q")
