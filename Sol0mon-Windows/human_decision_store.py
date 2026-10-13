"""
Human Decision Store — single source of truth for human-required decisions.

Replaces the three separate authorities:
  - human_decision_manager (flat-file, build phase)
  - human_decision_queue   (SQLite table, debate phase)
  - Workspace.human_required_items (JSON column, mirror of the queue)

All three are now thin shims that delegate here. One SQLite table, one
authority. No duplicated state, no sync drift.

The store accepts ANY decision dict shape (debate-phase `{question, options,
...}`, build-phase `{dispute_key, dispute_text, option_a, option_b, ...}`).
The full dict is persisted as JSON in the `data` column; indexed columns
(`resolved`, `priority`, `created_at`) support queries without deserializing.

Backward-compatible API:
  create(workspace_id, decision_dict)            -> decision_id (upsert)
  resolve(decision_id, chosen_option)            -> bool
  resolve_by_key(workspace_id, key, chosen_option) -> bool
  get(decision_id)                               -> dict | None
  pending(workspace_id)                          -> list[dict]  (priority-ordered)
  resolved_list(workspace_id)                     -> list[dict]
  all_for_workspace(workspace_id)                -> list[dict]
  pending_count(workspace_id)                    -> int
  pending_all_workspaces()                       -> list[dict]
  save_pending(workspace_id, decisions)          -> bool (replace pending set)
  delete(decision_id)                            -> bool
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid as _uuid
from datetime import datetime, timezone

DB_FILE = os.environ.get("HUMAN_DECISION_DB", "database/workspaces.db")


def _get_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decision_key(d: dict) -> str:
    """Stable identity for a decision, for dedup/match. Falls back through
    alternate key names so callers don't have to normalize."""
    return (d.get("decision_id")
            or d.get("dispute_key")
            or d.get("normalized_key")
            or d.get("dispute_text")
            or d.get("question")
            or "")


def _semantic_key(d: dict) -> str:
    """Semantic key WITHOUT the decision_id fallback — used for upsert
    so an auto-generated UUID doesn't shadow the dispute_key."""
    return (d.get("dispute_key")
            or d.get("normalized_key")
            or d.get("dispute_text")
            or d.get("question")
            or "")


