"""
test_architecture_classification.py - Architecture Classification Fix V1.

Debate #145 exposed the bug: verified decisions that named a concrete
technology ("Oracle VPS", "CoinGecko", "SMA Strategy", "Exchange Adapter")
were discarded and the answer reported "No verified architectural decisions
recorded." / "No implementation steps; nothing verified." because the
architecture composer only matched a hard-coded set of platform-specific
components (Temporal / PostgreSQL / R2 / Docker / ...).

Root cause: decision_renderer._architecture_components matched each verified
decision ONLY against the fixed _COMPONENTS alias table; a decision naming any
other technology matched nothing and was dropped.

Fix: a deterministic keyword/rule-based classifier (Phase 2) recognises a
verified decision as architecture (Infrastructure / Data / Integration /
Application) when it names a concrete technology, and a deterministic
implementation step is derived from the verified architecture (Phase 3). No
model calls, no debate/verification/consensus changes.

These tests pin:
  1.  Infrastructure decisions classify correctly.
  2.  Data provider decisions classify correctly.
  3.  Exchange decisions classify correctly.
  4.  Strategy decisions classify correctly.
  5.  Mixed decisions classify correctly.
  6.  Empty decisions produce empty architecture.
  7.  Implementation plan generated from architecture.
  8.  No implementation generated without architecture.
  9.  No model calls.
  10. Debate #145 fixture produces architecture output.
  +   Architecture & implementation persist in answer_memory (Phase 5).

All offline: no network, no browser, no model. Uses a throwaway DB swap.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import answer_memory
import decision_renderer as dr
import synthesis_engine
from decision_renderer import (
    render_architecture,
    render_implementation_plan,
    render_narrative_sections,
    _classify_decision_layer,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _vr(rows):
    """rows: list of (normalized_key, dispute, finding, confidence).
    Each becomes one RESOLVED claim."""
    return {
        "resolved_claims": [
            {"normalized_key": nk, "dispute": title,
             "verification": {"status": "RESOLVED", "confidence": conf,
                              "finding": finding,
                              "requires_external_evidence": False}}
            for nk, title, finding, conf in rows
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }


def _gc():
    return {"verified_decisions": [], "verified_tradeoffs": [], "human_required": []}


def debate145_vr():
    """Debate #145 style fixture — the verified decisions that were being
    discarded (infrastructure / data / exchange / strategy)."""
    return _vr([
        ("infrastructure", "Infrastructure: hosting provider vs managed cloud",
         "Oracle VPS", 96),
        ("data_provider", "Data Provider: CoinGecko vs CoinPaprika",
         "CoinGecko", 94),
        ("exchange", "Exchange: direct adapter vs aggregator broker",
         "Exchange Adapter", 92),
        ("algorithm", "Algorithmic Strategy: SMA vs EMA",
         "SMA Strategy", 90),
    ])


# --------------------------------------------------------------------------- #
# 1-4. Per-category classification                                             #
# --------------------------------------------------------------------------- #

def test_infrastructure_decision_classifies():
    vr, gc = _vr([("infra", "Hosting: VPS vs PaaS", "Oracle VPS", 90)]), _gc()
    arch = render_architecture(vr, gc)
    assert "Infrastructure:\n- Oracle VPS" in arch, arch
    # the other architecture categories are absent for an infra-only debate
    for other in ("Data:", "Integration:", "Application:"):
        assert other not in arch, f"{other} wrongly present: {arch}"
    # platform `## <Layer>` headings are not used for generic components
    assert "## " not in arch, arch


def test_data_provider_decision_classifies():
    vr, gc = _vr([("data", "Market Data: CoinGecko vs manual", "CoinGecko", 90)]), _gc()
    arch = render_architecture(vr, gc)
    assert "Data:\n- CoinGecko" in arch, arch
    for other in ("Infrastructure:", "Integration:", "Application:"):
        assert other not in arch, f"{other} wrongly present: {arch}"


def test_exchange_decision_classifies():
    vr, gc = _vr([("exch", "Execution: exchange adapter vs REST",
                   "Exchange Adapter", 90)]), _gc()
    arch = render_architecture(vr, gc)
    assert "Integration:\n- Exchange Adapter" in arch, arch
    for other in ("Infrastructure:", "Data:", "Application:"):
        assert other not in arch, f"{other} wrongly present: {arch}"


def test_strategy_decision_classifies():
    vr, gc = _vr([("algo", "Signal: SMA vs EMA", "SMA Strategy", 90)]), _gc()
    arch = render_architecture(vr, gc)
    assert "Application:\n- SMA Strategy" in arch, arch
    for other in ("Infrastructure:", "Data:", "Integration:"):
        assert other not in arch, f"{other} wrongly present: {arch}"


# --------------------------------------------------------------------------- #
# 5. Mixed decisions classify                                                  #
# --------------------------------------------------------------------------- #

def test_mixed_decisions_classify():
    vr, gc = debate145_vr(), _gc()
    arch = render_architecture(vr, gc)
    # all four domains surface, grouped under their categories
    assert "Infrastructure:\n- Oracle VPS" in arch
    assert "Data:\n- CoinGecko" in arch
    assert "Integration:\n- Exchange Adapter" in arch
    assert "Application:\n- SMA Strategy" in arch
    # the "No verified" fallback must be gone now that decisions are recognised
    assert "No verified architectural decisions recorded." not in arch
    # a generic keyword table probe: each layer classifies deterministically
    for haystack, layer in (
        (" oracle vps hosting ", "Infrastructure"),
        (" coingecko data source ", "Data"),
        (" exchange adapter broker ", "Integration"),
        (" sma strategy algorithm ", "Application"),
    ):
        assert _classify_decision_layer(haystack) == layer


# --------------------------------------------------------------------------- #
# 6. Empty decisions produce empty architecture                                #
# --------------------------------------------------------------------------- #

def test_empty_decisions_produce_empty_architecture():
    vr, gc = {"resolved_claims": [], "equivalent_claims": [],
              "unresolved_claims": []}, _gc()
    arch = render_architecture(vr, gc)
    assert "No verified architectural decisions recorded." in arch
    for category in ("Infrastructure:", "Data:", "Integration:", "Application:"):
        assert category not in arch
    # None / empty verification_result behaves the same
    assert "No verified architectural decisions recorded." in render_architecture(None, None)
    # a decision that names no architecture keyword is not classified
    unclassifiable = _vr([("misc", "Brand colors: teal vs indigo", "Indigo", 80)]), _gc()
    arch2 = render_architecture(unclassifiable[0], unclassifiable[1])
    assert "No verified architectural decisions recorded." in arch2
    for category in ("Infrastructure:", "Data:", "Integration:", "Application:"):
        assert category not in arch2


# --------------------------------------------------------------------------- #
# 7. Implementation plan generated from architecture                           #
# --------------------------------------------------------------------------- #

def test_implementation_plan_generated_from_architecture():
    vr, gc = debate145_vr(), _gc()
    plan = render_implementation_plan(vr, gc)
    arch = render_architecture(vr, gc)
    # one deterministically-ordered step per classified component
    assert "No implementation steps; nothing verified." not in plan
    expected = [
        "Step 1\nCreate deployment configuration: Oracle VPS",
        "Step 2\nCreate market data layer: CoinGecko",
        "Step 3\nCreate exchange integration layer: Exchange Adapter",
        "Step 4\nCreate strategy module: SMA Strategy",
    ]
    for step in expected:
        assert step in plan, f"missing step {step!r} in:\n{plan}"
    # nothing outside the verified architecture is invented
    assert "Temporal" not in plan
    assert "PostgreSQL" not in plan
    # every plan component appears as an architecture bullet (only-verified)
    assert plan.count("Step ") == sum(
        1 for ln in arch.splitlines() if ln.startswith("- ")
    )


# --------------------------------------------------------------------------- #
# 8. No implementation generated without architecture                          #
# --------------------------------------------------------------------------- #

def test_no_implementation_without_architecture():
    vr, gc = {"resolved_claims": [], "equivalent_claims": [],
              "unresolved_claims": []}, _gc()
    plan = render_implementation_plan(vr, gc)
    assert "No implementation steps; nothing verified." in plan
    assert "Step 1" not in plan
    assert "Create " not in plan
    # an unclassifiable decision produces no implementation step either
    plan2 = render_implementation_plan(
        _vr([("misc", "Brand colors: teal vs indigo", "Indigo", 80)]), _gc())
    assert "No implementation steps; nothing verified." in plan2
    assert "Step 1" not in plan2


# --------------------------------------------------------------------------- #
# 9. No model calls                                                             #
# --------------------------------------------------------------------------- #

def test_classification_makes_no_model_calls():
    # 9a. the renderer module exposes no model symbol and imports no client.
    for forbidden in ("generate", "llm", "complete", "chat", "prompt"):
        assert not hasattr(dr, forbidden), f"decision_renderer exposes {forbidden}"

    # 9b. importing decision_renderer alone pulls in no model/browser client.
    script = (
        "import sys\n"
        "import decision_renderer as d\n"
        "for m in ('cerebras_client','ollama_client','openrouter_client',"
        "'groq_client','gpt_worker','gemini_worker'):\n"
        "    assert m not in sys.modules, f'unexpected import: {m}'\n"
        f"for f in {('generate','llm','complete','chat','prompt')!r}:\n"
        "    assert not hasattr(d, f), f'unexpected symbol: {f}'\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout

    # 9c. the default synthesis path never calls the LLM (monkeypatch generate
    # to raise) and still produces the architecture from the decision model.
    def _must_not_run(*_a, **_k):
        raise AssertionError("architecture classification invoked the LLM")

    vr, gc = debate145_vr(), _gc()
    original = synthesis_engine.generate
    synthesis_engine.generate = _must_not_run
    try:
        answer = synthesis_engine.synthesize_answer(
            question="Design the trading system architecture",
            gpt_final="", gemini_final="",
            validated_disputes=[],
            consensus_score=100,
            verification_result=vr,
            governance_context=gc,
        )
    finally:
        synthesis_engine.generate = original
    assert "Infrastructure:\n- Oracle VPS" in answer
    assert "# Implementation Plan" in answer

    # 9d. deterministic — no hidden state, byte-identical across calls.
    assert render_architecture(vr, gc) == render_architecture(vr, gc)
    assert render_implementation_plan(vr, gc) == render_implementation_plan(vr, gc)
    assert render_narrative_sections(vr, gc) == render_narrative_sections(vr, gc)


# --------------------------------------------------------------------------- #
# 10. Debate #145 fixture produces architecture output                         #
# --------------------------------------------------------------------------- #

def test_debate_145_fixture_produces_architecture_output():
    vr, gc = debate145_vr(), _gc()
    arch = render_architecture(vr, gc)

    # acceptance criteria: architecture section surfaces the verified decisions
    assert arch.startswith("# Architecture")
    assert "# Architecture\n\nInfrastructure:\n- Oracle VPS" in arch
    assert "Data:\n- CoinGecko" in arch
    assert "Integration:\n- Exchange Adapter" in arch
    assert "Application:\n- SMA Strategy" in arch

    # the full deterministic synthesis answer (audit artifact) carries the
    # architecture + implementation sections, consistent with the verifier.
    answer = synthesis_engine.synthesize_answer(
        question="Design the trading system architecture",
        gpt_final="", gemini_final="",
        validated_disputes=[],
        consensus_score=100,
        verification_result=vr,
        governance_context=gc,
    )
    assert "# Verified Decisions" in answer          # audit artifact prefix
    assert "# Architecture" in answer
    assert "Infrastructure:\n- Oracle VPS" in answer
    assert "# Implementation Plan" in answer
    assert "Create deployment configuration: Oracle VPS" in answer
    # no rejection contradiction is raised by the deterministic narrative
    assert "# Contradiction Warning" not in answer


# --------------------------------------------------------------------------- #
# Persistence — architecture & implementation survive save/load (Phase 5)     #
# --------------------------------------------------------------------------- #

def _with_tmp_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    if os.path.exists(tmp.name):
        os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def test_architecture_and_plan_persist_across_save_load():
    """Phase 5: the architecture/implementation sections are part of
    synthesized_answer, which answer_memory stores verbatim, so they survive a
    save/load round-trip and a deterministic re-render from the persisted
    verification_result (resume equivalence). No schema change required."""
    db_path = _with_tmp_db()
    try:
        vr, gc = debate145_vr(), _gc()
        answer = synthesis_engine.synthesize_answer(
            question="Design the trading system architecture",
            gpt_final="", gemini_final="",
            validated_disputes=[], consensus_score=100,
            verification_result=vr, governance_context=gc,
        )
        debate_id = answer_memory.memory.save_debate(
            question="Design the trading system architecture",
            gpt_final="", gemini_final="",
            synthesized_answer=answer, consensus_score=96,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result=vr,
        )
        row = answer_memory.memory.get_debate(debate_id)

        # 1. persisted verbatim in synthesized_answer
        synth = row["synthesized_answer"]
        assert "# Architecture" in synth
        assert "Infrastructure:\n- Oracle VPS" in synth
        assert "# Implementation Plan" in synth
        assert "Create deployment configuration: Oracle VPS" in synth

        # 2. verification_result is preserved, enabling resume
        stored_vr = row["verification_result"]
        assert len(stored_vr["resolved_claims"]) == 4

        # 3. resume equivalence: re-rendering from the persisted decision model
        #    reproduces the same architecture byte-for-byte (workspace resume).
        assert render_architecture(stored_vr, _gc()) == render_architecture(vr, _gc())
        assert render_implementation_plan(stored_vr, _gc()) == render_implementation_plan(vr, _gc())
        assert "Infrastructure:\n- Oracle VPS" in render_architecture(stored_vr, _gc())

        # 4. assembly from stored parts matches the persisted artifact's sections
        reassembled = (
            dr.render_verified_decisions(stored_vr, _gc())
            + "\n\n" + dr.render_verified_tradeoffs(stored_vr, _gc())
            + "\n\n" + dr.render_human_required(stored_vr, _gc())
            + "\n\n" + render_narrative_sections(stored_vr, _gc())
        )
        assert "Infrastructure:\n- Oracle VPS" in reassembled
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    test_infrastructure_decision_classifies()
    test_data_provider_decision_classifies()
    test_exchange_decision_classifies()
    test_strategy_decision_classifies()
    test_mixed_decisions_classify()
    test_empty_decisions_produce_empty_architecture()
    test_implementation_plan_generated_from_architecture()
    test_no_implementation_without_architecture()
    test_classification_makes_no_model_calls()
    test_debate_145_fixture_produces_architecture_output()
    test_architecture_and_plan_persist_across_save_load()
    print("test_architecture_classification: ALL PASSED")
