"""
Project Sol0mon — Pipeline Scheduler (Stage 2 of every workflow).

Reads the planner's execution graph, resolves work-package readiness based on
dependency status, and dispatches READY packages through the existing
pipeline (one invocation of ``pipeline_runner.execute`` per package).

Work-package lifecycle (the spec's exact names):

    PENDING   — deps not all COMPLETED yet
    READY     — deps all COMPLETED; eligible to run
    RUNNING   — currently executing
    COMPLETED — succeeded
    FAILED    — raised an exception
    BLOCKED   — a dependency FAILED or BLOCKED (cannot run)

Only READY packages may execute. Independent READY packages are
parallelizable: ``max_workers`` controls the dispatch pool. The default
``max_workers=1`` preserves the prior sequential behaviour (the existing
debate pipeline is itself a long-running, side-effect-heavy subsystem with
browser CDP / Cerebras sessions; sequential dispatch matches its contract).
Callers that inject a stateless ``pipeline_fn`` (e.g. tests) can set
``max_workers>1`` to run independent packages concurrently.

Debate separation (knowledge-driven dispatch):

    research   → has no prior input
    architecture → consumes verified research (NOT the raw prompt)
    implementation → consumes architecture + verified research; OPTIONAL
    validation / documentation → consume implementation outputs

The scheduler builds each package's prompt from:
    (a) the package's parent-objective + type + description + deliverable, plus
    (b) the merged prior-stage knowledge for the package's type, when any.

This makes "architecture consumes verified research rather than the original
user prompt whenever research exists" hold automatically — without
re-implementing the architecture pipeline. The existing
``pipeline_runner.execute`` runs the same debate engine; only its prompt is
enriched with prior-stage evidence.

Implementation optionality: an ``implementation`` package whose
verified-decisions record no open implementation choices is skipped (its
status becomes BLOCKED with reason ``no_open_implementation_decisions``).
The caller can disable this guard via ``require_implementation=True``.

Pipeline position:

    Objective → Planner → Scheduler → Existing Pipeline
"""

from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from project_planner import topological_order, validate_execution_graph


# ---------------------------------------------------------------------------
# Status names (the spec's exact constants)
# ---------------------------------------------------------------------------

PENDING = "PENDING"
READY = "READY"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

ALL_STATUSES = (PENDING, READY, RUNNING, COMPLETED, FAILED, BLOCKED)

# Statuses that count as "unfinished" for downstream readiness.
_UNFINISHED = (PENDING, READY, RUNNING)
# Statuses that block dependents.
_BLOCKING = (FAILED, BLOCKED)


# ---------------------------------------------------------------------------
# Prompt construction (debate separation)
# ---------------------------------------------------------------------------

