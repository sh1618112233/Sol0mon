"""
test_build_spec_pipeline.py - TEST 3.

Input:  Human Required decisions (3 outcomes)
Expected: Decision Bundle -> Single aggregated bundle

Verifies Path C: workspace -> build prompt -> build spec -> store.
Verifies Path F: human_required -> decision_bundle -> human_decision_manager.

Tests that 3 HUMAN_REQUIRED outcomes produce 1 bundle persisted to
human_decisions.json in the workspace.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Make the project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
import human_decision_manager as hdm
from decision_bundle import collect_from_governance_result
from build_prompt_generator import SPEC_SECTIONS, extract_decisions, extract_tradeoffs


def _good_spec_text():
    return (
        "# Architecture\nLayered.\n\n"
        "# Components\nAPI.\n\n"
        "# File Structure\nsrc/\n\n"
        "# Dependencies\nfastapi\n\n"
        "# Constraints\nMUST use PG.\n\n"
        "# Testing Requirements\npytest\n\n"
        "# Acceptance Criteria\npass\n"
    )


def _gov_result_with_human_required():
    """A governance result with 3 HUMAN_REQUIRED outcomes + 1 verified decision."""
    return {
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "db",
             "decision": "PostgreSQL primary", "reasoning": "ACID"},
        ],
        "verified_tradeoffs": [
            {"outcome_type": "VERIFIED_TRADEOFF", "dispute_key": "cache",
             "option_a": "Redis", "option_b": "Memcached",
             "recommendation": "Redis", "reasoning": "persistence"},
        ],
        "human_required": [
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "deploy",
             "dispute_text": "K8s vs Nomad", "option_a": "K8s",
             "option_b": "Nomad", "reasoning": "ops preference",
             "decision_id": "d1"},
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "queue",
             "dispute_text": "Kafka vs RabbitMQ", "option_a": "Kafka",
             "option_b": "RabbitMQ", "reasoning": "throughput vs simplicity",
             "decision_id": "d2"},
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "monitoring",
             "dispute_text": "Prometheus vs Datadog", "option_a": "Prometheus",
             "option_b": "Datadog", "reasoning": "self-hosted vs managed",
             "decision_id": "d3"},
        ],
    }


def _fake_debate_factory(gov_result):
    """Build a fake debate_fn that returns a spec + the governance result."""
    def _debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": gov_result["verified_decisions"],
            "verified_tradeoffs": gov_result["verified_tradeoffs"],
            "human_required": gov_result["human_required"],
            "final_consensus_score": 75,
        }
    return _debate


def test_three_human_required_produce_one_bundle() -> None:
    """TEST 3: 3 HUMAN_REQUIRED -> 1 aggregated bundle."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    gov = _gov_result_with_human_required()

    wid = bw.create_workspace(source_question="test")
    bundle = bp.aggregate_human_decisions(wid, gov)

    assert bundle["decision_count"] == 3
    assert len(bundle["decisions"]) == 3
    assert "summary" in bundle

    # Persisted via the store (no flat file).
    pending = hdm.load_pending(wid)
    assert len(pending) == 3
    assert hdm.load_resolved(wid) == []


def test_build_spec_with_human_decisions_aggregation() -> None:
    """Full path: build spec with source_result containing HUMAN_REQUIRED
    -> spec generated + human decisions aggregated."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    gov = _gov_result_with_human_required()

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate_factory(gov),
        source_result=gov,
    )

    assert result["spec_status"] == "completed"
    assert result["spec_missing_sections"] == []

    # Human decisions were aggregated
    hd = result["human_decisions"]
    assert hd is not None
    assert hd["decision_count"] == 3

    # Verify in the workspace
    pending = hdm.load_pending(result["workspace_id"])
    assert len(pending) == 3


def test_build_spec_extracts_decisions_and_tradeoffs() -> None:
    """Build spec extraction: decisions + tradeoffs from governance result."""
    gov = _gov_result_with_human_required()
    decisions = extract_decisions(gov)
    tradeoffs = extract_tradeoffs(gov)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "PostgreSQL primary"
    assert len(tradeoffs) == 1
    assert tradeoffs[0]["recommendation"] == "Redis"


def test_build_spec_stored_in_workspace() -> None:
    """Spec is stored as implementation_plan.md in the workspace (canonical
    filename, audit fix #1 — previously engine+manifest disagreed on the name)."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    gov = _gov_result_with_human_required()

    result = bp.run_build_pipeline(
        "Build Project",
        debate_fn=_fake_debate_factory(gov),
        source_result=gov,
    )
    wid = result["workspace_id"]
    spec = bse.load_implementation_spec(wid)
    assert spec
    for s in SPEC_SECTIONS:
        assert s in spec.lower()


def test_resolve_human_decision_after_aggregation() -> None:
    """After aggregation, human_decision_manager.resolve() works."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    gov = _gov_result_with_human_required()

    wid = bw.create_workspace()
    bp.aggregate_human_decisions(wid, gov)

    # Resolve one
    assert hdm.resolve(wid, "d1", "K8s")
    pending = hdm.load_pending(wid)
    resolved = hdm.load_resolved(wid)
    assert len(pending) == 2
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "K8s"


if __name__ == "__main__":
    test_three_human_required_produce_one_bundle()
    test_build_spec_with_human_decisions_aggregation()
    test_build_spec_extracts_decisions_and_tradeoffs()
    test_build_spec_stored_in_workspace()
    test_resolve_human_decision_after_aggregation()
    print("test_build_spec_pipeline: ALL PASSED")
