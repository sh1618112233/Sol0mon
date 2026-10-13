"""
test_resurrection_constraints.py - TEST 2.

Verifies FIX 2 + FIX 4 + workspace output:
  - resurrect_answer carries the decision model (source_result never None).
  - Every resurrected workspace contains decisions.json + answer.json.
  - Verified decisions reach the build prompt as immutable constraints.

Seeds a throwaway answer_memory.db so the real DB is untouched.
Mocks the debate step so no browser/LLM is needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import answer_memory
import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
from build_prompt_generator import SPEC_SECTIONS


def _with_tmp_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def _verification_result_2_disputes():
    return {
        "resolved_claims": [
            {"dispute": "Primary DB: SQLite vs IndexedDB",
             "normalized_key": "primary_database",
             "verification": {"status": "RESOLVED", "confidence": 95,
                             "finding": "Resolved in favor of server-side authoritative storage sqlite; rejects indexeddb.",
                             "requires_external_evidence": False}},
            {"dispute": "Execution: Docker vs WebContainers",
             "normalized_key": "execution_environment",
             "verification": {"status": "RESOLVED", "confidence": 99,
                             "finding": "Resolved in favor of server-side docker workers; rejects webcontainers.",
                             "requires_external_evidence": False}},
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }, [
        {"normalized_key": "primary_database", "dispute": "Primary DB: SQLite vs IndexedDB",
         "status": "resolved", "outcome_type": "", "governed_outcome": None},
        {"normalized_key": "execution_environment", "dispute": "Execution: Docker vs WebContainers",
         "status": "resolved", "outcome_type": "", "governed_outcome": None},
    ]


def _seed_answer_with_decisions():
    vr, vd = _verification_result_2_disputes()
    debate_id = answer_memory.memory.save_debate(
        question="Design Sol0mon architecture",
        gpt_final="gpt", gemini_final="gem",
        synthesized_answer="Server-side SQLite + Docker workers.",
        consensus_score=96, conflict_severity="LOW",
        conflict_points=[d["normalized_key"] for d in vd],
        validated_disputes=vd, verification_result=vr,
    )
    return debate_id


def _good_spec_text():
    return (
        "# Architecture\nServer-side SQLite + Docker workers.\n\n"
        "# Components\napi, worker, sqlite, docker.\n\n"
        "# File Structure\nsrc/api src/worker\n\n"
        "# Dependencies\nfastapi, sqlite, docker.\n\n"
        "# Constraints\nMUST use sqlite primary; MUST use docker workers.\n\n"
        "# Testing Requirements\npytest\n\n"
        "# Acceptance Criteria\ntests green\n"
    )


def _fake_debate_factory():
    def _debate(prompt, memory_ids=None):
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
        }
    return _debate


def test_resurrect_answer_returns_source_result_not_none() -> None:
    """FIX 4: resurrect_answer passes the actual decision model;
    source_result is never None during resurrection."""
    db_path = _with_tmp_db()
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    try:
        aid = _seed_answer_with_decisions()
        res = bp.resurrect_answer(f"build answer {aid}")
        assert "error" not in res
        assert "source_result" in res, "resurrect_answer must return source_result"
        sr = res["source_result"]
        assert sr is not None, "source_result must never be None during resurrection"
        # The bucket the build extractor reads is populated.
        assert len(sr.get("verified_decisions", [])) == 2
        assert sr.get("verification_result", {}).get("verification_score") == 100
        assert len(sr.get("resolved_decisions", [])) == 2
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_workspace_contains_answer_and_decisions_json() -> None:
    """WORKSPACE OUTPUT: every resurrected workspace has answer.json +
    decisions.json (authoritative build input).

    Audit fix #4: content-validate build_state.json + implementation_plan.md
    rather than asserting mere existence. After resurrect_answer alone (no
    spec generation yet), the plan placeholder is intentionally empty and
    build_state.json carries the 'resurrected' stage (audit fix #3).
    """
    db_path = _with_tmp_db()
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    try:
        aid = _seed_answer_with_decisions()
        res = bp.resurrect_answer(f"build answer {aid}")
        wid = res["workspace_id"]
        wsdir = Path(tmp_ws) / f"workspace_{wid}"

        assert (wsdir / "answer.json").is_file()
        assert (wsdir / "decisions.json").is_file()

        # Audit fix #4: implementation_plan.md content check (not just is_file).
        # At resurrection time the spec has NOT been generated, so the file
        # carries its empty placeholder. The check now pins the truthful
        # post-resurrection content: an empty string, exactly the placeholder
        # laid down by build_workspace.DEFAULT_FILES.
        assert (wsdir / "implementation_plan.md").read_text() == ""

        # Audit fix #3 + #4: build_state.json content check (not just is_file).
        # resurrect_answer now persists the 'resurrected' stage, so the file
        # is no longer the stale "{}" placeholder.
        state = json.loads((wsdir / "build_state.json").read_text())
        assert state["status"] == "resurrected"
        assert state["workspace_id"] == wid
        assert state.get("source_answer_id") == str(aid)

        ans = json.loads((wsdir / "answer.json").read_text())
        assert ans["answer_id"] == aid
        assert "verification_result" in ans
        assert "verified_decisions" in ans

        decisions = json.loads((wsdir / "decisions.json").read_text())
        assert len(decisions["verified_decisions"]) == 2
        assert len(decisions["resolved_decisions"]) == 2
        assert decisions["verification_result"]["verification_score"] == 100
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_verified_decisions_reach_build_prompt_as_constraints() -> None:
    """FIX 5: verified decisions are consumed as immutable constraints in the
    build prompt (the enforcement point)."""
    db_path = _with_tmp_db()
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    captured_prompt = {}

    def _debate(prompt, memory_ids=None):
        captured_prompt["prompt"] = prompt
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "g", "gemini_final": "m", "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [], "final_consensus_score": 90,
        }

    try:
        aid = _seed_answer_with_decisions()
        result = bp.run_build_pipeline(f"build answer {aid}", debate_fn=_debate)
        assert result["answer_resurrected"] is True
        prompt = captured_prompt["prompt"].lower()
        # Constraints section is populated (not "(none)").
        assert "## constraints (immutable)" in prompt
        assert "(none)" not in prompt.split("## constraints (immutable)")[1].split("##")[0]
        # Both verified decisions appear as MUST constraints (finding text).
        assert "sqlite" in prompt
        assert "docker" in prompt
        # The rejected options are recorded in the decision reasoning so the
        # implementation debate sees them as off-limits.
        assert "indexeddb" in prompt or "rejects" in prompt
        assert "webcontainers" in prompt or "rejects" in prompt
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_build_spec_surfaces_verified_decisions_and_gaps() -> None:
    """build_spec returns verified_decisions + decision_gaps diagnostic."""
    db_path = _with_tmp_db()
    tmp_ws = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp_ws)
    try:
        aid = _seed_answer_with_decisions()
        res = bp.resurrect_answer(f"build answer {aid}")

        def _debate(prompt, memory_ids=None):
            return {"status": "completed",
                    "synthesized_answer": _good_spec_text(),
                    "gpt_final": "g", "gemini_final": "m", "validated_disputes": [],
                    "verified_decisions": [], "verified_tradeoffs": [],
                    "human_required": [], "final_consensus_score": 90}

        out = bse.build_spec(
            res["answer"]["synthesized_answer"],
            debate_fn=_debate,
            source_result=res["source_result"],
        )
        # FIX 5: verified_decisions carried into the spec result.
        assert len(out["verified_decisions"]) == 2
        # The honoring spec references both decisions -> no gaps.
        assert out["decision_gaps"] == []
        assert out["status"] == "completed"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    test_resurrect_answer_returns_source_result_not_none()
    test_workspace_contains_answer_and_decisions_json()
    test_verified_decisions_reach_build_prompt_as_constraints()
    test_build_spec_surfaces_verified_decisions_and_gaps()
    print("test_resurrection_constraints: ALL PASSED")
