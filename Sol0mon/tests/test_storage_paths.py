from __future__ import annotations

from pathlib import Path

import pytest

import storage_paths


def test_default_data_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    data_root = tmp_path / "persistent"
    monkeypatch.setenv("SOLOMON_DATA_DIR", str(data_root))
    monkeypatch.chdir(tmp_path)
    first = storage_paths.database_path("answers.db")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert storage_paths.database_path("answers.db") == first
    assert Path(first) == data_root / "answers.db"


def test_workspace_and_human_overrides_must_match(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB", str(tmp_path / "workspaces.db"))
    monkeypatch.setenv("HUMAN_DECISION_DB", str(tmp_path / "other.db"))
    with pytest.raises(RuntimeError, match="must point to the same"):
        storage_paths.workspace_database_path()


def test_external_cwd_legacy_store_requires_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "database"
    legacy.mkdir()
    (legacy / "workspaces.db").touch()
    with pytest.raises(RuntimeError, match="Possible legacy"):
        storage_paths.workspace_database_path()


def test_ambiguous_legacy_databases_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setattr(storage_paths, "SOURCE_ROOT", tmp_path / "source")
    monkeypatch.setattr(storage_paths, "REPOSITORY_ROOT", tmp_path / "repo")
    (tmp_path / "source").mkdir()
    (tmp_path / "repo").mkdir()
    (tmp_path / "source" / "answers.db").touch()
    (tmp_path / "repo" / "answers.db").touch()
    with pytest.raises(RuntimeError, match="Multiple legacy"):
        storage_paths.database_path("answers.db")


def test_workspace_database_migration_preserves_column_mapping(tmp_path, monkeypatch):
    import workspace_manager as wm

    db = tmp_path / "legacy.db"
    monkeypatch.setattr(wm, "DB_FILE", str(db))
    saved_initialized = wm.workspace_mgr._initialized
    wm.workspace_mgr._initialized = False
    try:
        ws = wm.Workspace(workspace_id="legacy", original_prompt="old data",
                          status=wm.WorkspaceStatus.COMPLETED,
                          impact_audit_trail=[{"event": "kept"}])
        assert wm.workspace_mgr.save_workspace(ws)
        with wm._get_connection() as conn:
            conn.execute("ALTER TABLE workspaces DROP COLUMN decision_history")
        wm.workspace_mgr._initialized = False
        loaded = wm.workspace_mgr.load_workspace("legacy")
        assert loaded is not None
        assert loaded.status == wm.WorkspaceStatus.COMPLETED
        assert loaded.impact_audit_trail == [{"event": "kept"}]
        assert loaded.decision_history == []
    finally:
        wm.workspace_mgr._initialized = saved_initialized
