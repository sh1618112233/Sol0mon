"""
Workspace Orchestrator — workspace-aware job orchestration layer.

Extends the existing orchestrator pattern with workspace lifecycle:
- submit_workspace(): Create a new workspace and start a debate
- resume_workspace(): Follow-up on an existing workspace
- reopen_dispute(): Reopen a specific dispute in a workspace
- resolve_human_requirement(): Provide a human decision to unblock a workspace

Backward compatible: existing orchestrator.submit_job() continues to work.
"""

import threading
from datetime import datetime, timezone

from job_store import (
    initialize_database as init_jobs,
    create_job,
    get_job,
    update_job,
)
from workspace_manager import (
    workspace_mgr,
    WorkspaceStatus,
)
from workspace_debate_engine import WorkspaceDebateEngine
from human_decision_queue import (
    HumanDecisionQueue,
    enqueue_human_decision,
    resolve_human_decision,
    get_pending_for_workspace,
)

# Lazy init
_jobs_initialized = False


def _ensure_db():
    global _jobs_initialized
    if not _jobs_initialized:
        init_jobs()
        _jobs_initialized = True


# ---------------------------------------------------------------------------
# Workspace-aware job runner
# ---------------------------------------------------------------------------


def _safe_run_workspace_job(job_id: str, workspace_id: str,
                             prompt: str, memory_ids: list | None = None):
    """Safely run a workspace debate job with crash recovery."""
    try:
        engine = WorkspaceDebateEngine()
        result = engine.run_in_workspace(
            prompt=prompt,
            memory_ids=memory_ids,
            workspace_id=workspace_id,
        )
        if result.get("_failed"):
            update_job(
                job_id=job_id,
                status="FAILED",
                consensus_score=result.get("final_consensus_score", 0),
                result=result.get("error", "Unknown workspace error"),
                error=result.get("error", ""),
            )
        else:
            update_job(
                job_id=job_id,
                status="COMPLETED",
                consensus_score=result.get("final_consensus_score", 0),
                result=result.get("synthesized_answer", ""),
            )
    except Exception as e:
        print(f"\n[WORKSPACE ORCHESTRATOR CRASH] Job #{job_id}: {e}")
        try:
            update_job(
                job_id=job_id,
                status="FAILED",
                consensus_score=0,
                result="[CRASH]",
                error=str(e),
            )
            # Also mark workspace as having a failure
            workspace_mgr.update_status(workspace_id, WorkspaceStatus.CREATED,
                                        error=str(e))
        except Exception as db_err:
            print(f"[WORKSPACE ORCHESTRATOR CORRUPTION] {db_err}")


