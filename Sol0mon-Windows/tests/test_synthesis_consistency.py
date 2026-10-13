"""
test_synthesis_consistency.py - TEST 4.

Given debate 55, generate a synthesized answer and verify that NO
contradiction exists between verification_result and the synthesized answer.
The Verified Decisions / Tradeoffs / Human Decisions Required sections are
generated deterministically from verification_result; only the LLM narrative
could contradict the verifier, and validate_synthesis_consistency() guards it.

No real model call is used: synthesize_answer's `narrative_fn` seam supplies a
fixed narrative (mirrors the debate_fn / executor_fn seams used elsewhere in
the codebase). This is not a NEW model call — production leaves narrative_fn
None and uses the existing Cerebras path.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decision_renderer import (
    render_verified_decisions,
    render_verified_tradeoffs,
    render_human_required,
    validate_synthesis_consistency,
    contradiction_warning,
)
from synthesis_engine import synthesize_answer

from test_deterministic_decisions import (
    debate55_verification_result,
    debate55_governance_context,
)


def _consistent_narrative() -> str:
    """A narrative that HONOURS every debate-55 verified winner and never
    recommends a rejected option."""
    return (
        "# Architecture\n"
        "Adopt a hybrid compute-leased architecture. Deploy server-side Docker "
        "workers behind Cloudflare Workers as the event-driven edge control "
        "plane. Choose server-side SQLite for the authoritative store, with a "
        "PostgreSQL migration path. Cost model is BYOK. Git-anchored "
        "server-side storage is the source of truth; we reject IndexedDB, "
        "WebContainers, WebAssembly, CRDTs (Y.js/Automerge), FastAPI, and the "
        "Single VM monolith.\n"
        "# Implementation Plan\n"
        "Build the control plane on Cloudflare Workers and the execution "
        "layer on server-side Docker. Store records in server-side SQLite.\n"
        "# Scaling Strategy\n"
        "Scale execution workers horizontally; migrate SQLite to PostgreSQL "
        "under load.\n"
        "# Operational Considerations\n"
        "BYOK keeps inference cost with the user; observe Docker workers.\n"
        "# Migration Path\n"
        "SQLite MVP, then PostgreSQL migration as data grows.\n"
        "# Risks\n"
        "WebContainers and client-side CRDTs are non-starters for the "
        "authoritative path; IndexedDB is off-limits for the system of record."
    )


def _contradictory_narrative() -> str:
    """A narrative that RECOMMENDS verified-rejected options."""
    return (
        "# Architecture\n"
        "Use WebContainers for the authoritative execution path. We should "
        "use WebAssembly too. Adopt IndexedDB as the primary store and "
        "recommend FastAPI as the always-on backend. Use client-side CRDTs "
        "(Y.js) and AutoMerge for state.\n"
        "# Implementation Plan\n"
        "# Scaling Strategy\n"
        "# Operational Considerations\n"
        "# Migration Path\n"
        "# Risks\n"
    )


# --------------------------------------------------------------------------- #
# TEST 4 — full synthesized answer is consistent with verification_result      #
# --------------------------------------------------------------------------- #


def test_synthesized_answer_has_no_contradiction() -> None:
    """TEST 4 (passing side): given debate 55 and a consistent narrative, the
    synthesized answer has no contradiction with verification_result."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()

    answer = synthesize_answer(
        question="Design the lowest-cost autonomous multi-agent platform",
        gpt_final="gpt position",
        gemini_final="gemini position",
        validated_disputes=[],
        consensus_score=99,
        verification_result=vr,
        governance_context=gc,
        narrative_fn=lambda _prompt: _consistent_narrative(),
    )

    # The three deterministic sections are present and authoritative.
    assert "# Verified Decisions" in answer
    assert "Decision Count: 7" in answer
    assert "# Architecture" in answer  # the LLM narrative follows

    contradictions = validate_synthesis_consistency(answer, vr, gc)
    assert contradictions == [], f"unexpected contradictions: {contradictions}"

    # No warning is appended for a consistent answer.
    assert "Contradiction Warning" not in answer


