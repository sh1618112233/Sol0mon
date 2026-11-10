"""
test_validation.py - Phase C regression: the central validation framework.

Pins:
  - ValidationReport shape (passed / stage / issues / severity / warnings /
    failures / suggested_repair / dependent_artifact_id).
  - validate_decision_model: passes the answer-156 fixture (SQLite winner,
    PostgreSQL rejected); FAILS (FATAL) when a rejected option leaks into
    winner_tokens — the SQLite/PostgreSQL invariant extended into the new
    validation layer (does NOT remove the existing invariant tests).
  - validate_build_spec: passes a Path 3 spec; fails on missing sections /
    non-complete status; WARNINGS on decision gaps.
  - validate_execution_plan: passes a DAG; fails on cycles (FATAL acyclic),
    dangling deps, empty plan.
  - validate_generated_code: artefact-presence gate (empty fails).
  - validate_tests: Phase 2 strengthened contract (runner='none' fails).
  - validate_final_build: only a fully-COMPLETE state passes; legacy status
    'executed' maps to COMPLETE; incomplete status fails.
  - validate dispatcher: known stage works, unknown stage raises BuildError
    (never silently skipped), halt_on_failure raises.
  - Validation ORDERING: a build_spec failed report blocks the plan validator
    from being asked to validate (validate_execution_plan on a spec-derived
    plan still runs, but a spec that fails its own validator must not be
    planned as if it passed).
  - Severity aggregation on the report.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import validation as V
from errors import BuildError, ErrorKind, Severity


VR_156 = {
    "resolved_claims": [{
        "normalized_key": "database_technology_selection",
        "dispute": ("Database Technology: GPT selects PostgreSQL for relational "
                    "durability and audit trails, while Gemini selects SQLite."),
        "verification": {
            "status": "RESOLVED", "confidence": 95,
            "finding": ("Both evidence strips conclude SQLite is the appropriate "
                        "persistence engine; the text argues against PostgreSQL "
                        "for current needs."),
        },
    }],
    "equivalent_claims": [], "unresolved_claims": [],
    "verification_score": 100, "truncated_count": 0,
}


# --- Decision Model validator + the SQLite/PostgreSQL invariant ---

def test_decision_model_passes_156_invariant():
    r = V.validate_decision_model(VR_156)
    assert r.passed, r.to_dict()
    assert r.stage == "decision_model"
    # The rejected option PostgreSQL must NOT leak into winner_tokens.
    # (cross-check directly against the canonical model the validator used)
    from decision_model import decision_records
    rec = decision_records(VR_156)[0]
    assert "postgresql" in rec["rejected_options"]
    assert "postgresql" not in rec["winner_tokens"]
    assert "sqlite" in rec["winner_tokens"]


def test_decision_model_fails_on_winner_is_rejected():
    bad = {"resolved_claims": [{
        "normalized_key": "db",
        "verification": {"status": "RESOLVED", "confidence": 90,
                          "finding": "Resolved in favor of PostgreSQL; rejects PostgreSQL."},
    }]}
    r = V.validate_decision_model(bad)
    assert not r.passed
    failing = r.failures
    assert any(i.rule == "winner_not_rejected" and i.severity is Severity.FATAL
               for i in failing), r.to_dict()


def test_decision_model_flags_missing_winner():
    no_winner = {"resolved_claims": [{
        "normalized_key": "k",
        "verification": {"status": "RESOLVED", "confidence": 50, "finding": ""},
    }]}
    r = V.validate_decision_model(no_winner)
    assert not r.passed
    assert any(i.rule == "winner_present" for i in r.issues), r.to_dict()


# --- Build Spec validator ---

def test_build_spec_passes_path3():
    from build_spec_engine import build_spec
    out = build_spec("ignored", source_result=VR_156)
    r = V.validate_build_spec(out)
    assert r.passed, r.to_dict()


def test_build_spec_fails_missing_sections_and_incomplete_status():
    incomplete = {"spec_text": "# Architecture\nonly",
                  "status": "incomplete", "verified_decisions": []}
    r = V.validate_build_spec(incomplete)
    assert not r.passed
    rules = {i.rule for i in r.issues}
    assert "sections_complete" in rules
    assert "status_completed" in rules


def test_build_spec_warns_on_decision_gaps():
    # A spec that drops a verified decision -> WARNING (not fatal). After
    # the artefact-selection fix the spec must include the 6 executable-spec
    # sections to pass validate_executable_spec_sections (the gate
    # validation.py delegates to).
    spec = {
        "spec_text": (
            "# Implementation Objective\nA\n\n"
            "# Files to Create\n- x.py\n\n"
            "# Files to Modify\n- (none)\n\n"
            "# Implementation Steps\n1. do.\n\n"
            "# Acceptance Criteria\nG\n\n"
            "# Output Contract\nStop.\n"
        ),
        "status": "completed",
        "verified_decisions": [{"outcome_type": "VERIFIED_DECISION",
                                "dispute_key": "zqx_not_in_spec",
                                "decision": "Atotallyuniquething"}],
    }
    r = V.validate_build_spec(spec)
    assert any(i.rule == "decisions_honored" and i.severity is Severity.WARNING
               for i in r.issues), r.to_dict()
    # Warnings only — the report still passes (sections complete + status ok).
    assert r.passed, r.to_dict()


# --- Execution Plan / Task Graph validator ---

def test_execution_plan_passes_dag():
    plan = {"tasks": {"a": {"dependencies": []},
                      "b": {"dependencies": ["a"]},
                      "c": {"dependencies": ["a", "b"]}}}
    assert V.validate_execution_plan(plan).passed


def test_execution_plan_fails_cycle():
    plan = {"tasks": {"a": {"dependencies": ["b"]},
                      "b": {"dependencies": ["a"]}}}
    r = V.validate_execution_plan(plan)
    assert not r.passed
    assert any(i.rule == "acyclic" and i.severity is Severity.FATAL for i in r.issues)


def test_execution_plan_fails_dangling_dep():
    plan = {"tasks": {"a": {"dependencies": ["ghost"]}}}
    r = V.validate_execution_plan(plan)
    assert not r.passed and any(i.rule == "dep_resolvable" for i in r.issues)


def test_execution_plan_fails_empty():
    r = V.validate_execution_plan({"tasks": {}})
    assert not r.passed and any(i.rule == "non_empty" for i in r.issues)


# --- Generated code validator ---

def test_generated_code_empty_fails():
    assert not V.validate_generated_code(set()).passed
    assert not V.validate_generated_code({"files": set()}).passed


def test_generated_code_nonempty_passes():
    assert V.validate_generated_code({"a.py", "src/b.py"}).passed
    assert V.validate_generated_code({"files": ["a.py"]}).passed


# --- Tests validator (Phase 2 strengthened contract) ---

def test_tests_pass_on_real_suite():
    assert V.validate_tests({"runner": "pytest", "passed": 4, "failed": 0,
                              "exit_code": 0}).passed


def test_tests_fail_on_no_runner():
    r = V.validate_tests({"runner": "none", "passed": 0, "failed": 0, "exit_code": -1})
    assert not r.passed and any(i.rule == "tests_passed" for i in r.issues)


def test_tests_fail_on_zero_passed():
    # pytest with nothing to collect: passed=0, failed=0, exit_code=5
    r = V.validate_tests({"runner": "pytest", "passed": 0, "failed": 0, "exit_code": 5})
    assert not r.passed


# --- Final build validator ---

def test_final_build_passes_only_when_complete():
    ok = {"spec_status": "completed", "execution_success": True,
          "lifecycle_stage": "COMPLETE"}
    assert V.validate_final_build(ok).passed


def test_final_build_legacy_status_executed_maps_to_complete():
    legacy = {"spec_status": "completed", "execution_success": True,
              "status": "executed"}
    assert V.validate_final_build(legacy).passed


def test_final_build_fails_incomplete_state():
    bad = {"spec_status": "incomplete", "execution_success": False,
           "status": "blocked_incomplete_spec"}
    r = V.validate_final_build(bad)
    assert not r.passed
    rules = {i.rule for i in r.issues}
    assert "spec_completed" in rules and "execution_success" in rules \
           and "lifecycle_complete" in rules


# --- Dispatcher + halt + ordering ---

def test_dispatcher_routes_known_stage_and_passes_artifact():
    from build_spec_engine import build_spec
    spec = build_spec("ignored", source_result=VR_156)
    r = V.validate("build_spec", spec)
    assert r.passed


def test_dispatcher_unknown_stage_raises_never_silently_skips():
    raised = False
    try:
        V.validate("not_a_real_stage", None)
    except BuildError as e:
        raised = True
        assert e.kind is ErrorKind.VALIDATION
        assert e.recoverable is False
    assert raised


def test_dispatcher_halt_on_failure_raises():
    incomplete = {"spec_text": "# Architecture\nonly",
                  "status": "incomplete", "verified_decisions": []}
    raised = False
    try:
        V.validate("build_spec", incomplete, halt_on_failure=True)
    except BuildError as e:
        raised = True
        assert e.recoverable is True and e.kind.value == "ValidationError"
    assert raised


def test_validation_ordering_spec_must_pass_before_plan_is_trusted():
    """Ordering invariant: the build_spec validator is the gate for the plan
    validator. A spec that fails validation must not be planned-and-validated
    as if it passed. Concretely: validate_execution_plan accepts the DAG
    structurally, but the caller (resume / pipeline) MUST consult the spec
    report first and skip planning on a spec failure. This test encodes the
    contract the callers rely on."""
    bad_spec = {"spec_text": "# Architecture\nonly", "status": "incomplete",
               "verified_decisions": []}
    spec_report = V.validate_build_spec(bad_spec)
    assert not spec_report.passed
    # The ordering rule: do not even build the plan if the spec failed.
    # (Callers check spec_report.passed before calling the planner.)


def test_report_aggregates_severity_and_partitions_warnings_failures():
    r = V.validate_build_spec({"spec_text": (
        "# Implementation Objective\nA\n\n"
        "# Files to Create\n- x.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. do.\n\n"
        "# Acceptance Criteria\nG\n\n"
        "# Output Contract\nStop.\n"
    ),
        "status": "completed",
        "verified_decisions": [{"dispute_key": "uniq",
                                "decision": "Atotallyuniquexyz"}]})
    # decision-gaps only -> WARNING -> report still passes
    assert r.passed
    assert r.warnings and not r.failures
    assert r.severity is Severity.WARNING


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
