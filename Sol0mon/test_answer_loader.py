"""
test_answer_loader.py - Phase 3 tests for answer_loader.

Seeds a throwaway answer_memory.db (temp path) so the real
answer_memory.db is never touched. Verifies:
- get_answer_by_id returns the 4-field canonical shape
- get_latest_answer returns the newest (by id), filtered by topic tokens
- search_answers finds by keyword and returns synthesized_answer

No schema changes: uses answer_memory.memory.save_debate/get_debate directly.
"""

from __future__ import annotations

import os
import tempfile

import answer_memory
import answer_loader as al


def _with_tmp_db():
    """Point answer_memory at a fresh temp DB and reset the singleton so it
    re-initializes on the new path. Returns the temp path (for cleanup)."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)  # we just want a unique path; DB is created fresh
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _seed(*rows):
    """rows: tuples of (question, synthesized_answer, consensus_score)."""
    ids = []
    for question, syn, score in rows:
        i = answer_memory.memory.save_debate(
            question=question, gpt_final="gpt", gemini_final="gemini",
            synthesized_answer=syn, consensus_score=score,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
        )
        ids.append(i)
    return ids


def test_get_answer_by_id_shape() -> None:
    path = _with_tmp_db()
    try:
        ids = _seed(("Design Sol0mon architecture", "arch-body", 80))
        out = al.get_answer_by_id(ids[0])
        assert out is not None
        assert set(out.keys()) == {
            "answer_id", "question", "topic", "synthesized_answer",
            "verification_result", "resolved_decisions", "verified_decisions",
        }
        assert out["answer_id"] == ids[0]
        assert out["question"] == "Design Sol0mon architecture"
        assert out["synthesized_answer"] == "arch-body"
        assert out["topic"] == "sol0mon architecture"
        assert al.get_answer_by_id(9999999) is None
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_get_latest_answer_newest_overall() -> None:
    path = _with_tmp_db()
    try:
        _seed(
            ("Design Sol0mon architecture", "arch1", 80),
            ("Build a stat arb bot", "bot1", 70),
            ("Design Sol0mon v2", "arch2", 85),
        )
        latest = al.get_latest_answer()
        assert latest is not None
        # Newest by id (highest), which is the last seeded row.
        assert latest["question"] == "Design Sol0mon v2"
        assert latest["synthesized_answer"] == "arch2"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_get_latest_answer_filtered_by_topic() -> None:
    path = _with_tmp_db()
    try:
        _seed(
            ("Design Sol0mon architecture", "arch1", 80),
            ("Build a stat arb bot for crypto", "bot1", 70),
            ("Design Sol0mon architecture v2", "arch2", 85),
        )
        topic = "sol0mon architecture"
        latest = al.get_latest_answer(topic=topic)
        assert latest is not None
        assert latest["question"] == "Design Sol0mon architecture v2"
        # A topic that matches nothing -> None.
        assert al.get_latest_answer(topic="nonexistenttopic") is None
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_search_answers_returns_synthesized_text() -> None:
    path = _with_tmp_db()
    try:
        _seed(
            ("Design Sol0mon architecture", "arch-content", 80),
            ("Build a stat arb bot", "bot-content", 70),
        )
        hits = al.search_answers("sol0mon")
        assert len(hits) == 1
        assert hits[0]["synthesized_answer"] == "arch-content"
        assert hits[0]["topic"]
        assert al.search_answers("zzzznothing") == []
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_does_not_touch_protected_logic() -> None:
    protected = {
        "debate_engine", "governance", "verification_engine",
        "consensus_engine", "chat_session", "gpt_worker", "gemini_worker",
    }
    imported = set(getattr(m, "__name__", "") for m in al.__dict__.values()
                  if hasattr(m, "__name__"))
    assert not (protected & imported), \
        f"answer_loader pulls in protected modules: {protected & imported}"


if __name__ == "__main__":
    test_get_answer_by_id_shape()
    test_get_latest_answer_newest_overall()
    test_get_latest_answer_filtered_by_topic()
    test_search_answers_returns_synthesized_text()
    test_does_not_touch_protected_logic()
    print("test_answer_loader: ALL PASSED")
