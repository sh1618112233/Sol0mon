"""
test_build_workspace.py - Phase 1 tests for the Build Foundation layer.

Verifies:
- workspace creation (dir + manifest + default files)
- workspace loading
- workspace persistence (save + reload round-trip)

Run: python3 test_build_workspace.py   (also pytest-compatible)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import build_workspace as bw


def _with_tmp_root(tmp: str) -> None:
    """Point build_workspace at a throwaway root so tests stay isolated."""
    bw.WORKSPACE_ROOT = Path(tmp)


def test_create_workspace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace(source_question="Design project X")
        ws_dir = Path(tmp) / f"workspace_{wid}"

        # All default files exist.
        assert (ws_dir / "workspace.json").is_file()
        for name in ("synthesized_answer.md", "implementation_plan.md",
                    "human_decisions.json", "build_state.json",
                    "answer.json", "decisions.json"):
            assert (ws_dir / name).is_file(), f"missing {name}"

        # Manifest matches the Build Foundation spec.
        manifest = json.loads((ws_dir / "workspace.json").read_text())
        assert manifest["workspace_id"] == wid
        assert manifest["status"] == "created"
        assert manifest["build_spec_version"] == 1
        assert manifest["source_answer_id"] is None
        assert manifest["source_question"] == "Design project X"


def test_load_workspace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        ws = bw.load_workspace(wid)
        assert ws is not None
        assert ws.workspace_id == wid
        assert ws.status == "created"
        assert ws.build_spec_version == 1

        # Missing workspace -> None.
        assert bw.load_workspace("does-not-exist") is None


def test_save_workspace_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        ws = bw.load_workspace(wid)
        assert ws is not None
        ws.source_answer_id = "ans-42"
        ws.status = "synthesizing"
        assert bw.save_workspace(ws)

        # Reload and verify the round-trip survives to disk.
        ws2 = bw.load_workspace(wid)
        assert ws2 is not None
        assert ws2.source_answer_id == "ans-42"
        assert ws2.status == "synthesizing"


def test_does_not_touch_debate_pipeline() -> None:
    # Build workspace is file-based; the debate workspace is SQLite-backed
    # in workspace_manager.py. The build layer here imports none of the
    # protected debate-pipeline modules.
    protected = {
        "debate_engine", "governance", "verification_engine",
        "consensus_engine", "chat_session", "gpt_worker", "gemini_worker",
    }
    imported = set(getattr(m, "__name__", "") for m in bw.__dict__.values()
                   if hasattr(m, "__name__"))
    assert not (protected & imported), \
        f"build_workspace pulls in debate-pipeline modules: {protected & imported}"


if __name__ == "__main__":
    test_create_workspace()
    test_load_workspace()
    test_save_workspace_persistence()
    test_does_not_touch_debate_pipeline()
    print("test_build_workspace: ALL PASSED")
