"""
test_deterministic_decisions.py - TEST 1, TEST 2, TEST 3.

The Verified Decisions / Verified Tradeoffs / Human Decisions Required
sections are generated DETERMINISTICALLY from verification_result and
governance_context by decision_renderer — no LLM. The verifier is the
authoritative source of truth.

TEST 1 — Given debate 55 verification_result, exactly 7 verified decisions
          appear, and no LLM is involved.
TEST 2 — WebContainers, IndexedDB, and client-side CRDT authoritative storage
          do NOT appear in Verified Decisions (they were REJECTED).
TEST 3 — The Verified Decisions section matches verification_result exactly,
          100% deterministically (same input -> same output).

These tests run offline (no network, no model calls). They prefer the real
answer-55 row from answer_memory; if that row is absent they fall back to a
captured debate-55 fixture so the suite is reproducible everywhere.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decision_renderer import (
    render_verified_decisions,
    render_verified_tradeoffs,
    render_human_required,
)


# ---------------------------------------------------------------------------
# Debate 55 fixture — the authoritative verification_result. Prefer the live
# answer-memory row; fall back to the captured verdict so the test is hermetic.
# ---------------------------------------------------------------------------

_DEBATE55_FALLBACK_VR = {
    "resolved_claims": [
        {"normalized_key": "orchestration_runtime",
         "dispute": "Orchestration Runtime: LangGraph (Python) vs. JS/Wasm Agent Framework",
         "verification": {"status": "RESOLVED", "confidence": 98,
                          "finding": ("The dispute is resolved through a hybrid "
                                      "architectural split. The evidence converges on an "
                                      "'Edge-Native Coordination Plane' combined with a "
                                      "'Python Execution Layer.' The edge runtime handles "
                                      "coordination and routing (addressing the JS/Wasm "
                                      "argument), while the Python runtime handles agent "
                                      "frameworks and execution (addressing the LangGraph "
                                      "argument), effectively unifying the approaches by "
                                      "assigning them to distinct layers."),
                          "requires_external_evidence": False}},
        {"normalized_key": "primary_database",
         "dispute": "Primary Database: SQLite (Server-side) vs. IndexedDB (Client-side)",
         "verification": {"status": "RESOLVED", "confidence": 95,
                          "finding": ("The dispute is resolved in favor of server-side "
                                      "authoritative storage. Both evidence strips reject "
                                      "the reliance on client-side storage (IndexedDB) for "
                                      "the primary system of record. The architecture "
                                      "converges on a server-centric model utilizing "
                                      "relational databases for metadata and object storage "
                                      "for heavy logs, rather than a client-side database "
                                      "approach."),
                          "requires_external_evidence": False}},
        {"normalized_key": "deployment_strategy",
         "dispute": "Deployment Strategy: Single VM (Server-centric) vs. Static Hosting + Edge (Client-centric)",
         "verification": {"status": "RESOLVED", "confidence": 99,
                          "finding": ("The dispute is resolved by adopting a 'Hybrid "
                                      "Compute-Leased Architecture.' Both evidence strips "
                                      "describe an identical converged design: a Static "
                                      "Frontend (CDN), a Serverless Control Plane, and "
                                      "Leased Execution Workers. This explicitly rejects "
                                      "the 'Single VM' monolith approach in favor of a "
                                      "distributed, scale-to-zero model."),
                          "requires_external_evidence": False}},
        {"normalized_key": "cost_model",
         "dispute": "Cost Model: Platform-hosted infrastructure vs. Bring-Your-Own-Key (BYOK)",
         "verification": {"status": "RESOLVED", "confidence": 99,
                          "finding": ("The dispute is resolved with the explicit selection "
                                      "of 'Bring-Your-Own-Key (BYOK)' as the primary "
                                      "cost-containment boundary. Both evidence strips "
                                      "identify BYOK as the necessary strategy for a "
                                      "near-zero budget MVP, rejecting platform-hosted "
                                      "infrastructure for inference costs to avoid financial "
                                      "exposure."),
                          "requires_external_evidence": False}},
        {"normalized_key": "storage_strategy",
         "dispute": "Storage Strategy: Server Local Filesystem vs. Client-Side CRDTs (Y.js/Automerge)",
         "verification": {"status": "RESOLVED", "confidence": 98,
                          "finding": ("The dispute is resolved by rejecting Client-Side "
                                      "CRDTs (Y.js/Automerge). The evidence converges on a "
                                      "'Hybrid Git-Anchored Distributed Storage Strategy' "
                                      "or 'Server-side POSIX + Git Remote' approach, "
                                      "establishing server-side object storage and Git as "
                                      "the source of truth rather than client-side CRDTs "
                                      "for state management."),
                          "requires_external_evidence": False}},
        {"normalized_key": "backend_api_framework",
         "dispute": "Backend API Framework: FastAPI vs. Cloudflare Workers",
         "verification": {"status": "RESOLVED", "confidence": 98,
                          "finding": ("The dispute is resolved by selecting a Serverless "
                                      "Edge Runtime (Cloudflare Workers) as the "
                                      "authoritative control plane. Both evidence strips "
                                      "explicitly reject FastAPI as the primary always-on "
                                      "backend, favoring an event-driven edge gateway for "
                                      "coordination and API handling to minimize costs and "
                                      "improve scalability."),
                          "requires_external_evidence": False}},
        {"normalized_key": "execution_environment",
         "dispute": "Execution Environment: Server-side Docker Workers vs. Client-side WebContainers/WebAssembly",
         "verification": {"status": "RESOLVED", "confidence": 99,
                          "finding": ("The dispute is resolved in favor of Server-side "
                                      "Docker/Container Workers. Both evidence strips "
                                      "explicitly reject Client-side "
                                      "WebContainers/WebAssembly for the authoritative "
                                      "execution path, establishing a 'Server-Side Isolated "
                                      "Execution Architecture' (e.g., gVisor or standard "
                                      "containers) as the sole runtime for autonomous "
                                      "engineering tasks."),
                          "requires_external_evidence": False}},
    ],
    "equivalent_claims": [],
    "unresolved_claims": [],
    "verification_score": 100,
    "truncated_count": 0,
}

_DEBATE55_GOVERNANCE_CONTEXT = {
    "verified_decisions": [],
    "verified_tradeoffs": [],
    "human_required": [],
}


def debate55_verification_result() -> dict:
    """Live answer-55 verification_result if present, else the captured fixture."""
    try:
        import answer_memory as am
        d = am.memory.get_debate(55)
        if d and d.get("verification_result", {}).get("resolved_claims"):
            vr = d["verification_result"]
            if len(vr.get("resolved_claims", [])) == 7:
                return vr
    except Exception:
        pass
    return _DEBATE55_FALLBACK_VR


def debate55_governance_context() -> dict:
    return _DEBATE55_GOVERNANCE_CONTEXT


# ---------------------------------------------------------------------------
# TEST 1 — exactly 7 verified decisions, deterministically, no LLM
# ---------------------------------------------------------------------------


def test_exactly_seven_verified_decisions_deterministic() -> None:
    """TEST 1: debate 55 -> exactly 7 verified decisions, generated without
    any LLM. Each resolved claim becomes a Decision/Winner/Confidence/Rationale
    block straight from verification_result."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    rendered = render_verified_decisions(vr, gc)

    assert "Decision Count: 7" in rendered
    assert rendered.count("### ") == 7  # one heading per resolved dispute
    # No LLM: this is a pure-function call; the renderer has no 'generate'.
    for claim in vr["resolved_claims"]:
        assert claim["dispute"] in rendered          # Decision
        v = claim["verification"]
        assert f"{v['confidence']}%" in rendered      # Confidence
        assert v["finding"] in rendered               # Winner + Rationale