class HumanDecisionStore:
    """Single source of truth for human-required decisions.

    All persistence goes through this class. The old modules
    (human_decision_manager, human_decision_queue) are backward-compat shims
    that delegate here. Workspace.human_required_items is a read view.
    """

    def __init__(self):
        pass

    def _ensure_initialized(self) -> None:
        # ponytail: always run CREATE TABLE IF NOT EXISTS — idempotent, cheap,
        # and immune to DB file deletion/recreation across test files.
        self._initialize_database()

    def _initialize_database(self) -> None:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS unified_human_decisions (
            decision_id  TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            data         TEXT NOT NULL DEFAULT '{}',
            resolved     INTEGER NOT NULL DEFAULT 0,
            chosen_option TEXT NOT NULL DEFAULT '',
            resolved_at  TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            priority     TEXT NOT NULL DEFAULT 'MEDIUM',
            PRIMARY KEY (workspace_id, decision_id)
        )
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_uhd_workspace
        ON unified_human_decisions(workspace_id)
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_uhd_resolved
        ON unified_human_decisions(resolved)
        """)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Create / upsert
    # ------------------------------------------------------------------

    def create(self, workspace_id: str, decision: dict,
               decision_id: str | None = None) -> str:
        """Insert or update a decision. If decision_id (or stable key) already
        exists for this workspace, merge the data. Returns the decision_id."""
        self._ensure_initialized()
        d = dict(decision) if isinstance(decision, dict) else {}
        d.setdefault("workspace_id", workspace_id)
        d.setdefault("resolved", False)

        did = decision_id or d.get("decision_id") or ""
        key = _semantic_key(d)

        # Check for existing by decision_id or stable key
        existing_id = self._find_existing(workspace_id, did, key)

        if existing_id:
            # Upsert: merge the new data over the existing record
            existing = self._load_row(existing_id)
            if existing:
                existing_data = json.loads(existing["data"]) if existing["data"] else {}
                existing_data.update(d)
                d = existing_data
                did = existing_id
                # Preserve resolved state and created_at
                d.setdefault("resolved", bool(existing["resolved"]))
                d.setdefault("created_at", existing["created_at"])
            else:
                did = existing_id

        if not did:
            did = str(_uuid.uuid4())
        d["decision_id"] = did
        if "created_at" not in d or not d["created_at"]:
            d["created_at"] = _now()
        priority = d.get("priority", "MEDIUM")

        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO unified_human_decisions (
            decision_id, workspace_id, data, resolved, chosen_option,
            resolved_at, created_at, priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, decision_id) DO UPDATE SET
            data = excluded.data,
            priority = excluded.priority
        """, (
            did, workspace_id, json.dumps(d),
            1 if d.get("resolved") else 0,
            d.get("chosen_option", ""),
            d.get("resolved_at", ""),
            d.get("created_at", _now()),
            priority,
        ))
        conn.commit()
        conn.close()
        return did

    def _find_existing(self, workspace_id: str, decision_id: str,
                       key: str) -> str | None:
        if decision_id:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute("""
            SELECT decision_id FROM unified_human_decisions
            WHERE workspace_id = ? AND decision_id = ?
            """, (workspace_id, decision_id))
            row = cursor.fetchone()
            conn.close()
            if row:
                return row[0]
        if key:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute("""
            SELECT decision_id, data FROM unified_human_decisions
            WHERE workspace_id = ? AND resolved = 0
            """, (workspace_id,))
            for did_row, data_str in cursor.fetchall():
                existing_data = json.loads(data_str) if data_str else {}
                if _semantic_key(existing_data) == key:
                    conn.close()
                    return did_row
            conn.close()
        return None

    def _load_row(self, decision_id: str,
                  workspace_id: str | None = None) -> dict | None:
        conn = _get_connection()
        cursor = conn.cursor()
        if workspace_id:
            cursor.execute("""
            SELECT decision_id, workspace_id, data, resolved, chosen_option,
                   resolved_at, created_at, priority
            FROM unified_human_decisions
            WHERE workspace_id = ? AND decision_id = ?
            """, (workspace_id, decision_id))
        else:
            cursor.execute("""
            SELECT decision_id, workspace_id, data, resolved, chosen_option,
                   resolved_at, created_at, priority
            FROM unified_human_decisions WHERE decision_id = ?
            """, (decision_id,))
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "decision_id": row[0], "workspace_id": row[1], "data": row[2],
            "resolved": row[3], "chosen_option": row[4], "resolved_at": row[5],
            "created_at": row[6], "priority": row[7],
        }

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    def resolve(self, decision_id: str, chosen_option: str,
                workspace_id: str | None = None) -> bool:
        """Resolve by decision_id. Returns False if not found or already resolved.

        If workspace_id is given, scopes the lookup to that workspace (needed
        when decision IDs are reused across workspaces, e.g. build-phase 'd1')."""
        self._ensure_initialized()
        row = self._load_row(decision_id, workspace_id)
        if row is None or row["resolved"]:
            return False
        data = json.loads(row["data"]) if row["data"] else {}
        data["resolved"] = True
        data["chosen_option"] = chosen_option
        data["resolved_at"] = _now()
        conn = _get_connection()
        cursor = conn.cursor()
        if workspace_id:
            cursor.execute("""
            UPDATE unified_human_decisions
            SET resolved = 1, chosen_option = ?, resolved_at = ?, data = ?
            WHERE workspace_id = ? AND decision_id = ? AND resolved = 0
            """, (chosen_option, data["resolved_at"], json.dumps(data),
                  workspace_id, decision_id))
        else:
            cursor.execute("""
            UPDATE unified_human_decisions
            SET resolved = 1, chosen_option = ?, resolved_at = ?, data = ?
            WHERE decision_id = ? AND resolved = 0
            """, (chosen_option, data["resolved_at"], json.dumps(data),
                  decision_id))
        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return updated

    def resolve_by_key(self, workspace_id: str, key: str,
                       chosen_option: str) -> bool:
        """Resolve by stable decision key (dispute_key/normalized_key/
        dispute_text/question). Returns False if not found."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT decision_id, data FROM unified_human_decisions
        WHERE workspace_id = ? AND resolved = 0
        """, (workspace_id,))
        for did, data_str in cursor.fetchall():
            d = json.loads(data_str) if data_str else {}
            if _semantic_key(d) == key or _decision_key(d) == key:
                conn.close()
                return self.resolve(did, chosen_option)
        conn.close()
        return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, decision_id: str,
            workspace_id: str | None = None) -> dict | None:
        """Get a single decision by ID. Returns the full decision dict or None."""
        self._ensure_initialized()
        row = self._load_row(decision_id, workspace_id)
        if row is None:
            return None
        data = json.loads(row["data"]) if row["data"] else {}
        data["resolved"] = bool(row["resolved"])
        data["chosen_option"] = row["chosen_option"]
        data["resolved_at"] = row["resolved_at"]
        return data

    def pending(self, workspace_id: str) -> list[dict]:
        """Get pending decisions, priority-ordered (HIGH > MEDIUM > LOW)."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT data FROM unified_human_decisions
        WHERE workspace_id = ? AND resolved = 0
        ORDER BY
            CASE priority
                WHEN 'HIGH' THEN 1
                WHEN 'MEDIUM' THEN 2
                WHEN 'LOW' THEN 3
            END,
            created_at ASC
        """, (workspace_id,))
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]

    def resolved_list(self, workspace_id: str) -> list[dict]:
        """Get resolved decisions for a workspace."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT data FROM unified_human_decisions
        WHERE workspace_id = ? AND resolved = 1
        ORDER BY resolved_at ASC
        """, (workspace_id,))
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]

    def all_for_workspace(self, workspace_id: str) -> list[dict]:
        """Get all decisions (pending + resolved) for a workspace."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT data FROM unified_human_decisions
        WHERE workspace_id = ?
        ORDER BY created_at ASC
        """, (workspace_id,))
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]

    def pending_count(self, workspace_id: str) -> int:
        """Count pending decisions for a workspace."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT COUNT(*) FROM unified_human_decisions
        WHERE workspace_id = ? AND resolved = 0
        """, (workspace_id,))
        count = cursor.fetchone()[0] or 0
        conn.close()
        return count

    def pending_all_workspaces(self) -> list[dict]:
        """Get all pending decisions across all workspaces, priority-ordered."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT data FROM unified_human_decisions
        WHERE resolved = 0
        ORDER BY
            CASE priority
                WHEN 'HIGH' THEN 1
                WHEN 'MEDIUM' THEN 2
                WHEN 'LOW' THEN 3
            END,
            created_at ASC
        """)
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]

    # ------------------------------------------------------------------
    # Build-phase API: replace the full pending set
    # ------------------------------------------------------------------

    def save_pending(self, workspace_id: str, decisions) -> bool:
        """Replace the pending set for a workspace. Accepts a list of decision
        dicts or a bundle dict with a 'decisions' key. Resolved decisions are
        preserved. Returns True on success."""
        self._ensure_initialized()
        if isinstance(decisions, dict) and "decisions" in decisions:
            decisions = decisions["decisions"]
        if not isinstance(decisions, list):
            decisions = []

        conn = _get_connection()
        cursor = conn.cursor()
        # Delete existing PENDING rows for this workspace (keep resolved)
        cursor.execute("""
        DELETE FROM unified_human_decisions
        WHERE workspace_id = ? AND resolved = 0
        """, (workspace_id,))
        conn.commit()
        conn.close()

        for d in decisions:
            if not isinstance(d, dict):
                continue
            self.create(workspace_id, d)
        return True

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, decision_id: str,
               workspace_id: str | None = None) -> bool:
        """Remove a decision from the store."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        if workspace_id:
            cursor.execute("""
            DELETE FROM unified_human_decisions
            WHERE workspace_id = ? AND decision_id = ?
            """, (workspace_id, decision_id))
        else:
            cursor.execute("""
            DELETE FROM unified_human_decisions WHERE decision_id = ?
            """, (decision_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

store = HumanDecisionStore()


if __name__ == "__main__":
    # Self-check: create, resolve, persistence, no duplicates
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HUMAN_DECISION_DB"] = f"{tmp}/test.db"
        import importlib
        import human_decision_store as _hds
        importlib.reload(_hds)
        s = _hds.HumanDecisionStore()

        did = s.create("ws1", {"question": "DB?", "options": ["PG", "SQLite"],
                               "dispute_key": "db", "priority": "HIGH"})
        assert did
        assert len(s.pending("ws1")) == 1
        assert s.pending_count("ws1") == 1

        # upsert by key — no duplicate
        did2 = s.create("ws1", {"question": "DB?", "dispute_key": "db"})
        assert did2 == did, f"upsert should return same id: {did} vs {did2}"
        assert len(s.pending("ws1")) == 1, "duplicate created!"

        # resolve
        assert s.resolve(did, "PG")
        assert len(s.pending("ws1")) == 0
        assert len(s.resolved_list("ws1")) == 1
        assert s.get(did)["chosen_option"] == "PG"

        # resolve by key
        did3 = s.create("ws1", {"dispute_key": "cache", "question": "Cache?"})
        assert s.resolve_by_key("ws1", "cache", "Redis")
        assert s.get(did3)["chosen_option"] == "Redis"

        # save_pending replaces
        s.save_pending("ws1", [{"dispute_key": "k8s", "question": "K8s?"}])
        assert len(s.pending("ws1")) == 1
        assert s.pending("ws1")[0]["dispute_key"] == "k8s"
        # resolved preserved
        assert len(s.resolved_list("ws1")) == 2

        print("human_decision_store self-check PASSED")
