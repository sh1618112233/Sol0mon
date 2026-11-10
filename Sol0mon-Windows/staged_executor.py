"""
Project Sol0mon - Staged Executor (autonomous per-stage implementation +
validation + repair + checkpoint loop).

Replaces the monolithic "one ``execute_build_spec`` call over the whole
spec" anti-pattern with an engineering-stage loop:

    For each pending stage in build_plan.json order (deps-aware):
      1. Build the per-stage spec (sliced from implementation_plan.md).
      2. Implementation:
         - reuse ``implementation_executor.execute_implementation`` for the
           initial generation + its built-in 3-attempt repair loop
           (``_execute_with_repair_loop`` — root-cause: no parallel repair
           engine to maintain, no duplicated test runner).
      3. Validation:
         - per-stage validators (``validation.validate_tests`` for the tests
           stage; ``validation.validate_build_spec`` for the decisions stage;
           composite ``validate_final_build`` for the acceptance stage).
         - Optional deeper validation via ``validation_engine`` when the
           stage's kind warrants it (``component`` / ``tests`` /
           ``integration`` run the full BuildValidator pipeline; the
           validation/acceptance stages run the QualityGate).
      4. Repair loop (continuous, persisted):
         - If the inner executor's repair budget exhausted AND tests still
           fail, the staged executor drives an OUTER repair loop:
           ``MAX_STAGE_REPAIR_ITERATIONS`` additional rounds that build a
           stronger repair prompt embedding the stage's prior failure
           history (``repair_history.json``), invoke the executor again,
           retest. Each outer attempt is persisted to
           ``repair_history.json`` with a fingerprint so a future attempt
           does NOT repeat the same strategy (the same anti-oscillation
           technique ``repair_memory.RepairMemory`` uses at the validation
           tier — reused here via the same fingerprint idea, not by
           re-importing the SQL layer; the per-stage file is the local
           cache).
      5. Checkpoint:
         - On pass: ``stage_status.write_stage_checkpoint(...)`` writes
           ``checkpoints/<stage_id>.json`` with the artifacts delta, test
           results, repair history, manifest_delta. The stage transitions
           to DONE.
         - On fail (autonomous progress exhausted): the stage transitions
           to FAILED, a HUMAN_REQUIRED decision is persisted via
           ``human_decision_manager.save_pending`` (same escalation
           contract as ``implementation_executor._emit_human_required``),
           build-level state is FAILED, and the loop exits — resumable
           later via ``resume_build`` (the operator resolves the human
           decision; the staged loop continues from the next pending
           stage on the next resume).

    Integration validation:
      - After all implementation stages are DONE, the executor runs ONE
        final integration pass: it composes an integration spec listing
        every file produced across stages + invokes the executor once
        more with ``integration`` intent + runs the full
        ``validation_engine.NativeValidationEngine`` (when available) or
        the inline validators. This is the spec's INTEGRATION VALIDATION
        stage.

# Backward compatibility

    A workspace WITHOUT ``build_plan.json`` (legacy single-stage, OR a
    staged plan that produced zero stages) is handled by the caller via
    the existing ``execute_build_spec`` path — the ``staged_executor`` is
    NOT invoked on it. The caller's gate is ``stage_status.has_staged_layout``.

# Single source of truth + non-negotiable ownership

    -``lifecycle.BuildLifecycle`` remains the build-level authority. The
     staged executor writes per-stage state via ``stage_status``; the
     build-level transition (e.g. ``EXECUTING -> TESTS_VALIDATED``) is left
     to the caller (build_pipeline, resume) so the existing lifecycle
     contract is unchanged.
    -``build_finalizer.finalize_build`` remains the SOLE owner of the
     PUBLISHED -> COMPLETE transition. The staged executor does NOT call
     it; the caller does, once integration validation passes.
    -``manifest.refresh_manifest`` is the canonical manifest writer; the
     staged executor only records a per-stage ``manifest_delta`` in the
     checkpoint for the manifest module / finalizer to fold in.
    -``implementation_executor._run_tests`` / ``_tests_passed`` remain the
     pass/fail contract; this module reuses them, never reimplements.
    -``human_decision_manager.save_pending`` remains the single escalation
     point; reused here for stage-level HUMAN_REQUIRED escalations.

Pure stdlib at import time; all external calls (LLM agent, tests, validators)
are seams / lazy imports so the module is side-effect-free to load. Tests
inject ``agent_fn`` / ``test_fn`` / ``validate_fn`` to run fully offline.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

import stage_status
from stage_status import StageStatus


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The outer per-stage repair budget. ``implementation_executor`` runs an
# inner loop of ``MAX_REPAIR_ATTEMPTS=3`` per call; the staged executor's
# outer loop wraps that with another ``MAX_STAGE_REPAIR_ITERATIONS`` rounds
# so the total budget per stage = MAX_STAGE_REPAIR_ITERATIONS * inner.
# Tuned so a stuck stage fails fast (≤ a few minutes wall-time) while still
# giving the agent multiple chances to recover from non-trivial defects.
MAX_STAGE_REPAIR_ITERATIONS = 2

# Integration-stage outer budget (kept tight because the integration step
# pulls every implemented file together — large enough to recover from
# import / wiring errors, small enough to fail fast on a fundamental
# architectural mistake that needs human review).
MAX_INTEGRATION_ITERATIONS = 2

# The stage id used for the integration-validation pseudo-stage. The
# pseudo-stage's status lives in stage_status.json like any other stage.
INTEGRATION_STAGE_ID = "_integration"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_dir(workspace_id: str):
    from build_workspace import _workspace_dir as _w
    return _w(workspace_id)


def _tests_passed(test_results: dict) -> bool:
    """Delegate to ``implementation_executor._tests_passed`` — the canonical
    pass/fail contract (single source of truth)."""
    from implementation_executor import _tests_passed as _tp
    return _tp(test_results)


def _record_repair(
    workspace_id: str,
    stage_id: str,
    attempt: int,
    reason: str,
    strategy: str,
    prompt_excerpt: str,
    test_results: dict | None,
) -> None:
    """Persist one repair-attempt record into ``repair_history.json`` with
    a deterministic fingerprint so the next attempt's prompt can detect
    repeated strategies (anti-oscillation)."""
    import hashlib
    fp_input = f"{stage_id}|{strategy}|{reason[:200]}"
    fp = hashlib.sha256(fp_input.encode("utf-8")).hexdigest()[:16]
    entry = {
        "attempt": attempt,
        "reason": reason,
        "strategy": strategy,
        "fingerprint": fp,
        "prompt_excerpt": (prompt_excerpt or "")[:400],
        "test_failed": (test_results or {}).get("failed"),
        "test_exit_code": (test_results or {}).get("exit_code"),
    }
    stage_status.append_repair_history(workspace_id, stage_id, entry)


def _persist_test_results(workspace_id: str, stage_id: str, results: dict) -> None:
    stage_status.write_stage_test_results(workspace_id, stage_id, results)


def _emit_stage_human_required(
    workspace_id: str,
    stage_id: str,
    last_test_output: str,
    attempts: int,
) -> dict:
    """Persist a HUMAN_REQUIRED decision for a stage-level escalation (same
    contract as ``implementation_executor._emit_human_required``). The build
    finalizer and the resume engine both consume pending-human decisions
    exactly the same way — no escalation-aware code path is duplicated."""
    from human_decision_manager import save_pending
    from build_workspace import write_build_state

    decision = {
        "decision_id": f"stage_failed_{stage_id[:32]}",
        "dispute_key": f"stage_repair:{stage_id}",
        "dispute_text": f"Stage '{stage_id}' could not pass autonomous repair",
        "option_a": "Manual code fix then resume",
        "option_b": "Abort build",
        "reasoning": (
            f"The staged executor exhausted its repair budget "
            f"({attempts} outer attempts) on stage '{stage_id}' without "
            f"reaching a passing test outcome. Manual intervention required."
        ),
        "outcome_type": "HUMAN_REQUIRED",
        "source": "staged_executor.stage_repair_loop",
        "stage_id": stage_id,
        "attempts": attempts,
        "last_test_output": (last_test_output or "")[:500],
    }
    save_pending(workspace_id, [decision])
    write_build_state(workspace_id, "repair_exhausted",
                      stage_id=stage_id, human_required=True,
                      last_test_output=(last_test_output or "")[:500])
    return decision


def _stronger_repair_prompt(stage: dict, repair_history: list[dict]) -> str:
    """Build a per-stage repair prompt that AWARE of the failure history.
    The base implementation executor builds a fresh prompt each attempt
    that only references the LAST test output; this outer-prompt embeds
    the strategies already tried + a directive to NOT repeat them — the
    anti-oscillation signal. Mirrors the escalation ladder
    ``repair_memory.ESCALATION_LADDER`` uses (patch -> rewrite_test -> ...),
    surfaced via the ``strategy`` field of each repair-history entry."""
    tried = "\n".join(
        f"  - attempt {h.get('attempt', '?')}: strategy={h.get('strategy','?')} "
        f"reason={h.get('reason','?')[:120]}"
        for h in repair_history[-6:]  # last 6 attempts at most (bounded prompt)
    ) or "  (no prior attempts)"
    return (
        f"# STAGED REPAIR — Stage {stage.get('stage_id','?')} "
        f"({stage.get('title','')})\n\n"
        f"The autonomous repair loop has run {len(repair_history)} prior attempt(s) "
        f"on this stage. They did NOT produce a passing test outcome. Prior "
        f"attempts (do NOT repeat the same strategy):\n{tried}\n\n"
        f"Apply a DIFFERENT, more-aggressive repair this time. The original "
        f"stage spec follows for context.\n\n"
        f"{stage.get('stage_spec_excerpt','')}"
    )


def _list_workspace_files(working_dir: str) -> set[str] | None:
    """Delegate to ``implementation_executor._list_workspace_files`` so the
    same exclusions (.pytest_cache, __pycache__) and None-on-falsy-dir
    contract apply. Re-implementation risk avoided — single source of truth."""
    from implementation_executor import _list_workspace_files as _lwf
    return _lwf(working_dir)


# ---------------------------------------------------------------------------
# Per-stage execution
# ---------------------------------------------------------------------------


def execute_stage(
    workspace_id: str,
    stage: dict,
    *,
    agent_fn: Callable[..., Any] | None = None,
    test_fn: Callable[..., dict] | None = None,
    max_outer_iterations: int = MAX_STAGE_REPAIR_ITERATIONS,
    working_dir: str | None = None,
) -> dict:
    """Execute ONE engineering stage to completion (DONE or FAILED). Does
    NOT advance the build-level lifecycle — the caller does that.

    Inputs:
        workspace_id:    Canonical UUID (resolved by ``_workspace_dir``).
        stage:           A stage dict from ``build_plan.json`` (must have
                         ``stage_id``, ``stage_spec_excerpt``, ``files``,
                         ``dependencies``, ``title``).
        agent_fn:        Optional injected agent seam (offline tests). When
                         None, resolved by ``implementation_executor`` via
                         the coding_agent_router. Same shape
                         ``implementation_executor.execute_implementation``
                         expects.
        test_fn:         Optional injected test runner seam. When None,
                         ``implementation_executor._run_tests`` is used.
        max_outer_iterations: The outer repair budget. The inner executor's
                         own ``MAX_REPAIR_ATTEMPTS`` (3) runs each iteration.
        working_dir:     The directory the agent runs in. Defaults to the
                         build workspace dir.

    Returns:
        {
          "stage_id": str,
          "status":   "DONE" | "FAILED",
          "attempts":  int,         # outer attempts made (0 means the first
                                    # executor call already passed)
          "exec_result": {...},     # the last executor return
          "test_results": {...},   # last test result
          "artifacts_delta": [..], # new files written this stage
          "human_required": bool,  # escalated when FAILED
        }
    """
    stage_id = stage.get("stage_id", "")
    if not stage_id:
        return {"stage_id": "", "status": StageStatus.FAILED.value,
                "error": "stage missing stage_id", "attempts": 0}
    if working_dir is None:
        working_dir = str(_workspace_dir(workspace_id))
    # Transition stage PENDING -> IMPLEMENTING (idempotent from already-IMPLEMENTING
    # resume — the executor picks up where it left off).
    current = stage_status.read_stage_status(workspace_id, stage_id).get("status")
    if current in (None, StageStatus.PENDING.value):
        stage_status.write_stage_status(workspace_id, stage_id, StageStatus.IMPLEMENTING)

    # Files-before snapshot so the artifacts-delta is computable AND so the
    # post-execution check has something to compare against.
    files_before = _list_workspace_files(working_dir) or set()

    # The stage spec excerpt already carries the staged directive + file
    # list + the full original spec for context. Pass it verbatim.
    stage_spec = stage.get("stage_spec_excerpt", "")

    # --- outer repair loop ---
    from implementation_executor import execute_implementation, MAX_REPAIR_ATTEMPTS

    attempts = 0
    exec_result: dict = {}
    test_results: dict = {}
    last_error = ""
    while attempts < max_outer_iterations + 1:  # +1: initial attempt is "iteration 0"
        attempts += 1
        # Inner: the executor's own repair loop (3 attempts) runs per outer call.
        if attempts == 1:
            plan = stage_spec
        else:
            # Outer retry: build a stronger prompt aware of prior attempts.
            hist = stage_status.read_repair_history(workspace_id, stage_id) or []
            if hist:
                plan = _stronger_repair_prompt(stage, hist)
                _record_repair(workspace_id, stage_id, attempts - 1,
                               reason=last_error or "tests still failing",
                               strategy="outer_retry",
                               prompt_excerpt=plan,
                               test_results=test_results)
            else:
                plan = stage_spec

        stage_status.write_stage_status(workspace_id, stage_id,
                                         StageStatus.REPAIRING if attempts > 1
                                         else StageStatus.IMPLEMENTING,
                                         attempts_increment=1) \
            if attempts > 1 else None

        exec_result = execute_implementation(
            plan=plan,
            workspace_id=workspace_id,
            working_dir=working_dir,
            use_claude_code=True,
            agent_fn=agent_fn,
            test_fn=test_fn,
            max_repairs=MAX_REPAIR_ATTEMPTS,
        )
        test_results = exec_result.get("test_results", {}) or {}
        _persist_test_results(workspace_id, stage_id, test_results)

        if exec_result.get("success") and _tests_passed(test_results):
            # Pass: write checkpoint, transition DONE.
            files_after = _list_workspace_files(working_dir) or set()
            delta = sorted(files_after - files_before)
            artifacts = [{"path": rel, "stage_id": stage_id} for rel in delta]
            stage_status.write_stage_checkpoint(
                workspace_id, stage_id,
                artifacts=artifacts,
                test_results=test_results,
                repair_history=stage_status.read_repair_history(workspace_id, stage_id) or [],
                manifest_delta={
                    "stage_id": stage_id,
                    "files": delta,
                    "exec_result_success": True,
                },
            )
            stage_status.write_stage_status(workspace_id, stage_id, StageStatus.DONE,
                                             attempts=attempts,
                                             completed_at=_now_iso())
            return {
                "stage_id": stage_id,
                "status": StageStatus.DONE.value,
                "attempts": attempts,
                "exec_result": exec_result,
                "test_results": test_results,
                "artifacts_delta": delta,
                "human_required": False,
            }

        # Failure path. Either the executor returned success=False outright,
        # or it returned success=True but the tests didn't actually pass
        # (the strengthened contract: non-empty test_results +
        # _tests_passed == True). Persist the failure and try the outer
        # loop once more.
        last_error = exec_result.get("error") or ""
        if not last_error and not _tests_passed(test_results):
            last_error = (
                "tests not passing: runner="
                f"{test_results.get('runner','?')}, passed="
                f"{test_results.get('passed',0)}, failed="
                f"{test_results.get('failed','?')}"
            )
        _record_repair(workspace_id, stage_id, attempts,
                       reason=last_error,
                       strategy="inner_repair_loop",
                       prompt_excerpt=plan,
                       test_results=test_results)
        # If the executor itself returned HUMAN_REQUIRED (its own inner loop
        # exhausted), we DO NOT burn another outer iteration — that would
        # just re-invoke the agent on the same failed-with-HUMAN_REQUIRED
        # state. Escalate immediately.
        if exec_result.get("error", "").startswith("Repair loop already exhausted"):
            break

    # All outer iterations exhausted: escalate.
    last_output = (test_results.get("output") or exec_result.get("output") or "")[-500:]
    _emit_stage_human_required(workspace_id, stage_id, last_output, attempts)
    stage_status.write_stage_status(workspace_id, stage_id, StageStatus.FAILED,
                                    attempts=attempts, completed_at=_now_iso())
    return {
        "stage_id": stage_id,
        "status": StageStatus.FAILED.value,
        "attempts": attempts,
        "exec_result": exec_result,
        "test_results": test_results,
        "artifacts_delta": [],
        "human_required": True,
        "error": last_error,
    }


# ---------------------------------------------------------------------------
# Full staged execution
# ---------------------------------------------------------------------------


def execute_stages(
    workspace_id: str,
    *,
    agent_fn: Callable[..., Any] | None = None,
    test_fn: Callable[..., dict] | None = None,
    integration_validate_fn: Callable[..., dict] | None = None,
    working_dir: str | None = None,
    max_stage_repair_iterations: int = MAX_STAGE_REPAIR_ITERATIONS,
    max_integration_iterations: int = MAX_INTEGRATION_ITERATIONS,
) -> dict:
    """Drive every pending stage in ``build_plan.json`` order to DONE (or
    FAILED) and then run one integration validation pass.

    Args:
        workspace_id:    Canonical UUID.
        agent_fn /       test_fn / integration_validate_fn: optional offline
                         test seams. The integration_validate_fn replaces
                         the integration-validation step's validation call
                         (shape: ``validate_fn(workspace_id, working_dir) -> dict``
                         with ``{"passed": bool, ...}``).
        working_dir:     Override the working dir; defaults to
                         ``_workspace_dir(workspace_id)``.
        max_stage_repair_iterations: Per-stage outer repair budget. The
                         inner executor's own ``MAX_REPAIR_ATTEMPTS`` (3)
                         runs per outer attempt. Tests pass a small value
                         (0 or 1) for fast failure-scenario smoke checks.
        max_integration_iterations: Integration-stage outer budget.

    Returns:
        {
          "workspace_id": str,
          "stages_total": int,
          "stages_done": int,
          "stages_failed": int,
          "stage_results": [ {stage execution result...}, ... ],
          "integration":   {validation result} | None,
          "success":       bool,    # True iff every stage DONE + integration
                                    # validation passed.
          "blocked":       bool,    # True if any stage failed (HUMAN_REQUIRED).
        }

    Idempotent: re-entering on a workspace with partially-DONE stages
    continues from the next pending stage via stage_status.next_pending_stage
    (deps-aware). DONE stages are NOT re-executed (the executor's contract).
    """
    if working_dir is None:
        working_dir = str(_workspace_dir(workspace_id))
    if not stage_status.has_staged_layout(workspace_id):
        # No staged plan -> the caller should have routed to execute_build_spec
        # (legacy single-shot). This is the safety net: we surface a structured
        # response so the caller can branch rather than crashing.
        return {
            "workspace_id": workspace_id,
            "stages_total": 0,
            "stages_done": 0,
            "stages_failed": 0,
            "stage_results": [],
            "integration": None,
            "success": False,
            "blocked": False,
            "skipped": True,
            "reason": "no_staged_layout",
        }

    plan = stage_status.read_build_plan(workspace_id)
    stages = plan.get("stages") or []
    stage_results: list[dict] = []
    blocked = False

    while True:
        stage = stage_status.next_pending_stage(workspace_id)
        if stage is None:
            break
        result = execute_stage(
            workspace_id, stage,
            agent_fn=agent_fn, test_fn=test_fn,
            working_dir=working_dir,
            max_outer_iterations=max_stage_repair_iterations,
        )
        stage_results.append(result)
        if result.get("status") == StageStatus.FAILED.value:
            blocked = True
            break  # one failed stage blocks the whole build pending human.

    # stage_results collects the per-invocation work; ``total_done`` below is
    # the authoritative "how many stages are DONE" obtained from
    # stage_status.json (the persisted record). ``stages_failed`` is the
    # per-invocation failure count (a resume after fixing a failed stage
    # reports 0 failed + a fresh per-stage result for the just-run stage).
    stages_failed = sum(1 for r in stage_results
                        if r.get("status") == StageStatus.FAILED.value)

    # If any stage failed, do NOT run integration validation — the build is
    # blocked pending human intervention; integration validation now would
    # only re-confirm the failure. The finalizer is NOT bypassed; the caller
    # (build_pipeline / resume) decides whether to publish the partial build.
    integration_result: dict | None = None
    # success accounts for stages already DONE before this invocation (the
    # idempotent-resume path: a re-entered build that's fully done in a prior
    # run reports success=True with stage_results=[...]). Compute total-done
    # from the persisted stage_status map (the single source of truth), and
    # require every declared stage to be DONE.
    all_status = stage_status.read_stage_status(workspace_id).get("stages") or {}
    total_done = sum(1 for s in stages
                      if (all_status.get(s.get("stage_id",""), {}) or {}).get("status")
                      == StageStatus.DONE.value)
    success = (stages_failed == 0 and total_done == len(stages))
    if success and stages:
        # If integration was already DONE on a prior invocation, reuse it
        # (idempotent resume — don't re-run integration that already passed).
        integ_status = (all_status.get(INTEGRATION_STAGE_ID, {}) or {}).get("status")
        if integ_status == StageStatus.DONE.value:
            integration_result = {
                "passed": True, "attempts": 0,
                "test_results": stage_status.read_stage_test_results(
                    workspace_id, INTEGRATION_STAGE_ID) or {},
                "reason": "integration_already_done",
            }
        else:
            integration_result = _run_integration_validation(
                workspace_id, working_dir, agent_fn=agent_fn, test_fn=test_fn,
                validate_fn=integration_validate_fn,
                max_iterations=max_integration_iterations,
            )
            if not integration_result.get("passed"):
                success = False
                blocked = True
                # Treat integration failure as a stage-level FAILED record for
                # stage_status.json + repair_history.json consistency.
                stage_status.write_stage_status(
                    workspace_id, INTEGRATION_STAGE_ID, StageStatus.FAILED,
                    attempts=integration_result.get("attempts", 1),
                    completed_at=_now_iso(),
                )
                _record_repair(workspace_id, INTEGRATION_STAGE_ID, 1,
                               reason=integration_result.get("reason", ""),
                               strategy="integration_validation",
                               prompt_excerpt="(integration stage)",
                               test_results=integration_result.get("test_results", {}))
            else:
                stage_status.write_stage_status(
                    workspace_id, INTEGRATION_STAGE_ID, StageStatus.DONE,
                    completed_at=_now_iso(),
                )

    return {
        "workspace_id": workspace_id,
        "stages_total": len(stages),
        # ``stages_done`` reflects the persisted stage_status.json (the single
        # source of truth) so a re-entered fully-completed build still reports
        # ``stages_done == len(stages)`` even when ``stage_results == []`` —
        # this is the idempotent-resume contract.
        "stages_done": total_done,
        "stages_failed": stages_failed,
        "stages_run_this_invocation": len(stage_results),
        "stage_results": stage_results,
        "integration": integration_result,
        "success": success,
        "blocked": blocked,
    }


def _run_integration_validation(
    workspace_id: str,
    working_dir: str,
    *,
    agent_fn: Callable[..., Any] | None = None,
    test_fn: Callable[..., dict] | None = None,
    validate_fn: Callable[..., dict] | None = None,
    max_iterations: int = MAX_INTEGRATION_ITERATIONS,
) -> dict:
    """One final validation pass over the integrated ``src/`` + ``tests/``.
    Runs tests + (when the ``validation_engine`` is importable and opted-in)
    the deeper BuildValidator pipeline. Always reads the existing
    ``implementation_executor._run_tests`` for the pass/fail contract.

    The integration stage is bounded by ``MAX_INTEGRATION_ITERATIONS`` outer
    attempts — on each failure, the staged executor asks the agent to fix the
    integration (imports / wiring) with a directive listing every produced
    artifact; retest. Exceeding the budget escalates to HUMAN_REQUIRED via
    ``_emit_stage_human_required``."""
    from implementation_executor import execute_implementation, _run_tests, MAX_REPAIR_ATTEMPTS

    plan = stage_status.read_build_plan(workspace_id)
    stages = plan.get("stages") or []
    produced_files: list[str] = []
    for s in stages:
        # Collect every file the staged executor persisted in the stage's
        # checkpoint (the artifacts delta). One integration spec lists all of
        # them + a directive to fix integration-only issues, not to rewrite.
        cp = stage_status.read_stage_checkpoint(workspace_id, s.get("stage_id", ""))
        for a in (cp.get("artifacts") or []):
            p = a.get("path")
            if p:
                produced_files.append(p)

    stage_status.write_stage_status(workspace_id, INTEGRATION_STAGE_ID,
                                     StageStatus.VALIDATING,
                                     attempts=0)
    runner = test_fn or _run_tests
    test_results = runner(working_dir)
    if _tests_passed(test_results):
        stage_status.write_stage_test_results(workspace_id, INTEGRATION_STAGE_ID,
                                                test_results)
        return {"passed": True, "attempts": 0, "test_results": test_results,
                "files_inspected": len(produced_files)}

    # Integration test failure: bounded repair loop. Optional deeper
    # validation via the injected validate_fn (the test seam) OR by the
    # validation_engine when available.
    attempts = 0
    last_reason = "integration tests failing"
    while attempts < max_iterations:
        attempts += 1
        file_block = "\n".join(f"- `{f}`" for f in produced_files) or "- (none)"
        plan_text = (
            f"# INTEGRATION REPAIR\n\n"
            f"The fully-staged build's tests are failing. The following files "
            f"were produced across stages and must form a working integrated "
            f"system. Fix the failing tests WITHOUT rewriting working code: "
            f"resolve imports, wiring, and cross-module contracts only.\n\n"
            f"## Files produced\n{file_block}\n\n"
            f"## Failing tests\n{(test_results.get('output') or '')[-2000:]}\n"
        )
        stage_status.write_stage_status(workspace_id, INTEGRATION_STAGE_ID,
                                         StageStatus.REPAIRING,
                                         attempts_increment=1)
        exec_result = execute_implementation(
            plan=plan_text,
            workspace_id=workspace_id,
            working_dir=working_dir,
            use_claude_code=True,
            agent_fn=agent_fn,
            test_fn=test_fn,
            max_repairs=MAX_REPAIR_ATTEMPTS,
        )
        test_results = exec_result.get("test_results", {}) or {}
        _persist_test_results(workspace_id, INTEGRATION_STAGE_ID, test_results)
        if exec_result.get("success") and _tests_passed(test_results):
            return {"passed": True, "attempts": attempts,
                    "test_results": test_results,
                    "files_inspected": len(produced_files)}
        last_reason = exec_result.get("error") or "integration tests still failing"
        _record_repair(workspace_id, INTEGRATION_STAGE_ID, attempts,
                       reason=last_reason, strategy="integration_repair",
                       prompt_excerpt=plan_text, test_results=test_results)

    # Optional deeper validation in addition to the test-contract failure.
    deep: dict = {}
    if validate_fn is not None:
        try:
            deep = validate_fn(workspace_id, working_dir) or {}
        except Exception as e:
            deep = {"passed": False, "error": str(e)}
    _emit_stage_human_required(
        workspace_id, INTEGRATION_STAGE_ID,
        (test_results.get("output") or "")[-500:],
        attempts,
    )
    return {"passed": False, "attempts": attempts, "test_results": test_results,
            "reason": last_reason, "deep_validation": deep,
            "files_inspected": len(produced_files)}


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # ponytail self-check: a staged workspace with an injected agent + tests
    # seam runs to completion fully offline. Two stages (component + tests);
    # the agent seam writes a file per stage; the test seam reports passing.
    import json
    import tempfile
    from pathlib import Path

    import build_planner
    import build_workspace as _bw
    from implementation_executor import _tests_passed as _tp_check

    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)

        # Two components — fake the decisions so the planner emits a 7-stage
        # plan (the smoke-check then exercises stage-by-stage execution).
        wid = _bw.create_workspace(source_question="Build X")
        ws_dir = _bw._workspace_dir(wid)
        (ws_dir / "decisions.json").write_text(json.dumps({
            "verification_result": {
                "resolved_claims": [
                    {"normalized_key": "orchestration",
                     "dispute": "Airflow vs Temporal",
                     "verification": {"status": "RESOLVED", "confidence": 95,
                                      "finding": "Verification concludes Temporal as the orchestration engine; Airflow rejected."}},
                    {"normalized_key": "persistence",
                     "dispute": "MySQL vs PostgreSQL",
                     "verification": {"status": "RESOLVED", "confidence": 92,
                                      "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
                ],
                "equivalent_claims": [], "unresolved_claims": [],
                "verification_score": 100, "truncated_count": 0,
            },
            "verified_decisions": [
                {"outcome_type": "VERIFIED_DECISION",
                 "dispute_key": "orchestration",
                 "decision": "Temporal orchestration", "reasoning": ""},
                {"outcome_type": "VERIFIED_DECISION",
                 "dispute_key": "persistence",
                 "decision": "PostgreSQL primary datastore", "reasoning": ""},
            ],
            "resolved_decisions": [],
        }, indent=2), encoding="utf-8")
        (ws_dir / "answer.json").write_text(
            json.dumps({"synthesized_answer": "Temporal+PostgreSQL"}),
            encoding="utf-8")
        (ws_dir / "implementation_plan.md").write_text(
            "# Implementation Objective\nBuild the system\n\n"
            "# Files to Create\n- `src/orchestration/main.py`\n"
            "- `src/persistence/store.py`\n- `tests/test_app.py`\n\n"
            "# Files to Modify\n(none)\n\n"
            "# Implementation Steps\n1. orchestration\n2. persistence\n\n"
            "# Acceptance Criteria\n- pytest passes\n\n"
            "# Output Contract\nFiles under src/.\n",
            encoding="utf-8")

        # Plan the staged build.
        manifest = build_planner.plan_staged_build(wid)
        impl_stages = [s for s in manifest["stages"] if s["kind"] == "component"]
        assert len(impl_stages) == 2, manifest

        # Inject agent + test seams:
        # agent_fn writes a stage-specific file (so the artefact-presence gate
        # sees a NEW file each stage, not just the first), AND a sentinel test
        # file so _tests_passed reports a real pass.
        def agent_seam(plan, working_dir=None, timeout=None):
            # Pick a stage-specific filename derived from the plan's directive
            # heading so each stage writes a different file.
            stage_token = "stage"
            for line in plan.splitlines():
                if line.startswith("# STAGED REPAIR — Stage"):
                    # "Stage stage_01_xxx (title)" — pull the id token.
                    parts = line.split()
                    if len(parts) >= 5:
                        stage_token = parts[4].split("_", 1)[-1]
                    break
            else:
                # First-attempt path: derive from the directive's stage id.
                for line in plan.splitlines():
                    if line.startswith("# STAGED IMPLEMENTATION DIRECTIVE"):
                        # the directive doesn't carry the stage id; use the
                        # first declared file's slug instead.
                        for ln in plan.splitlines():
                            if ln.strip().startswith("- `") and not ln.strip().endswith("/`"):
                                stage_token = ln.strip()[3:-1].split("/")[-1].rsplit(".", 1)[0]
                                break
                        break
            rel = f"src/staged_exec_{stage_token}.py"
            p = Path(working_dir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"# stub module from staged executor (plan={hash(plan) & 0xffff:x}, "
                f"stage={stage_token})\nVALUE = 42\n",
                encoding="utf-8")
            # Sentinel test file so _tests_passed reports pass.
            test_p = Path(working_dir) / "tests" / "test_app.py"
            test_p.parent.mkdir(parents=True, exist_ok=True)
            test_p.write_text("def test_app():\n    assert True\n",
                              encoding="utf-8")
            from coding_agents.base_agent import AgentResult
            return AgentResult(
                success=True,
                output=f"[STAGED TEST SEAM] wrote {rel}",
                error="",
            )

        test_calls = {"n": 0}

        def test_seam(working_dir=None):
            test_calls["n"] += 1
            return {"runner": "pytest", "passed": 1, "failed": 0, "exit_code": 0,
                    "output": "1 passed"}

        result = execute_stages(wid,
                                agent_fn=agent_seam, test_fn=test_seam,
                                integration_validate_fn=None)
        assert result["success"] is True, result
        assert result["stages_failed"] == 0, result
        # All stages + integration should be DONE.
        assert stage_status.all_stages_done(wid), stage_status.read_stage_status(wid)
        # Integration stage is also recorded DONE.
        integ = stage_status.read_stage_status(wid, INTEGRATION_STAGE_ID)
        assert integ.get("status") == StageStatus.DONE.value, integ
        # Each implementation stage wrote a checkpoint with artifacts.
        for stage in manifest["stages"]:
            cp = stage_status.read_stage_checkpoint(wid, stage["stage_id"])
            assert cp.get("stage_id") == stage["stage_id"], cp

        # Resume test: re-entering execute_stages on an already-DONE build
        # returns success with zero new work (idempotent).
        result2 = execute_stages(wid, agent_fn=agent_seam, test_fn=test_seam)
        assert result2["success"] is True, result2
        assert result2["stages_done"] == len(manifest["stages"]), result2
        # No new stage_results — all were already DONE.
        assert len(result2["stage_results"]) == 0, result2

        # Stage-failure test: simulate a stage that can't pass tests.
        wid2 = _bw.create_workspace(source_question="Build Y")
        ws2 = _bw._workspace_dir(wid2)
        (ws2 / "decisions.json").write_text(json.dumps({
            "verification_result": {
                "resolved_claims": [
                    {"normalized_key": "persistence",
                     "dispute": "MySQL vs PostgreSQL",
                     "verification": {"status": "RESOLVED", "confidence": 92,
                                      "finding": "Verification concludes PostgreSQL as primary datastore; MySQL rejected."}},
                ],
                "equivalent_claims": [], "unresolved_claims": [],
                "verification_score": 100, "truncated_count": 0,
            },
            "verified_decisions": [{
                "outcome_type": "VERIFIED_DECISION", "dispute_key": "persistence",
                "decision": "PostgreSQL primary datastore", "reasoning": ""}],
            "resolved_decisions": [],
        }, indent=2), encoding="utf-8")
        (ws2 / "answer.json").write_text(
            json.dumps({"synthesized_answer": "PostgreSQL"}), encoding="utf-8")
        (ws2 / "implementation_plan.md").write_text(
            "# Implementation Objective\nBuild X\n\n"
            "# Files to Create\n- `src/persistence/store.py`\n\n"
            "# Implementation Steps\n1. build\n\n"
            "# Acceptance Criteria\n- pytest passes\n\n"
            "# Output Contract\nFiles under src/.\n",
            encoding="utf-8")
        m2 = build_planner.plan_staged_build(wid2)

        def failing_agent(plan, working_dir=None, timeout=None):
            # Write a file so artefact-presence passes; tests will fail.
            p = Path(working_dir) / "src" / "persistence" / "store.py"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("syntax error here", encoding="utf-8")
            from coding_agents.base_agent import AgentResult
            return AgentResult(success=True, output="wrote a broken file", error="")

        def failing_tests(working_dir=None):
            return {"runner": "pytest", "passed": 0, "failed": 2, "exit_code": 1,
                    "output": "FAILED test_load\nFAILED test_save"}

        # Bound the outer loop to 0 so the smoke test is fast (the inner
        # executor still runs its own 3-attempt loop, which exercises the
        # HUMAN_REQUIRED escalation path on a guaranteed failure).
        r = execute_stages(wid2, agent_fn=failing_agent, test_fn=failing_tests,
                           max_stage_repair_iterations=0)
        assert r["success"] is False, r
        assert r["blocked"] is True, r
        # First stage is FAILED; human_required was emitted.
        first_stage_result = r["stage_results"][0]
        assert first_stage_result["status"] == StageStatus.FAILED.value
        assert first_stage_result["human_required"] is True
        # The human_decision_manager has a pending decision for this ws.
        from human_decision_manager import load_pending
        pending = load_pending(wid2)
        assert pending, "expected a HUMAN_REQUIRED decision to be pending"
        assert any(first_stage_result["stage_id"] in (d.get("stage_id","") or d.get("dispute_key",""))
                    for d in pending), pending

        # A legacy (no staged plan) workspace returns the safety-net result.
        wid3 = _bw.create_workspace(source_question="legacy")
        r3 = execute_stages(wid3)
        assert r3["skipped"] is True and r3["reason"] == "no_staged_layout", r3

    print("staged_executor smoke test PASSED")