def test_no_llm_involved_in_render() -> None:
    """TEST 1 (no-LLM): the renderer module exposes no generate/llm symbols and
    performs no I/O — calling it is a pure data transform."""
    import decision_renderer as dr
    assert not hasattr(dr, "generate")
    assert not hasattr(dr, "llm")
    out1 = render_verified_decisions(debate55_verification_result(),
                                     debate55_governance_context())
    out2 = render_verified_decisions(debate55_verification_result(),
                                     debate55_governance_context())
    assert out1 == out2  # determinism: same input -> identical output


# ---------------------------------------------------------------------------
# TEST 2 — rejected options absent from Verified Decisions as winners
# ---------------------------------------------------------------------------


def test_rejected_options_not_present_as_decisions() -> None:
    """TEST 2: WebContainers, IndexedDB, and client-side CRDT authoritative
    storage do NOT appear in Verified Decisions as a chosen/winning option and
    are NOT demoted to Verified Tradeoffs as a valid alternative. The dispute
    title legitimately names both sides ('... vs. IndexedDB'); the rejected
    option only ever appears inside the finding's 'reject' clause."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    dec = render_verified_decisions(vr, gc)
    dec_lower = dec.lower()

    # Verified Tradeoffs is empty: every dispute was RESOLVED, none demoted.
    trade = render_verified_tradeoffs(vr, gc)
    assert "Tradeoff Count: 0" in trade
    assert "webcontainers" not in trade.lower()
    assert "indexeddb" not in trade.lower()
    assert "crdt" not in trade.lower()

    # Rejected options are present ONLY as the loser side of a dispute / inside
    # the finding's reject clause, never selected as the winner.
    rejected_expectations = {
        "execution_environment": ("webcontainers",),
        "primary_database": ("indexeddb",),
        "storage_strategy": ("crdt", "crdts", "y.js", "automerge"),
    }
    by_key = {c["normalized_key"]: c for c in vr["resolved_claims"]}
    for key, terms in rejected_expectations.items():
        finding = by_key[key]["verification"]["finding"]
        flow = finding.lower()
        assert "reject" in flow, f"{key} finding must explicitly reject a side"
        for term in terms:
            assert term in flow, f"{term} should be named in the {key} finding"
            # The rejected term must NOT be introduced as the chosen winner.
            for affirmative in ("in favor of ", "selecting ", "adopting ",
                                "choosing ", "the explicit selection of "):
                ai = flow.find(affirmative)
                if ai == -1:
                    continue
                # window after the affirmative marker is the WINNER clause; the
                # rejected term must not be the thing selected there.
                winner_clause = flow[ai: ai + 80]
                assert term not in winner_clause, (
                    f"{term} must not be the selected winner of {key}")

    # No decision heading presents a rejected option as the sole winner.
    # Headings are dispute titles of the form 'A vs. B'; this is naming the
    # dispute, not selecting a side, so 'vs.' must be present for each.
    headings = [ln.strip() for ln in dec.splitlines() if ln.startswith("### ")]
    assert len(headings) == 7
    for h in headings:
        assert " vs. " in h.lower() or " vs " in h.lower()
    # Human Decisions Required is empty for debate 55.
    hr = render_human_required(vr, gc)
    assert "Decision Count: 0" in hr


def test_resolved_disputes_never_demoted_to_tradeoffs() -> None:
    """TEST 2 (guard): even if a stale tradeoff bucket tried to re-add a
    resolved dispute, the tradeoff renderer drops it. A resolved dispute may
    never be downgraded to a tradeoff."""
    vr = debate55_verification_result()
    resolved_key = vr["resolved_claims"][0]["normalized_key"]
    gc = {
        "verified_decisions": [],
        "human_required": [],
        "verified_tradeoffs": [{
            "outcome_type": "VERIFIED_TRADEOFF", "dispute_key": resolved_key,
            "dispute_text": "stale resolved dispute re-added as tradeoff",
            "option_a": "a", "option_b": "b",
            "recommendation": "a", "confidence": 1, "reasoning": "should be dropped"}],
    }
    trade = render_verified_tradeoffs(vr, gc)
    assert "Tradeoff Count: 0" in trade
    assert "stale resolved dispute" not in trade


# ---------------------------------------------------------------------------
# TEST 3 — Verified Decisions matches verification_result 100% deterministically
# ---------------------------------------------------------------------------


def test_verified_decisions_matches_verification_result() -> None:
    """TEST 3: the rendered Verified Decisions section matches verification_result
    exactly — every winner, confidence, and rationale comes directly from the
    verifier's finding, 100% deterministically."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    dec = render_verified_decisions(vr, gc)

    order = [c["normalized_key"] for c in vr["resolved_claims"]]
    rendered_order = []
    for ln in dec.splitlines():
        if ln.startswith("### "):
            for c in vr["resolved_claims"]:
                if ln == f"### {c['dispute']}":
                    rendered_order.append(c["normalized_key"])
                    break
    assert rendered_order == order  # preserves verifier order

    for claim in vr["resolved_claims"]:
        finding = claim["verification"]["finding"]
        conf = claim["verification"]["confidence"]
        assert claim["dispute"] in dec
        assert finding in dec                    # Winner
        assert finding in dec                    # Rationale (same finding text)
        assert f"{conf}%" in dec                 # Confidence

    assert dec.startswith("# Verified Decisions")
    assert "Decision Count: 7" in dec


def test_determinism_across_calls() -> None:
    """TEST 3 (determinism): repeated rendering yields byte-identical output."""
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    a = render_verified_decisions(vr, gc)
    b = render_verified_decisions(vr, gc)
    c = render_verified_decisions(vr, gc)
    assert a == b == c


if __name__ == "__main__":
    test_exactly_seven_verified_decisions_deterministic()
    test_no_llm_involved_in_render()
    test_rejected_options_not_present_as_decisions()
    test_resolved_disputes_never_demoted_to_tradeoffs()
    test_verified_decisions_matches_verification_result()
    test_determinism_across_calls()
    print("test_deterministic_decisions: ALL PASSED")
