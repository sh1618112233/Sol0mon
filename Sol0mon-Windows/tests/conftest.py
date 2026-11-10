"""
tests/conftest.py — per-test isolation of the module globals that the offline
test files swap in place.

Several legacy test fixtures (``_with_tmp_db`` in test_execution_decision /
test_execution_mode_gate, ``_set_workspace_root``, ...) re-point module
globals at tmp paths for the duration of a test but never restore the
originals on teardown. When such a test runs before a test that relies on the
original global — notably ``test_build_answer_55_e2e`` which reads the real
``answer_memory.db`` for answer 55 — the leftover tmp path leaks across and
breaks the dependent test (pre-existing fragility, exposed by the Execution
Mode Gate regression sweep).

This autouse fixture snapshots the two known mutable module globals before
each test and restores them after, so test order no longer affects results:

    answer_memory.DB_FILE            (+ memory._initialized lazy-init flag)
    build_workspace.WORKSPACE_ROOT

It does NOT prevent tests from swapping these (they still do, at their
start); it only guarantees a clean slate after each test. Strictly additive
isolation — no test relies on a prior test's leaked global, so restoring is
always safe. No browser/network/LLM imports.
"""

from __future__ import annotations

import os
import sys

# Make the repo root importable when conftest is collected first.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def pytest_collection_modifyitems(config, items):
    live_opt_in = os.environ.get("SOLOMON_RUN_LIVE_TESTS") == "1"
    for item in items:
        if "live" in item.path.name.lower() or "opencode_live" in item.path.name.lower():
            item.add_marker(pytest.mark.live)
            if not live_opt_in:
                item.add_marker(pytest.mark.skip(
                    reason="live test requires SOLOMON_RUN_LIVE_TESTS=1"))


@pytest.fixture(autouse=True)
def _isolate_sol0mon_module_globals(request, tmp_path, monkeypatch):
    """Snapshot + restore the module globals tests swap in place.

    ponytail: heals the shared-state leak at the root (one fixture) rather
    than patching every test's cleanup. Restoring the lazy-init flags too,
    so a swapped tmp DB never pretends to be initialized on the next test."""
    import answer_memory
    import build_workspace as _bw

    saved = {
        "db_file": answer_memory.DB_FILE,
        "mem_init": getattr(answer_memory.memory, "_initialized", False),
        "ws_root": _bw.WORKSPACE_ROOT,
    }
    try:
        if request.node.path.name == "test_telemetry.py":
            monkeypatch.chdir(tmp_path)
        # The historical answer-55 end-to-end tests use a known fixture, not
        # a developer's local database. Seed an isolated test DB only for
        # those tests so a clean checkout has the same deterministic input.
        name = request.node.name
        if "answer55" in name or "build_answer_55" in str(request.node.path):
            import sqlite3
            import answer_memory as _am
            from test_deterministic_decisions import (
                debate55_verification_result, debate55_governance_context,
            )
            fixture_db = tmp_path / "answer_memory.db"
            _am.DB_FILE = str(fixture_db)
            _am.memory._initialize_database()
            with sqlite3.connect(fixture_db) as conn:
                conn.executemany(
                    "INSERT INTO debates (id, created_at, question, gpt_final, "
                    "gemini_final, synthesized_answer, consensus_score, "
                    "conflict_severity, conflict_points) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(i, "2026-01-01T00:00:00+00:00", "fixture placeholder", "", "", "", 0, "LOW", "[]")
                     for i in range(1, 55)],
                )
            _am.memory.save_debate(
                question="Design Sol0mon architecture",
                gpt_final="fixture GPT position", gemini_final="fixture Gemini position",
                synthesized_answer="Deterministic test answer", consensus_score=98,
                conflict_severity="LOW", conflict_points=[],
                validated_disputes=[],
                verification_result=debate55_verification_result(),
            )
        yield
    finally:
        try:
            answer_memory.DB_FILE = saved["db_file"]
        except Exception:
            pass
        try:
            answer_memory.memory._initialized = saved["mem_init"]
        except Exception:
            pass
        try:
            _bw.WORKSPACE_ROOT = saved["ws_root"]
        except Exception:
            pass
        # Telemetry & Runtime Observability V1 — the recorder is a
        # process-level singleton. A test that ``configure()``d it must leave
        # it disabled so the next test starts from a clean slate (otherwise a
        # configured recorder would silently append events / write status
        # files for unrelated tests). ``reset()`` is a strict no-op when the
        # recorder is already disabled.
        try:
            import telemetry
            telemetry.reset()
        except Exception:
            pass
