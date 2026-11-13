from __future__ import annotations

import build_workspace
import human_decision_manager
import resume
import stage_status
import staged_executor


def test_staged_executor_blocks_pending_and_unavailable_store(monkeypatch):
    monkeypatch.setattr(human_decision_manager, "load_pending",
                        lambda _: [{"decision_id": "d1", "decision": "choose"}])
    result = staged_executor.execute_stages("workspace")
    assert result["blocked_reason"] == "pending_human_decisions"
    assert result["pending_count"] == 1
    assert result["pending_decisions"][0]["decision_id"] == "d1"

    def unavailable(_):
        raise OSError("database unavailable")

    monkeypatch.setattr(human_decision_manager, "load_pending", unavailable)
    result = staged_executor.execute_stages("workspace")
    assert result["blocked_reason"] == "human_decision_store_unavailable"
    assert "database unavailable" in result["error"]


def test_resume_force_cannot_bypass_humans_or_store_failure(monkeypatch):
    pending_count = resume._pending_human_count
    monkeypatch.setattr(resume, "_pending_human_count", lambda _: 2)
    pending = resume.resume_build("workspace", force=True)
    assert pending["reason"] == "blocked_pending_humans"
    assert pending["pending_count"] == 2

    def fail_store(_):
        raise OSError("database unavailable")

    monkeypatch.setattr(human_decision_manager, "load_pending", fail_store)
    monkeypatch.setattr(resume, "_pending_human_count", pending_count)
    unavailable = resume.resume_build("workspace", force=True)
    assert unavailable["reason"] == "human_decision_store_unavailable"


def test_force_rearms_failed_stage_after_humans_are_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(build_workspace, "WORKSPACE_ROOT", tmp_path)
    wid = build_workspace.create_workspace(source_question="resume")
    stage_status.write_build_plan(wid, {
        "stages": [{"stage_id": "stage_1", "dependencies": []}],
    })
    stage_status.write_stage_status(wid, "stage_1", stage_status.StageStatus.FAILED)
    monkeypatch.setattr(resume, "_pending_human_count", lambda _: 0)

    result = resume.resume_build(wid, force=True)
    assert result["reason"] == "staged_offline_no_execution"
    assert stage_status.read_stage_status(wid, "stage_1")["status"] == "PENDING"