def test_synthesized_answer_flags_recommended_rejected_option() -> None:
    """TEST 4 (failing side): when the narrative recommends an option the
    verifier rejected, a contradiction is flagged and a warning appended. The
    deterministic Verified Decisions section stands (the verifier wins)."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()

    answer = synthesize_answer(
        question="Design the lowest-cost autonomous multi-agent platform",
        gpt_final="gpt position",
        gemini_final="gemini position",
        validated_disputes=[],
        consensus_score=99,
        verification_result=vr,
        governance_context=gc,
        narrative_fn=lambda _prompt: _contradictory_narrative(),
    )

    # The verifier's decisions are STILL present and authoritative.
    assert "# Verified Decisions" in answer
    assert "Decision Count: 7" in answer

    # A contradiction warning is surfaced.
    assert "Contradiction Warning" in answer
    contradictions = validate_synthesis_consistency(answer, vr, gc)
    caught = {(c["type"], c["normalized_key"], c["option"]) for c in contradictions}
    # At minimum: WebContainers + IndexedDB + FastAPI + CRDT recommended.
    assert any("webcontainers" in o for _, _, o in caught)
    assert any("indexeddb" in o for _, _, o in caught)
    assert any("fastapi" in o for _, _, o in caught)
    assert any(("crdt" in o) for _, _, o in caught)


# --------------------------------------------------------------------------- #
# Direct unit checks of the consistency guard (no LLM, no synthesis)           #
# --------------------------------------------------------------------------- #


def test_consistency_clean_deterministic_section() -> None:
    """The deterministic Verified Decisions section alone never contradicts
    the verifier that produced it (it is rendered straight from it)."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    section = render_verified_decisions(vr, gc)
    assert validate_synthesis_consistency(section, vr, gc) == []


def test_consistency_flags_each_rejected_recommendation() -> None:
    """A narrative recommending each rejected option is flagged once per
    option — WebContainers, WebAssembly, IndexedDB, FastAPI, CRDTs."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    section = render_verified_decisions(vr, gc)
    narrative = (
        "# Architecture\n"
        "Use WebContainers and WebAssembly for execution. "
        "Adopt IndexedDB. Recommend FastAPI. Use CRDTs (Y.js/Automerge) for state.\n"
    )
    ans = section + "\n\n" + narrative
    cs = validate_synthesis_consistency(ans, vr, gc)
    opts = {c["option"] for c in cs}
    for token in ("webcontainers", "webassembly", "indexeddb", "fastapi", "crdts"):
        assert token in opts, f"{token} not flagged: {opts}"
    assert contradiction_warning(cs)


def test_consistency_passes_when_rejected_options_only_rejected() -> None:
    """A narrative that only REJECTS the rejected options (never recommends)
    is consistent — the negate context must not be mistaken for a recommend."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    section = render_verified_decisions(vr, gc)
    narrative = (
        "# Architecture\n"
        "Server-side Docker, SQLite, Cloudflare Workers, BYOK. "
        "We reject IndexedDB, avoid WebContainers, do not use CRDTs, "
        "FastAPI is off-limits, and the Single VM monolith is abandoned.\n"
    )
    ans = section + "\n\n" + narrative
    assert validate_synthesis_consistency(ans, vr, gc) == []


def test_consistency_no_contradiction_for_empty_narrative() -> None:
    """An empty narrative cannot contradict anything."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    assert validate_synthesis_consistency("", vr, gc) == []
    section = render_verified_decisions(vr, gc)
    assert validate_synthesis_consistency(section, vr, gc) == []


def test_synthesize_answer_is_narrative_only_via_seam() -> None:
    """FIX 5: the LLM is demoted to narrative-only. With the determinism seam,
    the returned answer starts with the deterministic sections and the supplied
    narrative follows — nothing else is regenerated."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    answer = synthesize_answer(
        question="q", gpt_final="g", gemini_final="m",
        validated_disputes=[], consensus_score=99,
        verification_result=vr, governance_context=gc,
        narrative_fn=lambda _p: "# Architecture\nhonoured.\n",
    )
    assert answer.startswith("# Verified Decisions")
    assert "# Architecture" in answer
    assert answer.count("# Verified Decisions") == 1
    assert answer.count("Decision Count: 7") == 1


if __name__ == "__main__":
    test_synthesized_answer_has_no_contradiction()
    test_synthesized_answer_flags_recommended_rejected_option()
    test_consistency_clean_deterministic_section()
    test_consistency_flags_each_rejected_recommendation()
    test_consistency_passes_when_rejected_options_only_rejected()
    test_consistency_no_contradiction_for_empty_narrative()
    test_synthesize_answer_is_narrative_only_via_seam()
    print("test_synthesis_consistency: ALL PASSED")
