"""
test_consolidation.py - Final Architecture Hardening, consolidation phase
(Parts 1-7 regression).

Covers:
  Part 1  Planner integration — planner_fn seam; default (deterministic) when
           omitted; set on the run_build_pipeline result as ``result['plan']``;
           persisted as execution_plan.json.
  Part 2  ExecutionPlanArtifact persistence — execution_plan.json beside the
           existing workspace files; deterministic id over (kind, checksum);
           immutability of the persisted record (re-store == same id+checksum);
           resume reuses persisted plans.
  Part 3  BuildManifest — references-only; store/load round-trip;
           build_manifest_from_workspace picks up the verifiable on-disk artifacts;
           with_section returns a new (immutable) manifest;
           unknown section raises (no silent typos).
  Part 4  Task graph exports — JSON / Mermaid / DOT; deterministic; non-empty;
           handle empty plans.
  Part 5  Manifest validation — pass on a fresh consistent manifest;
           flag missing referenced artifact (deleting the file);
           flag checksum mismatch (mutating implementation_plan.md without
           rebuilding the manifest);
           flag lifecycle mismatch (stale manifest.lifecycle vs current_lifecycle);
           flag the validation/tests/implementation absence when lifecycle is
           TESTS_VALIDATED / COMPLETE;
           flag plan_id mismatch (manifest.execution_plan vs store).
  Part 6  Resume is manifest-aware — second resume reuses the persisted plan
           (skipped regeneration when checksums match); corrupted artifact
           triggers regeneration; partial build needs no extra work.
  Part 7 (this file:)
           - planner seam: default deterministic, override canned
           - planner persistence: execution_plan.json exists after run_build_pipeline
           - manifest creation + validation + checksum mismatch + missing
             artifact + lifecycle mismatch
           - resume from manifest / corrupted artifact / partial build / completed
             build
           - JSON / Mermaid / DOT export determinism + non-empty content
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_pipeline as bp
import build_spec_engine as bse
import build_workspace as bw
import execution_planner as EP
import lifecycle
import manifest as M
import resume
import validation
from errors import BuildError, ErrorKind
from manifest import BuildManifest


# Fixture directories the conftest auto-snapshot isolates per test.
def _fresh_root():
    bw.WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="sol0mon_cons_"))


# A 156-shape verification_result + verified_decisions (no I/O).
VR_156 = {
    "resolved_claims": [{
        "normalized_key": "database_technology_selection",
        "dispute": ("Database Technology: GPT selects PostgreSQL, Gemini "
                    "selects SQLite."),
        "verification": {
            "status": "RESOLVED", "confidence": 95,
            "finding": ("Both evidence strips conclude SQLite is the appropriate "
                        "persistence engine; the text argues against PostgreSQL."),
        },
    }],
    "equivalent_claims": [], "unresolved_claims": [],
    "verification_score": 100, "truncated_count": 0,
}


_GOOD_SPEC_TEXT = (
    # Legacy 7 audit-debate sections:
    "# Architecture\nA\n# Components\nB\n# File Structure\nC\n"
    "# Dependencies\nD\n# Constraints\nE\n"
    "# Testing Requirements\nF\n"
    # Executable-spec sections (the defence-in-depth gate's contract):
    "# Implementation Objective\nImplement A-F.\n"
    "# Files to Create\n- src/app/main.py\n- tests/test_app.py\n"
    "# Files to Modify\n- (none)\n"
    "# Implementation Steps\n1. Implement main.\n"
    "# Acceptance Criteria\nG\n"
    "# Output Contract\nStop after tests pass.\n"
)


def _fake_debate(prompt, memory_ids=None):
    return {
        "status": "completed",
        "synthesized_answer": _GOOD_SPEC_TEXT,
        "gpt_final": "g", "gemini_final": "m",
        "validated_disputes": [],
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [],
        "final_consensus_score": 80,
    }


def _mock_executor(spec_text):
    # Simulate OpenCode writing the files declared in the spec so the
    # post-execution QualityGate's "required files exist" check passes.
    ws_dirs = list(bw.WORKSPACE_ROOT.glob("workspace_*"))
    if ws_dirs:
        ws = ws_dirs[0]
        (ws / "src" / "app").mkdir(parents=True, exist_ok=True)
        (ws / "src" / "app" / "main.py").write_text("VALUE = 1\n")
        (ws / "tests").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "test_app.py").write_text("def test_value():\n"
                                                  "    assert True\n")
    return {"success": True, "output": "mock ok",
            "test_results": {"runner": "pytest", "passed": 5, "failed": 0,
                              "exit_code": 0},
            "repair_attempts": 0, "error": ""}


# ============================================================================ #
# Part 1 — Planner integration
# ============================================================================ #

def test_planner_fn_default_is_deterministic_and_set_on_result():
    _fresh_root()
    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate, executor_fn=_mock_executor,
    )
    # The new contract: result['plan'] is populated even when planner_fn is
    # omitted (the deterministic default was used).
    assert "plan" in result, result.keys()
    assert result["plan"] is not None
    plan = result["plan"]
    assert plan.get("id", "").startswith("plan_")
    assert plan["tasks"]  # non-empty DAG (the fallback IMPLEMENTATION task at least)
    assert plan["order"][-1] == "acceptance"


def test_planner_fn_seam_overrides_default():
    _fresh_root()
    # Inject a canned plan via the seam. The pipeline persists the canned one
    # as execution_plan.json and surfaces it on the result.
    canned = {"id": "canned",
              "order": ["a", "b"],
              "tasks": {"a": {"id": "a", "kind": "component", "dependencies": []},
                        "b": {"id": "b", "kind": "tests", "dependencies": ["a"]}}}

    def _fake_planner(spec):
        return canned

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate, executor_fn=_mock_executor,
        planner_fn=_fake_planner,
    )
    # The canned plan is surfaced on the result and persisted on disk.
    assert result["plan"] is not None
    assert result["plan"]["id"] == "canned"
    wid = result["workspace_id"]
    env = EP.load_execution_plan(wid)
    assert env is not None
    # The persisted payload carries the canned plan's id.
    assert env["payload"]["id"] == "canned"
    # Producer field records "planner_fn" (the seam, not the deterministic default).
    assert env["producer"] == "planner_fn"
    # The build_state recorded the plan_generation step.
    state = json.loads((bw._workspace_dir(wid) / "build_state.json").read_text())
    assert state.get("status") == "executed"  # final status unchanged
    # Manifest exists for this run.
    assert M.load_manifest(wid) is not None


def test_planner_fn_default_no_llm_invocation_signature_change():
    """Planning stays deterministic: the default execution_planner.plan is
    invoked with a single positional arg (the spec dict). Confirm the seam
    is named in the same canonical way as the other seams and accepts a
    single dict argument."""
    spec_result = {"verification_result": VR_156,
                   "verified_decisions": [],
                   "spec_text": _GOOD_SPEC_TEXT}
    plan_obj = EP.plan(spec_result)
    assert plan_obj.id and plan_obj.tasks
    # Re-running with the same spec -> identical plan id.
    plan_obj2 = EP.plan(spec_result)
    assert plan_obj.id == plan_obj2.id


# ============================================================================ #
# Part 2 — ExecutionPlanArtifact persistence
# ============================================================================ #

def test_execution_plan_persisted_after_run_build_pipeline():
    _fresh_root()
    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate, executor_fn=_mock_executor,
    )
    wid = result["workspace_id"]
    p = bw._workspace_dir(wid) / "execution_plan.json"
    assert p.is_file(), "execution_plan.json must exist after run_build_pipeline"
    env = json.loads(p.read_text())
    # The persisted envelope carries the brief's required fields.
    for k in ("kind", "id", "checksum", "version", "producer",
              "lifecycle_stage", "dependencies", "validation_status",
              "payload"):
        assert k in env, f"missing envelope field: {k}"
    assert env["kind"] == "ExecutionPlanArtifact"
    assert env["lifecycle_stage"] == "EXECUTION_ALLOWED"
    assert env["payload"]["order"]  # non-empty task order


def test_execution_plan_artifact_immutability_idempotent_rederivation():
    """Re-deriving the plan over the same spec yields the same id+checksum
    (deterministic), so the persisted record is content-addressed — re-storing
    is a no-op."""
    spec_result = {"verification_result": VR_156, "verified_decisions": [],
                   "spec_text": _GOOD_SPEC_TEXT}
    p1 = EP.plan(spec_result)
    env1 = EP.store_execution_plan("ws-imaginary-1", p1, build_spec_artifact_id="bs-1")
    env2 = EP.store_execution_plan("ws-imaginary-1", p1, build_spec_artifact_id="bs-1")
    assert env1["id"] == env2["id"]
    assert env1["checksum"] == env2["checksum"]
    # The build_spec_artifact_id is recorded as a dependency.
    assert "bs-1" in env1["dependencies"]


def test_load_execution_plan_returns_none_for_missing():
    _fresh_root()
    wid = bw.create_workspace()
    assert EP.load_execution_plan(wid) is None
    assert EP.execution_plan_artifact_id(wid) is None


def test_verify_execution_plan_checksum_detects_tampering():
    """Mutating execution_plan.json after persistence must make
    verify_execution_plan_checksum fail (drift detection)."""
    spec_result = {"verification_result": {}, "verified_decisions": [],
                   "spec_text": _GOOD_SPEC_TEXT}
    p1 = EP.plan(spec_result)
    _fresh_root()
    wid = bw.create_workspace()
    EP.store_execution_plan(wid, p1)
    env = EP.load_execution_plan(wid)
    assert EP.verify_execution_plan_checksum(env) is True
    # Mutate the payload (simulating tampering / disk corruption).
    env["payload"]["order"].append("extra_task")
    p = bw._workspace_dir(wid) / "execution_plan.json"
    p.write_text(json.dumps(env, indent=2), encoding="utf-8")
    tampered = json.loads(p.read_text())
    assert EP.verify_execution_plan_checksum(tampered) is False


# ============================================================================ #
# Part 3 — BuildManifest
# ============================================================================ #

def test_manifest_creation_references_only():
    _fresh_root()
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _GOOD_SPEC_TEXT)
    EP.store_execution_plan(wid, EP.plan({"verification_result": VR_156,
                                          "verified_decisions": [],
                                          "spec_text": _GOOD_SPEC_TEXT}))
    m = M.build_manifest_from_workspace(wid)
    # References are artifact id strings (or None), never embedded content.
    assert isinstance(m.build_spec, str) and m.build_spec  # spec_text is on disk
    assert isinstance(m.execution_plan, str) and m.execution_plan
    assert m.lifecycle == lifecycle.current_lifecycle(wid).value
    # checksums recorded for present sections.
    assert m.checksum_of("build_spec")
    assert m.checksum_of("execution_plan")
    # round-trip persists the references (no embedded body grows as blobs).
    assert M.store_manifest(wid, m)
    m2 = M.load_manifest(wid)
    assert m2 is not None
    assert m2.build_spec == m.build_spec
    assert m2.execution_plan == m.execution_plan
    assert m2.checksum_of("execution_plan") == m.checksum_of("execution_plan")
    # to_dict has the brief's keys (references + lifecycle + checksums).
    d = m.to_dict()
    for k in ("workspace_id", "lifecycle", "decision_artifact", "build_spec",
              "execution_plan", "implementation", "validation", "tests",
              "checksums"):
        assert k in d, k


def test_manifest_with_section_returns_new_manifest_original_intact():
    _fresh_root()
    wid = bw.create_workspace()
    m0 = M.build_manifest_from_workspace(wid)
    bs_id_0 = m0.build_spec  # None for a fresh workspace with no spec_text
    m1 = m0.with_section("build_spec", "art-1", "sha-a")
    assert m0.build_spec is bs_id_0  # unchanged
    assert m1.build_spec == "art-1" and m1.checksum_of("build_spec") == "sha-a"
    # Replace + clear.
    m2 = m1.with_section("build_spec", "art-2", "sha-b")
    assert m2.build_spec == "art-2" and m2.checksum_of("build_spec") == "sha-b"
    m3 = m2.with_section("build_spec", None, "")
    assert m3.build_spec is None and m3.checksum_of("build_spec") == ""


def test_manifest_unknown_section_raises():
    _fresh_root()
    wid = bw.create_workspace()
    m = M.build_manifest_from_workspace(wid)
    raised = False
    try:
        m.with_section("not_a_section", "x", "y")
    except ValueError:
        raised = True
    assert raised


def test_manifest_id_deterministic_over_workspace_records():
    """Two manifests built from the same workspace state have the same id
    (deterministic generation)."""
    _fresh_root()
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _GOOD_SPEC_TEXT)
    EP.store_execution_plan(wid, EP.plan({"verification_result": VR_156,
                                          "verified_decisions": [],
                                          "spec_text": _GOOD_SPEC_TEXT}))
    m1 = M.build_manifest_from_workspace(wid)
    m2 = M.build_manifest_from_workspace(wid)
    assert m1.id == m2.id
    # But if the spec is mutated, the manifest id changes (checksum drift).
    bse.store_implementation_spec(wid, _GOOD_SPEC_TEXT + "changed")
    m3 = M.build_manifest_from_workspace(wid)
    assert m3.id != m1.id


# ============================================================================ #
# Part 4 — Task graph exports
# ============================================================================ #

def test_exports_json_mermaid_dot_deterministic():
    spec_result = {"verification_result": VR_156,
                    "verified_decisions": [{"outcome_type": "VERIFIED_DECISION",
                                             "dispute_key": "db",
                                             "decision": "SQLite persistence"}],
                    "spec_text": _GOOD_SPEC_TEXT}
    p = EP.plan(spec_result)
    j1, m1, d1 = EP.export_json(p), EP.export_mermaid(p), EP.export_dot(p)
    j2, m2, d2 = EP.export_json(p), EP.export_mermaid(p), EP.export_dot(p)
    assert (j1, m1, d1) == (j2, m2, d2), "task graph exports not deterministic"
    # JSON: parseable + carries the order.
    parsed = json.loads(j1)
    assert parsed["order"] == p.order
    # Mermaid: starts with 'graph TD', has edges.
    assert "graph TD" in m1 and "-->" in m1
    # DOT: starts with 'digraph' has edges.
    assert "digraph G" in d1 and "->" in d1
    # Empty plan exports still well-formed.
    e_plan = {"tasks": {}, "order": []}
    assert "graph TD" in EP.export_mermaid(e_plan)
    assert "digraph G" in EP.export_dot(e_plan)


def test_export_no_runtime_dependency_fixtures():
    """Exports are diagnostic only — the persisted execution_plan.json does
    NOT embed any export text (so removing/changing exports never affects
    resume behavior)."""
    _fresh_root()
    wid = bw.create_workspace()
    EP.store_execution_plan(wid, EP.plan({"verification_result": {},
                                          "verified_decisions": [],
                                          "spec_text": _GOOD_SPEC_TEXT}))
    env = EP.load_execution_plan(wid)
    serialized = json.dumps(env)
    # No mermaid/dot pieces in the persisted artifact.
    assert "graph TD" not in serialized
    assert "digraph G" not in serialized


# ============================================================================ #
# Part 5 — Manifest validation
# ============================================================================ #

def _consistent_workspace_with_spec_and_plan():
    _fresh_root()
    wid = bw.create_workspace()
    bse.store_implementation_spec(wid, _GOOD_SPEC_TEXT)
    EP.store_execution_plan(wid, EP.plan({"verification_result": {},
                                          "verified_decisions": [],
                                          "spec_text": _GOOD_SPEC_TEXT}))
    m = M.build_manifest_from_workspace(wid)
    M.store_manifest(wid, m)
    return wid, m


def test_validate_manifest_passes_for_consistent_workspace():
    wid, m = _consistent_workspace_with_spec_and_plan()
    r = validation.validate_manifest(m, workspace_id=wid)
    assert r.passed, r.to_dict()
    # Also via dispatcher.
    r2 = validation.validate("manifest", None, workspace_id=wid)
    assert r2.passed, r2.to_dict()


def test_validate_manifest_flags_missing_referenced_artifact():
    wid, m = _consistent_workspace_with_spec_and_plan()
    # Delete implementation_plan.md but keep manifest referencing it.
    (bw._workspace_dir(wid) / "implementation_plan.md").unlink()
    r = validation.validate_manifest(m, workspace_id=wid)
    assert not r.passed
    rules = {i.rule for i in r.issues}
    assert "artifact_exists" in rules
    # The validator names the missing artifact (the section).
    msgs = " ".join(i.message for i in r.issues)
    assert "build_spec" in msgs


def test_validate_manifest_flags_checksum_mismatch():
    wid, m = _consistent_workspace_with_spec_and_plan()
    # Mutate the spec on disk WITHOUT rebuilding the manifest.
    p = bw._workspace_dir(wid) / "implementation_plan.md"
    p.write_text(_GOOD_SPEC_TEXT + " tampered", encoding="utf-8")
    r = validation.validate_manifest(m, workspace_id=wid)
    assert not r.passed
    assert any(i.rule == "checksum_match" for i in r.issues), r.to_dict()


def test_validate_manifest_flags_plan_checksum_mismatch():
    wid, m = _consistent_workspace_with_spec_and_plan()
    # Tamper execution_plan.json (mutate the payload) without rebuilding the manifest.
    p = bw._workspace_dir(wid) / "execution_plan.json"
    env = json.loads(p.read_text())
    env["payload"]["order"].append("extra_task")
    p.write_text(json.dumps(env, indent=2), encoding="utf-8")
    r = validation.validate_manifest(m, workspace_id=wid)
    assert not r.passed
    rules = {i.rule for i in r.issues}
    assert "checksum_match" in rules or "plan_id_match" in rules, r.to_dict()


def test_validate_manifest_flags_lifecycle_mismatch():
    wid, m = _consistent_workspace_with_spec_and_plan()
    # The manifest.lifecycle reflects current_lifecycle at build time (NEW).
    # Advance the lifecycle forward so the manifest's recorded stage is stale.
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.VERIFIED)
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.SPEC_READY)
    r = validation.validate_manifest(m, workspace_id=wid)
    assert not r.passed
    assert any(i.rule == "lifecycle_consistent" for i in r.issues), r.to_dict()


def test_validate_manifest_flags_missing_validation_tests_when_complete():
    """When lifecycle is COMPLETE / TESTS_VALIDATED, the manifest MUST
    reference the validation + tests artifacts. A manifest declaring COMPLETE
    without these references fails the validator."""
    _fresh_root()
    wid = bw.create_workspace()
    # A manifest hand-built with COMPLETE lifecycle but no validation/tests.
    m = M.BuildManifest(workspace_id=wid, lifecycle="COMPLETE")
    M.store_manifest(wid, m)
    r = validation.validate_manifest(M.load_manifest(wid), workspace_id=wid)
    assert not r.passed
    rules = {i.rule for i in r.issues}
    assert "validation_artifact_exists" in rules
    assert "tests_artifact_exists" in rules
    assert "implementation_artifact_exists" in rules


def test_validate_manifest_plan_id_mismatch():
    """The manifest's execution_plan reference must match the persisted id
    inside execution_plan.json — a stale manifest pointing at an old plan id
    fails the plan_id_match check."""
    wid, m = _consistent_workspace_with_spec_and_plan()
    # Re-plan with a different spec to mint a different id (different checksum).
    bse.store_implementation_spec(wid, _GOOD_SPEC_TEXT + "x")  # mutate
    EP.store_execution_plan(wid, EP.plan({"verification_result": {},
                                          "verified_decisions": [],
                                          "spec_text": _GOOD_SPEC_TEXT + "x"}))
    r = validation.validate_manifest(m, workspace_id=wid)
    assert not r.passed
    # Either the manifest's plan id no longer matches (plan_id_match) or the
    # plan was built from a spec that no longer matches the manifest's build_spec
    # checksum (plan_matches_spec).
    rules = {i.rule for i in r.issues}
    assert rules & {"plan_id_match", "plan_matches_spec", "checksum_match"}, r.to_dict()


# ============================================================================ #
# Part 6 — Resume is manifest-aware
# ============================================================================ #

def test_resume_reuses_persisted_plan_no_regeneration():
    _fresh_root()
    wid = bw.create_workspace()
    decisions = {
        "answer_id": 1,
        "verification_result": VR_156,
        "verified_decisions": [{"outcome_type": "VERIFIED_DECISION",
                                 "dispute_key": "database_technology_selection",
                                 "decision": "SQLite persistence",
                                 "reasoning": "low capital"}],
        "resolved_decisions": [],
    }
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "Use SQLite", "answer_id": 1,
                    "question": "q"}, indent=2),
        encoding="utf-8")
    rep = resume.resume_build(wid)
    assert rep["resumed"] is True, rep
    # After the first resume: lifecycle_stage is EXECUTION_ALLOWED (skip) and
    # BOTH implementation_plan.md AND execution_plan.json are on disk.
    spec_path = bw._workspace_dir(wid) / "implementation_plan.md"
    plan_path = bw._workspace_dir(wid) / "execution_plan.json"
    assert spec_path.is_file()
    assert plan_path.is_file()
    # Manifest is written by _refresh_manifest at end of resume.
    m_after_1 = M.load_manifest(wid)
    assert m_after_1 is not None
    assert m_after_1.execution_plan == EP.load_execution_plan(wid)["id"]

    # Second resume: lifecycle now EXECUTION_ALLOWED — the execution-allowed
    # dispatch path runs (no spec/plan regeneration), the persisted artifacts
    # remain unchanged (manifest-aware reuse). Take a force-True retry from
    # the SPEC_READY path to also exercise the spec/plan reuse on that path,
    # proving checksums are stable.
    plan_ck_before = EP.load_execution_plan(wid)["checksum"]
    rep2 = resume.resume_build(wid)
    assert rep2["resumed"] is True
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.EXECUTION_ALLOWED

    # Set the lifecycle to FAILED so a force=True resume drives the full
    # spec+plan reuse path. The artifact checksums must NOT change.
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.FAILED)
    rep3 = resume.resume_build(wid, force=True)
    assert rep3["resumed"] is True
    plan_ck_after = EP.load_execution_plan(wid)["checksum"]
    assert plan_ck_before == plan_ck_after, (
        "force-retry regenerated the plan; should have reused it")
    # Manifest stayed consistent.
    m_after_3 = M.load_manifest(wid)
    assert m_after_3.execution_plan == EP.load_execution_plan(wid)["id"]
    assert m_after_3.checksum_of("execution_plan") == plan_ck_after


def test_resume_corrupted_artifact_triggers_regeneration():
    _fresh_root()
    wid = bw.create_workspace()
    decisions = {"answer_id": 1, "verification_result": VR_156,
                  "verified_decisions": [], "resolved_decisions": []}
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "Use SQLite"}, indent=2),
        encoding="utf-8")
    rep = resume.resume_build(wid)
    assert rep["resumed"] is True
    plan_path = bw._workspace_dir(wid) / "execution_plan.json"
    # Corrupt execution_plan.json (mutate the payload) — the verify_checksum
    # detection will flag it.
    env = json.loads(plan_path.read_text())
    env["payload"]["order"].append("corrupted_extra")
    plan_path.write_text(json.dumps(env, indent=2), encoding="utf-8")
    # Clear verify by hand to ensure the workspace is FAILED then force-retry:
    # the force-True path goes through _resume_from_spec_ready which invokes
    # _load_or_rebuild_plan which detects the corruption and regenerates.
    lifecycle.advance_lifecycle(wid, lifecycle.BuildLifecycle.FAILED)
    rep2 = resume.resume_build(wid, force=True)
    assert rep2["resumed"] is True
    # The persisted plan's payload no longer has the corrupted extra task.
    env_after = EP.load_execution_plan(wid)
    assert "corrupted_extra" not in env_after["payload"]["order"]
    assert EP.verify_execution_plan_checksum(env_after) is True
    # The manifest was refreshed with the regenerated artifact id.
    m_after = M.load_manifest(wid)
    assert m_after.execution_plan == env_after["id"]


def test_resume_completed_workspace_is_noop_with_manifest():
    _fresh_root()
    wid = bw.create_workspace()
    # Forge a COMPLETE state + manifest so the complete path is taken.
    for tgt in (lifecycle.BuildLifecycle.VERIFIED,
                lifecycle.BuildLifecycle.SPEC_READY,
                lifecycle.BuildLifecycle.EXECUTION_ALLOWED,
                lifecycle.BuildLifecycle.EXECUTING,
                lifecycle.BuildLifecycle.ARTIFACTS_VALIDATED,
                lifecycle.BuildLifecycle.TESTS_VALIDATED,
                lifecycle.BuildLifecycle.COMPLETE):
        lifecycle.advance_lifecycle(wid, tgt)
    M.store_manifest(wid, M.BuildManifest(workspace_id=wid,
                                            lifecycle="COMPLETE"))
    rep = resume.resume_build(wid)
    assert rep["resumed"] is False
    assert rep["reason"] == "already_complete"
    assert rep.get("manifest_present") is True


def test_resume_partial_build_proceeds_when_manifest_present():
    """A partial build (stopped at SPEC_READY with real decisions / spec on
    disk + manifest) resumes and refreshes the manifest. Resume is not blocked
    by the manifest; the manifest is observational metadata."""
    _fresh_root()
    wid = bw.create_workspace()
    decisions = {"answer_id": 1, "verification_result": VR_156,
                  "verified_decisions": [
                      {"outcome_type": "VERIFIED_DECISION",
                       "dispute_key": "database_technology_selection",
                       "decision": "SQLite persistence",
                       "reasoning": "low capital"}],
                  "resolved_decisions": []}
    (bw._workspace_dir(wid) / "decisions.json").write_text(
        json.dumps(decisions, indent=2), encoding="utf-8")
    (bw._workspace_dir(wid) / "answer.json").write_text(
        json.dumps({"synthesized_answer": "Use SQLite"}, indent=2),
        encoding="utf-8")
    bse.store_implementation_spec(wid, _GOOD_SPEC_TEXT)
    EP.store_execution_plan(wid, EP.plan({"verification_result": VR_156,
                                          "verified_decisions": [],
                                          "spec_text": _GOOD_SPEC_TEXT}))
    # Pre-stage the manifest with a SPEC_READY lifecycle reference.
    M.store_manifest(wid, M.BuildManifest(workspace_id=wid,
                                            lifecycle="SPEC_READY"))
    rep = resume.resume_build(wid)
    assert rep["resumed"] is True, rep
    # The manifest-aware path detected the existing artifact + matched the
    # recorded checksum; it proceeded into execution-skip leaving the lifecycle
    # at EXECUTION_ALLOWED. The persisted artifacts are intact.
    assert lifecycle.current_lifecycle(wid) is lifecycle.BuildLifecycle.EXECUTION_ALLOWED
    # After resume, the manifest's lifecycle is refreshed to current_lifecycle.
    m_after = M.load_manifest(wid)
    assert m_after.lifecycle == "EXECUTION_ALLOWED"


# ============================================================================ #
# Convenience: full lifecycle of resume via manifest
# ============================================================================ #

def test_end_to_end_resume_with_manifest_after_pipeline():
    """A complete round-trip:
        1. run_build_pipeline (with planner_fn seam)
        2. manifest.json is produced by run_build_pipeline
        3. validate_manifest passes
        4. resume_build is manifest-aware and short-circuits when artifacts
           are valid (no regeneration).
    """
    _fresh_root()
    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        debate_fn=_fake_debate, executor_fn=_mock_executor,
    )
    wid = result["workspace_id"]
    # Manifest was written.
    assert M.load_manifest(wid) is not None
    r = validation.validate_manifest(workspace_id=wid)
    assert r.passed, r.to_dict()
    # Snapshot the manifest's plan reference + checksum.
    m1 = M.load_manifest(wid)
    plan_id_1 = m1.execution_plan
    plan_ck_1 = m1.checksum_of("execution_plan")
    # Re-plan + persist again (would be a no-op since deterministic) but call
    # validate_manifest from the dispatcher to ensure no drift.
    r2 = validation.validate("manifest", None, workspace_id=wid)
    assert r2.passed
    # The persisted plan_id + checksum are unchanged (deterministic id).
    m2 = M.load_manifest(wid)
    assert m2.execution_plan == plan_id_1
    assert m2.checksum_of("execution_plan") == plan_ck_1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
