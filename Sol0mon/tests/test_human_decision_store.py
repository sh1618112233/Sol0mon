"""
test_human_decision_store.py - tests for the unified HumanDecisionStore.

Covers the 7 spec items:
  1. create + get (upsert by id)
  2. resolve (pending -> resolved, chosen_option, resolved_at)
  3. persistence across store instances (survives process boundary)
  4. resume: pending survives a fresh load
  5. workspace integration (Workspace.pending_human_items / add_human_required /
     resolve_human_item delegate to the store)
  6. no duplicated state (upsert by semantic key; save_pending replaces, not appends)
  7. backward-compat shims (human_decision_manager + human_decision_queue delegate)

All offline: no model/browser/API calls. Each test points the store at a
fresh temp DB and resets the singleton so state never leaks between tests.

Run: python3 -m pytest tests/test_human_decision_store.py -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import human_decision_store as hds
import human_decision_manager as hdm
import human_decision_queue as hdq


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Point the store at a fresh temp DB."""
    db_path = str(tmp_path / "test_decisions.db")
    monkeypatch.setattr(hds, "DB_FILE", db_path)
    return hds.store


# ---------------------------------------------------------------------------
# 1. create + get
# ---------------------------------------------------------------------------

def test_create_and_get(fresh_store):
    did = fresh_store.create("ws1", {
        "question": "DB?", "dispute_key": "db",
        "options": ["PG", "SQLite"], "priority": "HIGH",
    })
    assert did
    got = fresh_store.get(did)
    assert got is not None
    assert got["question"] == "DB?"
    assert got["dispute_key"] == "db"
    assert got["resolved"] is False
    # workspace_id stamped by the store
    assert got["workspace_id"] == "ws1"
    # pending list reflects it
    assert len(fresh_store.pending("ws1")) == 1
    assert fresh_store.pending_count("ws1") == 1


# ---------------------------------------------------------------------------
# 2. resolve
# ---------------------------------------------------------------------------

def test_resolve(fresh_store):
    did = fresh_store.create("ws1", {
        "dispute_key": "cache", "question": "Cache?",
        "option_a": "Redis", "option_b": "Memcached",
    })
    assert fresh_store.resolve(did, "Redis")
    assert fresh_store.pending("ws1") == []
    resolved = fresh_store.resolved_list("ws1")
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "Redis"
    assert resolved[0]["resolved"] is True
    assert "resolved_at" in resolved[0]
    # re-resolve is a no-op
    assert fresh_store.resolve(did, "Redis") is False


# ---------------------------------------------------------------------------
# 3. persistence across store instances
# ---------------------------------------------------------------------------

def test_persistence_across_instances(fresh_store, tmp_path, monkeypatch):
    did = fresh_store.create("ws1", {"dispute_key": "queue", "question": "Queue?"})
    assert fresh_store.resolve(did, "Kafka")
    # Simulate a new process: fresh load from the same DB file
    resumed = hds.store
    assert len(resumed.pending("ws1")) == 0
    resolved = resumed.resolved_list("ws1")
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "Kafka"
    got = resumed.get(did)
    assert got is not None and got["dispute_key"] == "queue"


# ---------------------------------------------------------------------------
# 4. resume: pending survives a fresh load
# ---------------------------------------------------------------------------

def test_resume_pending_survives(fresh_store):
    decisions = [
        {"decision_id": "d1", "dispute_key": "db", "question": "DB?"},
        {"decision_id": "d2", "dispute_key": "cache", "question": "Cache?"},
    ]
    assert fresh_store.save_pending("ws1", decisions)
    # Simulate resume: re-load pending state
    resumed = fresh_store.pending("ws1")
    assert len(resumed) == 2
    assert {d["decision_id"] for d in resumed} == {"d1", "d2"}
    # all_for_workspace sees pending + resolved
    assert len(fresh_store.all_for_workspace("ws1")) == 2


# ---------------------------------------------------------------------------
# 5. workspace integration
# ---------------------------------------------------------------------------

