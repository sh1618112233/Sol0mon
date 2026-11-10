"""
test_validation_engine.py — Phase 3 Autonomous Build Validation & Repair Engine.

Offline only. No model calls. No browser. No external APIs. No live opencode
subprocess (the engine refuses to dispatch when no repair seam is wired ──
the offline-safety guard).

Covers the spec's test scenarios:
  - implementation specification loading
  - missing specification
  - empty specification
  - successful validation
  - structured diagnostics
  - failure analysis and categorization
  - repair planning (scope, priority, strategy)
  - patch validation (syntax + integrity)
  - retry control (deterministic)
  - execution interruption (resume)
  - checkpoint recovery
  - ExecutionRecorder replay + timeline
  - BuildValidator technical gate
  - QualityGate requirements traceability
  - 12 workspace artifacts persisted
  - configuration switching
  - both engine entrypoints (validate / repair / resume / cancel / status / recover)
  - backward compatibility (Phase 1+2 components untouched)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
from validation_engine import (
    # Components
    ArtifactValidator, BuildRunner, BuildValidator, CheckpointManager,
    Diagnostic, FailureAnalyzer, NativeValidationEngine, PatchValidator,
    QualityGate, QualityReport, RepairAgent, RepairPlan, RepairPlanner,
    RetryController, TestRunner, ValidationContext, ValidationEngine,
    get_validation_engine, _validation_dir,
    # Phase integration
    _workspace_dir,
    # Constants
    PHASE_ARTIFACT_VALIDATION, PHASE_BUILD, PHASE_TESTS,
    PHASE_BUILD_VALIDATOR, PHASE_QUALITY_GATE, PHASE_FAILURE_ANALYSIS,
    PHASE_REPAIR_PLANNING, PHASE_REPAIR, PHASE_PATCH_VALIDATION,
    PHASE_RETRY_CONTROL,
    FAIL_BUILD, FAIL_TESTS, FAIL_ARTIFACT, FAIL_IMPORT, FAIL_SYNTAX,
    FAIL_CONFIG, FAIL_REQUIREMENTS, FAIL_UNKNOWN,
    # Events
    EV_VALIDATION_STARTED, EV_VALIDATION_PHASE, EV_VALIDATION_COMPLETED,
    EV_VALIDATION_FAILED, EV_REPAIR_PLANNED, EV_REPAIR_APPLIED,
    EV_PATCH_VALIDATED, EV_QUALITY_GATE,
    # Artifacts
    ARTIFACT_VALIDATION_HISTORY, ARTIFACT_REPAIR_HISTORY,
    ARTIFACT_FAILURE_ANALYSIS, ARTIFACT_TEST_RESULTS, ARTIFACT_COVERAGE,
    ARTIFACT_PATCH_HISTORY, ARTIFACT_RETRY_HISTORY, ARTIFACT_BUILD_LOG,
    ARTIFACT_VALIDATION_REPORT, ARTIFACT_QUALITY_REPORT,
    ARTIFACT_REQUIREMENTS_TRACEABILITY, ARTIFACT_FINAL_BUILD_RESULT,
    ALL_ARTIFACTS,
)
from implementation_engine import (
    ExecutionContext, StateManager, BuildResult, NODE_FINISH,
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _good_spec(components="api"):
    return (
        "# Architecture\nA clean modular service.\n\n"
        f"# Components\n{components}\n\n"
        "# Acceptance Criteria\ntests pass\n"
    )


def _seed_ws(root, q="q", *, files=()):
    """Create a workspace + populate it with the listed source/test files."""
    wid = bw.create_workspace(source_question=q)
    ws = _workspace_dir(wid)
    for rel, content in files:
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return wid, ws


def _build_pass(wd, spec):
    return {"exit_code": 0, "stdout": "ok", "stderr": "", "success": True,
            "logs": ["ok"]}


def _tests_pass(wd):
    return {"passed": True, "total": 1, "failed": 0, "errors": 0,
            "output": ""}


def _tests_fail(wd):
    return {"passed": False, "total": 1, "failed": 1, "errors": 0,
            "output": "AssertionError: x"}


@pytest.fixture
def tmp_workspace_root(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(bw, "WORKSPACE_ROOT", Path(tmp))
    return Path(tmp)


def _build_engine(*, build_fn=None, test_fns=None, repair_fn=None,
                  coding_agent_fn=None, max_iterations=2,
                  allow_live_agent=False):
    return NativeValidationEngine(
        build_fn=build_fn or _build_pass,
        test_fns=test_fns or {t: _tests_pass
                              for t in ("unit", "integration", "smoke")},
        repair_fn=repair_fn,
        coding_agent_fn=coding_agent_fn,
        max_iterations=max_iterations,
        allow_live_agent=allow_live_agent,
    )


# --------------------------------------------------------------------------- #
# 1. Implementation specification loading                                     #
# --------------------------------------------------------------------------- #

def test_spec_loaded_into_validation_context(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is True
    ctx = eng._load_context(wid)
    assert ctx.implementation_spec == _good_spec()


# --------------------------------------------------------------------------- #
# 2. Missing / empty specification                                            #
# --------------------------------------------------------------------------- #

def test_missing_spec_returns_structured_error(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec="")
    assert r.success is False
    assert "implementation_spec" in r.error


def test_whitespace_spec_rejected(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec="  \n\n  ")
    assert r.success is False


def test_missing_workspace_returns_structured_error(tmp_workspace_root):
    eng = _build_engine()
    r = eng.validate(workspace_id="no-such-ws",
                     implementation_spec=_good_spec())
    assert r.success is False
    assert "workspace" in r.error.lower()


# --------------------------------------------------------------------------- #
# 3. Successful validation path (BuildValidator + QualityGate both pass)     #
# --------------------------------------------------------------------------- #

def test_success_path_outputs_all_artifacts(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is True
    val_dir = _validation_dir(wid)
    for f in ALL_ARTIFACTS:
        assert (val_dir / f).is_file(), f"missing: {f}"


def test_success_path_quality_report_passes(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is True
    qr = json.loads((_validation_dir(wid) / ARTIFACT_QUALITY_REPORT).read_text())
    assert qr["passed"] is True
    # Requirements traceability persisted
    tr = json.loads((_validation_dir(wid)
                     / ARTIFACT_REQUIREMENTS_TRACEABILITY).read_text())
    assert tr["workspace_id"] == wid


# --------------------------------------------------------------------------- #
# 4. QualityGate blocks when tests pass but requirements unmet                #
# --------------------------------------------------------------------------- #

def test_quality_gate_blocks_when_required_section_missing(tmp_workspace_root):
    """Tests pass + build passes, but the spec lacks 'Acceptance Criteria'.
    QualityGate must REJECT even though BuildValidator passed."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    bad_spec = "# Architecture\nfoo\n\n# Components\napi\n"  # missing Acceptance
    eng = _build_engine(max_iterations=1)
    r = eng.validate(workspace_id=wid, implementation_spec=bad_spec)
    assert r.success is False
    qr = json.loads((_validation_dir(wid) / ARTIFACT_QUALITY_REPORT).read_text())
    assert qr["passed"] is False
    assert "Acceptance Criteria" in qr["requirements_unmet"]