def scoped_prompt(
    package: dict,
    objective: str,
    *,
    prior_knowledge: str = "",
) -> str:
    """Render a work package as a prompt for the existing debate pipeline.

    Existing ``execute()`` expects a single 'question / objective' string.
    We embed:
      - the parent objective (so packages stay coherent)
      - the package's own fields (title/type/description/deliverable)
      - prior-stage knowledge merged from ``workspace/research/`` and
        ``workspace/verified/`` when the package type CONSUMES a prior stage
        (the debate-separation rule). Empty prior_knowledge -> no injection,
        so a fresh workspace with no research still works (back-compat).
    """
    ptype = package.get("type", "")
    parts = [
        f"Parent objective: {objective}",
        f"Work package: {package.get('title', package.get('id', ''))}",
        f"Type: {ptype}",
        f"Description: {package.get('description', '')}",
        f"Expected deliverable: {package.get('deliverable', '')}",
    ]
    if prior_knowledge and prior_knowledge.strip():
        parts.append("")
        parts.append("Verified prior-stage knowledge (consume this; do not "
                    "re-debate what is already settled):")
        parts.append(prior_knowledge.strip())
    parts.append("")
    parts.append("Execute this work package toward the parent objective and "
                 "produce the expected deliverable.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Graph validation (defense-in-depth — the planner already validated)
# ---------------------------------------------------------------------------

def schedule(graph: dict) -> list[str]:
    """Return package ids in topological (dependency-first) order. Raises
    ``ValueError`` if the graph is structurally invalid."""
    issues = validate_execution_graph(graph)
    if issues:
        raise ValueError("scheduler: invalid execution graph: " + "; ".join(issues[:3]))
    return topological_order(graph)


# ---------------------------------------------------------------------------
# Readiness / status engine
# ---------------------------------------------------------------------------

def _initial_status(graph: dict) -> dict[str, str]:
    """Mark every package PENDING initially. Zero-dep packages are
    immediately READY."""
    status: dict[str, str] = {}
    for pkg in (graph or {}).get("work_packages", []):
        if isinstance(pkg, dict) and isinstance(pkg.get("id"), str):
            pid = pkg["id"]
            deps = pkg.get("dependencies") or []
            status[pid] = READY if not deps else PENDING
    return status


def _refresh_readiness(
    status: dict[str, str],
    by_id: dict[str, dict],
) -> None:
    """Promote PENDING packages whose deps are all COMPLETED to READY.
    Demote PENDING packages with any FAILED/BLOCKED dep to BLOCKED. Mutates
    ``status`` in place."""
    for pid, st in list(status.items()):
        if st != PENDING:
            continue
        deps = (by_id.get(pid) or {}).get("dependencies") or []
        if any(status.get(d) in _BLOCKING for d in deps):
            status[pid] = BLOCKED
            continue
        if all(status.get(d) == COMPLETED for d in deps):
            status[pid] = READY


def ready_packages(status: dict[str, str]) -> list[str]:
    """Return ids of packages currently READY (eligible to execute).
    Pure; deterministic order by insertion."""
    return [pid for pid, st in status.items() if st == READY]


# ---------------------------------------------------------------------------
# Implementation optionality (debate-separation guard)
# ---------------------------------------------------------------------------

def _open_implementation_decisions(
    workspace_id: str, package_id: str,
) -> bool:
    """True iff there are verified decisions recorded for ``package_id``
    that still name a concrete implementation choice (deploy/build/implement/
    create). When False, the implementation package is skipped — the
    architecture debate already settled everything, so a fresh
    implementation debate would re-litigate settled ground."""
    if not workspace_id:
        return True
    try:
        from workspace_knowledge import load_artifact
        text = load_artifact(workspace_id, "verified", package_id) or ""
    except Exception:
        return True
    if not text.strip():
        return True
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return True
    decisions = (data or {}).get("verified_decisions") or []
    if not decisions:
        return True
    # Import the deterministic implementation-signal matcher.
    from execution_decision import IMPLEMENTATION_SIGNALS, _count_word_hits
    for d in decisions:
        blob = " ".join(str(v) for v in (d.values() if isinstance(d, dict) else []))
        if _count_word_hits(blob.lower(), IMPLEMENTATION_SIGNALS) > 0:
            return True
    return False


# ---------------------------------------------------------------------------
# Default pipeline seam (production)
# ---------------------------------------------------------------------------

def _default_pipeline_fn(prompt: str) -> dict:
    """Lazy import so the scheduler module stays import-free of
    pipeline_runner::debate_engine at module load (the rest of the codebase
    lazy-imports the debate engine for the same reason — browser /
    Cerebras side effects)."""
    from pipeline_runner import execute
    return execute(prompt)


# ---------------------------------------------------------------------------
# Knowledge-loading seam (debate separation)
# ---------------------------------------------------------------------------

def _default_knowledge_fn(workspace_id: str, package_type: str) -> str:
    """Default knowledge seam: load the prior-stage merged knowledge for a
    package type. Returns "" when nothing has been persisted (back-compat)."""
    if not workspace_id:
        return ""
    try:
        from workspace_knowledge import load_stage_input
        return load_stage_input(workspace_id, package_type)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Main dispatch loop
# ---------------------------------------------------------------------------

def run_scheduled_pipeline(
    graph: dict,
    *,
    pipeline_fn: Callable[[str], dict] | None = None,
    on_package_start: Callable[[str], None] | None = None,
    on_package_complete: Callable[[str, dict], None] | None = None,
    workspace_id: str = "",
    max_workers: int = 1,
    require_implementation: bool = False,
    knowledge_fn: Callable[[str, str], str] | None = None,
) -> dict:
    """Dispatch the execution graph through the existing pipeline.

    Args:
        graph:        the planner's execution graph.
        pipeline_fn:   injectable seam (prompt -> result_dict). Production uses
                      ``pipeline_runner.execute``; tests inject a fake.
        on_package_start / on_package_complete: callbacks for log/telemetry;
                      both receive the package id (complete also receives the
                      result dict).
        workspace_id: needed for knowledge persistence + the implementation-
                      optionality guard.
        max_workers:  dispatch parallelism. 1 = sequential (default; preserves
                      the existing pipeline's side-effect contract). >1 is
                      safe only when ``pipeline_fn`` is stateless (tests).
        require_implementation: when True, the implementation-optionality
                      guard is bypassed (every implementation package runs).
        knowledge_fn: injectable seam for loading prior-stage knowledge.
                      ``knowledge_fn(workspace_id, package_type) -> str``;
                      defaults to ``workspace_knowledge.load_stage_input``.

    Returns:
        {
          "objective":   str,
          "packages":     {id: {status, ...}},
          "succeeded":    [ids...],
          "failed":       [ids...],
          "skipped":      [ids...],
          "started_at":   iso,
          "completed_at": iso,
          "max_workers":  int,
        }
    """
    fn = pipeline_fn or _default_pipeline_fn
    kfn = knowledge_fn or _default_knowledge_fn
    started_at = datetime.now(timezone.utc).isoformat()
    objective = (graph or {}).get("objective", "")
    by_id = {
        pkg["id"]: pkg
        for pkg in (graph or {}).get("work_packages", [])
        if isinstance(pkg, dict) and isinstance(pkg.get("id"), str)
    }
    order = schedule(graph)

    status = _initial_status(graph)
    packages: dict[str, dict] = {}

    def _dispatch(pid: str) -> dict:
        """Build the prompt for a package, call pipeline_fn, return result."""
        pkg = by_id.get(pid, {})
        ptype = pkg.get("type", "")
        prior = ""
        try:
            prior = kfn(workspace_id, ptype)
        except Exception:
            prior = ""
        prompt = scoped_prompt(pkg, objective, prior_knowledge=prior)
        return fn(prompt)

    def _handle_complete(pid: str, result: dict) -> None:
        packages[pid] = {"id": pid, "status": COMPLETED, "result": result}
        status[pid] = COMPLETED
        # Persist verified outcomes from the package's result so the NEXT
        # stage's knowledge seam can load them. Failure here is observational
        # — never blocks the pipeline.
        if workspace_id and isinstance(result, dict):
            try:
                from workspace_knowledge import save_debate_result
                save_debate_result(
                    workspace_id, pid, by_id.get(pid, {}).get("type", ""),
                    result,
                )
            except Exception:
                pass
        if on_package_complete:
            on_package_complete(pid, packages[pid])

    def _handle_failed(pid: str, err: Exception) -> None:
        packages[pid] = {"id": pid, "status": FAILED, "error": str(err)}
        status[pid] = FAILED
        if on_package_complete:
            on_package_complete(pid, packages[pid])

    def _handle_blocked(pid: str, reason: str, blocked_by: str = "") -> None:
        packages[pid] = {
            "id": pid, "status": BLOCKED,
            "skipped_reason": reason, "blocked_by": blocked_by,
        }
        status[pid] = BLOCKED
        if on_package_complete:
            on_package_complete(pid, packages[pid])

    # Sequential dispatch (default): preserves the existing pipeline's
    # side-effect contract (browser sessions, Cerebras rate limits, etc).
    # The structure is parallel-ready: the same _dispatch / _handle_*
    # closures are used by the parallel path below.
    if max_workers <= 1:
        while True:
            _refresh_readiness(status, by_id)
            # Record BLOCKED packages (deps failed) that were just demoted from
            # PENDING, so the report carries every package's terminal state.
            for pid, st in list(status.items()):
                if st == BLOCKED and pid not in packages:
                    _handle_blocked(
                        pid, "dependency_failed",
                        blocked_by=next(
                            (d for d in (by_id.get(pid, {}).get("dependencies") or [])
                             if status.get(d) in _BLOCKING), ""),
                    )
            ready = ready_packages(status)
            if not ready:
                break
            # Dispatch one at a time, in topological order, so the readiness
            # refresh after each completion releases the next dep batch.
            ready_set = set(ready)
            pid = next(p for p in order if p in ready_set)
            # Implementation optionality guard (debate separation):
            # skip implementation packages whose verified decisions carry
            # no open implementation choice (the architecture already settled
            # everything). The package ends up BLOCKED (its dependents may
            # still run if they don't actually need its output).
            if (by_id.get(pid, {}).get("type") == "implementation"
                    and not require_implementation
                    and not _open_implementation_decisions(workspace_id, pid)):
                _handle_blocked(
                    pid, "no_open_implementation_decisions",
                )
                continue
            status[pid] = RUNNING
            if on_package_start:
                on_package_start(pid)
            try:
                result = _dispatch(pid)
                _handle_complete(pid, result)
            except Exception as e:
                _handle_failed(pid, e)
    else:
        # Parallel dispatch: independent READY packages run concurrently.
        # The number of READY packages can be smaller than max_workers when
        # the graph is essentially linear (only one package is READY at a
        # time); in that case the ThreadPoolExecutor just doesn't fill its
        # pool. This is the cheapest correct parallel path: stdlib
        # concurrent.futures, no job queue, no back-pressure.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures: dict = {}
            while True:
                _refresh_readiness(status, by_id)
                # Record BLOCKED demotions (same reason as the sequential path).
                for pid, st in list(status.items()):
                    if st == BLOCKED and pid not in packages:
                        _handle_blocked(
                            pid, "dependency_failed",
                            blocked_by=next(
                                (d for d in (by_id.get(pid, {}).get("dependencies") or [])
                                 if status.get(d) in _BLOCKING), ""),
                        )
                # Submit every currently-READY package the pool has capacity
                # for. (We cap by max_workers since that's the pool size.)
                for pid in ready_packages(status):
                    if pid in futures or status[pid] != READY:
                        continue
                    if (by_id.get(pid, {}).get("type") == "implementation"
                            and not require_implementation
                            and not _open_implementation_decisions(workspace_id, pid)):
                        _handle_blocked(pid, "no_open_implementation_decisions")
                        continue
                    status[pid] = RUNNING
                    if on_package_start:
                        on_package_start(pid)
                    futures[pool.submit(_dispatch, pid)] = pid
                if not futures:
                    # No RUNNING packages; if any PENDING remained they were
                    # just demoted to BLOCKED above. Terminate the loop.
                    break
                # Wait for at least one to finish, then handle it and loop.
                done_iter = as_completed(futures)
                for fut in done_iter:
                    pid = futures.pop(fut)
                    try:
                        _handle_complete(pid, fut.result())
                    except Exception as e:
                        _handle_failed(pid, e)
                    break  # re-loop to refresh readiness with the new state
    succeeded = [pid for pid, st in status.items() if st == COMPLETED]
    failed = [pid for pid, st in status.items() if st == FAILED]
    skipped = [pid for pid, st in status.items() if st == BLOCKED]

    return {
        "objective": objective,
        "packages": packages,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "max_workers": max_workers,
    }


if __name__ == "__main__":
    # ponytail self-check: status transitions, failure propagation,
    # implementation optionality, parallel dispatch, knowledge injection.
    graph = {
        "objective": "ship X",
        "work_packages": [
            {"id": "r", "title": "Research", "type": "research",
             "description": "survey", "deliverable": "research.md",
             "dependencies": []},
            {"id": "a", "title": "Architecture", "type": "architecture",
             "description": "design", "deliverable": "architecture.md",
             "dependencies": ["r"]},
            {"id": "i", "title": "Implement", "type": "implementation",
             "description": "build", "deliverable": "src/",
             "dependencies": ["a"]},
            {"id": "v", "title": "Validate", "type": "validation",
             "description": "test", "deliverable": "tests/",
             "dependencies": ["i"]},
            {"id": "d", "title": "Docs", "type": "documentation",
             "description": "document", "deliverable": "README.md",
             "dependencies": ["i"]},
        ],
    }

    # Status initial: zero-dep packages READY, others PENDING.
    st = _initial_status(graph)
    assert st["r"] == READY
    assert st["a"] == PENDING and st["i"] == PENDING

    # Happy path sequential: every package COMPLETED in topological order.
    calls = []
    def fn_ok(prompt):
        calls.append(prompt)
        return {"status": "completed", "synthesized_answer": "ok"}
    res = run_scheduled_pipeline(graph, pipeline_fn=fn_ok, workspace_id="")
    assert res["succeeded"] == ["r", "a", "i", "v", "d"]
    assert res["failed"] == [] and res["skipped"] == []
    assert all(res["packages"][pid]["status"] == COMPLETED
               for pid in res["succeeded"])
    # Each package's prompt embeds the parent objective + its type field.
    assert "Parent objective: ship X" in calls[0]
    assert "Type: research" in calls[0]
    assert "Type: architecture" in calls[1]

    # Failure propagation: r fails → a, i, v, d BLOCKED.
    def fn_fail_research(prompt):
        if "Type: research" in prompt:
            raise RuntimeError("research blew up")
        return {"status": "completed"}
    res2 = run_scheduled_pipeline(graph, pipeline_fn=fn_fail_research)
    assert res2["failed"] == ["r"]
    assert set(res2["skipped"]) == {"a", "i", "v", "d"}
    assert all(res2["packages"][pid]["status"] == BLOCKED
               for pid in res2["skipped"])

    # Mid-pipeline failure: i fails → v, d BLOCKED; r, a COMPLETED.
    def fn_fail_impl(prompt):
        if "Type: implementation" in prompt:
            raise RuntimeError("impl blew up")
        return {"status": "completed"}
    res3 = run_scheduled_pipeline(graph, pipeline_fn=fn_fail_impl)
    assert res3["succeeded"] == ["r", "a"]
    assert res3["failed"] == ["i"]
    assert set(res3["skipped"]) == {"v", "d"}

    # Parallel dispatch (max_workers=2): same happy-path result.
    calls.clear()
    res_par = run_scheduled_pipeline(graph, pipeline_fn=fn_ok, max_workers=2)
    assert res_par["succeeded"] == ["r", "a", "i", "v", "d"]
    assert res_par["max_workers"] == 2

    # Knowledge injection: an architecture prompt receives research/verified
    # text when knowledge_fn returns non-empty.
    def kfn_research(ws, ptype):
        if ptype == "architecture":
            return "## [research] wp_r\n\nUse Redis."
        return ""
    calls.clear()
    run_scheduled_pipeline(graph, pipeline_fn=fn_ok, knowledge_fn=kfn_research)
    assert any("Verified prior-stage knowledge" in c and "Use Redis" in c
               for c in calls), calls

    # Implementation optionality: when verified decisions carry no
    # implementation signal, the implementation package is BLOCKED with
    # reason "no_open_implementation_decisions".
    import tempfile
    import build_workspace as bw
    from pathlib import Path
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="sol0mon_ps_"))
        from build_workspace import create_workspace
        from workspace_knowledge import save_artifact
        wid = create_workspace(source_question="ship X")
        # Architecture package 'a' completes + emits a verified record with
        # NO implementation signal.
        save_artifact(wid, "verified", "i",
                      json.dumps({"verified_decisions": [
                          {"decision": "use REST over gRPC"}]}))
        def fn_with_i(prompt):
            # The implementation package should NOT reach the pipeline.
            if "Type: implementation" in prompt:
                raise AssertionError("impl should have been skipped")
            return {"status": "completed"}
        res_opt = run_scheduled_pipeline(
            graph, pipeline_fn=fn_with_i, workspace_id=wid,
        )
        assert "i" in res_opt["skipped"], res_opt
        assert res_opt["packages"]["i"]["skipped_reason"] == \
            "no_open_implementation_decisions"
        # require_implementation=True overrides the guard.
        res_req = run_scheduled_pipeline(
            graph, pipeline_fn=lambda p: {"status": "completed"},
            workspace_id=wid, require_implementation=True,
        )
        assert "i" in res_req["succeeded"], res_req
        bw.WORKSPACE_ROOT = saved_root
    finally:
        bw.WORKSPACE_ROOT = saved_root

    # Invalid graph is rejected pre-dispatch.
    try:
        schedule({"work_packages": []})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    print("pipeline_scheduler self-check PASSED")
