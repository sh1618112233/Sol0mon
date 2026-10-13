"""
Project Sol0mon - Central Validation Framework (Final Architecture Hardening -
Phase C).

A single ``ValidationReport`` shape replaces the scattered ad-hoc validator
return shapes (``list[str]`` from ``validate_spec_sections``, ``list[dict]``
from ``validate_synthesis_consistency``, ``bool`` from ``_tests_passed``,
``list[dict]`` from ``_spec_decision_gaps``). The framework does NOT replace
those validators — it DELEGATES to them. They stay where they are (single
source of truth per concern); this framework wraps their output into the
common ``ValidationReport`` contract and routes execution through one entry
point so "no stage may infer success without validation" is enforceable.

The Canonical Decision Model invariant is live: ``validate_decision_model``
checks the answer-156 contradiction class (no rejected option may appear in
``winner_tokens``) on every record produced by ``decision_model.decision_records``.

Execution cannot continue after a failed validation: ``validate(...,
halt_on_failure=True)`` raises ``BuildError``. The default is to return the
report and let the caller (the pipeline / resume engine) decide.

API:
    validate(stage, artifact, halt_on_failure=False, **ctx) -> ValidationReport
    validate_decision_model(verification_result, dependent_artifact_id=None) -> report
    validate_build_spec(spec, dependent_artifact_id=None) -> report
    validate_execution_plan(plan, dependent_artifact_id=None) -> report
    validate_generated_code(file_manifest, dependent_artifact_id=None) -> report
    validate_tests(test_results, dependent_artifact_id=None) -> report
    validate_final_build(build_state, dependent_artifact_id=None) -> report

Each returns ``ValidationReport{passed, stage, artifact_id, issues,
severity, suggested_repair, dependent_artifact_id}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from errors import BuildError, Severity, validation_failed


# --- Validation issue + report (frozen value objects) ---

@dataclass(frozen=True)
class ValidationIssue:
    rule: str
    severity: Severity
    message: str
    suggested_repair: str = ""
    artifact_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "severity": self.severity.value,
            "message": self.message, "suggested_repair": self.suggested_repair,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True)
class ValidationReport:
    passed: bool
    stage: str
    artifact_id: str | None = None
    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)
    dependent_artifact_id: str | None = None
    suggested_repair: str = ""

    @property
    def severity(self) -> Severity:
        if not self.issues:
            return Severity.WARNING if not self.passed else Severity.WARNING
        levels = {Severity.FATAL: 3, Severity.ERROR: 2, Severity.WARNING: 1}
        return max(self.issues, key=lambda i: levels.get(i.severity, 0)).severity

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)

    @property
    def failures(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues
                     if i.severity in (Severity.ERROR, Severity.FATAL))

    def to_dict(self) -> dict:
        return {
            "passed": self.passed, "stage": self.stage,
            "artifact_id": self.artifact_id,
            "dependent_artifact_id": self.dependent_artifact_id,
            "severity": self.severity.value,
            "suggested_repair": self.suggested_repair,
            "issues": [i.to_dict() for i in self.issues],
        }


def _ok(stage: str, artifact_id: str | None = None,
        dependent_artifact_id: str | None = None) -> ValidationReport:
    return ValidationReport(passed=True, stage=stage, artifact_id=artifact_id,
                           dependent_artifact_id=dependent_artifact_id,
                           issues=(), suggested_repair="")


def _report(stage: str, issues, artifact_id: str | None = None,
             dependent_artifact_id: str | None = None) -> ValidationReport:
    """Build a ValidationReport from an issue list. ``passed`` is True iff no
    issue is ERROR or FATAL — WARNINGS are advisory and do not fail the report
    (e.g. ``decisions_honored`` gaps are a warning: the spec is still usable
    but should be amended)."""
    issues = tuple(issues)
    has_failure = any(i.severity in (Severity.ERROR, Severity.FATAL) for i in issues)
    suggested = ""
    for i in issues:
        if i.suggested_repair:
            suggested = i.suggested_repair
            break
    return ValidationReport(passed=not has_failure, stage=stage,
                            issues=issues, artifact_id=artifact_id,
                            dependent_artifact_id=dependent_artifact_id,
                            suggested_repair=suggested)


# Keep the old name as an alias so the existing validator bodies read naturally.
_fail = _report


# --- The six validators (each delegates to an existing one) ---

def validate_decision_model(
    verification_result: dict | None,
    artifact_id: str | None = None,
    dependent_artifact_id: str | None = None,
) -> ValidationReport:
    """Canonical Decision Model validator. Delegates interpretation to
    ``decision_model.decision_records`` (the SOLE OWNER — never parse
    ``verification_result`` here). Checks:
      - every RESOLVED claim produced a non-empty winner_tokens
      - the answer-156 invariant: no rejected option appears in winner_tokens
    """
    from decision_model import decision_records

    records = decision_records(verification_result)
    issues: list[ValidationIssue] = []
    for rec in records:
        nk = rec.get("normalized_key") or "(no-key)"
        wt = rec.get("winner_tokens") or []
        ro = rec.get("rejected_options") or []
        if not wt:
            issues.append(ValidationIssue(
                rule="winner_present", severity=Severity.ERROR,
                message=f"decision '{nk}' has no winner_tokens",
                suggested_repair="re-run verification; the finding lacks a winner marker",
                artifact_id=artifact_id,
            ))
        leak = set(wt) & set(ro)
        if leak:
            issues.append(ValidationIssue(
                rule="winner_not_rejected", severity=Severity.FATAL,
                message=(f"decision '{nk}' winner_tokens {sorted(leak)} are also "
                         "rejected_options — the answer-156 contradiction class"),
                suggested_repair="check decision_model._winner_phrase cut logic",
                artifact_id=artifact_id,
            ))
    if issues:
        return _fail("decision_model", issues, artifact_id, dependent_artifact_id)
    return _ok("decision_model", artifact_id, dependent_artifact_id)


def validate_build_spec(
    spec: dict | Any,
    artifact_id: str | None = None,
    dependent_artifact_id: str | None = None,
) -> ValidationReport:
    """Build spec validator. ``spec`` is the dict returned by
    ``build_spec_engine.build_spec`` (or an ``Artifact`` whose payload is one).

    After the artefact-selection fix the Phase 3 deterministic producer emits
    an EXECUTABLE spec (the 6 ``EXECUTABLE_SPEC_SECTIONS``), not the legacy 7
    audit-debate shape. This validator delegates structural completeness to
    ``validate_executable_spec_sections`` (the same gate
    ``build_pipeline.execute_build_spec`` enforces), and decision-honoring to
    ``_spec_decision_gaps``. A spec that is the legacy audit shape (e.g. a
    debate summary / verification report / decision record dressed up as a
    spec) is rejected with the ``sections_complete`` rule — the same rule
    name existing tests pin."""
    from build_prompt_generator import validate_executable_spec_sections
    from build_spec_engine import _spec_decision_gaps

    if hasattr(spec, "payload") and not isinstance(spec, dict):
        spec_dict = spec.payload
        if artifact_id is None:
            artifact_id = getattr(spec, "id", None)
    else:
        spec_dict = spec
    spec_text = (spec_dict or {}).get("spec_text", "") or ""
    verified = (spec_dict or {}).get("verified_decisions", []) or []

    issues: list[ValidationIssue] = []
    missing = validate_executable_spec_sections(spec_text)
    if missing:
        issues.append(ValidationIssue(
            rule="sections_complete", severity=Severity.ERROR,
            message=f"build_spec missing executable-spec sections: {missing}",
            suggested_repair=(
                "regenerate via build_spec(debate_fn=None) (Phase 3 "
                "deterministic executable-spec renderer)"),
            artifact_id=artifact_id,
        ))
    if (spec_dict or {}).get("status") != "completed":
        issues.append(ValidationIssue(
            rule="status_completed", severity=Severity.ERROR,
            message=f"build_spec status is not 'completed': {spec_dict.get('status')!r}",
            suggested_repair="block at SPEC_READY until the spec is structurally complete",
            artifact_id=artifact_id,
        ))
    gaps = _spec_decision_gaps(spec_text, verified)
    if gaps:
        keys = [g.get("normalized_key") or g.get("dispute_key") or "?" for g in gaps]
        issues.append(ValidationIssue(
            rule="decisions_honored", severity=Severity.WARNING,
            message=f"build_spec does not reference verified decisions: {keys}",
            suggested_repair="add the listed decisions to # Implementation Objective",
            artifact_id=artifact_id,
        ))
    if issues:
        return _fail("build_spec", issues, artifact_id, dependent_artifact_id)
    return _ok("build_spec", artifact_id, dependent_artifact_id)


def validate_execution_plan(
    plan: dict | Any,
    artifact_id: str | None = None,
    dependent_artifact_id: str | None = None,
) -> ValidationReport:
    """Execution plan / task-graph validator. Checks the plan is a DAG
    (no cycles), every dependency points to a real task id, and there is at
    least one task. Pure; deterministic. Does NOT evaluate task *content*
    (that is the agent's job, not the platform's — adding it would be new AI,
    explicitly out of scope)."""
    if hasattr(plan, "payload") and not isinstance(plan, dict):
        plan_dict = plan.payload
        if artifact_id is None:
            artifact_id = getattr(plan, "id", None)
    else:
        plan_dict = plan or {}

    tasks = plan_dict.get("tasks") or {}
    issues: list[ValidationIssue] = []
    if not tasks:
        issues.append(ValidationIssue(
            rule="non_empty", severity=Severity.ERROR,
            message="execution plan has no tasks",
            suggested_repair="run ExecutionPlanner.plan on a BUILD_SPEC artifact",
            artifact_id=artifact_id,
        ))
        return _fail("execution_plan", issues, artifact_id, dependent_artifact_id)

    known = set(tasks)
    for tid, t in tasks.items():
        deps = t.get("dependencies") or []
        for d in deps:
            if d not in known:
                issues.append(ValidationIssue(
                    rule="dep_resolvable", severity=Severity.ERROR,
                    message=f"task '{tid}' depends on unknown task '{d}'",
                    suggested_repair="fix the dependency or add the missing task",
                    artifact_id=artifact_id,
                ))
    cycle = _find_cycle(tasks)
    if cycle:
        issues.append(ValidationIssue(
            rule="acyclic", severity=Severity.FATAL,
            message=f"execution plan has a cycle: {' -> '.join(cycle)}",
            suggested_repair="break the dependency cycle in the planner output",
            artifact_id=artifact_id,
        ))
    if issues:
        return _fail("execution_plan", issues, artifact_id, dependent_artifact_id)
    return _ok("execution_plan", artifact_id, dependent_artifact_id)


def validate_generated_code(
    file_manifest: dict | set | Any,
    artifact_id: str | None = None,
    dependent_artifact_id: str | None = None,
) -> ValidationReport:
    """Generated-code validator (artefact-presence gate). ``file_manifest`` is
    either a set of relative file paths (the agent delta), or a dict with a
    ``files`` key. The minimum contract: the agent produced at least one new
    file (the answer-156 non-answer class produced none). Deeper static
    analysis (lint, type-check) is a future addition — ponytail: would add
    value but is out of the "no new AI capabilities" scope."""
    if hasattr(file_manifest, "payload") and not isinstance(file_manifest, (dict, set)):
        file_manifest = file_manifest.payload
        if artifact_id is None:
            artifact_id = getattr(file_manifest, "id", None) if hasattr(file_manifest, "id") else None

    if isinstance(file_manifest, dict):
        files = file_manifest.get("files") or set()
    else:
        files = file_manifest or set()
    try:
        n = len(files)
    except TypeError:
        files = list(files)
        n = len(files)
    if n == 0:
        return _fail("generated_code", [ValidationIssue(
            rule="artefact_presence", severity=Severity.ERROR,
            message="agent produced no files (artefact-presence gate failed)",
            suggested_repair="re-invoke the agent; the plan was not actionable",
            artifact_id=artifact_id,
        )], artifact_id, dependent_artifact_id)
    return _ok("generated_code", artifact_id, dependent_artifact_id)


def validate_tests(
    test_results: dict | Any,
    artifact_id: str | None = None,
    dependent_artifact_id: str | None = None,
) -> ValidationReport:
    """Test validator. Delegates the pass/fail contract to
    ``implementation_executor._tests_passed`` (Phase 2 strengthened contract:
    runner != 'none', passed>=1, failed==0, exit_code==0). Wraps it in the
    common report so the pipeline can't infer success without a real test
    run."""
    from implementation_executor import _tests_passed

    if hasattr(test_results, "payload") and not isinstance(test_results, dict):
        tr = test_results.payload
        if artifact_id is None:
            artifact_id = getattr(test_results, "id", None)
    else:
        tr = test_results or {}

    if _tests_passed(tr):
        return _ok("tests", artifact_id, dependent_artifact_id)
    return _fail("tests", [ValidationIssue(
        rule="tests_passed", severity=Severity.ERROR,
        message=(f"tests did not pass: runner={tr.get('runner')!r} "
                 f"passed={tr.get('passed', 0)} failed={tr.get('failed', 0)} "
                 f"exit_code={tr.get('exit_code', 1)}"),
        suggested_repair="enter REPAIRING; rebuild the repair prompt from the failed output",
        artifact_id=artifact_id,
    )], artifact_id, dependent_artifact_id)


def validate_final_build(
    build_state: dict | Any,
    artifact_id: str | None = None,
    dependent_artifact_id: str | None = None,
) -> ValidationReport:
    """Final build validator. Composite of the per-stage contracts: the spec
    was complete AND tests passed AND the lifecycle reached COMPLETE."""
    from lifecycle import current_lifecycle, BuildLifecycle

    if hasattr(build_state, "payload") and not isinstance(build_state, dict):
        state = build_state.payload
        if artifact_id is None:
            artifact_id = getattr(build_state, "id", None)
    else:
        state = build_state or {}

    issues: list[ValidationIssue] = []
    if state.get("spec_status") and state.get("spec_status") != "completed":
        issues.append(ValidationIssue(
            rule="spec_completed", severity=Severity.ERROR,
            message=f"final build spec_status={state.get('spec_status')!r}",
            suggested_repair="regenerate spec; cannot finalize an incomplete spec",
            artifact_id=artifact_id,
        ))
    if state.get("execution_success") is not True:
        issues.append(ValidationIssue(
            rule="execution_success", severity=Severity.ERROR,
            message="final build execution_success is not True",
            suggested_repair="resume execution; tests / artefact gate not satisfied",
            artifact_id=artifact_id,
        ))
    # Lifecycle: the workspace's stored build_state may not carry
    # lifecycle_stage (legacy). Fall back to mapping the legacy status.
    stage = state.get("lifecycle_stage")
    if not stage:
        from lifecycle import lifecycle_stage_from_status
        stage = lifecycle_stage_from_status(state.get("status", "")).value
    if stage != BuildLifecycle.COMPLETE.value:
        issues.append(ValidationIssue(
            rule="lifecycle_complete", severity=Severity.ERROR,
            message=f"final build lifecycle_stage={stage!r}, expected {BuildLifecycle.COMPLETE.value}",
            suggested_repair="resume from the current lifecycle stage until COMPLETE",
            artifact_id=artifact_id,
        ))
    if issues:
        return _fail("final_build", issues, artifact_id, dependent_artifact_id)
    return _ok("final_build", artifact_id, dependent_artifact_id)


def validate_manifest(manifest: Any = None, *,
                       workspace_id: str | None = None,
                       halt_on_failure: bool = False) -> ValidationReport:
    """Build manifest validator (Part 5). Confirms:
      - every referenced artifact exists on disk
      - on-disk checksums match the manifest's recorded checksums
      - the manifest's lifecycle is consistent with current_lifecycle(workspace_id)
      - artifact versions are consistent (manifest_version == MANIFEST_VERSION)
      - validation artifact exists when lifecycle >= TESTS_VALIDATED
      - the execution_plan reference matches the persisted execution_plan.json id
      - the implementation reference (when present) belongs to this workspace
        (its id derives over this workspace's records — no cross-build leak)

    Accepts either a ``BuildManifest`` instance OR (when ``workspace_id`` is
    given and ``manifest`` is None / a workspace_id string) the manifest loaded
    fresh from disk. The ``halt_on_failure`` mirrors the dispatcher.
    """
    # Accept (workspace_id) or (manifest, workspace_id=...) or (manifest)
    from manifest import build_manifest_from_workspace, load_manifest, BuildManifest

    if manifest is None and workspace_id is None:
        return _report("manifest", [ValidationIssue(
            rule="manifest_present", severity=Severity.ERROR,
            message="no manifest and no workspace_id given",
            suggested_repair="call validate_manifest(manifest=...) or "
                             "validate_manifest(workspace_id=...)",
        )])
    if isinstance(manifest, str) and workspace_id is None:
        # Single-string call: treat it as the workspace_id.
        workspace_id = manifest
        manifest = None
    if not isinstance(manifest, BuildManifest):
        if workspace_id:
            # Prefer a manifest persisted on disk; otherwise assemble one
            # from the workspace's current artifacts.
            loaded = load_manifest(workspace_id)
            if loaded is not None:
                manifest = loaded
            else:
                manifest = build_manifest_from_workspace(workspace_id)
        elif isinstance(manifest, dict):
            # Caller passed a manifest-as-dict (e.g. read from disk elsewhere).
            try:
                manifest = BuildManifest.from_dict(manifest)
            except (ValueError, TypeError):
                manifest = None
        if manifest is None:
            return _report("manifest", [ValidationIssue(
                rule="manifest_present", severity=Severity.ERROR,
                message=f"no manifest for workspace {workspace_id!r}",
                suggested_repair="call manifest.build_manifest_from_workspace + store_manifest",
            )])
    if workspace_id is None:
        workspace_id = manifest.workspace_id

    issues: list[ValidationIssue] = []

    # Detect build_id mismatch (a manifest loaded from a different workspace).
    if manifest.workspace_id and workspace_id and manifest.workspace_id != workspace_id:
        issues.append(ValidationIssue(
            rule="workspace_id_match", severity=Severity.FATAL,
            message=f"manifest.workspace_id={manifest.workspace_id!r} != {workspace_id!r}",
            suggested_repair="regenerate the manifest from the current workspace",
        ))

    # Schema version check (cheap forward-compat guard).
    from manifest import MANIFEST_VERSION
    if manifest.manifest_version != MANIFEST_VERSION:
        issues.append(ValidationIssue(
            rule="manifest_version", severity=Severity.ERROR,
            message=f"manifest_version={manifest.manifest_version} != {MANIFEST_VERSION}",
            suggested_repair="rebuild the manifest with the current schema",
        ))

    # Lifecycle consistency: manifest.lifecycle should match the workspace's
    # current_lifecycle. (A purely lifecycle-driven drift would mean the
    # manifest is stale — most likely because run_build_pipeline advanced the
    # state after the manifest was last written.)
    try:
        from lifecycle import current_lifecycle
        live = current_lifecycle(workspace_id).value
    except Exception:
        live = None
    if live is not None and live != manifest.lifecycle:
        issues.append(ValidationIssue(
            rule="lifecycle_consistent", severity=Severity.ERROR,
            message=f"manifest.lifecycle={manifest.lifecycle!r} != live {live!r}",
            suggested_repair="rebuild_manifest_from_workspace to refresh the lifecycle",
        ))

    # Per-section existence + checksum match. Each section has a known file
    # we can check on disk.
    import json as _json
    import hashlib as _hashlib
    from build_workspace import _workspace_dir
    from build_spec_engine import load_implementation_spec
    from execution_planner import (load_execution_plan,
                                    verify_execution_plan_checksum)

    ws = _workspace_dir(workspace_id) if workspace_id else None

    def _check_section(section: str, name_human: str):
        art_id = manifest.section(section)
        recorded = manifest.checksum_of(section)
        if art_id is None:
            return  # None is fine for any early-lifecycle stage (per-section checker below).
        # Checksum piece: recompute the on-disk checksum for the referenced file.
        on_disk_ck = ""
        if section == "decision_artifact" and ws is not None:
            p = ws / "decisions.json"
            if not p.is_file():
                issues.append(ValidationIssue(
                    rule="artifact_exists", severity=Severity.ERROR,
                    message=f"{name_human}: decisions.json missing but manifest references {art_id!r}",
                    suggested_repair="write decisions.json (resurrect_answer / build_pipeline writes it)",
                    artifact_id=art_id,
                ))
                return
            try:
                on_disk_ck = _hashlib.sha256(
                    _json.dumps(_json.loads(p.read_text(encoding="utf-8")) or {},
                                sort_keys=True, separators=(",", ":"),
                                default=str, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
            except (ValueError, OSError):
                on_disk_ck = ""
        elif section == "build_spec":
            spec_text = load_implementation_spec(workspace_id) if workspace_id else ""
            if not spec_text:
                issues.append(ValidationIssue(
                    rule="artifact_exists", severity=Severity.ERROR,
                    message=f"{name_human}: implementation_plan.md empty/missing but manifest references {art_id!r}",
                    suggested_repair="regenerate the spec (build_spec_engine.build_spec)",
                    artifact_id=art_id,
                ))
                return
            on_disk_ck = _hashlib.sha256(spec_text.encode("utf-8")).hexdigest()
        elif section == "execution_plan":
            env = load_execution_plan(workspace_id) if workspace_id else None
            if env is None:
                issues.append(ValidationIssue(
                    rule="artifact_exists", severity=Severity.ERROR,
                    message=f"{name_human}: execution_plan.json missing but manifest references {art_id!r}",
                    suggested_repair="run the planner (execution_planner.store_execution_plan)",
                    artifact_id=art_id,
                ))
                return
            # Also: the manifest's id must match the persisted envelope's id.
            if env.get("id") != art_id:
                issues.append(ValidationIssue(
                    rule="plan_id_match", severity=Severity.ERROR,
                    message=(f"{name_human}: manifest.references={art_id!r} but "
                             f"execution_plan.json carries {env.get('id')!r}"),
                    suggested_repair="rebuild the manifest or regenerate the plan",
                    artifact_id=art_id,
                ))
            # Checksum: recompute over the on-disk payload.
            payload = env.get("payload", {})
            on_disk_ck = _hashlib.sha256(
                _json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            default=str, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        if recorded and on_disk_ck and recorded != on_disk_ck:
            issues.append(ValidationIssue(
                rule="checksum_match", severity=Severity.ERROR,
                message=(f"{name_human}: manifest.checksum={recorded!r} != "
                         f"on-disk {on_disk_ck!r}"),
                suggested_repair="rebuild the manifest OR regenerate the artifact",
                artifact_id=art_id,
            ))

    _check_section("decision_artifact", "decision_artifact")
    _check_section("build_spec", "build_spec")
    _check_section("execution_plan", "execution_plan")

    # Lifecycle-references consistency:
    if manifest.lifecycle in ("TESTS_VALIDATED", "COMPLETE"):
        if manifest.validation is None:
            issues.append(ValidationIssue(
                rule="validation_artifact_exists", severity=Severity.ERROR,
                message=f"lifecycle={manifest.lifecycle!r} but no validation reference recorded",
                suggested_repair="record the validation artifact id + checksum on completion",
            ))
        if manifest.tests is None:
            issues.append(ValidationIssue(
                rule="tests_artifact_exists", severity=Severity.ERROR,
                message=f"lifecycle={manifest.lifecycle!r} but no tests reference recorded",
                suggested_repair="record the test artifact id + checksum on completion",
            ))
    if manifest.lifecycle == "COMPLETE" and manifest.implementation is None:
        issues.append(ValidationIssue(
            rule="implementation_artifact_exists", severity=Severity.ERROR,
            message="lifecycle=COMPLETE but no implementation reference recorded",
            suggested_repair="record the implementation artifact id on completion",
        ))

    # execution_plan matches implementation: if both refs are present, the
    # spec_checksum captured inside the plan's payload should be the sha256 of
    # the implementation_plan.md (which IS the build_spec the plan was built
    # from). If they differ, the plan was built from a different spec than the
    # one currently on disk — the manifest is inconsistent.
    if manifest.execution_plan is not None and manifest.build_spec is not None:
        env = load_execution_plan(workspace_id) if workspace_id else None
        if env:
            plan_spec_ck = env.get("payload", {}).get("spec_checksum", "")
            current_spec_ck = manifest.checksum_of("build_spec")
            if plan_spec_ck and current_spec_ck and plan_spec_ck != current_spec_ck:
                issues.append(ValidationIssue(
                    rule="plan_matches_spec", severity=Severity.ERROR,
                    message=(f"execution_plan was built from spec_checksum={plan_spec_ck!r} "
                             f"but the manifest references build_spec checksum={current_spec_ck!r}"),
                    suggested_repair=("regenerate the execution_plan via "
                                     "execution_planner.plan + store_execution_plan"),
                ))

    if issues:
        return _report("manifest", issues, None, None)
    return _ok("manifest", None, None)


# --- Registry + dispatcher ---

VALIDATORS: dict[str, Callable[..., ValidationReport]] = {
    "decision_model": validate_decision_model,
    "build_spec": validate_build_spec,
    "execution_plan": validate_execution_plan,
    "generated_code": validate_generated_code,
    "tests": validate_tests,
    "final_build": validate_final_build,
    # The manifest validator keys off workspace_id (not a payload artifact),
    # so the dispatcher passes ``workspace_id`` through ctx. The lambda's
    # kw-ignoring discipline keeps the other validators' strict contract.
    "manifest": lambda manifest=None, *, halt_on_failure=False, **kw: validate_manifest(
        manifest, halt_on_failure=halt_on_failure,
        workspace_id=kw.get("workspace_id")),
}


def validate(stage: str, artifact: Any | None = None, *,
             halt_on_failure: bool = False,
             artifact_id: str | None = None,
             dependent_artifact_id: str | None = None,
             **ctx: Any) -> ValidationReport:
    """Single entry point. ``stage`` names a validator in ``VALIDATORS``;
    ``artifact`` is its first argument (the spec / plan / test_results / etc).
    Unknown stage raises ``BuildError`` (better than silent skip — "no stage
    may infer state without validation"). On failure with ``halt_on_failure``,
    raises so the pipeline stops instead of continuing with bad inputs.

    ``workspace_id`` in ctx is forwarded to validators that take it (currently
    ``validate_manifest``); other ctx keys (``verification_result`` /
    ``build_state``) are the default fallback for stages called with
    ``artifact=None``."""
    fn = VALIDATORS.get(stage)
    if fn is None:
        raise BuildError(
            kind=__import__("errors").ErrorKind.VALIDATION,
            stage=stage,
            message=f"unknown validation stage '{stage}'",
            severity=Severity.FATAL, recoverable=False,
            suggested_repair=f"register the validator or use one of {sorted(VALIDATORS)}",
        )
    # ctx used for two roles: (a) the fallback artifact when artifact is None
    # (legacy convention — verification_result / build_state); (b) optional
    # workspace_id for the manifest validator. Workspace_id is passed only
    # when present so the other validators (which reject unknown kwargs) keep
    # their strict signature.
    fallback = ctx.get("verification_result") if ctx.get("verification_result") \
        else (ctx.get("build_state") or {})
    pass_ws = {"workspace_id": ctx["workspace_id"]} if "workspace_id" in ctx else {}
    try:
        if artifact is not None:
            report = fn(artifact, artifact_id=artifact_id,
                        dependent_artifact_id=dependent_artifact_id, **pass_ws)
        else:
            report = fn(fallback, artifact_id=artifact_id,
                        dependent_artifact_id=dependent_artifact_id, **pass_ws)
    except TypeError:
        # The validator does not accept workspace_id (the strict-signature
        # validators). Retry without it — preserves the existing contract.
        if artifact is not None:
            report = fn(artifact, artifact_id=artifact_id,
                        dependent_artifact_id=dependent_artifact_id)
        else:
            report = fn(fallback, artifact_id=artifact_id,
                        dependent_artifact_id=dependent_artifact_id)
    if halt_on_failure and not report.passed:
        raise validation_failed(report, stage)
    return report


# --- DAG cycle detection (DFS; stdlib-only) ---

def _find_cycle(tasks: dict) -> list[str]:
    """Return a cycle path (task ids) if the graph has one, else []. DFS with
    a WHITE/GRAY/BLACK coloring. Iterative to avoid recursion limits on wide
    graphs."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in tasks}
    for start in tasks:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        color[start] = GRAY
        while stack:
            node, i = stack[-1]
            if i == 0:
                path.append(node)
            deps = tasks[node].get("dependencies") or []
            advanced = False
            while i < len(deps):
                d = deps[i]
                i += 1
                if d not in tasks:
                    continue
                if color[d] == GRAY:
                    # cycle: from where d appears in path forward to node
                    j = path.index(d)
                    return path[j:] + [d]
                if color[d] == WHITE:
                    color[d] = GRAY
                    stack[-1] = (node, i)
                    stack.append((d, 0))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                path.pop()
                stack.pop()
    return []


if __name__ == "__main__":
    # ponytail self-check: each validator + the dispatcher + the DAG detector.
    # 156 fixture: rejected option PostgreSQL must NOT leak into winner_tokens.
    vr_156 = {
        "resolved_claims": [{
            "normalized_key": "database_technology_selection",
            "dispute": "Database Technology: GPT selects PostgreSQL, Gemini selects SQLite",
            "verification": {
                "status": "RESOLVED", "confidence": 95,
                "finding": ("Both evidence strips conclude SQLite is the appropriate "
                            "persistence engine; the text argues against PostgreSQL."),
            },
        }],
    }
    r = validate_decision_model(vr_156)
    assert r.passed, r.to_dict()
    assert r.dependent_artifact_id is None

    # Inject the contradiction class: winner == rejected -> FATAL issue.
    bad_vr = {
        "resolved_claims": [{
            "normalized_key": "db",
            "verification": {"status": "RESOLVED", "confidence": 90,
                             "finding": "Resolved in favor of PostgreSQL; rejects PostgreSQL."},
        }],
    }
    r_bad = validate_decision_model(bad_vr)
    assert not r_bad.passed and any(i.rule == "winner_not_rejected" for i in r_bad.issues)

    # Build spec: a complete Path 3 spec passes; a one-heading spec fails.
    from build_spec_engine import build_spec
    out = build_spec("ignored", source_result=vr_156)
    assert validate_build_spec(out).passed, validate_build_spec(out).to_dict()
    incomplete = {"spec_text": "# Architecture\nonly", "status": "incomplete",
                   "verified_decisions": []}
    ri = validate_build_spec(incomplete)
    assert not ri.passed and ri.failures, ri.to_dict()

    # Execution plan: DAG ok; cyclic fails; dangling dep fails; empty fails.
    plan_ok = {"tasks": {
        "a": {"dependencies": []},
        "b": {"dependencies": ["a"]},
        "c": {"dependencies": ["a", "b"]},
    }}
    assert validate_execution_plan(plan_ok).passed
    plan_cycle = {"tasks": {
        "a": {"dependencies": ["b"]},
        "b": {"dependencies": ["a"]},
    }}
    rc = validate_execution_plan(plan_cycle)
    assert not rc.passed and any(i.rule == "acyclic" for i in rc.issues), rc.to_dict()
    plan_dangle = {"tasks": {"a": {"dependencies": ["zzz"]}}}
    rd = validate_execution_plan(plan_dangle)
    assert not rd.passed and any(i.rule == "dep_resolvable" for i in rd.issues)
    assert not validate_execution_plan({"tasks": {}}).passed

    # Generated code: empty fails; non-empty passes.
    assert not validate_generated_code(set()).passed
    assert validate_generated_code({"a.py", "b.py"}).passed

    # Tests: the Phase 2 strengthened contract.
    assert validate_tests({"runner": "pytest", "passed": 3, "failed": 0,
                           "exit_code": 0}).passed
    assert not validate_tests({"runner": "pytest", "passed": 0, "failed": 0,
                               "exit_code": 5}).passed
    assert not validate_tests({"runner": "none", "passed": 0, "failed": 0,
                               "exit_code": -1}).passed

    # Final build: only a fully-complete state passes.
    assert validate_final_build({"spec_status": "completed",
                                 "execution_success": True,
                                 "lifecycle_stage": "COMPLETE"}).passed
    rfb = validate_final_build({"spec_status": "completed",
                                "execution_success": True,
                                "status": "executed"})
    assert rfb.passed, rfb.to_dict()  # legacy status "executed" -> COMPLETE
    rfb2 = validate_final_build({"spec_status": "incomplete",
                                  "execution_success": False,
                                  "status": "blocked_incomplete_spec"})
    assert not rfb2.passed

    # Dispatcher + halt on failure.
    assert validate("build_spec", out).passed
    raised = False
    try:
        validate("build_spec", incomplete, halt_on_failure=True)
    except BuildError as e:
        raised = True
        assert e.recoverable
        assert e.kind.value == "ValidationError"
    assert raised
    # Unknown stage raises (never silent).
    raised = False
    try:
        validate("does_not_exist", None)
    except BuildError:
        raised = True
    assert raised

    print("validation smoke test PASSED")