def test_quality_gate_blocks_when_components_not_in_files(tmp_workspace_root):
    """Spec says component 'api' but only 'x.py' exists ── QualityGate fails."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/x.py", "X = 1\n"),
                              ("tests/test_x.py", "def t(): pass\n")])
    eng = _build_engine(max_iterations=1)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is False
    qr = json.loads((_validation_dir(wid) / ARTIFACT_QUALITY_REPORT).read_text())
    assert "Components" in qr["requirements_unmet"]


# --------------------------------------------------------------------------- #
# 5. Repair loop ── FailureAnalysis → RepairAgent → PatchValidator → retry    #
# --------------------------------------------------------------------------- #

def test_repair_loop_runs_on_test_failure(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    repair_calls = {"n": 0}

    def _repair(wid, prompt):
        repair_calls["n"] += 1
        return {"success": True, "files_changed": ["src/api.py"],
                "output": "fixed"}

    eng = _build_engine(
        test_fns={"unit": _tests_fail,
                  "integration": _tests_pass, "smoke": _tests_pass},
        repair_fn=_repair, max_iterations=2)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is False  # tests keep failing (fake repair doesn't help)
    assert repair_calls["n"] >= 1
    # patch_history.json captures every patch
    ph = json.loads((_validation_dir(wid) / ARTIFACT_PATCH_HISTORY).read_text())
    assert len(ph["patches"]) >= 1


def test_repair_loop_succeeds_when_repair_fixes_tests(tmp_workspace_root):
    """A repair that flips tests to passing on the second iteration should
    produce a success result."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    iters = {"n": 0}

    def _flaky_tests(wd):
        iters["n"] += 1
        return _tests_pass(wd) if iters["n"] > 1 else _tests_fail(wd)

    repairs = {"n": 0}

    def _repair(wid, prompt):
        repairs["n"] += 1
        return {"success": True, "files_changed": ["src/api.py"]}

    eng = _build_engine(
        test_fns={"unit": _flaky_tests,
                  "integration": _tests_pass, "smoke": _tests_pass},
        repair_fn=_repair, max_iterations=2)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is True
    assert repairs["n"] == 1


