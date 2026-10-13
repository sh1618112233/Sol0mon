from datetime import datetime

# Telemetry & Runtime Observability V1 — observational only. The recorder is
# disabled by default and every emit/observe is a no-op when unconfigured, so
# existing behavior is unchanged. pipeline_runner is the outermost
# entrypoint (NOT debate decision logic), so it owns PipelineStarted /
# PipelineCompleted + the performance report generation.
from telemetry import recorder as _telemetry


def log(message):
    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[{timestamp}] {message}",
        flush=True
    )


def execute(prompt, memory_ids=None):

    log("PIPELINE STARTED")

    # Configure telemetry for this run (job_id generated). Default status /
    # report paths live in CWD so the live CLI path is observable; offline
    # tests inject ``debate_fn`` seams and never reach this call site, and
    # the recorder is a no-op when reset.
    import os
    _telemetry.configure(
        status_path=os.path.join(os.getcwd(), "workspace_status.json"),
        report_path=os.path.join(os.getcwd(), "performance_report.json"),
    )

    status = "failed"
    try:
        _telemetry.start_stage("Debate")
        from debate_engine import DebateEngine
        engine = DebateEngine()
        result = engine.run(question=prompt, memory_ids=memory_ids)
        status = result.get("status", "unknown")
        _telemetry.end_stage("Debate", status=status)
    finally:
        # Always emit completion + write the performance report, even on a
        # crash, so the user can see where time was spent. reset() leaves the
        # singleton disabled for the next run.
        _telemetry.emit("Completed", "Pipeline", status=status)
        _telemetry.save_report()
        _telemetry.reset()

    log("PIPELINE COMPLETE")

    return result


