"""
test_deterministic_synthesis.py — TEST 7 (Deterministic Synthesis).

The final answer must be fully reproducible from the verified decision
model with NO model call:

  TEST 7a — Same input always produces identical output (byte-for-byte).
  TEST 7b — Rejected options never appear as recommendations in the
            narrative (validate_synthesis_consistency returns []).
  TEST 7c — No model invocation required: with narrative_fn=None the
            synthesis_engine NEVER calls cerebras_client.generate — proven
            by monkeypatching `synthesis_engine.generate` to a fail-fast stub
            and asserting synthesis still succeeds unchanged.
  TEST 7d — All seven sections appear in canonical order (Verified Decisions,
            Verified Tradeoffs, Human Decisions Required, Architecture,
            Implementation Plan, Migration Path, Risks).

No network, no browser, no model. The debate-55 fixture is hermetic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import synthesis_engine

from decision_renderer import (
    render_verified_decisions,
    render_verified_tradeoffs,
    render_human_required,
    render_narrative_sections,
    validate_synthesis_consistency,
)

from test_deterministic_decisions import (
    debate55_verification_result,
    debate55_governance_context,
)


def _synthesize_default():
    """synthesize_answer with narrative_fn=None — the production deterministic
    path. No LLM, no seam."""
    return synthesis_engine.synthesize_answer(
        question="Design the lowest-cost autonomous multi-agent platform",
        gpt_final="GPT position (irrelevant under determinism).",
        gemini_final="Gemini position (irrelevant under determinism).",
        validated_disputes=[],
        consensus_score=99,
        verification_result=debate55_verification_result(),
        governance_context=debate55_governance_context(),
    )


# --------------------------------------------------------------------------- #
# TEST 7a — determinism: same input -> identical output                       #
# --------------------------------------------------------------------------- #


def test_determinism_same_inputs_identical_output() -> None:
    """TEST 7a: rendering twice from the same decision model yields
    byte-identical output."""
    a = _synthesize_default()
    b = _synthesize_default()
    c = _synthesize_default()
    assert a == b == c
    assert a  # non-empty


def test_determinism_independent_of_consensus_score_and_positions() -> None:
    """A fully-deterministic answer does not depend on the LLM positions or
    the (informational-only) consensus score — only on the decision model."""
    base = _synthesize_default()
    perturbed = synthesis_engine.synthesize_answer(
        question="completely different question text",
        gpt_final="completely different GPT position text",
        gemini_final="completely different Gemini position text",
        validated_disputes=[],
        consensus_score=10,  # different
        verification_result=debate55_verification_result(),
        governance_context=debate55_governance_context(),
    )
    assert base == perturbed


# --------------------------------------------------------------------------- #
# TEST 7b — rejected options never appear as recommendations                 #
# --------------------------------------------------------------------------- #


def test_rejected_options_never_recommended() -> None:
    """TEST 7b: the deterministic narrative never recommends a verified-
    rejected option. validate_synthesis_consistency is the contract; an
    empty list means no rejected option was put in a recommend context."""
    answer = _synthesize_default()
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    contradictions = validate_synthesis_consistency(answer, vr, gc)
    assert contradictions == [], (
        f"deterministic narrative contradicted the verifier: {contradictions}")


def test_rejected_options_absent_from_seven_sections() -> None:
    """A direct check of the four narrative sections — none of the known
    debate-55 rejected options is positioned as a recommended choice. The
    renderers emit winners in affirmative locations and rejected options
    only inside explicit reject / off-limits context."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    narrative = render_narrative_sections(vr, gc)
    # The rejected options may appear — but ONLY surrounded by a negate
    # context (reject / off-limits). Use the consistency guard: scanning the
    # narrative alone (no deterministic prefix) must yield no flags.
    assert validate_synthesis_consistency(narrative, vr, gc) == []


# --------------------------------------------------------------------------- #
# TEST 7c — no model invocation required                                     #
# --------------------------------------------------------------------------- #


