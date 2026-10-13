"""
test_research_verification_integration.py - Research-Driven Verification wiring.

Offline only. No model calls. No browser calls. No external APIs.

Verified contracts:
  1. requires_external_evidence triggers research.
  2. research evidence is persisted inside verification_result.
  3. workspace resume reloads evidence (SQLite round-trip via ws.resolved_disputes).
  4. verifier receives the research evidence in the pass-2 batch (external_evidence).
  5. research can change a dispute outcome (UNRESOLVED -> RESOLVED, used_research=True).
  6. exhausted research becomes HUMAN_REQUIRED (no silent continuation, no artificial
     certainty).
  7. no research runs when evidence is already sufficient.

The verifier's inference (which wraps a model) and the research entry point are
both injected via seams, so nothing reaches the network, a browser, or an LLM.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import verification_engine
from verification_engine import (
    verify_all_disputes,
    attach_research_evidence,
    extract_pre_emitted_outcome,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _dispute(nk, text):
    return {"dispute": text, "normalized_key": nk}


def _install_inference_fake(monkeypatch, pass1, pass2=None):
    """Replace run_verification_batch_inference with a programmable fake that
    returns `pass1` for the first call and `pass2` for the second (research
    re-evaluation) call. Captures every batch payload it sees."""
    captured = {"batches": []}
    state = {"calls": 0}

    def _fake(question, batch_payload):
        captured["batches"].append(batch_payload)
        state["calls"] += 1
        return pass1 if state["calls"] == 1 else (pass2 if pass2 is not None else pass1)

    monkeypatch.setattr(verification_engine, "run_verification_batch_inference", _fake)
    return captured


def _mk_research_fn(record=None, summary="Research confirms option X is superior."):
    """A fake research_fn that records its calls and returns a markdown string
    (the same shape research_runner.run_research returns in production)."""
    record = record if record is not None else {"calls": []}

    def _fn(topic):
        record["calls"].append(topic)
        return f"# Research: {topic}\n{summary}"
    return _fn, record


# --------------------------------------------------------------------------- #
# 1 + 7. requires_external_evidence triggers research; otherwise it never runs #
# --------------------------------------------------------------------------- #

def test_requires_external_evidence_triggers_research(monkeypatch):
    """A dispute flagged requires_external_evidence=True invokes the research
    function exactly once, and the returned verification_result carries the
    research evidence."""
    research_fn, record = _mk_research_fn()

    pass1 = [{
        "normalized_key": "primary_database",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "Cannot settle without external benchmarks.",
        "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "primary_database",
        "status": "RESOLVED", "confidence": 90,
        "finding": "External evidence resolved the dispute.",
        "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q", validated_disputes=[_dispute("primary_database", "SQLite vs PostgreSQL")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn,
    )

    assert len(record["calls"]) == 1
    assert "primary_database" in record["calls"][0] or "SQLite" in record["calls"][0]
    assert "research_evidence" in result
    assert len(result["research_evidence"]) == 1
    ev = result["research_evidence"][0]
    assert ev["dispute_key"] == "primary_database"
    assert ev["summary"]
    assert ev["timestamp"]
    assert isinstance(ev["sources"], list)


def test_no_research_runs_when_evidence_sufficient(monkeypatch):
    """If no dispute sets requires_external_evidence=True, research never runs
    and research_evidence is empty."""
    research_fn, record = _mk_research_fn()
    # If the engine ever calls research for a no-need dispute, blow up loudly.
    def _bomb(topic):
        raise AssertionError(f"research must not run when evidence is sufficient (called for {topic!r})")
    research_fn = _bomb

    pass1 = [{
        "normalized_key": "primary_database",
        "status": "RESOLVED", "confidence": 95,
        "finding": "Settled by the evidence on the page.",
        "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1)

    result = verify_all_disputes(
        question="q", validated_disputes=[_dispute("primary_database", "SQLite vs PostgreSQL")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn,
    )

    assert result["research_evidence"] == []
    # Resolved normally, no research audit trail.
    rec = next(r for r in result["resolved_claims"] if r["normalized_key"] == "primary_database")
    assert rec.get("used_research") in (None, False)


# --------------------------------------------------------------------------- #
# 2. research evidence is persisted inside verification_result               #
# --------------------------------------------------------------------------- #

def test_research_evidence_shape_is_persistable(monkeypatch):
    """The evidence record stored in verification_result has the exact required
    shape: {dispute_key, sources, summary, timestamp}. JSON-serializable so it
    survives the answer_memory JSON-TEXT column round-trip."""
    import json

    research_fn, _ = _mk_research_fn(summary="PostgreSQL scales better for multi-tenant workloads.")

    pass1 = [{
        "normalized_key": "primary_database",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "Need benchmarks.", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "primary_database",
        "status": "RESOLVED", "confidence": 88,
        "finding": "Settled via external evidence.", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q", validated_disputes=[_dispute("primary_database", "SQLite vs PostgreSQL")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )

    ev = result["research_evidence"][0]
    assert set(ev.keys()) >= {"dispute_key", "sources", "summary", "timestamp"}
    # Round-trip through JSON exactly (answer_memory stores verification_result as JSON TEXT).
    roundtripped = json.loads(json.dumps(result))
    assert roundtripped["research_evidence"][0]["dispute_key"] == "primary_database"
    assert "PostgreSQL scales better" in roundtripped["research_evidence"][0]["summary"]


# --------------------------------------------------------------------------- #
# 3. workspace resume reloads evidence                                        #
# --------------------------------------------------------------------------- #

def test_workspace_resume_reloads_evidence(monkeypatch, tmp_path):
    """Evidence attached to ws.resolved_disputes survives a save -> load
    round-trip through the existing workspace SQLite persistence (no new table,
    no new storage system). This is what makes 'workspace resume must retain
    evidence' true."""
    # Isolate the workspace DB to a tmp file.
    import workspace_manager as wm
    monkeypatch.setattr(wm, "DB_FILE", str(tmp_path / "ws_research.db"),
                        raising=False)
    # The module-level get_connection reads DB_FILE at call time, so the patch
    # takes effect. Reset the singleton's init flag so it re-initializes on the
    # new path.
    wm.workspace_mgr._initialized = False

    ws_id = wm.workspace_mgr.create_workspace("Design a planet-scale AI platform")

    evidence = {
        "dispute_key": "primary_database",
        "sources": [{"url": "https://example.gov/benchmarks", "title": "DB Benchmarks"}],
        "summary": "PostgreSQL outperforms SQLite under multi-tenant write load.",
        "timestamp": "2025-01-01T00:00:00+00:00",
    }
    dispute_dict = {
        "normalized_key": "primary_database",
        "decision": "Primary Database: PostgreSQL vs SQLite",
        "research_evidence": evidence,
    }

    ws = wm.workspace_mgr.load_workspace(ws_id)
    assert ws is not None
    ws.resolved_disputes = [dispute_dict]
    assert wm.workspace_mgr.save_workspace(ws)

    reloaded = wm.workspace_mgr.load_workspace(ws_id)
    assert reloaded is not None
    assert len(reloaded.resolved_disputes) == 1
    assert reloaded.resolved_disputes[0]["research_evidence"] == evidence


