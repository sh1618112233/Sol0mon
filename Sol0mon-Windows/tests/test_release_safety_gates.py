from __future__ import annotations

import build_pipeline
from answer_loader import _to_verified_decisions


def test_missing_or_malformed_verification_blocks_answer_build():
    assert "missing or malformed" in build_pipeline._answer_eligibility_reason({})
    assert "malformed" in build_pipeline._answer_eligibility_reason({
        "verification_result": {"resolved_claims": "bad", "unresolved_claims": []}
    })


def test_unresolved_verification_requires_explicit_governance_permission():
    answer = {"conflict_severity": "LOW", "verification_result": {
        "resolved_claims": [],
        "unresolved_claims": [{"normalized_key": "db_choice"}],
        "governance_result": {"verified_decisions": [], "verified_tradeoffs": []},
    }}
    assert "without an eligible governance" in build_pipeline._answer_eligibility_reason(answer)
    answer["verification_result"]["governance_result"]["verified_tradeoffs"] = [{
        "outcome_type": "VERIFIED_TRADEOFF", "normalized_key": "db_choice",
        "option_a": "SQLite", "option_b": "PostgreSQL",
    }]
    assert build_pipeline._answer_eligibility_reason(answer) is None


def test_failed_verification_always_blocks():
    assert "FAILED_VERIFICATION" in build_pipeline._answer_eligibility_reason({
        "conflict_severity": "FAILED_VERIFICATION",
        "verification_result": {"resolved_claims": [], "unresolved_claims": []},
    })


def test_human_choice_resolves_only_matching_human_required_identity():
    answer = {"verification_result": {
        "resolved_claims": [],
        "unresolved_claims": [{"normalized_key": "db"}],
        "governance_result": {"human_required": [{
            "outcome_type": "HUMAN_REQUIRED", "dispute_key": "db",
        }]},
    }}
    assert build_pipeline._answer_eligibility_reason(answer) is not None
    answer["human_decisions_resolved"] = [{
        "normalized_key": "db", "chosen_option": "SQLite",
    }]
    assert build_pipeline._answer_eligibility_reason(answer) is None


def test_answer_loader_skips_keyless_resolved_claim():
    result = {"resolved_claims": [{
        "dispute": "claim without identity",
        "verification": {"finding": "verified result"},
    }]}
    assert _to_verified_decisions(result) == []
