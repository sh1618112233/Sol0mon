"""
Workspace Manager — persistent workspace-based consensus assistant.

Transforms Autonomous Architect from a batch consensus pipeline into a
workspace-based assistant where architectural context persists across
multiple interactions.

A workspace is the primary entity, replacing the ephemeral "job" model.
Workspaces survive process restarts and support:
- Follow-up questions without rerunning the entire pipeline
- Partial debate resumption (only affected disputes revisited)
- Dispute reopening
- Human-required question queuing and resolution
- Verified tradeoff persistence
- Answer linking with parent-child relationships
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Database path — configurable, but defaults to same directory as job_store
# ---------------------------------------------------------------------------
DB_FILE = os.environ.get("WORKSPACE_DB", "database/workspaces.db")


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the workspace database with WAL journal mode."""
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# JSON helpers — safe serialize/deserialize with reasonable defaults
# ---------------------------------------------------------------------------

def _safe_json_dump(obj: Any, default: Any = None) -> str:
    """Serialize to JSON. Returns the serialized default if anything fails."""
    if obj is None:
        return json.dumps(default if default is not None else {})
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return json.dumps(default if default is not None else {})


def _safe_json_load(text: str | None, default: Any = None) -> Any:
    """Deserialize from JSON. Returns default if anything fails."""
    if text is None:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Workspace status constants
# ---------------------------------------------------------------------------

