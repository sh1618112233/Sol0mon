from __future__ import annotations

import build_pipeline
from answer_loader import _to_verified_decisions
import build_workspace


def test_missing_or_malformed_verification_blocks_answer_build():
    assert "verification" in build_pipeline._answer_eligibility_reason({"answer_id": 1})
    assert "malformed" in build_pipeline._answer_eligibility_reason({
        "answer_id": 1,
        "verification_result": {"resolved_claims": "bad", "unresolved_claims": []}
    })


def test_unresolved_verification_requires_explicit_governance_permission():
    answer = {"answer_id": 1, "conflict_severity": "LOW", "verification_result": {
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
        "answer_id": 1,
        "conflict_severity": "FAILED_VERIFICATION",
        "verification_result": {"resolved_claims": [], "unresolved_claims": []},
    })


def test_human_choice_resolves_only_matching_human_required_identity():
    answer = {"answer_id": 1, "verification_result": {
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


def test_answer_id_is_required_and_stop_human_does_not_authorize_build():
    assert "answer_id" in build_pipeline._answer_eligibility_reason({
        "verification_result": {"resolved_claims": [], "unresolved_claims": []}
    })
    assert not build_pipeline._is_full_build_decision({
        "execution_decision": "STOP_HUMAN_REQUIRED"
    })


def test_persisted_answer_files_fail_closed_and_prompt_workspaces_remain_valid(
        tmp_path, monkeypatch):
    import human_decision_manager

    monkeypatch.setattr(build_workspace, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(human_decision_manager, "load_pending", lambda _: [])
    wid = build_workspace.create_workspace(source_question="prompt")
    answer_path = build_workspace._workspace_dir(wid) / "answer.json"
    cases = [("", "could not be read or parsed"), ("[]", "not a JSON object"),
             ("null", "not a JSON object"),
             ('{"answer_id":""}', "answer_id"),
             ('{"synthesized_answer":"x"}', "answer_id"),
             ('{"answer_id":1}', "verification"),
             ('{"answer_id":1,"verification_result":{"resolved_claims":"bad","unresolved_claims":[]}}', "malformed")]
    for raw, reason in cases:
        answer_path.write_text(raw, encoding="utf-8")
        result = build_pipeline.execute_build_spec(wid)
        assert result["blocked"] is True
        assert reason in result["blocked_reason"]

    answer_path.write_text('{"answer_id":1}', encoding="utf-8")
    original_read_text = type(answer_path).read_text
    def failing_read(path, *args, **kwargs):
        if path == answer_path:
            raise OSError("read denied")
        return original_read_text(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(type(answer_path), "read_text", failing_read)
        result = build_pipeline.execute_build_spec(wid)
    assert "could not be read or parsed" in result["blocked_reason"]

    answer_path.unlink()
    result = build_pipeline.execute_build_spec(wid)
    assert result.get("error") == "No implementation spec found"


def test_valid_verified_answer_is_eligible():
    assert build_pipeline._answer_eligibility_reason({
        "answer_id": 42,
        "verification_result": {
            "resolved_claims": [{"normalized_key": "db"}],
            "unresolved_claims": [],
            "verification_score": 100,
        },
    }) is None


def test_empty_verification_without_score_is_rejected():
    reason = build_pipeline._answer_eligibility_reason({
        "answer_id": 1,
        "verification_result": {"resolved_claims": [], "unresolved_claims": []},
    })
    assert "no verification evidence" in reason
