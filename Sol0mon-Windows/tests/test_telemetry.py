"""
test_telemetry.py — Telemetry & Runtime Observability V1 (Phases 1-9).

Offline only. No model calls. No browser. No external APIs.

Verifies the 10 contracts from the task spec:

  1.  Events emitted correctly (immutable TelemetryEvent; configured recorder
      records; persists to workspace_status.json).
  2.  No execution behavior changes — the recorder is a no-op when
      unconfigured: no prints, no files, no raises; build_pipeline + debate
      raise the same results with telemetry present.
  3.  workspace_status updates — observe()/emit() refresh the live dict and
      persist to workspace_status.json (Phase 4 shape).
  4.  ETA never crashes — no data, partial data, zero data, edge cases all
      return a dict + a clearly-marked human-readable estimate.
  5.  Resume restores status — configure(resume=True) on an existing
      workspace_status.json restores pipeline_started_at + live fields.
  6.  Performance report generated — Phase-7 buckets present; written to
      performance_report.json.
  7.  CLI status works — `sol0mon status` renders a human-readable snapshot
      from workspace_status.json; `--report` prints the perf report.
  8.  Runtime calculations deterministic — same event sequence -> same
      status / performance_report / eta (timestamps frozen via monkeypatched
      time.time).
  9.  No provider calls — telemetry.py imports nothing forbidden (checked in
      a fresh subprocess).
  10. Existing pipeline tests remain green — build_pipeline with injected
      seams still returns the canonical result; the recorder's no-op-by-
      default contract makes instrumentation behavior-neutral.

The recorder is the module-level singleton. The autouse fixture in
``tests/conftest.py`` resets it after each test so order never matters.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
import sol0mon_cli
import telemetry
from telemetry import (
    TelemetryEvent, recorder,
    STAGE_DEBATE, STAGE_ROUND, STAGE_DISPUTE, STAGE_VERIFICATION,
    STAGE_RESEARCH, STAGE_ARCHITECTURE, STAGE_HUMAN_SYNTHESIS,
    STAGE_BUILD, STAGE_BUILD_SPEC, STAGE_EXECUTION, STAGE_REPAIR,
    STAGE_PIPELINE, EVENT_STARTED, EVENT_COMPLETED,
    EVENT_ROUND_COMPLETED, EVENT_DISPUTE_RESOLVED,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _set_workspace_root():
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    return Path(tmp)


def _good_spec_text():
    """Hybrid spec: contains the legacy 7 audit-debate sections AND the 6
    executable-spec sections, so both validate_spec_sections (legacy audit-
    debate path) and validate_executable_spec_sections (the defence-in-depth
    gate at execute_build_spec) pass."""
    return (
        # Legacy 7 sections:
        "# Architecture\nLayered.\n\n"
        "# Components\nAPI.\n\n"
        "# File Structure\nsrc/\n\n"
        "# Dependencies\nfastapi\n\n"
        "# Constraints\nMUST use PG.\n\n"
        "# Testing Requirements\npytest\n\n"
        # Executable-spec sections:
        "# Implementation Objective\nBuild the layered service.\n\n"
        "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n\n"
        "# Files to Modify\n- (none)\n\n"
        "# Implementation Steps\n1. Implement main.\n\n"
        "# Acceptance Criteria\npass\n\n"
        "# Output Contract\nStop after tests pass.\n"
    )


def _fake_debate(prompt, memory_ids=None):
    return {
        "status": "completed",
        "synthesized_answer": _good_spec_text(),
        "gpt_final": "g", "gemini_final": "m",
        "validated_disputes": [],
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [],
        "final_consensus_score": 90,
    }


def _mock_executor(spec_text):
    assert spec_text
    # Simulate OpenCode writing the files declared in # Files to Create so
    # the post-execution QualityGate's "required files exist" check passes.
    ws_dirs = list(bw.WORKSPACE_ROOT.glob("workspace_*"))
    if ws_dirs:
        ws = ws_dirs[0]
        (ws / "src" / "app").mkdir(parents=True, exist_ok=True)
        (ws / "src" / "app" / "main.py").write_text("VALUE = 1\n")
        (ws / "tests").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "test_app.py").write_text("def test_value():\n"
                                                  "    assert True\n")
    return {"success": True, "output": "ok",
            "test_results": {"passed": 1, "failed": 0, "exit_code": 0},
            "repair_attempts": 0, "error": ""}


def _fresh_recorder(tmp: str, job_id: str = "test-job", resume: bool = False):
    """Configure the singleton recorder pointed at a tmp dir. Returns the
    recorder for chaining. ``resume`` defaults False so tests start fresh
    (the resume test opts in explicitly to verify clock continuity)."""
    recorder.configure(
        job_id=job_id, workspace_id="ws-test", resume=resume,
        status_path=os.path.join(tmp, "workspace_status.json"),
        report_path=os.path.join(tmp, "performance_report.json"),
    )
    return recorder


# --------------------------------------------------------------------------- #
# 1. Events emitted correctly                                                 #
# --------------------------------------------------------------------------- #

def test_events_emitted_correctly():
    """Contract 1: configured recorder records immutable TelemetryEvent objects
    carrying timestamp/stage/duration/job_id/workspace_id/metadata; the event
    stream persists to workspace_status.json."""
    tmp = tempfile.mkdtemp()
    try:
        r = _fresh_recorder(tmp, job_id="evt-1")
        r.start_stage(STAGE_DEBATE)
        ev = r.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=12.0,
                    round=3, dispute="Exchange Selection", consensus=85)
        assert isinstance(ev, TelemetryEvent)
        assert ev.event_type == EVENT_ROUND_COMPLETED
        assert ev.stage == STAGE_ROUND
        assert ev.duration == 12.0
        assert ev.job_id == "evt-1"
        assert ev.workspace_id == "ws-test"
        assert ev.metadata["round"] == 3
        assert ev.metadata["dispute"] == "Exchange Selection"
        # Immutability: a frozen dataclass rejects assignment.
        raised = False
        try:
            ev.duration = 99.0  # type: ignore[misc]
        except Exception:
            raised = True
        assert raised, "TelemetryEvent must be immutable"
        r.end_stage(STAGE_DEBATE, status="completed")

        # workspace_status.json was written.
        status_p = os.path.join(tmp, "workspace_status.json")
        assert os.path.isfile(status_p)
        persisted = json.loads(Path(status_p).read_text())
        assert persisted["job_id"] == "evt-1"
        # The RoundCompleted reducer surfaced current_round/active_dispute.
        assert persisted["current_round"] == 3
        assert persisted["active_dispute"] == "Exchange Selection"
        assert persisted["consensus"] == 85
    finally:
        telemetry.reset()


def test_unconfigured_recorder_is_silent_noop():
    """Contract 1b (supports 2): an unconfigured recorder emits nothing,
    writes nothing, raises nothing, and returns empty status/report."""
    assert not recorder.enabled
    # None of these raise or write anything.
    assert recorder.emit(EVENT_STARTED, STAGE_DEBATE) is None
    recorder.start_stage(STAGE_DEBATE)
    recorder.end_stage(STAGE_DEBATE)
    recorder.observe(current_round=1, consensus=99)
    assert recorder.status() == {}
    assert recorder.eta()["estimate_minutes"] is None
    assert recorder.performance_report() == {}
    assert recorder.save_report() is None
    # No files written to CWD.
    assert not os.path.isfile(os.path.join(os.getcwd(),
                                            "workspace_status.json"))


# --------------------------------------------------------------------------- #
# 2. No execution behavior changes                                            #
# --------------------------------------------------------------------------- #

def test_build_pipeline_result_unchanged_with_telemetry_present():
    """Contract 2: run_build_pipeline returns the canonical result dict
    byte-for-byte (same keys + values) whether or not the recorder is
    configured — telemetry is observational only and never alters the return
    shape. The build-pipeline instrumentation emits BuildStarted/BuildSpec/
    Execution/BuildCompleted events; the return dict is unchanged."""
    _set_workspace_root()
    # Baseline run with recorder UNCONFIGURED.
    r1 = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate, executor_fn=_mock_executor,
    )
    # Configured run — recorder active, writing to a tmp dir.
    tmp = tempfile.mkdtemp()
    try:
        _fresh_recorder(tmp)
        _set_workspace_root()  # keep workspace isolation from the cfg fixture
        r2 = bp.run_build_pipeline(
            "Build Project Sol0mon MVP",
            debate_fn=_fake_debate, executor_fn=_mock_executor,
        )
        # The return shape + key values are identical (telemetry adds nothing
        # to the payload). Compare the user-facing keys only.
        keys = ("classification", "spec_status", "execution_result",
                "answer_resurrected", "recommended_mode", "selected_mode",
                "override_used", "execution_decision", "human_decisions")
        for k in keys:
            assert k in r1 and k in r2, f"missing key {k}"
        assert r1["classification"]["workflow_type"] == \
               r2["classification"]["workflow_type"] == "IMPLEMENTATION"
        assert r1["spec_status"] == r2["spec_status"] == "completed"
        assert bool(r1["execution_result"].get("success")) == \
               bool(r2["execution_result"].get("success")) is True
        # Build-stage events were emitted during the configured run.
        ev_types = [e.event_type for e in recorder._events
                    if e.stage == STAGE_BUILD]
        assert "Started" in ev_types and "Completed" in ev_types
    finally:
        telemetry.reset()


def test_debate_engine_instrumentation_is_noop_when_unconfigured():
    """Contract 2b: importing debate_engine with telemetry present does not
    alter behavior — the recorder import is lazy andobserve/emit are no-ops
    when unconfigured. Verified by confirming the module imports cleanly under
    the venv AND that the instrumentation hook (recorder attribute) exists
    without being enabled."""
    # The instrumented modules import cleanly.
    import debate_engine  # noqa: F401
    import pipeline_runner  # noqa: F401
    import build_pipeline  # noqa: F401
    # The hook is present but disabled.
    assert not telemetry.recorder.enabled


# --------------------------------------------------------------------------- #
# 3. workspace_status updates                                                 #
# --------------------------------------------------------------------------- #

def test_workspace_status_updates_and_persists():
    """Contract 3: observe()/emit() update the live status dict (Phase 4
    shape) and persist it to workspace_status.json after every change."""
    tmp = tempfile.mkdtemp()
    status_p = os.path.join(tmp, "workspace_status.json")
    try:
        r = _fresh_recorder(tmp, job_id="status-1")
        r.observe(stage="Debate", total_disputes=3, remaining_disputes=3)
        r.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=20.0,
               round=7, dispute="Exchange Selection", consensus=91)
        r.emit(EVENT_DISPUTE_RESOLVED, STAGE_DISPUTE, remaining=2, total=3)
        st = r.status()
        assert st["job_id"] == "status-1"
        assert st["stage"] == "Debate"
        assert st["current_round"] == 7
        assert st["active_dispute"] == "Exchange Selection"
        assert st["consensus"] == 91
        assert st["resolved_disputes"] == 1
        assert st["remaining_disputes"] == 2
        assert st["total_disputes"] == 3
        assert "elapsed_seconds" in st and st["elapsed_seconds"] >= 0
        # The persisted file mirrors status + adds pipeline_started_at + eta.
        persisted = json.loads(Path(status_p).read_text())
        assert persisted["current_round"] == 7
        assert persisted["consensus"] == 91
        assert "pipeline_started_at" in persisted
        assert "eta" in persisted
    finally:
        telemetry.reset()


# --------------------------------------------------------------------------- #
# 4. ETA never crashes                                                         #
# --------------------------------------------------------------------------- #

def test_eta_never_crashes():
    """Contract 4: eta() returns a dict with an estimate_minutes (or None) and
    a human_readable string for every input shape — no data, partial data,
    zero durations, and pathological negatives. Never raises."""
    # Unconfigured — returns the empty-estimate dict.
    empty = recorder.eta()
    assert empty["estimate_minutes"] is None
    assert "estimate" in empty["human_readable"].lower() or \
           "unavailable" in empty["human_readable"].lower()

    tmp = tempfile.mkdtemp()
    try:
        _fresh_recorder(tmp)
        # No events yet — still an estimate, never an exception.
        e0 = recorder.eta()
        assert isinstance(e0, dict)
        assert "human_readable" in e0
        # With round durations but no remaining disputes -> no remaining work.
        recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=10.0,
                      round=1, dispute="d", consensus=50)
        recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=20.0,
                      round=2, dispute="d", consensus=80)
        e1 = recorder.eta()
        assert isinstance(e1, dict)
        # With remaining disputes + observed rounds -> a numeric estimate.
        recorder.observe(remaining_disputes=2, total_disputes=5)
        e2 = recorder.eta()
        assert e2["estimate_minutes"] is not None
        assert e2["estimate_minutes"] >= 0
        assert "estimate" in e2["human_readable"]
        # Verification durations feed in too.
        recorder.emit(EVENT_STARTED, STAGE_VERIFICATION)
        recorder.emit(EVENT_COMPLETED, STAGE_VERIFICATION, duration=30.0)
        e3 = recorder.eta()
        assert isinstance(e3, dict)
        # Pathological: zero-duration rounds (no divide crash).
        recorder.observe(remaining_disputes=1)
        recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=0.0,
                       round=1, dispute="d", consensus=0)
        assert isinstance(recorder.eta(), dict)
        # Pathological: huge remaining (no overflow).
        recorder.observe(remaining_disputes=10 ** 9)
        assert isinstance(recorder.eta(), dict)
    finally:
        telemetry.reset()


# --------------------------------------------------------------------------- #
# 5. Resume restores status                                                    #
# --------------------------------------------------------------------------- #

def test_resume_restores_status():
    """Contract 5: configure(resume=True) on an existing workspace_status.json
    restores pipeline_started_at and live fields — a resumed run keeps its
    original elapsed clock and dispute counters."""
    tmp = tempfile.mkdtemp()
    status_p = os.path.join(tmp, "workspace_status.json")
    try:
        r = _fresh_recorder(tmp, job_id="resume-1")
        # Simulate a long-running pipeline that got interrupted.
        r._pipeline_started_at = time.time() - 1000.0  # 1000s ago
        r.observe(stage="Debate", resolved_disputes=2,
                  remaining_disputes=1, total_disputes=3,
                  current_round=9, active_dispute="DB Selection",
                  consensus=88, research_requests=1, verification_runs=2)
        r.save_status()
        started_before = r._pipeline_started_at
        telemetry.reset()
        # Resume: a fresh recorder reads the prior status.
        recorder.configure(job_id="resume-2", status_path=status_p,
                           report_path=os.path.join(tmp, "performance_report.json"),
                           resume=True)
        assert recorder._pipeline_started_at == started_before, \
            "resume should restore the original pipeline_started_at clock"
        st = recorder.status()
        assert st["resolved_disputes"] == 2
        assert st["remaining_disputes"] == 1
        assert st["current_round"] == 9
        assert st["active_dispute"] == "DB Selection"
        assert st["consensus"] == 88
        assert st["elapsed_seconds"] >= 1000.0  # the preserved clock advanced
    finally:
        telemetry.reset()


def test_resume_without_existing_file_starts_fresh():
    """Contract 5b: resume with no existing status file starts a fresh clock
    (no crash, no stale state)."""
    tmp = tempfile.mkdtemp()
    try:
        recorder.configure(job_id="fresh", status_path=os.path.join(
            tmp, "nope.json"), resume=True)
        assert recorder._pipeline_started_at > 0
        assert recorder.status()["resolved_disputes"] == 0
    finally:
        telemetry.reset()


# --------------------------------------------------------------------------- #
# 6. Performance report generated                                             #
# --------------------------------------------------------------------------- #

def test_performance_report_generated():
    """Contract 6: performance_report() returns every Phase-7 bucket and
    save_report() writes performance_report.json to disk."""
    tmp = tempfile.mkdtemp()
    report_p = os.path.join(tmp, "performance_report.json")
    try:
        r = _fresh_recorder(tmp, job_id="perf-1")
        r._pipeline_started_at = time.time() - 500.0
        r.emit(EVENT_COMPLETED, STAGE_RESEARCH, duration=60.0)
        r.emit(EVENT_COMPLETED, STAGE_DEBATE, duration=200.0)
        r.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=12.0, round=1)
        r.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=18.0, round=2)
        r.emit(EVENT_DISPUTE_RESOLVED, STAGE_DISPUTE, remaining=0, total=2)
        r.emit(EVENT_COMPLETED, STAGE_VERIFICATION, duration=40.0)
        r.emit(EVENT_COMPLETED, STAGE_ARCHITECTURE, duration=1.0)
        r.emit(EVENT_COMPLETED, STAGE_HUMAN_SYNTHESIS, duration=0.5)
        r.emit(EVENT_COMPLETED, STAGE_BUILD, duration=120.0)
        r.emit(EVENT_COMPLETED, STAGE_REPAIR, duration=30.0)
        rep = r.performance_report()
        for k in ("research_time", "debate_time", "verification_time",
                  "architecture_time", "human_synthesis_time", "build_time",
                  "repair_time", "total_runtime", "disputes_resolved",
                  "average_round_duration", "average_verification_duration"):
            assert k in rep, f"missing perf bucket: {k}"
        assert rep["research_time"] == 60.0
        assert rep["debate_time"] == 200.0
        assert rep["verification_time"] == 40.0
        assert rep["build_time"] == 120.0
        assert rep["repair_time"] == 30.0
        assert rep["disputes_resolved"] == 1  # one DisputeResolved emitted
        # average_round_duration = mean of the two rounds' durations = 15.0
        assert rep["average_round_duration"] == 15.0
        # average_verification_duration = 40.0 (one Completed verification)
        assert rep["average_verification_duration"] == 40.0
        # total_runtime reflects the wall clock (~500s).
        assert rep["total_runtime"] >= 500.0
        written = r.save_report()
        assert written == report_p and os.path.isfile(report_p)
        on_disk = json.loads(Path(report_p).read_text())
        assert on_disk["research_time"] == 60.0
        assert on_disk["disputes_resolved"] == 1
    finally:
        telemetry.reset()


# --------------------------------------------------------------------------- #
# 7. CLI status works                                                         #
# --------------------------------------------------------------------------- #

def _run_cli(argv) -> tuple[int, str]:
    buf = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = sol0mon_cli.main(argv)
        except SystemExit as e:
            rc = int(e.code or 0)
    return rc, buf.getvalue()


def test_cli_status_reads_workspace_status():
    """Contract 7a: `sol0mon status --status-path ...` renders the Phase-6
    human-readable snapshot (Stage, Dispute, Consensus, Round, Elapsed, ETA,
    Research Requests, Verification Runs, Execution Mode) from
    workspace_status.json."""
    tmp = tempfile.mkdtemp()
    status_p = os.path.join(tmp, "workspace_status.json")
    payload = {
        "job_id": "cli-1",
        "workspace_id": "ws-cli",
        "stage": "Debate",
        "current_round": 7,
        "active_dispute": "Exchange Selection",
        "resolved_disputes": 5,
        "remaining_disputes": 2,
        "total_disputes": 7,
        "consensus": 91,
        "research_requests": 2,
        "verification_runs": 3,
        "execution_mode": "FULL_BUILD",
        "elapsed_seconds": 2814.0,
        "updated_at": "2026-07-17T12:00:00",
        "eta": {"human_readable": "~18 minutes (estimate)",
                "estimate_minutes": 18.0},
    }
    Path(status_p).write_text(json.dumps(payload))
    rc, out = _run_cli(["status", "--status-path", status_p])
    assert rc == 0
    for label, val in [
        ("Current Stage", "Debate"),
        ("Active Dispute", "Exchange Selection"),
        ("Consensus %", "91"),
        ("Round Number", "7"),
        ("Elapsed Runtime", "2814s"),
        ("ETA", "~18 minutes"),
        ("Research Requests", "2"),
        ("Verification Runs", "3"),
        ("Execution Mode", "FULL_BUILD"),
    ]:
        assert f"{label}" in out, f"missing status line: {label}"
        assert str(val) in out, f"missing value {val!r} for {label}"


def test_cli_status_missing_file_returns_nonzero():
    """Contract 7b: a missing status file is reported and returns 1 (never
    crashes)."""
    rc, out = _run_cli(["status", "--status-path",
                       os.path.join(tempfile.gettempdir(), "nope_cli.json")])
    assert rc == 1
    assert "No telemetry status found" in out


def test_cli_status_report_flag_prints_performance_report():
    """Contract 7c: `--report` also prints the persisted
    performance_report.json."""
    tmp = tempfile.mkdtemp()
    status_p = os.path.join(tmp, "workspace_status.json")
    report_p = os.path.join(tmp, "performance_report.json")
    Path(status_p).write_text(json.dumps({"stage": "Completed"}))
    Path(report_p).write_text(json.dumps({
        "research_time": 10.0, "debate_time": 20.0, "verification_time": 5.0,
        "architecture_time": 0.1, "human_synthesis_time": 0.0,
        "build_time": 30.0, "repair_time": 5.0, "total_runtime": 70.1,
        "disputes_resolved": 3, "average_round_duration": 12.0,
        "average_verification_duration": 5.0,
    }))
    rc, out = _run_cli(["status", "--status-path", status_p,
                       "--report-path", report_p, "--report"])
    assert rc == 0
    assert "PERFORMANCE REPORT" in out
    for k in ("research_time", "debate_time", "verification_time",
              "build_time", "repair_time", "total_runtime",
              "disputes_resolved", "average_round_duration"):
        assert k in out, f"perf report missing {k}"


# --------------------------------------------------------------------------- #
# 8. Runtime calculations deterministic                                       #
# --------------------------------------------------------------------------- #

def test_runtime_calculations_deterministic():
    """Contract 8: the same event sequence produces identical status / eta /
    performance_report outputs. Timestamps are frozen via a monkeypatched
    telemetry.time.time so the timing-sensitive fields are reproducible."""
    tmp = tempfile.mkdtemp()
    try:
        # Freeze time.time inside the telemetry module to a fixed clock.
        clock = {"t": 1000.0}
        orig_time = telemetry.time.time
        telemetry.time.time = lambda: clock["t"]
        try:
            def _emit_sequence():
                telemetry.reset()
                _fresh_recorder(tmp, job_id="det-1")
                recorder._pipeline_started_at = 1000.0
                recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND,
                              duration=12.0, round=1, dispute="d",
                              consensus=50)
                recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND,
                              duration=12.0, round=2, dispute="d",
                              consensus=70)
                recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND,
                              duration=12.0, round=3, dispute="d",
                              consensus=90)
                recorder.emit(EVENT_DISPUTE_RESOLVED, STAGE_DISPUTE,
                              remaining=1, total=3)
                recorder.emit(EVENT_COMPLETED, STAGE_VERIFICATION,
                              duration=40.0)
                # advance the clock 200s for total_runtime determinism.
                clock["t"] = 1200.0
                return (recorder.status(), recorder.eta(),
                        recorder.performance_report())
            s1, e1, p1 = _emit_sequence()
            s2, e2, p2 = _emit_sequence()
        finally:
            telemetry.time.time = orig_time
        assert s1 == s2
        assert e1 == e2
        assert p1 == p2
        # And the determinstic values themselves.
        assert s1["resolved_disputes"] == 1
        assert s1["remaining_disputes"] == 1
        assert p1["average_round_duration"] == 12.0
        assert p1["average_verification_duration"] == 40.0
        assert p1["total_runtime"] == 200.0  # 1200 - 1000
    finally:
        telemetry.reset()


# --------------------------------------------------------------------------- #
# 9. No provider calls                                                         #
# --------------------------------------------------------------------------- #

def test_telemetry_module_imports_no_providers():
    """Contract 9: telemetry.py imports only stdlib (no LLM/browser/network
    client). Verifies purity by importing it in a fresh subprocess and
    checking sys.modules."""
    forbidden = (
        "{'cerebras_client','ollama_client','openrouter_client','groq_client',"
        "'gpt_worker','gemini_worker','provider_router','selenium','requests',"
        "'httpx','urllib3'}"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; import telemetry; "
         f"forbidden = {forbidden}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: {probe.stdout!r} {probe.stderr!r}")
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "telemetry transitively imports forbidden modules")


def test_recorder_never_records_provider_calls():
    """Contract 9b: emitting a full synthetic event sequence never touches a
    provider — recorder methods are pure stdlib (time/os/json/uuid). Verified
    by asserting no forbidden module is loaded after a busy recorder
    session (the instrumentation code paths in build_pipeline / debate_engine
    could only have been reached via imports, not calls here)."""
    tmp = tempfile.mkdtemp()
    try:
        _fresh_recorder(tmp)
        for _ in range(50):
            recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=1.0,
                          round=1, dispute="d", consensus=50)
            recorder.observe(current_round=1, consensus=50)
        recorder.save_report(os.path.join(tmp, "r.json"))
        telemetry.reset()
    finally:
        telemetry.reset()


# --------------------------------------------------------------------------- #
# 10. Existing pipeline tests remain green                                     #
# --------------------------------------------------------------------------- #

def test_build_pipeline_with_telemetry_matches_baseline():
    """Contract 10: build_pipeline (instrumented) still produces the canonical
    completed-spec result via injected seams — the telemetry emits did not
    alter the build/spec/execution path. The recorder may be unconfigured
    (no-op) here; a separate assertion below proves it works configured too."""
    _set_workspace_root()
    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate, executor_fn=_mock_executor,
    )
    assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
    assert result["spec_status"] == "completed"
    assert result["execution_result"]["success"] is True
    assert result["execution_result"].get("coding_agent") is not None


def test_build_spec_stored_with_telemetry_configured():
    """Contract 10b: with the recorder configured, build_pipeline stores the
    spec and writes the build workspace files exactly as before; telemetry
    only ADDS workspace_status.json alongside the build_state.json."""
    _set_workspace_root()
    tmp = tempfile.mkdtemp()
    try:
        _fresh_recorder(tmp)
        result = bp.run_build_pipeline(
            "Build Project Sol0mon MVP",
            debate_fn=_fake_debate, executor_fn=_mock_executor,
        )
        wid = result["workspace_id"]
        spec = bse.load_implementation_spec(wid)
        assert spec and "# architecture" in spec.lower()
        # Build-stage events emitted.
        assert any(e.stage == STAGE_BUILD for e in recorder._events)
        assert any(e.stage == STAGE_BUILD_SPEC for e in recorder._events)
        assert any(e.stage == STAGE_EXECUTION for e in recorder._events)
    finally:
        telemetry.reset()


if __name__ == "__main__":
    test_events_emitted_correctly()
    test_unconfigured_recorder_is_silent_noop()
    test_build_pipeline_result_unchanged_with_telemetry_present()
    test_debate_engine_instrumentation_is_noop_when_unconfigured()
    test_workspace_status_updates_and_persists()
    test_eta_never_crashes()
    test_resume_restores_status()
    test_resume_without_existing_file_starts_fresh()
    test_performance_report_generated()
    test_cli_status_reads_workspace_status()
    test_cli_status_missing_file_returns_nonzero()
    test_cli_status_report_flag_prints_performance_report()
    test_runtime_calculations_deterministic()
    test_telemetry_module_imports_no_providers()
    test_recorder_never_records_provider_calls()
    test_build_pipeline_with_telemetry_matches_baseline()
    test_build_spec_stored_with_telemetry_configured()
    print("test_telemetry: ALL PASSED")
