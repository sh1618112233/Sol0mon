from __future__ import annotations

import json

import answer_memory
import build_workspace
import human_decision_store
import sol0mon_cli
import workspace_manager
import build_pipeline


def test_cli_resolves_build_workspace_choice_and_regenerates_spec(
    tmp_path, monkeypatch, capsys,
):
    data = tmp_path / "data"
    data.mkdir()
    db = str(data / "shared.db")
    monkeypatch.setattr(human_decision_store, "DB_FILE", db)
    monkeypatch.setattr(workspace_manager, "DB_FILE", db)
    monkeypatch.setattr(answer_memory, "DB_FILE", str(data / "answers.db"))
    monkeypatch.setattr(build_workspace, "WORKSPACE_ROOT", tmp_path / "workspaces")
    human_decision_store.store._initialized = False
    answer_id = answer_memory.memory.save_debate(
        question="Choose a database", gpt_final="position A",
        gemini_final="position B", synthesized_answer="Build the persistence layer.",
        consensus_score=45, conflict_severity="LOW", conflict_points=["database_choice"],
        validated_disputes=[{"normalized_key": "database_choice",
                             "dispute": "SQLite vs PostgreSQL"}],
        verification_result={
            "resolved_claims": [],
            "unresolved_claims": [{"normalized_key": "database_choice",
                                   "dispute": "SQLite vs PostgreSQL"}],
            "governance_result": {
                "verified_decisions": [], "verified_tradeoffs": [],
                "human_required": [{"outcome_type": "HUMAN_REQUIRED",
                                    "dispute_key": "database_choice",
                                    "dispute_text": "SQLite vs PostgreSQL",
                                    "option_a": "SQLite", "option_b": "PostgreSQL",
                                    "reasoning": "Both satisfy current requirements."}],
            },
        },
    )
    started = build_pipeline.run_build_pipeline(f"Build answer {answer_id}")
    assert started.get("workspace_id")
    wid = started["workspace_id"]
    pending = human_decision_store.store.pending(wid)
    assert len(pending) == 1
    decision_id = pending[0]["decision_id"]
    folder = build_workspace._workspace_dir(wid)

    assert sol0mon_cli.main([
        "human", "resolve", wid, decision_id, "SQLite",
    ]) == 0
    capsys.readouterr()

    saved = json.loads((folder / "answer.json").read_text(encoding="utf-8"))
    assert saved["human_decisions_resolved"][0]["chosen_option"] == "SQLite"
    assert human_decision_store.store.pending(wid) == []
    spec = (folder / "implementation_plan.md").read_text(encoding="utf-8")
    assert "Explicit human choice" in spec
    assert "SQLite" in spec
    state = json.loads((folder / "build_state.json").read_text(encoding="utf-8"))
    assert state["execution_decision"] == "IMPLEMENTATION_REQUIRED"
