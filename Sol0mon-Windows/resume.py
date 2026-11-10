"""
Project Sol0mon - Resume Engine (Final Architecture Hardening - Phase G).

A single ``resume_build(workspace_id)`` entry point that determines the
resume point from (1) the lifecycle state, (2) validated artifacts, (3)
completed tasks, and continues on the SAME workspace — never restarting from
the beginning unnecessarily.

Why not just re-call ``run_build_pipeline``? Because ``resurrect_answer``
calls ``create_workspace`` which mints a NEW workspace_id every time — there is
no way to resume the same workspace by re-running the top-level pipeline. The
resumable units are the STAGE functions (``generate_build_spec``,
``execute_build_spec``) which operate on an existing ``workspace_id``. Resume
dispatches to those, and the existing per-module resume logic takes over:
  - the execution_decision is immutable (persisted in build_state.json —
    ``_load_execution_decision_block`` reads it without reclassifying)
  - the repair loop resumes from ``_load_build_state`` (repair_attempts /
    repair_log checkpoint; a prior exhausted run is re-emitted, never re-run)

Three public entry points (the spec's three resume modes):
    resume_build(workspace_id, use_claude_code=False, force=False) -> dict
    resume_execution(workspace_id, use_claude_code=False) -> dict
    resume_validation(workspace_id) -> dict

Resume is SAFE to call offline (``use_claude_code=False`` default — the same
default as ``run_build_pipeline``): execution is skipped cleanly if no agent
is requested, so a resume on a SPEC_READY workspace just re-runs the (cache
of the) spec + planning + validation, never a model call. ``force=True``
restarts a FAILED build from SPEC_READY (the only stage where a deterministic
re-run is safe).

Every resumed stage advances the lifecycle via ``advance_lifecycle`` so
``lifecycle_stage`` is populated over time and the next resume reads it
directly (no more legacy-status mapping once the build has run under this
layer).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from lifecycle import (
    BuildLifecycle, current_lifecycle, advance_lifecycle,
    lifecycle_stage_from_status,
)


def _read_workspace_state(workspace_id: str) -> dict:
    from build_workspace import _workspace_dir
    if not workspace_id:
        return {}
    p = _workspace_dir(workspace_id) / "build_state.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _read_decisions_model(workspace_id: str) -> dict:
    """Read the authoritative ``decisions.json`` for a workspace. The canonical
    decision model lives here (build_pipeline.resurrect_answer wrote it). The
    planner / spec re-use this file so resume never re-parses prose."""
    from build_workspace import _workspace_dir
    p = _workspace_dir(workspace_id) / "decisions.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _read_source_answer_text(workspace_id: str) -> str:
    from build_workspace import _workspace_dir
    for name in ("answer.json",):
        p = _workspace_dir(workspace_id) / name
        if p.is_file():
            try:
                a = json.loads(p.read_text(encoding="utf-8")) or {}
                return a.get("synthesized_answer", "") or ""
            except (json.JSONDecodeError, OSError):
                pass
    # Fall back to the markdown sidecar (kept for back-compat).
    p = _workspace_dir(workspace_id) / "synthesized_answer.md"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _shape_source_result(decisions: dict, answer_text: str) -> dict:
    """Reshape ``decisions.json`` into the ``source_result`` form
    ``build_spec_engine.build_spec`` consumes (verified_decisions /
    verification_result / synthesized_answer). The decisions.json IS the
    canonical model — single source of truth, never re-parsed prose."""
    return {
        "verified_decisions": decisions.get("verified_decisions") or [],
        "verified_tradeoffs": [],
        "human_required": [],
        "verification_result": decisions.get("verification_result") or {},
        "resolved_decisions": decisions.get("resolved_decisions") or [],
        "synthesized_answer": answer_text,
    }


def _pending_human_count(workspace_id: str) -> int:
    try:
        from human_decision_manager import load_pending
        return len(load_pending(workspace_id) or [])
    except Exception:
        return 0


def _block_status_of(state: dict) -> str:
    """The block_reason that put us in BLOCKED ("" if none). Used to decide
    whether to drop the block on resume (e.g. pending humans resolved)."""
    return state.get("blocked_reason") or state.get("block_status") or ""


def resume_build(
    workspace_id: str,
    *,
    use_claude_code: bool = False,
    force: bool = False,
    staged_agent_fn: Any = None,
    staged_test_fn: Any = None,
) -> dict:
    """Resume an existing build from the last valid stage. Manifest-aware
    (Final Architecture Hardening — Part 6):

        manifest  ->  artifacts  ->  lifecycle  ->  resume point

    The manifest is loaded first; the on-disk deterministic artifacts (build_spec
    at ``implementation_plan.md``, execution plan at ``execution_plan.json``)
    are reused whenever their checksums match the manifest's record. Only
    missing-OR-invalid artifacts are recomputed (deterministic Path 3 spec +
    deterministic ``execution_planner.plan``). Existing tests that pin a
    manifest-LESS resume keep working: when no manifest exists on disk, the
    path is identical to the pre-Part-6 lifecycle-keyed resume.

    Staged FULL_BUILD resume (extension): when the workspace has a staged
    build plan (``stage_status.has_staged_layout`` returns True), the
    dispatch routes to ``_resume_staged_build`` for stage-aware resume —
    skips DONE stages, continues the unfinished stage's repair loop,
    runs integration validation once all stages are DONE. The
    ``staged_agent_fn`` / ``staged_test_fn`` seams are forwarded to the
    staged executor for offline test scenarios; production leaves them
    None. When the staged plan is absent (legacy / single-stage), the
    existing lifecycle-keyed dispatch is byte-for-byte unchanged.

    Returns a structured resume report. See module docstring for the dispatch
    table."""
    if not workspace_id:
        return {"error": "no workspace_id", "resumed": False}

    # Staged dispatch: a workspace with a staged build plan gets a
    # stage-aware resume (continue from the next pending stage). This runs
    # BEFORE the lifecycle dispatch so a partially-staged workspace (some
    # stages DONE, some PENDING, build-level EXECUTING) continues correctly.
    # The legacy single-stage path is preserved when no staged plan exists.
    try:
        import stage_status
        if stage_status.has_staged_layout(workspace_id):
            return _resume_staged_build(workspace_id,
                                          use_claude_code=use_claude_code,
                                          force=force,
                                          staged_agent_fn=staged_agent_fn,
                                          staged_test_fn=staged_test_fn)
    except Exception as e:
        # Staged-resume dispatch failure is non-fatal: fall through to the
        # lifecycle-keyed path. The structured resume report will surface
        # the build-level state; the operator can re-run.
        print(f"[RESUME] staged dispatch skipped: {e}")

    # Part 6 — manifest-aware resume. Load the manifest (None if absent, e.g.
    # a workspace created before this layer) and derive the resume point from
    # (manifest, on-disk artifacts, lifecycle). The lifecycle stage is still
    # the authoritative resume signal — the manifest points at artifacts but
    # cannot replace the build_state's record of the live stage.
    from manifest import load_manifest, BuildManifest
    manifest = load_manifest(workspace_id)

    stage = current_lifecycle(workspace_id)
    state = _read_workspace_state(workspace_id)

    # Terminal states — nothing to resume.
    if stage is BuildLifecycle.COMPLETE:
        return {"resumed": False, "reason": "already_complete",
                "lifecycle_stage": stage.value,
                "manifest_present": manifest is not None,
                "workspace_id": workspace_id}
    if stage is BuildLifecycle.ABORTED:
        return {"resumed": False, "reason": "aborted",
                "lifecycle_stage": stage.value, "workspace_id": workspace_id}

    # Pass the manifest through to the stage functions so they reuse deterministic
    # artifacts instead of regenerating them.
    if stage is BuildLifecycle.FAILED:
        if not force:
            return {"resumed": False, "reason": "failed_no_force",
                     "lifecycle_stage": stage.value,
                     "status": state.get("status"),
                     "suggested_action": ("call resume_build(..., force=True) to "
                                          "retry from SPEC_READY, or fix the "
                                          "failure cause first"),
                     "workspace_id": workspace_id}
        # force: restart from SPEC_READY (the only safe deterministic re-run).
        return _resume_from_spec_ready(workspace_id, use_claude_code, state,
                                        manifest=manifest)

    if stage is BuildLifecycle.BLOCKED:
        pending_n = _pending_human_count(workspace_id)
        if pending_n > 0:
            return {"resumed": False, "reason": "blocked_pending_humans",
                    "lifecycle_stage": stage.value,
                    "block_reason": _block_status_of(state),
                    "pending_count": pending_n,
                    "suggested_action": "resolve_human_requirement then resume_build",
                    "workspace_id": workspace_id}
        if _block_status_of(state) == "incomplete_spec":
            return _resume_from_spec_ready(workspace_id, use_claude_code, state,
                                          manifest=manifest)
        return _resume_from_execution_allowed(workspace_id, use_claude_code, state,
                                                manifest=manifest)

    if stage in (BuildLifecycle.NEW, BuildLifecycle.VERIFIED):
        return _resume_from_spec_ready(workspace_id, use_claude_code, state,
                                       manifest=manifest)

    if stage in (BuildLifecycle.SPEC_READY, BuildLifecycle.EXECUTION_ALLOWED):
        return _resume_from_execution_allowed(workspace_id, use_claude_code, state,
                                                manifest=manifest)

    if stage in (BuildLifecycle.EXECUTING, BuildLifecycle.ARTIFACTS_VALIDATED,
                 BuildLifecycle.TESTS_VALIDATED, BuildLifecycle.REPAIRING):
        # Re-enter the repair loop; _load_build_state resumes from the checkpoint.
        return _resume_from_execution_allowed(workspace_id, use_claude_code, state,
                                                manifest=manifest)

    # Should be unreachable (every state handled above) but never infer.
    return {"resumed": False, "reason": "unknown_lifecycle_stage",
            "lifecycle_stage": stage.value, "workspace_id": workspace_id}


def _sha256_text(text: str) -> str:
    import hashlib as _h
    return _h.sha256((text or "").encode("utf-8")).hexdigest()


def _load_or_rebuild_spec_result(
    workspace_id: str,
    manifest: "BuildManifest | None",
    answer_text: str,
    source_result: dict,
) -> dict:
    """Final Architecture Hardening — Part 6. Reuse the on-disk
    ``implementation_plan.md`` when its sha256 matches the manifest's recorded
    ``build_spec`` checksum; otherwise regenerate deterministically (Path 3,
    ``debate_fn=None``) and persist. Returns the spec_result dict in either
    case, with ``spec_text`` populated.

    When ``manifest`` is None (a pre-Part-6 workspace or one where the manifest
    hasn't been written), this always regenerates — preserving the prior
    resume behavior byte-for-byte.
    """
    from build_spec_engine import build_spec, load_implementation_spec, store_implementation_spec

    spec_text = load_implementation_spec(workspace_id)
    on_disk_ck = _sha256_text(spec_text) if spec_text else ""
    manifest_ck = manifest.checksum_of("build_spec") if manifest else ""

    if (manifest is not None and manifest_ck and on_disk_ck
            and on_disk_ck == manifest_ck):
        # Reuse: synthesize a minimal spec_result keyed off the on-disk text
        # + the canonical verified_decisions from source_result (which came
        # from decisions.json — the single source of truth).
        return {
            "spec_text": spec_text,
            "prompt": "",
            "decisions": source_result.get("verified_decisions") or [],
            "constraints": source_result.get("verified_decisions") or [],
            "tradeoffs": [],
            "verified_decisions": source_result.get("verified_decisions") or [],
            "verification_result": source_result.get("verification_result") or {},
            "decision_gaps": [],
            "debate_result": {"status": "completed"},
            "status": "completed",
            "missing_sections": [],
        }

    # Regenerate (deterministic Path 3).
    spec_result = build_spec(source_answer=answer_text, debate_fn=None,
                             source_result=source_result)
    if spec_result.get("spec_text"):
        store_implementation_spec(workspace_id, spec_result["spec_text"])
    return spec_result


def _load_or_rebuild_plan(
    workspace_id: str,
    manifest: "BuildManifest | None",
    spec_result: dict,
) -> dict:
    """Reuse ``execution_plan.json`` when its checksum still matches the
    payload AND the on-disk spec checksum (captured as ``spec_checksum`` in
    the plan payload) matches the just-reused spec's text sha256. Otherwise
    recompute deterministically via ``execution_planner.plan`` + store.

    Returns the persisted/loaded plan payload dict (so report.plan.* reads it
    the same way the producing path does). Determinism is preserved: a
    regenerated plan has the same id as the persisted one whenever the spec
    hasn't changed.
    """
    from execution_planner import (
        plan as _plan, store_execution_plan, load_execution_plan,
        verify_execution_plan_checksum,
    )

    env = load_execution_plan(workspace_id)
    spec_text = spec_result.get("spec_text") or ""
    current_spec_ck = _sha256_text(spec_text)

    if env is not None and verify_execution_plan_checksum(env):
        plan_spec_ck = (env.get("payload") or {}).get("spec_checksum", "")
        if (spec_text and plan_spec_ck == current_spec_ck
                and (manifest is None
                     or env.get("id") == (manifest.execution_plan or env.get("id")))):
            # Reuse — return the payload as-is.
            return env.get("payload") or {}

    # Recompute + persist.
    plan_obj = _plan(spec_result)
    envelope = store_execution_plan(workspace_id, plan_obj, producer="execution_planner")
    return envelope.get("payload") or plan_obj.to_dict()


def _refresh_manifest(workspace_id: str) -> None:
    """Part 6 — re-assemble + persist the manifest after a resume moved the
    build forward. Failure is observational (catch + ignore) so resume never
    blocks on manifest persistence.

    Audit 4 (Duplicate Concepts): delegates to the canonical
    ``manifest.refresh_manifest`` (sole owner of the post-state-change
    manifest-write concept); ``build_pipeline._persist_build_manifest``
    delegates to the same authority. No two modules duplicate the
    assembly+persistence body.
    """
    from manifest import refresh_manifest
    refresh_manifest(workspace_id)


def _resume_from_spec_ready(workspace_id: str, use_claude_code: bool,
                            state: dict,
                            manifest: "BuildManifest | None" = None) -> dict:
    """Regenerate the spec deterministically (debate_fn=None Path 3), validate
    it, plan it, then hand off to execution. ``force=True`` re-runs land here.

    Manifest-aware (Part 6): when ``manifest`` is provided, the on-disk
    ``implementation_plan.md`` and ``execution_plan.json`` are reused verbatim
    whenever their checksums match the manifest's record — never regenerated.
    """
    import build_pipeline as bp  # local: keep the import lazy

    decisions = _read_decisions_model(workspace_id)
    answer_text = _read_source_answer_text(workspace_id)
    source_result = _shape_source_result(decisions, answer_text)

    if not decisions.get("verification_result") and not answer_text:
        # Nothing to build from — a brand-new empty workspace.
        return {"resumed": False, "reason": "no_source_to_resume_from",
                "lifecycle_stage": current_lifecycle(workspace_id).value,
                "manifest_present": manifest is not None,
                "workspace_id": workspace_id}

    # A force-retry from FAILED uses the FAILED -> SPEC_READY recovery edge
    # (see lifecycle.BUILD_LIFECYCLE) and skips the VERIFIED advance (the
    # decisions.json is already verified — we are regenerating the spec only).
    current = current_lifecycle(workspace_id)
    if current is not BuildLifecycle.FAILED:
        advance_lifecycle(workspace_id, BuildLifecycle.VERIFIED,
                          resumed_from=state.get("lifecycle_stage", state.get("status", "")))

    # Part 6: reuse on-disk deterministic spec when checksum matches the
    # manifest's record. Otherwise regenerate via Path 3.
    spec_result = _load_or_rebuild_spec_result(workspace_id, manifest,
                                                answer_text, source_result)
    if spec_result.get("status") == "completed":
        advance_lifecycle(workspace_id, BuildLifecycle.SPEC_READY,
                          spec_status="completed",
                          spec_missing_sections=spec_result.get("missing_sections", []))
        # Part 6: reuse the persisted plan when its spec_checksum matches the
        # spec we're about to consume. Otherwise determininistically regenerate.
        plan_payload = _load_or_rebuild_plan(workspace_id, manifest, spec_result)
        report = _validate_and_plan(workspace_id, spec_result, plan_payload)
        _refresh_manifest(workspace_id)
        # Hand off to execution (skips cleanly when use_claude_code=False).
        exec_report = _resume_from_execution_allowed(
            workspace_id, use_claude_code, _read_workspace_state(workspace_id),
            manifest=manifest,
        )
        _refresh_manifest(workspace_id)
        return {"resumed": True,
                "lifecycle_stage": exec_report.get("lifecycle_stage", "SPEC_READY"),
                "spec": spec_result,
                "plan": plan_payload,
                "validation": report, "execution": exec_report,
                "manifest_present": manifest is not None,
                "workspace_id": workspace_id}
    advance_lifecycle(workspace_id, BuildLifecycle.FAILED,
                      spec_status=spec_result.get("status"),
                      spec_missing_sections=spec_result.get("missing_sections", []))
    _refresh_manifest(workspace_id)
    return {"resumed": False, "reason": "spec_still_incomplete",
            "lifecycle_stage": "FAILED", "spec": spec_result,
            "manifest_present": manifest is not None,
            "workspace_id": workspace_id}


def _resume_staged_build(
    workspace_id: str,
    *,
    use_claude_code: bool = False,
    force: bool = False,
    staged_agent_fn: Any = None,
    staged_test_fn: Any = None,
) -> dict:
    """Stage-aware resume for a staged FULL_BUILD workspace.

    The dispatch contract:
      * If every stage is DONE and integration is DONE  -> terminal; no work.
      * If a stage transitioned to FAILED                -> if force, re-arm the
        stage to PENDING (re-run with stronger repair context); else surface
        the HUMAN_REQUIRED decision as the resume's blocked reason.
      * Otherwise (in-progress / pending stages)         -> continue from the
        next pending stage via ``staged_executor.execute_stages`` (deps-aware).
        ``use_claude_code=False`` skips execution cleanly — resume reads the
        on-disk stage_status.json + manifest without invoking the agent (an
        offline status-report mode).

    On successful completion (all stages + integration DONE), the build
    advances to TESTS_VALIDATED and routes through the Build Finalizer for
    publication — the same finalization path the non-staged resume uses
    (``_finalize_after_resume``), so PUBLISHED -> COMPLETE remains the
    finalizer's sole ownership. The staged executor writes lifecycle/manifest
    records through the shared helpers; this function ONLY advances the
    build-level lifecycle + calls the finalizer.
    """
    import stage_status
    import staged_executor

    plan = stage_status.read_build_plan(workspace_id)
    stages = plan.get("stages") or []
    statuses = stage_status.read_stage_status(workspace_id).get("stages") or {}
    stages_done = sum(1 for s in stages
                     if (statuses.get(s.get("stage_id",""), {}) or {}).get("status") == "DONE")
    stages_failed = sum(1 for s in stages
                        if (statuses.get(s.get("stage_id",""), {}) or {}).get("status") == "FAILED")
    pending = stage_status.next_pending_stage(workspace_id)

    # Terminal: nothing to resume (every stage DONE + integration DONE).
    integ_status = (statuses.get(staged_executor.INTEGRATION_STAGE_ID, {}) or {}).get("status")
    if stages and stages_done == len(stages) and integ_status == "DONE":
        return {"resumed": False, "reason": "staged_build_already_complete",
                "lifecycle_stage": current_lifecycle(workspace_id).value,
                "stages_done": stages_done, "stages_total": len(stages),
                "workspace_id": workspace_id}

    # A previously FAILED stage with a HUMAN_REQUIRED pending blocks unless
    # ``force=True``. Otherwise the operator's resolution is what unblocks it.
    human_pending = _pending_human_count(workspace_id)
    if stages_failed > 0 or human_pending > 0:
        if not force:
            return {"resumed": False, "reason": "staged_build_failed_no_force",
                    "lifecycle_stage": current_lifecycle(workspace_id).value,
                    "stages_done": stages_done, "stages_failed": stages_failed,
                    "pending_count": human_pending,
                    "suggested_action": ("call resume_build(..., force=True) to "
                                          "retry from the first failed stage, or "
                                          "resolve the pending HUMAN_REQUIRED "
                                          "decision first"),
                    "workspace_id": workspace_id}
        # Force-retry: re-arm every FAILED stage to PENDING so the executor
        # can pick them up. The achieved execution from prior DONE stages is
        # preserved (the executor's ``next_pending_stage`` only returns stages
        # whose deps are all DONE; a FAILED-deps chain re-runs after this
        # re-arm because the executor only counts DONE, not FAILED).
        for s in stages:
            sid = s.get("stage_id", "")
            rec = statuses.get(sid, {}) or {}
            if rec.get("status") == "FAILED":
                stage_status.write_stage_status(workspace_id, sid,
                                                  stage_status.StageStatus.PENDING,
                                                  completed_at=None,
                                                  attempts=0)
        # Also re-arm a failed integration stage.
        if integ_status == "FAILED":
            stage_status.write_stage_status(
                workspace_id, staged_executor.INTEGRATION_STAGE_ID,
                stage_status.StageStatus.PENDING, completed_at=None, attempts=0)
        statuses = stage_status.read_stage_status(workspace_id).get("stages") or {}

    # Offline status-report mode (use_claude_code=False): do NOT dispatch the
    # executor; return the on-disk stage-status summary so the CLI / operator
    # can see where the build sits. This mirrors the legacy resume path's
    # ``_resume_from_execution_allowed`` behavior on a skip.
    if not use_claude_code:
        return {"resumed": False, "reason": "staged_offline_no_execution",
                "lifecycle_stage": current_lifecycle(workspace_id).value,
                "stages_total": len(stages),
                "stages_done": stages_done,
                "stages_failed": stages_failed,
                "pending_count": human_pending,
                "next_pending_stage": (stage_status.next_pending_stage(workspace_id)
                                       or {}).get("stage_id"),
                "workspace_id": workspace_id}

    # Production dispatch: advance to EXECUTING (the legacy build-level
    # lifecycle; stage-level state is owned by stage_status / the staged
    # executor) and invoke execute_stages.
    advance_lifecycle(workspace_id, BuildLifecycle.EXECUTING)
    try:
        staged_result = staged_executor.execute_stages(
            workspace_id,
            agent_fn=staged_agent_fn,
            test_fn=staged_test_fn,
        )
    except Exception as e:
        advance_lifecycle(workspace_id, BuildLifecycle.FAILED,
                           execution_error=f"staged_executor: {str(e)[:200]}")
        _refresh_manifest(workspace_id)
        return {"resumed": False, "reason": "staged_execution_error",
                "error": str(e), "workspace_id": workspace_id}

    # Map the staged result onto a legacy-style exec_result so the existing
    # downstream path (``_classify_execution_result`` -> ``_finalize_after_resume``)
    # works unchanged. The shape is the SAME the staged dispatch in
    # ``build_pipeline.run_build_pipeline`` produces.
    if staged_result.get("success"):
        exec_result = {
            "success": True,
            "output": (f"staged resume: {staged_result.get('stages_done',0)}/"
                       f"{staged_result.get('stages_total',0)} stages + "
                       "integration passed"),
            "test_results": (staged_result.get("integration", {}) or {}).get("test_results", {}),
            "error": "",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "staged": True,
        }
    elif staged_result.get("blocked"):
        exec_result = {
            "success": False, "blocked": True,
            "blocked_reason": "pending_human_decisions",
            "test_results": {}, "error": "staged execution blocked",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "staged": True,
        }
    else:
        exec_result = {
            "success": False, "error": "staged execution failed",
            "test_results": {}, "staged": True,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
    # Reuse the existing classify + finalize path so PUBLISHED -> COMPLETE
    # remains the finalizer's sole ownership (Audit 2 — Lifecycle Ownership).
    report = _classify_execution_result(workspace_id, exec_result, executing=True)
    report["staged_execution"] = staged_result
    return report


def _resume_from_execution_allowed(workspace_id: str, use_claude_code: bool,
                                    state: dict,
                                    manifest: "BuildManifest | None" = None) -> dict:
    """Bridge from SPEC_READY/EXECUTION_ALLOWED into the executor. The repair
    loop's ``_load_build_state`` checkpoint handles mid-execution resume."""
    import build_pipeline as bp

    pending = _pending_human_count(workspace_id)
    if pending > 0:
        return {"resumed": False, "reason": "blocked_pending_humans",
                "lifecycle_stage": BuildLifecycle.BLOCKED.value,
                "pending_count": pending, "workspace_id": workspace_id}

    advance_lifecycle(workspace_id, BuildLifecycle.EXECUTION_ALLOWED,
                      resumed_from=state.get("lifecycle_stage", state.get("status", "")))
    # Only step into EXECUTING when an executor will actually run. ``execute_build_spec``
    # skips cleanly when no executor_fn AND no use_claude_code, so a skip leaves us
    # at EXECUTION_ALLOWED (ready and waiting) — never an illegal retreat.
    executing = bool(use_claude_code)
    if executing:
        advance_lifecycle(workspace_id, BuildLifecycle.EXECUTING)
    try:
        exec_result = bp.execute_build_spec(
            workspace_id, use_claude_code=use_claude_code,
        )
    except Exception as e:
        if executing:
            advance_lifecycle(workspace_id, BuildLifecycle.FAILED,
                              execution_error=str(e)[:200])
        return {"resumed": False, "reason": "execution_error",
                "error": str(e), "workspace_id": workspace_id}

    return _classify_execution_result(workspace_id, exec_result, executing)


def _classify_execution_result(workspace_id: str, exec_result: dict,
                                executing: bool) -> dict:
    """Map an executor result onto a lifecycle transition and a resume report.
    The executor itself wrote the prior ``status``; we mirror the same
    conclusion into ``lifecycle_stage`` so the next resume reads it directly.

    ``executing`` is whether we advanced to EXECUTING (i.e. an executor ran).
    A skipped execution (no opt-in) is NOT a transition — the build stays at
    EXECUTION_ALLOWED, ready for an operator to opt in.

    Part 6: the manifest is refreshed after every branch so the next resume
    sees the authoritative lifecycle + the artifacts this run produced."""
    _refresh_manifest(workspace_id)
    stage_label = current_lifecycle(workspace_id).value
    if exec_result.get("blocked"):
        advance_lifecycle(workspace_id, BuildLifecycle.BLOCKED,
                          blocked_reason=exec_result.get("blocked_reason"))
        _refresh_manifest(workspace_id)
        return {"resumed": True, "lifecycle_stage": BuildLifecycle.BLOCKED.value,
                "execution": exec_result, "workspace_id": workspace_id}
    if exec_result.get("skipped"):
        # No transition: execution was not attempted. The build is ready and
        # waiting at EXECUTION_ALLOWED for an operator to opt in.
        return {"resumed": True, "lifecycle_stage": stage_label,
                "execution": exec_result, "skipped": True,
                "workspace_id": workspace_id}
    if exec_result.get("error") or not exec_result.get("success"):
        # Reached only when an executor ran (executing=True). EXECUTING -> FAILED.
        advance_lifecycle(workspace_id, BuildLifecycle.FAILED,
                          execution_error=str(exec_result.get("error", ""))[:200])
        _refresh_manifest(workspace_id)
        return {"resumed": True, "lifecycle_stage": BuildLifecycle.FAILED.value,
                "execution": exec_result, "workspace_id": workspace_id}

    advance_lifecycle(workspace_id, BuildLifecycle.ARTIFACTS_VALIDATED)
    advance_lifecycle(workspace_id, BuildLifecycle.TESTS_VALIDATED)

    # Build Finalization — Publication. Resume's executor reached
    # TESTS_VALIDATED on a successful run; the SAME reconciliation that
    # ``build_pipeline.run_build_pipeline`` invokes post-manifest MUST run
    # here, otherwise a build completed via ``resume_execution`` would
    # bypass Publication and leave answer_memory / workspace DB / telemetry
    # un-reconciled — the exact Debate #164 divergence the Build Finalizer
    # was introduced to prevent. Architecture audit (Audit 2 — Lifecycle
    # Ownership): the finalizer is the sole owner of PUBLISHED -> COMPLETE,
    # so resume routes through it instead of jumping TESTS_VALIDATED -> COMPLETE.
    finalization = _finalize_after_resume(workspace_id, exec_result)
    return {"resumed": True,
            "lifecycle_stage": current_lifecycle(workspace_id).value,
            "execution": exec_result, "workspace_id": workspace_id,
            "finalization": finalization}


def _finalize_after_resume(workspace_id: str, exec_result: dict) -> dict:
    """Run the Build Finalizer after a successful resume execution.

    Mirrors what ``build_pipeline.run_build_pipeline`` does post-manifest:
    call ``build_finalizer.finalize_build`` so Publication runs (advancing
    PUBLISHED -> COMPLETE) and every persistence layer is reconciled.

    Loads the on-disk build_state + manifest + decisions/answer models the
    finalizer requires. Failures are surfaced in the finalization result
    rather than swallowed — the finalizer's own transaction contract gives
    per-step status; we propagate the whole dict so the resume caller can
    observe which step (if any) failed.
    """
    try:
        from build_finalizer import finalize_build
        from build_workspace import _workspace_dir, load_workspace as _load_bw
        from manifest import load_manifest

        ws_dir = _workspace_dir(workspace_id)
        bw_ws = _load_bw(workspace_id)
        parent_answer_id = None
        if bw_ws is not None and bw_ws.source_answer_id is not None:
            try:
                parent_answer_id = int(bw_ws.source_answer_id)
            except (TypeError, ValueError):
                parent_answer_id = None
        manifest = load_manifest(workspace_id)
        manifest_dict = manifest.to_dict() if manifest else {}

        # exec_dec: the finalizer prefers build_state's recorded verdict
        # (the override verdict that ran the build); exec_result carries no
        # decision verdict, just the test/agent result. So exec_dec is left
        # empty and the finalizer's effective_* fallback to build_state.
        return finalize_build(
            workspace_id,
            parent_answer_id=parent_answer_id,
            exec_dec={},
            spec_result={},
            exec_result=exec_result,
            manifest=manifest_dict,
            classification={"workflow_type": "IMPLEMENTATION"},
            original_prompt=(bw_ws.source_question if bw_ws else "")
                            or f"resume {workspace_id}",
            user_intent_prompt=(bw_ws.source_question if bw_ws else None),
            spec_status=None,
            spec_missing_sections=None,
        )
    except Exception as e:
        return {"finalized": False,
                "steps": [{"step": "finalizer_dispatch",
                           "status": "failed",
                           "error": str(e)}],
                "consistency": None}


def _validate_and_plan(workspace_id: str, spec_result: dict,
                       persisted_plan_payload: dict | None = None) -> dict:
    """Run the central validators over the spec + plan. Parts D + E + the
    consolidation phase: when ``persisted_plan_payload`` is provided (the
    manifest-aware reuse path), the validator runs against THAT plan instead
    of a freshly-derived one — so a reused plan is validated by the same
    contract the producing path uses.

    Pure (no I/O beyond the workspace files the spec already wrote)."""
    from validation import validate_build_spec, validate_execution_plan
    from execution_planner import plan as plan_tasks

    spec_report = validate_build_spec(spec_result)
    if persisted_plan_payload is not None:
        plan_report = validate_execution_plan(persisted_plan_payload)
        plan_dict = persisted_plan_payload
        plan_id = (persisted_plan_payload or {}).get("id", "")
        plan_order = (persisted_plan_payload or {}).get("order", [])
        plan_task_count = len((persisted_plan_payload or {}).get("tasks") or {})
    else:
        task_plan = plan_tasks(spec_result)
        plan_report = validate_execution_plan(task_plan.to_dict())
        plan_dict = task_plan.to_dict()
        plan_id = task_plan.id
        plan_order = task_plan.order
        plan_task_count = len(task_plan.tasks)
    return {
        "spec_validation": spec_report.to_dict(),
        "plan_validation": plan_report.to_dict(),
        "plan_order": plan_order,
        "plan_id": plan_id,
        "plan_tasks": plan_task_count,
        "reused_plan": persisted_plan_payload is not None,
    }


def resume_execution(workspace_id: str, *, use_claude_code: bool = False) -> dict:
    """Resume only the execution (+ repair loop) stage. Reads the lifecycle and
    refuses to run if the build is not at least SPEC_READY / EXECUTION_ALLOWED
    (calling the executor on an un-planned workspace would infer execution)."""
    stage = current_lifecycle(workspace_id)
    if stage in (BuildLifecycle.COMPLETE, BuildLifecycle.ABORTED):
        return {"resumed": False, "reason": "terminal",
                "lifecycle_stage": stage.value, "workspace_id": workspace_id}
    if stage in (BuildLifecycle.NEW, BuildLifecycle.VERIFIED):
        return {"resumed": False, "reason": "spec_not_ready",
                "suggested_action": "call resume_build first (generates spec + plan)",
                "lifecycle_stage": stage.value, "workspace_id": workspace_id}
    from manifest import load_manifest
    return _resume_from_execution_allowed(
        workspace_id, use_claude_code, _read_workspace_state(workspace_id),
        manifest=load_manifest(workspace_id),
    )


def resume_validation(workspace_id: str) -> dict:
    """Re-run the validators against the workspace's current artifacts + state.
    Pure; no mutation. Used to confirm a build's invariants after a manual fix
    or before declaring COMPLETE."""
    from validation import (validate_decision_model, validate_build_spec,
                            validate_final_build)
    decisions = _read_decisions_model(workspace_id)
    state = _read_workspace_state(workspace_id)

    dm_report = validate_decision_model(decisions.get("verification_result"))
    # If an implementation_plan.md exists, validate it too.
    spec_report = None
    try:
        from build_spec_engine import load_implementation_spec
        from build_prompt_generator import validate_executable_spec_sections
        spec_text = load_implementation_spec(workspace_id)
        if spec_text:
            spec_dict = {"spec_text": spec_text,
                         "status": ("completed"
                                    if not validate_executable_spec_sections(spec_text)
                                    else "incomplete"),
                         "verified_decisions": decisions.get("verified_decisions", [])}
            spec_report = validate_build_spec(spec_dict)
    except Exception:
        pass
    fb_report = validate_final_build(state)
    return {
        "resumed": True, "lifecycle_stage": current_lifecycle(workspace_id).value,
        "decision_model": dm_report.to_dict(),
        "build_spec": spec_report.to_dict() if spec_report else None,
        "final_build": fb_report.to_dict(),
        "workspace_id": workspace_id,
    }


if __name__ == "__main__":
    # ponytail self-check: a real workspace created + resumed without touching
    # a model. The conftest-style WORKSPACE_ROOT swap is inlined (no pytest).
    import tempfile
    from build_workspace import WORKSPACE_ROOT as _R0
    import build_workspace as _bw
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)
        wid = _bw.create_workspace(source_question="Build Sol0mon MVP")

        # Empty NEW workspace: nothing to resume from.
        rep = resume_build(wid)
        assert rep["resumed"] is False and rep["reason"] == "no_source_to_resume_from", rep

        # Plant a verified decision (the decisions.json the pipeline would
        # write) so spec generation has the canonical model to read.
        from build_workspace import _workspace_dir
        decisions = {
            "answer_id": 1,
            "verification_result": {
                "resolved_claims": [{
                    "normalized_key": "database",
                    "dispute": "PostgreSQL vs SQLite",
                    "verification": {"status": "RESOLVED", "confidence": 95,
                                     "finding": "Both strips conclude SQLite; "
                                                "argues against PostgreSQL."},
                }],
                "equivalent_claims": [], "unresolved_claims": [],
                "verification_score": 100, "truncated_count": 0,
            },
            "verified_decisions": [{"outcome_type": "VERIFIED_DECISION",
                                    "dispute_key": "database",
                                    "decision": "SQLite persistence",
                                    "reasoning": "low capital"}],
            "resolved_decisions": [],
        }
        (_workspace_dir(wid) / "decisions.json").write_text(
            json.dumps(decisions, indent=2), encoding="utf-8")
        (_workspace_dir(wid) / "answer.json").write_text(
            json.dumps({"synthesized_answer": "Use SQLite"}, indent=2),
            encoding="utf-8")

        rep = resume_build(wid)  # no model: skipped execution
        assert rep["resumed"] is True, rep
        # Skipped execution leaves the build at EXECUTION_ALLOWED (ready and
        # waiting) — NOT SPEC_READY (the gate cleared) and NOT EXECUTING (no
        # executor ran). The truthful label comes from current_lifecycle.
        assert rep["lifecycle_stage"] == "EXECUTION_ALLOWED", rep
        assert rep["execution"]["skipped"] is True, rep
        # Spec was generated + validated + planned.
        assert rep["validation"]["plan_tasks"] >= 1, rep["validation"]

        # Second resume: lifecycle now EXECUTION_ALLOWED. advance_lifecycle
        # populated lifecycle_stage for the next resume to read directly.
        from lifecycle import current_lifecycle
        assert current_lifecycle(wid) is BuildLifecycle.EXECUTION_ALLOWED
        rep2 = resume_build(wid)
        assert rep2["resumed"] is True
        # Force on a non-failed stage just re-runs spec (idempotent-ish).
        rep3 = resume_build(wid, force=True)
        assert rep3["resumed"] is True

        # resume_validation returns all three validator reports.
        rv = resume_validation(wid)
        assert rv["resumed"] is True
        assert rv["decision_model"]["passed"] is True
        assert rv["build_spec"] is not None and rv["build_spec"]["passed"] is True

        # Staged-resume test plant: create a new staged workspace, mark some
        # stages DONE on disk (simulating prior progress), then resume and
        # assert the staged dispatch continues from the next pending stage.
        # The ``use_claude_code=False`` offline-status-report path is exercised
        # so the smoke check stays model-free.
        wid_s = _bw.create_workspace(source_question="Staged MVP",
                                      human_slug="Staged Resume Smoke")
        ws_s = _bw._workspace_dir(wid_s)
        # Plant a verification result the architecture matcher picks up as 2
        # components AND run the planner so the staged plan exists on disk.
        decisions_s = {
            "answer_id": 2,
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
                 "decision": "Temporal"},
                {"outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
                 "decision": "PostgreSQL"},
            ],
            "resolved_decisions": [],
        }
        (ws_s / "decisions.json").write_text(json.dumps(decisions_s, indent=2),
                                                encoding="utf-8")
        (ws_s / "answer.json").write_text(
            json.dumps({"synthesized_answer": "Temporal+PostgreSQL"}),
            encoding="utf-8")
        from build_spec_engine import store_implementation_spec
        store_implementation_spec(wid_s,
                                    "# Implementation Objective\nX\n\n"
                                    "# Files to Create\n- `src/a.py`\n- `src/b.py`\n\n"
                                    "# Files to Modify\n(none)\n\n"
                                    "# Implementation Steps\n1. a\n2. b\n\n"
                                    "# Acceptance Criteria\n- pytest passes\n\n"
                                    "# Output Contract\nFiles under src/.\n")

        import build_planner as _bp
        import stage_status as _ss
        _bp.plan_staged_build(wid_s)
        plan_s = _ss.read_build_plan(wid_s)
        sids = [s["stage_id"] for s in plan_s["stages"]]
        assert len(sids) >= 5, plan_s

        # Mark the first component stage DONE on disk (simulating partial
        # prior progress) so the resume should continue from the second
        # pending stage (which depends on the first).
        _ss.write_stage_status(wid_s, sids[0], _ss.StageStatus.IMPLEMENTING)
        _ss.write_stage_status(wid_s, sids[0], _ss.StageStatus.DONE)
        # Offline resume (use_claude_code=False) returns the staged status
        # summary without driving the agent. The legacy dispatch path
        # (which would re-run spec) is NOT taken — staged layout short-circuits.
        rep_s = resume_build(wid_s)  # use_claude_code defaults to False
        assert rep_s["reason"] == "staged_offline_no_execution", rep_s
        assert rep_s["stages_done"] == 1, rep_s
        assert rep_s["stages_total"] == len(sids), rep_s

        # An already-complete staged build (every stage DONE + integration
        # DONE) reports "already_complete" — no re-execution.
        for sid in sids:
            _ss.write_stage_status(wid_s, sid, _ss.StageStatus.DONE)
        from staged_executor import INTEGRATION_STAGE_ID
        _ss.write_stage_status(wid_s, INTEGRATION_STAGE_ID, _ss.StageStatus.DONE)
        rep_s2 = resume_build(wid_s)
        assert rep_s2["reason"] == "staged_build_already_complete", rep_s2

        _bw.WORKSPACE_ROOT = _R0
    print("resume smoke test PASSED")
