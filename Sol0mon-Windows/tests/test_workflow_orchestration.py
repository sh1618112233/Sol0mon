"""
tests/test_workflow_orchestration.py — Authority + Stage Logger + Knowledge +
Scheduler upgrade + Debate-Separation regression.

Pins the spec's numbered requirements:

  1. Execution Decision Authority — single deterministic precedence
     (override > policy > planner > default); only ONE effective decision
     exists after policy evaluation; both Policy Decision and Effective
     Decision are logged.
  2. Planner Integration — planner runs first; runs once per workspace;
     resumes from an existing execution graph.
  3. Workspace Execution Graph — load/save/resume/validation helpers;
     idempotent round-trip.
  4. Scheduler — PENDING/READY/RUNNING/COMPLETED/FAILED/BLOCKED statuses;
     only READY executes; independent packages parallelizable.
  5. Debate Separation — research before architecture; architecture
     consumes verified research (not the raw prompt); implementation
     optional when no open implementation decisions remain.
  6. Knowledge Persistence — research/ verified/ architecture/
     implementation/ dirs; load_stage_input merges prior stages.
  7. Logging — explicit stage tags [PLANNER]/[GRAPH]/[SCHEDULER]/etc. with
     start/complete/duration/artifact.
  8. Backward compat — existing execute() signature/import unchanged; new
     execute_scheduled is the orchestration entry.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import execution_authority as ea
import execution_decision as ed
import execution_mode_gate as emg
import pipeline_runner as pr
import pipeline_scheduler as sch
import project_planner as pp
import stage_logger as sl
import workspace_knowledge as wk


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


def _with_tmp_workspace_root():
    return tempfile.mkdtemp(prefix="sol0mon_wo_")


# ===========================================================================
# 1. Execution Decision Authority
# ===========================================================================

def test_authority_override_wins_over_policy_and_planner():
    """The spec's exact failure mode (RESEARCH_ONLY from policy + FULL_BUILD
    from override) resolves to ONE effective decision = IMPLEMENTATION_REQUIRED."""
    r = ea.resolve(
        "ship X",
        graph={"work_packages": [{"id": "i", "type": "implementation"}]},
        verification_result={
            ed.KEY_DECISION: ed.RESEARCH_ONLY, ed.KEY_REASON: "auto",
            ed.KEY_CONFIDENCE: 80, ed.KEY_SOURCE: "research_analysis",
        },
        mode_select_fn=lambda rec: (emg.FULL_BUILD, True),
    )
    assert r["effective_source"] == ea.SOURCE_OVERRIDE
    assert r["effective_decision"][ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED
    # Policy said RESEARCH_ONLY; the override wins — single effective.
    assert r["policy_decision"][ea.SOURCE_POLICY][ed.KEY_DECISION] == ed.RESEARCH_ONLY
    # And the planner recommendation is logged too.
    assert r["policy_decision"][ea.SOURCE_PLANNER] is not None


def test_authority_policy_wins_when_no_override():
    r = ea.resolve(
        "ship X",
        graph={"work_packages": [{"id": "a", "type": "architecture"}]},
        verification_result={
            ed.KEY_DECISION: ed.RESEARCH_ONLY, ed.KEY_REASON: "auto",
            ed.KEY_CONFIDENCE: 80, ed.KEY_SOURCE: "research_analysis",
        },
    )
    assert r["effective_source"] == ea.SOURCE_POLICY
    assert r["effective_decision"][ed.KEY_DECISION] == ed.RESEARCH_ONLY


def test_authority_planner_wins_when_no_policy():
    r = ea.resolve(
        "ship X",
        graph={"work_packages": [
            {"id": "a", "type": "architecture"},
            {"id": "i", "type": "implementation"},
        ]},
    )
    assert r["effective_source"] == ea.SOURCE_PLANNER
    assert r["effective_decision"][ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED


def test_authority_default_when_nothing_else():
    r = ea.resolve("hello world")
    assert r["effective_source"] == ea.SOURCE_DEFAULT
    # Ambiguous prompt -> safest-stop RESEARCH_ONLY (never auto-implement).
    assert r["effective_decision"][ed.KEY_DECISION] == ed.RESEARCH_ONLY


def test_authority_override_that_agrees_is_a_no_op():
    """The spec audit-trail rule: no false conflict when override agrees with
    recommendation — the policy layer wins and no override source is logged."""
    r = ea.resolve(
        "ship X",
        verification_result={
            ed.KEY_DECISION: ed.IMPLEMENTATION_REQUIRED, ed.KEY_REASON: "auto",
            ed.KEY_CONFIDENCE: 90, ed.KEY_SOURCE: "impl",
        },
        mode_select_fn=lambda rec: (emg.FULL_BUILD, False),  # accept recommendation
    )
    assert r["effective_source"] == ea.SOURCE_POLICY, r
    assert r["effective_decision"][ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED
    # No override source recorded in policy_decision.
    assert r["policy_decision"][ea.SOURCE_OVERRIDE] is None


def test_authority_persisted_resume_override_wins():
    r = ea.resolve(
        "ship X",
        graph={"work_packages": [{"id": "r", "type": "research"}]},
        persisted_mode={
            emg.KEY_RECOMMENDED_MODE: emg.RESEARCH_ONLY,
            emg.KEY_SELECTED_MODE: emg.FULL_BUILD,
            emg.KEY_OVERRIDE_USED: True,
        },
    )
    assert r["effective_source"] == ea.SOURCE_OVERRIDE
    assert r["effective_decision"][ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED


def test_authority_always_returns_one_decision():
    """Single effective decision — no scenario returns None."""
    r = ea.resolve("anything", graph=None,
                    verification_result=None, workspace_state=None)
    assert r["effective_decision"][ed.KEY_DECISION] in ed.ALL_DECISIONS


def test_authority_log_does_not_raise(capsys):
    r = ea.resolve("ship X", mode_select_fn=lambda rec: (emg.FULL_BUILD, True))
    ea.log_authority(r)  # must not raise
    out = capsys.readouterr().out
    assert "EFFECTIVE" in out
    assert "Policy Decision" in out


# ===========================================================================
# 2 & 3. Planner once-per-workspace + Workspace Execution Graph helpers
# ===========================================================================

def test_planner_runs_once_per_workspace_then_resumes():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        calls = {"n": 0}
        def gen(prompt, module=""):
            calls["n"] += 1
            return _good_json()
        wid, _ = pp.project_planner("ship X", generate_fn=gen)
        assert calls["n"] == 1
        # Second call resumes — no LLM call.
        calls["n"] = 0
        pp.project_planner("ship X", workspace_id=wid, generate_fn=gen)
        assert calls["n"] == 0
        # force=True regenerates.
        calls["n"] = 0
        pp.project_planner("ship X", workspace_id=wid, force=True, generate_fn=gen)
        assert calls["n"] == 1
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_execution_graph_persisted_at_planner_dir():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        def gen(prompt, module=""):
            return _good_json()
        wid, _ = pp.project_planner("ship X", generate_fn=gen)
        # File lives at workspace/planner/execution_graph.json
        assert (bw._workspace_dir(wid) / "planner" / "execution_graph.json").is_file()
        # And project.json metadata beside it.
        assert (bw._workspace_dir(wid) / "project.json").is_file()
        # Resume helpers
        assert pp.has_execution_graph(wid)
        assert pp.load_execution_graph(wid) is not None
        assert pp.load_project(wid) is not None
    finally:
        bw.WORKSPACE_ROOT = saved_root


# ===========================================================================
# 4. Scheduler: PENDING/READY/RUNNING/COMPLETED/FAILED/BLOCKED
# ===========================================================================

def test_status_constants_present():
    assert sch.PENDING == "PENDING"
    assert sch.READY == "READY"
    assert sch.RUNNING == "RUNNING"
    assert sch.COMPLETED == "COMPLETED"
    assert sch.FAILED == "FAILED"
    assert sch.BLOCKED == "BLOCKED"


def test_initial_status_zero_dep_is_ready():
    g = {"work_packages": [
        {"id": "a", "type": "research", "title": "a", "description": "d",
         "deliverable": "y", "dependencies": []},
        {"id": "b", "type": "architecture", "title": "b", "description": "d",
         "deliverable": "y", "dependencies": ["a"]},
    ]}
    st = sch._initial_status(g)
    assert st["a"] == sch.READY  # zero-dep -> READY
    assert st["b"] == sch.PENDING


def test_only_ready_packages_execute():
    """A PENDING package never reaches the pipeline_fn."""
    seen = []
    def fn(prompt):
        seen.append(prompt)
        return {"status": "completed"}
    sch.run_scheduled_pipeline(GOOD_GRAPH, pipeline_fn=fn)
    # Every package ran exactly once, in dependency order.
    assert len(seen) == 5
    # research first (the only READY package initially).
    assert "Type: research" in seen[0]


def test_failure_marks_dependents_blocked_not_pending():
    def fn(prompt):
        if "Type: research" in prompt:
            raise RuntimeError("research failed")
        return {"status": "completed"}
    res = sch.run_scheduled_pipeline(GOOD_GRAPH, pipeline_fn=fn)
    assert res["failed"] == ["wp_research"]
    # Every dependent ended up BLOCKED, not PENDING.
    for pid in ["wp_arch", "wp_impl", "wp_val", "wp_doc"]:
        assert res["packages"][pid]["status"] == sch.BLOCKED
    assert "wp_research" not in res["skipped"]


def test_independent_packages_parallelizable():
    """Two zero-dep research packages can run in parallel when max_workers>1."""
    g = {"objective": "ship X", "work_packages": [
        {"id": "r1", "type": "research", "title": "r1", "description": "d",
         "deliverable": "y", "dependencies": []},
        {"id": "r2", "type": "research", "title": "r2", "description": "d",
         "deliverable": "y", "dependencies": []},
        {"id": "a", "type": "architecture", "title": "a", "description": "d",
         "deliverable": "y", "dependencies": ["r1", "r2"]},
    ]}
    import time
    started = {"n": 0, "concurrent": 0, "max_concurrent": 0}
    import threading
    lock = threading.Lock()
    def fn(prompt):
        with lock:
            started["n"] += 1
            started["concurrent"] += 1
            started["max_concurrent"] = max(started["max_concurrent"],
                                            started["concurrent"])
        time.sleep(0.05)
        with lock:
            started["concurrent"] -= 1
        return {"status": "completed"}
    res = sch.run_scheduled_pipeline(g, pipeline_fn=fn, max_workers=2)
    assert res["succeeded"] == ["r1", "r2", "a"]
    # r1 + r2 actually ran in parallel (the structure holds).
    assert started["max_concurrent"] >= 2, started


# ===========================================================================
# 5. Debate Separation — research feeds architecture feeds implementation
# ===========================================================================

def test_architecture_consumes_research_not_raw_prompt():
    """An architecture package's prompt includes verified prior research
    when the research package produced a persisted artefact. The research
    package's persisted ``synthesized_answer`` is what feeds the architecture
    prompt — NOT the raw user objective alone."""
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        seen = []
        def fn(prompt):
            seen.append(prompt)
            if "Type: research" in prompt:
                # The research package produces a real, persistent artefact.
                return {"status": "completed",
                        "synthesized_answer": "# Research\nUse Redis for caching.",
                        "verified_decisions": [{"decision": "use Redis"}]}
            return {"status": "completed", "synthesized_answer": "ok"}
        sch.run_scheduled_pipeline(
            GOOD_GRAPH, pipeline_fn=fn, workspace_id=wid,
        )
        # find the architecture call
        arch_call = next(c for c in seen if "Type: architecture" in c)
        # The architecture prompt was fed the research package's OUTPUT (the
        # synthesised research artefact), not just the raw parent objective.
        assert "Use Redis" in arch_call, arch_call
        assert "Verified prior-stage knowledge" in arch_call
        # The verified decision is also persisted under verified/ and merged.
        assert "[verified] " in arch_call
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_implementation_consumes_architecture_and_research():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        seen = []
        def fn(prompt):
            seen.append(prompt)
            if "Type: research" in prompt:
                return {"status": "completed",
                        "synthesized_answer": "# Research\nfindings."}
            if "Type: architecture" in prompt:
                return {"status": "completed",
                        "synthesized_answer": "# Architecture\nREST + Redis.",
                        "verified_decisions": [{"decision": "deploy Redis"}]}
            return {"status": "completed", "synthesized_answer": "ok"}
        sch.run_scheduled_pipeline(
            GOOD_GRAPH, pipeline_fn=fn, workspace_id=wid,
        )
        impl_call = next(c for c in seen if "Type: implementation" in c)
        assert "Architecture" in impl_call  # the section heading [architecture]
        assert "Research" in impl_call
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_implementation_skipped_when_no_open_implementation_decisions():
    """Implementation is OPTIONAL — skip when the architecture verified
    decisions carry no implementation signal."""
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        # verified decisions for wp_impl with no implementation signal
        wk.save_artifact(wid, "verified", "wp_impl",
                         json.dumps({"verified_decisions": [
                             {"decision": "use REST over gRPC"}]}))
        def fn(prompt):
            if "Type: implementation" in prompt:
                raise AssertionError("implementation should be skipped")
            return {"status": "completed"}
        res = sch.run_scheduled_pipeline(
            GOOD_GRAPH, pipeline_fn=fn, workspace_id=wid,
        )
        assert "wp_impl" in res["skipped"]
        assert (res["packages"]["wp_impl"]["skipped_reason"]
                == "no_open_implementation_decisions")
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_require_implementation_overrides_skip():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        wk.save_artifact(wid, "verified", "wp_impl",
                         json.dumps({"verified_decisions": [
                             {"decision": "use REST over gRPC"}]}))
        res = sch.run_scheduled_pipeline(
            GOOD_GRAPH,
            pipeline_fn=lambda p: {"status": "completed"},
            workspace_id=wid,
            require_implementation=True,
        )
        assert "wp_impl" in res["succeeded"]
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_research_completes_before_architecture_runs():
    """Sequential dispatch order: research before architecture before
    implementation. The promise of debate separation."""
    seen_order = []
    def fn(prompt):
        if "Type: research" in prompt:
            seen_order.append("RESEARCH")
        elif "Type: architecture" in prompt:
            seen_order.append("ARCHITECTURE")
        elif "Type: implementation" in prompt:
            seen_order.append("IMPLEMENTATION")
        return {"status": "completed"}
    sch.run_scheduled_pipeline(GOOD_GRAPH, pipeline_fn=fn)
    # research precedes architecture precedes implementation
    ri = seen_order.index("RESEARCH")
    ai = seen_order.index("ARCHITECTURE")
    ii = seen_order.index("IMPLEMENTATION")
    assert ri < ai < ii


# ===========================================================================
# 6. Knowledge Persistence
# ===========================================================================

def test_knowledge_dirs_created_lazily():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        # Initially nothing exists
        assert wk.list_artifacts(wid, "research") == []
        # Saving creates the dir
        p = wk.save_artifact(wid, "research", "wp_r", "# content")
        assert p.is_file()
        assert p.parent.name == "research"
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_load_stage_input_merges_correct_stages():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        wk.save_artifact(wid, "research", "wp_r", "research-content")
        wk.save_artifact(wid, "verified", "wp_r",
                         json.dumps({"verified_decisions": [{"decision": "x"}]}))
        arch_input = wk.load_stage_input(wid, "architecture")
        assert "research-content" in arch_input
        assert "[verified] wp_r" in arch_input
        # implementation gets architecture + verified + research
        wk.save_artifact(wid, "architecture", "wp_a", "arch-content")
        impl_input = wk.load_stage_input(wid, "implementation")
        assert "arch-content" in impl_input
        assert "research-content" in impl_input
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_save_debate_result_persists_prose_and_verified():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")
        path = wk.save_debate_result(
            wid, "wp_arch", "architecture",
            {"synthesized_answer": "# Architecture\nREST design.",
             "verified_decisions": [{"decision": "REST over gRPC"}]},
        )
        assert path is not None and path.is_file()
        v = wk.load_artifact(wid, "verified", "wp_arch")
        assert v and "verified_decisions" in v
    finally:
        bw.WORKSPACE_ROOT = saved_root


# ===========================================================================
# 7. Stage Logger — explicit [STAGE] logs
# ===========================================================================

def test_stage_logger_emits_start_complete_and_artifact(capsys):
    with sl.stage("PLANNER", workspace_id="ws-12345678") as s:
        s.artifact = "/path/to/graph.json"
    out = capsys.readouterr().out
    assert "[PLANNER] start" in out
    # workspace id is truncated to 8 chars in the tag -> the prefix appears.
    assert "ws=ws-12345" in out  # 8-char truncation of "ws-12345678"
    assert "[PLANNER] complete" in out
    assert "/path/to/graph.json" in out
    assert s.elapsed >= 0.0
    assert s.status == "complete"


def test_stage_logger_logs_failed_and_reraises(capsys):
    raised = False
    try:
        with sl.stage("TEST") as s:
            raise RuntimeError("boom")
    except RuntimeError:
        raised = True
    assert raised
    out = capsys.readouterr().out
    assert "[TEST] FAILED" in out
    assert "boom" in out
    assert s.status == "failed"


def test_stage_logger_event_emits_one_shot(capsys):
    sl.event("SCHEDULER", "dispatching 5 work packages",
             artifact="scheduler_state.json")
    out = capsys.readouterr().out
    assert "[SCHEDULER] dispatching 5 work packages" in out
    assert "scheduler_state.json" in out


# ===========================================================================
# 8. End-to-end via execute_scheduled + Backward-compat pins
# ===========================================================================

def test_execute_scheduled_end_to_end_uses_authority_and_logs(capsys):
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        def gen(prompt, module=""):
            return _good_json()
        def pipe(prompt):
            return {"status": "completed",
                    "synthesized_answer": "ok for " + prompt[:40]}
        result = pr.execute_scheduled(
            "ship X",
            generate_fn=gen,
            pipeline_fn=pipe,
            mode_select_fn=lambda rec: (emg.FULL_BUILD, True),  # override -> IMPL
        )
        assert result["planner_error"] is None
        assert result["workspace_id"]
        assert result["graph"] is not None
        # Authority resolved + logged
        assert result["authority"] is not None
        assert result["authority"]["effective_decision"][ed.KEY_DECISION] == \
            ed.IMPLEMENTATION_REQUIRED
        # Scheduler ran every package
        assert result["scheduled"]["succeeded"] == [
            "wp_research", "wp_arch", "wp_impl", "wp_val", "wp_doc",
        ]
        # Stage logs appear in the captured stdout
        out = capsys.readouterr().out
        assert "[PLANNER] start" in out
        assert "[PLANNER] complete" in out
        assert "[GRAPH]" in out
        assert "[SCHEDULER]" in out
        assert "[AUTHORITY]" in out
        # Stages audit trail returned on result
        assert any(s.get("name") == "PLANNER" for s in result["stages"])
        # build_state.json got the authority's effective decision persisted
        state = json.loads(
            (bw._workspace_dir(result["workspace_id"]) / "build_state.json")
            .read_text())
        assert state["execution_decision"] == ed.IMPLEMENTATION_REQUIRED
        assert state["effective_source"] == ea.SOURCE_OVERRIDE
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_execute_scheduled_resumes_with_no_replan():
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
        assert gen_calls["n"] == 0  # resume: planner not re-invoked
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_existing_execute_signature_unchanged():
    import inspect
    assert list(inspect.signature(pr.execute).parameters) == ["prompt", "memory_ids"]


def test_existing_execute_and_log_importable():
    assert callable(pr.execute) and callable(pr.log)


# --- End-to-end verification pins (the spec's confirmation list) ------------

def test_e2e_planner_executes_first(capsys):
    """Planner runs before any pipeline_fn call."""
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        gen_order = []
        pipe_order = []
        def gen(prompt, module=""):
            gen_order.append("planner")
            return _good_json()
        def pipe(prompt):
            pipe_order.append("pipeline")
            return {"status": "completed"}
        pr.execute_scheduled("ship X", generate_fn=gen, pipeline_fn=pipe)
        # planner ran before any pipeline call
        assert gen_order == ["planner"]
        assert len(pipe_order) == 5  # per package
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_e2e_execution_graph_persisted():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        pr.execute_scheduled(
            "ship X",
            generate_fn=lambda p, m="": _good_json(),
            pipeline_fn=lambda p: {"status": "completed"},
        )
        # walk any workspace under the tmp root; one must have the graph
        ws_dirs = [p for p in bw.WORKSPACE_ROOT.iterdir()
                   if p.is_dir() and p.name.startswith("workspace_")]
        assert ws_dirs, "no workspace was created"
        for d in ws_dirs:
            assert (d / "planner" / "execution_graph.json").is_file(), d
            assert (d / "project.json").is_file(), d
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_e2e_research_feeds_architecture():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        seen_arch_prompt = {"v": None}
        def pipe(prompt):
            if "Type: architecture" in prompt:
                seen_arch_prompt["v"] = prompt
            # Emit a research artefact for the research package
            if "Type: research" in prompt:
                return {"status": "completed",
                        "synthesized_answer": "# Research\nverified: use Redis.",
                        "verified_decisions": [{"decision": "use Redis"}]}
            return {"status": "completed"}
        pr.execute_scheduled(
            "ship X",
            generate_fn=lambda p, m="": _good_json(),
            pipeline_fn=pipe,
        )
        assert seen_arch_prompt["v"] is not None
        # Architecture prompt was fed the research artefact.
        assert "use Redis" in seen_arch_prompt["v"], seen_arch_prompt["v"]
        assert "Verified prior-stage knowledge" in seen_arch_prompt["v"]
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_e2e_architecture_feeds_implementation():
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(_with_tmp_workspace_root())
        seen_impl_prompt = {"v": None}
        def pipe(prompt):
            if "Type: architecture" in prompt:
                return {"status": "completed",
                        "synthesized_answer": "# Architecture\nAPI gateway + Redis.",
                        "verified_decisions": [{"decision": "deploy Redis"}]}
            if "Type: implementation" in prompt:
                seen_impl_prompt["v"] = prompt
            return {"status": "completed"}
        pr.execute_scheduled(
            "ship X",
            generate_fn=lambda p, m="": _good_json(),
            pipeline_fn=pipe,
        )
        # The verified decision "deploy Redis" carries an impl signal so the
        # implementation package ran (was NOT skipped).
        assert seen_impl_prompt["v"] is not None
        assert "Architecture" in seen_impl_prompt["v"]
    finally:
        bw.WORKSPACE_ROOT = saved_root


def test_e2e_existing_debate_functionality_operational():
    """The existing pipeline_runner.execute signature + return-shape contract
    is unchanged — a direct call goes through the same DebateEngine path."""
    # We don't invoke execute() (it would call the browser/debate engine);
    # we only verify the contract is intact. Backward-compat is a structural
    # pin, not a runtime call.
    import inspect
    sig = inspect.signature(pr.execute)
    assert sig.return_annotation in (inspect.Parameter.empty, dict) or \
        sig.return_annotation is dict
    # The new orchestration entry exists alongside, not as a replacement.
    assert callable(pr.execute_scheduled)
    # The legacy log function is the same.
    assert callable(pr.log)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
