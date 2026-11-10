"""
Project Sol0mon - Phase 2: Human Decision Aggregation.

Problem this solves: governance produces one HUMAN_REQUIRED outcome per
unresolved dispute, and historically each interrupts the user separately.
This module collects all HUMAN_REQUIRED outcomes from a governance pass
into ONE bundle so the user is asked once.

API:
    add_decision(decision)      # accumulate one HUMAN_REQUIRED outcome
    generate_bundle()           # collapse accumulated decisions into one bundle
    collect_from_governance_result(gov_result, workspace_id)
                                # integration seam: feed a GovernanceResult.to_dict()
                                  (key "human_required") straight in -> one bundle

Decision shape (from governance.GovernedOutcome.to_dict()):
    outcome_type, dispute_key, dispute_text, decision, option_a, option_b,
    recommendation, confidence, reasoning, ...

Bundle shape (output of generate_bundle()):
    bundle_id, workspace_id, created_at, decision_count, decisions, summary
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decision_key(d: dict) -> str:
    """Stable identity for a decision, for dedup. Falls back through
    governance's alternate key names so callers don't have to normalize."""
    return (d.get("decision_id")
            or d.get("dispute_key")
            or d.get("normalized_key")
            or d.get("dispute_text")
            or "")


@dataclass
class DecisionBundle:
    """Accumulator that collapses N HUMAN_REQUIRED outcomes into one ask."""

    workspace_id: str = ""
    decisions: list[dict] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def add_decision(self, decision: dict) -> None:
        """Append a HUMAN_REQUIRED decision, deduping by stable key."""
        k = _decision_key(decision)
        if k and k in self._seen:
            return
        self.decisions.append(decision)
        if k:
            self._seen.add(k)

    def generate_bundle(self) -> dict:
        """Collapse accumulated decisions into a single bundle.

        This is the 'ask the user once' payload: one summary line plus the
        full decision list for the UI to render as one prompt.
        """
        keys = [_decision_key(d) or d.get("dispute_text", "?")
                for d in self.decisions]
        summary = (f"{len(self.decisions)} human-required decision(s) need "
                   f"your input: {', '.join(keys)}" if self.decisions
                   else "No human-required decisions.")
        return {
            "bundle_id": str(uuid.uuid4()),
            "workspace_id": self.workspace_id,
            "created_at": _now(),
            "decision_count": len(self.decisions),
            "decisions": list(self.decisions),
            "summary": summary,
        }


# ---------------------------------------------------------------------------
# Module-level convenience (mirrors workspace_manager pattern)
# ---------------------------------------------------------------------------

_default_bundle = DecisionBundle()


def add_decision(decision: dict, bundle: "DecisionBundle | None" = None) -> None:
    """Accumulate a HUMAN_REQUIRED decision. Uses the module default bundle
    when none is passed, so callers can collect across a governance run and
    then call generate_bundle() once."""
    (bundle if bundle is not None else _default_bundle).add_decision(decision)


def generate_bundle(bundle: "DecisionBundle | None" = None) -> dict:
    """Produce one bundle from accumulated decisions. Resets the module
    default bundle after generating so the next run starts clean."""
    if bundle is not None:
        return bundle.generate_bundle()
    out = _default_bundle.generate_bundle()
    reset_bundle()
    return out


def reset_bundle() -> None:
    """Clear the module default bundle. Call between independent runs."""
    global _default_bundle
    _default_bundle = DecisionBundle()


# ---------------------------------------------------------------------------
# Integration seam - callable from the debate pipeline's post-governance
# block without modifying governance scoring or debate methodology.
# ---------------------------------------------------------------------------

def collect_from_governance_result(gov_result: dict,
                                   workspace_id: str = "") -> dict:
    """Collect all HUMAN_REQUIRED outcomes from a GovernanceResult dict and
    produce a single bundle. Does not mutate gov_result.

    GovernanceResult.to_dict() exposes human-required items under
    "human_required" (lowercase); we also accept "human_required_items"
    for the workspace-side dict shape. One bundle for the whole pass.
    """
    items = []
    if isinstance(gov_result, dict):
        items = gov_result.get("human_required") \
            or gov_result.get("human_required_items") \
            or []
    bundle = DecisionBundle(workspace_id=workspace_id)
    for item in items:
        bundle.add_decision(item)
    return bundle.generate_bundle()


if __name__ == "__main__":
    # Smoke self-check: 3 HUMAN_REQUIRED -> 1 bundle
    outcomes = [
        {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "db_choice",
         "dispute_text": "PostgreSQL vs SQLite", "option_a": "PostgreSQL",
         "option_b": "SQLite"},
        {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "cache_choice",
         "dispute_text": "Redis vs Memcached", "option_a": "Redis",
         "option_b": "Memcached"},
        {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "queue_choice",
         "dispute_text": "Kafka vs RabbitMQ", "option_a": "Kafka",
         "option_b": "RabbitMQ"},
    ]
    b = DecisionBundle()
    for o in outcomes:
        b.add_decision(o)
    bundle = b.generate_bundle()
    assert bundle["decision_count"] == 3, bundle["decision_count"]
    assert len(bundle["decisions"]) == 3
    assert "db_choice" in bundle["summary"]
    # dedup
    b.add_decision(outcomes[0])
    assert len(b.decisions) == 3
    # seam
    gov = {"human_required": outcomes}
    seam = collect_from_governance_result(gov, "ws-1")
    assert seam["decision_count"] == 3 and seam["workspace_id"] == "ws-1"
    print("decision_bundle smoke test PASSED")
