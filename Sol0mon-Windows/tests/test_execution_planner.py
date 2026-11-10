"""
test_execution_planner.py - Phases D + E regression: deterministic Execution
Planner + Task Graph.

Pins:
  - Determinism: same BuildSpec -> byte-identical plan.to_dict() + same id.
  - The plan is a valid DAG (passes validate_execution_plan).
  - Topological order respects dependencies; acceptance is the last task.
  - Task records carry: inputs / outputs / dependencies / owner / status /
    retries / acceptance_criteria / validation (the validator name).
  - Resume readiness via TaskGraph.ready(): a task becomes ready only once
    all its dependencies are DONE.
  - Empty spec -> a non-empty fallback plan (the DAG is never empty).
  - plan_from_workspace reads decisions.json (the canonical source of truth —
    never re-parses markdown).
  - The SQLite winner feeds the canonical component matcher; the rejected
    option never produces a component task.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import execution_planner as EP
import validation as V
from execution_planner import Task, TaskKind, TaskStatus


VR_156 = {
    "resolved_claims": [{
        "normalized_key": "database_technology_selection",
        "dispute": "Database Technology: GPT selects PostgreSQL, Gemini selects SQLite",
        "verification": {"status": "RESOLVED", "confidence": 95,
                         "finding": ("Both evidence strips conclude SQLite is the "
                                     "appropriate persistence engine; the text "
                                     "argues against PostgreSQL.")},
    }],
    "equivalent_claims": [], "unresolved_claims": [],
    "verification_score": 100, "truncated_count": 0,
}


def _spec(verification_result=VR_156, verified_decisions=None):
    return {
        "verification_result": verification_result,
        "verified_decisions": verified_decisions if verified_decisions is not None else [
            {"outcome_type": "VERIFIED_DECISION",
             "dispute_key": "database_technology_selection",
             "decision": "SQLite as primary persistence engine",
             "reasoning": "low capital"}],
        "spec_text": ("# Architecture\nA\n# Components\nB\n# File Structure\nC\n"
                      "# Dependencies\nD\n# Constraints\nE\n"
                      "# Testing Requirements\nF\n# Acceptance Criteria\nG\n"),
        "status": "completed",
    }


# --- Determinism ---

def test_plan_is_deterministic():
    spec = _spec()
    p1 = EP.plan(spec)
    p2 = EP.plan(spec)
    assert p1.to_dict() == p2.to_dict()
    assert p1.id == p2.id and p1.order == p2.order
    assert p1.tasks == p2.tasks


def test_different_spec_different_plan_id():
    p1 = EP.plan(_spec())
    p2 = EP.plan(_spec(verified_decisions=[
        {"dispute_key": "different", "decision": "Different thing", "reasoning": ""}]))
    assert p1.id != p2.id


# --- DAG validity ---

def test_plan_passes_validator():
    p = EP.plan(_spec())
    r = V.validate_execution_plan(p.to_dict())
    assert r.passed, r.to_dict()


def test_empty_spec_still_yields_non_empty_plan():
    p = EP.plan({})
    assert p.tasks and len(p.order) >= 1
    assert V.validate_execution_plan(p.to_dict()).passed
    # fallback task is a component (agent still has something to point at)
    assert any(t.kind is TaskKind.COMPONENT for t in p.tasks.values())


# --- Topological order ---

def test_order_respects_dependencies():
    p = EP.plan(_spec())
    pos = {tid: i for i, tid in enumerate(p.order)}
    for tid, t in p.tasks.items():
        for d in t.dependencies:
            assert pos[d] < pos[tid], f"{d} after {tid}"
    # acceptance is the terminal task of the DAG
    assert p.order[-1] == "acceptance"
    # validation precedes acceptance
    assert pos["validation"] < pos["acceptance"]
    # tests + integration precede validation
    assert pos["tests"] < pos["validation"]
    assert pos["integration"] < pos["validation"]


# --- Task record contract ---

def test_tasks_carry_required_fields():
    p = EP.plan(_spec())
    for tid, t in p.tasks.items():
        for field in ("id", "kind", "inputs", "outputs", "dependencies",
                      "owner", "status", "retries", "acceptance_criteria",
                      "validation"):
            assert hasattr(t, field), (tid, field)
        assert t.status is TaskStatus.PENDING
        assert t.retries == 0
    # validation + acceptance tasks point at the final_build validator
    assert p.tasks["validation"].validation == "final_build"
    assert p.tasks["acceptance"].validation == "final_build"


# --- Resume readiness via ready() ---

def test_ready_progressive_release_as_deps_done():
    p = EP.plan(_spec())
    from dataclasses import replace
    # Initially: all component tasks are ready (no deps).
    ready0 = set(p.ready())
    assert ready0 and all(p.tasks[t].kind is TaskKind.COMPONENT for t in ready0)

    # Mark all components DONE -> decisions + integration become ready.
    done = {tid: replace(t, status=TaskStatus.DONE)
            for tid, t in p.tasks.items() if t.kind is TaskKind.COMPONENT}
    p1 = replace(p, tasks={**p.tasks, **done})
    ready1 = set(p1.ready())
    assert "integration" in ready1
    # decisions depend on all components, so they're ready too.
    dec_ids = {tid for tid, t in p1.tasks.items() if t.kind is TaskKind.DECISION}
    assert dec_ids <= ready1

    # Now also mark decisions + tests + integration DONE -> validation ready.
    done2 = dict(done)
    for tid in list(p1.tasks):
        if p1.tasks[tid].kind in (TaskKind.DECISION, TaskKind.TESTS,
                                 TaskKind.INTEGRATION):
            done2[tid] = replace(p1.tasks[tid], status=TaskStatus.DONE)
    p2 = replace(p, tasks={**p.tasks, **done2})
    ready2 = set(p2.ready())
    assert "validation" in ready2


# --- is_complete + dependents ---

def test_is_complete_and_dependents():
    p = EP.plan(_spec())
    assert not p.is_complete()
    # the acceptance task's dependents is empty (terminal)
    assert p.dependents("acceptance") == []
    # tests has dependents (validation)
    assert "validation" in p.dependents("tests")


# --- plan_from_workspace reads the canonical decisions.json ---

def test_plan_from_workspace_uses_decisions_json():
    tmp = tempfile.mkdtemp(prefix="sol0mon_ep_")
    bw.WORKSPACE_ROOT = Path(tmp)
    wid = bw.create_workspace()
    decisions = {
        "verification_result": VR_156,
        "verified_decisions": [{"outcome_type": "VERIFIED_DECISION",
                                "dispute_key": "database_technology_selection",
                                "decision": "SQLite persistence",
                                "reasoning": "low capital"}],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    # leave answer.json empty so the canonical read does not need prose
    p = EP.plan_from_workspace(wid)
    assert p.tasks and V.validate_execution_plan(p.to_dict()).passed
    # The SQLite winner produced at least one decision task; the rejected
    # PostgreSQL never produced a component task (the single source of truth
    # invariant holds through the planner).
    dec_tasks = [t for t in p.tasks.values() if t.kind is TaskKind.DECISION]
    assert dec_tasks, "expected a decision task from the verified SQLite decision"


def test_rejected_option_never_becomes_a_component_task():
    p = EP.plan(_spec())
    all_text = json.dumps([t.to_dict() for t in p.tasks.values()]).lower()
    # The 156 winner is SQLite; PostgreSQL was rejected. The spec's component
    # matcher (single source of truth) must NOT have produced a component task
    # named after the rejected option.
    # We assert there is no component whose output path slug contains 'postgresql'.
    for t in p.tasks.values():
        if t.kind is TaskKind.COMPONENT:
            for o in t.outputs:
                assert "postgresql" not in o.lower(), (t.id, t.outputs)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
