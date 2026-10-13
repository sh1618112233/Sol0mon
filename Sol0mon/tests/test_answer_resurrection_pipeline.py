"""
test_answer_resurrection_pipeline.py - TEST 2.

Input:  "Build answer <id>"
Expected: Answer Loader -> Workspace Creation -> Build Prompt -> Build Spec

Verifies Path B: answer selection -> workspace creation.
Verifies Path C: workspace -> build prompt -> build spec -> store.

Seeds a throwaway answer_memory.db so the real DB is untouched.
Mocks the debate step (debate_fn) so no browser is needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Make the project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import answer_memory
import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
from build_prompt_generator import SPEC_SECTIONS


def _with_tmp_db():
    """Point answer_memory at a fresh temp DB. Returns path for cleanup."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _seed_answers(*rows):
    """Seed answers. Returns list of IDs."""
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


def _good_spec_text():
    return (
        "# Architecture\nLayered.\n\n"
        "# Components\nAPI, worker, DB.\n\n"
        "# File Structure\nsrc/api src/worker\n\n"
        "# Dependencies\nfastapi\n\n"
        "# Constraints\nMUST use PostgreSQL.\n\n"
        "# Testing Requirements\npytest\n\n"
        "# Acceptance Criteria\ntests green\n"
    )


def _fake_debate(prompt, memory_ids=None):
    return {
        "status": "completed",
        "synthesized_answer": _good_spec_text(),
        "gpt_final": "pos-g", "gemini_final": "pos-m",
        "validated_disputes": [],
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [],
        "final_consensus_score": 80,
    }


def test_build_answer_id_resurrects_and_creates_workspace() -> None:
    """TEST 2: 'Build answer <id>' -> load answer -> create workspace."""
    db_path = _with_tmp_db()
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    try:
        ids = _seed_answers(
            ("Design Sol0mon architecture", "arch-body", 80),
            ("Build a stat arb bot", "bot-body", 70),
        )
        target = ids[0]  # first answer

        res = bp.resurrect_answer(f"build answer {target}")
        assert "error" not in res
        assert res["answer"]["answer_id"] == target
        wid = res["workspace_id"]
        ws = bw.load_workspace(wid)
        assert ws is not None
        assert ws.source_answer_id == str(target)
        assert ws.source_question == "Design Sol0mon architecture"

        # Synthesized answer written to workspace file
        ans_file = Path(tmp_ws) / f"workspace_{wid}" / "synthesized_answer.md"
        assert ans_file.read_text() == "arch-body"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_resurrect_missing_answer_returns_error() -> None:
    db_path = _with_tmp_db()
    try:
        res = bp.resurrect_answer("build answer 999999")
        assert "error" in res
        assert res["workspace_id"] is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_full_pipeline_build_answer_id_to_build_spec() -> None:
    """Full path: 'Build answer <id>' -> classify -> resurrect -> build spec -> store."""
    db_path = _with_tmp_db()
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    try:
        ids = _seed_answers(("Design Sol0mon", "Use PostgreSQL + FastAPI.", 80))
        target = ids[0]

        result = bp.run_build_pipeline(
            f"Build answer {target}",
            debate_fn=_fake_debate,
        )

        assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
        assert result["answer_resurrected"] is True
        wid = result["workspace_id"]

        # Spec stored inside the workspace
        spec = bse.load_implementation_spec(wid)
        assert spec
        for section in SPEC_SECTIONS:
            assert section in spec.lower()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_full_pipeline_direct_prompt_to_build_spec() -> None:
    """Direct implementation prompt (no answer ref) -> create workspace -> build spec."""
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    try:
        result = bp.run_build_pipeline(
            "Build Project Sol0mon MVP",
            debate_fn=_fake_debate,
        )
        assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
        assert result["answer_resurrected"] is False
        assert result["workspace_id"]
        # Spec should be stored
        spec = bse.load_implementation_spec(result["workspace_id"])
        assert spec  # non-empty
    finally:
        pass  # tempdir cleaned by OS


if __name__ == "__main__":
    test_build_answer_id_resurrects_and_creates_workspace()
    test_resurrect_missing_answer_returns_error()
    test_full_pipeline_build_answer_id_to_build_spec()
    test_full_pipeline_direct_prompt_to_build_spec()
    print("test_answer_resurrection_pipeline: ALL PASSED")
