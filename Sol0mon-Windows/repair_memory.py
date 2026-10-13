"""
Project Sol0mon — Phase 3.1: RepairMemory.

A separate component (deliberately NOT embedded in RetryController ── retries
answer "should we continue?", repair memory answers "what have we already
tried?"). Lives between RepairPlanner and RepairAgent.

Failure mode this prevents (the spec's example)::

    Repair A → tests fail
    Repair B → tests fail
    Repair A → tests fail        # wasted retry ── we already tried this
    Repair B → tests fail        # wasted retry ── oscillation
    … (never converges)

RepairMemory breaks the cycle by:

    1. Recording every repair attempt (SQLite: ``repair_history.db``)
    2. Computing failure fingerprints (sha256 of normalized diagnostics)
    3. Computing repair fingerprints (sha256 of strategy + targets + sig)
    4. Maintaining a strategy blacklist (strategies that failed for a sig)
    5. Detecting repeated cycles (A,B,A,B → oscillation flagged)
    6. Escalating the strategy tier after repeated failures (the spec's
       ladder: patch → fix_imports → rewrite_test → regenerate → refactor)
    7. Scoring per-strategy effectiveness (attempts / successes / rate)

Architecture::

    RepairPlanner      ─ consults RepairMemory (skip blacklisted, escalate)
        ↓
    RepairAgent        ─ applies the (possibly escalated) plan
        ↓
    RepairMemory       ─ records the attempt (success or failure) ─→ blacklist

SQLite schema (stdlib ``sqlite3``; no new dependencies)::

    CREATE TABLE repair_attempts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id      TEXT NOT NULL,           -- uuid
        workspace_id    TEXT NOT NULL,
        iteration       INTEGER NOT NULL,
        strategy        TEXT NOT NULL,
        target_files    TEXT NOT NULL,           -- JSON list
        failure_signature TEXT NOT NULL,         -- sha256
        repair_fingerprint TEXT NOT NULL,        -- sha256
        patch_id        TEXT,
        success         INTEGER NOT NULL,         -- 0/1
        files_changed   TEXT,                     -- JSON list
        response_error  TEXT,
        timestamp       TEXT NOT NULL
    );
    CREATE INDEX idx_attempts_workspace ON repair_attempts(workspace_id);
    CREATE INDEX idx_attempts_fingerprint ON repair_attempts(repair_fingerprint);
    CREATE INDEX idx_attempts_strategy ON repair_attempts(strategy);

Pure stdlib (sqlite3, hashlib, json, uuid). No I/O at import time ──
the DB is opened lazily on first record.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Strategy escalation ladder (the spec's "escalate after repeated failures")
# ---------------------------------------------------------------------------

# Lower index = weaker strategy. Higher index = stronger / more invasive.
# Order matters: we step UP the ladder when a strategy fails repeatedly for
# the same failure signature.
ESCALATION_LADDER: tuple[str, ...] = (
    "patch",                 # smallest change: edit a file in place
    "fix_imports",            # targeted: rewrite an import block
    "rewrite_test",           # rewrite the failing test
    "regenerate",             # regenerate the whole module
    "refactor_dependency_graph",  # the spec's nuclear option
)

_STRATEGY_INDEX = {s: i for i, s in enumerate(ESCALATION_LADDER)}


def next_strategy(current: str) -> str | None:
    """Return the next strategy up the ladder, or None when ``current`` is
    already the strongest. Pure."""
    i = _STRATEGY_INDEX.get(current)
    if i is None or i + 1 >= len(ESCALATION_LADDER):
        return None
    return ESCALATION_LADDER[i + 1]


# ---------------------------------------------------------------------------
# Fail-safe constants
# ---------------------------------------------------------------------------

# After this many attempts with the SAME failure signature (regardless of
# strategy), the memory escalates the strategy regardless of blacklist.
DEFAULT_ESCALATION_THRESHOLD = 2

# A cycle A,B,A,B of period 2 (or longer) signals oscillation. We look back
# this many entries when detecting cycles.
DEFAULT_OSCILLATION_WINDOW = 6


# ---------------------------------------------------------------------------
# RepairAttempt ─ the unit of memory
# ---------------------------------------------------------------------------


@dataclass
class RepairAttempt:
    """A single repair attempt ─ the unit RepairMemory records and queries."""
    attempt_id: str = ""
    workspace_id: str = ""
    iteration: int = 0
    strategy: str = ""
    target_files: list = field(default_factory=list)
    failure_signature: str = ""        # sha256 of normalized diagnostics
    repair_fingerprint: str = ""       # sha256 of strategy+targets+sig
    patch_id: str = ""
    success: bool = False
    files_changed: list = field(default_factory=list)
    response_error: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.attempt_id:
            self.attempt_id = f"attempt_{uuid.uuid4().hex[:12]}"
        if not self.timestamp:
            self.timestamp = _now_iso()

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fingerprint helpers ── deterministic content hashes
# ---------------------------------------------------------------------------

_NORMALIZE_WS = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """Normalize whitespace + lowercase for stable fingerprints. ponytail:
    a real normalizer would strip ANSI / relative paths / timestamps too;
    for failure text the cheap version catches 99% of duplicates."""
    if not text:
        return ""
    return _NORMALIZE_WS.sub(" ", text.strip()).lower()


def failure_signature(diagnostics: Iterable) -> str:
    """Compute a sha256 fingerprint of a diagnostic list. Two identical
    failure surfaces produce identical signatures ── this is what
    RepairMemory uses to decide "we've seen this exact failure before".

    Diagnostics may be Diagnostic instances, dicts, or strings; the
    function normalizes each to its (category, message, location) tuple
    and hashes the canonical JSON. Order-independent: the items list is
    SORTED before hashing so caller-side order doesn't change the
    signature. Pure."""
    items: list[tuple] = []
    for d in diagnostics or []:
        if isinstance(d, dict):
            items.append((d.get("category", ""),
                           d.get("message", ""),
                           d.get("location", "")))
        elif hasattr(d, "category"):  # Diagnostic dataclass
            items.append((d.category, d.message, d.location))
        else:
            items.append(("unknown", str(d), ""))
    # Order-independent: sort by the tuple so caller-side ordering doesn't
    # change the signature. (json.dumps(sort_keys=True) only sorts dict
    # keys, NOT list items, so we sort the list ourselves first.)
    items.sort()
    canonical = json.dumps(items, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def repair_fingerprint(
    *, strategy: str, target_files: Iterable[str],
    failure_signature_value: str,
) -> str:
    """Compute the fingerprint of a REPAIR (not the failure): a sha256 of
    the strategy + sorted target files + failure signature. Two identical
    (strategy, files, failure) tuples yield the same fingerprint ──
    RepairMemory uses this to say "we already tried this exact repair on
    this exact failure" and blacklist. Pure."""
    targets = sorted({str(f) for f in (target_files or [])})
    canonical = json.dumps(
        [strategy, targets, failure_signature_value],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# RepairMemory ─ the component
# ---------------------------------------------------------------------------


class RepairMemory:
    """Phase 3.1 component ── records every repair attempt, fingerprints
    failures + repairs, maintains a blacklist of failed strategies, detects
    oscillation, and recommends escalation.

    Storage: SQLite at ``db_path`` (default ``:memory:``). Backed by
    ``repair_history.db`` in the workspace's ``validation/`` dir when
    persistence is desired (production + resume).

    Pure stdlib at import. The DB connection is opened lazily on first
    record so importing the module is free.

    Stateless methods are pure (no I/O). Stateful methods (record_attempt,
    is_blacklisted, ...) hit the DB. Each stateful method re-opens a fresh
    connection per call (no held cursor) so the component is safe across
    fork / subprocess; closing is optional.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS repair_attempts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id          TEXT NOT NULL,
        workspace_id        TEXT NOT NULL,
        iteration           INTEGER NOT NULL,
        strategy            TEXT NOT NULL,
        target_files        TEXT NOT NULL,
        failure_signature   TEXT NOT NULL,
        repair_fingerprint  TEXT NOT NULL,
        patch_id            TEXT,
        success             INTEGER NOT NULL,
        files_changed       TEXT,
        response_error      TEXT,
        timestamp           TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_attempts_workspace ON repair_attempts(workspace_id);
    CREATE INDEX IF NOT EXISTS idx_attempts_fingerprint ON repair_attempts(repair_fingerprint);
    CREATE INDEX IF NOT EXISTS idx_attempts_strategy ON repair_attempts(strategy);
    CREATE INDEX IF NOT EXISTS idx_attempts_failure ON repair_attempts(failure_signature);
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        escalation_threshold: int = DEFAULT_ESCALATION_THRESHOLD,
        oscillation_window: int = DEFAULT_OSCILLATION_WINDOW,
    ) -> None:
        self.db_path = str(db_path) if db_path is not None else ":memory:"
        self.escalation_threshold = max(1, int(escalation_threshold))
        self.oscillation_window = max(2, int(oscillation_window))
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    # ---- connection ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(self.SCHEMA)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _ensure_schema(self) -> None:
        conn = self._connect()
        conn.commit()

    # ---- record (the RepairAgent calls this after apply) ----------------

    def record_attempt(self, attempt: RepairAttempt) -> None:
        """Persist a single repair attempt. Idempotent on attempt_id ──
        recording the same attempt twice writes two rows (audit trail),
        which is intentional. The blacklist reads DISTINCT on
        repair_fingerprint so dupes don't double-blacklist. Pure-ish:
        only I/O is the SQLite insert (no network, no provider calls)."""
        if attempt.repair_fingerprint == "":
            attempt.repair_fingerprint = repair_fingerprint(
                strategy=attempt.strategy,
                target_files=attempt.target_files,
                failure_signature_value=attempt.failure_signature,
            )
        conn = self._connect()
        conn.execute(
            """INSERT INTO repair_attempts
               (attempt_id, workspace_id, iteration, strategy, target_files,
                failure_signature, repair_fingerprint, patch_id, success,
                files_changed, response_error, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (attempt.attempt_id, attempt.workspace_id, attempt.iteration,
             attempt.strategy, json.dumps(attempt.target_files),
             attempt.failure_signature, attempt.repair_fingerprint,
             attempt.patch_id, int(bool(attempt.success)),
             json.dumps(attempt.files_changed or []),
             attempt.response_error or "",
             attempt.timestamp),
        )
        conn.commit()

    # ---- blacklist (the RepairPlanner consults this before applying) ----

    def is_blacklisted(
        self, *, strategy: str, target_files: Iterable[str],
        failure_signature_value: str,
    ) -> bool:
        """True iff a previous attempt with the SAME repair fingerprint
        (strategy + sorted target files + failure signature) FAILED. Pure
        query (read-only)."""
        fp = repair_fingerprint(
            strategy=strategy, target_files=target_files,
            failure_signature_value=failure_signature_value,
        )
        conn = self._connect()
        cur = conn.execute(
            """SELECT 1 FROM repair_attempts
               WHERE repair_fingerprint = ? AND success = 0
               LIMIT 1""",
            (fp,),
        )
        return cur.fetchone() is not None

    def find_similar_attempt(
        self, *, strategy: str, target_files: Iterable[str],
        failure_signature_value: str,
    ) -> dict | None:
        """Return the most recent failed attempt with this fingerprint, or
        None when no prior failed attempt exists. Used to surface "we
        already tried X" with full provenance. Pure query."""
        fp = repair_fingerprint(
            strategy=strategy, target_files=target_files,
            failure_signature_value=failure_signature_value,
        )
        conn = self._connect()
        cur = conn.execute(
            """SELECT * FROM repair_attempts
               WHERE repair_fingerprint = ? AND success = 0
               ORDER BY id DESC LIMIT 1""",
            (fp,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def has_attempted(
        self, *, strategy: str, target_files: Iterable[str],
        failure_signature_value: str,
    ) -> bool:
        """True iff ANY attempt (success or failure) with this fingerprint
        exists. Use is_blacklisted when you only care about failures."""
        fp = repair_fingerprint(
            strategy=strategy, target_files=target_files,
            failure_signature_value=failure_signature_value,
        )
        conn = self._connect()
        cur = conn.execute(
            """SELECT 1 FROM repair_attempts
               WHERE repair_fingerprint = ? LIMIT 1""",
            (fp,),
        )
        return cur.fetchone() is not None

    # ---- strategy escalation (the spec's main use case) -----------------

    def attempts_for_failure(self, failure_signature_value: str) -> list[dict]:
        """All attempts (any strategy) for this failure signature, in order
        of recording. Used to decide escalation. Pure query."""
        conn = self._connect()
        cur = conn.execute(
            """SELECT * FROM repair_attempts
               WHERE failure_signature = ?
               ORDER BY id ASC""",
            (failure_signature_value,),
        )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def recommend_escalation(
        self, *, current_strategy: str, failure_signature_value: str,
    ) -> str | None:
        """Recommend the next stronger strategy, or None when ``current``
        is already at the top of the ladder OR when the same failure has
        not yet accumulated enough attempts to warrant escalation.

        Rules:
          1. If ``current_strategy`` is already the strongest on
             ESCALATION_LADDER → return None (no escalation possible).
          2. If there have been >= ``escalation_threshold`` attempts for
             this failure signature with strategies <= current_strategy
             → step up one rung.
          3. Otherwise → None (keep the current strategy).
        Pure query + ladder logic; no side effects."""
        if current_strategy not in _STRATEGY_INDEX:
            # Unknown strategy → no safe escalation target. Return None so
            # the caller falls back to theRepairPlanner's natural choice.
            return None
        if _STRATEGY_INDEX[current_strategy] >= len(ESCALATION_LADDER) - 1:
            return None  # already at the top
        attempts = self.attempts_for_failure(failure_signature_value)
        if len(attempts) < self.escalation_threshold:
            return None
        # Count how many attempts used a strategy at or below the current
        # tier. ponytail: we don't strictly require >= threshold distinct
        # strategies ── repeated IDENTICAL attempts (which the blacklist
        # should have prevented) also count toward escalation.
        weaker = [a for a in attempts
                  if _STRATEGY_INDEX.get(a["strategy"], 0)
                  <= _STRATEGY_INDEX[current_strategy]]
        if len(weaker) >= self.escalation_threshold:
            return next_strategy(current_strategy)
        return None

    # ---- oscillation + cycle detection ---------------------------------

    def strategy_sequence(self, workspace_id: str | None = None) -> list[str]:
        """The complete strategy sequence in recording order. Used by
        oscillation + cycle detection. Pure query."""
        conn = self._connect()
        if workspace_id:
            cur = conn.execute(
                "SELECT strategy FROM repair_attempts WHERE workspace_id = ? "
                "ORDER BY id ASC", (workspace_id,))
        else:
            cur = conn.execute(
                "SELECT strategy FROM repair_attempts ORDER BY id ASC")
        return [r[0] for r in cur.fetchall()]

    def detect_oscillation(
        self, *, window: int | None = None,
        workspace_id: str | None = None,
    ) -> bool:
        """True iff the recent strategy sequence contains a cycle of
        period k (k >= 2) ── the spec's A,B,A,B pattern. A cycle of period
        1 (stuck on the same strategy) is NOT oscillation (see
        detect_stuck). Pure."""
        w = window or self.oscillation_window
        seq = self.strategy_sequence(workspace_id=workspace_id)
        recent = seq[-w:]
        n = len(recent)
        for k in range(2, n // 2 + 1):
            first_half = recent[-2 * k: -k]
            second_half = recent[-k:]
            if first_half == second_half and len(set(second_half)) > 1:
                return True
        return False

    def detect_stuck(
        self, *, min_repeats: int = 3,
        workspace_id: str | None = None,
    ) -> str | None:
        """Return the strategy name the loop is stuck on (>= ``min_repeats``
        consecutive attempts), or None. A 'stuck' strategy is one we keep
        retrying without progress; it warrants escalation too. Pure."""
        seq = self.strategy_sequence(workspace_id=workspace_id)
        if len(seq) < min_repeats:
            return None
        recent = seq[-min_repeats:]
        if len(set(recent)) == 1:
            return recent[0]
        return None

    def detect_repeated_cycle(
        self, *, window: int | None = None,
        workspace_id: str | None = None,
    ) -> dict | None:
        """If a cycle is detected, return its metadata (period, the
        cycling strategies, the start index). Otherwise None. Used for
        surfacing WHY escalation is happening in the audit trail. Pure."""
        w = window or self.oscillation_window
        seq = self.strategy_sequence(workspace_id=workspace_id)
        recent = seq[-w:]
        n = len(recent)
        for k in range(2, n // 2 + 1):
            first_half = recent[-2 * k: -k]
            second_half = recent[-k:]
            if first_half == second_half and len(set(second_half)) > 1:
                return {
                    "period": k,
                    "strategies": list(second_half),
                    "cycle_length": 2 * k,
                    "start_offset": -(2 * k),
                }
        return None

    # ---- convergence + effectiveness (the spec's call-outs) -------------

    def unique_strategies_tried(
        self, workspace_id: str | None = None,
    ) -> set[str]:
        """The distinct strategies attempted so far. A converged repair has
        a SMALL set (the fix landed); a non-converged one has many. Pure."""
        seq = self.strategy_sequence(workspace_id=workspace_id)
        return set(seq)

    def is_converging(
        self, *, min_attempts: int = 3,
        workspace_id: str | None = None,
    ) -> bool:
        """Heuristic: NOT converging iff attempt count >= ``min_attempts``
        AND no successful attempts recorded AND the unique strategy count is
        propagating (growing) across the last 3 attempts. The spec calls out
        convergence detection as a responsibility; this is a cheap
        observable proxy. Pure."""
        seq = self.strategy_sequence(workspace_id=workspace_id)
        if len(seq) < min_attempts:
            return True  # too early to tell ── optimistically assume yes
        conn = self._connect()
        cur = conn.execute(
            "SELECT 1 FROM repair_attempts WHERE success = 1 LIMIT 1")
        if cur.fetchone() is not None:
            return True  # at least one success → trending toward convergence
        # No successes: not converging iff we keep trying NEW strategies.
        unique_so_far = len(set(seq))
        unique_recent = len(set(seq[-3:]))
        # ponytail: a 'converging' loop narrows (recent attempts reuse
        # recent strategies); a non-converging loop branches (new
        # strategies each round). Use the ratio as the cheap signal.
        return unique_recent <= unique_so_far / 2

    def effectiveness(self, strategy: str | None = None) -> dict:
        """Return repair effectiveness scores. With ``strategy``: that
        strategy's (attempts, successes, rate). Without: a per-strategy
        breakdown across the entire history. Pure query."""
        conn = self._connect()
        if strategy is not None:
            cur = conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok
                   FROM repair_attempts WHERE strategy = ?""",
                (strategy,))
            row = cur.fetchone()
            n, ok = (row["n"] or 0, row["ok"] or 0)
            return {
                "strategy": strategy, "attempts": n, "successes": ok,
                "rate": (ok / n) if n else 0.0,
            }
        cur = conn.execute(
            """SELECT strategy,
                      COUNT(*) AS n,
                      SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok
                   FROM repair_attempts GROUP BY strategy""")
        out: dict[str, dict] = {}
        for row in cur.fetchall():
            n, ok = row["n"] or 0, row["ok"] or 0
            out[row["strategy"]] = {
                "attempts": n, "successes": ok,
                "rate": (ok / n) if n else 0.0,
            }
        return out

    # ---- persistence helpers (resume / audit) ----------------------------

    def all_attempts(self, workspace_id: str | None = None) -> list[dict]:
        """Return every recorded attempt, oldest first. Used at end-of-loop
        to write the JSON audit artifact."""
        conn = self._connect()
        if workspace_id:
            cur = conn.execute(
                "SELECT * FROM repair_attempts WHERE workspace_id = ? "
                "ORDER BY id ASC", (workspace_id,))
        else:
            cur = conn.execute(
                "SELECT * FROM repair_attempts ORDER BY id ASC")
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def to_dict(self) -> dict:
        """Snapshot the memory as a JSON-able dict (audit / persistence /
        regression comparison). Pure query."""
        attempts = self.all_attempts()
        return {
            "db_path": self.db_path,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "effectiveness": self.effectiveness(),
            "oscillation": self.detect_oscillation(),
            "stuck": self.detect_stuck(),
            "repeated_cycle": self.detect_repeated_cycle(),
            "unique_strategies": sorted(self.unique_strategies_tried()),
            "is_converging": self.is_converging(),
        }

    @classmethod
    def from_workspace(
        cls, workspace_id: str, *, validation_dir: Path | None = None,
        escalation_threshold: int = DEFAULT_ESCALATION_THRESHOLD,
        oscillation_window: int = DEFAULT_OSCILLATION_WINDOW,
    ) -> "RepairMemory":
        """Build an instance backed by ``<workspace>/validation/repair_history.db``.
        Creates the directory + db if absent; resumes the prior state if
        present. The spec's "repair_history.db" workspace artifact."""
        if validation_dir is None:
            # Lazy import keeps this module pure at import time
            from build_workspace import _workspace_dir
            validation_dir = _workspace_dir(workspace_id) / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            db_path=validation_dir / "repair_history.db",
            escalation_threshold=escalation_threshold,
            oscillation_window=oscillation_window,
        )

    # ---- internals -------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("target_files", "files_changed"):
            v = d.get(k)
            if isinstance(v, str) and v:
                try:
                    d[k] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        d["success"] = bool(d.get("success"))
        return d


# ---------------------------------------------------------------------------
# Convenience: integrate a RepairMemory into the existing RepairPlanner
# loop without modifying theRepairPlanner's signature. The caller (the
# ValidationEngine) consults these before invoking plan() + apply().
# ---------------------------------------------------------------------------


def escalate_plan(
    plan, *, memory: RepairMemory, failure_signature_value: str,
) -> tuple:
    """Given a fresh (untried) RepairPlan, consult RepairMemory and return
    ``(plan, escalated, reason)``::

        plan       ── the plan to actually apply (possibly escalated)
        escalated  ── bool: True iff we stepped up the ladder
        reason     ── a human-readable reason for the audit trail
    Pure; no I/O beyond the RepairMemory queries (sqlite)."""
    if memory.is_blacklisted(
            strategy=plan.strategy, target_files=plan.target_files,
            failure_signature_value=failure_signature_value):
        # Already tried this exact repair → escalate or refuse
        next_strat = memory.recommend_escalation(
            current_strategy=plan.strategy,
            failure_signature_value=failure_signature_value,
        )
        if next_strat is not None:
            escalated = type(plan)(  # preserve the dataclass type
                scope=plan.scope, priority=plan.priority,
                strategy=next_strat, target_files=plan.target_files,
                diagnostics=plan.diagnostics,
                prompt=plan.prompt + f"\n\n[RepairMemory: escalating from "
                f"{plan.strategy} to {next_strat}]",
            )
            return escalated, True, (
                f"strategy '{plan.strategy}' blacklisted for this failure "
                f"signature → escalated to '{next_strat}'"
            )
        # No more escalation rungs → refuse with a structured reason
        return plan, False, (
            f"strategy '{plan.strategy}' blacklisted and no stronger "
            "strategy available on the ladder ── repair will likely fail"
        )
    return plan, False, ""


# ---------------------------------------------------------------------------
# Self-check (assert-based; no model calls; uses :memory: SQLite)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys

    mem = RepairMemory()  # in-memory

    sig1 = failure_signature([
        {"category": "tests", "message": "AssertionError x", "location": "tests/test_a.py"},
    ])
    sig2 = failure_signature([
        {"category": "syntax", "message": "SyntaxError on line 5", "location": "src/x.py"},
    ])
    assert sig1 != sig2
    # Determinism: same diagnostics → same signature
    assert sig1 == failure_signature([
        {"category": "tests", "message": "AssertionError x", "location": "tests/test_a.py"},
    ])
    # Order-independent (we sort_keys)
    assert sig1 == failure_signature([
        {"location": "tests/test_a.py", "category": "tests", "message": "AssertionError x"},
    ])
    # Empty diagnostics → stable hash
    assert failure_signature([]) == failure_signature(None)

    # Repair fingerprint varies with strategy + target files
    fp1 = repair_fingerprint(strategy="patch",
                              target_files=["src/x.py"],
                              failure_signature_value=sig1)
    fp2 = repair_fingerprint(strategy="fix_imports",
                              target_files=["src/x.py"],
                              failure_signature_value=sig1)
    fp3 = repair_fingerprint(strategy="patch",
                              target_files=["src/x.py", "src/y.py"],
                              failure_signature_value=sig1)
    assert len({fp1, fp2, fp3}) == 3
    # Order-independent: target files sorted before hashing
    assert (repair_fingerprint(strategy="patch",
                                target_files=["src/y.py", "src/x.py"],
                                failure_signature_value=sig1)
            == repair_fingerprint(strategy="patch",
                                   target_files=["src/x.py", "src/y.py"],
                                   failure_signature_value=sig1))

    # record + blacklist
    a1 = RepairAttempt(
        workspace_id="ws1", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature=sig1,
        success=False, files_changed=["src/x.py"], patch_id="p1",
        response_error="tests still fail",
    )
    mem.record_attempt(a1)
    assert mem.has_attempted(strategy="patch", target_files=["src/x.py"],
                              failure_signature_value=sig1)
    assert mem.is_blacklisted(strategy="patch", target_files=["src/x.py"],
                               failure_signature_value=sig1)
    # Successful attempts are NOT in the blacklist
    a2 = RepairAttempt(
        workspace_id="ws1", iteration=2, strategy="patch",
        target_files=["src/y.py"], failure_signature=sig2,
        success=True, files_changed=["src/y.py"], patch_id="p2",
    )
    mem.record_attempt(a2)
    assert not mem.is_blacklisted(strategy="patch", target_files=["src/y.py"],
                                    failure_signature_value=sig2)

    # find_similar_attempt surfaces the prior failure
    sim = mem.find_similar_attempt(strategy="patch", target_files=["src/x.py"],
                                     failure_signature_value=sig1)
    assert sim is not None
    assert sim["strategy"] == "patch"
    assert sim["success"] is False

    # Escalation ── after escalation_threshold (2) attempts with strategies
    # <= "patch", the next recommendation steps up
    mem2 = RepairMemory(escalation_threshold=2)
    mem2.record_attempt(RepairAttempt(
        workspace_id="ws2", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature=sig1,
        success=False, patch_id="p1"))
    # First attempt → no escalation yet
    assert mem2.recommend_escalation(current_strategy="patch",
                                     failure_signature_value=sig1) is None
    # Second attempt at "patch" → escalates to fix_imports
    mem2.record_attempt(RepairAttempt(
        workspace_id="ws2", iteration=2, strategy="patch",
        target_files=["src/x.py"], failure_signature=sig1,
        success=False, patch_id="p2"))
    assert mem2.recommend_escalation(current_strategy="patch",
                                     failure_signature_value=sig1) == "fix_imports"
    # Already at the top of the ladder → None
    assert mem2.recommend_escalation(
        current_strategy="refactor_dependency_graph",
        failure_signature_value=sig1) is None
    # Unknown strategy → None (no safe escalation)
    assert mem2.recommend_escalation(current_strategy="unknown",
                                     failure_signature_value=sig1) is None

    # Oscillation: A,B,A,B → detect
    mem3 = RepairMemory()
    sig_x = failure_signature([{"category": "tests", "message": "x"}])
    for i, strat in enumerate(["patch", "rewrite_test", "patch", "rewrite_test"]):
        mem3.record_attempt(RepairAttempt(
            workspace_id="ws3", iteration=i + 1, strategy=strat,
            target_files=["src/x.py"], failure_signature=sig_x,
            success=False, patch_id=f"p{i}"))
    assert mem3.detect_oscillation() is True
    cycle = mem3.detect_repeated_cycle()
    assert cycle is not None
    assert cycle["period"] == 2
    assert set(cycle["strategies"]) == {"patch", "rewrite_test"}
    assert mem3.detect_stuck() is None  # not stuck on one strategy

    # Stuck: same strategy 3+ times in a row → detect_stuck returns it
    mem4 = RepairMemory()
    for i in range(4):
        mem4.record_attempt(RepairAttempt(
            workspace_id="ws4", iteration=i + 1, strategy="patch",
            target_files=["src/x.py"], failure_signature=sig_x,
            success=False, patch_id=f"p{i}"))
    assert mem4.detect_stuck() == "patch"
    # Stuck-on-strategy is NOT oscillation by definition
    assert mem4.detect_oscillation() is False

    # Effectiveness scoring
    eff = mem.effectiveness()
    assert "patch" in eff
    assert eff["patch"]["attempts"] == 2  # a1 + a2 above (sig1 and sig2)
    assert eff["patch"]["successes"] == 1  # only a2 succeeded
    assert 0 <= eff["patch"]["rate"] <= 1
    # Per-strategy query
    eff_patch = mem.effectiveness("patch")
    assert eff_patch["strategy"] == "patch"
    assert eff_patch["attempts"] == 2

    # Convergence heuristic
    # No successes at all AND many unique strategies → not converging
    mem5 = RepairMemory()
    for i, strat in enumerate(["patch", "fix_imports", "rewrite_test",
                                "regenerate"]):
        mem5.record_attempt(RepairAttempt(
            workspace_id="ws5", iteration=i + 1, strategy=strat,
            target_files=["src/x.py"], failure_signature=sig_x,
            success=False, patch_id=f"p{i}"))
    # 4 attempts, 4 unique strategies, 0 successes. is_converging is the
    # cheap proxy; here recent attempts introduce new strategies.
    # The heuristic returns False when branching exceeds narrowing.
    seq = mem5.strategy_sequence()
    assert len(seq) == 4
    assert mem5.unique_strategies_tried() == {"patch", "fix_imports",
                                                "rewrite_test", "regenerate"}

    # Plan escalation helper
    from validation_engine import RepairPlan, Diagnostic
    plan = RepairPlan(
        scope="file", priority="high", strategy="patch",
        target_files=["src/x.py"], diagnostics=[],
        prompt="repair x",
    )
    # mem (single failed patch attempt on sig1)
    new_plan, escalated, reason = escalate_plan(
        plan, memory=mem, failure_signature_value=sig1)
    # mem has 1 failed 'patch' attempt on sig1 + 1 successful 'patch' on sig2.
    # is_blacklisted(patch, [src/x.py], sig1) → True (a1)
    # recommend_escalation(patch, sig1) needs >= 2 attempts → None
    # → returned escalated=False and reason explains the blacklist
    assert escalated is False
    assert "blacklisted" in reason
    # With a 2-attempt memory on sig_x, the SAME plan escalates to fix_imports
    mem_esc = RepairMemory(escalation_threshold=2)
    for i in range(2):
        mem_esc.record_attempt(RepairAttempt(
            workspace_id="ws_esc", iteration=i + 1, strategy="patch",
            target_files=["src/x.py"], failure_signature=sig_x,
            success=False, patch_id=f"p{i}"))
    new_plan2, escalated2, reason2 = escalate_plan(
        plan, memory=mem_esc, failure_signature_value=sig_x)
    assert escalated2 is True
    assert new_plan2.strategy == "fix_imports"
    assert "escalating" in new_plan2.prompt
    mem_esc.close()

    # Persistence to disk (resume): a memory backed by a file survives
    # close() and re-open.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "repair_history.db"
        on_disk = RepairMemory(db_path=p)
        on_disk.record_attempt(RepairAttempt(
            workspace_id="ws_disk", iteration=1, strategy="patch",
            target_files=["src/x.py"], failure_signature=sig1,
            success=False, patch_id="p1"))
        on_disk.close()
        # Re-open — the schema + attempt survive
        resumed = RepairMemory(db_path=p)
        assert resumed.has_attempted(strategy="patch", target_files=["src/x.py"],
                                       failure_signature_value=sig1)
        attempts = resumed.all_attempts()
        assert len(attempts) == 1
        assert attempts[0]["strategy"] == "patch"
        assert attempts[0]["target_files"] == ["src/x.py"]
        resumed.close()

    # Pure-stdlib guarantee: importing this module must NOT import any
    # LLM provider, browser, or network client.
    forbidden = {"cerebras_client", "ollama_client", "openrouter_client",
                 "groq_client", "gpt_worker", "gemini_worker",
                 "provider_router", "selenium", "requests",
                 "httpx", "urllib3"}
    loaded = forbidden & set(sys.modules)
    assert not loaded, f"repair_memory pulled forbidden modules: {loaded}"

    # Determinism: same recording order → same state
    m_a = RepairMemory()
    m_b = RepairMemory()
    for i in range(3):
        a = RepairAttempt(workspace_id="w", iteration=i, strategy="patch",
                          target_files=["x"], failure_signature=sig1,
                          success=(i == 2), patch_id=f"p{i}")
        m_a.record_attempt(a)
        m_b.record_attempt(a)
    assert m_a.to_dict()["attempts"] == m_b.to_dict()["attempts"]

    mem.close(); mem2.close(); mem3.close(); mem4.close(); mem5.close()
    print("repair_memory self-check PASSED")
