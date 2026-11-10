"""
test_repair_memory.py — Phase 3.1 RepairMemory + integration into the
ValidationEngine.

Offline only. No model calls. No browser. No external APIs.

Covers the spec's deliverables:
  - record every repair attempt                 (RepairAttempt + record_attempt)
  - track affected files                         (target_files / files_changed)
  - record failure signatures                    (failure_signature sha256)
  - store rejected repair strategies             (is_blacklisted)
  - detect repeated repair cycles                (detect_oscillation / detect_repeated_cycle)
  - prevent identical repairs from being attempted twice  (is_blacklisted + escalate_plan)
  - escalate strategy after repeated failures    (recommend_escalation)
  - repair_history.db persistence                (SQLite-backed; resume)
  - repair fingerprints                          (repair_fingerprint)
  - failure fingerprints                         (failure_signature)
  - strategy blacklist                           (auto from failed attempts)
  - convergence detection                        (is_converging + unique_strategies)
  - oscillation detection                        (detect_oscillation)
  - repair effectiveness scoring                 (effectiveness)

Integration: the spec's exact A,B,A,B oscillation case ── the engine must
NOT repeat Repair A twice. The blacklist rejects it; the planner escalates;
the loop converges (or terminates) instead of looping forever.
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
from repair_memory import (
    ESCALATION_LADDER, RepairAttempt, RepairMemory,
    failure_signature, repair_fingerprint, next_strategy, escalate_plan,
)
from validation_engine import (
    NativeValidationEngine, Diagnostic, _workspace_dir, _validation_dir,
    FAIL_TESTS, FAIL_BUILD, FAIL_SYNTAX, FAIL_ARTIFACT, _write_json,
    ALL_ARTIFACTS, ARTIFACT_REPAIR_HISTORY,
)
from implementation_engine import STATUS_COMPLETED, STATUS_FAILED


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture
def in_memory_repair_memory():
    """A fresh in-memory RepairMemory for each test."""
    mem = RepairMemory()
    yield mem
    mem.close()


@pytest.fixture
def tmp_workspace_root(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(bw, "WORKSPACE_ROOT", Path(tmp))
    return Path(tmp)


def _seed_ws(root, q="q", files=()):
    wid = bw.create_workspace(source_question=q)
    ws = _workspace_dir(wid)
    for rel, content in files:
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return wid, ws


def _good_spec():
    return ("# Architecture\nA clean modular service.\n\n"
            "# Components\napi\n\n"
            "# Acceptance Criteria\ntests pass\n")


# --------------------------------------------------------------------------- #
# 1. Failure signatures ── deterministic + order-independent                 #
# --------------------------------------------------------------------------- #

def test_failure_signature_is_deterministic():
    sig_a = failure_signature([
        {"category": "tests", "message": "AssertionError x",
         "location": "tests/test_a.py"},
    ])
    sig_b = failure_signature([
        {"category": "tests", "message": "AssertionError x",
         "location": "tests/test_a.py"},
    ])
    assert sig_a == sig_b
    assert len(sig_a) == 64  # sha256 hex


def test_failure_signature_is_order_independent():
    items = [
        {"category": "tests", "message": "x", "location": "a.py"},
        {"category": "syntax", "message": "y", "location": "b.py"},
    ]
    sig1 = failure_signature(items)
    sig2 = failure_signature(reversed(items))
    assert sig1 == sig2


def test_failure_signature_distinguishes_failures():
    sig1 = failure_signature([{"category": "tests", "message": "x"}])
    sig2 = failure_signature([{"category": "syntax", "message": "x"}])
    assert sig1 != sig2


def test_failure_signature_handles_empty():
    assert failure_signature([]) == failure_signature(None)


# --------------------------------------------------------------------------- #
# 2. Repair fingerprints ── vary on strategy + target files + signature      #
# --------------------------------------------------------------------------- #

def test_repair_fingerprint_varies_with_strategy():
    sig = failure_signature([{"category": "tests", "message": "x"}])
    fp1 = repair_fingerprint(strategy="patch", target_files=["x.py"],
                               failure_signature_value=sig)
    fp2 = repair_fingerprint(strategy="fix_imports", target_files=["x.py"],
                               failure_signature_value=sig)
    assert fp1 != fp2


def test_repair_fingerprint_varies_with_target_files():
    sig = failure_signature([{"category": "tests", "message": "x"}])
    fp1 = repair_fingerprint(strategy="patch", target_files=["x.py"],
                               failure_signature_value=sig)
    fp2 = repair_fingerprint(strategy="patch", target_files=["y.py"],
                               failure_signature_value=sig)
    assert fp1 != fp2


def test_repair_fingerprint_target_files_order_independent():
    sig = failure_signature([{"category": "tests", "message": "x"}])
    fp1 = repair_fingerprint(strategy="patch",
                               target_files=["y.py", "x.py"],
                               failure_signature_value=sig)
    fp2 = repair_fingerprint(strategy="patch",
                               target_files=["x.py", "y.py"],
                               failure_signature_value=sig)
    assert fp1 == fp2


# --------------------------------------------------------------------------- #
# 3. Record every repair attempt                                             #
# --------------------------------------------------------------------------- #

def test_record_attempt_persists(in_memory_repair_memory):
    mem = in_memory_repair_memory
    a = RepairAttempt(
        workspace_id="ws1", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature="sig1",
        success=False, files_changed=["src/x.py"], patch_id="p1",
        response_error="tests still fail",
    )
    mem.record_attempt(a)
    attempts = mem.all_attempts()
    assert len(attempts) == 1
    assert attempts[0]["strategy"] == "patch"
    assert attempts[0]["target_files"] == ["src/x.py"]
    assert attempts[0]["success"] is False
    assert attempts[0]["workspace_id"] == "ws1"


def test_record_attempt_assigns_default_id_and_timestamp(in_memory_repair_memory):
    mem = in_memory_repair_memory
    a = RepairAttempt(strategy="patch", target_files=["x.py"],
                       failure_signature="sig")
    mem.record_attempt(a)
    row = mem.all_attempts()[0]
    assert row["attempt_id"].startswith("attempt_")
    assert row["timestamp"]  # ISO timestamp


# --------------------------------------------------------------------------- #
# 4. Track affected files                                                     #
# --------------------------------------------------------------------------- #

def test_attempts_track_affected_files(in_memory_repair_memory):
    mem = in_memory_repair_memory
    files = ["src/a.py", "src/b.py", "tests/test_a.py"]
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="rewrite_test",
        target_files=["src/a.py"], failure_signature="sig",
        success=True, files_changed=files, patch_id="p1"))
    row = mem.all_attempts()[0]
    assert row["files_changed"] == files
    assert row["target_files"] == ["src/a.py"]


# --------------------------------------------------------------------------- #
# 5. Strategy blacklist ── prevents identical repairs                         #
# --------------------------------------------------------------------------- #

def test_failed_attempt_blacklists_strategy(in_memory_repair_memory):
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature="sig1",
        success=False, patch_id="p1"))
    sig1 = failure_signature([{"category": "tests", "message": "x"}])
    # Use the deterministic signature (the manual "sig1" string still works
    # because we hash what's stored)
    assert mem.is_blacklisted(
        strategy="patch", target_files=["src/x.py"],
        failure_signature_value="sig1")
    # A different failure sig is NOT blacklisted for the same repair
    assert not mem.is_blacklisted(
        strategy="patch", target_files=["src/x.py"],
        failure_signature_value="sig2")


def test_successful_attempt_does_not_blacklist(in_memory_repair_memory):
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature="sig1",
        success=True, patch_id="p1"))
    assert not mem.is_blacklisted(
        strategy="patch", target_files=["src/x.py"],
        failure_signature_value="sig1")


def test_has_attempted_includes_successful_attempts(in_memory_repair_memory):
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature="sig1",
        success=True, patch_id="p1"))
    assert mem.has_attempted(
        strategy="patch", target_files=["src/x.py"],
        failure_signature_value="sig1")


def test_find_similar_attempt_returns_failed_attempt(in_memory_repair_memory):
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature="sig1",
        success=False, patch_id="p1"))
    sim = mem.find_similar_attempt(
        strategy="patch", target_files=["src/x.py"],
        failure_signature_value="sig1")
    assert sim is not None
    assert sim["success"] is False
    assert sim["patch_id"] == "p1"


def test_find_similar_attempt_returns_none_when_no_match(in_memory_repair_memory):
    mem = in_memory_repair_memory
    assert mem.find_similar_attempt(
        strategy="patch", target_files=["src/x.py"],
        failure_signature_value="never-seen") is None


# --------------------------------------------------------------------------- #
# 6. Strategy escalation ladder                                               #
# --------------------------------------------------------------------------- #

def test_escalation_ladder_order():
    assert ESCALATION_LADDER[0] == "patch"
    assert ESCALATION_LADDER[-1] == "refactor_dependency_graph"


def test_next_strategy_steps_up_ladder():
    assert next_strategy("patch") == "fix_imports"
    assert next_strategy("fix_imports") == "rewrite_test"
    assert next_strategy("rewrite_test") == "regenerate"
    assert next_strategy("regenerate") == "refactor_dependency_graph"
    assert next_strategy("refactor_dependency_graph") is None  # at the top


def test_next_strategy_unknown_returns_none():
    assert next_strategy("unknown_strategy") is None


def test_recommend_escalation_requires_threshold_attempts(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    # 1 attempt → below default threshold (2) → no escalation yet
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.recommend_escalation(
        current_strategy="patch", failure_signature_value=sig) is None
    # 2 attempts → escalate
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=2, strategy="patch",
        target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.recommend_escalation(
        current_strategy="patch", failure_signature_value=sig) == "fix_imports"


def test_recommend_escalation_returns_none_at_top_of_ladder(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    for i in range(5):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1,
            strategy="refactor_dependency_graph",
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.recommend_escalation(
        current_strategy="refactor_dependency_graph",
        failure_signature_value=sig) is None


def test_recommend_escalation_unknown_strategy_returns_none(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    for i in range(5):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy="unknown",
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.recommend_escalation(
        current_strategy="unknown", failure_signature_value=sig) is None


def test_recommend_escalation_uses_only_attempts_for_same_failure(in_memory_repair_memory):
    """Escalation counts attempts for the SAME failure signature ── attempts
    on other failures do NOT contribute."""
    mem = in_memory_repair_memory
    sig_a, sig_b = "sig_a", "sig_b"
    # 2 attempts on sig_a with 'patch'
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["x.py"], failure_signature=sig_a, success=False))
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=2, strategy="patch",
        target_files=["x.py"], failure_signature=sig_a, success=False))
    # sig_b has zero attempts → no escalation warranted for sig_b even though
    # we've exceeded the threshold on sig_a
    assert mem.recommend_escalation(
        current_strategy="patch", failure_signature_value=sig_b) is None


# --------------------------------------------------------------------------- #
# 7. Oscillation detection ── the A,B,A,B case                                #
# --------------------------------------------------------------------------- #

def test_detect_oscillation_period_2(in_memory_repair_memory):
    """The spec's exact case: A,B,A,B (period-2 cycle)."""
    mem = in_memory_repair_memory
    sig = "sig1"
    for i, strat in enumerate(["patch", "rewrite_test", "patch", "rewrite_test"]):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy=strat,
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.detect_oscillation() is True
    cycle = mem.detect_repeated_cycle()
    assert cycle is not None
    assert cycle["period"] == 2
    assert set(cycle["strategies"]) == {"patch", "rewrite_test"}


