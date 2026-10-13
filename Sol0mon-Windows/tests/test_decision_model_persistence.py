"""
test_decision_model_persistence.py - TEST 1.

Verifies FIX 1 (answer_memory._extract_decisions bridges
verification_result -> resolved_decisions with winner/confidence/rationale).

Debate 55 shape: 7 resolved disputes survive into resolved_decisions with the
actual decision content (not the dispute title), and verification_result is
preserved verbatim.

Seeds a throwaway answer_memory.db (temp path) so the real DB is untouched.
No model calls, no browser.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import answer_memory
import answer_loader as al


def _with_tmp_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _debate55_verification_result():
    """The 7-resolved-dispute shape from real answer_memory.db debate 55."""
    disputes = [
        ("orchestration_runtime",
         "Orchestration Runtime: LangGraph (Python) vs. JS/Wasm Agent Framework",
         "Resolved through a hybrid split: Edge-Native Coordination Plane + Python Execution Layer.", 98),
        ("primary_database",
         "Primary Database: SQLite (Server-side) vs. IndexedDB (Client-side)",
         "Resolved in favor of server-side authoritative storage; rejects IndexedDB.", 95),
        ("deployment_strategy",
         "Deployment Strategy: Single VM vs. Static Hosting + Edge",
         "Resolved by Hybrid Compute-Leased Architecture; rejects Single VM.", 99),
        ("cost_model",
         "Cost Model: Platform-hosted infrastructure vs. BYOK",
         "Resolved with explicit selection of BYOK; rejects platform-hosted.", 99),
        ("storage_strategy",
         "Storage: Server Local Filesystem vs. Client-Side CRDTs",
         "Resolved by rejecting Client-Side CRDTs; server-side POSIX + Git.", 98),
        ("backend_api_framework",
         "Backend API Framework: FastAPI vs. Cloudflare Workers",
         "Resolved by selecting Cloudflare Workers; rejects FastAPI.", 98),
        ("execution_environment",
         "Execution: Server-side Docker Workers vs. Client-side WebContainers",
         "Resolved in favor of Server-side Docker Workers; rejects WebContainers.", 99),
    ]
    resolved_claims = []
    validated_disputes = []
    for nk, title, finding, conf in disputes:
        resolved_claims.append({
            "dispute": title,
            "normalized_key": nk,
            "verification": {
                "status": "RESOLVED",
                "confidence": conf,
                "finding": finding,
                "requires_external_evidence": False,
            },
        })
        validated_disputes.append({
            "normalized_key": nk,
            "dispute": title,
            "status": "resolved",
            "outcome_type": "",
            "governed_outcome": None,
        })
    return {
        "resolved_claims": resolved_claims,
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }, validated_disputes


def test_seven_resolved_disputes_survive_into_resolved_decisions() -> None:
    """TEST 1: all 7 resolved disputes survive into resolved_decisions with
    decision/winner/confidence/rationale populated from the verification finding."""
    db_path = _with_tmp_db()
    try:
        vr, vd = _debate55_verification_result()
        debate_id = answer_memory.memory.save_debate(
            question="Design lowest-cost architecture for 100k users",
            gpt_final="gpt_server", gemini_final="gemini_client",
            synthesized_answer="Final synthesized answer.",
            consensus_score=98,
            conflict_severity="LOW",
            conflict_points=[d["normalized_key"] for d in vd],
            validated_disputes=vd,
            verification_result=vr,
        )
        assert debate_id >= 1

        row = answer_memory.memory.get_debate(debate_id)
        resolved = row["resolved_decisions"]
        assert len(resolved) == 7, f"expected 7 decisions, got {len(resolved)}"

        keys = {d["normalized_key"] for d in resolved}
        expected = {
            "orchestration_runtime", "primary_database", "deployment_strategy",
            "cost_model", "storage_strategy", "backend_api_framework",
            "execution_environment",
        }
        assert keys == expected, f"keys mismatch: {keys ^ expected}"

        for d in resolved:
            # FIX 1 contract: decision/winner/confidence/rationale populated.
            assert d["status"] == "resolved"
            assert d["decision"], f"{d['normalized_key']} has empty decision"
            assert d["winner"], f"{d['normalized_key']} has empty winner"
            assert d["rationale"], f"{d['normalized_key']} has empty rationale"
            assert d["finding"], f"{d['normalized_key']} has empty finding"
            assert d["confidence"] > 0, f"{d['normalized_key']} zero confidence"
            # decision must NOT be the bare dispute title (the old bug).
            assert d["decision"] != d["dispute"], \
                f"{d['normalized_key']}: decision is still the dispute title"
            # the finding content survives into rationale.
            assert d["rationale"] == d["finding"]
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_verification_result_preserved_verbatim() -> None:
    """The authoritative source (verification_result) is preserved untouched."""
    db_path = _with_tmp_db()
    try:
        vr, vd = _debate55_verification_result()
        debate_id = answer_memory.memory.save_debate(
            question="q", gpt_final="g", gemini_final="m",
            synthesized_answer="a", consensus_score=98,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=vd, verification_result=vr,
        )
        row = answer_memory.memory.get_debate(debate_id)
        assert row["verification_result"]["verification_score"] == 100
        assert len(row["verification_result"]["resolved_claims"]) == 7
        # findings preserved
        for claim in row["verification_result"]["resolved_claims"]:
            assert claim["verification"]["finding"]
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_shape_exposes_decision_model() -> None:
    """answer_loader._shape exposes verification_result, resolved_decisions,
    and a Schema-A verified_decisions bucket bridged from verification."""
    db_path = _with_tmp_db()
    try:
        vr, vd = _debate55_verification_result()
        debate_id = answer_memory.memory.save_debate(
            question="Design Sol0mon architecture",
            gpt_final="g", gemini_final="m",
            synthesized_answer="syn body", consensus_score=98,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=vd, verification_result=vr,
        )
        out = al.get_answer_by_id(debate_id)
        assert out is not None
        assert "verification_result" in out
        assert "resolved_decisions" in out
        assert "verified_decisions" in out
        # Bridge: 7 resolved claims -> 7 verified_decisions (Schema-A shape).
        vdec = out["verified_decisions"]
        assert len(vdec) == 7
        for d in vdec:
            assert d["outcome_type"] == "VERIFIED_DECISION"
            assert d.get("dispute_key")
            assert d.get("decision")
            assert d.get("reasoning")
            assert d["decision"] == d["reasoning"]  # finding doubled as both
        assert len(out["resolved_decisions"]) == 7
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    test_seven_resolved_disputes_survive_into_resolved_decisions()
    test_verification_result_preserved_verbatim()
    test_shape_exposes_decision_model()
    print("test_decision_model_persistence: ALL PASSED")
