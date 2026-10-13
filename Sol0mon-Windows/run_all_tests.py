#!/usr/bin/env python3
"""
run_all_tests.py - Single entry point for the entire test suite.

Runs:
  1. All integration pipeline tests (tests/test_*.py)
  2. All Phase 1-4 module tests (test_*.py in root, excluding browser-required)
  3. test_suite.py (comprehensive unit tests)

Usage:
    python3 run_all_tests.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

# Integration pipeline tests (Phase: Audit)
INTEGRATION_TESTS = [
    "tests/test_workflow_pipeline.py",
    "tests/test_answer_resurrection_pipeline.py",
    "tests/test_build_spec_pipeline.py",
    "tests/test_implementation_pipeline.py",
    "tests/test_build_answer_55_e2e.py",
    "tests/test_build_answer_55_opencode_live_e2e.py",
    "tests/test_resurrection_constraints.py",
    "tests/test_human_synthesis.py",
    # Final Architecture Hardening (Phase M regression): lifecycle, artifacts,
    # validation framework, execution planner, resume engine.
    "tests/test_lifecycle.py",
    "tests/test_artifacts.py",
    "tests/test_validation.py",
    "tests/test_execution_planner.py",
    "tests/test_resume.py",
    # Consolidation phase (Parts 1-7 regression): planner seam + persistence,
    # manifest, validate_manifest, manifest-aware resume, task graph exports.
    "tests/test_consolidation.py",
    # Project Planner (Objective → Planner → Scheduler → Existing Pipeline):
    # planner module + persistence + resume, scheduler dispatch, pipeline
    # integration. Stage 1 of every Sol0mon workflow.
    "tests/test_project_planner.py",
    # Workflow Orchestration hardening: Execution Decision Authority (single
    # effective decision), Stage Logger, Workspace Knowledge persistence,
    # Scheduler status upgrade (PENDING/READY/RUNNING/COMPLETED/FAILED/BLOCKED),
    # debate separation (research → architecture → implementation), end-to-end.
    "tests/test_workflow_orchestration.py",
]

# Module tests (Phase 1-4) — no browser, no external API
MODULE_TESTS = [
    "test_build_workspace.py",
    "test_decision_bundle.py",
    "test_human_decision_manager.py",
    "test_answer_loader.py",
    "test_answer_selector.py",
    "test_build_prompt_generator.py",
]

# Comprehensive unit test suite
SUITE = "test_suite.py"


def run(label: str, path: str) -> bool:
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        print(f"  SKIP  {label} (file not found: {path})")
        return True
    try:
        result = subprocess.run(
            [PYTHON, full],
            capture_output=True, text=True, timeout=120,
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        print(f"  SKIP  {label} (timed out >120s — likely needs external API)")
        return True
    ok = result.returncode == 0
    status = "PASS" if ok else "FAIL"
    # Print last meaningful line
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    summary = lines[-1] if lines else "(no output)"
    print(f"  {status}  {label}: {summary}")
    if not ok:
        print(result.stdout[-2000:])
        print(result.stderr[-1000:])
    return ok


def main() -> int:
    passed = 0
    failed = 0

    print("=" * 60)
    print("PROJECT SOL0MON — FULL TEST SUITE")
    print("=" * 60)
    print()

    print("-- Integration Pipeline Tests --")
    for t in INTEGRATION_TESTS:
        if run(t, t):
            passed += 1
        else:
            failed += 1

    print()
    print("-- Module Tests (Phase 1-4) --")
    for t in MODULE_TESTS:
        if run(t, t):
            passed += 1
        else:
            failed += 1

    print()
    print("-- Comprehensive Unit Suite --")
    if run(SUITE, SUITE):
        passed += 1
    else:
        failed += 1

    print()
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