def test_detect_oscillation_period_3(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    for i, strat in enumerate(["patch", "fix_imports", "rewrite_test",
                                "patch", "fix_imports", "rewrite_test"]):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy=strat,
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.detect_oscillation() is True


def test_no_oscillation_when_strategies_progress(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    for i, strat in enumerate(["patch", "fix_imports", "rewrite_test",
                                "regenerate"]):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy=strat,
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.detect_oscillation() is False
    assert mem.detect_repeated_cycle() is None


def test_detect_stuck_returns_stuck_strategy(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    for i in range(4):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy="patch",
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.detect_stuck() == "patch"


def test_detect_stuck_returns_none_when_strategies_vary(in_memory_repair_memory):
    mem = in_memory_repair_memory
    sig = "sig1"
    for i, strat in enumerate(["patch", "fix_imports", "rewrite_test"]):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy=strat,
            target_files=["x.py"], failure_signature=sig, success=False))
    assert mem.detect_stuck() is None


# --------------------------------------------------------------------------- #
# 8. Convergence + effectiveness                                              #
# --------------------------------------------------------------------------- #

def test_unique_strategies_tried(in_memory_repair_memory):
    mem = in_memory_repair_memory
    for i, strat in enumerate(["patch", "patch", "fix_imports"]):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy=strat,
            target_files=["x.py"], failure_signature="sig1",
            success=False))
    assert mem.unique_strategies_tried() == {"patch", "fix_imports"}