def _safe_run_resume_job(job_id: str, workspace_id: str, follow_up: str):
    """Safely run a workspace resume job with crash recovery."""
    try:
        engine = WorkspaceDebateEngine()
        result = engine.resume_workspace(workspace_id, follow_up)
        if result.get("_failed"):
            update_job(
                job_id=job_id,
                status="FAILED",
                consensus_score=result.get("final_consensus_score", 0),
                result=result.get("error", "Unknown resume error"),
                error=result.get("error", ""),
            )
        else:
            update_job(
                job_id=job_id,
                status="COMPLETED",
                consensus_score=result.get("final_consensus_score", 0),
                result=result.get("synthesized_answer", ""),
            )
    except Exception as e:
        print(f"\n[WORKSPACE ORCHESTRATOR CRASH] Job #{job_id}: {e}")
        try:
            update_job(
                job_id=job_id,
                status="FAILED",
                consensus_score=0,
                result="[CRASH]",
                error=str(e),
            )
        except Exception as db_err:
            print(f"[WORKSPACE ORCHESTRATOR CORRUPTION] {db_err}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def submit_workspace(prompt: str, memory_ids: list | None = None) -> dict:
    """Submit a new prompt as a workspace-based debate.

    Returns dict with workspace_id, job_id so the caller can poll.
    """
    _ensure_db()

    # Create workspace first
    workspace_id = workspace_mgr.create_workspace(prompt, memory_ids=memory_ids)

    # Create a job for status tracking (existing orchestrator compatibility)
    job_id = create_job(prompt)

    worker = threading.Thread(
        target=_safe_run_workspace_job,
        args=(job_id, workspace_id, prompt, memory_ids),
        daemon=False,
    )
    worker.start()

    return {
        "workspace_id": workspace_id,
        "job_id": job_id,
        "status": "DEBATING",
    }


def follow_up_workspace(workspace_id: str, follow_up: str) -> dict:
    """Submit a follow-up question to an existing workspace.

    Only affected disputes are re-debated. Does NOT rerun the full pipeline.
    """
    _ensure_db()

    # Verify workspace exists
    ws = workspace_mgr.load_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace {workspace_id} not found"}

    job_id = create_job(f"[FOLLOW-UP] {follow_up}")

    worker = threading.Thread(
        target=_safe_run_resume_job,
        args=(job_id, workspace_id, follow_up),
        daemon=False,
    )
    worker.start()

    return {
        "workspace_id": workspace_id,
        "job_id": job_id,
        "status": "DEBATING",
        "existing_decisions": len(ws.verified_decisions),
        "existing_tradeoffs": len(ws.verified_tradeoffs),
        "pending_human_items": len(ws.pending_human_items),
    }


def reopen_workspace_dispute(workspace_id: str, dispute_key: str,
                              reason: str = "") -> dict:
    """Reopen a specific dispute in a workspace."""
    _ensure_db()

    ws = workspace_mgr.load_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace {workspace_id} not found"}

    job_id = create_job(f"[REOPEN] {dispute_key}: {reason}")

    engine = WorkspaceDebateEngine()
    # Run synchronously since reopening is typically lightweight
    result = engine.reopen_dispute(workspace_id, dispute_key, reason)

    if result.get("_failed"):
        update_job(job_id=job_id, status="FAILED",
                   result=result.get("error", "Reopen failed"),
                   error=result.get("error", ""),
                   consensus_score=0)
    else:
        update_job(job_id=job_id, status="COMPLETED",
                   result=result.get("synthesized_answer", ""),
                   consensus_score=result.get("final_consensus_score", 0))

    return {
        "workspace_id": workspace_id,
        "job_id": job_id,
        "status": "COMPLETED",
        "result": result,
    }


def resolve_human_requirement(workspace_id: str, decision_id: str,
                               chosen_option: str) -> dict:
    """Resolve a HUMAN_REQUIRED outcome with a user's decision.

    Marks the human decision as resolved and updates the workspace.
    The caller should then call follow_up_workspace() to resume debate
    with the resolved constraint.
    """
    ws = workspace_mgr.load_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace {workspace_id} not found"}

    # Resolve in human decision queue
    success = resolve_human_decision(decision_id, chosen_option)
    if not success:
        return {"error": f"Could not resolve decision {decision_id} — "
                         "it may already be resolved or not exist"}

    # Update workspace status if all human items resolved
    queue = HumanDecisionQueue()
    if queue.get_pending_count(workspace_id) == 0:
        # Check workspace too
        ws2 = workspace_mgr.load_workspace(workspace_id)
        if ws2 and not ws2.pending_human_items:
            ws2.status = WorkspaceStatus.CONSENSUS_REACHED
            workspace_mgr.save_workspace(ws2)

    return {
        "workspace_id": workspace_id,
        "decision_id": decision_id,
        "chosen_option": chosen_option,
        "status": "resolved",
        "pending_remaining": queue.get_pending_count(workspace_id),
    }


def get_workspace_status(workspace_id: str) -> dict:
    """Get current workspace status including pending items."""
    ws = workspace_mgr.load_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace {workspace_id} not found"}

    return {
        "workspace_id": ws.workspace_id,
        "original_prompt": ws.original_prompt[:200],
        "status": ws.status,
        "consensus_score": ws.consensus_score,
        "verified_decisions_count": len(ws.verified_decisions),
        "verified_tradeoffs_count": len(ws.verified_tradeoffs),
        "human_required_count": len(ws.pending_human_items),
        "answer_count": len(ws.answer_history),
        "human_required_items": ws.pending_human_items,
        "verified_tradeoffs": [
            {"option_a": t.get("option_a", ""),
             "option_b": t.get("option_b", ""),
             "recommendation": t.get("recommendation", "")}
            for t in ws.verified_tradeoffs
        ],
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
    }


def list_workspaces(status: str | None = None, limit: int = 50) -> list[dict]:
    """List workspaces, optionally filtered by status."""
    return workspace_mgr.list_workspaces(limit=limit, status=status)


def get_answer_chain(answer_id: int) -> list[dict]:
    """Get the full answer chain from a given answer back to the original."""
    return memory.get_answer_chain(answer_id)


# Import for answer_chain
from answer_memory import memory


if __name__ == "__main__":
    # Smoke test (doesn't require browser since we only test the non-browser parts)
    result = submit_workspace("Quick smoke test — will fail without browser")
    print(f"Workspace submitted: {result}")

    ws_id = result["workspace_id"]
    status = get_workspace_status(ws_id)
    print(f"Status: {status['status']}, decisions={status['verified_decisions_count']}")

    # Clean up test workspace
    workspace_mgr.delete_workspace(ws_id)
    print("Workspace orchestrator smoke test PASSED")