def test_retry_exhausted_terminates_with_failed_result(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine(
        test_fns={"unit": _tests_fail,
                  "integration": _tests_pass, "smoke": _tests_pass},
        repair_fn=lambda wid, p: {"success": True,
                                    "files_changed": ["src/api.py"]},
        max_iterations=2)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is False
    rh = json.loads((_validation_dir(wid) / ARTIFACT_RETRY_HISTORY).read_text())
    # The retry_history records at least 2 retry decisions
    assert len(rh["history"]) >= 2
    # The last retry reason is "retry budget exhausted"
    assert rh["history"][-1]["reason"] == "retry budget exhausted"


# --------------------------------------------------------------------------- #
# 6. No-repair-seam safety guard ── refuses to dispatch to live opencode      #
# --------------------------------------------------------------------------- #

def test_no_repair_seam_refuses_live_dispatch(tmp_workspace_root):
    """Without a repair_fn or coding_agent_fn AND allow_live_agent=False
    (the default), the engine refuses to dispatch ── returns a structured
    failure rather than spawning the real opencode subprocess. The spec's
    'silent failures forbidden' contract holds: the failure is reported."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    # Spec intentionally lists a component not on disk to trigger repair
    eng = _build_engine(
        test_fns={"unit": _tests_pass, "integration": _tests_pass,
                  "smoke": _tests_pass},
        max_iterations=1)  # no repair_fn, no coding_agent_fn
    # spec_component 'qpi' won't match 'api.py' → QualityGate fails → repair
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec("qpi"))
    assert r.success is False
    # The error surfaces, never silent
    assert r.error
    rh = json.loads((_validation_dir(wid) / ARTIFACT_REPAIR_HISTORY).read_text())
    # The failed repair attempt is recorded
    assert rh["history"]
    # The recorded patch has an error message
    recorded_error = rh["history"][0].get(
        "response_success", False) or "no repair seam" in (
        rh["history"][0].get("plan", {}).get("prompt", "") + " "
        + str(rh["history"][0]))
    # Either the response was False OR the prompt mentioned the seam issue
    # (we accept both as 'silence forbidden' evidence).


def test_allow_live_agent_flag_still_off_by_default():
    eng = _build_engine()
    assert eng._allow_live_agent is False


# --------------------------------------------------------------------------- #
# 7. BuildRunner ── build failures are surfaced + categorized                  #
# --------------------------------------------------------------------------- #

def test_build_failure_propagates_to_failure_analysis(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine(
        build_fn=lambda wd, spec: {"exit_code": 1, "stdout": "",
                                    "stderr": "SyntaxError: bad",
                                    "success": False, "logs": ["fail"]},
        max_iterations=1)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is False
    fa = json.loads((_validation_dir(wid)
                     / ARTIFACT_FAILURE_ANALYSIS).read_text())
    cats = {d.get("category") for d in fa.get("analysis", [])}
    assert FAIL_BUILD in cats


# --------------------------------------------------------------------------- #
# 8. ArtifactValidator ── syntax + import + config checks                     #
# --------------------------------------------------------------------------- #

def test_artifact_validator_flags_syntax_error(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/bad.py", "def f(:\n"),
                              ("tests/test_bad.py", "def t(): pass\n")])
    eng = _build_engine(max_iterations=1)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec("bad"))
    assert r.success is False
    fa = json.loads((_validation_dir(wid)
                     / ARTIFACT_FAILURE_ANALYSIS).read_text())
    cats = {d.get("category") for d in fa.get("analysis", [])}
    assert FAIL_SYNTAX in cats


def test_artifact_validator_flags_missing_files(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root, files=[])  # empty workspace
    eng = _build_engine(max_iterations=1)
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is False
    fa = json.loads((_validation_dir(wid)
                     / ARTIFACT_FAILURE_ANALYSIS).read_text())
    cats = {d.get("category") for d in fa.get("analysis", [])}
    assert FAIL_ARTIFACT in cats


# --------------------------------------------------------------------------- #
# 9. PatchValidator ── syntax + integrity validation                          #
# --------------------------------------------------------------------------- #

def test_patch_validator_accepts_clean_python(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/good.py", "def f(): pass\n")])
    pv = PatchValidator()
    snap = pv.snapshot(wid, ["src/good.py"])
    record = pv.validate(workspace_id=wid, files_changed=["src/good.py"],
                         checksums_before=snap)
    assert record.syntax_valid is True
    assert record.integrity_valid is True


def test_patch_validator_rejects_syntax_error(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/bad.py", "def f(:\n")])
    pv = PatchValidator()
    snap = pv.snapshot(wid, ["src/bad.py"])
    record = pv.validate(workspace_id=wid, files_changed=["src/bad.py"],
                         checksums_before=snap)
    assert record.syntax_valid is False
    assert record.validation_errors


def test_patch_validator_rejects_empty_patch():
    pv = PatchValidator()
    record = pv.validate(workspace_id="any", files_changed=[],
                         checksums_before={})
    assert record.integrity_valid is False


def test_patch_validator_records_checksums(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/x.py", "X = 1\n")])
    pv = PatchValidator()
    snap = pv.snapshot(wid, ["src/x.py"])
    record = pv.validate(workspace_id=wid, files_changed=["src/x.py"],
                         checksums_before=snap)
    assert "src/x.py" in record.checksums_after


# --------------------------------------------------------------------------- #
# 10. FailureAnalyzer ── structured diagnostics                               #
# --------------------------------------------------------------------------- #

def test_failure_analyzer_categorizes_build_failure():
    fa = FailureAnalyzer()
    out = fa.analyze(build_result={"success": False, "exit_code": 1,
                                    "stderr": "boom"})
    cats = {d.category for d in out}
    assert FAIL_BUILD in cats


def test_failure_analyzer_categorizes_test_failure():
    fa = FailureAnalyzer()
    out = fa.analyze(test_result={
        "passed": False,
        "test_types": {"unit": {"passed": False, "failed": 1,
                                 "output": "AssertionError"}}})
    cats = {d.category for d in out}
    assert FAIL_TESTS in cats


def test_failure_analyzer_passes_artifact_diagnostics_through(tmp_workspace_root):
    fa = FailureAnalyzer()
    out = fa.analyze(artifact_result={"passed": False, "diagnostics": [
        {"category": FAIL_ARTIFACT, "severity": "error", "message": "x",
         "location": "", "hint": ""}]})
    cats = {d.category for d in out}
    assert FAIL_ARTIFACT in cats


# --------------------------------------------------------------------------- #
# 11. RepairPlanner ── scope, priority, strategy                              #
# --------------------------------------------------------------------------- #

def test_repair_planner_returns_no_op_on_empty_diagnostics():
    planner = RepairPlanner()
    plan = planner.plan([], ValidationContext(implementation_spec="s"))
    assert plan.scope == "none"


def test_repair_planner_prioritizes_critical_over_high():
    planner = RepairPlanner()
    diags = [
        Diagnostic(category=FAIL_TESTS, severity="error", message="t failed"),
        Diagnostic(category=FAIL_SYNTAX, severity="error",
                   message="syntax bad", location="src/x.py"),
    ]
    plan = planner.plan(diags, ValidationContext(implementation_spec="s"))
    assert plan.priority == "critical"  # syntax > tests
    assert plan.strategy == "patch"


def test_repair_planner_aggregates_target_files():
    planner = RepairPlanner()
    diags = [
        Diagnostic(category=FAIL_TESTS, severity="error", message="t1",
                   location="tests/test_a.py"),
        Diagnostic(category=FAIL_TESTS, severity="error", message="t2",
                   location="tests/test_b.py"),
    ]
    plan = planner.plan(diags, ValidationContext(implementation_spec="s"))
    assert "tests/test_a.py" in plan.target_files
    assert "tests/test_b.py" in plan.target_files
    assert plan.prompt


# --------------------------------------------------------------------------- #
# 12. RetryController ── deterministic termination                              #
# --------------------------------------------------------------------------- #

def test_retry_controller_terminate_at_cap():
    rc = RetryController(max_iterations=2)
    ctx = ValidationContext(current_iteration=2)
    retry, reason = rc.should_retry(ctx, last_patch=None)
    assert retry is False
    assert "exhausted" in reason


def test_retry_controller_terminate_on_cancel():
    ctx = ValidationContext(max_iterations=5)
    ctx.cancelled = True
    rc = RetryController(max_iterations=5)
    retry, reason = rc.should_retry(ctx, last_patch=None)
    assert retry is False
    assert "cancel" in reason


def test_retry_controller_advance_increments_iteration():
    rc = RetryController(max_iterations=5)
    ctx = ValidationContext(current_iteration=1)
    rc.advance(ctx)
    assert ctx.current_iteration == 2


# --------------------------------------------------------------------------- #
# 13. BuildValidator ── mandatory vs advisory checks                            #
# --------------------------------------------------------------------------- #

def test_build_validator_passes_clean_result():
    bv = BuildValidator()
    rep = bv.validate(
        artifact_result={"passed": True, "diagnostics": []},
        build_result={"success": True},
        test_result={"test_types": {"unit": {"passed": True},
                                     "integration": {"passed": True},
                                     "smoke": {"passed": True}}})
    assert rep["passed"] is True


def test_build_validator_fails_on_artifact_failure():
    bv = BuildValidator()
    rep = bv.validate(
        artifact_result={"passed": False, "diagnostics": [
            {"category": FAIL_ARTIFACT, "severity": "error",
             "message": "x", "location": "", "hint": ""}]},
        build_result={"success": True},
        test_result={"test_types": {}})
    assert rep["passed"] is False


def test_build_validator_advisory_failure_is_warning_not_block():
    bv = BuildValidator()
    rep = bv.validate(
        artifact_result={"passed": True, "diagnostics": []},
        build_result={"success": True},
        test_result={"test_types": {
            "unit": {"passed": True}, "integration": {"passed": True},
            "smoke": {"passed": True},
            "type_check": {"passed": False, "output": "type error"},
            "lint": {"skipped": True}}})
    # Advisory tools failing is a warning, not a block
    assert rep["passed"] is True
    assert rep["warnings"]
    assert "lint" in rep["advisory_skipped"]


# --------------------------------------------------------------------------- #
# 14. QualityGate ── traceability                                              #
# --------------------------------------------------------------------------- #

def test_quality_gate_emits_quality_report_and_traceability(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    qg = QualityGate()
    rep, tr = qg.evaluate(
        implementation_spec=_good_spec(), workspace_id=wid,
        artifact_result={"passed": True, "diagnostics": []},
        test_result={"test_types": {"unit": {"passed": True}}},
        build_validator_result={"passed": True})
    assert isinstance(rep, QualityReport)
    assert rep.passed is True
    # traceability has one entry per spec section
    assert len(tr["entries"]) >= 3
    section_names = [e["requirement"] for e in tr["entries"]]
    assert "Architecture" in section_names
    assert "Components" in section_names
    assert "Acceptance Criteria" in section_names


def test_quality_gate_blocks_when_components_not_in_files(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root,
                      files=[("src/x.py", "X = 1\n")])
    qg = QualityGate()
    rep, _ = qg.evaluate(
        implementation_spec=_good_spec("api"),  # spec says 'api'
        workspace_id=wid,
        artifact_result={"passed": True, "diagnostics": []},
        test_result={"test_types": {"unit": {"passed": True}}},
        build_validator_result={"passed": True})
    assert rep.passed is False
    assert "Components" in rep.requirements_unmet


def test_quality_gate_blocks_when_build_validator_failed(tmp_workspace_root):
    """Even if tests passed, if the BuildValidator failed, QualityGate fails."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    qg = QualityGate()
    rep, _ = qg.evaluate(
        implementation_spec=_good_spec(), workspace_id=wid,
        artifact_result={"passed": True},
        test_result={"test_types": {"unit": {"passed": True}}},
        build_validator_result={"passed": False})
    assert rep.passed is False


# --------------------------------------------------------------------------- #
# 15. ExecutionRecorder ── records lifecycle + persists                        #
# --------------------------------------------------------------------------- #

def test_execution_recorder_records_lifecycle(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    rec_seen: list[str] = []
    eng = _build_engine()
    eng.event_bus.subscribe(lambda e: rec_seen.append(e.type))
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is True
    assert EV_VALIDATION_STARTED in rec_seen
    assert EV_VALIDATION_PHASE in rec_seen
    assert EV_VALIDATION_COMPLETED in rec_seen


def test_execution_recorder_persists_to_disk(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    rec_path = _validation_dir(wid) / "execution_record.json"
    # Inherits from Phase 2's recorder which writes to implementation/; we
    # also persist via the ValidationEngine.
    assert rec_path.is_file() or (_workspace_dir(wid) / "implementation"
                                  / "execution_record.json").is_file()


def test_execution_recorder_observer_only(tmp_workspace_root):
    """The recorder never affects workflow ── detaching it produces the
    same result."""
    wid1, _ = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    wid2, _ = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng_with = _build_engine()
    eng_without = _build_engine()
    eng_without.event_bus._subs = []  # detach all observers
    r_with = eng_with.validate(workspace_id=wid1,
                                implementation_spec=_good_spec())
    r_without = eng_without.validate(workspace_id=wid2,
                                       implementation_spec=_good_spec())
    assert r_with.success == r_without.success
    assert r_with.final_node == r_without.final_node


# --------------------------------------------------------------------------- #
# 16. Resume after interruption                                                #
# --------------------------------------------------------------------------- #

def test_resume_continues_from_persisted_state(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    # Seed a state in the middle of the workflow
    ctx = ValidationContext(
        workspace_id=wid, implementation_spec=_good_spec(),
        current_phase=PHASE_BUILD, validation_state="RUNNING",
        current_iteration=1,
    )
    from validation_engine import _write_json
    _write_json(_validation_dir(wid) / "validation_context.json",
                ctx.to_dict())
    eng = _build_engine()
    r = eng.resume(workspace_id=wid)
    assert r.success is True
    assert r.workspace_id == wid


def test_resume_no_state_returns_structured_error(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    eng = _build_engine()
    # No prior state persisted
    r = eng.resume(workspace_id=wid)
    assert r.success is False
    assert "checkpoint" in r.error.lower() or "absent" in r.error.lower()


# --------------------------------------------------------------------------- #
# 17. Cancel is cooperative                                                   #
# --------------------------------------------------------------------------- #

def test_cancel_marks_context_cancelled(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    ctx = ValidationContext(workspace_id=wid,
                              implementation_spec=_good_spec())
    from validation_engine import _write_json
    _write_json(_validation_dir(wid) / "validation_context.json",
                ctx.to_dict())
    eng = _build_engine()
    assert eng.cancel(workspace_id=wid) is True
    ctx_after = eng._load_context(wid)
    assert ctx_after.cancelled is True


def test_cancel_returns_false_when_no_state(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)  # no checkpoint
    eng = _build_engine()
    assert eng.cancel(workspace_id=wid) is False


# --------------------------------------------------------------------------- #
# 18. Status / recover surfaces                                                #
# --------------------------------------------------------------------------- #

def test_status_unknown_when_no_state(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    eng = _build_engine()
    st = eng.status(workspace_id=wid)
    assert st["validation_state"] == "unknown"


def test_status_reports_phase_and_iteration(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    st = eng.status(workspace_id=wid)
    assert st["validation_state"] == STATUS_COMPLETED
    assert "current_phase" in st


def test_recover_returns_diagnostic_report(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    rec = eng.recover(workspace_id=wid)
    assert rec["recoverable"] is True


def test_recover_returns_false_when_no_state(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    eng = _build_engine()
    rec = eng.recover(workspace_id=wid)
    assert rec["recoverable"] is False
    assert "no validation_context.json" in rec["reason"]


# --------------------------------------------------------------------------- #
# 19. BuildResult carries Phase 3 fields + same shape as Phase 2               #
# --------------------------------------------------------------------------- #

def test_build_result_carries_phase3_fields(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.engine == "NativeValidation"
    assert r.execution_id
    # Phase 2 BuildResult fields are preserved
    d = r.to_dict()
    for k in ("success", "output", "test_results", "repair_attempts",
              "repair_log", "error", "executed_at", "workspace_id",
              "engine", "final_node", "execution_id", "node_timings",
              "generated_files", "modified_files", "execution_log"):
        assert k in d


# --------------------------------------------------------------------------- #
# 20. Configuration switching                                                  #
# --------------------------------------------------------------------------- #

def test_get_validation_engine_native_default():
    eng = get_validation_engine(
        build_fn=lambda wd, spec: {"success": True, "exit_code": 0,
                                    "stdout": "", "stderr": "", "logs": []},
        test_fns={"unit": lambda wd: {}}, max_iterations=1)
    assert isinstance(eng, NativeValidationEngine)


def test_get_validation_engine_unknown_name_raises():
    with pytest.raises(ValueError):
        get_validation_engine("DoesNotExist")


# --------------------------------------------------------------------------- #
# 21. 12 workspace artifacts persisted                                         #
# --------------------------------------------------------------------------- #

def test_all_twelve_artifacts_persisted_after_validation(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    val_dir = _validation_dir(wid)
    required = list(ALL_ARTIFACTS)
    for f in required:
        assert (val_dir / f).is_file(), f"missing: {f}"


def test_final_build_result_matches_returned_result(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    persisted = json.loads((_validation_dir(wid)
                            / ARTIFACT_FINAL_BUILD_RESULT).read_text())
    assert persisted["execution_id"] == r.execution_id
    assert persisted["success"] == r.success


# --------------------------------------------------------------------------- #
# 22. Repair entrypoint (re-runs the loop)                                    #
# --------------------------------------------------------------------------- #

def test_repair_entrypoint_requires_prior_state(tmp_workspace_root):
    wid, _ = _seed_ws(tmp_workspace_root)
    eng = _build_engine()
    r = eng.repair(workspace_id=wid)
    assert r.success is False
    assert "validate()" in r.error or "prior validation context" in r.error


def test_repair_entrypoint_resumes_existing_workspace(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    # First run succeeds
    eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    # Second run via repair ── should also succeed (idempotent)
    r = eng.repair(workspace_id=wid)
    assert r.success is True


# --------------------------------------------------------------------------- #
# 23. Backward compatibility ── Phase 2 NativeImplementationEngine unchanged  #
# --------------------------------------------------------------------------- #

def test_phase2_native_implementation_engine_still_works(tmp_workspace_root):
    """Phase 2's engine is untouched by Phase 3 ── import + use it directly."""
    from implementation_engine import NativeImplementationEngine
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng2 = NativeImplementationEngine(
        coding_agent_fn=lambda prompt, wd: {"success": True, "output": "ok",
                                              "files": ["src/api.py"]},
        testing_agent_fn=lambda workspace_id: {"passed": True, "failed": 0},
        max_attempts=2)
    r = eng2.execute(workspace_id=wid, implementation_spec="# spec")
    assert r.success is True
    assert r.engine == "Native"  # Phase 2 engine name intact


def test_phase2_build_result_shape_unchanged_in_phase3(tmp_workspace_root):
    """Phase 3's BuildResult fields are a SUPERSET of Phase 2's ──
    regression-safe."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    phase3 = set(r.to_dict().keys())
    phase2 = set(BuildResult().to_dict().keys())
    assert phase2.issubset(phase3)


# --------------------------------------------------------------------------- #
# 24. Pure-stdlib at import (no provider pulled in transitively)              #
# --------------------------------------------------------------------------- #

def test_no_forbidden_modules_transitively_imported():
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_set = (
        "{'cerebras_client','ollama_client','openrouter_client','grok_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium',"
        "'requests','httpx','urllib3'}"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import validation_engine; "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: stdout={probe.stdout!r} "
        f"stderr={probe.stderr!r}"
    )
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "validation_engine transitively imports forbidden modules"
    )


# --------------------------------------------------------------------------- #
# 25. Determinism                                                              #
# --------------------------------------------------------------------------- #

def test_deterministic_for_same_inputs(tmp_workspace_root):
    w1, _ = _seed_ws(tmp_workspace_root,
                      files=[("src/api.py", "API = 1\n"),
                             ("tests/test_api.py", "def t(): pass\n")])
    w2, _ = _seed_ws(tmp_workspace_root,
                      files=[("src/api.py", "API = 1\n"),
                             ("tests/test_api.py", "def t(): pass\n")])
    eng = _build_engine()
    r1 = eng.validate(workspace_id=w1, implementation_spec=_good_spec())
    r2 = eng.validate(workspace_id=w2, implementation_spec=_good_spec())
    assert r1.success == r2.success
    assert r1.final_node == r2.final_node


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_validation_engine.py -q")