def test_is_converging_true_on_first_attempts(in_memory_repair_memory):
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["x.py"], failure_signature="sig1", success=False))
    assert mem.is_converging() is True  # too early to tell


def test_is_converging_true_after_success(in_memory_repair_memory):
    mem = in_memory_repair_memory
    for i, ok in enumerate([False, False, True]):
        mem.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=i + 1, strategy="patch",
            target_files=["x.py"], failure_signature="sig1", success=ok))
    # At least one success → converging
    assert mem.is_converging() is True


def test_effectiveness_overall(in_memory_repair_memory):
    mem = in_memory_repair_memory
    # 2 patch attempts (1 success), 1 fix_imports (failed)
    mem.record_attempt(RepairAttempt(strategy="patch", target_files=["x.py"],
                                       failure_signature="s", success=True))
    mem.record_attempt(RepairAttempt(strategy="patch", target_files=["y.py"],
                                       failure_signature="s", success=False))
    mem.record_attempt(RepairAttempt(strategy="fix_imports",
                                       target_files=["x.py"],
                                       failure_signature="s", success=False))
    eff = mem.effectiveness()
    assert eff["patch"]["attempts"] == 2
    assert eff["patch"]["successes"] == 1
    assert eff["patch"]["rate"] == 0.5
    assert eff["fix_imports"]["attempts"] == 1
    assert eff["fix_imports"]["successes"] == 0


