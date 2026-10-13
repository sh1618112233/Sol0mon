"""
Project Sol0mon - Phase 3: Answer Resurrection - answer_loader.

Reads synthesized answers from the existing answer_memory.db WITHOUT schema
changes. Wraps answer_memory.memory and reshapes each row into the canonical
Build Foundation contract:

    {
        "answer_id": 54,
        "question": "...",
        "topic": "...",
        "synthesized_answer": "..."
    }

API:
    get_answer_by_id(answer_id)            -> dict | None
    get_latest_answer(topic: str | None)   -> dict | None  (newest by id DESC;
                                             filtered by topic token subset)
    search_answers(query, limit=20)        -> list[dict]

No mutation of answer_memory.py. The 'topic' field is derived via
answer_selector.identify_topic (pure helper). The latest-filter uses
answer_selector.topic_matches.
"""

from __future__ import annotations

from answer_memory import memory
from answer_selector import identify_topic, topic_matches


def _to_verified_decisions(verification_result: dict) -> list[dict]:
    """Bridge Schema B -> Schema A: translate verification_result.resolved_claims
    into the GovernedOutcome.to_dict shape that the build extractor
    (build_prompt_generator.extract_decisions/constraints) consumes.

    Only RESOLVED claims become verified_decisions; UNRESOLVED claims are
    intentionally NOT forced into the immutable-decisions bucket. The `finding`
    states the winner and is stored as both `decision` and `reasoning` so the
    build prompt can present and enforce it.
    """
    out: list[dict] = []
    if not isinstance(verification_result, dict):
        return out
    for claim in verification_result.get("resolved_claims", []) or []:
        if not isinstance(claim, dict):
            continue
        v = claim.get("verification", {}) if isinstance(claim.get("verification"), dict) else {}
        finding = str(v.get("finding", "") or "")
        if not finding:
            continue
        try:
            confidence = int(v.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        out.append({
            "outcome_type": "VERIFIED_DECISION",
            "dispute_key": claim.get("normalized_key", ""),
            "dispute_text": claim.get("dispute", ""),
            "decision": finding,
            "option_a": None,
            "option_b": None,
            "recommendation": None,
            "confidence": confidence,
            "reasoning": finding,
            "evidence_quality": "unassessed",
            "architectural_validity": "unassessed",
            "implementation_impact": "unassessed",
            "operational_impact": "unassessed",
            "scalability_impact": "unassessed",
        })
    return out


def _shape(debate: dict) -> dict:
    """Reshape an answer_memory debate row into the Build Foundation output.

    FIX 2: carries the machine-readable decision model, not just prose.
    `verification_result` is the authoritative source at rest; `resolved_decisions`
    is the enriched per-dispute record; `verified_decisions` is the Schema-A
    bucket the build extractor consumes as immutable constraints.
    """
    return {
        "answer_id": debate["id"],
        "question": debate["question"],
        "topic": identify_topic(debate["question"]),
        "synthesized_answer": debate["synthesized_answer"],
        "verification_result": debate.get("verification_result", {}),
        "resolved_decisions": debate.get("resolved_decisions", []),
        "verified_decisions": _to_verified_decisions(debate.get("verification_result", {})),
    }


def get_answer_by_id(answer_id: int) -> dict | None:
    """Fetch one answer by integer ID. None if missing."""
    d = memory.get_debate(answer_id)
    return _shape(d) if d else None


def get_latest_answer(topic: str | None = None) -> dict | None:
    """Newest answer (highest id), optionally filtered by topic token subset.

    topic is a space-joined token string (e.g. 'statarb bot'); a candidate
    matches when every topic token appears in its question (case-insensitive).
    Empty/None topic = newest answer overall.
    """
    for row in memory.get_recent_debates(limit=200):  # newest-first by id DESC
        if topic is None or topic_matches(topic, row["question"]):
            d = memory.get_debate(row["id"])
            if d:
                return _shape(d)
    return None


def search_answers(query: str, limit: int = 20) -> list[dict]:
    """Free-text search across question + decision_summary + conflict_points +
    resolved_decisions. Returns shaped answers (with synthesized_answer)."""
    out = []
    for row in memory.search_debates(query, limit=limit):
        d = memory.get_debate(row["id"])
        if d:
            out.append(_shape(d))
    return out


if __name__ == "__main__":
    # Smoke self-check against real answer_memory.db (read-only).
    stats = memory.get_stats()
    total = stats.get("total_debates", 0)
    if total == 0:
        print("answer_loader smoke test SKIPPED (no debates in memory)")
    else:
        recent = memory.get_recent_debates(limit=1)
        if recent:
            latest = get_answer_by_id(recent[0]["id"])
            assert latest is not None
            assert set(latest.keys()) == {
                "answer_id", "question", "topic", "synthesized_answer",
                "verification_result", "resolved_decisions", "verified_decisions",
            }
            assert get_answer_by_id(999999) is None
            assert get_latest_answer() is not None
            print(f"answer_loader smoke test PASSED ({total} debates in memory)")
