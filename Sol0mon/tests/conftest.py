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


@pytest.fixture
def answer55_db(tmp_path):
    """Seed a deterministic answer 55 in a private database for regression tests."""
    import answer_memory
    from test_deterministic_decisions import debate55_verification_result

    path = tmp_path / "answer_memory.db"
    previous_path = answer_memory.DB_FILE
    answer_memory.DB_FILE = str(path)
    answer_memory.memory._initialized = False
    for index in range(54):
        answer_memory.memory.save_debate(
            question=f"fixture filler {index}", gpt_final="g", gemini_final="m",
            synthesized_answer="fixture", consensus_score=0,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
        )
    answer_id = answer_memory.memory.save_debate(
        question="Design the lowest-cost autonomous multi-agent platform",
        gpt_final="g", gemini_final="m", synthesized_answer="fixture answer",
        consensus_score=99, conflict_severity="LOW", conflict_points=[],
        validated_disputes=[],
        verification_result=debate55_verification_result(),
    )
    assert answer_id == 55
    yield answer_id
    answer_memory.DB_FILE = previous_path
    answer_memory.memory._initialized = False


@pytest.fixture(autouse=True)
def _isolate_sol0mon_module_globals():
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
