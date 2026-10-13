"""
Human Decision Queue — backward-compat shim over HumanDecisionStore.

This module preserves the public API (HumanDecision, HumanDecisionQueue,
enqueue_human_decision, resolve_human_decision, get_pending_for_workspace)
by delegating to the unified HumanDecisionStore. No separate SQLite table;
the store is the single source of truth.

When the debate path enqueues a decision, it goes to the store ONLY — no
duplicate write to workspace.human_required_items (the store is the authority
and Workspace.pending_human_items reads from it directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from human_decision_store import store


# ---------------------------------------------------------------------------
# HumanDecision dataclass — preserved for backward compat
# ---------------------------------------------------------------------------

@dataclass
class HumanDecision:
    """A single decision that requires human input. Kept for backward compat;
    the store persists arbitrary dicts, this class is a convenience shape."""

    decision_id: str
    workspace_id: str
    question: str
    options: list[str] = field(default_factory=list)
    reasoning: str = ""
    affected_disputes: list[str] = field(default_factory=list)
    gpt_position: str = ""
    gemini_position: str = ""

    resolved: bool = False
    chosen_option: str = ""
    resolved_at: str = ""

    created_at: str = ""
    confidence: int = 0
    priority: str = "MEDIUM"

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "workspace_id": self.workspace_id,
            "question": self.question,
            "options": self.options,
            "reasoning": self.reasoning,
            "affected_disputes": self.affected_disputes,
            "gpt_position": self.gpt_position,
            "gemini_position": self.gemini_position,
            "resolved": self.resolved,
            "chosen_option": self.chosen_option,
            "resolved_at": self.resolved_at,
            "created_at": self.created_at,
            "confidence": self.confidence,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HumanDecision":
        return cls(
            decision_id=data.get("decision_id", ""),
            workspace_id=data.get("workspace_id", ""),
            question=data.get("question", ""),
            options=data.get("options", []),
            reasoning=data.get("reasoning", ""),
            affected_disputes=data.get("affected_disputes", []),
            gpt_position=data.get("gpt_position", ""),
            gemini_position=data.get("gemini_position", ""),
            resolved=data.get("resolved", False),
            chosen_option=data.get("chosen_option", ""),
            resolved_at=data.get("resolved_at", ""),
            created_at=data.get("created_at", ""),
            confidence=data.get("confidence", 0),
            priority=data.get("priority", "MEDIUM"),
        )


# ---------------------------------------------------------------------------
# HumanDecisionQueue — delegates to the store
# ---------------------------------------------------------------------------

class HumanDecisionQueue:
    """Manages the lifecycle of human-required decisions. Delegates to the
    unified HumanDecisionStore. Kept for backward compat."""

    def __init__(self):
        pass

    def enqueue(self, decision: HumanDecision) -> str:
        """Add a human-required decision. Returns the decision_id."""
        did = store.create(decision.workspace_id, decision.to_dict())
        print(f"[HUMAN QUEUE] Enqueued decision {did[:8]}... "
              f"for workspace {decision.workspace_id[:8]}...: "
              f"{decision.question[:80]}")
        return did

    def get_pending(self, workspace_id: str) -> list[HumanDecision]:
        """Get all unresolved human decisions for a workspace."""
        return [HumanDecision.from_dict(d)
                for d in store.pending(workspace_id)]

    def get_all(self, workspace_id: str) -> list[HumanDecision]:
        """Get all human decisions (resolved and pending)."""
        return [HumanDecision.from_dict(d)
                for d in store.all_for_workspace(workspace_id)]

    def resolve(self, decision_id: str, chosen_option: str) -> bool:
        """Resolve a human decision with the user's choice."""
        success = store.resolve(decision_id, chosen_option)
        if success:
            print(f"[HUMAN QUEUE] Resolved decision {decision_id[:8]}... "
                  f"-> {chosen_option}")
        return success

    def get_decision(self, decision_id: str) -> HumanDecision | None:
        """Get a single decision by ID."""
        d = store.get(decision_id)
        return HumanDecision.from_dict(d) if d else None

    def get_pending_count(self, workspace_id: str) -> int:
        """Count unresolved human decisions for a workspace."""
        return store.pending_count(workspace_id)

    def list_pending_all_workspaces(self) -> list[HumanDecision]:
        """Get all pending human decisions across all workspaces."""
        return [HumanDecision.from_dict(d)
                for d in store.pending_all_workspaces()]

    def delete_decision(self, decision_id: str) -> bool:
        """Remove a decision from the queue."""
        return store.delete(decision_id)


# ---------------------------------------------------------------------------
# Convenience functions — delegate to the store (single source of truth)
# ---------------------------------------------------------------------------

def enqueue_human_decision(
    workspace_id: str,
    question: str,
    options: list[str],
    reasoning: str = "",
    affected_disputes: list[str] | None = None,
    gpt_position: str = "",
    gemini_position: str = "",
    confidence: int = 0,
    priority: str = "MEDIUM",
) -> str:
    """Create and enqueue a human-required decision in the unified store.

    The store is the single source of truth; Workspace.pending_human_items
    reads from it. No duplicate write to workspace.human_required_items.
    """
    decision = HumanDecision(
        decision_id="",
        workspace_id=workspace_id,
        question=question,
        options=options,
        reasoning=reasoning,
        affected_disputes=affected_disputes or [],
        gpt_position=gpt_position,
        gemini_position=gemini_position,
        confidence=confidence,
        priority=priority,
    )
    return HumanDecisionQueue().enqueue(decision)


def resolve_human_decision(decision_id: str, chosen_option: str) -> bool:
    """Resolve a human decision in the store. The store is the authority;
    Workspace.pending_human_items reads from it so no separate sync needed."""
    return HumanDecisionQueue().resolve(decision_id, chosen_option)


def get_pending_for_workspace(workspace_id: str) -> list[dict]:
    """Get pending human decisions as dicts for a workspace."""
    return store.pending(workspace_id)


if __name__ == "__main__":
    from workspace_manager import create_workspace, delete_workspace

    ws_id = create_workspace("Design a scalable database architecture")
    print(f"Workspace: {ws_id}")

    d_id = enqueue_human_decision(
        workspace_id=ws_id,
        question="Database Choice: SQLite vs PostgreSQL",
        options=["SQLite", "PostgreSQL"],
        reasoning="MVP simplicity vs production scalability.",
        affected_disputes=["database_choice"],
        confidence=40,
        priority="HIGH",
    )
    print(f"Decision ID: {d_id}")

    queue = HumanDecisionQueue()
    pending = queue.get_pending(ws_id)
    assert len(pending) == 1
    assert pending[0].question == "Database Choice: SQLite vs PostgreSQL"
    print(f"Pending: {len(pending)}")

    resolved = resolve_human_decision(d_id, "PostgreSQL")
    assert resolved

    pending = queue.get_pending(ws_id)
    assert len(pending) == 0
    all_decisions = queue.get_all(ws_id)
    assert len(all_decisions) == 1
    assert all_decisions[0].resolved
    assert all_decisions[0].chosen_option == "PostgreSQL"
    print("Resolution verified")

    delete_workspace(ws_id)
    print("Human decision queue smoke test PASSED")
