#!/usr/bin/env python3
"""
sol0mon_cli.py — Phase D1 production entrypoint for Project Sol0mon.

Three commands:

    sol0mon debate "<prompt>"
        Run the full debate pipeline (pipeline_runner.execute) on the prompt
        and print synthesized_answer + consensus_score. Offline-safe: the
        debate_fn seam is injectable; production uses the real engine.

    sol0mon build answer <id>
        Resurrect a prior answer by ID, generate a build spec, and execute it
        through coding_agent_router -> OpenCodeAgent into the build workspace.
        Equivalent to build_pipeline.run_build_pipeline(f"build answer <id>",
        use_claude_code=True).

    sol0mon resume [workspace_id]
        Resume a workspace from its current engineering lifecycle stage.
        With no workspace_id: list the five most recent workspaces
        (topic / lifecycle stage / status / last updated / workspace id)
        and resume the one selected by project number. With an explicit
        workspace_id: resume immediately (advanced). ``--follow-up`` runs
        a follow-up debate on a Research or Completed stage workspace.

Audit D1: wires existing modules together — no new features, no LangGraph,
no API clients, no external research. Calls build_pipeline.run_build_pipeline
and pipeline_runner.execute, both already integration-tested.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Make the project root importable when invoked as `python3 sol0mon_cli.py`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _cmd_debate(args: argparse.Namespace) -> int:
    from pipeline_runner import execute
    # Execution Mode Gate V1 — let the user choose the execution depth BEFORE
    # the debate begins (Phase 3). The recommendation is computed from the
    # prompt via the deterministic execution_decision engine (unchanged); the
    # user may accept it (Enter) or pick a deeper / shallower mode. The debate
    # then runs. RESEARCH_ONLY / RESEARCH_ARCHITECTURE stop at synthesis; a
    # FULL_BUILD selection chains into the build pipeline on the just-saved
    # answer (resurrect-by-id), enforcing the user's choice regardless of the
    # post-debate auto classification (the Debate #145 fix).
    from execution_mode_gate import (
        recommend_mode_from_prompt, prompt_execution_mode, fixed_selection_fn,
        FULL_BUILD as _MODE_FULL,
    )
    recommended_mode = recommend_mode_from_prompt(args.prompt)
    selected_mode, override_used = prompt_execution_mode(recommended_mode)
    result = execute(args.prompt)
    # Phase 5 — Debate-Governed Execution Decisions: surface the decision
    # the debate emitted. `pipeline_runner.execute` returns the
    # DebateEngine result dict, which carries execution_decision /
    # execution_reason / execution_confidence on every completed debate.
    exec_dec = result.get("execution_decision")
    exec_reason = result.get("execution_reason", "")
    exec_source = result.get("execution_decision_source", "")
    if exec_dec:
        print("Execution Decision:")
        print(exec_dec)
        if exec_source:
            print(f"Source: {exec_source}")
        if exec_reason:
            print(f"Reason: {exec_reason}")
        print()
    print(f"Execution Mode: recommended={recommended_mode} selected={selected_mode} "
          f"override_used={override_used}")
    print()
    # Phase 4/5 — FULL_BUILD chains into the build pipeline on the just-saved
    # answer, carrying the pre-debate selection verbatim (no re-prompt). This
    # honors the user's pre-debate choice even when the post-debate auto
    # classification disagreed with it (Debate #145). Other modes stop here at
    # synthesis, exactly as before.
    if selected_mode == _MODE_FULL and result.get("answer_id") is not None:
        print(f"Chaining FULL_BUILD -> build pipeline (answer {result['answer_id']})")
        from build_pipeline import run_build_pipeline
        build_result = run_build_pipeline(
            f"build answer {result['answer_id']}",
            use_claude_code=True,
            mode_select_fn=fixed_selection_fn(selected_mode, override_used),
        )
        print(json.dumps({
            "build": {
                "status": build_result.get("status"),
                "execution_decision": build_result.get("execution_decision"),
                "execution_blocked": build_result.get("execution_blocked"),
                "selected_mode": build_result.get("selected_mode"),
                "override_used": build_result.get("override_used"),
                "spec_status": build_result.get("spec_status"),
                "workspace_id": build_result.get("workspace_id"),
            },
        }, indent=2, default=str))
        return 0 if build_result.get("status") != "not_implementation" else 0
    # Phase 5 — Human-Readable Synthesis V3 display. Three views:
    #   --json   : the raw machine artifact (full debate result) — no change
    #              to existing automation that consumed the machine result.
    #   --audit  : the audit artifact (synthesized_answer).
    #   default  : the human-facing human_answer. Re-rendered with the user's
    #              selected_mode so "Recommended Next Action" reflects their
    #              choice (debate_engine persists a human_answer with the
    #              selected_mode=None fallback; the CLI is the presentation
    #              layer). Falls back to the stored human_answer, then to
    #              synthesized_answer, when the result lacks a
    #              verification_result to re-render from (legacy/fakes).
    if getattr(args, "json", False):
        print(json.dumps(
            {k: v for k, v in result.items() if k != "coding_agent"},
            indent=2, default=str))
        return 0 if result.get("status") != "failed" else 1
    payload = {
        "status": result.get("status"),
        "consensus_score": result.get("final_consensus_score"),
        "execution_decision": exec_dec or None,
        "execution_decision_source": exec_source or None,
        "recommended_mode": recommended_mode,
        "selected_mode": selected_mode,
        "override_used": override_used,
    }
    if getattr(args, "audit", False):
        payload["synthesized_answer"] = result.get("synthesized_answer", "")
    else:
        display_human = result.get("human_answer")
        vr = result.get("verification_result")
        if vr is not None:
            from human_synthesis_renderer import render_human_answer as _render
            display_human = _render(
                args.prompt, vr,
                result.get("synthesized_answer", ""),
                result.get("execution_decision"), selected_mode,
            )
        if not display_human:
            display_human = result.get("synthesized_answer", "")
        payload["human_answer"] = display_human
    print(json.dumps(payload, indent=2))
    return 0 if result.get("status") != "failed" else 1


def _cmd_build(args: argparse.Namespace) -> int:
    from build_pipeline import run_build_pipeline
    # Execution Mode Gate V1 — prompt the user to accept or override the
    # recommended execution depth BEFORE resurrecting/builds. The pipeline
    # maps the resurrected answer's execution_decision to a recommended mode,
    # calls prompt_execution_mode (Phase 3 menu), persists the selection
    # (Phase 4), and enforces it (Phase 5). The debate #145 failure mode —
    # verified architecture/decisions that still classified RESEARCH_ONLY and
    # never reached OpenCode — is fixed: the user can force FULL_BUILD here.
    from execution_mode_gate import prompt_execution_mode
    prompt = f"build answer {args.answer_id}"
    result = run_build_pipeline(prompt, use_claude_code=True,
                                mode_select_fn=prompt_execution_mode)
    # Phase 5 — Debate-Governed Execution Decisions: surface a block when the
    # resurrected answer is RESEARCH_ONLY or ARCHITECTURE_ONLY. The build
    # pipeline stopped before any spec generation or coding-agent call.
    if result.get("execution_blocked"):
        print("Execution Decision:")
        print(result.get("execution_decision", ""))
        src = result.get("execution_decision_source", "")
        if src:
            print(f"Source: {src}")
        reason = result.get("execution_reason", "")
        if reason:
            print(f"Reason: {reason}")
        print()
        print(json.dumps({
            "status": result.get("status"),
            "execution_decision": result.get("execution_decision"),
            "execution_decision_source": result.get("execution_decision_source"),
            "execution_blocked": True,
            "recommended_mode": result.get("recommended_mode"),
            "selected_mode": result.get("selected_mode"),
            "override_used": result.get("override_used"),
            "workspace_id": result.get("workspace_id"),
        }, indent=2))
        return 0  # the block is a deliberate STOP, not a failure.
    print(json.dumps({
        "classification": result.get("classification"),
        "workspace_id": result.get("workspace_id"),
        "answer_resurrected": result.get("answer_resurrected"),
        "recommended_mode": result.get("recommended_mode"),
        "selected_mode": result.get("selected_mode"),
        "override_used": result.get("override_used"),
        "spec_status": result.get("spec_status"),
        "spec_missing_sections": result.get("spec_missing_sections", []),
        "execution_result": {
            k: v for k, v in (result.get("execution_result") or {}).items()
            if k != "coding_agent"  # AgentResult is not JSON-serializable
        },
        "human_decisions": result.get("human_decisions"),
    }, indent=2, default=str))
    exec_result = result.get("execution_result") or {}
    return 0 if exec_result.get("success") else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    """Resume a workspace. Two methods (spec):

        Method 1 — ``sol0mon resume`` (no workspace_id).
            Lists the five most recent workspaces (canonical
            workspace_mgr + canonical lifecycle) and prompts for a
            project number; the chosen workspace resumes from its
            current engineering lifecycle stage.

        Method 2 — ``sol0mon resume <workspace_id>``.
            Advanced users pass the workspace_id directly; resume runs
            immediately without the list.

    Resume routes by engineering stage to the same canonical entry
    points the existing CLI trusted (resume.resume_build /
    resume_execution / resume_validation / workspace_orchestrator's
    follow-up debate path) — no new state, no duplicated metadata."""
    from workspace_resume_ui import execute_resume, select_workspace

    if getattr(args, "workspace_id", None):
        ws_id = args.workspace_id
    else:
        ws_id = select_workspace()
        if not ws_id:
            print("Resume cancelled.")
            return 1

    follow_up = getattr(args, "follow_up", None)
    result = execute_resume(ws_id,
                            use_claude_code=getattr(args, "use_claude_code",
                                                    False),
                            follow_up=follow_up)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("resumed", False) else (
        0 if result.get("reason") in ("already_complete", "blocked_pending_humans",
                                     "skipped")
        else 1)


def _cmd_status(args: argparse.Namespace) -> int:
    """Telemetry & Runtime Observability V1 — ``sol0mon status``.

    Reads ``workspace_status.json`` (written continuously by the recorder
    while the pipeline runs) and renders a human-readable runtime snapshot:
    current stage, active dispute, consensus %, round number, elapsed
    runtime, ETA, research requests, verification runs, and execution mode.
    Pure read — never starts a pipeline, never affects execution. With
    ``--report`` it also prints the persisted ``performance_report.json`` if
    one exists. With an explicit ``--workspace <id>`` it reads the build
    workspace's build_state.json under the configured WORKSPACE_ROOT instead
    of CWD.

    ``argparse`` rejects negative numbers; the path defaults are CWD-relative
    (where pipeline_runner writes them on the live CLI path).
    """
    import os
    from telemetry import (
        STAGE_PIPELINE, STAGE_DEBATE, STAGE_VERIFICATION,
    )


    status_path = args.status_path or os.path.join(os.getcwd(),
                                                    "workspace_status.json")
    report_path = args.report_path or os.path.join(os.getcwd(),
                                                    "performance_report.json")

    # ``--workspace <id>`` reads the build workspace's build_state.json
    # (written by build_pipeline) instead of the pipeline-ledger status file.
    if getattr(args, "workspace", None):
        try:
            from build_workspace import _workspace_dir
            ws_dir = _workspace_dir(args.workspace)
            alt = ws_dir / "workspace_status.json"
            if alt.is_file():
                status_path = str(alt)
            alt_r = ws_dir / "performance_report.json"
            if alt_r.is_file():
                report_path = str(alt_r)
        except Exception:
            pass

    if not os.path.isfile(status_path):
        print("No telemetry status found.")
        print(f"  looked for: {status_path}")
        print("  (run `sol0mon debate <prompt>` to start a pipeline that writes it)")
        return 1

    import json as _json
    try:
        with open(status_path, encoding="utf-8") as f:
            status = _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        print(f"status file unreadable: {e}")
        return 1

    eta = status.get("eta") or {}
    eta_text = eta.get("human_readable", "Estimate unavailable")
    print("=" * 60)
    print("SOL0MON — RUNTIME STATUS")
    print("=" * 60)
    print(f"  Current Stage      : {status.get('stage', 'unknown')}")
    print(f"  Active Dispute     : {status.get('active_dispute', '-') or '-'}")
    print(f"  Consensus %        : {status.get('consensus', 0)}")
    print(f"  Round Number       : {status.get('current_round', 0)}")
    elapsed = status.get("elapsed_seconds", 0)
    print(f"  Elapsed Runtime    : {round(float(elapsed or 0))}s")
    print(f"  ETA                : {eta_text}")
    print(f"  Research Requests  : {status.get('research_requests', 0)}")
    print(f"  Verification Runs  : {status.get('verification_runs', 0)}")
    print(f"  Execution Mode     : {status.get('execution_mode', '-') or '-'}")
    print(f"  Disputes           : "
          f"{status.get('resolved_disputes', 0)} resolved / "
          f"{status.get('remaining_disputes', 0)} remaining / "
          f"{status.get('total_disputes', 0)} total")
    if status.get("job_id"):
        print(f"  Job ID             : {status['job_id']}")
    if status.get("workspace_id"):
        print(f"  Workspace ID       : {status['workspace_id']}")
    print(f"  Updated            : {status.get('updated_at', '-')}")
    print("=" * 60)

    if getattr(args, "report", False):
        if not os.path.isfile(report_path):
            print("\nNo performance report found "
                  f"(looked for: {report_path})")
            return 0
        try:
            with open(report_path, encoding="utf-8") as f:
                rep = _json.load(f)
        except (OSError, _json.JSONDecodeError) as e:
            print(f"performance report unreadable: {e}")
            return 1
        print("\nPERFORMANCE REPORT")
        print("-" * 60)
        for k in ("research_time", "debate_time", "verification_time",
                 "architecture_time", "human_synthesis_time", "build_time",
                 "repair_time", "total_runtime", "disputes_resolved",
                 "average_round_duration", "average_verification_duration"):
            print(f"  {k:.<28}: {rep.get(k, 0)}")
        print("-" * 60)
    return 0


def _cmd_human(args: argparse.Namespace) -> int:
    from human_decision_store import store
    from workspace_manager import workspace_mgr, WorkspaceStatus
    from build_workspace import _workspace_dir, write_build_state
    build_dir = _workspace_dir(args.workspace_id)
    is_build_workspace = (build_dir / "workspace.json").is_file()
    if args.human_action == "list":
        for item in store.all_for_workspace(args.workspace_id):
            print(json.dumps(item, ensure_ascii=False, default=str))
        return 0
    item = store.get(args.decision_id, workspace_id=args.workspace_id)
    if item is None:
        print("Decision not found in the specified workspace.")
        return 1
    build_answer = None
    if is_build_workspace:
        try:
            build_answer = json.loads((build_dir / "answer.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print("Build workspace has no readable source answer; choice was not applied.")
            return 1
    ws = None if is_build_workspace else workspace_mgr.load_workspace(args.workspace_id)
    if ws is None and not is_build_workspace:
        print("Workspace not found.")
        return 1
    if not store.resolve(args.decision_id, args.option,
                         workspace_id=args.workspace_id):
        print("Choice must exactly match one of the recorded options; decision may already be resolved.")
        return 1
    choice = {
        "decision": f"Human decision: {item.get('question', '')} — {args.option}",
        "normalized_key": (item.get("normalized_key") or item.get("dispute_key")
                           or next(iter(item.get("affected_disputes") or []), None)
                           or args.decision_id),
        "status": "human_choice",
        "provenance": "human",
        "decision_id": args.decision_id,
        "chosen_option": args.option,
        "options": item.get("options", []),
        "reasoning": item.get("reasoning", ""),
    }
    if is_build_workspace:
        answer = build_answer
        answer_path = build_dir / "answer.json"
        choices = answer.setdefault("human_decisions_resolved", [])
        choices = [d for d in choices if d.get("decision_id") != args.decision_id]
        choices.append(choice)
        answer["human_decisions_resolved"] = choices
        answer_path.write_text(json.dumps(answer, indent=2), encoding="utf-8")
        decisions_path = build_dir / "decisions.json"
        try:
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            decisions = {}
        decisions["human_decisions_resolved"] = choices
        decisions_path.write_text(json.dumps(decisions, indent=2), encoding="utf-8")
        from build_pipeline import _source_result_from_answer, generate_build_spec
        source_result = _source_result_from_answer(answer)
        resolved_keys = {d.get("normalized_key") for d in choices}
        source_result["human_required"] = [
            d for d in source_result.get("human_required", [])
            if (d.get("normalized_key") or d.get("dispute_key")) not in resolved_keys
        ]
        verification_for_execution = dict(source_result.get("verification_result") or {})
        verification_for_execution["unresolved_claims"] = [
            d for d in verification_for_execution.get("unresolved_claims", [])
            if (d.get("normalized_key") or "") not in resolved_keys
        ]
        from execution_decision import classify_execution_decision
        execution_decision = classify_execution_decision(
            question=f"Build answer {answer.get('answer_id', '')}",
            verified_decisions=(source_result.get("verified_decisions", [])
                                 + source_result.get("governance_decisions", [])
                                 + source_result.get("human_choices", [])),
            verified_tradeoffs=source_result.get("verified_tradeoffs", []),
            human_required=source_result["human_required"],
            synthesized_answer=answer.get("synthesized_answer", ""),
            verification_result=verification_for_execution,
        )
        spec_result = generate_build_spec(
            args.workspace_id, answer.get("synthesized_answer", ""),
            source_result=source_result,
        )
        if not spec_result.get("spec_text"):
            print("Human choice was recorded, but the implementation spec could not be regenerated.")
            return 1
        write_build_state(args.workspace_id, "spec_generated",
                          spec_status=spec_result.get("status"),
                          human_decision_resolved=args.decision_id,
                          execution_decision=execution_decision.get("execution_decision"),
                          execution_reason="Reclassified after workspace-scoped human decision resolution.",
                          execution_confidence=execution_decision.get("execution_confidence", 0),
                          execution_decision_source="human_resolution")
        if args.execute:
            from resume import resume_build
            result = resume_build(args.workspace_id, use_claude_code=True)
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("resumed") else 1
        print("Human choice recorded and the build specification regenerated. "
              f"Continue with: python sol0mon_cli.py resume {args.workspace_id} --use-claude-code")
        return 0

    ws.verified_decisions = [d for d in ws.verified_decisions
                             if d.get("normalized_key") != choice["normalized_key"]]
    ws.verified_decisions.append(choice)
    ws.status = WorkspaceStatus.COMPLETED
    workspace_mgr.save_workspace(ws)
    print("Human choice recorded in workspace constraints. Resume the workspace to continue.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sol0mon",
        description="Project Sol0mon — autonomous consensus-driven architecture.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_debate = sub.add_parser("debate", help="Run the debate pipeline on a prompt.")
    p_debate.add_argument("prompt", help="The prompt to debate.")
    # Phase 4 — Human-Readable Synthesis V3. Default prints the human-facing
    # human_answer; --audit prints the raw audit artifact (synthesized_answer);
    # --json prints the full machine artifact (the debate result dict) exactly
    # as existing automation consumed it.
    p_debate.add_argument(
        "--audit", action="store_true", dest="audit", default=False,
        help="Display the audit artifact (synthesized_answer) instead of the "
             "human-facing answer.",
    )
    p_debate.add_argument(
        "--json", action="store_true", dest="json", default=False,
        help="Display the raw machine artifact (the full debate result) as JSON.",
    )
    p_debate.set_defaults(func=_cmd_debate)

    p_build = sub.add_parser("build", help="Build from a resurrected answer.")
    sub_build = p_build.add_subparsers(dest="build_subcommand", required=True)
    p_build_answer = sub_build.add_parser("answer", help="Build from answer <id>.")
    p_build_answer.add_argument("answer_id", type=int, help="Answer ID to resurrect.")
    p_build_answer.set_defaults(func=_cmd_build)

    p_resume = sub.add_parser(
        "resume",
        help="Resume a workspace from its current lifecycle stage. "
             "With no workspace_id: list the five most recent workspaces "
             "and select by project number. With an explicit workspace_id: "
             "resume immediately (advanced).")
    p_resume.add_argument("workspace_id", nargs="?", default=None,
                          help="Optional workspace ID to resume (advanced). "
                               "Omit to list recent workspaces and select.")
    p_resume.add_argument("--follow-up", dest="follow_up", default=None,
                          help="Follow-up question for a Research/Completed "
                               "stage workspace — runs a follow-up debate "
                               "through the existing workspace_orchestrator "
                               "follow-up path.")
    p_resume.add_argument("--use-claude-code", dest="use_claude_code",
                          action="store_true", default=False,
                          help="Opt in to a coding-agent execution resume "
                               "(Implementation stage). Default offline-safe.")
    p_resume.set_defaults(func=_cmd_resume)

    p_human = sub.add_parser("human", help="Inspect and resolve workspace human decisions.")
    sub_human = p_human.add_subparsers(dest="human_action", required=True)
    p_human_list = sub_human.add_parser("list", help="List decisions for a workspace.")
    p_human_list.add_argument("workspace_id")
    p_human_list.set_defaults(func=_cmd_human)
    p_human_resolve = sub_human.add_parser("resolve", help="Resolve with an exact recorded option.")
    p_human_resolve.add_argument("workspace_id")
    p_human_resolve.add_argument("decision_id")
    p_human_resolve.add_argument("option")
    p_human_resolve.add_argument("--execute", action="store_true",
                                 help="Resume and execute the build immediately after regenerating its specification.")
    p_human_resolve.set_defaults(func=_cmd_human)

    # Telemetry & Runtime Observability V1 — read-only runtime status.
    p_status = sub.add_parser("status",
                              help="Show the live runtime status "
                                   "(stage, dispute, consensus, round, "
                                   "elapsed, ETA).")
    p_status.add_argument("--status-path", dest="status_path", default=None,
                          help="Path to workspace_status.json "
                               "(default: ./workspace_status.json).")
    p_status.add_argument("--report-path", dest="report_path", default=None,
                          help="Path to performance_report.json "
                               "(default: ./performance_report.json).")
    p_status.add_argument("--workspace", dest="workspace", default=None,
                          help="Build workspace id — read its workspace_status.json"
                               " under the configured workspace root instead.")
    p_status.add_argument("--report", action="store_true", dest="report",
                          default=False,
                          help="Also print the persisted performance report.")
    p_status.set_defaults(func=_cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from answer_memory import DB_FILE as answer_db
    from workspace_manager import DB_FILE as workspace_db
    from build_workspace import WORKSPACE_ROOT
    logging.getLogger("sol0mon").info(
        "Persistent paths: answer_db=%s workspace_db=%s build_workspaces=%s",
        answer_db, workspace_db, WORKSPACE_ROOT,
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
