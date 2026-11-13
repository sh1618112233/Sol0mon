from __future__ import annotations

import answer_memory


def test_answer_search_treats_like_metacharacters_literally(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_memory, "DB_FILE", str(tmp_path / "answers.db"))
    memory = answer_memory.AnswerMemory()
    memory.save_debate(
        question="Rates 100%_done: O'Brien Ω",
        gpt_final="", gemini_final="", synthesized_answer="",
        consensus_score=0, conflict_severity="LOW", conflict_points=[],
    )
    assert len(memory.search_debates("100%_done")) == 1
    assert len(memory.search_debates("O'Brien Ω")) == 1
    assert memory.search_debates("100Xdone") == []
    assert memory.search_debates("")
