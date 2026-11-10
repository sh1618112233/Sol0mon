"""
tests/test_project_planner.py — Project Planner + Pipeline Scheduler regression.

Pins:
  - validate_execution_graph rejects: dup ids, cycles, missing deliverables,
    invalid dependency references, unknown work package types, missing
    required fields, non-object / non-list shapes.
  - topological_order respects dependencies and is insertion-stable.
  - plan_objective: retries on malformed JSON, retries on empty response,
    retries on schema validation failure; raises after the budget.
  - generate_fn seam: production (provider router) is the default; tests
    inject a fake without touching network.
  - model preference defaults to gpt-oss-120b; carried into the persisted
    graph metadata.
  - Persistence: save_execution_graph + load_execution_graph round-trip;
    project.json + planner/execution_graph.json written in the workspace.
  - Resume: project_planner does NOT regenerate a valid graph (LLM seam not
    called); force=True regenerates.
  - Scheduler: dependency-ordered sequential dispatch; failure propagation
    (a failed package's dependents are skipped); invalid graph rejected.
  - execute_scheduled integration: planner + scheduler + injected pipeline_fn
    produce a per-package report; planner failure returns a structured
    failure (no raise).
  - Existing pipeline_runner.execute is unchanged (signature + imports).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import pipeline_runner as pr
import pipeline_scheduler as sch
import project_planner as pp


GOOD_GRAPH = {
    "objective": "ship X",
    "work_packages": [
        {"id": "wp_research", "title": "Research", "type": "research",
         "description": "survey", "deliverable": "research.md",
         "dependencies": []},
        {"id": "wp_arch", "title": "Architecture", "type": "architecture",
         "description": "design", "deliverable": "architecture.md",
         "dependencies": ["wp_research"]},
        {"id": "wp_impl", "title": "Implement", "type": "implementation",
         "description": "build", "deliverable": "src/",
         "dependencies": ["wp_arch"]},
        {"id": "wp_val", "title": "Validate", "type": "validation",
         "description": "test", "deliverable": "tests/",
         "dependencies": ["wp_impl"]},
        {"id": "wp_doc", "title": "Docs", "type": "documentation",
         "description": "document", "deliverable": "README.md",
         "dependencies": ["wp_impl"]},
    ],
}


def _good_json():
    return json.dumps(GOOD_GRAPH)


# --- validate_execution_graph ------------------------------------------------

def test_validate_accepts_good_graph():
    assert pp.validate_execution_graph(GOOD_GRAPH) == []


def test_validate_rejects_non_object():
    assert pp.validate_execution_graph([]) == ["execution graph must be a JSON object"]


def test_validate_rejects_empty_packages():
    assert pp.validate_execution_graph({"work_packages": []}) != []


def test_validate_rejects_duplicate_ids():
    bad = {"work_packages": [
        {"id": "x", "title": "t", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": []},
        {"id": "x", "title": "t2", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": []},
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("duplicate" in i for i in issues), issues


def test_validate_rejects_unknown_type():
    bad = {"work_packages": [
        {"id": "x", "title": "t", "type": "exotic", "description": "d",
         "deliverable": "y", "dependencies": []},
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("unknown type" in i for i in issues), issues


def test_validate_rejects_missing_deliverable():
    bad = {"work_packages": [
        {"id": "x", "title": "t", "type": "research", "description": "d",
         "deliverable": "", "dependencies": []},
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("deliverable" in i for i in issues), issues


def test_validate_rejects_missing_required_field():
    bad = {"work_packages": [
        {"id": "x", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": []},  # no title
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("missing required field 'title'" in i for i in issues), issues


def test_validate_rejects_unknown_dependency_reference():
    bad = {"work_packages": [
        {"id": "x", "title": "t", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": ["nonexistent"]},
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("unknown package" in i for i in issues), issues


def test_validate_rejects_circular_dependencies():
    bad = {"work_packages": [
        {"id": "a", "title": "a", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": ["b"]},
        {"id": "b", "title": "b", "type": "architecture", "description": "d",
         "deliverable": "y", "dependencies": ["a"]},
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("circular" in i for i in issues), issues


def test_validate_rejects_non_string_deps():
    bad = {"work_packages": [
        {"id": "x", "title": "t", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": "wp_research"},
    ]}
    issues = pp.validate_execution_graph(bad)
    assert any("list of strings" in i for i in issues), issues


# --- topological_order -------------------------------------------------------

def test_topological_order_respects_dependencies():
    order = pp.topological_order(GOOD_GRAPH)
    pos = {tid: i for i, tid in enumerate(order)}
    for pkg in GOOD_GRAPH["work_packages"]:
        for d in pkg["dependencies"]:
            assert pos[d] < pos[pkg["id"]], (d, pkg["id"], order)
    # research first, validation last among its dependents chain
    assert order[0] == "wp_research"
    # both impl and arch come before their dependents
    assert pos["wp_arch"] < pos["wp_impl"]
    assert pos["wp_impl"] < pos["wp_val"]
    assert pos["wp_impl"] < pos["wp_doc"]


def test_topological_order_raises_on_cycle():
    cyclic = {"work_packages": [
        {"id": "a", "title": "a", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": ["b"]},
        {"id": "b", "title": "b", "type": "research", "description": "d",
         "deliverable": "y", "dependencies": ["a"]},
    ]}
    try:
        pp.topological_order(cyclic)
        raise AssertionError("expected PlannerError on cycle")
    except pp.PlannerError:
        pass


# --- plan_objective retry behavior ------------------------------------------

def test_plan_objective_returns_valid_graph_with_injected_fn():
    def fake(prompt, module=""):
        return _good_json()
    g = pp.plan_objective("ship X", generate_fn=fake, max_retries=3)
    assert g["work_packages"] == GOOD_GRAPH["work_packages"]
    assert g["objective"] == "ship X"
    assert g["model"] == pp.DEFAULT_MODEL
    assert g["planner"] == "project_planner"
    assert "planned_at" in g


def test_plan_objective_retries_on_malformed_json():
    calls = {"n": 0}
    def fake(prompt, module=""):
        calls["n"] += 1
        return "not json at all" if calls["n"] == 1 else _good_json()
    g = pp.plan_objective("ship X", generate_fn=fake, max_retries=3)
    assert calls["n"] == 2, calls
    assert g["work_packages"] == GOOD_GRAPH["work_packages"]


def test_plan_objective_retries_on_empty_response():
    calls = {"n": 0}
    def fake(prompt, module=""):
        calls["n"] += 1
        return "" if calls["n"] == 1 else _good_json()
    g = pp.plan_objective("ship X", generate_fn=fake, max_retries=3)
    assert calls["n"] == 2
    assert g["work_packages"] == GOOD_GRAPH["work_packages"]


def test_plan_objective_retries_on_schema_validation_failure():
    bad = {"work_packages": [
        {"id": "x", "type": "exotic", "dependencies": [], "description": "d",
         "deliverable": "", "title": "t"},
    ]}
    calls = {"n": 0}
    def fake(prompt, module=""):
        calls["n"] += 1
        return json.dumps(bad) if calls["n"] == 1 else _good_json()
    g = pp.plan_objective("ship X", generate_fn=fake, max_retries=3)
    assert calls["n"] == 2
    assert g["work_packages"] == GOOD_GRAPH["work_packages"]


def test_plan_objective_raises_after_retry_budget():
    def fake(prompt, module=""):
        return "not json"
    try:
        pp.plan_objective("ship X", generate_fn=fake, max_retries=2)
        raise AssertionError("expected PlannerError")
    except pp.PlannerError as e:
        assert "2 attempt" in str(e)


def test_plan_objective_raises_on_empty_objective():
    try:
        pp.plan_objective("   ", generate_fn=lambda *a, **k: _good_json())
        raise AssertionError("expected PlannerError")
    except pp.PlannerError:
        pass


def test_plan_objective_raises_on_provider_failure_after_retries():
    def fake(prompt, module=""):
        raise RuntimeError("network down")
    try:
        pp.plan_objective("ship X", generate_fn=fake, max_retries=2)
        raise AssertionError("expected PlannerError")
    except pp.PlannerError as e:
        assert "provider failure" in str(e)


def test_planner_prompt_isolates_planner_role():
    prompt = pp.build_planner_prompt("ship Y")
    # The prompt restricts the model to decomposition; it explicitly forbids
    # the forbidden responsibilities so the planner does not creep into
    # research / architecture / technology selection.
    assert "work package" in prompt.lower()
    assert "research" in prompt and "architecture" in prompt
    assert "implementation" in prompt and "validation" in prompt
    assert "documentation" in prompt
    assert "do not" in prompt.lower() or "do NOT" in prompt


# --- persistence + resume ---------------------------------------------------

def _with_tmp_workspace_root():
    tmp = tempfile.mkdtemp(prefix="sol0mon_pp_")
    return tmp


def test_persistence_round_trip():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        wid = bw.create_workspace(source_question="ship X")
        env = pp.save_execution_graph(wid, dict(GOOD_GRAPH))
        assert env["workspace_id"] == wid
        assert env["package_count"] == 5
        # planner/execution_graph.json + project.json at the workspace root
        gpath = pp._graph_path(wid)
        ppath = pp._project_path(wid)
        assert gpath.is_file() and ppath.is_file()
        loaded = pp.load_execution_graph(wid)
        assert loaded is not None
        assert loaded["work_packages"] == GOOD_GRAPH["work_packages"]
        project = pp.load_project(wid)
        assert project["workspace_id"] == wid
        assert project["objective"] == "ship X"
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_load_returns_none_for_missing_or_corrupt():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        wid = bw.create_workspace(source_question="ship X")
        assert pp.load_execution_graph(wid) is None
        assert pp.load_project(wid) is None
        assert pp.has_execution_graph(wid) is False
        # Corrupt JSON
        gpath = pp._graph_path(wid)
        gpath.parent.mkdir(parents=True, exist_ok=True)
        gpath.write_text("{not valid json", encoding="utf-8")
        assert pp.load_execution_graph(wid) is None
        # Valid file but invalid schema -> has_execution_graph False
        gpath.write_text(json.dumps({"work_packages": []}), encoding="utf-8")
        assert pp.has_execution_graph(wid) is False
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_project_planner_resumes_without_reinvoking_llm():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        calls = {"n": 0}
        def fake(prompt, module=""):
            calls["n"] += 1
            return _good_json()
        wid, g1 = pp.project_planner("ship X", generate_fn=fake)
        assert calls["n"] == 1
        assert pp.has_execution_graph(wid)
        # Second call: resume MUST NOT call the LLM.
        calls["n"] = 0
        wid2, g2 = pp.project_planner("ship X", workspace_id=wid,
                                       generate_fn=fake)
        assert wid2 == wid
        assert calls["n"] == 0
        # Resume returns the persisted graph verbatim (with persistence metadata);
        # the original parse was the same minus the persistence metadata added
        # by save_execution_graph. Compare on the structural fields the planner
        # is responsible for, not the persistence metadata.
        assert g2["work_packages"] == g1["work_packages"]
        assert g2["objective"] == g1["objective"]
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_project_planner_force_regenerates():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        calls = {"n": 0}
        def fake(prompt, module=""):
            calls["n"] += 1
            return _good_json()
        wid, _ = pp.project_planner("ship X", generate_fn=fake)
        assert calls["n"] == 1
        # force=True -> LLM is invoked again.
        calls["n"] = 0
        pp.project_planner("ship X", workspace_id=wid, force=True,
                           generate_fn=fake)
        assert calls["n"] == 1
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_project_planner_creates_workspace_when_none_given():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        def fake(prompt, module=""):
            return _good_json()
        wid, g = pp.project_planner("ship X", generate_fn=fake)
        assert wid  # a workspace_id was minted
        assert pp.has_execution_graph(wid)
    finally:
        bw.WORKSPACE_ROOT = saved_root


# --- scheduler dispatch + failure propagation -------------------------------

def test_schedule_returns_topological_order():
    assert sch.schedule(GOOD_GRAPH) == [
        "wp_research", "wp_arch", "wp_impl", "wp_val", "wp_doc",
    ]


def test_schedule_rejects_invalid_graph():
    bad = {"work_packages": []}
    try:
        sch.schedule(bad)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_run_scheduled_pipeline_happy_path():
    calls = []
    def fn(prompt):
        calls.append(prompt)
        return {"status": "completed"}
    res = sch.run_scheduled_pipeline(GOOD_GRAPH, pipeline_fn=fn)
    assert res["succeeded"] == ["wp_research", "wp_arch", "wp_impl", "wp_val", "wp_doc"]
    assert res["failed"] == [] and res["skipped"] == []
    assert len(calls) == 5
    # Per-package results stored with the spec's COMPLETED status.
    for pid in res["succeeded"]:
        assert res["packages"][pid]["status"] == sch.COMPLETED
        assert res["packages"][pid]["result"] == {"status": "completed"}


def test_run_scheduled_pipeline_failure_propagates_to_dependents():
    def fn(prompt):
        if "Type: research" in prompt:
            raise RuntimeError("research blew up")
        return {"status": "completed"}
    res = sch.run_scheduled_pipeline(GOOD_GRAPH, pipeline_fn=fn)
    assert res["failed"] == ["wp_research"]
    # Every other package depends (transitively) on research -> BLOCKED.
    assert set(res["skipped"]) == {"wp_arch", "wp_impl", "wp_val", "wp_doc"}
    assert res["succeeded"] == []
    for pid in res["skipped"]:
        assert res["packages"][pid]["status"] == sch.BLOCKED
        assert res["packages"][pid]["skipped_reason"] == "dependency_failed"


def test_run_scheduled_pipeline_mid_pipeline_failure():
    def fn(prompt):
        if "Type: implementation" in prompt:
            raise RuntimeError("impl blew up")
        return {"status": "completed"}
    res = sch.run_scheduled_pipeline(GOOD_GRAPH, pipeline_fn=fn)
    assert res["succeeded"] == ["wp_research", "wp_arch"]
    assert res["failed"] == ["wp_impl"]
    assert set(res["skipped"]) == {"wp_val", "wp_doc"}


def test_scoped_prompt_carries_parent_objective_and_fields():
    pkg = GOOD_GRAPH["work_packages"][1]  # wp_arch
    sp = sch.scoped_prompt(pkg, "ship X")
    assert "Parent objective: ship X" in sp
    assert "Architecture" in sp
    assert "architecture" in sp  # type
    assert "architecture.md" in sp  # deliverable


def test_on_package_callbacks_invoked():
    starts, completes = [], []
    def fn(prompt):
        return {"status": "completed"}
    res = sch.run_scheduled_pipeline(
        GOOD_GRAPH, pipeline_fn=fn,
        on_package_start=lambda pid: starts.append(pid),
        on_package_complete=lambda pid, r: completes.append((pid, r["status"])),
    )
    assert starts == ["wp_research", "wp_arch", "wp_impl", "wp_val", "wp_doc"]
    assert [pid for pid, _ in completes] == starts
    # The spec's COMPLETED status is the terminal "succeeded" state.
    assert all(s == sch.COMPLETED for _, s in completes)


# --- pipeline_runner.execute_scheduled --------------------------------------

def test_execute_scheduled_dispatches_per_package():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        def gen(prompt, module=""):
            return _good_json()
        def pipe(prompt):
            return {"status": "completed", "synthesized_answer": prompt[:60]}
        result = pr.execute_scheduled(
            "ship X", generate_fn=gen, pipeline_fn=pipe,
        )
        assert result["planner_error"] is None
        assert result["workspace_id"]
        assert result["graph"] is not None
        sched = result["scheduled"]
        assert sched["succeeded"] == [
            "wp_research", "wp_arch", "wp_impl", "wp_val", "wp_doc",
        ]
        assert result["plan"]["work_packages"] == GOOD_GRAPH["work_packages"]
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_execute_scheduled_resumes_existing_graph_without_replanning():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        gen_calls = {"n": 0}
        def gen(prompt, module=""):
            gen_calls["n"] += 1
            return _good_json()
        def pipe(prompt):
            return {"status": "completed"}
        r1 = pr.execute_scheduled("ship X", generate_fn=gen, pipeline_fn=pipe)
        gen_calls["n"] = 0
        r2 = pr.execute_scheduled(
            "ship X", workspace_id=r1["workspace_id"],
            generate_fn=gen, pipeline_fn=pipe,
        )
        assert r2["workspace_id"] == r1["workspace_id"]
        # Resume: no LLM call.
        assert gen_calls["n"] == 0
        assert r2["scheduled"]["succeeded"] == r1["scheduled"]["succeeded"]
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_execute_scheduled_returns_structured_failure_on_planner_error():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        def gen(prompt, module=""):
            return "not json"
        def pipe(prompt):
            return {"status": "completed"}
        result = pr.execute_scheduled(
            "ship X", generate_fn=gen, pipeline_fn=pipe, max_retries=2,
        )
        assert result["planner_error"] is not None
        assert result["graph"] is None
        assert result["scheduled"] is None
        # Planner error did not raise; orchestrator can still see workspace_id
        assert "workspace_id" in result
    finally:
        bw.WORKSPACE_ROOT = saved_root


# --- backward compatibility: existing execute() unchanged -------------------

def test_pipeline_runner_execute_signature_unchanged():
    import inspect
    sig = inspect.signature(pr.execute)
    params = list(sig.parameters)
    assert params == ["prompt", "memory_ids"], params


def test_existing_execute_attribute_still_importable():
    # The legacy entry point must continue to import and be callable.
    assert callable(pr.execute)
    assert callable(pr.log)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