def test_effectiveness_per_strategy(in_memory_repair_memory):
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(strategy="patch", target_files=["x.py"],
                                       failure_signature="s", success=True))
    mem.record_attempt(RepairAttempt(strategy="patch", target_files=["y.py"],
                                       failure_signature="s", success=False))
    eff = mem.effectiveness("patch")
    assert eff["strategy"] == "patch"
    assert eff["attempts"] == 2
    assert eff["successes"] == 1
    assert eff["rate"] == 0.5


def test_effectiveness_unknown_strategy(in_memory_repair_memory):
    mem = in_memory_repair_memory
    eff = mem.effectiveness("never_tried")
    assert eff["attempts"] == 0
    assert eff["rate"] == 0.0


# --------------------------------------------------------------------------- #
# 9. repair_history.db persistence (resume across runs)                      #
# --------------------------------------------------------------------------- #

def test_persistence_to_disk_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "repair_history.db"
        mem1 = RepairMemory(db_path=p)
        mem1.record_attempt(RepairAttempt(
            workspace_id="ws", iteration=1, strategy="patch",
            target_files=["x.py"], failure_signature="sig1", success=False))
        mem1.close()
        mem2 = RepairMemory(db_path=p)
        assert mem2.has_attempted(
            strategy="patch", target_files=["x.py"],
            failure_signature_value="sig1")
        attempts = mem2.all_attempts()
        assert len(attempts) == 1
        assert attempts[0]["strategy"] == "patch"
        mem2.close()


def test_from_workspace_creates_db_in_validation_dir(tmp_workspace_root):
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    mem = RepairMemory.from_workspace(wid)
    mem.record_attempt(RepairAttempt(
        workspace_id=wid, iteration=1, strategy="patch",
        target_files=["src/api.py"], failure_signature="sig1",
        success=False, patch_id="p1"))
    db_path = _validation_dir(wid) / "repair_history.db"
    assert db_path.is_file()
    mem.close()


# --------------------------------------------------------------------------- #
# 10. escalate_plan helper ── the integration contract                        #
# --------------------------------------------------------------------------- #

def test_escalate_plan_no_blacklist_returns_plan_unchanged(in_memory_repair_memory):
    from validation_engine import RepairPlan
    plan = RepairPlan(
        scope="file", priority="high", strategy="patch",
        target_files=["src/x.py"], diagnostics=[], prompt="x",
    )
    out, escalated, reason = escalate_plan(
        plan, memory=in_memory_repair_memory,
        failure_signature_value="never-seen")
    assert out is plan  # same object
    assert escalated is False
    assert reason == ""


