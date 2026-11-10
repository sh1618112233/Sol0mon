"""
Human Decision Manager — backward-compat shim over HumanDecisionStore.

Previously a flat-file store (workspaces/workspace_<id>/human_decisions.json).
Now delegates to the unified HumanDecisionStore (single SQLite source of truth).
The public API (save_pending, load_pending, load_resolved, resolve) is unchanged.
"""

from __future__ import annotations

from human_decision_store import store, _semantic_key


def save_pending(workspace_id: str, decisions) -> bool:
    """Write the given decisions as the workspace's pending list. Accepts a
    list of decision dicts or a bundle dict. Delegates to the unified store."""
    return store.save_pending(workspace_id, decisions)


def load_pending(workspace_id: str) -> list:
    """Return the pending decision list for the workspace ([] if absent)."""
    return store.pending(workspace_id)


def load_resolved(workspace_id: str) -> list:
    """Return the resolved decision list for the workspace ([] if absent)."""
    return store.resolved_list(workspace_id)


def resolve(workspace_id: str, decision_id: str, choice: str) -> bool:
    """Move one pending decision to resolved, recording the user's choice.

    Matches by decision_id first, then by the stable decision key
    (dispute_key/normalized_key/dispute_text/question). Returns False if not
    found.
    """
    # Try direct decision_id match first (scoped to this workspace)
    if store.resolve(decision_id, choice, workspace_id):
        return True
    # Fall back to semantic key match
    return store.resolve_by_key(workspace_id, decision_id, choice)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    import build_workspace as bw

    with tempfile.TemporaryDirectory() as tmp:
        bw.WORKSPACE_ROOT = Path(tmp)
        wid = bw.create_workspace()
        decisions = [
            {"decision_id": "d1", "dispute_key": "db", "dispute_text": "db?",
             "option_a": "PG", "option_b": "SQLite"},
            {"decision_id": "d2", "dispute_key": "cache", "dispute_text": "cache?",
             "option_a": "Redis", "option_b": "Memcached"},
        ]
        assert save_pending(wid, decisions)
        loaded = load_pending(wid)
        assert len(loaded) == 2 and loaded[0]["decision_id"] == "d1"
        assert resolve(wid, "d1", "PG")
        assert len(load_pending(wid)) == 1
        resolved = load_resolved(wid)
        assert len(resolved) == 1 and resolved[0]["chosen_option"] == "PG"
        assert not resolve(wid, "nope", "x")
        # bundle-dict input path
        from decision_bundle import DecisionBundle
        b = DecisionBundle(workspace_id=wid)
        b.add_decision(decisions[1])
        assert save_pending(wid, b.generate_bundle())
        assert len(load_pending(wid)) == 1
        print("human_decision_manager smoke test PASSED")