def test_workspace_integration(fresh_store, tmp_path, monkeypatch):
    import build_workspace as bw
    monkeypatch.setattr(bw, "WORKSPACE_ROOT", Path(tmp_path))
    from workspace_manager import Workspace
    ws = Workspace(workspace_id="ws_int", original_prompt="test")

    # pending_human_items reads from the store
    assert ws.pending_human_items == []

    # add_human_required delegates to store.create
    ws.add_human_required({
        "decision_id": "h1", "dispute_key": "k8s", "question": "K8s?",
    })
    pending = ws.pending_human_items
    assert len(pending) == 1
    assert pending[0]["dispute_key"] == "k8s"

    # resolve_human_item delegates to store.resolve_by_key
    assert ws.resolve_human_item("k8s", "Nomad") is True
    assert ws.pending_human_items == []
    resolved = fresh_store.resolved_list("ws_int")
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "Nomad"


# ---------------------------------------------------------------------------
# 6. no duplicated state
# ---------------------------------------------------------------------------

def test_no_duplicated_state(fresh_store):
    # upsert by semantic key: same dispute_key -> one row
    did1 = fresh_store.create("ws1", {"dispute_key": "db", "question": "DB?"})
    did2 = fresh_store.create("ws1", {"dispute_key": "db", "question": "DB?"})
    assert did1 == did2, "upsert should return same id"
    assert len(fresh_store.pending("ws1")) == 1

    # save_pending replaces the pending set, does not append
    fresh_store.save_pending("ws1", [
        {"dispute_key": "cache", "question": "Cache?"},
        {"dispute_key": "queue", "question": "Queue?"},
    ])
    pending = fresh_store.pending("ws1")
    assert len(pending) == 2
    assert {d["dispute_key"] for d in pending} == {"cache", "queue"}
    # the old 'db' decision is gone from pending (save_pending replaces)
    assert "db" not in {d["dispute_key"] for d in pending}

    # resolved decisions are preserved across save_pending
    did_r = fresh_store.create("ws1", {"dispute_key": "resolved_thing"})
    fresh_store.resolve(did_r, "yes")
    fresh_store.save_pending("ws1", [{"dispute_key": "new_pending"}])
    assert len(fresh_store.resolved_list("ws1")) == 1


# ---------------------------------------------------------------------------
# 7. backward-compat shims
# ---------------------------------------------------------------------------

def test_backward_compat_shims(fresh_store, tmp_path, monkeypatch):
    # human_decision_manager shim
    decisions = [
        {"decision_id": "d1", "dispute_key": "db", "dispute_text": "db?",
         "option_a": "PG", "option_b": "SQLite"},
        {"decision_id": "d2", "dispute_key": "cache", "dispute_text": "cache?",
         "option_a": "Redis", "option_b": "Memcached"},
    ]
    assert hdm.save_pending("ws1", decisions)
    loaded = hdm.load_pending("ws1")
    assert len(loaded) == 2
    assert {d["decision_id"] for d in loaded} == {"d1", "d2"}
    assert hdm.resolve("ws1", "d1", "PG") is True
    assert len(hdm.load_pending("ws1")) == 1
    resolved = hdm.load_resolved("ws1")
    assert len(resolved) == 1 and resolved[0]["chosen_option"] == "PG"

    # bundle-dict input path
    bundle = {"decisions": [{"decision_id": "d3", "dispute_key": "k8s"}],
              "summary": "x", "decision_count": 1}
    assert hdm.save_pending("ws2", bundle)
    assert len(hdm.load_pending("ws2")) == 1

    # human_decision_queue shim
    qid = hdq.enqueue_human_decision(
        "ws3", "Queue?", ["Kafka", "RabbitMQ"],
        reasoning="ops preference", affected_disputes=["queue"],
        priority="HIGH",
    )
    pending = hdq.get_pending_for_workspace("ws3")
    assert len(pending) == 1 and pending[0]["question"] == "Queue?"
    assert hdq.resolve_human_decision(qid, "Kafka") is True
    assert hdq.get_pending_for_workspace("ws3") == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