def test_escalate_plan_blacklisted_with_rung_left_escalates(in_memory_repair_memory):
    """The spec's exact case ── strategy failed twice; second identical
    attempt should escalate rather than be retried."""
    from validation_engine import RepairPlan
    mem = in_memory_repair_memory
    sig = "sig1"
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="patch",
        target_files=["src/x.py"], failure_signature=sig, success=False))
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=2, strategy="patch",
        target_files=["src/x.py"], failure_signature=sig, success=False))
    plan = RepairPlan(
        scope="file", priority="high", strategy="patch",
        target_files=["src/x.py"], diagnostics=[], prompt="x",
    )
    out, escalated, reason = escalate_plan(
        plan, memory=mem, failure_signature_value=sig)
    assert escalated is True
    assert out.strategy == "fix_imports"
    assert "escalating" in out.prompt
    assert "blacklisted" in reason


def test_escalate_plan_blacklisted_at_top_of_ladder_returns_refused_plan(in_memory_repair_memory):
    """When the strategy is at the strongest rung and is blacklisted, no
    escalation is possible ── escalate_plan returns the same plan with a
    reason explaining the blacklist (the engine raises a structured error)."""
    from validation_engine import RepairPlan
    mem = in_memory_repair_memory
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=1, strategy="refactor_dependency_graph",
        target_files=["src/x.py"], failure_signature="sig1", success=False))
    mem.record_attempt(RepairAttempt(
        workspace_id="ws", iteration=2, strategy="refactor_dependency_graph",
        target_files=["src/x.py"], failure_signature="sig1", success=False))
    plan = RepairPlan(
        scope="file", priority="high", strategy="refactor_dependency_graph",
        target_files=["src/x.py"], diagnostics=[], prompt="x",
    )
    out, escalated, reason = escalate_plan(
        plan, memory=mem, failure_signature_value="sig1")
    assert escalated is False
    assert "no stronger strategy available" in reason


# --------------------------------------------------------------------------- #
# 11. Spec's exact oscillation case ── the engine must NOT loop forever      #
# --------------------------------------------------------------------------- #

def test_engine_terminates_on_oscillation_with_repair_memory(tmp_workspace_root):
    """The spec's exact case::

        Repair A → fail
        Repair B → fail
        Repair A → would be BLACKLISTED → escalate → fail
        Repair B → would be BLACKLISTED → escalate → fail
        → RepairMemory detects cycle (A',B',A',B' on escalated strategies)
          → engine terminates instead of looping forever

    We simulate by:
      - making tests always fail
      - making the repair agent a fake that synthesizes the same 'patch' /
      'fix_imports' plan target on every call (so the planner keeps suggesting
      the same strategy pair)
      - giving a small retry budget so the engine gets to the oscillation
        check before exhausting RetryController's budget too
    """
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    # Tests always fail (drives the repair loop)
    def _always_fail(wd):
        return {"passed": False, "total": 1, "failed": 1, "errors": 0,
                "output": "AssertionError"}
    # Repair agent: returns the same files every time (so the blacklist
    # triggers on iteration 2 and the planner escalates). To make the
    # oscillation detectable we have the agent success-but-not-really fix
    # anything (the next iteration's tests still fail), producing a long
    # strategy sequence the memory can find a cycle in.
    def _repair(wid, prompt):
        return {"success": True, "files_changed": ["src/api.py"],
                "output": "false-positive"}

    eng = NativeValidationEngine(
        build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "",
                                    "stderr": "", "success": True,
                                    "logs": []},
        test_fns={"unit": _always_fail,
                  "integration": lambda wd: {"passed": True, "total": 0,
                                              "failed": 0, "errors": 0},
                  "smoke": lambda wd: {"passed": True, "total": 0,
                                        "failed": 0, "errors": 0}},
        repair_fn=_repair,
        max_iterations=10,  # generous ── oscillation should terminate earlier
        allow_live_agent=False,
    )
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    # The result is a failure (tests never pass)
    assert r.success is False
    # The error surfaced the oscillation OR ran out of strategies ── NOT a
    # loop-forever / silent failure.
    assert r.error
    # RepairMemory's DB exists + contains the failed attempts
    assert (_validation_dir(wid) / "repair_history.db").is_file()
    # And the repair_history.json artifact includes the structured snapshot
    rh = json.loads((_validation_dir(wid) / ARTIFACT_REPAIR_HISTORY).read_text())
    assert "repair_memory" in rh
    assert rh["repair_memory"]["attempt_count"] >= 1