class WorkspaceStatus:
    CREATED = "created"
    DEBATING = "debating"
    CONSENSUS_REACHED = "consensus_reached"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Workspace dataclass — in-memory representation
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    """Persistent workspace holding all architectural context across interactions."""

    workspace_id: str
    original_prompt: str

    # Core architectural state
    verified_decisions: list[dict] = field(default_factory=list)
    verified_tradeoffs: list[dict] = field(default_factory=list)
    human_required_items: list[dict] = field(default_factory=list)
    architecture_constraints: list[dict] = field(default_factory=list)

    # Debate state
    consensus_history: list[dict] = field(default_factory=list)
    active_disputes: list[dict] = field(default_factory=list)
    resolved_disputes: list[dict] = field(default_factory=list)

    # Current positions (from GPT/Gemini)
    gpt_position: str = ""
    gemini_position: str = ""

    # Answer history — list of {answer_id, parent_answer_id, question, timestamp}
    answer_history: list[dict] = field(default_factory=list)

    # Impact-analysis audit trail — per follow-up: {follow_up_type,
    # affected_dispute_count, reopened_disputes, preserved_disputes, ...}.
    # Survives resume/resurrection/chaining via this persisted column.
    impact_audit_trail: list[dict] = field(default_factory=list)

    # Pre-debate research (V1): topic-keyed evidence for reuse across
    # follow-ups + per-run audit trail (research_plan/topics/sources/reuse).
    research_evidence: list[dict] = field(default_factory=list)
    research_audit_trail: list[dict] = field(default_factory=list)

    # Metadata
    status: str = WorkspaceStatus.CREATED
    current_job_id: str = ""
    consensus_score: int = 0
    created_at: str = ""
    updated_at: str = ""

    # Failure tracking
    error: str = ""
    retry_count: int = 0

    def to_dict(self) -> dict:
        """Full serialization for storage."""
        return {
            "workspace_id": self.workspace_id,
            "original_prompt": self.original_prompt,
            "verified_decisions": self.verified_decisions,
            "verified_tradeoffs": self.verified_tradeoffs,
            "human_required_items": self.human_required_items,
            "architecture_constraints": self.architecture_constraints,
            "consensus_history": self.consensus_history,
            "active_disputes": self.active_disputes,
            "resolved_disputes": self.resolved_disputes,
            "gpt_position": self.gpt_position,
            "gemini_position": self.gemini_position,
            "answer_history": self.answer_history,
            "impact_audit_trail": self.impact_audit_trail,
            "research_evidence": self.research_evidence,
            "research_audit_trail": self.research_audit_trail,
            "status": self.status,
            "current_job_id": self.current_job_id,
            "consensus_score": self.consensus_score,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Workspace":
        """Reconstruct from stored dictionary."""
        return cls(
            workspace_id=data.get("workspace_id", ""),
            original_prompt=data.get("original_prompt", ""),
            verified_decisions=data.get("verified_decisions", []),
            verified_tradeoffs=data.get("verified_tradeoffs", []),
            human_required_items=data.get("human_required_items", []),
            architecture_constraints=data.get("architecture_constraints", []),
            consensus_history=data.get("consensus_history", []),
            active_disputes=data.get("active_disputes", []),
            resolved_disputes=data.get("resolved_disputes", []),
            gpt_position=data.get("gpt_position", ""),
            gemini_position=data.get("gemini_position", ""),
            answer_history=data.get("answer_history", []),
            impact_audit_trail=data.get("impact_audit_trail", []),
            research_evidence=data.get("research_evidence", []),
            research_audit_trail=data.get("research_audit_trail", []),
            status=data.get("status", WorkspaceStatus.CREATED),
            current_job_id=data.get("current_job_id", ""),
            consensus_score=data.get("consensus_score", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            error=data.get("error", ""),
            retry_count=data.get("retry_count", 0),
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def resolved_decision_keys(self) -> set[str]:
        """Set of normalized_keys for all resolved decisions."""
        return {d.get("normalized_key", "") for d in self.resolved_disputes}

    @property
    def verified_decision_keys(self) -> set[str]:
        """Set of normalized_keys for verified decisions."""
        return {d.get("normalized_key", "") for d in self.verified_decisions}

    @property
    def pending_human_items(self) -> list[dict]:
        """Human-required items that haven't been resolved yet.
        Reads from the unified HumanDecisionStore (single source of truth)."""
        from human_decision_store import store
        return store.pending(self.workspace_id)

    def add_answer(self, answer_id: int, parent_answer_id: int | None,
                   question: str) -> None:
        """Record an answer in the history chain."""
        self.answer_history.append({
            "answer_id": answer_id,
            "parent_answer_id": parent_answer_id,
            "question": question[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_verified_decision(self, decision: dict) -> None:
        """Add a verified decision, deduplicating by normalized_key."""
        nk = decision.get("normalized_key", "")
        existing = {d.get("normalized_key") for d in self.verified_decisions}
        if nk and nk not in existing:
            self.verified_decisions.append(decision)
        elif not nk:
            self.verified_decisions.append(decision)

    def add_verified_tradeoff(self, tradeoff: dict) -> None:
        """Add a verified tradeoff with option_a, option_b, recommendation, confidence, reasoning."""
        required = {"option_a", "option_b"}
        if not all(k in tradeoff for k in required):
            return
        # Deduplicate by option pair
        existing = {(t.get("option_a", ""), t.get("option_b", ""))
                    for t in self.verified_tradeoffs}
        pair = (tradeoff.get("option_a", ""), tradeoff.get("option_b", ""))
        if pair not in existing:
            self.verified_tradeoffs.append(tradeoff)

    def add_human_required(self, item: dict) -> None:
        """Queue a human-required decision via the unified store."""
        from human_decision_store import store
        store.create(self.workspace_id, item)

    def resolve_human_item(self, question: str, choice: str) -> bool:
        """Resolve a human-required item via the unified store."""
        from human_decision_store import store
        return store.resolve_by_key(self.workspace_id, question, choice)

    def get_constraint_context(self) -> str:
        """Build constraint injection text from resolved decisions."""
        if not self.verified_decisions:
            return ""
        lines = ["<workspace_constraints>"]
        for d in self.verified_decisions[-10:]:  # Last 10 constraints
            lines.append(f"  [{d.get('normalized_key', '')}]: {d.get('decision', '')}")
        lines.append("</workspace_constraints>")
        lines.append("The above decisions are SETTLED from prior interactions. "
                      "Do not contradict them.")
        return "\n".join(lines)

    def mark_updated(self) -> None:
        """Update the timestamp."""
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def add_impact_audit(self, entry: dict) -> None:
        """Append a follow-up impact-analysis audit record to the trail."""
        if isinstance(entry, dict):
            self.impact_audit_trail.append(entry)

    def add_research_audit(self, entry: dict) -> None:
        """Append a pre-debate research audit record to the trail."""
        if isinstance(entry, dict):
            self.research_audit_trail.append(entry)


# ---------------------------------------------------------------------------
# WorkspaceManager — persistence layer
# ---------------------------------------------------------------------------

class WorkspaceManager:
    """Manages workspace lifecycle: create, load, update, delete, list, search.

    Uses SQLite for MVP with a PostgreSQL-compatible schema (no SQLite-specific
    features beyond WAL mode and basic types that map cleanly to PG).
    """

    def __init__(self):
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazy initialization to avoid import-time side effects."""
        if self._initialized:
            return
        self._initialize_database()
        self._initialized = True

    def _initialize_database(self) -> None:
        """Create workspace tables if they don't exist.

        Schema is designed to be PostgreSQL-compatible:
        - Uses TEXT for JSON columns (JSONB in PG can be migrated)
        - Uses TEXT for timestamps (TIMESTAMPTZ in PG)
        - No SQLite-specific pragmas beyond WAL
        """
        conn = _get_connection()
        cursor = conn.cursor()

        # NOTE: We intentionally do NOT use foreign keys on human_decisions
        # referencing workspaces because INSERT OR REPLACE in SQLite triggers
        # ON DELETE CASCADE internally when replacing rows. This would delete
        # all human decisions on every workspace save. Instead, we manage
        # referential integrity at the application layer.
        cursor.execute("PRAGMA foreign_keys=OFF")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            original_prompt TEXT NOT NULL,
            verified_decisions TEXT NOT NULL DEFAULT '[]',
            verified_tradeoffs TEXT NOT NULL DEFAULT '[]',
            human_required_items TEXT NOT NULL DEFAULT '[]',
            architecture_constraints TEXT NOT NULL DEFAULT '[]',
            consensus_history TEXT NOT NULL DEFAULT '[]',
            active_disputes TEXT NOT NULL DEFAULT '[]',
            resolved_disputes TEXT NOT NULL DEFAULT '[]',
            gpt_position TEXT NOT NULL DEFAULT '',
            gemini_position TEXT NOT NULL DEFAULT '',
            answer_history TEXT NOT NULL DEFAULT '[]',
            impact_audit_trail TEXT NOT NULL DEFAULT '[]',
            research_evidence TEXT NOT NULL DEFAULT '[]',
            research_audit_trail TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'created',
            current_job_id TEXT NOT NULL DEFAULT '',
            consensus_score INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0
        )
        """)

        # Index for listing and searching
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspaces_status
        ON workspaces(status)
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspaces_updated
        ON workspaces(updated_at)
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspaces_created
        ON workspaces(created_at)
        """)

        conn.commit()

        # Schema migration: add any missing columns from older versions
        self._migrate_schema(cursor, conn)

        conn.close()

    def _migrate_schema(self, cursor, conn) -> None:
        """Add columns that may be missing from older workspace databases."""
        cursor.execute("PRAGMA table_info(workspaces)")
        columns = {col[1] for col in cursor.fetchall()}

        migrations = [
            ("verified_tradeoffs", "TEXT NOT NULL DEFAULT '[]'"),
            ("architecture_constraints", "TEXT NOT NULL DEFAULT '[]'"),
            ("consensus_history", "TEXT NOT NULL DEFAULT '[]'"),
            ("human_required_items", "TEXT NOT NULL DEFAULT '[]'"),
            ("answer_history", "TEXT NOT NULL DEFAULT '[]'"),
            ("impact_audit_trail", "TEXT NOT NULL DEFAULT '[]'"),
            ("research_evidence", "TEXT NOT NULL DEFAULT '[]'"),
            ("research_audit_trail", "TEXT NOT NULL DEFAULT '[]'"),
            ("error", "TEXT NOT NULL DEFAULT ''"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ]

        for col_name, col_def in migrations:
            if col_name not in columns:
                print(f"[WORKSPACE SCHEMA MIGRATION] Adding column: {col_name}")
                cursor.execute(f"ALTER TABLE workspaces ADD COLUMN {col_name} {col_def}")

        conn.commit()

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def create_workspace(self, prompt: str, memory_ids: list | None = None) -> str:
        """Create a new workspace from a user prompt.

        Args:
            prompt: The user's initial prompt/question.
            memory_ids: Optional list of prior debate IDs for context.

        Returns:
            The workspace_id (UUID string).
        """
        self._ensure_initialized()

        workspace_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # If memory_ids provided, embed them as initial constraints
        constraints = []
        if memory_ids:
            from answer_memory import memory
            for mid in memory_ids:
                debate = memory.get_debate(mid)
                if debate and debate.get("resolved_decisions"):
                    for rd in debate["resolved_decisions"]:
                        if isinstance(rd, dict):
                            constraints.append({
                                "source_debate_id": mid,
                                "decision": rd.get("decision", ""),
                                "normalized_key": rd.get("normalized_key", ""),
                                "status": rd.get("status", "unresolved"),
                            })

        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO workspaces (
            workspace_id, original_prompt, architecture_constraints,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """, (
            workspace_id,
            prompt,
            json.dumps(constraints),
            now,
            now,
        ))
        conn.commit()
        conn.close()

        print(f"[WORKSPACE] Created workspace {workspace_id[:8]}... "
              f"with {len(constraints)} inherited constraint(s)")
        return workspace_id

    def load_workspace(self, workspace_id: str) -> Workspace | None:
        """Load a workspace by ID. Returns None if not found."""
        self._ensure_initialized()

        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM workspaces WHERE workspace_id = ?",
                       (workspace_id,))
        row = cursor.fetchone()
        conn.close()

        if row is None:
            return None

        return self._row_to_workspace(row)

    def save_workspace(self, workspace: Workspace) -> bool:
        """Persist workspace state to the database. Uses UPDATE/INSERT,
        not INSERT OR REPLACE, to avoid SQLite's cascade-delete behavior."""
        self._ensure_initialized()

        workspace.mark_updated()

        conn = _get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1 FROM workspaces WHERE workspace_id = ?",
                           (workspace.workspace_id,))
            exists = cursor.fetchone() is not None

            if exists:
                cursor.execute("""
                UPDATE workspaces SET
                    original_prompt = ?,
                    verified_decisions = ?,
                    verified_tradeoffs = ?,
                    human_required_items = ?,
                    architecture_constraints = ?,
                    consensus_history = ?,
                    active_disputes = ?,
                    resolved_disputes = ?,
                    gpt_position = ?,
                    gemini_position = ?,
                    answer_history = ?,
                    impact_audit_trail = ?,
                    research_evidence = ?,
                    research_audit_trail = ?,
                    status = ?,
                    current_job_id = ?,
                    consensus_score = ?,
                    updated_at = ?,
                    error = ?,
                    retry_count = ?
                WHERE workspace_id = ?
                """, (
                    workspace.original_prompt,
                    _safe_json_dump(workspace.verified_decisions, []),
                    _safe_json_dump(workspace.verified_tradeoffs, []),
                    _safe_json_dump(workspace.human_required_items, []),
                    _safe_json_dump(workspace.architecture_constraints, []),
                    _safe_json_dump(workspace.consensus_history, []),
                    _safe_json_dump(workspace.active_disputes, []),
                    _safe_json_dump(workspace.resolved_disputes, []),
                    workspace.gpt_position,
                    workspace.gemini_position,
                    _safe_json_dump(workspace.answer_history, []),
                    _safe_json_dump(workspace.impact_audit_trail, []),
                    _safe_json_dump(workspace.research_evidence, []),
                    _safe_json_dump(workspace.research_audit_trail, []),
                    workspace.status,
                    workspace.current_job_id,
                    workspace.consensus_score,
                    workspace.updated_at,
                    workspace.error,
                    workspace.retry_count,
                    workspace.workspace_id,
                ))
            else:
                cursor.execute("""
                INSERT INTO workspaces (
                    workspace_id, original_prompt, verified_decisions,
                    verified_tradeoffs, human_required_items,
                    architecture_constraints, consensus_history,
                    active_disputes, resolved_disputes,
                    gpt_position, gemini_position, answer_history,
                    impact_audit_trail,
                    research_evidence, research_audit_trail,
                    status, current_job_id, consensus_score,
                    created_at, updated_at, error, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workspace.workspace_id,
                    workspace.original_prompt,
                    _safe_json_dump(workspace.verified_decisions, []),
                    _safe_json_dump(workspace.verified_tradeoffs, []),
                    _safe_json_dump(workspace.human_required_items, []),
                    _safe_json_dump(workspace.architecture_constraints, []),
                    _safe_json_dump(workspace.consensus_history, []),
                    _safe_json_dump(workspace.active_disputes, []),
                    _safe_json_dump(workspace.resolved_disputes, []),
                    workspace.gpt_position,
                    workspace.gemini_position,
                    _safe_json_dump(workspace.answer_history, []),
                    _safe_json_dump(workspace.impact_audit_trail, []),
                    _safe_json_dump(workspace.research_evidence, []),
                    _safe_json_dump(workspace.research_audit_trail, []),
                    workspace.status,
                    workspace.current_job_id,
                    workspace.consensus_score,
                    workspace.created_at or datetime.now(timezone.utc).isoformat(),
                    workspace.updated_at,
                    workspace.error,
                    workspace.retry_count,
                ))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[WORKSPACE SAVE ERROR] {e}")
            conn.close()
            return False

    def update_status(self, workspace_id: str, status: str,
                      error: str = "") -> bool:
        """Lightweight status update without full save."""
        self._ensure_initialized()
        conn = _get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE workspaces
            SET status = ?, error = ?, updated_at = ?
            WHERE workspace_id = ?
            """, (status, error, datetime.now(timezone.utc).isoformat(), workspace_id))
            conn.commit()
            conn.close()
            return cursor.rowcount > 0
        except Exception as e:
            print(f"[WORKSPACE STATUS UPDATE ERROR] {e}")
            conn.close()
            return False

    def upsert_build_workspace(
        self,
        workspace_id: str,
        *,
        original_prompt: str = "",
        source_answer_id: str | None = None,
        source_topic: str | None = None,
        status: str = "completed",
        consensus_score: int = 0,
        verified_decisions: list | None = None,
        verified_tradeoffs: list | None = None,
        human_required_items: list | None = None,
        architecture_constraints: list | None = None,
        consensus_history: list | None = None,
        active_disputes: list | None = None,
        resolved_disputes: list | None = None,
        gpt_position: str = "",
        gemini_position: str = "",
        answer_history: list | None = None,
        impact_audit_trail: list | None = None,
        research_evidence: list | None = None,
        research_audit_trail: list | None = None,
        current_job_id: str = "",
        error: str = "",
        retry_count: int = 0,
        created_at: str | None = None,
    ) -> bool:
        """Build Finalizer STEP 2 — register a filesystem build workspace
        into the SQL `workspaces` table so the build is discoverable by
        `sol0mon resume <id>` and `sol0mon status --workspace <id>` (which
        both query `workspace_manager.load_workspace` -> SELECT * FROM
        workspaces).

        The forensic audit proved `build_pipeline` only wrote filesystem
        JSON (`workspace.json`/`build_state.json`) and never INSERTed into
        `database/workspaces.db.workspaces` — hence the SQL table was empty
        (0 rows) even when a build had completed on disk. Resume/status then
        reported "Workspace not found" despite a fully-built workspace
        existing under `workspaces/<id>/`. This method closes that gap.

        Idempotent: re-runs UPSERT (UPDATE-where-exists, else INSERT) with
        the same data. The first call after a successful build populates the
        row; subsequent reconciliation calls are no-ops on the durable state
        (the `updated_at` timestamp advances, which is intentional — it
        reflects that reconciliation was re-run).

        `status` defaults to "completed" — the Build Finalizer only runs
        after the build lifecycle reaches COMPLETE, so anything else here
        would be a lie. Callers that need to register an in-progress
        workspace (none today) pass `status=` explicitly.

        Returns True on a successful persist, False on a DB error (the Build
        Finalizer surfaces False as a transactional step failure rather than
        swallowing it).
        """
        self._ensure_initialized()
        now = datetime.now(timezone.utc).isoformat()
        verified_decisions = verified_decisions or []
        verified_tradeoffs = verified_tradeoffs or []
        human_required_items = human_required_items or []
        architecture_constraints = architecture_constraints or []
        consensus_history = consensus_history or []
        active_disputes = active_disputes or []
        resolved_disputes = resolved_disputes or []
        answer_history = answer_history or []
        impact_audit_trail = impact_audit_trail or []
        research_evidence = research_evidence or []
        research_audit_trail = research_audit_trail or []

        if answer_history is None or not answer_history:
            if source_answer_id is not None:
                answer_history = [{
                    "answer_id": source_answer_id,
                    "parent_answer_id": None,
                    "question": original_prompt,
                    "timestamp": now,
                }]

        conn = _get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?",
                (workspace_id,)
            )
            exists = cursor.fetchone() is not None
            if exists:
                cursor.execute("""
                UPDATE workspaces SET
                    original_prompt = ?,
                    verified_decisions = ?,
                    verified_tradeoffs = ?,
                    human_required_items = ?,
                    architecture_constraints = ?,
                    consensus_history = ?,
                    active_disputes = ?,
                    resolved_disputes = ?,
                    gpt_position = ?,
                    gemini_position = ?,
                    answer_history = ?,
                    impact_audit_trail = ?,
                    research_evidence = ?,
                    research_audit_trail = ?,
                    status = ?,
                    current_job_id = ?,
                    consensus_score = ?,
                    updated_at = ?,
                    error = ?,
                    retry_count = ?
                WHERE workspace_id = ?
                """, (
                    original_prompt,
                    _safe_json_dump(verified_decisions, []),
                    _safe_json_dump(verified_tradeoffs, []),
                    _safe_json_dump(human_required_items, []),
                    _safe_json_dump(architecture_constraints, []),
                    _safe_json_dump(consensus_history, []),
                    _safe_json_dump(active_disputes, []),
                    _safe_json_dump(resolved_disputes, []),
                    gpt_position, gemini_position,
                    _safe_json_dump(answer_history, []),
                    _safe_json_dump(impact_audit_trail, []),
                    _safe_json_dump(research_evidence, []),
                    _safe_json_dump(research_audit_trail, []),
                    status, current_job_id, consensus_score,
                    now, error, retry_count, workspace_id,
                ))
            else:
                cursor.execute("""
                INSERT INTO workspaces (
                    workspace_id, original_prompt, verified_decisions,
                    verified_tradeoffs, human_required_items,
                    architecture_constraints, consensus_history,
                    active_disputes, resolved_disputes,
                    gpt_position, gemini_position, answer_history,
                    impact_audit_trail,
                    research_evidence, research_audit_trail,
                    status, current_job_id, consensus_score,
                    created_at, updated_at, error, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workspace_id, original_prompt,
                    _safe_json_dump(verified_decisions, []),
                    _safe_json_dump(verified_tradeoffs, []),
                    _safe_json_dump(human_required_items, []),
                    _safe_json_dump(architecture_constraints, []),
                    _safe_json_dump(consensus_history, []),
                    _safe_json_dump(active_disputes, []),
                    _safe_json_dump(resolved_disputes, []),
                    gpt_position, gemini_position,
                    _safe_json_dump(answer_history, []),
                    _safe_json_dump(impact_audit_trail, []),
                    _safe_json_dump(research_evidence, []),
                    _safe_json_dump(research_audit_trail, []),
                    status, current_job_id, consensus_score,
                    created_at or now, now, error, retry_count,
                ))
            conn.commit()
            return True
        except Exception as e:
            print(f"[WORKSPACE UPSERT ERROR] {e}")
            return False
        finally:
            conn.close()

    def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace permanently."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM workspaces WHERE workspace_id = ?",
                       (workspace_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def list_workspaces(self, limit: int = 50, status: str | None = None) -> list[dict]:
        """List workspaces, optionally filtered by status. Most recent first."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()

        if status:
            cursor.execute("""
            SELECT workspace_id, original_prompt, status, consensus_score,
                   created_at, updated_at, error
            FROM workspaces
            WHERE status = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """, (status, limit))
        else:
            cursor.execute("""
            SELECT workspace_id, original_prompt, status, consensus_score,
                   created_at, updated_at, error
            FROM workspaces
            ORDER BY updated_at DESC
            LIMIT ?
            """, (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "workspace_id": row[0],
                "original_prompt": row[1][:200],
                "status": row[2],
                "consensus_score": row[3],
                "created_at": row[4],
                "updated_at": row[5],
                "error": row[6],
            }
            for row in rows
        ]

    def list_pending_human_items(self, workspace_id: str) -> list[dict]:
        """Get unresolved human-required items for a workspace."""
        ws = self.load_workspace(workspace_id)
        if ws is None:
            return []
        return ws.pending_human_items

    def search_workspaces(self, query: str, limit: int = 20) -> list[dict]:
        """Search workspaces by prompt text."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()

        safe_query = query.replace("%", "\\%").replace("_", "\\_")
        like_query = f"%{safe_query}%"

        cursor.execute("""
        SELECT workspace_id, original_prompt, status, consensus_score,
               created_at, updated_at
        FROM workspaces
        WHERE original_prompt LIKE ?
        ORDER BY updated_at DESC
        LIMIT ?
        """, (like_query, limit))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "workspace_id": row[0],
                "original_prompt": row[1][:200],
                "status": row[2],
                "consensus_score": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]

    def get_stats(self) -> dict:
        """Return aggregate workspace statistics."""
        self._ensure_initialized()
        conn = _get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM workspaces")
        total = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM workspaces WHERE status = ?",
                       (WorkspaceStatus.AWAITING_HUMAN,))
        awaiting = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM workspaces WHERE status = ?",
                       (WorkspaceStatus.COMPLETED,))
        completed = cursor.fetchone()[0] or 0

        cursor.execute("SELECT AVG(consensus_score) FROM workspaces WHERE status = ?",
                       (WorkspaceStatus.COMPLETED,))
        avg_score = cursor.fetchone()[0]
        conn.close()

        return {
            "total_workspaces": total,
            "awaiting_human": awaiting,
            "completed": completed,
            "average_consensus_score": round(avg_score, 2) if avg_score else 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_workspace(self, row: tuple) -> Workspace:
        """Convert a database row to a Workspace object."""
        cols = [
            "workspace_id", "original_prompt", "verified_decisions",
            "verified_tradeoffs", "human_required_items",
            "architecture_constraints", "consensus_history",
            "active_disputes", "resolved_disputes",
            "gpt_position", "gemini_position", "answer_history",
            "impact_audit_trail",
            "research_evidence", "research_audit_trail",
            "status", "current_job_id", "consensus_score",
            "created_at", "updated_at", "error", "retry_count",
        ]

        # Map row values to column names, handling variable column counts
        data = {}
        for i, col in enumerate(cols):
            if i < len(row):
                data[col] = row[i]
            else:
                # Column didn't exist in this row version
                data[col] = "" if col in ("gpt_position", "gemini_position",
                                            "current_job_id", "created_at",
                                            "updated_at", "error") else \
                            [] if col.endswith("s") or col.endswith("ems") or \
                                   col.endswith("ory") or col.endswith("ail") \
                                   or col.endswith("ence") \
                                   else 0

        # Deserialize JSON columns
        json_cols = [
            "verified_decisions", "verified_tradeoffs", "human_required_items",
            "architecture_constraints", "consensus_history",
            "active_disputes", "resolved_disputes", "answer_history",
            "impact_audit_trail",
            "research_evidence", "research_audit_trail",
        ]
        for col in json_cols:
            data[col] = _safe_json_load(data[col], [])

        return Workspace(**data)

    def close(self) -> None:
        """No-op for interface consistency. SQLite connections are short-lived."""
        pass


# ---------------------------------------------------------------------------
# Singleton instance for module-level use
# ---------------------------------------------------------------------------

workspace_mgr = WorkspaceManager()

# ---------------------------------------------------------------------------
# Top-level convenience functions (mirroring answer_memory pattern)
# ---------------------------------------------------------------------------


def create_workspace(prompt: str, memory_ids: list | None = None) -> str:
    """Create a new workspace. Returns workspace_id."""
    return workspace_mgr.create_workspace(prompt, memory_ids=memory_ids)


def load_workspace(workspace_id: str) -> Workspace | None:
    """Load an existing workspace."""
    return workspace_mgr.load_workspace(workspace_id)


def save_workspace(workspace: Workspace) -> bool:
    """Persist workspace state."""
    return workspace_mgr.save_workspace(workspace)


def list_workspaces(limit: int = 50) -> list[dict]:
    """List recent workspaces."""
    return workspace_mgr.list_workspaces(limit=limit)


def delete_workspace(workspace_id: str) -> bool:
    """Delete a workspace."""
    return workspace_mgr.delete_workspace(workspace_id)


def get_pending_human_items(workspace_id: str) -> list[dict]:
    """Get unresolved human-required items."""
    return workspace_mgr.list_pending_human_items(workspace_id)


def resolve_human_item(workspace_id: str, question: str, choice: str) -> bool:
    """Resolve a human-required item with user's choice."""
    ws = workspace_mgr.load_workspace(workspace_id)
    if ws is None:
        return False
    result = ws.resolve_human_item(question, choice)
    if result:
        workspace_mgr.save_workspace(ws)
    return result


if __name__ == "__main__":
    # Quick smoke test
    ws_id = create_workspace("Design a planet-scale consensus platform")
    print(f"Created workspace: {ws_id}")

    ws = load_workspace(ws_id)
    assert ws is not None
    print(f"Loaded workspace: status={ws.status}, prompt={ws.original_prompt[:50]}...")

    # Test add operations
    ws.add_verified_decision({
        "decision": "Use PostgreSQL for primary storage",
        "normalized_key": "database_choice",
    })
    ws.add_verified_tradeoff({
        "option_a": "Kubernetes",
        "option_b": "Nomad",
        "recommendation": "Use Kubernetes for better ecosystem",
        "confidence": 85,
        "reasoning": "Kubernetes has broader community support",
    })
    ws.add_human_required({
        "question": "Database Choice",
        "options": ["SQLite", "PostgreSQL"],
        "reasoning": "MVP simplicity vs production scalability",
        "affected_disputes": ["database_choice"],
    })
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    assert save_workspace(ws)

    # Reload and verify
    ws2 = load_workspace(ws_id)
    assert ws2 is not None
    assert len(ws2.verified_decisions) == 1
    assert len(ws2.verified_tradeoffs) == 1
    assert len(ws2.pending_human_items) == 1
    assert ws2.status == WorkspaceStatus.CONSENSUS_REACHED

    # List and search
    workspaces = list_workspaces(limit=5)
    assert len(workspaces) >= 1
    found = workspace_mgr.search_workspaces("planet-scale")
    assert len(found) >= 1

    # Human item resolution
    assert resolve_human_item(ws_id, "Database Choice", "PostgreSQL")
    ws3 = load_workspace(ws_id)
    from human_decision_store import store as _store
    resolved = _store.resolved_list(ws_id)
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "PostgreSQL"

    # Stats
    stats = workspace_mgr.get_stats()
    print(f"Stats: {stats}")

    # Cleanup test data
    assert delete_workspace(ws_id)
    print("Workspace manager smoke test PASSED")