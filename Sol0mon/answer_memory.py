import sqlite3
import json
from datetime import datetime, timezone

DB_FILE = "answer_memory.db"


class AnswerMemory:

    def __init__(self):
        pass

    def _ensure_initialized(self):
        """Always guarantee the schema before use.

        Stops trusting the _initialized singleton flag: it used to stay True
        after the DB was deleted / recreated / DB_FILE swapped between suite
        runs, leaving the debates table gone while init short-circuited, which
        produced 'no such table: debates' in suite (but not in isolation).
        _initialize_database() is fully idempotent (CREATE TABLE IF NOT
        EXISTS + guarded ALTER migrations) and cheap, so running it on every
        connection is safe. Mirrors the HumanDecisionStore fix. Tests still set
        answer_memory.memory._initialized = False for back-compat; the flag is
        now inert — schema is always guaranteed here. (ponytail: dropped
        singleton state in favour of idempotent CREATE-IF-NOT-EXISTS.)"""
        self._initialize_database()

    def _get_connection(self):
        self._ensure_initialized()
        return sqlite3.connect(DB_FILE)

    def _initialize_database(self):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("PRAGMA journal_mode=WAL")

        # H6: Redesigned schema. Drop raw gpt_final/gemini_final transcript columns.
        # Store compact resolved decisions and a decision summary instead.
        # Keep question, consensus_score, and conflict_severity for searchability.
        # validated_disputes and verification_result are normalized to reduce bloat.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS debates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            question TEXT NOT NULL,
            gpt_final TEXT NOT NULL,
            gemini_final TEXT NOT NULL,
            synthesized_answer TEXT NOT NULL,
            consensus_score INTEGER NOT NULL,
            conflict_severity TEXT NOT NULL,
            conflict_points TEXT NOT NULL
        )
        """)
        conn.commit()

        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_debates_question
        ON debates(question)
        """)
        conn.commit()

        # H6: Schema migration — add resolved_decisions and decision_summary columns
        cursor.execute("PRAGMA table_info(debates)")
        columns = [col[1] for col in cursor.fetchall()]

        if "resolved_decisions" not in columns:
            print("[SCHEMA MIGRATION] Adding resolved_decisions column for compact decision storage...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN resolved_decisions TEXT NOT NULL DEFAULT '[]'
            """)
            conn.commit()

        if "decision_summary" not in columns:
            print("[SCHEMA MIGRATION] Adding decision_summary column...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN decision_summary TEXT NOT NULL DEFAULT ''
            """)
            conn.commit()

        if "validated_disputes" not in columns:
            print("[SCHEMA MIGRATION] Adding validated_disputes column...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN validated_disputes TEXT NOT NULL DEFAULT '[]'
            """)
            conn.commit()

        if "verification_result" not in columns:
            print("[SCHEMA MIGRATION] Adding verification_result column...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN verification_result TEXT NOT NULL DEFAULT '{}'
            """)
            conn.commit()

        if "parent_answer_id" not in columns:
            print("[SCHEMA MIGRATION] Adding parent_answer_id column for answer linking...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN parent_answer_id INTEGER
            """)
            conn.commit()

        if "workspace_id" not in columns:
            print("[SCHEMA MIGRATION] Adding workspace_id column for workspace association...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''
            """)
            conn.commit()

        # Human-Readable Synthesis V3 — store the human-facing answer alongside
        # the audit synthesized_answer. Idempotent: the column_info guard makes
        # the migration a no-op on DBs that already have it. DEFAULT '' keeps
        # legacy rows (and callers that never pass human_answer) backward-
        # compatible: they read back as an empty string, never an error.
        if "human_answer" not in columns:
            print("[SCHEMA MIGRATION] Adding human_answer column for human-facing synthesis...")
            cursor.execute("""
                ALTER TABLE debates
                ADD COLUMN human_answer TEXT NOT NULL DEFAULT ''
            """)
            conn.commit()

        # Index for workspace-based lookups
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_debates_workspace_id
        ON debates(workspace_id)
        """)
        conn.commit()

        conn.close()

    def _extract_decisions(self, validated_disputes, verification_result):
        """Bridge verification_result -> resolved_decisions.

        FIX 1: the authoritative decision content lives in
        verification_result.resolved_claims[].verification (status, confidence,
        finding). Each `finding` states the winner and the rejected alternative.
        Previously this method stored the dispute TITLE as `decision` and an empty
        `rationale`, discarding the winner/confidence/finding. Now each record
        carries decision/winner/confidence/rationale/finding so a consumer can
        reconstruct what was decided without re-reading the raw verification blob.
        """
        decisions = []
        if not isinstance(verification_result, dict):
            return decisions

        resolved_claims = verification_result.get("resolved_claims", []) or []
        unresolved_claims = verification_result.get("unresolved_claims", []) or []

        # Index validated_disputes by normalized_key for the dispute title.
        dispute_titles: dict[str, str] = {}
        if isinstance(validated_disputes, list):
            for d in validated_disputes:
                if isinstance(d, dict):
                    nk = d.get("normalized_key", "")
                    if nk and nk not in dispute_titles:
                        dispute_titles[nk] = d.get("dispute", "") or d.get("dispute_text", "")

        def _record(claim, status):
            if not isinstance(claim, dict):
                return None
            nk = claim.get("normalized_key", "")
            if not nk:
                return None
            v = claim.get("verification", {}) if isinstance(claim.get("verification"), dict) else {}
            finding = str(v.get("finding", "") or "")
            confidence = v.get("confidence", 0)
            try:
                confidence = int(confidence)
            except (TypeError, ValueError):
                confidence = 0
            # finding states the winner (e.g. "resolved in favor of X; rejects Y").
            # decision/winner/rationale all derive from the finding so the
            # outcome survives even if the dispute title is ambiguous.
            return {
                "normalized_key": nk,
                "dispute": claim.get("dispute", "") or dispute_titles.get(nk, ""),
                "status": status,
                "decision": finding or claim.get("dispute", ""),
                "winner": finding,
                "confidence": confidence,
                "rationale": finding,
                "finding": finding,
                "requires_external_evidence": bool(v.get("requires_external_evidence", False)),
            }

        for claim in resolved_claims:
            rec = _record(claim, "resolved")
            if rec:
                decisions.append(rec)
        for claim in unresolved_claims:
            rec = _record(claim, "unresolved")
            if rec:
                decisions.append(rec)

        # Backfill any validated_disputes whose keys verification never reported
        # (e.g. truncated disputes) so they are not silently dropped.
        seen = {d.get("normalized_key") for d in decisions}
        if isinstance(validated_disputes, list):
            for d in validated_disputes:
                if not isinstance(d, dict):
                    continue
                nk = d.get("normalized_key", "")
                if nk and nk not in seen:
                    decisions.append({
                        "normalized_key": nk,
                        "dispute": d.get("dispute", "") or d.get("dispute_text", ""),
                        "status": "unverified",
                        "decision": "",
                        "winner": "",
                        "confidence": 0,
                        "rationale": "",
                        "finding": "",
                        "requires_external_evidence": False,
                    })
        return decisions

    def _build_decision_summary(self, synthesized_answer, resolved_decisions):
        """H6: Build a compact summary from the synthesized answer and decisions."""
        if synthesized_answer and len(synthesized_answer) > 500:
            # Take the first 500 chars as a compact summary rather than the full answer
            return synthesized_answer[:500].strip()
        return synthesized_answer or ""

    def save_debate(
        self,
        question,
        gpt_final,
        gemini_final,
        synthesized_answer,
        consensus_score,
        conflict_severity="LOW",
        conflict_points=None,
        validated_disputes=None,
        verification_result=None,
        parent_answer_id=None,
        workspace_id=None,
        human_answer="",
    ):
        if conflict_points is None:
            conflict_points = []

        if validated_disputes is None:
            validated_disputes = []

        if verification_result is None:
            verification_result = {}

        # H6: Extract compact decisions and summary before storage
        resolved_decisions = self._extract_decisions(validated_disputes, verification_result)
        decision_summary = self._build_decision_summary(synthesized_answer, resolved_decisions)

        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO debates (
                created_at,
                question,
                gpt_final,
                gemini_final,
                synthesized_answer,
                consensus_score,
                conflict_severity,
                conflict_points,
                validated_disputes,
                verification_result,
                resolved_decisions,
                decision_summary,
                parent_answer_id,
                workspace_id,
                human_answer
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                question,
                gpt_final,
                gemini_final,
                synthesized_answer,
                int(consensus_score),
                str(conflict_severity),
                json.dumps(conflict_points),
                json.dumps(validated_disputes),
                json.dumps(verification_result),
                json.dumps(resolved_decisions),
                decision_summary,
                parent_answer_id,
                workspace_id or "",
                human_answer or "",
            )
        )

        debate_id = cursor.lastrowid

        conn.commit()
        conn.close()

        print(
            f"[ANSWER_MEMORY] Stored Debate #{debate_id} "
            f"(Consensus={consensus_score}%) "
            f"Decisions={len(resolved_decisions)} "
            f"Summary={len(decision_summary)} chars"
        )

        return debate_id

    def get_debate(self, debate_id):
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id, created_at, question, gpt_final, gemini_final,
                synthesized_answer, consensus_score, conflict_severity,
                conflict_points, validated_disputes, verification_result,
                resolved_decisions, decision_summary,
                parent_answer_id, workspace_id, human_answer
            FROM debates
            WHERE id = ?
            """,
            (debate_id,)
        )

        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        def safe_json(value, default):
            try:
                return json.loads(value)
            except Exception:
                return default

        return {
            "id": row[0],
            "created_at": row[1],
            "question": row[2],
            "gpt_final": row[3],
            "gemini_final": row[4],
            "synthesized_answer": row[5],
            "consensus_score": row[6],
            "conflict_severity": row[7],
            "conflict_points": safe_json(row[8], []),
            "validated_disputes": safe_json(row[9], []),
            "verification_result": safe_json(row[10], {}),
            "resolved_decisions": safe_json(row[11], []),
            "decision_summary": row[12] or "",
            "parent_answer_id": row[13] if len(row) > 13 else None,
            "workspace_id": row[14] if len(row) > 14 else "",
            "human_answer": row[15] if len(row) > 15 else "",
        }

    def get_human_answer(self, debate_id):
        """Return only the human-facing answer for a debate, or None if the
        debate does not exist. Empty string when the debate exists but no
        human_answer was stored (legacy rows / failed-verification rows).
        Does not break existing callers of get_debate."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT human_answer FROM debates WHERE id = ?",
            (debate_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        return row[0] or ""

    def get_recent_debates(self, limit=10):
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id, created_at, question, consensus_score, conflict_severity
            FROM debates
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row[0],
                "created_at": row[1],
                "question": row[2],
                "consensus_score": row[3],
                "conflict_severity": row[4]
            }
            for row in rows
        ]

    def list_debates(self, limit=50):
        return self.get_recent_debates(limit)

    def search_debates(self, query, limit=20):
        conn = self._get_connection()
        cursor = conn.cursor()

        # Security: escape LIKE wildcards to prevent pattern-injection abuse
        safe_query = query.replace("%", "\\%").replace("_", "\\_")
        like_query = f"%{safe_query}%"

        cursor.execute(
            """
            SELECT
                id, created_at, question, consensus_score, conflict_severity
            FROM debates
            WHERE
                question LIKE ?
                OR decision_summary LIKE ?
                OR conflict_points LIKE ?
                OR resolved_decisions LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                like_query,
                like_query,
                like_query,
                like_query,
                limit
            )
        )

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row[0],
                "created_at": row[1],
                "question": row[2],
                "consensus_score": row[3],
                "conflict_severity": row[4]
            }
            for row in rows
        ]

    def delete_debate(self, debate_id):
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM debates WHERE id = ?",
            (debate_id,)
        )

        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        return deleted > 0

    def debate_exists(self, debate_id):
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM debates WHERE id = ? LIMIT 1",
            (debate_id,)
        )

        exists = cursor.fetchone() is not None
        conn.close()

        return exists

    def update_build_result(
        self,
        debate_id,
        workspace_id=None,
        execution_decision=None,
        execution_reason=None,
        execution_confidence=None,
        execution_decision_source=None,
    ):
        """Build Finalizer STEP 1 — reconcile the originating debate row with
        the completed build.

        This is the SOLE UPDATE path on `debates`. The forensic audit proved
        that `save_debate` (INSERT) was the only mutator and that
        `workspace_id` / the post-override `execution_decision` were therefore
        frozen at debate-time forever — the build pipeline's `workspace_id`
        was never written back. This method closes that gap without creating
        a duplicate row and without changing the decision model: it mutates
        only the columns the Build Finalizer owns.

        - `workspace_id` (when provided) is written to the `workspace_id`
          column so consumers navigating "latest answer -> its build
          workspace" succeed.
        - Any of `execution_decision` / `execution_reason` /
          `execution_confidence` / `execution_decision_source` (when
          provided) are merged INTO the existing `verification_result` JSON
          blob under the top-level keys the rest of the codebase already
          reads (`execution_decision`, `execution_reason`,
          `execution_confidence`, `execution_decision_source`). The verifier
          fields (`resolved_claims`, `verification_score`, etc.) are
          preserved — only the decision-verdict keys are overridden, which
          is exactly what `_maybe_engine_override` already did inside the
          build workspace's `build_state.json` (the Build Finalizer now
          mirrors that into the durable debate row).

        Idempotent: re-running with the same args is a no-op (the JSON merge
        overwrites the same keys with the same values; the row count check is
        `rowcount > 0`). No row is created when `debate_id` is absent — the
        Build Finalizer surfaces that as a structured failure rather than
        silently synthesizing a phantom debate.

        Returns True when the row was found and updated, False otherwise. The
        caller (Build Finalizer) treats False as a transactional step failure
        and reports it.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM debates WHERE id = ? LIMIT 1",
                (debate_id,)
            )
            if cursor.fetchone() is None:
                return False

            sets = []
            params: list = []
            if workspace_id is not None:
                sets.append("workspace_id = ?")
                params.append(workspace_id)
            if any(v is not None for v in (
                execution_decision, execution_reason,
                execution_confidence, execution_decision_source,
            )):
                cursor.execute(
                    "SELECT verification_result FROM debates WHERE id = ?",
                    (debate_id,)
                )
                row = cursor.fetchone()
                vr = row[0] if row else None
                try:
                    vr_obj = json.loads(vr) if vr else {}
                    if not isinstance(vr_obj, dict):
                        vr_obj = {}
                except (TypeError, ValueError):
                    vr_obj = {}
                if execution_decision is not None:
                    vr_obj["execution_decision"] = execution_decision
                if execution_reason is not None:
                    vr_obj["execution_reason"] = execution_reason
                if execution_confidence is not None:
                    try:
                        vr_obj["execution_confidence"] = int(execution_confidence)
                    except (TypeError, ValueError):
                        pass
                if execution_decision_source is not None:
                    vr_obj["execution_decision_source"] = execution_decision_source
                sets.append("verification_result = ?")
                params.append(json.dumps(vr_obj))

            if not sets:
                return True  # nothing to do, row exists

            params.append(debate_id)
            cursor.execute(
                f"UPDATE debates SET {', '.join(sets)} WHERE id = ?",
                tuple(params)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def build_context(self, debate_ids):
        """H7: Build structured context from prior debates.
        Uses resolved_decisions and decision_summary instead of raw synthesized_answer.
        Delimits context with XML-like tags for clear separation from active instructions.
        """
        context_blocks = []

        for debate_id in debate_ids:
            debate = self.get_debate(debate_id)
            if debate is None:
                continue

            # H7: Use decision_summary and resolved_decisions instead of raw synthesized_answer
            decision_summary = debate.get("decision_summary", "")
            resolved_decisions = debate.get("resolved_decisions", [])

            # Build a compact, structured context block
            parts = []

            if decision_summary:
                parts.append(f"<decision_summary>\n{decision_summary}\n</decision_summary>")

            if resolved_decisions:
                decision_lines = []
                for d in resolved_decisions:
                    if isinstance(d, dict):
                        status = d.get("status", "unresolved")
                        nk = d.get("normalized_key", "")
                        text = d.get("decision", "")
                        decision_lines.append(f"  [{status.upper()}] {nk}: {text}")
                if decision_lines:
                    parts.append(f"<resolved_decisions>\n" + "\n".join(decision_lines) + "\n</resolved_decisions>")

            if not parts:
                continue

            context_blocks.append(
                f"<!-- PRIOR DEBATE #{debate['id']} -->\n"
                + "\n".join(parts)
                + f"\n<!-- consensus_score={debate['consensus_score']}% "
                f"severity={debate['conflict_severity']} -->"
            )

        return "\n\n".join(context_blocks)

    def get_answers_by_workspace(self, workspace_id):
        """Return all answers linked to a workspace, ordered by creation time."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id, created_at, question, consensus_score,
                conflict_severity, parent_answer_id, workspace_id
            FROM debates
            WHERE workspace_id = ?
            ORDER BY id ASC
            """,
            (workspace_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row[0],
                "created_at": row[1],
                "question": row[2],
                "consensus_score": row[3],
                "conflict_severity": row[4],
                "parent_answer_id": row[5] if len(row) > 5 else None,
                "workspace_id": row[6] if len(row) > 6 else "",
            }
            for row in rows
        ]

    def get_answer_chain(self, answer_id):
        """Traverse the parent chain from an answer back to the original.
        Returns list of answer dicts from newest to oldest."""
        chain = []
        current_id = answer_id
        max_depth = 50  # safety limit

        while current_id is not None and max_depth > 0:
            debate = self.get_debate(current_id)
            if debate is None:
                break
            chain.append(debate)
            current_id = debate.get("parent_answer_id")
            max_depth -= 1

        return chain

    def get_stats(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM debates")
        total_debates = cursor.fetchone()[0]

        cursor.execute("SELECT AVG(consensus_score) FROM debates")
        avg_score = cursor.fetchone()[0]

        conn.close()

        return {
            "total_debates": total_debates,
            "average_consensus_score":
                round(avg_score, 2)
                if avg_score is not None
                else 0
        }


memory = AnswerMemory()