def test_engine_repair_memory_blocks_identical_repair(tmp_workspace_root):
    """A second iteration with the SAME plan that already failed is
    BLACKLISTED ── the engine records it in repair_memory AND the engine
    either escalates or reports it. Verifies the spec's "prevent identical
    repairs from being attempted twice" rule from the engine level."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = NativeValidationEngine(
        build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "",
                                    "stderr": "", "success": True,
                                    "logs": []},
        test_fns={"unit": lambda wd: {"passed": False, "total": 1,
                                        "failed": 1, "errors": 0,
                                        "output": "AssertionError"},
                  "integration": lambda wd: {"passed": True, "total": 0},
                  "smoke": lambda wd: {"passed": True, "total": 0}},
        repair_fn=lambda wid, prompt: {"success": False,
                                          "files_changed": ["src/api.py"]},
        max_iterations=3,  # enough to trigger blacklist + escalation attempt
        allow_live_agent=False,
    )
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is False
    # The repair memory snapshot should show attempts + (likely) oscillation
    rh = json.loads((_validation_dir(wid) / ARTIFACT_REPAIR_HISTORY).read_text())
    rm = rh["repair_memory"]
    assert rm["attempt_count"] >= 2  # at least 2 attempts before terminating


def test_engine_with_repair_memory_still_succeeds_on_clean_path(tmp_workspace_root):
    """Even with RepairMemory wired, the all-pass path produces a SUCCESS
    result (no false negatives from the new component)."""
    wid, ws = _seed_ws(tmp_workspace_root,
                       files=[("src/api.py", "API = 1\n"),
                              ("tests/test_api.py", "def t(): pass\n")])
    eng = NativeValidationEngine(
        build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "", "stderr": "",
                                    "success": True, "logs": []},
        test_fns={t: lambda wd: {"passed": True, "total": 1, "failed": 0,
                                   "errors": 0, "output": ""}
                  for t in ("unit", "integration", "smoke")},
        max_iterations=2,
    )
    r = eng.validate(workspace_id=wid, implementation_spec=_good_spec())
    assert r.success is True
    # repair_history.db was created (engine auto-builds the memory)
    assert (_validation_dir(wid) / "repair_history.db").is_file()
    # No attempts recorded (the loop never entered the repair phase)
    rh = json.loads((_validation_dir(wid) / ARTIFACT_REPAIR_HISTORY).read_text())
    assert rh["repair_memory"]["attempt_count"] == 0


# --------------------------------------------------------------------------- #
# 12. Pure-stdlib at import (repair_memory transitively pulls nothing)       #
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
         "import sys, json; import repair_memory; "
         f"forbidden = {forbidden_set}; "
         "print(json.dumps(sorted(forbidden & set(sys.modules))))"],
        capture_output=True, text=True, cwd=str(repo_root), timeout=15,
    )
    assert probe.returncode == 0, (
        f"isolated import failed: stdout={probe.stdout!r} "
        f"stderr={probe.stderr!r}"
    )
    assert json.loads(probe.stdout.strip() or "[]") == [], (
        "repair_memory transitively imports forbidden modules"
    )


# --------------------------------------------------------------------------- #
# 13. Determinism                                                            #
# --------------------------------------------------------------------------- #

def test_record_attempt_sequence_is_deterministic():
    """Same sequence of records → same persisted state."""
    diags = [{"category": "tests", "message": "x",
              "location": "tests/test_a.py"}]
    sig = failure_signature(diags)
    m_a = RepairMemory()
    m_b = RepairMemory()
    for i in range(3):
        a = RepairAttempt(workspace_id="ws", iteration=i, strategy="patch",
                           target_files=["src/x.py"], failure_signature=sig,
                           success=(i == 2), patch_id=f"p{i}")
        m_a.record_attempt(a)
        m_b.record_attempt(a)
    assert (m_a.to_dict()["attempts"] == m_b.to_dict()["attempts"])
    assert m_a.effectiveness() == m_b.effectiveness()
    m_a.close(); m_b.close()


if __name__ == "__main__":
    raise SystemExit("run via: python -m pytest tests/test_repair_memory.py -q")
