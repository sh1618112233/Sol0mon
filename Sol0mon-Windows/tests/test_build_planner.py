"""
test_build_planner.py - pins the staged-build decomposer.

Pins:
  - compose_stages groups execution_planner's DAG into component + decisions
    + tests + integration + validation + acceptance stages.
  - Stage IDs are deterministic (re-plan yields byte-identical IDs).
  - Cross-stage dependencies are the union of task deps mapped to containing
    stage ids (so the executor's deps-done check is O(stages)).
  - Per-stage spec excerpt includes a STAGED IMPLEMENTATION DIRECTIVE that
    names the stage_id (so the agent can produce stage-specific files).
  - Re-plan preserves existing stage_status records (resume-friendly).
  - Empty-tasks fallback yields a single "implementation" stage.
  - The staged plan ID is a deterministic hash of stage identities.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_planner as bp
import stage_status
from stage_status import StageStatus


def _tmp_root():
    tmp = tempfile.mkdtemp(prefix="sol0mon_bp_")
    bw.WORKSPACE_ROOT = Path(tmp)
    return tmp


def _plant_workspace_with_two_components():
    """Plant a workspace whose verification result makes
    decision_renderer._architecture_components emit 2 components (Temporal + PostgreSQL)."""
    wid = bw.create_workspace(source_question="Build X")
    decisions = {
        "answer_id": 1,
        "verification_result": {
            "resolved_claims": [
                {"normalized_key": "orchestration",
                 "dispute": "Airflow vs Temporal",
                 "verification": {"status": "RESOLVED", "confidence": 95,
                                  "finding": "Verification concludes Temporal as orchestration engine; Airflow rejected."}},
                {"normalized_key": "persistence",
                 "dispute": "MySQL vs PostgreSQL",
                 "verification": {"status": "RESOLVED", "confidence": 92,
                                  "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
            ],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "orchestration",
             "decision": "Temporal orchestration", "reasoning": ""},
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
             "decision": "PostgreSQL primary datastore", "reasoning": ""},
        ],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "Temporal+PostgreSQL"}),
        encoding="utf-8")
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        "# Implementation Objective\nBuild\n\n"
        "# Files to Create\n- `src/orchestration/main.py`\n"
        "- `src/persistence/store.py`\n- `tests/test_app.py`\n\n"
        "# Files to Modify\n(none)\n\n"
        "# Implementation Steps\n1. orchestration\n2. persistence\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n",
        encoding="utf-8")
    return wid


def test_compose_stages_groups_tasks_by_kind():
    _tmp_root()
    wid = _plant_workspace_with_two_components()
    manifest = bp.plan_staged_build(wid)
    stages = manifest["stages"]
    kinds = [s["kind"] for s in stages]
    # Expect: component, component, decisions, tests, integration, validation, acceptance.
    assert kinds == ["component", "component", "decisions", "tests",
                      "integration", "validation", "acceptance"], kinds
    comp_stages = [s for s in stages if s["kind"] == "component"]
    assert all(s["dependencies"] == [] for s in comp_stages)
    by_kind = {s["kind"]: s for s in stages}
    # Decisions deps == every component stage id.
    assert set(by_kind["decisions"]["dependencies"]) == {s["stage_id"] for s in comp_stages}
    # Tests deps == components + decisions stage.
    assert set(by_kind["tests"]["dependencies"]) == (
        {s["stage_id"] for s in comp_stages} | {by_kind["decisions"]["stage_id"]}
    )
    # Validation deps == tests + integration.
    assert set(by_kind["validation"]["dependencies"]) == {
        by_kind["tests"]["stage_id"], by_kind["integration"]["stage_id"]}
    # Acceptance deps == validation.
    assert by_kind["acceptance"]["dependencies"] == [by_kind["validation"]["stage_id"]]


def test_plan_id_is_deterministic():
    _tmp_root()
    wid = _plant_workspace_with_two_components()
    m1 = bp.plan_staged_build(wid)
    m2 = bp.plan_staged_build(wid)
    assert m1["plan_id"] == m2["plan_id"], (m1["plan_id"], m2["plan_id"])
    assert [s["stage_id"] for s in m1["stages"]] == \
           [s["stage_id"] for s in m2["stages"]]


def test_per_stage_spec_excerpt_has_directive_with_stage_id():
    _tmp_root()
    wid = _plant_workspace_with_two_components()
    manifest = bp.plan_staged_build(wid)
    for s in manifest["stages"]:
        excerpt = s["stage_spec_excerpt"]
        assert "# STAGED IMPLEMENTATION DIRECTIVE (stage_id=" in excerpt, excerpt[:200]
        assert s["stage_id"] in excerpt, (s["stage_id"], excerpt[:200])
        # The original spec is appended as context for the agent.
        assert "# ORIGINAL FULL SPEC" in excerpt, excerpt[:200]


def test_replan_preserves_existing_stage_status():
    _tmp_root()
    wid = _plant_workspace_with_two_components()
    manifest = bp.plan_staged_build(wid)
    comp_stage_ids = [s["stage_id"] for s in manifest["stages"] if s["kind"] == "component"]
    stage_status.write_stage_status(wid, comp_stage_ids[0], StageStatus.IMPLEMENTING)
    # Re-run plan_staged_build; the seeded status for that stage must remain
    # IMPLEMENTING (the planner only writes PENDING for stages with no record).
    bp.plan_staged_build(wid)
    rec = stage_status.read_stage_status(wid, comp_stage_ids[0])
    assert rec["status"] == "IMPLEMENTING", rec


def test_empty_tasks_fallback_is_single_implementation_stage():
    _tmp_root()
    wid = bw.create_workspace(source_question="greenfield")
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps({"verification_result": {}, "verified_decisions": []}),
        encoding="utf-8")
    (bw._workspace_dir(wid) / "implementation_plan.md").write_text(
        "# Implementation Objective\nX\n\n"
        "# Files to Create\n- `src/app.py`\n\n"
        "# Implementation Steps\n1. build\n\n"
        "# Acceptance Criteria\n- pytest passes\n\n"
        "# Output Contract\nFiles under src/.\n",
        encoding="utf-8")
    m = bp.plan_staged_build(wid)
    # execution_planner synthesizes a fallback component -> multi-stage plan
    # (component + tests + integration + validation + acceptance). The
    # single-stage fallback only triggers when compose_stages is called with
    # NO tasks — exercise that path directly.
    empty_stages = bp.compose_stages({"tasks": {}}, "spec text")
    assert len(empty_stages) == 1
    assert empty_stages[0]["kind"] == "implementation"


def test_human_readable_directory_generated_when_slug_used():
    _tmp_root()
    wid = bw.create_workspace(source_question="x", human_slug="Autonomous Engine")
    ws_dir = bw._workspace_dir(wid)
    assert ws_dir.name.startswith("autonomous_engine_"), ws_dir.name
    # The canonical UUID lives inside workspace.json.
    ws_obj = bw.load_workspace(wid)
    assert ws_obj.workspace_id == wid


def test_plan_id_includes_spec_checksum_and_execution_plan_id():
    _tmp_root()
    wid = _plant_workspace_with_two_components()
    m = bp.plan_staged_build(wid)
    assert m["source_spec_checksum"]
    assert m["execution_plan_id"]
    assert m["plan_id"].startswith("stage_") and len(m["plan_id"]) >= 16


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