def test_no_model_call_on_default_path() -> None:
    """TEST 7c: with narrative_fn=None the synthesis_engine must NOT invoke
    cerebras_client.generate. We monkeypatch the imported symbol on the
    synthesis_engine module to a fail-fast stub and assert synthesis still
    completes unchanged — proving the production path is model-free."""
    sentinel_called = {"count": 0}

    def _must_not_be_called(*_args, **_kwargs):
        sentinel_called["count"] += 1
        raise AssertionError(
            "synthesis_engine.synthesize_answer invoked generate() on the "
            "default (narrative_fn=None) path — the production answer must be "
            "fully deterministic and reproducible without a model call.")

    original = synthesis_engine.generate
    synthesis_engine.generate = _must_not_be_called
    try:
        answer = _synthesize_default()
    finally:
        synthesis_engine.generate = original

    assert sentinel_called["count"] == 0, (
        "generate() was called on the deterministic default path")
    # The answer is well-formed: starts with the verifier-authoritative
    # section and contains every narrative section.
    assert answer.startswith("# Verified Decisions")
    for header in ("# Verified Decisions", "# Verified Tradeoffs",
                   "# Human Decisions Required", "# Architecture",
                   "# Implementation Plan", "# Migration Path", "# Risks"):
        assert header in answer, f"missing section: {header}"


def test_decision_renderer_module_has_no_generate_symbol() -> None:
    """The decision_renderer module — which now renders every narrative
    section — exposes no llm/generate/complete symbol; the deterministic
    output cannot depend on any external inference call by construction."""
    import decision_renderer as dr
    for forbidden in ("generate", "llm", "complete", "chat", "prompt"):
        assert not hasattr(dr, forbidden), (
            f"decision_renderer unexpectedly exposes '{forbidden}'")


# --------------------------------------------------------------------------- #
# TEST 7d — seven sections in canonical order                                #
# --------------------------------------------------------------------------- #


def test_seven_sections_present_in_canonical_order() -> None:
    """TEST 7d: the deterministic answer contains exactly the seven
    canonical sections in order — Verified Decisions, Verified Tradeoffs,
    Human Decisions Required, Architecture, Implementation Plan, Migration
    Path, Risks. Each appears exactly once at the top level."""
    answer = _synthesize_default()
    headers = [
        "# Verified Decisions",
        "# Verified Tradeoffs",
        "# Human Decisions Required",
        "# Architecture",
        "# Implementation Plan",
        "# Migration Path",
        "# Risks",
    ]
    positions = [answer.find(h) for h in headers]
    assert all(p >= 0 for p in positions), (
        f"missing header(s): {[h for h, p in zip(headers, positions) if p < 0]}")
    assert positions == sorted(positions), (
        "canonical order violated")
    for h in headers:
        assert answer.count(h) == 1, f"header appeared more than once: {h}"


def test_deterministic_sections_match_renderer_output_verbatim() -> None:
    """The three verifier-authoritative sections in the answer are EXACTLY
    what decision_renderer.render_* produce — they are the source of truth
    and survive byte-for-byte into the final answer."""
    answer = _synthesize_default()
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    assert render_verified_decisions(vr, gc) in answer
    assert render_verified_tradeoffs(vr, gc) in answer
    assert render_human_required(vr, gc) in answer


def test_debate_55_renders_seven_decisions_deterministically() -> None:
    """End-to-end on the debate-55 fixture: the final answer carries all
    seven verified decisions (no LLM, fully reproducible), and the verifier's
    finding text appears verbatim for each."""
    answer = _synthesize_default()
    vr = debate55_verification_result()
    assert "Decision Count: 7" in answer
    for claim in vr["resolved_claims"]:
        assert claim["dispute"] in answer
        assert claim["verification"]["finding"] in answer


if __name__ == "__main__":
    test_determinism_same_inputs_identical_output()
    test_determinism_independent_of_consensus_score_and_positions()
    test_rejected_options_never_recommended()
    test_rejected_options_absent_from_seven_sections()
    test_no_model_call_on_default_path()
    test_decision_renderer_module_has_no_generate_symbol()
    test_seven_sections_present_in_canonical_order()
    test_deterministic_sections_match_renderer_output_verbatim()
    test_debate_55_renders_seven_decisions_deterministically()
    print("test_deterministic_synthesis: ALL PASSED")
