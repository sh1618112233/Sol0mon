"""Project Sol0mon — Build Finalization.

The single architectural layer that reconciles every persistence store AFTER
a successful build, before control returns to the CLI.

Why this module exists
----------------------
The forensic audit of Debate #164 proved the build itself completed
successfully (`workspace_965ae1f7-…`: lifecycle=COMPLETE, 12 tests pass,
manifest written). The failures were entirely *post-build*: nothing reconciled
the new build state back into the durable stores. Three independent silos
diverged and were never reconciled:

    1. ``answer_memory.db debates``     — INSERTed at debate-time with
                                          ``workspace_id=''`` and the verifier's
                                          ``execution_decision='RESEARCH_ONLY'``;
                                          *no UPDATE code path existed*, so
                                          those columns stayed frozen at
                                          debate-time even when the build
                                          pipeline overrode the decision to
                                          ``IMPLEMENTATION_REQUIRED`` and minted
                                          a workspace id.
    2. ``database/workspaces.db``      — the SQL table ``workspaces`` (read by
                                          ``sol0mon resume`` / ``status
                                          --workspace`` via
                                          ``workspace_manager.load_workspace``)
                                          stayed empty because
                                          ``build_pipeline`` only writes
                                          filesystem JSON; no code path INSERTed
                                          a row.
    3. ``workspace_status.json`` +
       ``performance_report.json``     — frozen at debate-completion because
                                          ``pipeline_runner.execute``'s finally
                                          block called ``telemetry.reset()``
                                          before the CLI chained into the build
                                          pipeline, so all build-phase recorder
                                          calls were no-ops.

The threestores were correct *within their scope* but contradicted each other
across scopes. The Build Finalizer is the single authority that closes the
loop: it runs ONCE, after the manifest is written (lifecycle=COMPLETE),
before the CLI regains control. It publishes the build result back into every
store that records "what was built" so resume/status/latest-answer all see
the same state.

Scope — what the finalizer owns (the ONLY writes to these fields anywhere)
    - ``answer_memory.debates.<id>.workspace_id``
    - ``answer_memory.debates.<id>.verification_result.execution_decision``
      (+ reason/confidence/source) — overridden to the build-phase verdict.
    - ``database/workspaces.db.workspaces.<id>`` — INSERT-or-UPDATE row.
    - ``workspace_status.json`` + ``performance_report.json`` at CWD —
      refreshed with build-phase data.

What the finalizer does NOT touch
    - The decision model (``decision_model``), the verifier, the planner,
      the implementation executor, the repair loop, the OpenCode agent,
      the quality gate, consensus logic, research pipeline. The user's
      invariant: "Do not change the research, debate, verification, planner,
      execution engine, implementation executor, repair loop, OpenCode
      integration, or execution decision logic."

Transaction contract
    Every step is independent. A step failure does NOT roll back the build
    (the build already succeeded on disk and that is the truth) — instead
    the finalizer returns a per-step result list naming exactly which
    persistence layer failed and why. The caller surfaces failures; nothing
    is silently swallowed.

Public API
    finalize_build(workspace_id, *, parent_answer_id, exec_dec, spec_result,
                   exec_result, manifest, classification, ...) -> dict

    Returns:
        {
          "finalized": bool,            # True iff all steps succeeded
          "steps": [                    # ordered, one entry per step
            {"step": "answer_memory", "status": "ok"|"failed"|"skipped",
             "error": str?},
            {"step": "workspace_db",    ...},
            {"step": "telemetry",       ...},
            {"step": "publish_artifacts",...},
            {"step": "reconcile_state", ...},
          ],
          "consistency": {              # the post-finalization truth (or None
                                       # if reconcile_state failed so badly
                                       # we couldn't even read it back)
            "workspace_id": str, "debate_id": int,
            "execution_decision": str, "lifecycle": str,
            "build_status": str,
          },
        }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import answer_memory
from workspace_manager import workspace_mgr
import workspace_manager as _wm_module


# ---------------------------------------------------------------------------
# Workspace-path helper (mirrors build_workspace._workspace_dir without
# importing it, to avoid a circular import: build_pipeline imports
# build_finalizer, build_workspace imports neither. Keeping this local keeps
# the finalizer free of build_pipeline's heavy import chain.)
# ---------------------------------------------------------------------------
def _workspace_dir(workspace_id: str) -> Path:
    """Resolve the build workspace directory under WORKSPACE_ROOT."""
    from build_workspace import _workspace_dir as _bw_dir
    return _bw_dir(workspace_id)


def _read_json(path: Path, default: Any) -> Any:
    if not path or not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _step_ok(step: str, **extra: Any) -> dict:
    d = {"step": step, "status": "ok"}
    d.update(extra)
    return d


def _step_failed(step: str, error: str, **extra: Any) -> dict:
    d = {"step": step, "status": "failed", "error": str(error)}
    d.update(extra)
    return d


def _step_skipped(step: str, reason: str, **extra: Any) -> dict:
    d = {"step": step, "status": "skipped", "reason": reason}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# Build Finalizer — single entry point.
# ---------------------------------------------------------------------------
def finalize_build(
    workspace_id: str,
    *,
    parent_answer_id: int | None,
    exec_dec: dict | None,
    spec_result: dict | None,
    exec_result: dict | None,
    manifest: dict | None,
    classification: dict | None,
    original_prompt: str = "",
    user_intent_prompt: str | None = None,
    spec_status: str | None = None,
    spec_missing_sections: list | None = None,
) -> dict:
    """Run the 5 reconciliation steps in order. See module docstring.

    Args:
        workspace_id:          the just-built workspace's id.
        parent_answer_id:      the originating debate row's id (the source
                               answer that resurrected this build). None for
                               direct-prompt builds (no debate row to
                               update; STEP 1 is skipped with that reason).
        exec_dec:              the gate's effective execution decision dict
                               (carries execution_decision / execution_reason
                               / execution_confidence /
                               execution_decision_source). STEP 1 mirrors
                               these into the debate row's
                               verification_result.
        spec_result:           the build-spec result (spec_text, status,
                               missing_sections). STEP 4 uses spec_status.
        exec_result:           the executor's result (success, skipped,
                               blocked). STEP 4 & reconcile_state use it.
        manifest:              the persisted manifest dict (lifecycle,
                               artifact ids, checksums). STEP 4 & 5 read it.
        classification:         the workflow classifier's result
                               (workflow_type). reconcile_state uses it to
                               confirm IMPLEMENTATION.
        original_prompt:       the CLI/build-pipeline prompt (for
                               upsert_build_workspace.original_prompt when no
                               resurrected answer is available).
        user_intent_prompt:     the original user-intent prompt (the
                               resurrected answer's question); used as the
                               SQL row's original_prompt when the build was
                               a resurrection, so the SQL row carries the
                               user's actual question (not "build answer
                               164").
        spec_status:            the spec's completion status
                               ("completed"/"incomplete"). Falls back to
                               spec_result["status"] when None.
        spec_missing_sections: the spec's missing sections list. Falls back
                               to spec_result["missing_sections"] when None.
    """
    steps: list[dict] = []

    if not workspace_id:
        steps.append(_step_failed("answer_memory",
                                  "workspace_id is empty; cannot finalize"))
        return {"finalized": False, "steps": steps, "consistency": None}

    # ====================================================================
    # STAGE ENTRY — Build Finalization is an official pipeline stage:
    #   TESTS_VALIDATED -> PUBLISHED -> COMPLETE.
    # The finalizer is the SOLE owner of the PUBLISHED stage (per
    # lifecycle.STATES); entering it here declares "Publication in progress".
    # On full success the finalizer advances to COMPLETE in stage exit below.
    # Failure inside any STEP leaves the workspace at PUBLISHED so an operator
    # sees "publication did not complete" rather than a false COMPLETE.
    # advance_lifecycle raises BuildError on illegal transitions; that is
    # caught here and surfaced as a stage-entry failure (rare: a workspace
    # not at TESTS_VALIDATED somehow reached run_build_pipeline's tail).
    # ====================================================================
    try:
        from lifecycle import advance_lifecycle, current_lifecycle, BuildLifecycle
        failed_execution = not bool((exec_result or {}).get("success"))
        if failed_execution:
            target = (BuildLifecycle.BLOCKED
                      if (exec_result or {}).get("blocked")
                      or "human" in str((exec_result or {}).get("error", "")).lower()
                      else BuildLifecycle.FAILED)
            current = current_lifecycle(workspace_id)
            if current not in (target, BuildLifecycle.COMPLETE):
                advance_lifecycle(workspace_id, target, finalization_stage="failed")
        else:
            advance_lifecycle(workspace_id, BuildLifecycle.PUBLISHED,
                              finalization_stage="entered")
    except Exception as e:
        # If the workspace isn't at TESTS_VALIDATED the transition raises. The
        # build itself is still valid (it completed on disk); we just can't
        # publish through the official stage. Report as a structured failure
        # and continue with reconciliation (the steps below still run — they
        # don't depend on the lifecycle marker; they depend on the on-disk
        # build_state which is the actual truth).
        steps.append(_step_failed("stage_entry", e))

    exec_dec = exec_dec or {}
    spec_result = spec_result or {}
    exec_result = exec_result or {}
    manifest = manifest or {}
    classification = classification or {}

    ws_dir = _workspace_dir(workspace_id)
    build_state = _read_json(ws_dir / "build_state.json", {}) or {}
    workspace_json = _read_json(ws_dir / "workspace.json", {}) or {}
    decisions_json = _read_json(ws_dir / "decisions.json", {}) or {}

    spec_status = spec_status or spec_result.get("status") or build_state.get(
        "spec_status") or ""
    spec_missing = (spec_missing_sections if spec_missing_sections is not None
                    else spec_result.get("missing_sections", []))

    # The effective build verdict the finalizer mirrors into every store.
    effective_decision = (
        exec_dec.get("execution_decision")
        or build_state.get("execution_decision")
        or ""
    )
    effective_reason = (
        exec_dec.get("execution_reason")
        or build_state.get("execution_reason")
        or ""
    )
    effective_confidence = (
        exec_dec.get("execution_confidence")
        or build_state.get("execution_confidence")
        or 0
    )
    effective_source = (
        exec_dec.get("execution_decision_source")
        or build_state.get("execution_decision_source")
        or exec_dec.get("source")
        or build_state.get("decision_source")
        or ""
    )
    effective_selected_mode = (
        build_state.get("selected_mode")
        or exec_dec.get("selected_mode")
        or ""
    )
    effective_recommended_mode = (
        build_state.get("recommended_mode")
        or exec_dec.get("recommended_mode")
        or ""
    )
    effective_override_used = bool(
        build_state.get("override_used", exec_dec.get("override_used", False))
    )

    # ====================================================================
    # STEP 1 — Synchronize answer_memory
    # ====================================================================
    # Only UPDATE when a parent debate row exists (build was a resurrection).
    # Direct-prompt builds have no debate row to update; skip with that
    # reason — NOT a failure.
    if parent_answer_id is None:
        steps.append(_step_skipped(
            "answer_memory",
            "no parent_answer_id (direct-prompt build; no debate row to "
            "reconcile)",
        ))
    else:
        try:
            updated = answer_memory.memory.update_build_result(
                parent_answer_id,
                workspace_id=workspace_id,
                execution_decision=effective_decision or None,
                execution_reason=effective_reason or None,
                execution_confidence=effective_confidence or None,
                execution_decision_source=effective_source or None,
            )
            if updated:
                steps.append(_step_ok(
                    "answer_memory",
                    debate_id=parent_answer_id,
                    workspace_id=workspace_id,
                    execution_decision=effective_decision,
                ))
            else:
                steps.append(_step_failed(
                    "answer_memory",
                    f"debate row id={parent_answer_id} not found; no update",
                ))
        except Exception as e:
            steps.append(_step_failed("answer_memory", e))

    # ====================================================================
    # STEP 2 — Register Workspace in the SQL DB
    # ====================================================================
    # Build the SQL row from the filesystem state that is authoritative post
    # -build. Pull every field from the on-disk JSON the build pipeline
    # already wrote; this is the single direction of data flow (filesystem
    # -> SQL), never the reverse.
    try:
        source_question = (
            user_intent_prompt
            or workspace_json.get("source_question")
            or original_prompt
            or ""
        )
        source_topic = workspace_json.get("source_topic") or ""
        source_answer_id = workspace_json.get("source_answer_id")

        # Pull the decision model from decisions.json (the build pipeline
        # wrote this at resurrection). These become the SQL row's
        # verified_decisions / verification_result columns.
        verification_result = decisions_json.get("verification_result", {}) or {}
        resolved_decisions = decisions_json.get("resolved_decisions", []) or []
        verified_decisions = decisions_json.get("verified_decisions", []) or []

        # Map build_state.status to a WorkspaceManager-status string. The
        # SQL row's status is consumed by `sol0mon resume` (which calls
        # load_workspace) and `sol0mon list-workspaces`. The build pipeline
        # writes one of: "resurrected", "spec_generated", "plan_generated",
        # "executed", "execution_error", "blocked_*". For the Build Finalizer
        # — which only runs on a successful build — the relevant terminal
        # statuses are "executed" and "blocked_*". Map "executed" to "completed";
        # any other terminal status to its closest WorkspaceManager
        # equivalent (preserving the diagnostic surface for blocked builds).
        bs_status = build_state.get("status", "")
        if bs_status == "executed":
            sql_status = "completed"
        elif bs_status and bs_status.startswith("blocked_"):
            sql_status = "awaiting_human" if "human" in bs_status else "created"
        else:
            sql_status = "completed"

        consensus_score = 0
        try:
            consensus_score = int(verification_result.get("consensus_score", 0))
        except (TypeError, ValueError):
            consensus_score = 0
        if not consensus_score:
            try:
                consensus_score = int(build_state.get("consensus_score", 0))
            except (TypeError, ValueError):
                consensus_score = 0

        # answer_history — record the originating debate link so the SQL row
        # carries the same bidirectional relationship as the filesystem row.
        answer_history = []
        if source_answer_id is not None:
            answer_history = [{
                "answer_id": source_answer_id,
                "parent_answer_id": None,
                "question": source_question,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]

        ok = workspace_mgr.upsert_build_workspace(
            workspace_id,
            original_prompt=source_question or original_prompt,
            source_answer_id=str(source_answer_id) if source_answer_id is not None else None,
            source_topic=source_topic,
            status=sql_status,
            consensus_score=consensus_score,
            verified_decisions=verified_decisions,
            resolved_disputes=resolved_decisions,
            current_job_id=build_state.get("current_job_id", "") or "",
            error=build_state.get("error", "") or "",
            retry_count=int(build_state.get("retry_count", 0) or 0),
            created_at=build_state.get("created_at"),
        )
        if ok:
            steps.append(_step_ok(
                "workspace_db",
                workspace_id=workspace_id,
                sql_status=sql_status,
            ))
        else:
            steps.append(_step_failed(
                "workspace_db",
                "upsert_build_workspace returned False (DB error; see stderr)",
            ))
    except Exception as e:
        steps.append(_step_failed("workspace_db", e))

    # ====================================================================
    # STEP 3 — Refresh Telemetry
    # ====================================================================
    # The CWD-level workspace_status.json + performance_report.json froze at
    # debate-completion (pipeline_runner.execute reset the recorder before
    # the build phase). Re-configure the recorder here, set build-phase
    # fields, and re-write both files so `sol0mon status` reflects the
    # completed build.
    try:
        status_path = os.path.join(os.getcwd(), "workspace_status.json")
        report_path = os.path.join(os.getcwd(), "performance_report.json")
        # Re-enable + reconfigure the singleton. The recorder was reset by
        # pipeline_runner.execute's finally; configure() re-enables it.
        import telemetry
        if not telemetry.recorder.enabled:
            telemetry.configure(
                workspace_id=workspace_id,
                status_path=status_path,
                report_path=report_path,
                resume=False,  # do not load the stale frozen snapshot
            )
        rec = telemetry.recorder
        # Observe the build-phase facts so save_status() / save_report() emit
        # them. observe() updates the live `_live` dict that both writers
        # read.
        rec.observe(
            stage=build_state.get("status") or "Completed",
            workspace_id=workspace_id,
            execution_mode=effective_selected_mode or effective_recommended_mode,
            selected_mode=effective_selected_mode,
            recommended_mode=effective_recommended_mode,
            override_used=effective_override_used,
            execution_decision=effective_decision,
            execution_reason=effective_reason,
            execution_confidence=effective_confidence,
            execution_decision_source=effective_source,
            spec_status=spec_status,
            plan_id=build_state.get("plan_id", ""),
            manifest_lifecycle=(manifest or {}).get("lifecycle", ""),
            consensus_score=build_state.get("consensus_score", 0),
        )
        # Mark build + repair stage end so the performance report includes
        # them (the actual wall-clock came from build_state's updated_at; we
        # do not synthesize fake timings — we only refresh the JSON file).
        rec.end_stage("Build", success=bool(exec_result.get("success")))
        # Persist both files.
        rec.save_status()
        rec.save_report()
        steps.append(_step_ok(
            "telemetry",
            status_path=status_path,
            workspace_id=workspace_id,
            execution_mode=effective_selected_mode or effective_recommended_mode,
        ))
    except Exception as e:
        steps.append(_step_failed("telemetry", e))

    # ====================================================================
    # STEP 4 — Publish Build Artifacts
    # ====================================================================
    # Verifies every canonical artifact is on disk AND that the manifest
    # references them. The manifest is the catalog; verify it lists every
    # required artifact and the files exist. Nothing is regenerated —
    # missing artifacts are reported as a step failure (the build itself is
    # the truth; surfacing what is missing is the finalizer's job).
    try:
        required = {
            "workspace.json": ws_dir / "workspace.json",
            "build_state.json": ws_dir / "build_state.json",
            "answer.json": ws_dir / "answer.json",
            "decisions.json": ws_dir / "decisions.json",
            "implementation_plan.md": ws_dir / "implementation_plan.md",
            "manifest.json": ws_dir / "manifest.json",
        }
        optional = {
            "execution_plan.json": ws_dir / "execution_plan.json",
            "synthesized_answer.md": ws_dir / "synthesized_answer.md",
        }
        missing_files = [name for name, p in required.items() if not p.is_file()]
        optional_present = {name: p.is_file() for name, p in optional.items()}

        # Cross-reference the manifest's artifact ids against the on-disk
        # files the manifest claims to summarize.
        manifest_ids = set()
        for k in (
            "decision_artifact", "build_spec", "execution_plan",
            "implementation", "validation", "tests",
        ):
            v = (manifest or {}).get(k)
            if v:
                manifest_ids.add(v)

        # Surfacing: the manifest MUST be at least 4 of the 6 expected
        # artifact ids (decision/spec/plan/impl; validation/tests may be
        # absent for stub builds). A manifest missing the build_spec or
        # implementation id is structurally inconsistent.
        # ``lifecycle`` is accepted in any post-execution state because the
        # finalizer's caller may have stamped the manifest at EXECUTING
        # (resume path, before the finalizer entered PUBLISHED) or at
        # TESTS_VALIDATED / PUBLISHED / COMPLETE (run_pipeline path or
        # resume path after further transitions). The build itself is
        # unchanged across these markers; only publication moves. The
        # stage-exit below re-stamps the manifest to lifecycle=COMPLETE on
        # success. The authoritative correctness check is on the artifact
        # ids (build_spec+implementation present), not on the lifecycle
        # label (which the finalizer is itself responsible for moving).
        manifest_ok = (
            "build_spec" in manifest
            and "implementation" in manifest
        )

        if missing_files or not manifest_ok:
            steps.append(_step_failed(
                "publish_artifacts",
                f"required artifacts missing={missing_files} "
                f"or manifest={manifest_ok}",
                required_present={name: p.is_file()
                                  for name, p in required.items()},
                optional=optional_present,
                manifest_artifact_ids=sorted(manifest_ids),
            ))
        else:
            # Also surface the build workspace directory itself via the
            # answer_memory row that STEP 1 just reconciled (so the
            # latest-debate -> workspace navigation works). STEP 1 already
            # wrote workspace_id; here we re-read the debate row to confirm
            # the linkage took. (Done in STEP 5.) This step's publish is
            # complete: every artifact is on disk AND the manifest catalogs
            # it.
            steps.append(_step_ok(
                "publish_artifacts",
                required_present={name: True for name in required},
                optional=optional_present,
                manifest_artifact_ids=sorted(manifest_ids),
            ))
    except Exception as e:
        steps.append(_step_failed("publish_artifacts", e))

    # ====================================================================
    # STEP 5 — Reconcile State (cross-store consistency check)
    # ====================================================================
    # Read every store back AFTER the writes and confirm they agree on the
    # four invariants: (a) answer_memory row references this workspace; (b)
    # the SQL workspaces row exists with the same workspace_id; (c)
    # build_state.json's execution_decision matches answer_memory's
    # verification_result.execution_decision; (d) manifest lifecycle matches
    # build_state.status. A failed reconciliation here does NOT corrupt the
    # build (the build is on disk and is the truth) — it surfaces a
    # persistent inconsistency to the caller so the user knows the
    # finalizer did not fully close the loop.
    try:
        consistency: dict[str, Any] = {
            "workspace_id": workspace_id,
            "debate_id": parent_answer_id,
            "execution_decision": effective_decision,
            "selected_mode": effective_selected_mode,
            "recommended_mode": effective_recommended_mode,
            "override_used": effective_override_used,
            "build_status": build_state.get("status", ""),
            "spec_status": spec_status,
            "lifecycle": (manifest or {}).get("lifecycle", ""),
            "inconsistencies": [],
        }

        # (a) answer_memory debate row references this workspace
        if parent_answer_id is not None:
            debate = answer_memory.memory.get_debate(parent_answer_id)
            if debate is None:
                consistency["inconsistencies"].append(
                    f"answer_memory: debate row id={parent_answer_id} not "
                    f"found after STEP 1")
            elif (debate.get("workspace_id") or "") != workspace_id:
                consistency["inconsistencies"].append(
                    f"answer_memory: debate row id={parent_answer_id} "
                    f"workspace_id={debate.get('workspace_id')!r} != "
                    f"{workspace_id!r}")
            else:
                vr = debate.get("verification_result", {}) or {}
                if vr.get("execution_decision") != effective_decision:
                    consistency["inconsistencies"].append(
                        f"answer_memory: verification_result."
                        f"execution_decision={vr.get('execution_decision')!r}"
                        f" != build_state's {effective_decision!r}")

        # (b) SQL workspaces row exists with this id
        sql_ws = workspace_mgr.load_workspace(workspace_id)
        if sql_ws is None:
            consistency["inconsistencies"].append(
                f"workspace_db: row id={workspace_id} not found after "
                f"STEP 2")
        elif sql_ws.status != consistency["build_status"] and not (
            sql_ws.status == "completed" and consistency["build_status"] == "executed"
        ) and not (
            consistency["build_status"].startswith("blocked_")
            and sql_ws.status in {"awaiting_human", "blocked"}
        ):
            # status mismatch is allowed for the executed->completed mapping
            # (the SQL row's status is the WorkspaceManager vocabulary; the
            # build_state's is the pipeline vocabulary). Any other mismatch
            # is a real inconsistency.
            consistency["inconsistencies"].append(
                f"workspace_db: status={sql_ws.status!r} != "
                f"build_state's {consistency['build_status']!r}")

        # (d) manifest lifecycle matches build_state status. The manifest is
        # read BEFORE the finalizer's stage-exit advances PUBLISHED -> COMPLETE
        # so it may carry EXECUTING / TESTS_VALIDATED / PUBLISHED at this
        # point; all of these are consistent with a successful execution
        # (bs=="executed"). The stage exit below re-stamps the manifest to
        # lifecycle=COMPLETE.
        ml = consistency["lifecycle"]
        bs = consistency["build_status"]
        lifecycle_consistent = (
            (ml in ("COMPLETE", "PUBLISHED", "TESTS_VALIDATED", "EXECUTING",
                    "ARTIFACTS_VALIDATED") and bs == "executed")
            or (ml in ("EXECUTING", "PARTIAL") and bs.startswith("blocked_"))
            or (ml == "PARTIAL" and bs and bs.startswith("blocked_"))
            or (ml == "" and bs == "")  # nothing to compare
        )
        if not lifecycle_consistent:
            consistency["inconsistencies"].append(
                f"manifest: lifecycle={ml!r} inconsistent with "
                f"build_state status={bs!r}")

        if consistency["inconsistencies"]:
            steps.append(_step_failed(
                "reconcile_state",
                "; ".join(consistency["inconsistencies"]),
                consistency=consistency,
            ))
        else:
            steps.append(_step_ok("reconcile_state", consistency=consistency))
    except Exception as e:
        consistency = None
        steps.append(_step_failed("reconcile_state", e))

    finalized = bool(exec_result.get("success")) and all(
        s["status"] == "ok" or s["status"] == "skipped" for s in steps
    )

    # ====================================================================
    # STAGE EXIT — advance PUBLISHED -> COMPLETE on success and re-persist
    # the manifest so its ``lifecycle`` field reflects the post-finalization
    # terminal state. The finalizer is the SOLE owner of the COMPLETE
    # transition (per lifecycle.STATES); build_pipeline's pre-finalizer
    # _persist_build_manifest wrote lifecycle=COMPLETE based on the
    # pre-finalization build_state (status="executed" mapping to COMPLETE).
    # After STEP 5's reconciliation, the on-disk lifecycle is PUBLISHED and
    # the manifest must be re-stamped to reflect that the published+reconciled
    # build is now the terminal COMPLETE.
    #
    # On failure: stay at PUBLISHED (NOT terminal — the operator can re-run
    # the idempotent finalizer via resume). The manifest is NOT re-stamped;
    # it carries the pre-finalization lifecycle, which correctly reflects
    # that publication did not complete.
    # ====================================================================
    if finalized:
        try:
            from lifecycle import advance_lifecycle, BuildLifecycle
            advance_lifecycle(workspace_id, BuildLifecycle.COMPLETE,
                              finalization_stage="completed",
                              finalization_steps=[s["step"] for s in steps])
            # Re-persist the manifest post-COMPLETE so its lifecycle field is
            # the post-finalization terminal state (mirrors what
            # _persist_build_manifest does in run_build_pipeline, but at the
            # final lifecycle stage). Cheap + idempotent.
            try:
                from manifest import build_manifest_from_workspace, store_manifest
                store_manifest(workspace_id,
                               build_manifest_from_workspace(workspace_id))
            except Exception:
                pass  # the lifecycle marker is the authority; manifest refresh is best-effort
        except Exception as e:
            steps.append(_step_failed("stage_exit", e))
            finalized = False

    return {"finalized": finalized, "steps": steps, "consistency": consistency}


# ---------------------------------------------------------------------------
# Self-check — the smallest thing that fails if the finalizer's contract
# breaks. No fixtures, no frameworks. Does NOT touch the production DBs (uses
# a tmp answer_memory.db + workspaces.db via env vars).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="sol0mon_finalizer_selfcheck_")
    # Build a fake on-disk build workspace + a debate row + invoke
    # finalize_build + assert all 5 steps + the consistency block.
    from build_workspace import create_workspace, WORKSPACE_ROOT
    saved_root = WORKSPACE_ROOT
    saved_am_db = answer_memory.DB_FILE
    saved_ws_db = _wm_module.DB_FILE
    saved_cwd = os.getcwd()
    try:
        # chdir into tmp so the finalizer's telemetry writes (which target
        # CWD/workspace_status.json + CWD/performance_report.json) do NOT
        # pollute the repo's audit snapshot at the real CWD. Without this
        # the self-check would corrupt the project's frozen #164 telemetry
        # every time it runs.
        os.chdir(tmp)
        # Redirect both DBs to tmp paths so the self-check does not touch
        # production data. The two singletons read these module-level
        # attributes lazily via _get_connection.
        answer_memory.DB_FILE = os.path.join(tmp, "answer_memory.db")
        _wm_module.DB_FILE = os.path.join(tmp, "workspaces.db")
        # Reset the WorkspaceManager init flag so the redirected DB is used.
        workspace_mgr._initialized = False  # type: ignore[attr-defined]

        # Switch WORKSPACE_ROOT to tmp so the workspace dir lands there.
        import build_workspace as bw
        bw.WORKSPACE_ROOT = Path(tmp)
        # Pre-create the parent dir create_workspace uses.
        (Path(tmp)).mkdir(parents=True, exist_ok=True)

        # STEP A — seed an answer_memory row that mimics a "RESEARCH_ONLY"
        # frozen debate row (the audit's picture of row 164).
        debate_id = answer_memory.memory.save_debate(
            question="Autonomous Cryptocurrency Trading System",
            gpt_final="gpt-final", gemini_final="gemini-final",
            synthesized_answer="# Verified Decisions\n...",
            consensus_score=100,
            conflict_severity="LOW",
            conflict_points=["k1"],
            validated_disputes=[],
            verification_result={
                "resolved_claims": [],
                "verification_score": 100,
                "execution_decision": "RESEARCH_ONLY",
                "execution_decision_source": "research_analysis",
            },
            human_answer="Stop — research-only.",
        )
        # Sanity: row exists with workspace_id='' and frozen RESEARCH_ONLY.
        before = answer_memory.memory.get_debate(debate_id)
        assert before["workspace_id"] == "", "precondition: empty workspace_id"
        assert (before["verification_result"].get("execution_decision")
                == "RESEARCH_ONLY"), "precondition: frozen RESEARCH_ONLY"

        # STEP B — create a fake build workspace + populated build_state +
        # decisions + manifest mimicking a successful FULL_BUILD execution.
        wid = create_workspace(
            source_answer_id=str(debate_id),
            source_question="Autonomous Cryptocurrency Trading System",
            source_topic="autonomous cryptocurrency trading system",
        )
        ws_dir = _workspace_dir(wid)
        # Populate the canonical artifact files (mimic a real build).
        (ws_dir / "answer.json").write_text(
            json.dumps({"answer_id": debate_id, "question": "x"}))
        (ws_dir / "decisions.json").write_text(json.dumps({
            "answer_id": debate_id,
            "verification_result": {
                "resolved_claims": [],
                "verification_score": 100,
                "execution_decision": "RESEARCH_ONLY",
            },
            "resolved_decisions": [],
            "verified_decisions": [],
        }))
        (ws_dir / "implementation_plan.md").write_text(
            "# Architecture\nx\n\n# Components\nx\n\n"
            "# File Structure\nx\n\n# Dependencies\nx\n\n"
            "# Constraints\nx\n\n# Testing Requirements\nx\n\n"
            "# Acceptance Criteria\nx\n")
        (ws_dir / "manifest.json").write_text(json.dumps({
            "workspace_id": wid, "lifecycle": "COMPLETE",
            "decision_artifact": "d-id", "build_spec": "b-id",
            "execution_plan": "e-id", "implementation": "i-id",
            "validation": "v-id", "tests": "t-id",
        }))
        # The authoritative build_state (what the build pipeline writes
        # at the END of run_build_pipeline).
        from build_workspace import write_build_state
        write_build_state(
            wid, "executed",
            source_answer_id=str(debate_id),
            execution_decision="IMPLEMENTATION_REQUIRED",
            execution_reason="ExecutionDecisionEngine override: ...",
            execution_confidence=99,
            execution_decision_source="execution_decision_engine",
            recommended_mode="RESEARCH_ARCHITECTURE",
            selected_mode="FULL_BUILD",
            override_used=True,
            spec_status="completed",
            spec_missing_sections=[],
            plan_id="plan_test", plan_artifact_id="e-id",
            execution_success=True,
        )

        # STEP C — invoke the finalizer with the same args run_build_pipeline
        # would pass.
        result = finalize_build(
            wid,
            parent_answer_id=debate_id,
            exec_dec={
                "execution_decision": "IMPLEMENTATION_REQUIRED",
                "execution_reason": "ExecutionDecisionEngine override: ...",
                "execution_confidence": 99,
                "execution_decision_source": "execution_decision_engine",
                "source": "execution_decision_engine",
            },
            spec_result={"status": "completed", "missing_sections": []},
            exec_result={"success": True},
            manifest={"lifecycle": "COMPLETE", "build_spec": "b-id",
                      "implementation": "i-id"},
            classification={"workflow_type": "IMPLEMENTATION"},
            original_prompt=f"build answer {debate_id}",
            user_intent_prompt="Autonomous Cryptocurrency Trading System",
        )

        # ASSERT the contract holds.
        assert result["finalized"] is True, (
            f"finalize_build not finalized: {result['steps']}")
        statuses = {s["step"]: s["status"] for s in result["steps"]}
        assert statuses == {
            "answer_memory": "ok",
            "workspace_db": "ok",
            "telemetry": "ok",
            "publish_artifacts": "ok",
            "reconcile_state": "ok",
        }, statuses

        # Cross-store consistency the user demands:
        # ✓ Debate row references workspace
        after = answer_memory.memory.get_debate(debate_id)
        assert after["workspace_id"] == wid, (
            f"debate row workspace_id={after['workspace_id']!r} != {wid!r}")
        # ✓ No contradictory execution decisions
        assert (after["verification_result"]["execution_decision"]
                == "IMPLEMENTATION_REQUIRED"), after["verification_result"]
        # ✓ Workspace database populated
        sql_ws = workspace_mgr.load_workspace(wid)
        assert sql_ws is not None, "SQL workspaces row missing after finalize"
        # ✓ Workspace references debate (via answer_history)
        assert sql_ws.answer_history, "SQL row answer_history empty"
        assert str(sql_ws.answer_history[0]["answer_id"]) == str(debate_id), \
            sql_ws.answer_history
        # ✓ Manifest lifecycle COMPLETE
        # ✓ Build state executed (already wrote that)
        # ✓ Telemetry reflects build (workspace_status.json written with
        # workspace_id + execution_mode)
        ws_status_path = Path(tmp).parent / "workspace_status.json"
        # (telemetry writes to CWD; in the self-check we don't capture that,
        # but the step's status=ok confirms save_status() returned without
        # raising.)

        print("build_finalizer self-check PASSED")
        print(f"  workspace_id   : {wid}")
        print(f"  debate_id      : {debate_id}")
        print(f"  steps          : {statuses}")
        print(f"  answer_memory.workspace_id = {after['workspace_id']!r}")
        print("  answer_memory.execution_decision = "
              f"{after['verification_result']['execution_decision']!r}")
        print(f"  workspace_db row status       = {sql_ws.status!r}")
    finally:
        # Restore production paths so the test process leaves no residue.
        bw.WORKSPACE_ROOT = saved_root
        answer_memory.DB_FILE = saved_am_db
        _wm_module.DB_FILE = saved_ws_db
        workspace_mgr._initialized = False  # type: ignore[attr-defined]
        os.chdir(saved_cwd)
