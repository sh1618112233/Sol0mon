"""
test_human_decision_manager.py - Phase 2 tests for the build-phase
human decision store.

Verifies:
- save_pending / load_pending round-trip
- resolve moves pending -> resolved with chosen_option + resolved_at
- file format is {"pending": [], "resolved": []}
- missing/empty workspace -> []
- accepts a bundle dict (decision_bundle.generate_bundle()) on save

Run: python3 test_human_decision_manager.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import build_workspace as bw
import human_decision_manager as hdm
from decision_bundle import DecisionBundle


def _with_tmp_root(tmp: str) -> None:
    bw.WORKSPACE_ROOT = Path(tmp)


def _decisions():
    return [
        {"decision_id": "d1", "dispute_key": "db", "dispute_text": "db?",
         "option_a": "PG", "option_b": "SQLite"},
        {"decision_id": "d2", "dispute_key": "cache", "dispute_text": "cache?",
         "option_a": "Redis", "option_b": "Memcached"},
        {"decision_id": "d3", "dispute_key": "queue", "dispute_text": "queue?",
         "option_a": "Kafka", "option_b": "RabbitMQ"},
    ]


def test_save_and_load_pending_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        assert hdm.save_pending(wid, _decisions())
        loaded = hdm.load_pending(wid)
        assert len(loaded) == 3
        assert loaded[0]["decision_id"] == "d1"
        assert loaded[2]["dispute_key"] == "queue"


def test_file_format_pending_resolved() -> None:
    # Store-backed: no flat file, but pending/resolved partition holds.
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        hdm.save_pending(wid, _decisions())
        assert len(hdm.load_pending(wid)) == 3
        assert hdm.load_resolved(wid) == []
        # round-trip: decision_ids preserved
        pending = hdm.load_pending(wid)
        assert {d["decision_id"] for d in pending} == {"d1", "d2", "d3"}


def test_resolve_moves_pending_to_resolved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        hdm.save_pending(wid, _decisions())
        assert hdm.resolve(wid, "d2", "Redis")
        pending = hdm.load_pending(wid)
        resolved = hdm.load_resolved(wid)
        assert len(pending) == 2
        assert len(resolved) == 1
        assert resolved[0]["decision_id"] == "d2"
        assert resolved[0]["chosen_option"] == "Redis"
        assert "resolved_at" in resolved[0]


def test_resolve_missing_returns_false() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        hdm.save_pending(wid, _decisions())
        assert not hdm.resolve(wid, "nope", "x")
        assert len(hdm.load_pending(wid)) == 3


def test_load_pending_missing_workspace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        assert hdm.load_pending("does-not-exist") == []
        assert hdm.load_resolved("does-not-exist") == []


def test_save_accepts_bundle_dict() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        b = DecisionBundle(workspace_id=wid)
        for d in _decisions():
            b.add_decision(d)
        bundle = b.generate_bundle()
        assert hdm.save_pending(wid, bundle)
        assert len(hdm.load_pending(wid)) == 3


def test_three_outcomes_one_persisted_list() -> None:
    # The Phase 2 success criterion at the storage layer: 3 HUMAN_REQUIRED
    # outcomes land as a single pending list (one save), ready for one ask.
    with tempfile.TemporaryDirectory() as tmp:
        _with_tmp_root(tmp)
        wid = bw.create_workspace()
        b = DecisionBundle(workspace_id=wid)
        for d in _decisions():
            b.add_decision(d)
        bundle = b.generate_bundle()
        assert hdm.save_pending(wid, bundle["decisions"])
        pending = hdm.load_pending(wid)
        assert len(pending) == 3
        # decision_ids and dispute_keys round-trip (store adds metadata fields)
        assert {d["decision_id"] for d in pending} == {"d1", "d2", "d3"}
        assert {d["dispute_key"] for d in pending} == {"db", "cache", "queue"}


def test_does_not_touch_protected_logic() -> None:
    protected = {
        "debate_engine", "governance", "verification_engine",
        "consensus_engine", "chat_session", "gpt_worker", "gemini_worker",
    }
    imported = set(getattr(m, "__name__", "") for m in hdm.__dict__.values()
                  if hasattr(m, "__name__"))
    assert not (protected & imported), \
        f"human_decision_manager pulls in protected modules: {protected & imported}"


if __name__ == "__main__":
    test_save_and_load_pending_round_trip()
    test_file_format_pending_resolved()
    test_resolve_moves_pending_to_resolved()
    test_resolve_missing_returns_false()
    test_load_pending_missing_workspace()
    test_save_accepts_bundle_dict()
    test_three_outcomes_one_persisted_list()
    test_does_not_touch_protected_logic()
    print("test_human_decision_manager: ALL PASSED")
