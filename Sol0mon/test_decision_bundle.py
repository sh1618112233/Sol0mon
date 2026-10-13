"""
test_decision_bundle.py - Phase 2 tests for Human Decision Aggregation.

Headline success criterion: 3 HUMAN_REQUIRED outcomes produce 1 bundle.

Run: python3 test_decision_bundle.py
"""

from __future__ import annotations

import tempfile

import build_workspace as bw
from decision_bundle import (
    DecisionBundle,
    add_decision,
    generate_bundle,
    reset_bundle,
    collect_from_governance_result,
)


def _outcomes():
    return [
        {"outcome_type": "HUMAN_REQUIRED", "decision_id": "d1",
         "dispute_key": "db_choice", "dispute_text": "PostgreSQL vs SQLite",
         "option_a": "PostgreSQL", "option_b": "SQLite", "confidence": 60},
        {"outcome_type": "HUMAN_REQUIRED", "decision_id": "d2",
         "dispute_key": "cache_choice", "dispute_text": "Redis vs Memcached",
         "option_a": "Redis", "option_b": "Memcached", "confidence": 55},
        {"outcome_type": "HUMAN_REQUIRED", "decision_id": "d3",
         "dispute_key": "queue_choice", "dispute_text": "Kafka vs RabbitMQ",
         "option_a": "Kafka", "option_b": "RabbitMQ", "confidence": 50},
    ]


def test_three_outcomes_produce_one_bundle() -> None:
    bundle = DecisionBundle(workspace_id="ws-1")
    for o in _outcomes():
        bundle.add_decision(o)
    out = bundle.generate_bundle()

    assert out["decision_count"] == 3
    assert len(out["decisions"]) == 3
    assert out["workspace_id"] == "ws-1"
    assert "bundle_id" in out and out["bundle_id"]
    assert "created_at" in out
    assert all(d["decision_id"] in out["summary"] for d in out["decisions"])
    # One bundle, one summary line -> user asked once.
    assert out["summary"].count(", ") == 2


def test_dedup_does_not_inflate_count() -> None:
    bundle = DecisionBundle()
    o = _outcomes()
    bundle.add_decision(o[0])
    bundle.add_decision(o[0])  # same decision_id -> dedup
    bundle.add_decision(o[1])
    out = bundle.generate_bundle()
    assert out["decision_count"] == 2
    assert len(out["decisions"]) == 2


def test_empty_bundle_is_safe() -> None:
    out = DecisionBundle().generate_bundle()
    assert out["decision_count"] == 0
    assert out["decisions"] == []
    assert "No human-required" in out["summary"]


def test_module_level_collect_then_generate() -> None:
    reset_bundle()
    add_decision(_outcomes()[0])
    add_decision(_outcomes()[1])
    add_decision(_outcomes()[2])
    out = generate_bundle()
    assert out["decision_count"] == 3
    # default bundle was reset after generate -> next generate is empty
    assert generate_bundle()["decision_count"] == 0


def test_integration_seam_from_governance_result() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bw.WORKSPACE_ROOT = __import__("pathlib").Path(tmp)
        wid = bw.create_workspace()
        gov_result = GovernanceResultShape(human_required=_outcomes()).to_dict()
        bundle = collect_from_governance_result(gov_result, workspace_id=wid)
        assert bundle["decision_count"] == 3
        assert bundle["workspace_id"] == wid
        assert all(d in bundle["decisions"] for d in _outcomes())


class GovernanceResultShape:
    """Minimal stand-in for the real GovernanceResult.to_dict() output:
    { "human_required": [ ... GovernedOutcome dicts ... ] }."""

    def __init__(self, human_required):
        self.human_required = human_required

    def to_dict(self):
        return {"human_required": list(self.human_required)}


def test_does_not_touch_protected_logic() -> None:
    protected = {
        "debate_engine", "governance", "verification_engine",
        "consensus_engine", "chat_session", "gpt_worker", "gemini_worker",
    }
    import decision_bundle as db
    imported = set(getattr(m, "__name__", "") for m in db.__dict__.values()
                  if hasattr(m, "__name__"))
    assert not (protected & imported), \
        f"decision_bundle pulls in protected modules: {protected & imported}"


if __name__ == "__main__":
    test_three_outcomes_produce_one_bundle()
    test_dedup_does_not_inflate_count()
    test_empty_bundle_is_safe()
    test_module_level_collect_then_generate()
    test_integration_seam_from_governance_result()
    test_does_not_touch_protected_logic()
    print("test_decision_bundle: ALL PASSED")
