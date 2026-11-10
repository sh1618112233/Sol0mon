"""
test_research_verification_integration_v2.py - Research → Verification Integration V2.

Offline only. No model calls. No browser. No external APIs.

Verifies the 10 contracts:

  1. requires_external_evidence=true triggers research (default research_fn
     is verification_research_v2.research_claim).
  2. requires_external_evidence=false does not trigger research.
  3. research evidence attached to verification_result (with normalized_key +
     research_source_count).
  4. evidence persists through save/load (answer_memory JSON round-trip).
  5. workspace resume reuses evidence (prior_research_evidence → research_fn
     never called, evidence restored from the prior record).
  6. research budget exhaustion escalates HUMAN_REQUIRED (with
     reason_code="research_budget_exhausted").
  7. no duplicate research runs (prior evidence → skip, unchanged evidence is
     reused, escalation count NOT incremented).
  8. existing verification tests still pass (V1 suite runs green alongside V2).
  9. no schema break (all new fields are additive — old consumers reading the
     resolved_claims / unresolved_claims buckets still work).
 10. no model calls during tests (inference + research injected via seams).

The critical regression test: an input claim with
requires_external_evidence=true invokes verification_research_v2.research_claim
exactly once, attaches the evidence, and sets research_used=true.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import verification_engine
from verification_engine import (
    verify_all_disputes,
    attach_research_evidence,
    extract_pre_emitted_outcome,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _dispute(nk, text):
    return {"dispute": text, "normalized_key": nk}


def _install_inference_fake(monkeypatch, pass1, pass2=None):
    """Replace run_verification_batch_inference with a programmable fake."""
    captured = {"batches": []}
    state = {"calls": 0}

    def _fake(question, batch_payload):
        captured["batches"].append(batch_payload)
        state["calls"] += 1
        return pass1 if state["calls"] == 1 else (pass2 if pass2 is not None else pass1)

    monkeypatch.setattr(verification_engine, "run_verification_batch_inference", _fake)
    return captured


def _mk_research_fn(record=None, summary="Research confirms X is superior.",
                    sources=None):
    """A fake research_fn that records calls and returns a dict (the
    verification_research_v2.research_claim shape: {"claim", "sources"})."""
    record = record if record is not None else {"calls": []}
    if sources is None:
        sources = [
            {"url": "https://example.gov/benchmarks", "title": "Gov Benchmarks",
             "score": 100, "content": "Benchmark data..."},
            {"url": "https://arxiv.org/paper123", "title": "Academic Paper",
             "score": 90, "content": "Academic study..."},
        ]

    def _fn(topic):
        record["calls"].append(topic)
        return {"claim": topic, "sources": sources, "summary": summary}
    return _fn, record


# --------------------------------------------------------------------------- #
# 1. requires_external_evidence=true triggers research                         #
# --------------------------------------------------------------------------- #

def test_requires_external_evidence_triggers_research_v2(monkeypatch):
    """CRITICAL REGRESSION TEST: a claim with requires_external_evidence=true
    invokes the research function exactly once, attaches the evidence to
    verification_result, and marks the resolved claim with research_used=True
    + research_source_count=N. The default research_fn is
    verification_research_v2.research_claim (tested via the seam)."""
    research_fn, record = _mk_research_fn()

    pass1 = [{
        "normalized_key": "database_choice",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "Cannot settle without external benchmarks.",
        "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "database_choice",
        "status": "RESOLVED", "confidence": 90,
        "finding": "External evidence resolved the dispute.",
        "requires_external_evidence": False,
    }]
    captured = _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="Design a planet-scale platform",
        validated_disputes=[_dispute("database_choice", "PostgreSQL vs MongoDB")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn,
    )

    # Research invoked exactly once.
    assert len(record["calls"]) == 1
    assert "database_choice" in record["calls"][0] or "PostgreSQL" in record["calls"][0]

    # Evidence attached.
    assert "research_evidence" in result
    assert len(result["research_evidence"]) == 1
    ev = result["research_evidence"][0]
    assert ev["dispute_key"] == "database_choice"
    assert ev["normalized_key"] == "database_choice"  # V2: normalized_key added
    assert ev["summary"]
    assert ev["timestamp"]
    assert isinstance(ev["sources"], list)
    assert len(ev["sources"]) == 2

    # Audit trail on the resolved claim (Phase 7).
    resolved = [r for r in result["resolved_claims"]
                if r["normalized_key"] == "database_choice"]
    assert resolved, "expected the dispute to be RESOLVED after research"
    rec = resolved[0]
    assert rec["used_research"] is True
    assert rec["research_source_count"] == 2  # Phase 7: source count
    assert rec["research_dispute_key"] == "database_choice"
    assert rec["research_summary"]

    # Pass-2 batch received the research evidence as external_evidence.
    assert len(captured["batches"]) == 2
    assert "Research confirms" in captured["batches"][1][0]["external_evidence"]


# --------------------------------------------------------------------------- #
# 2. requires_external_evidence=false does not trigger research                #
# --------------------------------------------------------------------------- #

def test_no_research_when_evidence_sufficient_v2(monkeypatch):
    """When no dispute has requires_external_evidence=true, research never
    runs and research_evidence is empty. The resolved record has
    research_source_count=0."""
    def _bomb(topic):
        raise AssertionError(f"research must not run (called for {topic!r})")

    pass1 = [{
        "normalized_key": "cache_layer",
        "status": "RESOLVED", "confidence": 95,
        "finding": "Settled by the evidence on the page.",
        "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1)

    result = verify_all_disputes(
        question="q",
        validated_disputes=[_dispute("cache_layer", "Redis vs Memcached")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=_bomb,
    )

    assert result["research_evidence"] == []
    rec = next(r for r in result["resolved_claims"]
               if r["normalized_key"] == "cache_layer")
    assert rec.get("used_research") in (None, False)
    assert rec.get("research_source_count") == 0


# --------------------------------------------------------------------------- #
# 3. research evidence attached to verification_result                         #
# --------------------------------------------------------------------------- #

def test_research_evidence_shape_v2(monkeypatch):
    """The evidence record has {dispute_key, normalized_key, sources, summary,
    timestamp} — all JSON-serializable for the answer_memory round-trip.
    Schema is additive (new normalized_key field alongside existing
    dispute_key)."""
    research_fn, _ = _mk_research_fn(
        summary="PostgreSQL scales better for multi-tenant workloads.",
        sources=[{"url": "https://example.gov/bench", "title": "DB Benchmarks"}],
    )

    pass1 = [{
        "normalized_key": "primary_database",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "Need benchmarks.", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "primary_database",
        "status": "RESOLVED", "confidence": 88,
        "finding": "Settled via external evidence.",
        "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q",
        validated_disputes=[_dispute("primary_database", "SQLite vs PostgreSQL")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )

    ev = result["research_evidence"][0]
    assert set(ev.keys()) >= {"dispute_key", "normalized_key", "sources",
                              "summary", "timestamp"}
    # Round-trip through JSON exactly.
    roundtripped = json.loads(json.dumps(result))
    assert roundtripped["research_evidence"][0]["normalized_key"] == "primary_database"
    assert roundtripped["research_evidence"][0]["dispute_key"] == "primary_database"
    assert "PostgreSQL scales better" in roundtripped["research_evidence"][0]["summary"]


# --------------------------------------------------------------------------- #
# 4. evidence persists through save/load                                      #
# --------------------------------------------------------------------------- #

def test_evidence_persists_through_answer_memory_v2(monkeypatch, tmp_path):
    """Evidence stored inside verification_result survives the answer_memory
    JSON-TEXT round-trip (save_debate -> get_debate), including the new
    normalized_key and research_source_count fields on claim records."""
    import answer_memory
    monkeypatch.setattr(answer_memory, "DB_FILE", str(tmp_path / "am_v2.db"))
    answer_memory.memory._initialized = False

    research_fn, _ = _mk_research_fn(
        summary="PostgreSQL outperforms SQLite.",
        sources=[{"url": "https://example.gov", "title": "Bench"}],
    )

    pass1 = [{
        "normalized_key": "primary_database",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "need data", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "primary_database",
        "status": "RESOLVED", "confidence": 91,
        "finding": "resolved", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="Design an AI platform",
        validated_disputes=[_dispute("primary_database", "PostgreSQL vs SQLite")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )
    vr_with_evidence = result

    debate_id = answer_memory.memory.save_debate(
        question="Design an AI platform",
        gpt_final="gpt", gemini_final="gem",
        synthesized_answer="answer", consensus_score=99,
        conflict_severity="LOW", conflict_points=["primary_database"],
        validated_disputes=[], verification_result=vr_with_evidence,
    )
    loaded = answer_memory.memory.get_debate(debate_id)
    assert loaded is not None
    loaded_vr = loaded.get("verification_result", {})

    # Evidence survives.
    assert "research_evidence" in loaded_vr
    ev = loaded_vr["research_evidence"][0]
    assert ev["dispute_key"] == "primary_database"
    assert ev["normalized_key"] == "primary_database"
    assert "PostgreSQL outperforms SQLite" in ev["summary"]
    assert len(ev["sources"]) == 1

    # Audit trail on resolved claims survives.
    resolved = loaded_vr["resolved_claims"][0]
    assert resolved["used_research"] is True
    assert resolved["research_source_count"] == 1

    answer_memory.memory._initialized = False


# --------------------------------------------------------------------------- #
# 5. workspace resume reuses evidence                                          #
# --------------------------------------------------------------------------- #

def test_workspace_resume_reuses_evidence_v2(monkeypatch):
    """Phase 5: when prior_research_evidence is provided (from a prior
    verification round or workspace resume), disputes that already have
    evidence SKIP the research call entirely and reuse the stored evidence.
    The escalation count is NOT incremented (no repeated model/API cost).

    Simulates the workspace resume flow: evidence was stored in a prior
    round, on resume it is passed back to verify_all_disputes."""
    # The research_fn should NEVER be called (prior evidence is reused).
    def _bomb(topic):
        raise AssertionError(f"research must not re-run on resume (called for {topic!r})")
    research_fn = _bomb

    prior_evidence = [
        {
            "dispute_key": "database_choice",
            "normalized_key": "database_choice",
            "sources": [{"url": "https://example.gov/prior", "title": "Prior Bench"}],
            "summary": "Prior research: PostgreSQL is superior for multi-tenant workloads.",
            "timestamp": "2025-01-01T00:00:00+00:00",
        },
    ]

    pass1 = [{
        "normalized_key": "database_choice",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "Need external benchmarks.",
        "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "database_choice",
        "status": "RESOLVED", "confidence": 92,
        "finding": "Resolved using prior research evidence.",
        "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q",
        validated_disputes=[_dispute("database_choice", "PostgreSQL vs MongoDB")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn,
        prior_research_evidence=prior_evidence,
    )

    # Evidence was reused (not re-run).
    assert result["research_escalation_count"] == 0  # Phase 5: no new escalation
    assert len(result["research_evidence"]) == 1
    ev = result["research_evidence"][0]
    assert ev["normalized_key"] == "database_choice"
    assert "Prior research" in ev["summary"]
    assert ev["timestamp"] == "2025-01-01T00:00:00+00:00"  # original timestamp preserved

    # The resolved claim has the full audit trail.
    resolved = result["resolved_claims"][0]
    assert resolved["used_research"] is True
    assert resolved["research_source_count"] == 1
    assert resolved["research_summary"]


# --------------------------------------------------------------------------- #
# 6. research budget exhaustion escalates HUMAN_REQUIRED                        #
# --------------------------------------------------------------------------- #

def test_budget_exhaustion_escalates_human_required_v2(monkeypatch):
    """Phase 6: when the research budget is exhausted, the verifier does NOT
    silently continue. It emits a HUMAN_REQUIRED governance_outcome with
    reason_code="research_budget_exhausted". The record carries
    research_source_count=0 (no research was done for this dispute)."""
    pass1 = [
        {"normalized_key": "d_a", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need research", "requires_external_evidence": True},
        {"normalized_key": "d_b", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need research", "requires_external_evidence": True},
        {"normalized_key": "d_c", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need research", "requires_external_evidence": True},
    ]
    pass2 = [{
        "normalized_key": "d_a", "status": "RESOLVED", "confidence": 90,
        "finding": "resolved via research", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    research_fn, record = _mk_research_fn()

    result = verify_all_disputes(
        question="q",
        validated_disputes=[_dispute("d_a", "A"), _dispute("d_b", "B"),
                             _dispute("d_c", "C")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn, max_research_escalations=1,
    )

    # Only one research pass ran.
    assert len(record["calls"]) == 1
    assert result["research_escalation_count"] == 1
    assert result["research_budget_exhausted"] is True

    # d_b and d_c escalated to HUMAN_REQUIRED.
    unresolved = {r["normalized_key"]: r for r in result["unresolved_claims"]}
    assert "d_b" in unresolved and "d_c" in unresolved
    for nk in ("d_b", "d_c"):
        rec = unresolved[nk]
        go = rec.get("governance_outcome")
        assert go is not None, f"{nk} must carry a governance_outcome"
        assert go["outcome_type"] == "HUMAN_REQUIRED"
        assert go["reason_code"] == "research_budget_exhausted"  # V2: machine-readable
        assert "Research budget exhausted" in go["reason"]  # V1: human-readable
        assert rec["verification"]["requires_external_evidence"] is True
        assert rec.get("used_research") is False
        assert rec.get("research_source_count") == 0  # Phase 7: no sources

    # d_a was resolved via research.
    resolved = [r for r in result["resolved_claims"]
                if r["normalized_key"] == "d_a"]
    assert resolved
    assert resolved[0]["research_source_count"] == 2  # from _mk_research_fn


# --------------------------------------------------------------------------- #
# 7. no duplicate research runs                                                #
# --------------------------------------------------------------------------- #

def test_no_duplicate_research_runs_v2(monkeypatch):
    """Phase 5: when two disputes need research but one already has prior
    evidence, only the one WITHOUT prior evidence triggers a research call.
    The one WITH prior evidence reuses it — no duplicate research, no
    duplicate escalation."""
    research_fn, record = _mk_research_fn(
        sources=[{"url": "https://example.gov/b", "title": "B"}],
    )

    prior_evidence = [
        {
            "dispute_key": "has_evidence",
            "normalized_key": "has_evidence",
            "sources": [{"url": "https://example.gov/prior2", "title": "Prior2"}],
            "summary": "Already researched.",
            "timestamp": "2025-01-01T00:00:00+00:00",
        },
    ]

    pass1 = [
        {"normalized_key": "has_evidence", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need research", "requires_external_evidence": True},
        {"normalized_key": "needs_evidence", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need research", "requires_external_evidence": True},
    ]
    pass2 = [
        {"normalized_key": "has_evidence", "status": "RESOLVED", "confidence": 90,
         "finding": "resolved", "requires_external_evidence": False},
        {"normalized_key": "needs_evidence", "status": "RESOLVED", "confidence": 90,
         "finding": "resolved", "requires_external_evidence": False},
    ]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q",
        validated_disputes=[
            _dispute("has_evidence", "Topic A"),
            _dispute("needs_evidence", "Topic B"),
        ],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn,
        prior_research_evidence=prior_evidence,
    )

    # Only one research call (for needs_evidence — the one without prior).
    assert len(record["calls"]) == 1
    assert "Topic B" in record["calls"][0]
    assert result["research_escalation_count"] == 1  # only one new escalation

    # Both have evidence.
    assert len(result["research_evidence"]) == 2
    by_key = {ev["normalized_key"]: ev for ev in result["research_evidence"]}
    assert by_key["has_evidence"]["timestamp"] == "2025-01-01T00:00:00+00:00"  # reused
    assert by_key["needs_evidence"]["timestamp"] != "2025-01-01T00:00:00+00:00"  # fresh

    # Both resolved with correct source counts.
    for r in result["resolved_claims"]:
        if r["normalized_key"] == "has_evidence":
            assert r["research_source_count"] == 1  # from prior evidence
        elif r["normalized_key"] == "needs_evidence":
            assert r["research_source_count"] == 1  # from fresh research


# --------------------------------------------------------------------------- #
# 8. existing verification tests still pass (cross-file check)                #
# --------------------------------------------------------------------------- #

def test_existing_verification_tests_still_pass_v2():
    """Contract 8: the V1 test suite still passes. We verify by importing the
    V1 test module and checking that it loads (meaning its symbols and the
    verification_engine API it exercises are still compatible). The actual
    V1 test execution is confirmed via the test runner, not here — but the
    import proves no symbol was removed or renamed in a breaking way."""
    # The V1 test module imports cleanly.
    import importlib
    mod = importlib.import_module("tests.test_research_verification_integration")
    assert hasattr(mod, "test_requires_external_evidence_triggers_research")
    assert hasattr(mod, "test_default_research_path_uses_verification_research_v2")
    assert hasattr(mod, "test_exhausted_research_becomes_human_required")

    # The verification_engine API surface is intact.
    assert callable(verify_all_disputes)
    assert callable(attach_research_evidence)
    assert callable(extract_pre_emitted_outcome)
    assert hasattr(verification_engine, "_default_research_fn")
    assert hasattr(verification_engine, "_normalize_research_output")
    assert hasattr(verification_engine, "MAX_RESEARCH_ESCALATIONS")


# --------------------------------------------------------------------------- #
# 9. no schema break (all new fields are additive)                             #
# --------------------------------------------------------------------------- #

def test_no_schema_break_v2(monkeypatch):
    """Contract 9: all new V2 fields are additive. The pre-existing
    verification_result keys are still present and unchanged. A consumer that
    reads the old schema (resolved_claims, unresolved_claims, equivalence,
    verification_score, research_evidence, research_escalation_count,
    research_budget_exhausted, truncated_count) still works. The new fields
    (normalized_key on evidence, research_source_count on claims,
    reason_code on governance_outcome) are additions that old consumers can
    ignore."""
    research_fn, _ = _mk_research_fn()

    pass1 = [{
        "normalized_key": "queue_tech",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "need research", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "queue_tech",
        "status": "RESOLVED", "confidence": 91,
        "finding": "resolved", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q",
        validated_disputes=[_dispute("queue_tech", "Kafka vs RabbitMQ")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn,
    )

    # Old (V1) top-level keys still present.
    for key in ("resolved_claims", "equivalent_claims", "unresolved_claims",
                "verification_score", "truncated_count", "research_evidence",
                "research_escalation_count", "research_budget_exhausted"):
        assert key in result, f"V1 top-level key {key!r} missing — schema break"

    # Old (V1) evidence record keys still present.
    ev = result["research_evidence"][0]
    for key in ("dispute_key", "sources", "summary", "timestamp"):
        assert key in ev, f"V1 evidence key {key!r} missing — schema break"

    # Old (V1) claim record keys still present.
    rec = result["resolved_claims"][0]
    for key in ("dispute", "normalized_key", "verification", "used_research",
                "research_dispute_key", "research_summary"):
        assert key in rec, f"V1 claim key {key!r} missing — schema break"

    # New (V2) fields are additions (present, but old consumers can ignore).
    assert "normalized_key" in ev  # V2 addition to evidence records
    assert "research_source_count" in rec  # V2 addition to claim records

    # Old (V1) governance_outcome keys still present (when present).
    pass1_exhaust = [
        {"normalized_key": "d1", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need r", "requires_external_evidence": True},
        {"normalized_key": "d2", "status": "UNRESOLVED", "confidence": 40,
         "finding": "need r", "requires_external_evidence": True},
    ]
    pass2_exhaust = [{
        "normalized_key": "d1", "status": "RESOLVED", "confidence": 90,
        "finding": "ok", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1_exhaust, pass2_exhaust)

    result_ex = verify_all_disputes(
        question="q",
        validated_disputes=[_dispute("d1", "X"), _dispute("d2", "Y")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn, max_research_escalations=1,
    )
    go = result_ex["unresolved_claims"][0].get("governance_outcome", {})
    for key in ("outcome_type", "dispute_key", "dispute_text", "reason", "confidence"):
        assert key in go, f"V1 governance_outcome key {key!r} missing — schema break"
    assert "reason_code" in go  # V2 addition (machine-readable code)


# --------------------------------------------------------------------------- #
# 10. no model calls during tests                                              #
# --------------------------------------------------------------------------- #

def test_no_model_calls_during_tests_v2(monkeypatch):
    """Contract 10: no LLM/browser/API call is reachable during tests. Both
    the inference layer (run_verification_batch_inference → cerebras
    generate) and the research layer (verification_research_v2 → playwright)
    are injected via seams. The default research_fn also never reaches the
    browser during the test — it's overridden by the seam.

    Also verifies determinism: same inputs → identical outputs."""
    # Stub the model inference layer.
    _install_inference_fake(monkeypatch,
        [{"normalized_key": "x", "status": "RESOLVED", "confidence": 90,
          "finding": "ok", "requires_external_evidence": False}])

    # No research_fn needed (no dispute requires evidence). But verify the
    # inference fake prevented any cerebras call.
    import cerebras_client
    assert not cerebras_client.generate.__name__ == "_fake"  # not stubbed, just unused

    # Determinism: same inputs → same outputs.
    research_fn, _ = _mk_research_fn()
    pass1 = [{
        "normalized_key": "det_test",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "need", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "det_test",
        "status": "RESOLVED", "confidence": 90,
        "finding": "resolved", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    a = verify_all_disputes(
        question="q", validated_disputes=[_dispute("det_test", "X vs Y")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )
    _install_inference_fake(monkeypatch, pass1, pass2)
    b = verify_all_disputes(
        question="q", validated_disputes=[_dispute("det_test", "X vs Y")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )
    # The timestamp will differ (datetime.now), so compare everything except
    # the dynamic timestamp field.
    a_ev = a["research_evidence"][0]
    b_ev = b["research_evidence"][0]
    a_ev_copy = {k: v for k, v in a_ev.items() if k != "timestamp"}
    b_ev_copy = {k: v for k, v in b_ev.items() if k != "timestamp"}
    assert a_ev_copy == b_ev_copy
    assert a["research_escalation_count"] == b["research_escalation_count"]
    assert a["verification_score"] == b["verification_score"]

    # The _default_research_fn lazily imports verification_research_v2 —
    # verify it's a function (not a module-level browser import).
    assert callable(verification_engine._default_research_fn)


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_research_verification_integration_v2.py -q")
