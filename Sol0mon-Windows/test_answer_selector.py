"""
test_answer_selector.py - Phase 3 tests for answer_selector.

Verifies:
- identify_topic strips command/stopwords and lowercases
- topic_matches does token-subset matching (empty topic = matches all)
- select_answer dispatches:
    'answer 54' / '54' / 'build answer 54' -> direct ID lookup
    'build latest <topic>'                 -> latest answer matching topic
    'latest'                               -> newest overall
    malformed                              -> None

Seeds a throwaway temp DB so the real answer_memory.db is untouched.
"""

from __future__ import annotations

import os
import tempfile

import answer_memory
import answer_selector as ase


def _with_tmp_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _seed(*rows):
    ids = []
    for question, syn, score in rows:
        i = answer_memory.memory.save_debate(
            question=question, gpt_final="g", gemini_final="m",
            synthesized_answer=syn, consensus_score=score,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
        )
        ids.append(i)
    return ids


def test_identify_topic_strips_stopwords() -> None:
    assert ase.identify_topic("build latest statarb bot") == "statarb bot"
    assert ase.identify_topic("Build Latest Sol0mon Architecture") == "sol0mon architecture"
    assert ase.identify_topic("latest") == ""
    assert ase.identify_topic("") == ""
    assert ase.identify_topic("answer 54") == "54"


def test_topic_matches() -> None:
    assert ase.topic_matches("sol0mon architecture", "Design Sol0mon architecture v2")
    assert ase.topic_matches("", "anything whatsoever")
    # genuine negative: token absent from text
    assert ase.topic_matches("statarb", "Design Sol0mon architecture") is False
    # multi-token subset: every token must be present
    assert ase.topic_matches("statarb bot", "a stateful bot") is False  # 'statarb' missing
    assert ase.topic_matches("bot cyber", "cyber bot corps")  # both tokens present


def test_select_answer_by_id_variants() -> None:
    path = _with_tmp_db()
    try:
        ids = _seed(
            ("Design Sol0mon architecture", "a1", 80),
            ("Build a stat arb bot for crypto", "a2", 70),
            ("Design Sol0mon architecture v2", "a3", 85),
        )
        target = ids[2]

        for spec in (f"answer {target}", f"build answer {target}", str(target)):
            out = ase.select_answer(spec)
            assert out is not None
            assert out["answer_id"] == target
            assert out["synthesized_answer"] == "a3"

        # Missing id -> None.
        assert ase.select_answer("answer 9999999") is None
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_select_answer_latest_with_topic() -> None:
    path = _with_tmp_db()
    try:
        ids = _seed(
            ("Design Sol0mon architecture", "a1", 80),
            ("Build a stat arb bot for crypto", "a2", 70),
            ("Design Sol0mon architecture v2", "a3", 85),
        )
        out = ase.select_answer("build latest sol0mon architecture")
        assert out is not None
        assert out["answer_id"] == ids[2]
        assert out["synthesized_answer"] == "a3"

        # Latest matching a topic that only the bot-row has.
        bot = ase.select_answer("build latest crypto bot")
        # 'crypto' appears in the bot row; 'bot' appears in the bot row too.
        # 'crypto bot' -> topic 'crypto bot' (both tokens must appear).
        assert bot is not None
        assert bot["answer_id"] == ids[1]
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_select_answer_bare_latest_is_newest_overall() -> None:
    path = _with_tmp_db()
    try:
        ids = _seed(
            ("Design Sol0mon architecture", "a1", 80),
            ("Build a stat arb bot for crypto", "a2", 70),
        )
        out = ase.select_answer("latest")
        assert out is not None
        assert out["answer_id"] == ids[1]  # newest by id
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_select_answer_malformed_returns_none() -> None:
    path = _with_tmp_db()
    try:
        _seed(("Design Sol0mon architecture", "a1", 80))
        # topic that matches nothing
        assert ase.select_answer("build latest zzqxnotopic") is None
        assert ase.select_answer("") is None
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_select_answer_output_shape_includes_topic() -> None:
    path = _with_tmp_db()
    try:
        ids = _seed(("Design Sol0mon architecture", "a1", 80))
        out = ase.select_answer(f"answer {ids[0]}")
        assert set(out.keys()) == {
            "answer_id", "question", "topic", "synthesized_answer",
            "verification_result", "resolved_decisions", "verified_decisions",
        }
        assert out["topic"] == "sol0mon architecture"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_does_not_touch_protected_logic() -> None:
    protected = {
        "debate_engine", "governance", "verification_engine",
        "consensus_engine", "chat_session", "gpt_worker", "gemini_worker",
    }
    # select_answer imports answer_loader lazily; check the module's own imports
    import sys
    mods = set(sys.modules.keys())
    leaked = protected & mods - {"answer_memory"}  # answer_memory is allowed
    assert not leaked, f"protected modules imported by selector path: {leaked}"


if __name__ == "__main__":
    test_identify_topic_strips_stopwords()
    test_topic_matches()
    test_select_answer_by_id_variants()
    test_select_answer_latest_with_topic()
    test_select_answer_bare_latest_is_newest_overall()
    test_select_answer_malformed_returns_none()
    test_select_answer_output_shape_includes_topic()
    test_does_not_touch_protected_logic()
    print("test_answer_selector: ALL PASSED")
