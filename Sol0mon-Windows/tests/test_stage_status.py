"""
test_stage_status.py - pins the per-stage state machine + persistence.

Pins:
  - StateStatus enum + transition table (PENDING -> IMPLEMENTING -> ...).
  - read/write build_plan.json, stage_status.json, checkpoints/<id>.json,
    repair_history.json, test_results.json.
  - deps-aware next_pending_stage + all_stages_done.
  - Backward-compat: legacy workspace (no build_plan.json) -> has_staged_layout
    False -> next_pending_stage None -> all_stages_done False.
  - Determinism: re-running write_stage_status preserves attempts across
    updates (the resume checkpoint contract).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import stage_status
from stage_status import StageStatus, StageStatusError, stage_transition


def _tmp_root():
    tmp = tempfile.mkdtemp(prefix="sol0mon_ss_")
    bw.WORKSPACE_ROOT = Path(tmp)
    return tmp


def test_state_machine_transitions():
    assert stage_transition(StageStatus.PENDING, StageStatus.IMPLEMENTING) is StageStatus.IMPLEMENTING
    assert stage_transition(StageStatus.IMPLEMENTING, StageStatus.VALIDATING) is StageStatus.VALIDATING
    assert stage_transition(StageStatus.REPAIRING, StageStatus.VALIDATING) is StageStatus.VALIDATING
    # DONE is terminal.
    raised = False
    try:
        stage_transition(StageStatus.DONE, StageStatus.IMPLEMENTING)
    except StageStatusError:
        raised = True
    assert raised


def test_legacy_workspace_no_staged_layout():
    _tmp_root()
    wid = bw.create_workspace(source_question="legacy")
    assert stage_status.has_staged_layout(wid) is False
    assert stage_status.next_pending_stage(wid) is None
    assert stage_status.all_stages_done(wid) is False
    # read functions are safe on a legacy workspace (empty defaults).
    assert stage_status.read_build_plan(wid) == {"stages": [], "generated_at": None, "plan_id": ""}
    assert stage_status.read_stage_status(wid) == {"stages": {}, "updated_at": None}
    assert stage_status.read_stage_status(wid, "stage_xx") == {}


def test_write_and_seed_plan():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    plan = {
        "plan_id": "pid",
        "generated_at": "2026-07-21T00:00:00+00:00",
        "stages": [
            {"stage_id": "stage_00_a", "title": "A", "kind": "component",
             "dependencies": [], "files": ["src/a.py"]},
            {"stage_id": "stage_01_b", "title": "B", "kind": "component",
             "dependencies": ["stage_00_a"], "files": ["src/b.py"]},
        ],
    }
    assert stage_status.write_build_plan(wid, plan) is True
    assert stage_status.has_staged_layout(wid) is True
    loaded = stage_status.read_build_plan(wid)
    assert loaded["plan_id"] == "pid"
    assert [s["stage_id"] for s in loaded["stages"]] == ["stage_00_a", "stage_01_b"]


def test_seed_pending_status_for_each_stage():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    plan = {
        "plan_id": "p", "generated_at": "t",
        "stages": [
            {"stage_id": "stage_00", "title": "A", "kind": "component", "dependencies": [], "files": []},
            {"stage_id": "stage_01", "title": "B", "kind": "tests", "dependencies": ["stage_00"], "files": []},
        ],
    }
    stage_status.write_build_plan(wid, plan)
    for s in plan["stages"]:
        stage_status.write_stage_status(wid, s["stage_id"], StageStatus.PENDING)
        rec = stage_status.read_stage_status(wid, s["stage_id"])
        assert rec["status"] == "PENDING"


def test_next_pending_stage_respects_dependencies():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    stage_status.write_build_plan(wid, {
        "plan_id": "p", "generated_at": "t",
        "stages": [
            {"stage_id": "stage_00_a", "title": "A", "kind": "component",
             "dependencies": [], "files": []},
            {"stage_id": "stage_01_b", "title": "B", "kind": "component",
             "dependencies": ["stage_00_a"], "files": []},
            {"stage_id": "stage_02_c", "title": "C", "kind": "tests",
             "dependencies": ["stage_00_a", "stage_01_b"], "files": []},
        ],
    })
    for sid in ("stage_00_a", "stage_01_b", "stage_02_c"):
        stage_status.write_stage_status(wid, sid, StageStatus.PENDING)

    # First next: stage_00_a (no deps).
    nxt = stage_status.next_pending_stage(wid)
    assert nxt["stage_id"] == "stage_00_a", nxt
    stage_status.write_stage_status(wid, "stage_00_a", StageStatus.IMPLEMENTING)
    # stage_00_a is now IMPLEMENTING (not PENDING) -> skip; stage_01_b's dep
    # is still not DONE -> not eligible. No next pending.
    assert stage_status.next_pending_stage(wid) is None
    # Mark stage_00_a DONE -> stage_01_b becomes eligible.
    stage_status.write_stage_status(wid, "stage_00_a", StageStatus.DONE)
    assert stage_status.next_pending_stage(wid)["stage_id"] == "stage_01_b"
    stage_status.write_stage_status(wid, "stage_01_b", StageStatus.DONE)
    # stage_02_c deps both DONE -> eligible.
    assert stage_status.next_pending_stage(wid)["stage_id"] == "stage_02_c"
    stage_status.write_stage_status(wid, "stage_02_c", StageStatus.DONE)
    assert stage_status.next_pending_stage(wid) is None
    assert stage_status.all_stages_done(wid) is True


def test_attempts_preserved_across_writes():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    stage_status.write_build_plan(wid, {
        "plan_id": "p", "generated_at": "t",
        "stages": [{"stage_id": "stage_00", "title": "A", "kind": "component",
                    "dependencies": [], "files": []}],
    })
    stage_status.write_stage_status(wid, "stage_00", StageStatus.PENDING, attempts=0)
    stage_status.write_stage_status(wid, "stage_00", StageStatus.IMPLEMENTING,
                                     attempts_increment=1)
    stage_status.write_stage_status(wid, "stage_00", StageStatus.REPAIRING,
                                     attempts_increment=1)
    rec = stage_status.read_stage_status(wid, "stage_00")
    assert rec["attempts"] == 2, rec  # 0 -> 1 -> 2


def test_checkpoint_write_and_read():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    stage_status.write_build_plan(wid, {
        "plan_id": "p", "generated_at": "t",
        "stages": [{"stage_id": "stage_00", "title": "A", "kind": "component",
                    "dependencies": [], "files": []}],
    })
    cp = stage_status.write_stage_checkpoint(
        wid, "stage_00",
        artifacts=[{"path": "src/a.py", "sha256": "deadbeef"}],
        test_results={"runner": "pytest", "passed": 3, "failed": 0, "exit_code": 0},
    )
    assert cp["stage_id"] == "stage_00"
    assert cp["artifacts"][0]["path"] == "src/a.py"
    read = stage_status.read_stage_checkpoint(wid, "stage_00")
    assert read["test_results"]["passed"] == 3
    # stage_status.json now has last_checkpoint set.
    rec = stage_status.read_stage_status(wid, "stage_00")
    assert rec["last_checkpoint"] == "checkpoints/stage_00.json"
    assert rec["status"] == "CHECKPOINTED"


def test_repair_history_and_test_results_persistence():
    _tmp_root()
    wid = bw.create_workspace(source_question="x")
    stage_status.write_build_plan(wid, {
        "plan_id": "p", "generated_at": "t",
        "stages": [{"stage_id": "stage_00", "title": "A", "kind": "component",
                    "dependencies": [], "files": []}],
    })
    stage_status.append_repair_history(wid, "stage_00",
                                        {"attempt": 1, "reason": "fail1",
                                         "strategy": "patch"})
    stage_status.append_repair_history(wid, "stage_00",
                                        {"attempt": 2, "reason": "fail2",
                                         "strategy": "rewrite_test"})
    hist = stage_status.read_repair_history(wid, "stage_00")
    assert len(hist) == 2 and hist[0]["attempt"] == 1, hist
    assert all("attempted_at" in h for h in hist), hist
    stage_status.write_stage_test_results(wid, "stage_00",
                                            {"runner": "pytest", "passed": 5,
                                             "failed": 0, "exit_code": 0})
    assert stage_status.read_stage_test_results(wid, "stage_00")["passed"] == 5


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
