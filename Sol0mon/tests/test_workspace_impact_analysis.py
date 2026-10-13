"""
tests/test_workspace_impact_analysis.py — deterministic follow-up impact
analysis for workspace continuation.

Covers workspace_impact_analyzer.analyze_followup_impact() and its wiring
into WorkspaceDebateEngine.resume_workspace():

  A. Clarification — reopens nothing.
  B. Extension     — reopens affected only.
  C. Contradiction — reopens affected only.

Critical scenario: a workspace with database_choice, provider_routing,
deployment_model, storage_strategy; follow-up "Assume PostgreSQL cannot be
used." -> affected {database_choice, storage_strategy}; preserved
{provider_routing, deployment_model}; debate queue size = 2, NOT 4.

All tests are offline: no model, no browser, no API, no SQLite-required for
the analyzer unit tests; the integration test uses fakes + an in-memory
workspace.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workspace_impact_analyzer import (
    analyze_followup_impact,
    audit_entry,
)
from workspace_manager import Workspace, WorkspaceStatus
from debate_engine import Dispute

import workspace_debate_engine as wde


# ---------------------------------------------------------------------------
# Fixtures: the critical 4-decision workspace, as a plain dict and a Workspace.
# ---------------------------------------------------------------------------

_CRITICAL_DECISIONS = [
    {"normalized_key": "database_choice",
     "decision": "Use PostgreSQL as the primary database",
     "agreed_position": "PostgreSQL gives us ACID transactions",
     "priority": "HIGH", "priority_weight": 5},
    {"normalized_key": "provider_routing",
     "decision": "Route requests via an API gateway to model providers",
     "priority": "MEDIUM", "priority_weight": 3},
    {"normalized_key": "deployment_model",
     "decision": "Deploy on Kubernetes with horizontal pod autoscaling",
     "priority": "MEDIUM", "priority_weight": 3},
    {"normalized_key": "storage_strategy",
     "decision": "Use the database native replication for the storage tier",
     "agreed_position": "Rely on the database WAL replication",
     "priority": "HIGH", "priority_weight": 5},
]


def _critical_dict_ws() -> dict:
    return {
        "verified_decisions": list(_CRITICAL_DECISIONS),
        "verified_tradeoffs": [],
        "resolved_disputes": [],
        "human_required_items": [],
    }


def _critical_workspace() -> Workspace:
    ws = Workspace(workspace_id="ws_critical", original_prompt="design a system")
    for d in _CRITICAL_DECISIONS:
        ws.add_verified_decision(dict(d))
    return ws


# ---------------------------------------------------------------------------
# 1. Clarification reopens nothing
# ---------------------------------------------------------------------------

def test_clarification_reopens_nothing() -> None:
    impact = analyze_followup_impact(
        _critical_dict_ws(), "Explain the database choice in more detail.")
    assert impact["follow_up_type"] == "clarification", impact
    assert impact["affected_disputes"] == [], impact
    assert impact["reopen_required"] is False, impact
    assert len(impact["unaffected_disputes"]) == 4, impact


# ---------------------------------------------------------------------------
# 2. CRITICAL: PostgreSQL contradiction -> 2 affected, 2 preserved, queue=2
# ---------------------------------------------------------------------------

def test_contradiction_postgresql_critical() -> None:
    impact = analyze_followup_impact(
        _critical_dict_ws(), "Assume PostgreSQL cannot be used.")
    assert impact["follow_up_type"] == "contradiction", impact
    # debate queue size must be 2, NOT 4
    assert len(impact["affected_disputes"]) == 2, impact
    assert set(impact["affected_disputes"]) == {"database_choice",
                                                 "storage_strategy"}, impact
    assert set(impact["unaffected_disputes"]) == {"provider_routing",
                                                   "deployment_model"}, impact
    assert impact["reopen_required"] is True, impact
    # dependency: storage_strategy depends on database_choice (references
    # "database"); provider_routing/deployment_model do not.
    assert "storage_strategy" in impact["affected_disputes"], impact


# ---------------------------------------------------------------------------
# 3. Extension with no related existing decision reopens nothing
# ---------------------------------------------------------------------------

def test_extension_unrelated_reopens_nothing() -> None:
    impact = analyze_followup_impact(
        _critical_dict_ws(), "Which CI/CD tool should we use for the pipeline?")
    assert impact["follow_up_type"] == "extension", impact
    assert impact["affected_disputes"] == [], impact
    assert impact["reopen_required"] is False, impact
    assert len(impact["unaffected_disputes"]) == 4, impact


# ---------------------------------------------------------------------------
# 4. Dependency propagation chains across hops (A -> B -> C)
# ---------------------------------------------------------------------------

def test_dependency_propagation_chains() -> None:
    ws = {
        "verified_decisions": [
            {"normalized_key": "database_choice",
             "decision": "Use PostgreSQL as the primary database"},
            {"normalized_key": "storage_strategy",
             "decision": "Use the database native replication for storage"},
            {"normalized_key": "backup_plan",
             "decision": "Stream storage snapshots to the backup bucket daily"},
        ],
        "verified_tradeoffs": [], "resolved_disputes": [],
        "human_required_items": [],
    }
    # contradiction hits database_choice; storage_strategy references
    # "database"; backup_plan references "storage" -> both reopen.
    impact = analyze_followup_impact(ws, "Assume PostgreSQL cannot be used.")
    assert impact["affected_disputes"] == ["backup_plan", "database_choice",
                                           "storage_strategy"], impact
    assert impact["reopen_required"] is True


# ---------------------------------------------------------------------------
# 5. Unaffected decisions are preserved exactly
# ---------------------------------------------------------------------------

def test_unaffected_preserved_exactly() -> None:
    impact = analyze_followup_impact(
        _critical_dict_ws(), "Assume PostgreSQL cannot be used.")
    affected = set(impact["affected_disputes"])
    preserved = set(impact["unaffected_disputes"])
    assert affected.isdisjoint(preserved)
    assert preserved == {"provider_routing", "deployment_model"}


# ---------------------------------------------------------------------------
# 6. Tradeoffs are analyzed via dispute_key
# ---------------------------------------------------------------------------

def test_tradeoffs_included() -> None:
    ws = _critical_dict_ws()
    ws["verified_tradeoffs"] = [{
        "dispute_key": "cache_layer",
        "option_a": "Redis", "option_b": "No cache",
        "recommendation": "Redis", "reasoning": "Database reads are hot",
    }]
    impact = analyze_followup_impact(ws, "Drop Redis and use the database cache.")
    # cache_layer is directly hit ("redis", "database"); storage_strategy and
    # database_choice depend on "database". The tradeoff MUST appear in the
    # universe (affected or unaffected).
    universe = set(impact["affected_disputes"]) | set(impact["unaffected_disputes"])
    assert "cache_layer" in universe, impact


# ---------------------------------------------------------------------------
# 7. Human-required disputes are part of the universe and can be affected
# ---------------------------------------------------------------------------

def test_human_required_included_and_affected() -> None:
    ws = _critical_dict_ws()
    ws["human_required_items"] = [{
        "dispute_text": "Which database driver should we ship?",
        "affected_disputes": ["database_choice"],
        "reasoning": "driver choice depends on the database",
    }]
    impact = analyze_followup_impact(ws, "Assume PostgreSQL cannot be used.")
    universe = set(impact["affected_disputes"]) | set(impact["unaffected_disputes"])
    # The human-required item's dispute (database_choice) is the one being
    # directly hit, so it must be in affected.
    assert "database_choice" in impact["affected_disputes"], impact
    # universe must include the human-required dispute key too (same key).
    assert "database_choice" in universe


# ---------------------------------------------------------------------------
# 8. Analyzer accepts both a plain dict and a Workspace object
# ---------------------------------------------------------------------------

def test_accepts_dict_and_workspace_object() -> None:
    fu = "Assume PostgreSQL cannot be used."
    from_dict = analyze_followup_impact(_critical_dict_ws(), fu)
    from_ws = analyze_followup_impact(_critical_workspace(), fu)
    assert from_dict["affected_disputes"] == from_ws["affected_disputes"]
    assert from_dict["follow_up_type"] == from_ws["follow_up_type"]
    assert from_ws["follow_up_type"] == "contradiction"


# ---------------------------------------------------------------------------
# 9. Empty workspace: nothing to affect
# ---------------------------------------------------------------------------

def test_empty_workspace() -> None:
    ws = {"verified_decisions": [], "verified_tradeoffs": [],
          "resolved_disputes": [], "human_required_items": []}
    impact = analyze_followup_impact(ws, "Assume PostgreSQL cannot be used.")
    assert impact["affected_disputes"] == []
    assert impact["unaffected_disputes"] == []
    assert impact["reopen_required"] is False
    assert impact["follow_up_type"] == "contradiction"


# ---------------------------------------------------------------------------
# 10. audit_entry shape matches Phase 8 spec
# ---------------------------------------------------------------------------

def test_audit_entry_shape() -> None:
    impact = analyze_followup_impact(
        _critical_dict_ws(), "Assume PostgreSQL cannot be used.")
    entry = audit_entry(impact, "Assume PostgreSQL cannot be used.")
    assert entry["follow_up_type"] == "contradiction"
    assert entry["affected_dispute_count"] == 2
    assert set(entry["reopened_disputes"]) == {"database_choice",
                                                "storage_strategy"}
    assert set(entry["preserved_disputes"]) == {"provider_routing",
                                                 "deployment_model"}
    assert entry["follow_up"] == "Assume PostgreSQL cannot be used."
    assert "timestamp" in entry and entry["reasoning"]


# ---------------------------------------------------------------------------
# 11. Integration: resume_workspace re-debates only affected disputes
#     (queue size = 2) and preserves the rest; audit trail persisted.
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self, label="X"):
        self.label = label

    def start(self): pass

    def close(self): pass

    def ask(self, prompt, dispute_key=None):
        return f"{self.label}:response"


class _FakeWM:
    def __init__(self, ws):
        self._ws = ws
        self.saves = 0

    def load_workspace(self, _id):
        return self._ws

    def save_workspace(self, _ws):
        self.saves += 1


def _install_integration_patches(debated: list):
    patches = []

    import conflict_extractor as ce
    patches.append((ce, "extract_conflicts", ce.extract_conflicts))
    ce.extract_conflicts = lambda _g, _m: {"severity": "LOW",
                                           "disputed_claims": [],
                                           "agreed_claims": [],
                                           "resolved_disputes": []}

    patches.append((wde, "verify_all_disputes", wde.verify_all_disputes))
    wde.verify_all_disputes = lambda **_kw: {
        "resolved_claims": [], "unresolved_claims": [],
        "verification_score": 100, "truncated_count": 0,
    }

    import synthesis_engine as se
    patches.append((se, "synthesize_answer", se.synthesize_answer))
    se.synthesize_answer = lambda **_kw: "SYNTH_OFFLINE"

    class _FakeMem:
        def save_debate(self, **_kw): return 4242
    patches.append((wde, "memory", wde.memory))
    wde.memory = _FakeMem()

    class _FakeHQ:
        def get_pending_count(self, _id): return 0
    patches.append((wde, "HumanDecisionQueue", wde.HumanDecisionQueue))
    wde.HumanDecisionQueue = _FakeHQ

    import gpt_worker as gw
    import gemini_worker as gemw
    patches.append((gw, "GPTSession", gw.GPTSession))
    patches.append((gemw, "GeminiSession", gemw.GeminiSession))
    gw.GPTSession = lambda label="GPT": _FakeSession(label)
    gemw.GeminiSession = lambda label="GEM": _FakeSession(label)

    # engine-level: build a queue from raw text, record each debate call.
    engine = wde.WorkspaceDebateEngine()

    def _build_queue(raw, _g, _m):
        return [Dispute(dispute_text=t, normalized_key=f"q{i}",
                       gpt_source="g", gemini_source="m",
                       priority="MEDIUM", priority_weight=3)
                for i, t in enumerate(raw)]

    engine._build_dispute_queue = _build_queue
    # Pre-debate research seam: no-op fake keeps this offline (no browser).
    engine._pre_debate_research_fn = lambda topic: {"claim": topic, "sources": []}

    def _debate(_q, dispute, _g, _m):
        debated.append(dispute.normalized_key)
        dispute.gpt_current = f"g::{dispute.normalized_key}"
        dispute.gemini_current = f"m::{dispute.normalized_key}"
        dispute.consensus_score = 91
        return "resolved"

    engine._debate_dispute = _debate
    return patches, engine


def test_resume_workspace_only_affected_redebated() -> None:
    ws = _critical_workspace()
    ws.resolved_disputes = [
        {"normalized_key": "database_choice", "dispute": "Use PostgreSQL",
         "research_evidence": {"normalized_key": "database_choice",
                                "sources": ["s1", "s2"]}},
        {"normalized_key": "provider_routing", "dispute": "Route via gateway",
         "research_evidence": {"normalized_key": "provider_routing",
                                "sources": ["s3"]}},
        {"normalized_key": "deployment_model", "dispute": "Deploy on K8s",
         "research_evidence": {"normalized_key": "deployment_model",
                                "sources": ["s4"]}},
        {"normalized_key": "storage_strategy", "dispute": "DB replication",
         "research_evidence": {"normalized_key": "storage_strategy",
                                "sources": ["s5"]}},
    ]
    ws.status = WorkspaceStatus.CONSENSUS_REACHED

    debated: list = []
    orig_wm = wde.workspace_mgr
    patches, engine = _install_integration_patches(debated)
    wde.workspace_mgr = _FakeWM(ws)

    try:
        result = engine.resume_workspace("ws_critical",
                                           "Assume PostgreSQL cannot be used.")
    finally:
        wde.workspace_mgr = orig_wm
        for mod, attr, original in patches:
            setattr(mod, attr, original)

    # --- debate queue size = 2, NOT 4 ---
    assert len(debated) == 2, f"expected 2 re-debated, got {debated}"

    # --- affected removed from settled verified_decisions; preserved kept ---
    vd_keys = {d.get("normalized_key") for d in ws.verified_decisions}
    assert "database_choice" not in vd_keys, vd_keys
    assert "storage_strategy" not in vd_keys, vd_keys
    assert "provider_routing" in vd_keys, vd_keys
    assert "deployment_model" in vd_keys, vd_keys

    # --- unaffected resolved_disputes keep their research evidence ---
    rd_keys = {d.get("normalized_key"): d for d in ws.resolved_disputes}
    assert "provider_routing" in rd_keys
    assert "deployment_model" in rd_keys
    assert rd_keys["provider_routing"].get("research_evidence", {}).get(
        "sources") == ["s3"]
    assert "database_choice" not in rd_keys
    assert "storage_strategy" not in rd_keys

    # --- Phase 8 audit trail persisted on the workspace ---
    assert ws.impact_audit_trail, "audit trail not persisted"
    last = ws.impact_audit_trail[-1]
    assert last["follow_up_type"] == "contradiction", last
    assert last["affected_dispute_count"] == 2, last
    assert set(last["reopened_disputes"]) == {"database_choice",
                                              "storage_strategy"}, last
    assert set(last["preserved_disputes"]) == {"provider_routing",
                                                "deployment_model"}, last

    assert result["status"] == "completed", result


if __name__ == "__main__":
    test_clarification_reopens_nothing()
    test_contradiction_postgresql_critical()
    test_extension_unrelated_reopens_nothing()
    test_dependency_propagation_chains()
    test_unaffected_preserved_exactly()
    test_tradeoffs_included()
    test_human_required_included_and_affected()
    test_accepts_dict_and_workspace_object()
    test_empty_workspace()
    test_audit_entry_shape()
    test_resume_workspace_only_affected_redebated()
    print("test_workspace_impact_analysis: ALL PASSED (11 tests)")
