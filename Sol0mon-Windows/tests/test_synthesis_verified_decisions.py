"""
test_synthesis_verified_decisions.py - TEST 3 + TEST 4.

TEST 4: build_synthesis_prompt includes winner + confidence + rationale for
        resolved disputes (not just the dispute title).  (FIX 3)
TEST 3: an implementation spec cannot select architecture that verification
        rejected — the verified decisions are injected as immutable MUST
        constraints so the build prompt forbids the rejected option.

No model calls; builds the prompt text deterministically and inspects it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synthesis_engine import build_synthesis_prompt
from build_prompt_generator import generate_build_prompt, extract_decisions, extract_constraints
from build_spec_engine import build_spec, _spec_decision_gaps


def _verification_result():
    return {
        "resolved_claims": [
            {"dispute": "Execution: Server-side Docker vs Client-side WebContainers",
             "normalized_key": "execution_environment",
             "verification": {
                 "status": "RESOLVED", "confidence": 99,
                 "finding": "Resolved in favor of Server-side Docker Workers. "
                            "Both evidence strips explicitly reject Client-side "
                            "WebContainers/WebAssembly for the authoritative path.",
                 "requires_external_evidence": False}},
            {"dispute": "Primary Database: SQLite (server) vs IndexedDB (client)",
             "normalized_key": "primary_database",
             "verification": {
                 "status": "RESOLVED", "confidence": 95,
                 "finding": "Resolved in favor of server-side SQLite authoritative "
                            "storage; rejects client-side IndexedDB.",
                 "requires_external_evidence": False}},
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }


# --------------------------------------------------------------------------- #
# TEST 4 — synthesis context carries winner + confidence + rationale          #
# --------------------------------------------------------------------------- #

def test_synthesis_prompt_includes_winner_confidence_rationale() -> None:
    """FIX 3 / TEST 4: the synthesis prompt's RESOLVED section contains the
    finding (winner), the confidence, and the rationale — not just the title."""
    vr = _verification_result()
    prompt = build_synthesis_prompt(
        question="Design architecture",
        gpt_final="gpt", gemini_final="gemini",
        validated_disputes=[
            {"normalized_key": "execution_environment",
             "dispute": "Execution: Docker vs WebContainers"},
        ],
        consensus_score=96,
        verification_result=vr,
        governance_context=None,
    )

    # The dispute title alone is no longer the only thing under RESOLVED.
    assert "Execution: Server-side Docker vs Client-side WebContainers" in prompt
    # Winner (finding content) is present.
    assert "Server-side Docker Workers" in prompt
    # Confidence is present.
    assert "Confidence: 99%" in prompt
    # Rationale label + finding present.
    assert "Rationale:" in prompt
    assert "reject Client-side WebContainers" in prompt
    # The other dispute's confidence too.
    assert "Confidence: 95%" in prompt
    assert "authoritative storage" in prompt


def test_synthesis_prompt_no_longer_bare_title_only() -> None:
    """Regression guard: the old behaviour emitted only
    '[RESOLVED] key: title'. Ensure winner lines now exist."""
    vr = _verification_result()
    prompt = build_synthesis_prompt(
        question="q", gpt_final="g", gemini_final="m",
        validated_disputes=[], consensus_score=100,
        verification_result=vr, governance_context=None,
    )
    assert "Winner:" in prompt
    assert "Confidence:" in prompt
    # The bare-title-only line must not be the whole RESOLVED block.
    resolved_block = prompt.split("Resolved Disputes")[1].split("Equivalent")[0]
    assert "Server-side Docker Workers" in resolved_block


# --------------------------------------------------------------------------- #
# TEST 3 — spec cannot select architecture verification rejected              #
# --------------------------------------------------------------------------- #

def _verified_decisions_from_vr(vr):
    """Schema-A bucket bridged from verification_result (as answer_loader does)."""
    import answer_loader as al
    return al._to_verified_decisions(vr)


def test_build_prompt_forbids_rejected_architecture() -> None:
    """TEST 3: the build prompt lists the verified winner as an immutable MUST
    constraint and records the rejected option in the reasoning, so the
    implementation debate cannot legitimately select the rejected architecture."""
    vr = _verification_result()
    vdec = _verified_decisions_from_vr(vr)
    src = {"verified_decisions": vdec, "verification_result": vr}
    decisions = extract_decisions(src)
    constraints = extract_constraints(src)

    assert len(constraints) == 2, "both verified decisions must become constraints"
    prompt = generate_build_prompt(
        "Server-side SQLite + Docker workers.", decisions, constraints, []
    )

    low = prompt.lower()
    # Constraints section is populated and marked immutable.
    assert "## constraints (immutable)" in low
    assert "(none)" not in low.split("## constraints (immutable)")[1].split("##")[0]
    # The winning option is mandated as a MUST constraint.
    assert "must" in low
    assert "server-side docker workers" in low
    assert "authoritative storage" in low and "sqlite" in low
    # The rejected option is carried in the reasoning so it's visibly off-limits.
    assert "webcontainers" in low
    assert "indexeddb" in low


def test_decision_gaps_detects_spec_ignoring_verification() -> None:
    """TEST 3 (diagnostic side): _spec_decision_gaps flags spec text that drops
    a verified decision entirely (the necessary-condition check for a spec
    silently contradicting verification)."""
    vr = _verification_result()
    vdec = _verified_decisions_from_vr(vr)

    # Spec that honours the decisions -> no gaps.
    honoring = (
        "# Architecture\nServer-side docker workers + server-side sqlite.\n"
        "# Components\napi\n# File Structure\nsrc\n# Dependencies\ndocker sqlite\n"
        "# Constraints\nMUST use docker + sqlite.\n"
        "# Testing Requirements\npytest\n# Acceptance Criteria\npass\n"
    )
    assert _spec_decision_gaps(honoring, vdec) == []

    # Spec that ignores ALL verified decisions -> both flagged as gaps.
    ignoring = (
        "# Architecture\nclient-side webcontainers + indexeddb only.\n"
        "# Components\nx\n# File Structure\ny\n# Dependencies\nz\n"
        "# Constraints\nnone\n# Testing Requirements\nnone\n"
        "# Acceptance Criteria\nnone\n"
    )
    gaps = _spec_decision_gaps(ignoring, vdec)
    assert len(gaps) == 2, f"expected 2 gaps, got {len(gaps)}"


def test_build_spec_with_ignoring_debate_still_surfaces_gaps() -> None:
    """TEST 3 end-to-end: even if the (mocked) debate produces a spec that ignores
    the decisions, build_spec surfaces them in decision_gaps so a caller can
    reject the spec. The enforcement point (constraints in prompt) is also fed."""
    vr = _verification_result()
    vdec = _verified_decisions_from_vr(vr)
    src = {"verified_decisions": vdec, "verification_result": vr}

    def _ignoring_debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": (
                "# Architecture\nclient webcontainers + indexeddb.\n"
                "# Components\nx\n# File Structure\ny\n# Dependencies\nz\n"
                "# Constraints\nnone\n# Testing Requirements\nnone\n"
                "# Acceptance Criteria\nnone\n"
            ),
            "gpt_final": "g", "gemini_final": "m", "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [], "final_consensus_score": 80,
        }

    out = build_spec("irrelevant prose", debate_fn=_ignoring_debate, source_result=src)
    # The decision model is surfaced regardless of what the debate produced.
    assert len(out["verified_decisions"]) == 2
    # The ignoring spec is flagged.
    assert len(out["decision_gaps"]) == 2
    # The build prompt that was fed to the debate contained the constraints.
    assert "server-side docker workers" in out["prompt"].lower()


if __name__ == "__main__":
    test_synthesis_prompt_includes_winner_confidence_rationale()
    test_synthesis_prompt_no_longer_bare_title_only()
    test_build_prompt_forbids_rejected_architecture()
    test_decision_gaps_detects_spec_ignoring_verification()
    test_build_spec_with_ignoring_debate_still_surfaces_gaps()
    print("test_synthesis_verified_decisions: ALL PASSED")