def test_attach_research_evidence_merges_by_normalized_key():
    """attach_research_evidence copies the matching evidence record onto each
    dispute dict (by normalized_key), and leaves disputes without research
    untouched. Pure function — no I/O."""
    disputes = [
        {"normalized_key": "primary_database", "decision": "PostgreSQL"},
        {"normalized_key": "execution", "decision": "Docker"},
    ]
    vr = {
        "research_evidence": [
            {"dispute_key": "primary_database", "sources": [], "summary": "s", "timestamp": "t"},
        ],
    }
    out = attach_research_evidence(disputes, vr)
    assert out[0]["research_evidence"]["dispute_key"] == "primary_database"
    assert "research_evidence" not in out[1]


# --------------------------------------------------------------------------- #
# 4. verifier receives the research evidence                                  #
# --------------------------------------------------------------------------- #

def test_verifier_receives_research_evidence_in_pass2(monkeypatch):
    """The second-pass inference batch (the research re-evaluation pass) must
    receive the research summary as the external_evidence field for that
    dispute, so the verifier can actually use the research."""
    research_fn, _ = _mk_research_fn(summary="BENCHMARK_SUMMARY_MARKER")

    pass1 = [{
        "normalized_key": "primary_database",
        "status": "UNRESOLVED", "confidence": 40,
        "finding": "Need external data.", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "primary_database",
        "status": "RESOLVED", "confidence": 91,
        "finding": "Resolved using external evidence.", "requires_external_evidence": False,
    }]
    captured = _install_inference_fake(monkeypatch, pass1, pass2)

    verify_all_disputes(
        question="q", validated_disputes=[_dispute("primary_database", "SQLite vs PostgreSQL")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )

    assert len(captured["batches"]) == 2
    pass2_batch = captured["batches"][1]
    assert len(pass2_batch) == 1
    item = pass2_batch[0]
    assert item["normalized_key"] == "primary_database"
    assert "BENCHMARK_SUMMARY_MARKER" in item["external_evidence"]


# --------------------------------------------------------------------------- #
# 5. research can change a dispute outcome                                     #
# --------------------------------------------------------------------------- #

def test_research_changes_dispute_outcome(monkeypatch):
    """Pass 1 returns UNRESOLVED + requires_external_evidence=True. After
    research, pass 2 returns RESOLVED. The final record must be RESOLVED with
    the audit-trail fields used_research=True / research_dispute_key /
    research_summary populated (Phase 5)."""
    research_fn, _ = _mk_research_fn(summary="Authoritative evidence settles it.")

    pass1 = [{
        "normalized_key": "cache_layer", "status": "UNRESOLVED", "confidence": 45,
        "finding": "Need external benchmarks.", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "cache_layer", "status": "RESOLVED", "confidence": 92,
        "finding": "External benchmarks resolve the dispute.", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    result = verify_all_disputes(
        question="q", validated_disputes=[_dispute("cache_layer", "Redis vs Memcached")],
        gpt_position="gpt", gemini_position="gemini", research_fn=research_fn,
    )

    resolved = [r for r in result["resolved_claims"] if r["normalized_key"] == "cache_layer"]
    assert resolved, "expected the dispute to be RESOLVED after research"
    rec = resolved[0]
    assert rec.get("used_research") is True
    assert rec.get("research_dispute_key") == "cache_layer"
    assert rec.get("research_summary")
    assert result["research_escalation_count"] == 1


# --------------------------------------------------------------------------- #
# 6. exhausted research becomes HUMAN_REQUIRED                                #
# --------------------------------------------------------------------------- #

def test_exhausted_research_becomes_human_required(monkeypatch):
    """When the research budget is exhausted before a dispute could be
    resolved, the verifier does NOT silently continue and invent certainty.
    It emits a HUMAN_RECORD carrying:
        governance_outcome.outcome_type == "HUMAN_REQUIRED"
        governance_outcome.reason mentions the exhausted research budget.
    """
    # Three disputes all require research, but the budget is 1.
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
        validated_disputes=[_dispute("d_a", "A"), _dispute("d_b", "B"), _dispute("d_c", "C")],
        gpt_position="gpt", gemini_position="gemini",
        research_fn=research_fn, max_research_escalations=1,
    )

    # Only one research pass ran (the budget).
    assert len(record["calls"]) == 1
    assert result["research_escalation_count"] == 1

    # d_b and d_c were NOT resolved and NOT silently dropped.
    unresolved = {r["normalized_key"]: r for r in result["unresolved_claims"]}
    assert "d_b" in unresolved and "d_c" in unresolved
    for nk in ("d_b", "d_c"):
        rec = unresolved[nk]
        go = rec.get("governance_outcome")
        assert go is not None, f"{nk} must carry a governance_outcome"
        assert go["outcome_type"] == "HUMAN_REQUIRED"
        assert "Research budget exhausted" in go["reason"]
        assert rec["verification"]["requires_external_evidence"] is True
        assert rec.get("used_research") is False


def test_extract_pre_emitted_outcue_pre_emitted(monkeypatch):
    """extract_pre_emitted_outcome returns the HUMAN_REQUIRED governance_outcome
    dict from a verification record so consumers can honor it WITHOUT a model
    call. Returns None when no pre-emitted outcome exists."""
    vr = {
        "unresolved_claims": [
            {"normalized_key": "d_x",
             "verification": {"status": "UNRESOLVED", "confidence": 0,
                               "finding": "exhausted"},
             "governance_outcome": {"outcome_type": "HUMAN_REQUIRED",
                                    "dispute_key": "d_x",
                                    "reason": "Research budget exhausted before dispute could be resolved."}},
        ],
        "resolved_claims": [],
    }
    pre = extract_pre_emitted_outcome(vr, "d_x")
    assert pre is not None
    assert pre["outcome_type"] == "HUMAN_REQUIRED"
    assert extract_pre_emitted_outcome(vr, "missing_key") is None
    assert extract_pre_emitted_outcome(None, "d_x") is None


# --------------------------------------------------------------------------- #
# Answer-persistence bonus: evidence survives answer_memory JSON round-trip   #
# --------------------------------------------------------------------------- #

def test_research_evidence_survives_answer_persistence(monkeypatch, tmp_path):
    """Evidence stored inside verification_result survives the answer_memory
    JSON-TEXT round-trip (save_debate -> get_debate). Confirms 'evidence
    survives answer persistence' and 'evidence survives answer chaining' (each
    debate row carries its own verification_result)."""
    import answer_memory
    monkeypatch.setattr(answer_memory, "DB_FILE", str(tmp_path / "am_research.db"))
    answer_memory.memory._initialized = False

    vr_with_evidence = {
        "resolved_claims": [],
        "unresolved_claims": [],
        "equivalent_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
        "research_evidence": [
            {"dispute_key": "primary_database",
             "sources": [{"url": "https://example.gov", "title": "Bench"}],
             "summary": "PostgreSQL outperforms SQLite.",
             "timestamp": "2025-01-01T00:00:00+00:00"},
        ],
    }

    debate_id = answer_memory.memory.save_debate(
        question="Design an AI platform",
        gpt_final="gpt", gemini_final="gemini",
        synthesized_answer="answer",
        consensus_score=99,
        conflict_severity="LOW",
        conflict_points=["primary_database"],
        validated_disputes=[],
        verification_result=vr_with_evidence,
    )
    loaded = answer_memory.memory.get_debate(debate_id)
    assert loaded is not None
    loaded_vr = loaded.get("verification_result", {})
    assert "research_evidence" in loaded_vr
    assert loaded_vr["research_evidence"][0]["dispute_key"] == "primary_database"
    assert "PostgreSQL outperforms SQLite" in loaded_vr["research_evidence"][0]["summary"]

    # Good-citizen cleanup: reset the lazy-init flag so later tests touching
    # answer_memory re-initialize on their own (restored) DB_FILE, rather than
    # skipping init on a stale schema (a pre-existing module-state hazard).
    answer_memory.memory._initialized = False


# --------------------------------------------------------------------------- #
# No model / browser / API call paths are reachable                            #
# --------------------------------------------------------------------------- #

def test_default_research_path_uses_verification_research_v2(monkeypatch):
    """When research_fn is None, the engine resolves to
    verification_research_v2.research_claim (the browser-based research module
    named by the V2 integration spec) — NOT research_runner.run_research. We
    assert this by faking verification_research_v2.research_claim and asserting
    it is the function called when research_fn=None; research_runner is never
    imported by the engine during this path."""
    import verification_research_v2

    called = {"vr_v2": 0}

    def _fake_research_claim(claim):
        called["vr_v2"] += 1
        return {"claim": claim, "sources": [{"url": "https://example.gov", "title": "Bench"}]}
    monkeypatch.setattr(verification_research_v2, "research_claim", _fake_research_claim)

    pass1 = [{
        "normalized_key": "queue_tech", "status": "UNRESOLVED", "confidence": 40,
        "finding": "need research", "requires_external_evidence": True,
    }]
    pass2 = [{
        "normalized_key": "queue_tech", "status": "RESOLVED", "confidence": 90,
        "finding": "resolved", "requires_external_evidence": False,
    }]
    _install_inference_fake(monkeypatch, pass1, pass2)

    # research_fn=None -> default path (verification_research_v2.research_claim).
    result = verify_all_disputes(
        question="q", validated_disputes=[_dispute("queue_tech", "Kafka vs RabbitMQ")],
        gpt_position="gpt", gemini_position="gemini", research_fn=None,
    )
    assert called["vr_v2"] == 1
    assert len(result["research_evidence"]) == 1
    assert result["research_evidence"][0]["normalized_key"] == "queue_tech"
    # The evidence record carries the sources list; the resolved claim
    # record carries research_source_count.
    assert len(result["research_evidence"][0]["sources"]) == 1
    resolved = result["resolved_claims"][0]
    assert resolved["research_source_count"] == 1


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_research_verification_integration.py -q")