def execute_scheduled(
    prompt,
    *,
    memory_ids=None,
    generate_fn=None,
    pipeline_fn=None,
    force_replan=False,
    workspace_id=None,
    max_retries=3,
    mode_select_fn=None,
    max_workers=1,
    require_implementation=False,
):
    """Full integrated pipeline:

        Objective → Planner → Authority → Scheduler → Existing Pipeline.

    The first stage (PLANNER) decomposes ``prompt`` into a structured
    execution graph of work packages (idempotent: resumes from an existing
    graph unless ``force_replan=True``). The Authority (single deterministic
    precedence: override > policy > planner > default) resolves one
    Effective Execution Decision and logs both the Policy Decision and the
    Effective Decision. The Scheduler (GRAPH → SCHEDULER) dispatches work
    packages in dependency order, with the spec's exact statuses
    (PENDING/READY/RUNNING/COMPLETED/FAILED/BLOCKED). Each package's prompt
    is enriched with prior-stage knowledge from the workspace (research →
    architecture → implementation). Existing ``pipeline_runner.execute`` is
    unchanged and remains the single-package entry point for backward
    compatibility.

    Args:
        prompt:                The user's objective.
        memory_ids:            Optional memory context for the per-package calls.
        generate_fn:           Injected LLM seam for the planner (tests pass a
                               fake; production uses the provider router).
        pipeline_fn:           Injected per-package pipeline seam (tests pass a
                               fake; production uses ``execute``).
        force_replan:          Re-run the planner even if a valid graph is
                               already persisted. Default False = resume.
        workspace_id:          Optional workspace id; created automatically
                               when None (the planner owns its workspace).
        max_retries:           Planner LLM retry budget on malformed responses.
        mode_select_fn:        Optional override seam for the Execution
                               Authority (recommended_mode -> (selected_mode,
                               override_used)). Production passes a CLI prompt;
                               tests inject a lambda.
        max_workers:           Scheduler parallelism (default 1 = sequential,
                               matching the existing pipeline's side-effect
                               contract; >1 only when pipeline_fn is stateless).
        require_implementation: Force implementation packages to run even
                               when no open implementation decision remains
                               (default False = respect the debate-separation
                               optionality guard).

    Returns:
        {
          "workspace_id": str,
          "graph":        {...} | None,
          "plan":         {...} | None,
          "authority":    {...},            # the Effective Execution Decision
          "scheduled":    {...} | None,
          "planner_error": str | None,
          "stages":       [Stage, ...],    # the stage-logger audit trail
        }
    """
    from project_planner import (
        project_planner, load_execution_graph, _graph_path, PlannerError,
    )
    from pipeline_scheduler import run_scheduled_pipeline, COMPLETED, FAILED, BLOCKED
    import execution_authority as ea  # legacy reference; Stage 2 no longer uses it
    import stage_logger as sl
    import execution_decision as ed
    import execution_mode_gate as emg

    stages: list[dict] = []
    log("SCHEDULED PIPELINE STARTED")

    # -------- Stage 1: PLANNER -------------------------------------------------
    plan_error: str | None = None
    graph: dict | None = None
    try:
        with sl.stage("PLANNER", workspace_id=workspace_id or "") as s:
            workspace_id, graph = project_planner(
                prompt,
                workspace_id=workspace_id,
                force=force_replan,
                generate_fn=generate_fn,
                max_retries=max_retries,
            )
            s.workspace_id = workspace_id
            s.artifact = str(_graph_path(workspace_id))
            stages.append(s.to_dict())
    except PlannerError as e:
        plan_error = str(e)
        stages.append(s.to_dict())  # type: ignore[name-defined]
        log(f"[PLANNER] FAILED: {plan_error}")
        return {
            "workspace_id": workspace_id or "",
            "graph": None,
            "plan": None,
            "authority": None,
            "scheduled": None,
            "planner_error": plan_error,
            "stages": stages,
        }

    # -------- Stage 1b: GRAPH (validate + log) --------------------------------
    with sl.stage("GRAPH", workspace_id=workspace_id) as s:
        from project_planner import validate_execution_graph
        issues = validate_execution_graph(graph)
        if issues:
            raise RuntimeError(f"invalid graph persisted: {issues[:3]}")
        s.artifact = str(_graph_path(workspace_id))
        stages.append(s.to_dict())
    log(f"[GRAPH] {len((graph or {}).get('work_packages', []))} work packages validated")

    # -------- Stage 2: EXECUTION DECISION ENGINE (Effective Execution
    # Decision) --------------------------------------------------------------
    # Phase 7 — the new ExecutionDecisionEngine replaces the legacy
    # execution_authority. The verifier's emit is a RECOMMENDATION, not a
    # terminating decision (Rule 3 + Rule 4 of the engine contract): when the
    # user's prompt carries explicit implementation / architecture intent it
    # wins over a soft heuristic emit. Hard governance blocks
    # (STOP_UNRESOLVED_DISPUTES) are still honored.
    #
    # The engine writes the stage plan to execution/execution_plan.json (the
    # single source of truth downstream execution reads), plus the decision
    # / reason / state artifacts for resume / interruption recovery. We
    # translate the engine's ExecutionDecision into the legacy ``authority``
    # dict shape the scheduler / build_state still consume so the gate stays
    # the single live enforcer (no parallel gate is introduced).
    import json as _json
    from build_pipeline import _load_workspace_state, _load_execution_decision_block
    workspace_state = _load_workspace_state(workspace_id)
    verification_result = None
    try:
        from build_workspace import _workspace_dir
        ans_path = _workspace_dir(workspace_id) / "answer.json"
        if ans_path.is_file():
            verification_result = (
                _json.loads(ans_path.read_text(encoding="utf-8")) or {}
            ).get("verification_result")
    except Exception:
        pass

    from execution_decision_engine import (
        decide as _engine_decide, persist_decision as _persist_decision,
        log_decision as _log_decision, mode_to_legacy_decision as _mode_to_dec,
        reconcile_decision as _reconcile_decision,
        load_persisted_plan as _load_persisted_plan,
        RESEARCH_ONLY as _MODE_RO, FULL_BUILD as _MODE_FB,
    )
    try:
        decision = _engine_decide(
            user_prompt=prompt,
            planner_output=graph,
            verification_result=verification_result,
            mode_select_fn=mode_select_fn,
        )
    except Exception as _e:
        # Ponytail: never silently default to RESEARCH_ONLY. If the engine
        # rejects the inputs, surface the error rather than terminating.
        return {
            "workspace_id": workspace_id or "",
            "graph": graph,
            "plan": None,
            "authority": None,
            "scheduled": None,
            "planner_error": f"execution_decision_engine: {_e}",
            "stages": stages,
        }
    # Resume: carry completed stages forward from any prior persisted plan.
    prior_plan = _load_persisted_plan(workspace_id)
    if prior_plan is not None:
        decision = _reconcile_decision(decision, workspace_id)
    _persist_decision(workspace_id, decision)
    _log_decision(decision)

    # Translate the engine's ExecutionDecision into the legacy authority
    # dict shape so the existing scheduler / build_state contract holds
    # without a parallel gate (the spec's "no parallel gate" rule).
    legacy_dec = _mode_to_dec(decision.execution_mode)
    eff = {
        ed.KEY_DECISION: legacy_dec,
        ed.KEY_REASON: decision.reason,
        ed.KEY_CONFIDENCE: decision.confidence,
        ed.KEY_SOURCE: decision.source,
        "blocked": decision.blocked,
        "block_status": decision.block_status,
    }
    authority = {
        "effective_decision": eff,
        "effective_source": decision.source,
        "recommended_mode": decision.recommended_mode,
        # NEW: the structured stage plan the ExecutionPlanner persisted.
        "execution_decision_engine": decision.to_dict(),
    }
    if decision.selected_mode is not None:
        eff[emg.KEY_SELECTED_MODE] = decision.selected_mode
        eff[emg.KEY_RECOMMENDED_MODE] = decision.recommended_mode
        eff[emg.KEY_OVERRIDE_USED] = decision.override_used

    # If the effective decision HARD-STOPS before implementation, we still
    # dispatch the scheduler — but only the packages that the effective
    # depth permits. The scheduler's implementation-optionality guard will
    # BLOCK implementation packages when there are no open decisions, and
    # the engine's decision here feeds that gate (via the verified/
    # knowledge written by the architecture stage). For now we log and
    # proceed: the scheduler's depth-respecting skips handle the rest.
    if eff.get("blocked"):
        log(f"[AUTHORITY] execution is BLOCKED at this depth: "
            f"{eff.get('block_status')}")
    else:
        log(f"[AUTHORITY] effective decision: {eff.get(ed.KEY_DECISION)} "
            f"(mode={decision.execution_mode}, source={decision.source})")

    # Persist the authority's effective decision into build_state.json so a
    # resume sees the same authoritative decision without recomputing.
    try:
        from build_workspace import write_build_state
        write_build_state(
            workspace_id, "authority_resolved",
            execution_decision=eff.get(ed.KEY_DECISION),
            execution_reason=eff.get(ed.KEY_REASON, ""),
            execution_confidence=eff.get(ed.KEY_CONFIDENCE, 0),
            execution_decision_source=eff.get(ed.KEY_SOURCE, ""),
            effective_source=authority.get("effective_source"),
            recommended_mode=authority.get("recommended_mode"),
        )
    except Exception:
        pass

    # -------- Stage 3: SCHEDULER ----------------------------------------------
    if pipeline_fn is not None:
        per_package_fn = pipeline_fn
    else:
        def per_package_fn(pkg_prompt: str) -> dict:
            return execute(pkg_prompt, memory_ids=memory_ids)

    scheduler_stage_logs: list[dict] = []
    with sl.stage("SCHEDULER", workspace_id=workspace_id) as s:
        # Map package types to sub-stage tags so each package logs under
        # its own stage label (RESEARCH / ARCHITECTURE / IMPLEMENTATION).
        type_to_stage = {
            "research": "RESEARCH",
            "architecture": "ARCHITECTURE",
            "implementation": "IMPLEMENTATION",
            "validation": "TEST",
            "documentation": "BUILD",  # docs is the build's finalizing step
        }
        def _on_start(pid: str) -> None:
            ptype = next(
                (p.get("type") for p in (graph or {}).get("work_packages", [])
                 if isinstance(p, dict) and p.get("id") == pid), "",
            )
            stage_tag = type_to_stage.get(ptype, "SCHEDULER")
            sl.event(stage_tag, f"start {pid}")
            scheduler_stage_logs.append({"pid": pid, "stage": stage_tag,
                                         "event": "start"})

        def _on_complete(pid: str, result: dict) -> None:
            ptype = next(
                (p.get("type") for p in (graph or {}).get("work_packages", [])
                 if isinstance(p, dict) and p.get("id") == pid), "",
            )
            stage_tag = type_to_stage.get(ptype, "SCHEDULER")
            status = result.get("status", "unknown")
            sl.event(stage_tag, f"complete {pid} ({status})")
            scheduler_stage_logs.append({"pid": pid, "stage": stage_tag,
                                         "event": "complete",
                                         "status": status})

        sl.event("SCHEDULER", f"dispatching "
                  f"{len((graph or {}).get('work_packages', []))} work packages "
                  f"(max_workers={max_workers})")
        scheduled = run_scheduled_pipeline(
            graph,
            pipeline_fn=per_package_fn,
            on_package_start=_on_start,
            on_package_complete=_on_complete,
            workspace_id=workspace_id,
            max_workers=max_workers,
            require_implementation=require_implementation,
        )
        s.artifact = f"{len(scheduled['succeeded'])} completed"
        stages.append(s.to_dict())
        stages.extend(scheduler_stage_logs)

    log(
        f"SCHEDULED PIPELINE COMPLETE: "
        f"{len(scheduled['succeeded'])} succeeded, "
        f"{len(scheduled['failed'])} failed, "
        f"{len(scheduled['skipped'])} skipped"
    )
    return {
        "workspace_id": workspace_id,
        "graph": graph,
        "plan": load_execution_graph(workspace_id),
        "authority": authority,
        "scheduled": scheduled,
        "planner_error": None,
        "stages": stages,
    }


if __name__ == "__main__":
    # Offline self-check: classify-only path (no browser, no LLM, no debate).
    result = execute("Test prompt")
    print(f"execute() smoke: {result.get('status', 'unknown')}")

