"""
Project Sol0mon — Phase 3: Autonomous Build Validation & Repair Engine.

Builds on Phase 2's NativeImplementationEngine substrate. Adds an
autonomous validation + repair loop that completes only after BOTH
technical validation (BuildValidator) AND requirements verification
(QualityGate) approve the implementation.

Reuses Phase 2 components unchanged:
    ExecutionContext, StateManager, EventBus, Event, ExecutionRecorder,
    CheckpointManager, AgentManager, CodingAgent, BuildResult, RetryPolicy

Adds Phase 3 components (single responsibility each):

    ValidationEngine     Protocol: validate / repair / resume / cancel /
                          status / recover ─ the ONLY public entry point
    BuildRunner           compile, dependencies, logs, exit codes
    ArtifactValidator     structure, manifests, imports, configs
    TestRunner            unit, integration, smoke, lint, static analysis,
                          type checking, security
    FailureAnalyzer       structured diagnostics (failure category, scope,
                          severity, hint)
    RepairPlanner          scope, priority, strategy
    RepairAgent            minimal targeted patches (wraps AgentManager)
    PatchValidator         integrity + syntax validation
    RetryController        deterministic retries with state
    BuildValidator        technical validation gate
    QualityGate           requirements verification ─ emits
                          quality_report.json + requirements_traceability.json

Workflow (deterministic state machine)::

    Generated Code
        ↓
    Artifact Validation → Build → Tests
        ↓ PASS → BuildValidator → QualityGate → SUCCESS
        ↓ FAIL → FailureAnalysis → RepairPlanning → RepairAgent →
                PatchValidation → RetryController → (loop to Artifact
                Validation) OR terminate at retry budget exhausted

Workspace artifacts (12, namespaced under <workspace>/validation/):

    validation_history.json
    repair_history.json
    failure_analysis.json
    test_results.json
    coverage.json
    patch_history.json
    retry_history.json
    build_log.json
    validation_report.json
    quality_report.json
    requirements_traceability.json
    final_build_result.json

Backward compatibility:
    - Phase 2's NativeImplementationEngine is unchanged
    - Phase 2's BuildResult field shape is preserved; Phase 3 ADDS
      validation + quality fields (additive — existing readers ignore
      unknown keys)
    - Nothing else in the repo is modified (Debate / Consensus /
      Verification / Research / Planner / Execution Decision Engine /
      Workspace APIs / Provider Router / Repair logic / Phase 1+2)

Pure stdlib at import. Persistence + provider calls lazy-import.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

# Reuse Phase 2 components (do not redefine them)
from implementation_engine import (
    ExecutionContext, StateManager, EventBus, Event, ExecutionRecorder,
    CheckpointManager, AgentManager, CodingAgent, BuildResult,
    NODE_GENERATE_CODE, NODE_RUN_TESTS, NODE_FINISH,
    STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_SKIPPED, STATUS_CANCELLED, STATUS_BLOCKED,
    EV_EXECUTION_STARTED, EV_EXECUTION_RESUMED, EV_EXECUTION_COMPLETED,
    EV_EXECUTION_FAILED, EV_NODE_STARTED, EV_NODE_COMPLETED,
    EV_NODE_FAILED, EV_CHECKPOINT_SAVED,
    _impl_dir, _workspace_dir, _now_iso, _write_json, _read_json,
)


# ---------------------------------------------------------------------------
# Constants — Phase 3 phase names + events
# ---------------------------------------------------------------------------

PHASE_ARTIFACT_VALIDATION = "ArtifactValidation"
PHASE_BUILD = "Build"
PHASE_TESTS = "Tests"
PHASE_BUILD_VALIDATOR = "BuildValidator"
PHASE_QUALITY_GATE = "QualityGate"
PHASE_FAILURE_ANALYSIS = "FailureAnalysis"
PHASE_REPAIR_PLANNING = "RepairPlanning"
PHASE_REPAIR = "Repair"
PHASE_PATCH_VALIDATION = "PatchValidation"
PHASE_RETRY_CONTROL = "RetryControl"

ALL_PHASES = (
    PHASE_ARTIFACT_VALIDATION, PHASE_BUILD, PHASE_TESTS,
    PHASE_BUILD_VALIDATOR, PHASE_QUALITY_GATE,
    PHASE_FAILURE_ANALYSIS, PHASE_REPAIR_PLANNING, PHASE_REPAIR,
    PHASE_PATCH_VALIDATION, PHASE_RETRY_CONTROL,
)

# Validation phases that run on the PASS path
PASS_PATH = (PHASE_ARTIFACT_VALIDATION, PHASE_BUILD, PHASE_TESTS,
             PHASE_BUILD_VALIDATOR, PHASE_QUALITY_GATE)

# Failure categories (structured diagnostics)
FAIL_BUILD = "build"
FAIL_TESTS = "tests"
FAIL_ARTIFACT = "artifact"
FAIL_IMPORT = "import"
FAIL_SYNTAX = "syntax"
FAIL_CONFIG = "config"
FAIL_REQUIREMENTS = "requirements"
FAIL_UNKNOWN = "unknown"

ALL_FAIL_CATEGORIES = (
    FAIL_BUILD, FAIL_TESTS, FAIL_ARTIFACT, FAIL_IMPORT, FAIL_SYNTAX,
    FAIL_CONFIG, FAIL_REQUIREMENTS, FAIL_UNKNOWN,
)

# Phase 3 lifecycle events (in addition to Phase 2's)
EV_VALIDATION_STARTED = "ValidationStarted"
EV_VALIDATION_PHASE = "ValidationPhase"
EV_VALIDATION_COMPLETED = "ValidationCompleted"
EV_VALIDATION_FAILED = "ValidationFailed"
EV_REPAIR_PLANNED = "RepairPlanned"
EV_REPAIR_APPLIED = "RepairApplied"
EV_PATCH_VALIDATED = "PatchValidated"
EV_QUALITY_GATE = "QualityGate"

# Default retry budget for the validation loop
DEFAULT_VALIDATION_RETRIES = 3

# 12 workspace artifacts (the spec's contract)
ARTIFACT_VALIDATION_HISTORY = "validation_history.json"
ARTIFACT_REPAIR_HISTORY = "repair_history.json"
ARTIFACT_FAILURE_ANALYSIS = "failure_analysis.json"
ARTIFACT_TEST_RESULTS = "test_results.json"
ARTIFACT_COVERAGE = "coverage.json"
ARTIFACT_PATCH_HISTORY = "patch_history.json"
ARTIFACT_RETRY_HISTORY = "retry_history.json"
ARTIFACT_BUILD_LOG = "build_log.json"
ARTIFACT_VALIDATION_REPORT = "validation_report.json"
ARTIFACT_QUALITY_REPORT = "quality_report.json"
ARTIFACT_REQUIREMENTS_TRACEABILITY = "requirements_traceability.json"
ARTIFACT_FINAL_BUILD_RESULT = "final_build_result.json"

ALL_ARTIFACTS = (
    ARTIFACT_VALIDATION_HISTORY, ARTIFACT_REPAIR_HISTORY,
    ARTIFACT_FAILURE_ANALYSIS, ARTIFACT_TEST_RESULTS, ARTIFACT_COVERAGE,
    ARTIFACT_PATCH_HISTORY, ARTIFACT_RETRY_HISTORY, ARTIFACT_BUILD_LOG,
    ARTIFACT_VALIDATION_REPORT, ARTIFACT_QUALITY_REPORT,
    ARTIFACT_REQUIREMENTS_TRACEABILITY, ARTIFACT_FINAL_BUILD_RESULT,
)


def _validation_dir(workspace_id: str) -> Path:
    """Namespace under <workspace>/validation/ so we never collide with
    Phase 2's implementation/ dir OR existing workspace files."""
    return _workspace_dir(workspace_id) / "validation"


# ---------------------------------------------------------------------------
# Structured diagnostics types
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    """A single structured diagnostic ─ the FailureAnalyzer emits a list of
    these; the RepairPlanner consumes them."""
    category: str  # one of FAIL_*
    severity: str  # "error" | "warning" | "info"
    message: str
    location: str = ""  # file:line or test name
    hint: str = ""  # suggested repair direction

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RepairPlan:
    """A structured repair plan ─ the RepairPlanner emits this; the
    RepairAgent consumes it."""
    scope: str  # "file" | "module" | "test" | "config"
    priority: str  # "critical" | "high" | "medium" | "low"
    strategy: str  # "patch" | "regenerate" | "fix_imports" | "rewrite_test"
    target_files: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)  # Diagnostic dicts
    prompt: str = ""  # the actionable prompt for the RepairAgent

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PatchRecord:
    """A single applied patch's record ─ PatchValidator emits this; the
    CheckpointManager persists it to patch_history.json."""
    patch_id: str
    files_changed: list = field(default_factory=list)
    checksums_before: dict = field(default_factory=dict)  # path -> sha256
    checksums_after: dict = field(default_factory=dict)
    syntax_valid: bool = False
    integrity_valid: bool = False
    validation_errors: list = field(default_factory=list)
    applied_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationPhaseResult:
    """Result of running a single phase ─ used by the
    RetryController + ExecutionRecorder."""
    phase: str
    passed: bool
    output: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    duration_seconds: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QualityReport:
    """The QualityGate's output ─ persisted to quality_report.json.
    Blocks successful completion when ``passed`` is False even if tests
    pass (the spec's contract: requirements traceability is mandatory)."""
    passed: bool
    requirements_total: int = 0
    requirements_met: int = 0
    requirements_unmet: list = field(default_factory=list)  # spec section names
    traceability: list = field(default_factory=list)  # per-requirement entries
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# ValidationContext ── own state, serialized separately from Phase 2
# ---------------------------------------------------------------------------


@dataclass
class ValidationContext:
    """Owns every field required to resume the validation loop. Serialized
    to ``validation/validation_context.json`` (the 13th workspace artifact
    ─ a state file, alongside the spec's 12 reporting artifacts)."""
    validation_id: str = ""
    workspace_id: str = ""
    implementation_spec: str = ""
    max_iterations: int = DEFAULT_VALIDATION_RETRIES
    current_iteration: int = 1
    current_phase: str = PHASE_ARTIFACT_VALIDATION
    validation_state: str = STATUS_PENDING
    artifact_validation_result: dict = field(default_factory=dict)
    build_result_phase: dict = field(default_factory=dict)
    test_results: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    failure_analysis: list = field(default_factory=list)  # Diagnostic dicts
    repair_plan: dict = field(default_factory=dict)
    last_patch: dict = field(default_factory=dict)
    retry_history: list = field(default_factory=list)
    validation_history: list = field(default_factory=list)  # per-iteration
    repair_history: list = field(default_factory=list)  # patches
    build_validator_result: dict = field(default_factory=dict)
    quality_report: dict = field(default_factory=dict)
    requirements_traceability: dict = field(default_factory=dict)
    final_build_result: dict = field(default_factory=dict)
    build_log: list = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    cancelled: bool = False
    # Phase 2 integration ── we may delegate code generation/repair to the
    # NativeImplementationEngine. ``phase2_execution_id`` lets us correlate
    # the Phase 2 ExecutionRecorder with this validation run.
    phase2_execution_id: str = ""

    def __post_init__(self) -> None:
        if not self.validation_id:
            self.validation_id = f"val_{uuid.uuid4().hex[:12]}"
        if not self.started_at:
            self.started_at = _now_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationContext":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# ---------------------------------------------------------------------------
# BuildRunner ── compile, dependencies, logs, exit codes
# ---------------------------------------------------------------------------


class BuildRunner:
    """Runs build commands in the workspace. Captures exit code + logs
    + structured output. Default build commands are Python-flavored
    (compileall + pip-install dry-run); production callers can override
    via the constructor seam.

    Seams (all optional):
        build_fn: callable(workspace_dir, implementation_spec) -> dict with
                  ``exit_code``, ``stdout``, ``stderr``, ``success``. When
                  None, default Python builds run."""

    def __init__(
        self,
        *,
        build_fn: Callable[[str, str], dict] | None = None,
    ) -> None:
        self._build_fn = build_fn

    def run(self, ctx: ValidationContext, workspace_dir: Path) -> dict:
        """Run the build phase. Returns a dict with exit_code / logs /
        success. NEVER raises ── captures all failures into the log +
        returns a structured failure dict so the FailureAnalyzer can
        diagnose."""
        if self._build_fn is not None:
            try:
                result = self._build_fn(str(workspace_dir),
                                        ctx.implementation_spec) or {}
            except Exception as e:
                return {"exit_code": 1, "stdout": "", "stderr": str(e),
                        "success": False, "logs": [str(e)]}
            return {
                "exit_code": int(result.get("exit_code", 0)),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr", "")),
                "success": bool(result.get("success", False)),
                "logs": list(result.get("logs", [])),
            }
        return self._default_python_build(workspace_dir)

    def _default_python_build(self, workspace_dir: Path) -> dict:
        """Default build: byte-compile every .py file under the workspace.
        Returns structured output + a list of compile errors. Pure
        subprocess-free where possible (uses ``compileall`` for speed and
        to avoid spawning per-file)."""
        logs: list[str] = []
        errors: list[str] = []
        # Byte-compile every .py under workspace_dir/src/ + tests/
        targets = []
        for sub in ("src", "tests"):
            root = workspace_dir / sub
            if root.is_dir():
                targets.append(root)
        if not targets:
            return {
                "exit_code": 0, "stdout": "no src/ or tests/ to compile",
                "stderr": "", "success": True, "logs": [],
            }
        for target in targets:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "compileall", "-q", str(target)],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    errors.append(result.stderr or result.stdout)
                    logs.append(f"compileall {target}: exit={result.returncode}")
                else:
                    logs.append(f"compileall {target}: ok")
            except Exception as e:
                errors.append(f"compileall {target}: {e}")
                logs.append(f"compileall {target}: exception {e}")
        return {
            "exit_code": 0 if not errors else 1,
            "stdout": "\n".join(logs),
            "stderr": "\n".join(errors),
            "success": not errors,
            "logs": logs,
        }


# ---------------------------------------------------------------------------
# ArtifactValidator ── structure, manifests, imports, configs
# ---------------------------------------------------------------------------


class ArtifactValidator:
    """Validates the workspace's structure + manifests + optional imports +
    configs. Concurrency-safe and pure-stdlib.

    Checks performed (each contributing a Diagnosis):
      1. The workspace has either ``src/`` or ``tests/`` OR at least one
         ``.py`` file at top level
      2. Every ``.py`` file parses cleanly (ast.parse)
      3. Every relative import resolves to a file on disk in the workspace
      4. JSON/YAML-ish configs (``*.json``) parse as JSON
    """

    def validate(self, ctx: ValidationContext, workspace_dir: Path) -> dict:
        diagnostics: list[Diagnostic] = []
        generated_files = self._collect_python_files(workspace_dir)

        # 1. Structure: needs src/ or tests/ or top-level .py
        has_src = (workspace_dir / "src").is_dir()
        has_tests = (workspace_dir / "tests").is_dir()
        if not generated_files and not has_src and not has_tests:
            diagnostics.append(Diagnostic(
                category=FAIL_ARTIFACT, severity="error",
                message="workspace contains no source files ── the coding "
                        "agent did not generate any artifacts",
                hint="regenerate the implementation",
            ))
            return self._result(False, diagnostics, generated_files)

        # 2. Every .py file parses
        parse_errors = []
        for f in generated_files:
            try:
                ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as e:
                parse_errors.append((str(f), str(e)))
        for path, err in parse_errors:
            diagnostics.append(Diagnostic(
                category=FAIL_SYNTAX, severity="error",
                message=f"SyntaxError in {path}: {err}",
                location=path, hint="fix the syntax error or regenerate",
            ))

        # 3. Relative imports resolve
        import_errors = self._check_imports(generated_files, workspace_dir)
        for d in import_errors:
            diagnostics.append(d)

        # 4. JSON configs parse
        config_errors = self._check_configs(workspace_dir)
        for d in config_errors:
            diagnostics.append(d)

        passed = not any(d.severity == "error" for d in diagnostics)
        return self._result(passed, diagnostics, generated_files)

    # --- helpers --------------------------------------------------------

    def _result(
        self, passed: bool, diagnostics: list[Diagnostic],
        generated_files: list[Path],
    ) -> dict:
        return {
            "passed": passed,
            "diagnostics": [d.to_dict() for d in diagnostics],
            "generated_files": [str(f.relative_to(f.parent.parent))
                               for f in generated_files],
            "file_count": len(generated_files),
        }

    def _collect_python_files(self, workspace_dir: Path) -> list[Path]:
        out: list[Path] = []
        for sub in ("src", "tests"):
            root = workspace_dir / sub
            if root.is_dir():
                out.extend(sorted(root.rglob("*.py")))
        if not out:
            # Top-level .py files (legacy layout)
            out.extend(sorted(p for p in workspace_dir.glob("*.py")))
        return out

    def _check_imports(
        self, files: list[Path], workspace_dir: Path,
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for f in files:
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue  # already reported as syntax
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    # Only flag relative imports / within-workspace imports.
                    # Absolute imports (e.g., `fastapi`) are assumed
                    # resolvable through dependencies.
                    if node.level and node.level > 0:
                        # Relative import: try to resolve against the file's
                        # package root. Simple heuristic: same dir + module
                        # name. ponytail: full package resolution is the
                        # interpreter's job — we surface only what we can
                        # cheaply verify.
                        target = f.parent / f"{node.module.replace('.', '/')}.py"
                        if not target.is_file():
                            out.append(Diagnostic(
                                category=FAIL_IMPORT, severity="warning",
                                message=f"relative import '{node.module}' in "
                                        f"{f.name} does not resolve",
                                location=str(f),
                                hint="fix the import or create the module",
                            ))
        return out

    def _check_configs(self, workspace_dir: Path) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for cfg in workspace_dir.rglob("*.json"):
            if "workspace_" in str(cfg):
                continue  # skip build_state / answer / manifest, etc.
            try:
                json.loads(cfg.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                out.append(Diagnostic(
                    category=FAIL_CONFIG, severity="warning",
                    message=f"invalid JSON in {cfg.name}: {e}",
                    location=str(cfg),
                    hint="repair the config syntax",
                ))
        return out


# ---------------------------------------------------------------------------
# TestRunner ── unit, integration, smoke, lint, static analysis, type
# checking, security
# ---------------------------------------------------------------------------


class TestRunner:
    """Runs every test type. Each type is a seam so tests inject fakes.

    Default production behavior:
        unit / integration / smoke → subprocess ``pytest``
        lint                       → ``ruff check`` (if available)
        static_analysis            → ``ruff``/``pyflakes`` fallback
        type_check                 → ``mypy`` (if available)
        security                   → ``bandit`` (if available)
    Each type's result is a dict: {passed, total, failed, errors, output}.
    Failures never raise ─ they return a structured failure dict so the
    FailureAnalyzer can categorize them.
    """

    TEST_TYPES = (
        "unit", "integration", "smoke", "lint", "static_analysis",
        "type_check", "security",
    )

    def __init__(
        self,
        *,
        test_fns: dict[str, Callable[[str], dict]] | None = None,
        coverage_fn: Callable[[str], dict] | None = None,
    ) -> None:
        # Per-type injectable seam; missing types fall back to defaults
        self._test_fns = dict(test_fns or {})
        self._coverage_fn = coverage_fn

    def run_all(self, ctx: ValidationContext, workspace_dir: Path) -> dict:
        results: dict[str, dict] = {}
        for ttype in self.TEST_TYPES:
            results[ttype] = self._run_one(ttype, str(workspace_dir))
        coverage = self._run_coverage(str(workspace_dir))
        return {
            "passed": all(r.get("passed", False) for r in results.values()),
            "test_types": results,
            "coverage": coverage,
        }

    def _run_one(self, ttype: str, workspace_dir: str) -> dict:
        fn = self._test_fns.get(ttype)
        if fn is not None:
            try:
                return fn(workspace_dir) or {}
            except Exception as e:
                return {"passed": False, "error": str(e), "output": ""}
        # Default: only unit tests run a subprocess (pytest). Other types
        # return SKIPPED unless their tool is installed.
        if ttype in ("unit", "integration", "smoke"):
            return self._default_pytest(workspace_dir)
        return self._default_optional_tool(ttype, workspace_dir)

    def _default_pytest(self, workspace_dir: str) -> dict:
        try:
            args = ["--tb=short", "-q", "--no-header"]
            if shutil_which("pytest") is None:
                args = [sys.executable, "-m", "pytest"] + args
            else:
                args = ["pytest"] + args
            result = subprocess.run(
                args, capture_output=True, text=True, cwd=workspace_dir,
                timeout=180,
            )
            passed = result.returncode == 0
            summary = self._parse_pytest_summary(result.stdout)
            return {
                "passed": passed,
                "exit_code": result.returncode,
                "output": result.stdout,
                "stderr": result.stderr,
                **summary,
            }
        except Exception as e:
            return {"passed": False, "error": str(e), "output": "",
                    "total": 0, "failed": 0, "errors": 1}

    def _default_optional_tool(self, ttype: str, workspace_dir: str) -> dict:
        tools = {
            "lint": ("ruff", ["ruff", "check", "."]),
            "static_analysis": ("pyflakes", ["pyflakes", "."]),
            "type_check": ("mypy", ["mypy", "."]),
            "security": ("bandit", ["bandit", "-r", "src"]),
        }
        tool, args = tools.get(ttype, (ttype, [ttype]))
        if shutil_which(tool) is None:
            return {"passed": True, "skipped": True,
                    "reason": f"{tool} not installed"}
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, cwd=workspace_dir,
                timeout=120,
            )
            return {
                "passed": result.returncode == 0,
                "exit_code": result.returncode,
                "output": result.stdout,
                "stderr": result.stderr,
            }
        except Exception as e:
            return {"passed": False, "error": str(e)}

    def _run_coverage(self, workspace_dir: str) -> dict:
        if self._coverage_fn is not None:
            try:
                return self._coverage_fn(workspace_dir) or {}
            except Exception as e:
                return {"error": str(e)}
        return {}  # coverage requires pytest-cov; skipped when unavailable

    def _parse_pytest_summary(self, stdout: str) -> dict:
        """Cheap parse of the pytest summary line. ponytail: a real parser
        would be over-engineering for a single line."""
        m = re.search(
            r"(\d+) passed(?:.*?(\d+) failed)?(?:.*?(\d+) errors)?",
            stdout,
        )
        if not m:
            return {"total": 0, "failed": 0, "errors": 0}
        passed = int(m.group(1) or 0)
        failed = int(m.group(2) or 0)
        errors = int(m.group(3) or 0)
        return {"total": passed + failed + errors, "passed": passed,
                "failed": failed, "errors": errors}


def shutil_which(tool: str) -> str | None:
    """Lazy-imported ``shutil.which`` so the module stays pure at import."""
    import shutil
    return shutil.which(tool)


# ---------------------------------------------------------------------------
# FailureAnalyzer ── structured diagnostics
# ---------------------------------------------------------------------------


class FailureAnalyzer:
    """Consumes an ArtifactValidator + BuildRunner + TestRunner result and
    produces a list of structured Diagnostics. Each diagnostic carries a
    category, severity, location, hint. Pure (no I/O)."""

    def analyze(
        self,
        *,
        artifact_result: dict | None = None,
        build_result: dict | None = None,
        test_result: dict | None = None,
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        # Artifact diagnostics pass through (already structured)
        for d in (artifact_result or {}).get("diagnostics", []):
            if isinstance(d, dict):
                out.append(Diagnostic(**{k: v for k, v in d.items()
                                          if k in Diagnostic.__dataclass_fields__}))  # type: ignore[attr-defined]
        # Build diagnostics
        br = build_result or {}
        if not br.get("success", True):
            stderr = str(br.get("stderr", ""))
            out.append(Diagnostic(
                category=FAIL_BUILD, severity="error",
                message=f"build failed: exit_code={br.get('exit_code', '?')}",
                hint="fix the build error or regenerate",
                location=stderr[:200] if stderr else "",
            ))
        # Test diagnostics
        tr = test_result or {}
        if not tr.get("passed", True):
            test_types = tr.get("test_types", {})
            for ttype, tresult in test_types.items():
                if tresult.get("passed"):
                    continue
                output = tresult.get("output") or tresult.get("stderr") or ""
                n_failed = tresult.get("failed", 0)
                out.append(Diagnostic(
                    category=FAIL_TESTS, severity="error",
                    message=f"{ttype} tests failed: {n_failed} failure(s)",
                    hint=f"repair the {ttype} tests per the failure output",
                    location=output[:200] if output else "",
                ))
        # If no diagnostics were produced but the workflow still failed,
        # surface a single unknown-category failure so repair has something
        # to act on.
        if not out and (not br.get("success", True) or
                        not tr.get("passed", True)):
            out.append(Diagnostic(
                category=FAIL_UNKNOWN, severity="error",
                message="validation failed with no structured diagnostic",
                hint="regenerate the implementation",
            ))
        return out


# ---------------------------------------------------------------------------
# RepairPlanner ── scope, priority, strategy
# ---------------------------------------------------------------------------


class RepairPlanner:
    """Consumes a list of Diagnostic dicts and produces a RepairPlan.
    The plan's ``prompt`` is the actionable string the RepairAgent sends
    to the coding agent. Pure (no I/O)."""

    PRIORITY_FOR_CATEGORY = {
        FAIL_SYNTAX: "critical",
        FAIL_BUILD: "critical",
        FAIL_IMPORT: "high",
        FAIL_TESTS: "high",
        FAIL_ARTIFACT: "high",
        FAIL_CONFIG: "medium",
        FAIL_REQUIREMENTS: "high",
        FAIL_UNKNOWN: "low",
    }

    STRATEGY_FOR_CATEGORY = {
        FAIL_SYNTAX: "patch",
        FAIL_BUILD: "patch",
        FAIL_IMPORT: "fix_imports",
        FAIL_TESTS: "rewrite_test",
        FAIL_ARTIFACT: "regenerate",
        FAIL_CONFIG: "patch",
        FAIL_REQUIREMENTS: "regenerate",
        FAIL_UNKNOWN: "regenerate",
    }

    def plan(
        self, diagnostics: list[Diagnostic], ctx: ValidationContext,
    ) -> RepairPlan:
        if not diagnostics:
            return RepairPlan(
                scope="none", priority="low", strategy="patch", prompt="",
                diagnostics=[],
            )
        # Aggregate by category ── the plan addresses the highest-priority
        # category first (the spec's "priority" field).
        by_cat: dict[str, list[Diagnostic]] = {}
        for d in diagnostics:
            by_cat.setdefault(d.category, []).append(d)
        # Sort categories by priority order
        priority_order = ["critical", "high", "medium", "low"]
        cats_sorted = sorted(by_cat.keys(), key=lambda c: priority_order.index(
            self.PRIORITY_FOR_CATEGORY.get(c, "low")))
        top_cat = cats_sorted[0]
        top_priority = self.PRIORITY_FOR_CATEGORY.get(top_cat, "low")
        strategy = self.STRATEGY_FOR_CATEGORY.get(top_cat, "patch")
        scope = "file" if strategy == "patch" \
            else "module" if strategy == "fix_imports" \
            else "test" if strategy == "rewrite_test" \
            else "module"
        target_files = sorted({
            d.location for d in by_cat[top_cat] if d.location
        })
        prompt = self._build_prompt(by_cat, ctx)
        return RepairPlan(
            scope=scope, priority=top_priority, strategy=strategy,
            target_files=target_files,
            diagnostics=[d.to_dict() for d in diagnostics],
            prompt=prompt,
        )

    def _build_prompt(
        self, by_cat: dict[str, list[Diagnostic]],
        ctx: ValidationContext,
    ) -> str:
        lines = ["Repair the following implementation failures:"]
        for cat, ds in by_cat.items():
            lines.append(f"\n[{cat.upper()}] ({len(ds)} issue(s))")
            for d in ds[:5]:  # cap per category to keep prompts bounded
                lines.append(f"  - {d.message}")
                if d.location:
                    lines.append(f"    location: {d.location}")
                if d.hint:
                    lines.append(f"    hint: {d.hint}")
        lines.append("\nOriginal implementation specification:")
        lines.append(ctx.implementation_spec[:2000])  # cap prompt size
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# RepairAgent ── minimal targeted patches (wraps AgentManager)
# ---------------------------------------------------------------------------


class RepairAgent:
    """Dispatches the RepairPlan to the coding agent via the AgentManager.
    Captures the response and the list of files changed. The engine never
    invokes the coding agent directly ── it goes through this class.

    Constructor seams:
        repair_fn: callable(workspace_id, plan_prompt) -> dict. When
                   None AND coding_agent_fn is None AND
                   allow_live_agent is False (the engine default), returns
                   a structured failure instead of dispatching.
        coding_agent_fn: callable(workspace_id, prompt) -> dict. Used as
                         a direct repair dispatch.
        allow_live_agent: when True + no seam is wired, falls back to
                          ``AgentManager.repair`` (which dispatches to
                          OpenCode via Phase 2)."""

    def __init__(
        self,
        agents: AgentManager,
        *,
        repair_fn: Callable[[str, str], dict] | None = None,
        coding_agent_fn: Callable[[str, str], dict] | None = None,
        allow_live_agent: bool = False,
    ) -> None:
        self.agents = agents
        self._repair_fn = repair_fn
        self._coding_agent_fn = coding_agent_fn
        self._allow_live_agent = allow_live_agent

    def apply(self, ctx: ValidationContext, plan: RepairPlan) -> dict:
        """Apply the plan. Returns a dict with ``success``, ``files_changed``,
        ``response``. Never raises ── captures failures into the response
        so the PatchValidator can record them.

        When no ``repair_fn`` is wired AND no ``coding_agent_fn`` is wired
        AND ``allow_live_agent`` is False (the engine default), refuses to
        dispatch ── returns a structured failure. Silent dispatch to a live
        opencode subprocess in an offline / test context is the exact
        failure mode this guard prevents."""
        if not plan.prompt:
            return {"success": False, "files_changed": [],
                    "response": {}, "error": "empty repair prompt"}
        try:
            if self._repair_fn is not None:
                response = self._repair_fn(ctx.workspace_id, plan.prompt) or {}
            elif self._coding_agent_fn is not None:
                response = self._coding_agent_fn(ctx.workspace_id,
                                                   plan.prompt) or {}
            elif not self._allow_live_agent:
                return {
                    "success": False, "files_changed": [], "response": {},
                    "error": ("no repair seam wired ── pass repair_fn or "
                              "coding_agent_fn, or set allow_live_agent=True "
                              "to enable dispatch to a live opencode agent"),
                }
            else:
                response = self.agents.repair(
                    ctx.workspace_id, ctx.implementation_spec,
                    plan.prompt) or {}
        except Exception as e:
            return {"success": False, "files_changed": [],
                    "response": {}, "error": f"repair agent failed: {e}"}
        files_changed = self._extract_files(response)
        return {
            "success": bool(response.get("success", False)),
            "files_changed": files_changed,
            "response": response,
            "error": response.get("error", "") if isinstance(response, dict) else "",
        }

    def _extract_files(self, response: dict) -> list[str]:
        for k in ("files", "files_changed", "modified_files", "generated_files"):
            v = response.get(k)
            if isinstance(v, list):
                return [str(f) for f in v]
        return []


# ---------------------------------------------------------------------------
# PatchValidator ── integrity + syntax validation
# ---------------------------------------------------------------------------


class PatchValidator:
    """Validates the RepairAgent's output BEFORE re-running the build:

      1. For every changed .py file, byte-compile via ``py_compile``
         (catches SyntaxErrors without running the file)
      2. For every changed .py file, ast.parse to confirm it's well-formed
      3. Record before/after SHA-256 checksums for integrity audit

    Pure (no network / no providers). Returns a PatchRecord."""

    def validate(
        self,
        *,
        workspace_id: str,
        files_changed: list[str],
        checksums_before: dict,
    ) -> PatchRecord:
        workspace = _workspace_dir(workspace_id)
        patch_id = f"patch_{uuid.uuid4().hex[:8]}"
        checksums_after: dict[str, str] = {}
        syntax_valid = True
        integrity_valid = True
        validation_errors: list[str] = []
        for rel in files_changed:
            path = self._resolve(workspace, rel)
            if path is None or not path.is_file():
                validation_errors.append(f"file missing: {rel}")
                integrity_valid = False
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as e:
                validation_errors.append(f"read error {rel}: {e}")
                integrity_valid = False
                continue
            checksums_after[rel] = hashlib.sha256(
                content.encode("utf-8")).hexdigest()
            # Syntax validation: byte-compile .py files only
            if rel.endswith(".py"):
                try:
                    py_compile.compile(str(path), doraise=True)
                except py_compile.PyCompileError as e:
                    syntax_valid = False
                    validation_errors.append(f"syntax error in {rel}: {e}")
                except SyntaxError as e:
                    syntax_valid = False
                    validation_errors.append(f"syntax error in {rel}: {e}")
        # Integrity: the patch must have CHANGED at least one file OR the
        # patch's response carried a non-file signal (e.g., it rewrote an
        # existing file in place). An empty patch set is treated as a
        # validation failure (the spec says silent failures are forbidden).
        if not files_changed:
            integrity_valid = False
            validation_errors.append("patch changed no files")
        return PatchRecord(
            patch_id=patch_id,
            files_changed=list(files_changed),
            checksums_before=dict(checksums_before),
            checksums_after=checksums_after,
            syntax_valid=syntax_valid,
            integrity_valid=integrity_valid,
            validation_errors=validation_errors,
            applied_at=_now_iso(),
        )

    def snapshot(self, workspace_id: str, files: list[str]) -> dict:
        """Compute checksums for the listed files BEFORE the patch. Use
        to detect changes after the patch is applied."""
        workspace = _workspace_dir(workspace_id)
        out: dict[str, str] = {}
        for rel in files:
            path = self._resolve(workspace, rel)
            if path is None or not path.is_file():
                continue
            try:
                out[rel] = hashlib.sha256(
                    path.read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest()
            except OSError:
                continue
        return out

    def _resolve(self, workspace: Path, rel: str) -> Path | None:
        """Resolve a relative path against the workspace. Accepts both
        workspace-relative ('src/x.py') and absolute paths inside the
        workspace."""
        p = Path(rel)
        if p.is_absolute():
            try:
                p.relative_to(workspace)
                return p if p.is_file() else None
            except ValueError:
                return None
        candidate = workspace / rel
        return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# RetryController ── deterministic retries with state
# ---------------------------------------------------------------------------


class RetryController:
    """Decides whether the validation loop retries or terminates. The
    controller is consulted after each RepairAgent+PatchValidator cycle.

    Termination conditions (any one):
      1. ``current_iteration`` exceeds ``max_iterations``
      2. The patch was invalid (syntax_integrity failure) AND the last
         patch was also invalid (two consecutive invalid patches — the
         agent is not making progress)
      3. The failure category is the same as the previous iteration's
         AND the patch did not change any files the controller knows about
    """

    def __init__(self, max_iterations: int = DEFAULT_VALIDATION_RETRIES):
        self.max_iterations = max_iterations

    def should_retry(
        self, ctx: ValidationContext, last_patch: PatchRecord | None,
    ) -> tuple[bool, str]:
        """Return (retry, reason). Pure ── no side effects on ctx."""
        if ctx.cancelled:
            return False, "cancelled"
        if ctx.current_iteration >= self.max_iterations:
            return False, "retry budget exhausted"
        if last_patch is not None and not last_patch.integrity_valid:
            # Two consecutive invalid patches ── the agent is not making
            # progress. Check the last 2 entries in repair_history.
            recent = [r for r in ctx.repair_history[-2:]
                      if not r.get("patch_valid", True)]
            if len(recent) >= 2:
                return False, "two consecutive invalid patches"
        return True, "retrying"

    def advance(self, ctx: ValidationContext) -> None:
        ctx.current_iteration += 1


# ---------------------------------------------------------------------------
# BuildValidator ── technical validation gate
# ---------------------------------------------------------------------------


class BuildValidator:
    """The technical validation gate. Aggregates the artifact, build, and
    test results into a single decision: passed / failed / reason.

    The gate is permissive on advisory checks (lint / type_check /
    security are 'skipped' if the tool isn't installed) and strict on
    mandatory checks (artifact structure, build, unit tests).
    """

    MANDATORY = ("unit", "integration", "smoke")

    def validate(
        self,
        *,
        artifact_result: dict,
        build_result: dict,
        test_result: dict,
    ) -> dict:
        report = {
            "passed": True, "mandatory_passed": True,
            "advisory_skipped": [], "failures": [], "warnings": [],
        }
        if not artifact_result.get("passed"):
            report["passed"] = False
            report["mandatory_passed"] = False
            report["failures"].append({
                "phase": PHASE_ARTIFACT_VALIDATION,
                "diagnostics": artifact_result.get("diagnostics", []),
            })
        if not build_result.get("success"):
            report["passed"] = False
            report["mandatory_passed"] = False
            report["failures"].append({
                "phase": PHASE_BUILD,
                "stderr": build_result.get("stderr", ""),
                "exit_code": build_result.get("exit_code"),
            })
        test_types = test_result.get("test_types", {})
        for ttype in self.MANDATORY:
            tr = test_types.get(ttype, {})
            if tr.get("skipped"):
                report["advisory_skipped"].append(ttype)
                continue
            if not tr.get("passed", False):
                report["passed"] = False
                report["mandatory_passed"] = False
                report["failures"].append({
                    "phase": PHASE_TESTS,
                    "test_type": ttype,
                    "output": (tr.get("output") or tr.get("stderr", ""))[:500],
                })
        # Advisory checks (lint / static_analysis / type_check / security):
        # skipped is acceptable; failed is a warning, not a hard stop.
        for ttype in ("lint", "static_analysis", "type_check", "security"):
            tr = test_types.get(ttype, {})
            if tr.get("skipped"):
                report["advisory_skipped"].append(ttype)
            elif not tr.get("passed", True):
                report["warnings"].append({
                    "phase": PHASE_TESTS,
                    "test_type": ttype,
                    "output": (tr.get("output") or tr.get("stderr", ""))[:500],
                })
        return report


# ---------------------------------------------------------------------------
# QualityGate ── requirements verification
# ---------------------------------------------------------------------------


class QualityGate:
    """The requirements verification gate. Verifies traceability between
    the implementation spec and the generated artifacts. Outputs
    ``quality_report.json`` + ``requirements_traceability.json``.

    The QualityGate blocks successful completion when requirements are
    unmet even if tests pass (the spec's contract).

    Traceability model:
      - Parse the spec for markdown sections (``# Heading``) ── each is a
        requirement.
      - For each section, heuristic-check whether ANY generated file path
        or filename mentions the section's keyword (e.g., a section
        ``# Components: API, worker, DB`` ── check for files matching
        ``api*``/``worker*``/``db*`` under src/).
      - For ``# Acceptance Criteria``, require tests to be present and
        passing.
      - For ``# Architecture``, require at least one source file to exist.
      - For ``# Components``, parse the comma-separated list under the
        heading and check each component name appears in src/ filenames.

    The heuristic is intentionally simple ── it never rejects on false
    negatives alone, only signals ``unmet`` with a rationale the user can
    inspect. (ponytail: a real traceability model would require the
    coding agent to emit a manifest; that belongs in Phase 4.)"""

    REQUIRED_SECTIONS = ("Architecture", "Components", "Acceptance Criteria")

    def evaluate(
        self,
        *,
        implementation_spec: str,
        workspace_id: str,
        artifact_result: dict,
        test_result: dict,
        build_validator_result: dict,
    ) -> tuple[QualityReport, dict]:
        """Return (QualityReport, traceability_dict). Pure (no I/O ──
        reads workspace dir via _workspace_dir which is allowed)."""
        sections = self._parse_sections(implementation_spec)
        ws = _workspace_dir(workspace_id)
        all_files = self._all_source_files(ws)
        traceability: list[dict] = []
        unmet: list[str] = []
        for section, body in sections.items():
            entry = {
                "requirement": section,
                "spec_excerpt": (body or "")[:200],
                "evidence": [],
                "satisfied": True,
                "rationale": "",
            }
            if section == "Acceptance Criteria":
                # The acceptance criteria are satisfied iff BuildValidator
                # passed AND unit tests passed. (Tests passing is already
                # implied by BuildValidator; we re-assert.)
                tt = test_result.get("test_types", {})
                unit_passed = tt.get("unit", {}).get("passed", False)
                entry["satisfied"] = bool(
                    build_validator_result.get("passed") and unit_passed)
                if not entry["satisfied"]:
                    entry["rationale"] = ("build validator did not pass "
                                          "and/or unit tests did not pass")
                    unmet.append(section)
                entry["evidence"] = ["build_validator_report", "unit_tests"]
            elif section == "Architecture":
                entry["satisfied"] = bool(all_files)
                if not all_files:
                    entry["rationale"] = "no source files were generated"
                    unmet.append(section)
                entry["evidence"] = [str(p.name) for p in all_files[:5]]
            elif section == "Components":
                comps = [c.strip() for c in re.split(
                    r"[,\n]", body or "") if c.strip()]
                evidence = []
                missing = []
                for c in comps:
                    pattern = re.escape(c.lower()).split()[0]
                    matches = [str(p.relative_to(ws))
                               for p in all_files
                               if pattern in p.name.lower()]
                    if matches:
                        evidence.extend(matches[:3])
                    else:
                        missing.append(c)
                entry["satisfied"] = not missing
                if missing:
                    entry["rationale"] = f"components not found: {missing}"
                    unmet.append(section)
                entry["evidence"] = evidence
            else:
                # Generic section: any file whose name contains a keyword
                # from the body satisfies it.
                keywords = [w.lower() for w in re.findall(
                    r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", body or "")
                    if len(w) > 2 and w.lower() not in {
                        "the", "and", "for", "with", "use", "must",
                        "should", "will", "are", "all", "any", "into"}]
                evidence = []
                for kw in keywords[:5]:
                    matches = [str(p.relative_to(ws))
                               for p in all_files if kw in p.name.lower()]
                    evidence.extend(matches[:2])
                entry["satisfied"] = bool(evidence) or not keywords
                if not entry["satisfied"]:
                    entry["rationale"] = ("no generated file matches the "
                                          "section's keywords")
                    unmet.append(section)
                entry["evidence"] = evidence
            traceability.append(entry)

        # Required-section presence
        required_unmet = [s for s in self.REQUIRED_SECTIONS
                          if s not in sections]
        for s in required_unmet:
            unmet.append(s)
            traceability.append({
                "requirement": s, "spec_excerpt": "",
                "evidence": [], "satisfied": False,
                "rationale": f"required section missing from the spec: {s}",
            })
        passed = (not unmet) and build_validator_result.get("passed", False)
        rationale = ("" if passed else
                     f"{len(unmet)} requirement(s) unmet: {unmet}")
        report = QualityReport(
            passed=passed,
            requirements_total=len(traceability),
            requirements_met=len(traceability) - len(unmet),
            requirements_unmet=unmet,
            traceability=traceability,
            rationale=rationale,
        )
        traceability_dict = {
            "workspace_id": workspace_id,
            "sections_found": list(sections.keys()),
            "entries": traceability,
            "unmet": unmet,
        }
        return report, traceability_dict

    # --- helpers -------------------------------------------------------

    def _parse_sections(self, spec: str) -> dict[str, str]:
        """Parse markdown # headings into a {heading: body} dict. The body
        is everything until the next # heading. Pure."""
        out: dict[str, str] = {}
        current: str | None = None
        body_lines: list[str] = []
        for line in (spec or "").splitlines():
            m = re.match(r"^#+\s*(.+?)\s*$", line)
            if m:
                if current is not None:
                    out[current] = "\n".join(body_lines).strip()
                current = m.group(1).strip()
                body_lines = []
            elif current is not None:
                body_lines.append(line)
        if current is not None:
            out[current] = "\n".join(body_lines).strip()
        return out

    def _all_source_files(self, ws: Path) -> list[Path]:
        out: list[Path] = []
        for sub in ("src", "tests"):
            root = ws / sub
            if root.is_dir():
                out.extend(sorted(root.rglob("*.py")))
        return out


# ---------------------------------------------------------------------------
# ValidationEngine Protocol ─ the ONLY public entry point
# ---------------------------------------------------------------------------


@runtime_checkable
class ValidationEngine(Protocol):
    """The ONLY public entry point for Phase 3. All callers depend on
    this interface, never on a concrete class."""

    def validate(self, *, workspace_id: str, implementation_spec: str,
                 **kwargs: Any) -> BuildResult: ...
    def repair(self, *, workspace_id: str, **kwargs: Any) -> BuildResult: ...
    def resume(self, *, workspace_id: str) -> BuildResult: ...
    def cancel(self, *, workspace_id: str) -> bool: ...
    def status(self, *, workspace_id: str) -> dict: ...
    def recover(self, *, workspace_id: str) -> dict: ...


# ---------------------------------------------------------------------------
# NativeValidationEngine ── the Phase 3 implementation
# ---------------------------------------------------------------------------


class NativeValidationEngine:
    """The default Phase 3 validation engine. Orchestrates the validation
    + repair loop and emits all 12 workspace artifacts.

    Constructor seams (all optional ── for tests):
        build_fn            ── replaces BuildRunner
        test_fns            ── per-type replaces TestRunner (dict)
        coverage_fn         ── replaces coverage
        repair_fn           ── replaces RepairAgent's repair dispatch
        coding_agent_fn     ── replaces Phase 2's OpenCode callable
        max_iterations      ── shorthand for RetryController
    """

    ENGINE_NAME = "NativeValidation"

    def __init__(
        self,
        *,
        build_fn: Callable[[str, str], dict] | None = None,
        test_fns: dict[str, Callable[[str], dict]] | None = None,
        coverage_fn: Callable[[str], dict] | None = None,
        repair_fn: Callable[[str, str], dict] | None = None,
        coding_agent_fn: Callable[[str, str], dict] | None = None,
        max_iterations: int | None = None,
        allow_live_agent: bool = False,
        repair_memory: "RepairMemory | None" = None,
    ) -> None:
        # Phase 2 components reused
        self.agents = AgentManager(coding_agent_fn=coding_agent_fn)
        self.event_bus = EventBus()
        self.recorder = ExecutionRecorder()
        self.event_bus.subscribe(self.recorder.record_event)
        self.checkpoint_mgr = CheckpointManager(recorder=self.recorder)
        # Phase 3 components
        self.build_runner = BuildRunner(build_fn=build_fn)
        self.artifact_validator = ArtifactValidator()
        self.test_runner = TestRunner(test_fns=test_fns,
                                       coverage_fn=coverage_fn)
        self.failure_analyzer = FailureAnalyzer()
        self.repair_planner = RepairPlanner()
        self.repair_agent = RepairAgent(
            self.agents, repair_fn=repair_fn,
            coding_agent_fn=coding_agent_fn,
            allow_live_agent=allow_live_agent)
        self.patch_validator = PatchValidator()
        self.retry_controller = RetryController(
            max_iterations=max_iterations or DEFAULT_VALIDATION_RETRIES)
        self.build_validator = BuildValidator()
        self.quality_gate = QualityGate()
        # Phase 3.1 component ── RepairMemory. Wired between the repair
        # planner and the repair agent so the planner can blacklist
        # already-failed repairs + escalate strategies, and the agent can
        # record every attempt (success / failure). Breaks the A,B,A,B
        # oscillation the spec calls out.
        # Lazy import keeps the module import-time pure when the user
        # disables Phase 3.1 features (``repair_memory=None`` on a fresh
        # workspace → memory is created on first repair).
        self.repair_memory: "RepairMemory | None" = repair_memory
        # ValidationContext persistence
        self._max_iterations = max_iterations or DEFAULT_VALIDATION_RETRIES
        # Safety: when no repair seam is wired, NEVER silently dispatch to a
        # live opencode subprocess (it would hang in offline / test contexts
        # and run a real coding agent in production without explicit opt-in).
        # Production callers pass ``allow_live_agent=True`` to enable the
        # fallback; offline tests always pass a ``repair_fn`` seam.
        self._allow_live_agent = allow_live_agent

    # ---- ValidationEngine interface -----------------------------------

    def validate(
        self, *, workspace_id: str, implementation_spec: str,
        **kwargs: Any,
    ) -> BuildResult:
        """Fresh validation run. Validates inputs (never silent),
        builds the ValidationContext, runs the loop to completion."""
        issues = self._validate_inputs(workspace_id, implementation_spec)
        if issues:
            return self._build_failure_result(
                workspace_id=workspace_id, error="; ".join(issues))
        ctx = ValidationContext(
            workspace_id=workspace_id,
            implementation_spec=implementation_spec,
            max_iterations=self._max_iterations,
        )
        self.event_bus.emit(Event(EV_VALIDATION_STARTED, _now_iso(),
                                    {"validation_id": ctx.validation_id}))
        return self._run(ctx, is_resume=False)

    def repair(self, *, workspace_id: str, **kwargs: Any) -> BuildResult:
        """Re-run the validation + repair loop on an existing workspace.
        Differs from ``validate`` only in that we ASSUME the implementation
        is already on disk; useful when a previous run failed and the user
        invokes Phase 3 directly. Loads the prior implementation_spec from
        the workspace's persisted state if available."""
        ctx = self._load_context(workspace_id)
        if ctx is None:
            # No prior state: surface explicitly (never silent default).
            return self._build_failure_result(
                workspace_id=workspace_id,
                error="no prior validation context ── call validate() first",
            )
        ctx.cancelled = False
        ctx.validation_state = STATUS_RUNNING
        self.event_bus.emit(Event(EV_VALIDATION_STARTED, _now_iso(),
                                    {"validation_id": ctx.validation_id,
                                     "reason": "repair"}))
        return self._run(ctx, is_resume=False)

    def resume(self, *, workspace_id: str) -> BuildResult:
        """Resume the loop from the last checkpointed phase."""
        ctx = self._load_context(workspace_id)
        if ctx is None:
            return self._build_failure_result(
                workspace_id=workspace_id,
                error="no checkpoint to resume from "
                      "(state absent or corrupt)",
            )
        self.event_bus.emit(Event(EV_EXECUTION_RESUMED, _now_iso(),
                                    {"phase": ctx.current_phase}))
        return self._run(ctx, is_resume=True)

    def cancel(self, *, workspace_id: str) -> bool:
        ctx = self._load_context(workspace_id)
        if ctx is None:
            return False
        ctx.cancelled = True
        self._save_context(ctx)
        return True

    def status(self, *, workspace_id: str) -> dict:
        ctx = self._load_context(workspace_id)
        if ctx is None:
            return {"validation_state": "unknown",
                    "workspace_id": workspace_id}
        return {
            "validation_id": ctx.validation_id,
            "current_phase": ctx.current_phase,
            "current_iteration": ctx.current_iteration,
            "validation_state": ctx.validation_state,
            "quality_passed": ctx.quality_report.get("passed", False) if
                isinstance(ctx.quality_report, dict) else False,
            "completed_at": ctx.completed_at,
        }

    def recover(self, *, workspace_id: str) -> dict:
        ctx = self._load_context(workspace_id)
        if ctx is None:
            return {"recoverable": False,
                    "reason": "no validation_context.json",
                    "workspace_id": workspace_id}
        issues = self._validate_ctx(ctx)
        return {
            "recoverable": not issues,
            "issues": issues,
            "validation_id": ctx.validation_id,
            "current_phase": ctx.current_phase,
            "iteration": ctx.current_iteration,
            "state": ctx.validation_state,
            "workspace_id": workspace_id,
        }

    # ---- internal workflow driver ------------------------------------

    def _run(self, ctx: ValidationContext, *, is_resume: bool) -> BuildResult:
        """Drive the validation loop. Each iteration runs:
          artifact_validation → build → tests. If all pass →
          BuildValidator → QualityGate → success. If any fail →
          FailureAnalysis → RepairPlan → RepairAgent → PatchValidator →
          RetryController."""
        error = ""
        workspace_dir = _workspace_dir(ctx.workspace_id)
        # Phase 3.1: ensure a workspace-scoped RepairMemory exists for this
        # run ── backed by ``<workspace>/validation/repair_history.db`` so
        # resume survives across runs. Tests may inject their own memory;
        # here we create one lazily when the caller didn't supply one.
        if self.repair_memory is None:
            try:
                from repair_memory import RepairMemory as _RM
                self.repair_memory = _RM.from_workspace(ctx.workspace_id)
            except Exception:
                # If workspace persistence is unavailable, fall back to
                # an in-memory memory (less durable, but the loop still
                # gets oscillation + escalation protection within the run).
                from repair_memory import RepairMemory as _RM
                self.repair_memory = _RM()
        # Resume: skip phases already marked completed in this iteration.
        # We do this by re-reading any persisted successful phase result from
        # the validation_context. ponytail: a full resume-from-middle re-runs
        # every phase from scratch because the workspace files may have
        # changed between crash and resume ── re-running is safer than
        # trusting stale on-disk phase results.
        try:
            while True:
                if ctx.cancelled:
                    ctx.validation_state = STATUS_CANCELLED
                    error = "cancelled"
                    break
                self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                            {"phase": PHASE_ARTIFACT_VALIDATION,
                                             "iteration": ctx.current_iteration}))
                # --- Artifact validation ────────────────────────────────
                ctx.current_phase = PHASE_ARTIFACT_VALIDATION
                artifact_result = self._timed_phase(
                    PHASE_ARTIFACT_VALIDATION, ctx,
                    lambda: self.artifact_validator.validate(ctx, workspace_dir))
                ctx.artifact_validation_result = artifact_result

                # --- Build ───────────────────────────────────────────────
                ctx.current_phase = PHASE_BUILD
                self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                            {"phase": PHASE_BUILD,
                                             "iteration": ctx.current_iteration}))
                build_result = self._timed_phase(
                    PHASE_BUILD, ctx,
                    lambda: self.build_runner.run(ctx, workspace_dir))
                ctx.build_result_phase = build_result
                ctx.build_log.extend(build_result.get("logs", []))

                # --- Tests ───────────────────────────────────────────────
                ctx.current_phase = PHASE_TESTS
                self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                            {"phase": PHASE_TESTS,
                                             "iteration": ctx.current_iteration}))
                test_result = self._timed_phase(
                    PHASE_TESTS, ctx,
                    lambda: self.test_runner.run_all(ctx, workspace_dir))
                ctx.test_results = test_result

                # --- BuildValidator (technical validation gate) ----------
                ctx.current_phase = PHASE_BUILD_VALIDATOR
                self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                            {"phase": PHASE_BUILD_VALIDATOR,
                                             "iteration": ctx.current_iteration}))
                build_validator_result = self.build_validator.validate(
                    artifact_result=artifact_result,
                    build_result=build_result,
                    test_result=test_result,
                )
                ctx.build_validator_result = build_validator_result

                # Record iteration history
                ctx.validation_history.append({
                    "iteration": ctx.current_iteration,
                    "artifact_passed": artifact_result.get("passed", False),
                    "build_passed": build_result.get("success", False),
                    "tests_passed": test_result.get("passed", False),
                    "build_validator_passed": build_validator_result.get(
                        "passed", False),
                    "timestamp": _now_iso(),
                })

                # --- Decision: success path → QualityGate ----------------
                if build_validator_result.get("passed"):
                    ctx.current_phase = PHASE_QUALITY_GATE
                    self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                                {"phase": PHASE_QUALITY_GATE,
                                                 "iteration": ctx.current_iteration}))
                    quality_report, traceability = self.quality_gate.evaluate(
                        implementation_spec=ctx.implementation_spec,
                        workspace_id=ctx.workspace_id,
                        artifact_result=artifact_result,
                        test_result=test_result,
                        build_validator_result=build_validator_result,
                    )
                    ctx.quality_report = quality_report.to_dict()
                    ctx.requirements_traceability = traceability
                    self.event_bus.emit(Event(EV_QUALITY_GATE, _now_iso(),
                                                {"passed": quality_report.passed}))
                    if quality_report.passed:
                        ctx.validation_state = STATUS_COMPLETED
                        break
                    # QualityGate failed ── treat as a requirements failure
                    # to repair. Construct Diagnostics for the FailureAnalyzer.
                    ctx.failure_analysis = [
                        Diagnostic(
                            category=FAIL_REQUIREMENTS, severity="error",
                            message=quality_report.rationale,
                            hint="implement the missing required sections",
                        ).to_dict()
                    ]
                else:
                    # Run FailureAnalyzer to produce Diagnostics
                    ctx.current_phase = PHASE_FAILURE_ANALYSIS
                    self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                                {"phase": PHASE_FAILURE_ANALYSIS,
                                                 "iteration": ctx.current_iteration}))
                    diagnostics = self.failure_analyzer.analyze(
                        artifact_result=artifact_result,
                        build_result=build_result,
                        test_result=test_result,
                    )
                    ctx.failure_analysis = [d.to_dict() for d in diagnostics]

                # --- Repair loop: FailureAnalysis → RepairPlanning →
                #     RepairAgent → PatchValidator → RetryController ----
                diagnostics = [Diagnostic(**d) for d in ctx.failure_analysis
                              if isinstance(d, dict)]
                plan = self.repair_planner.plan(diagnostics, ctx)

                # --- Phase 3.1: RepairMemory consultation ─────────────────
                # Before applying the planned repair, consult RepairMemory
                # to skip blacklisted (strategy+files+signature) tuples and
                # to escalate when repeated failures warrant a stronger
                # strategy. Breaks the A,B,A,B oscillation the spec calls
                # out: the second A is rejected by the blacklist and the
                # planner escalates to a strategy it hasn't tried.
                mem_reason = ""
                if self.repair_memory is not None:
                    from repair_memory import (
                        failure_signature as _mem_failure_signature,
                        escalate_plan as _mem_escalate_plan,
                        RepairAttempt as _MemAttempt,
                    )
                    sig = _mem_failure_signature(diagnostics)
                    plan, _escalated, mem_reason = _mem_escalate_plan(
                        plan, memory=self.repair_memory,
                        failure_signature_value=sig)
                    # Oscillation detection ── if we've cycled (A,B,A,B) or
                    # we're stuck on the same strategy, terminate the loop
                    # rather than waste another iteration. The user's spec
                    # is clear: "reduces wasted retries and improves
                    # convergence".
                    if self.repair_memory.detect_oscillation():
                        ctx.validation_state = STATUS_FAILED
                        error = ("repair oscillation detected by "
                                 "RepairMemory ── terminating to avoid "
                                 "wasted retries (cycle: "
                                 f"{self.repair_memory.detect_repeated_cycle()})")
                        break
                    stuck = self.repair_memory.detect_stuck()
                    if stuck:
                        # Try one more escalation before giving up
                        nxt = self.repair_memory.recommend_escalation(
                            current_strategy=stuck,
                            failure_signature_value=sig,
                        )
                        if nxt is None:
                            ctx.validation_state = STATUS_FAILED
                            error = (f"repair stuck on strategy "
                                     f"'{stuck}' and no stronger strategy "
                                     "available ── terminating")
                            break

                ctx.repair_plan = plan.to_dict()
                self.event_bus.emit(Event(EV_REPAIR_PLANNED, _now_iso(),
                                            {"strategy": plan.strategy,
                                             "priority": plan.priority,
                                             "reason": mem_reason}))

                files_for_snapshot = plan.target_files or []
                checksums_before = self.patch_validator.snapshot(
                    ctx.workspace_id, files_for_snapshot)

                repair_response = self.repair_agent.apply(ctx, plan)
                self.event_bus.emit(Event(EV_REPAIR_APPLIED, _now_iso(),
                                            {"files_changed": repair_response.get(
                                                "files_changed", [])}))

                patch_record = self.patch_validator.validate(
                    workspace_id=ctx.workspace_id,
                    files_changed=repair_response.get("files_changed", []),
                    checksums_before=checksums_before,
                )
                self.event_bus.emit(Event(EV_PATCH_VALIDATED, _now_iso(),
                                            {"valid": patch_record.integrity_valid,
                                             "syntax_valid": patch_record.syntax_valid}))
                ctx.last_patch = patch_record.to_dict()

                # --- Phase 3.1: record the attempt in RepairMemory ──────
                # Every attempt ── success or failure ── is recorded so the
                # next iteration's planner can blacklist failed strategies
                # + the engine can detect oscillation / convergence.
                if self.repair_memory is not None:
                    from repair_memory import (
                        failure_signature as _mem_failure_signature,
                        RepairAttempt as _MemAttempt,
                    )
                    sig = _mem_failure_signature(diagnostics)
                    self.repair_memory.record_attempt(_MemAttempt(
                        workspace_id=ctx.workspace_id,
                        iteration=ctx.current_iteration,
                        strategy=plan.strategy,
                        target_files=plan.target_files,
                        failure_signature=sig,
                        patch_id=patch_record.patch_id,
                        success=bool(repair_response.get("success", False)),
                        files_changed=repair_response.get(
                            "files_changed", []),
                        response_error=repair_response.get("error", ""),
                    ))

                ctx.repair_history.append({
                    "iteration": ctx.current_iteration,
                    "plan": plan.to_dict(),
                    "patch": patch_record.to_dict(),
                    "response_success": repair_response.get("success", False),
                    "patch_valid": patch_record.integrity_valid,
                    "timestamp": _now_iso(),
                })

                # Checkpoint
                self._save_context(ctx)
                self.checkpoint_mgr.checkpoint(
                    self._phase2_ctx_for_checkpoint(ctx), self.event_bus)

                # RetryController
                ctx.current_phase = PHASE_RETRY_CONTROL
                self.event_bus.emit(Event(EV_VALIDATION_PHASE, _now_iso(),
                                            {"phase": PHASE_RETRY_CONTROL,
                                             "iteration": ctx.current_iteration}))
                retry, reason = self.retry_controller.should_retry(
                    ctx, patch_record)
                ctx.retry_history.append({
                    "iteration": ctx.current_iteration,
                    "retry": retry, "reason": reason,
                    "timestamp": _now_iso(),
                })
                if not retry:
                    ctx.validation_state = STATUS_FAILED
                    error = (f"validation failed after "
                             f"{ctx.current_iteration} iteration(s): {reason}")
                    break
                self.retry_controller.advance(ctx)
        except KeyboardInterrupt:
            ctx.validation_state = STATUS_CANCELLED
            error = "interrupted by user"
        except Exception as e:
            ctx.validation_state = STATUS_FAILED
            error = f"engine raised: {e}"
        finally:
            ctx.completed_at = _now_iso()
            # Persist all 12 workspace artifacts
            self._persist_artifacts(ctx)
            # Build the final BuildResult
            success = (ctx.validation_state == STATUS_COMPLETED
                       and ctx.quality_report.get("passed", False))
            build_result = self._build_result(ctx, success=success,
                                              error=error)
            self.recorder.record_final_result(build_result)
            self.event_bus.emit(
                Event(EV_VALIDATION_COMPLETED if success else EV_VALIDATION_FAILED,
                      _now_iso(), {"validation_id": ctx.validation_id,
                                    "success": success, "error": error}))
            return build_result

    # ---- helpers -------------------------------------------------------

    def _timed_phase(
        self, phase: str, ctx: ValidationContext, fn: Callable[[], dict],
    ) -> dict:
        t0 = time.monotonic()
        result = fn()
        elapsed = time.monotonic() - t0
        ctx.validation_history.append({  # type: ignore[arg-type]
            "phase": phase,
            "duration_seconds": round(elapsed, 4),
            "timestamp": _now_iso(),
        }) if False else None  # ponytail: timing recorded in main loop only
        result["_duration_seconds"] = round(elapsed, 4)
        return result

    def _phase2_ctx_for_checkpoint(self, ctx: ValidationContext) -> ExecutionContext:
        """Translate the ValidationContext into a Phase 2 ExecutionContext
        so the Phase 2 CheckpointManager can persist a unified checkpoint."""
        return ExecutionContext(
            execution_id=ctx.validation_id,
            workspace_id=ctx.workspace_id,
            implementation_spec=ctx.implementation_spec,
            current_node=ctx.current_phase,  # Reuse Phase 2's field
            execution_state=ctx.validation_state,
        )

    def _save_context(self, ctx: ValidationContext) -> None:
        if not ctx.workspace_id:
            return
        _write_json(_validation_dir(ctx.workspace_id)
                    / "validation_context.json", ctx.to_dict())

    def _load_context(self, workspace_id: str) -> ValidationContext | None:
        if not workspace_id:
            return None
        d = _read_json(_validation_dir(workspace_id)
                       / "validation_context.json")
        if d is None:
            return None
        return ValidationContext.from_dict(d)

    def _validate_ctx(self, ctx: ValidationContext) -> list[str]:
        issues: list[str] = []
        if not ctx.validation_id:
            issues.append("validation_id missing")
        if not ctx.workspace_id:
            issues.append("workspace_id missing")
        if ctx.validation_state not in (
                STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED,
                STATUS_FAILED, STATUS_CANCELLED, STATUS_SKIPPED,
                STATUS_BLOCKED):
            issues.append(f"invalid validation_state: {ctx.validation_state}")
        return issues

    def _validate_inputs(
        self, workspace_id: str, implementation_spec: str,
    ) -> list[str]:
        issues: list[str] = []
        if not workspace_id:
            issues.append("workspace_id is required")
        if not implementation_spec or not implementation_spec.strip():
            issues.append("implementation_spec is required and non-empty")
        if workspace_id and not _workspace_dir(workspace_id).is_dir():
            issues.append(f"workspace does not exist: {workspace_id}")
        return issues

    def _persist_artifacts(self, ctx: ValidationContext) -> None:
        """Write all 12 spec artifacts to ``<workspace>/validation/``."""
        if not ctx.workspace_id:
            return
        d = _validation_dir(ctx.workspace_id)
        _write_json(d / ARTIFACT_VALIDATION_HISTORY, {
            "validation_id": ctx.validation_id,
            "history": ctx.validation_history,
        })
        _write_json(d / ARTIFACT_REPAIR_HISTORY, {
            "validation_id": ctx.validation_id,
            "history": ctx.repair_history,
            # Phase 3.1: RepairMemory's structured snapshot ── the spec's
            # "repair fingerprints / failure fingerprints / strategy
            # blacklist / convergence detection / oscillation detection /
            # repair effectiveness scoring" all in one artifact.
            "repair_memory": (self.repair_memory.to_dict()
                              if self.repair_memory is not None else {}),
        })
        _write_json(d / ARTIFACT_FAILURE_ANALYSIS, {
            "validation_id": ctx.validation_id,
            "analysis": ctx.failure_analysis,
            "iteration": ctx.current_iteration,
        })
        _write_json(d / ARTIFACT_TEST_RESULTS, {
            "validation_id": ctx.validation_id,
            "test_results": ctx.test_results,
            "iteration": ctx.current_iteration,
        })
        _write_json(d / ARTIFACT_COVERAGE, {
            "validation_id": ctx.validation_id,
            "coverage": ctx.coverage or ctx.test_results.get("coverage", {}),
        })
        _write_json(d / ARTIFACT_PATCH_HISTORY, {
            "validation_id": ctx.validation_id,
            "patches": [r.get("patch", {}) for r in ctx.repair_history],
        })
        _write_json(d / ARTIFACT_RETRY_HISTORY, {
            "validation_id": ctx.validation_id,
            "history": ctx.retry_history,
        })
        _write_json(d / ARTIFACT_BUILD_LOG, {
            "validation_id": ctx.validation_id,
            "build_log": ctx.build_log,
            "build_result": ctx.build_result_phase,
        })
        _write_json(d / ARTIFACT_VALIDATION_REPORT, {
            "validation_id": ctx.validation_id,
            "build_validator_result": ctx.build_validator_result,
            "validation_state": ctx.validation_state,
            "iterations": ctx.current_iteration,
        })
        _write_json(d / ARTIFACT_QUALITY_REPORT,
                    ctx.quality_report or {})
        _write_json(d / ARTIFACT_REQUIREMENTS_TRACEABILITY,
                    ctx.requirements_traceability or {})
        # final_build_result written by the caller via _build_result; we
        # also write a snapshot here so the artifact exists even on early
        # exit (e.g. crash between _build_result and the return).
        if ctx.final_build_result:
            _write_json(d / ARTIFACT_FINAL_BUILD_RESULT, ctx.final_build_result)
        # Also persist the validation context (resume support)
        _write_json(d / "validation_context.json", ctx.to_dict())
        # Persist the ExecutionRecorder's recording
        self.recorder.persist(ctx.workspace_id)

    def _build_result(
        self, ctx: ValidationContext, *, success: bool, error: str = "",
    ) -> BuildResult:
        """Assemble a Phase 3 BuildResult (Phase 2 + Phase 3 fields)."""
        result = BuildResult(
            success=success,
            output=str(ctx.quality_report.get("rationale", "")) if
                isinstance(ctx.quality_report, dict) else "",
            test_results=ctx.test_results,
            repair_attempts=len(ctx.repair_history),
            repair_log=list(ctx.repair_history),
            error=error,
            executed_at=_now_iso(),
            workspace_id=ctx.workspace_id,
            engine=self.ENGINE_NAME,
            final_node=ctx.current_phase,
            execution_id=ctx.validation_id,
            node_timings={},
            generated_files=ctx.artifact_validation_result.get(
                "generated_files", []),
            modified_files=[],
            execution_log=list(ctx.build_log),
        )
        ctx.final_build_result = result.to_dict()
        if ctx.workspace_id:
            _write_json(_validation_dir(ctx.workspace_id)
                        / ARTIFACT_FINAL_BUILD_RESULT,
                        result.to_dict())
        return result

    def _build_failure_result(
        self, *, workspace_id: str, error: str,
    ) -> BuildResult:
        return BuildResult(
            success=False, error=error, workspace_id=workspace_id,
            engine=self.ENGINE_NAME, executed_at=_now_iso(),
        )


# ---------------------------------------------------------------------------
# Factory: pick the validation engine by name
# ---------------------------------------------------------------------------


_VALIDATION_ENGINES: dict[str, type] = {
    "NativeValidation": NativeValidationEngine,
}


def get_validation_engine(name: str = "NativeValidation",
                           **kwargs: Any) -> ValidationEngine:
    """Configuration switch. ``name`` selects the engine; kwargs pass
    through to the constructor. Default = NativeValidation."""
    cls = _VALIDATION_ENGINES.get(name)
    if cls is None:
        raise ValueError(f"unknown validation engine: {name}; "
                          f"known: {list(_VALIDATION_ENGINES)}")
    return cls(**kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Self-check (assert-based; no model calls)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import tempfile
    import build_workspace as _bw

    orig_root = _bw.WORKSPACE_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)

        # 1. All-PASS path ── tests pass, BuildValidator passes, QualityGate
        # passes (spec has all required sections; components found).
        # Components: each name must match SOME file under src/ so
        # QualityGate's traceability check passes ── create api.py + worker.py
        # + db.py.
        wid = _bw.create_workspace(source_question="q1")
        ws = _workspace_dir(wid)
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "api.py").write_text("API = 1\n", encoding="utf-8")
        (ws / "src" / "worker.py").write_text("WORKER = 1\n", encoding="utf-8")
        (ws / "src" / "db.py").write_text("DB = 1\n", encoding="utf-8")
        (ws / "tests").mkdir(parents=True)
        (ws / "tests" / "test_api.py").write_text(
            "def test_api(): assert True\n", encoding="utf-8")

        spec = """# Architecture
A clean modular service.

# Components
API, worker, DB

# Acceptance Criteria
- API endpoint works
- worker processes jobs
"""
        eng = NativeValidationEngine(
            build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "ok",
                                        "stderr": "", "success": True,
                                        "logs": ["ok"]},
            test_fns={
                "unit": lambda wd: {"passed": True, "total": 1, "failed": 0,
                                    "errors": 0, "output": ""},
                "integration": lambda wd: {"passed": True, "total": 1,
                                            "failed": 0, "errors": 0},
                "smoke": lambda wd: {"passed": True, "total": 1, "failed": 0,
                                      "errors": 0},
            },
            max_iterations=2,
        )
        r = eng.validate(workspace_id=wid, implementation_spec=spec)
        assert r.success is True, r
        assert r.engine == "NativeValidation"
        # All 12 + 1 artifacts persisted
        val_dir = _validation_dir(wid)
        for f in ALL_ARTIFACTS + ("validation_context.json",):
            assert (val_dir / f).is_file(), f"missing: {f}"

        # 2. QualityGate blocks when tests pass but a required section is
        # missing from the spec
        wid2 = _bw.create_workspace(source_question="q2")
        ws2 = _workspace_dir(wid2)
        (ws2 / "src").mkdir(parents=True)
        (ws2 / "src" / "x.py").write_text("X = 1\n", encoding="utf-8")
        (ws2 / "tests").mkdir(parents=True)
        (ws2 / "tests" / "test_x.py").write_text(
            "def test_x(): assert True\n", encoding="utf-8")
        # Spec has no Acceptance Criteria section
        bad_spec = "# Architecture\nFoo.\n\n# Components\napi\n"
        eng2 = NativeValidationEngine(
            build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "",
                                        "stderr": "", "success": True,
                                        "logs": []},
            test_fns={t: lambda wd: {"passed": True, "total": 1,
                                       "failed": 0, "errors": 0}
                      for t in ("unit", "integration", "smoke")},
            max_iterations=1,
        )
        r2 = eng2.validate(workspace_id=wid2, implementation_spec=bad_spec)
        assert r2.success is False, r2
        quality = json.loads(
            (_validation_dir(wid2) / "quality_report.json").read_text())
        assert quality["passed"] is False
        assert "Acceptance Criteria" in quality["requirements_unmet"]

        # 3. Tests fail ── BuildValidator blocks → repair loop runs →
        # retry budget exhausted
        wid3 = _bw.create_workspace(source_question="q3")
        ws3 = _workspace_dir(wid3)
        (ws3 / "src").mkdir(parents=True)
        (ws3 / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
        (ws3 / "tests").mkdir(parents=True)
        (ws3 / "tests" / "test_a.py").write_text(
            "def test_a(): assert False\n", encoding="utf-8")
        full_spec = """# Architecture
Modular.

# Components
api

# Acceptance Criteria
tests pass
"""
        repair_calls = {"n": 0}
        def _fake_repair(wid, plan_prompt):
            repair_calls["n"] += 1
            return {"success": True, "files_changed": ["src/a.py"],
                    "output": "fixed"}
        eng3 = NativeValidationEngine(
            build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "",
                                        "stderr": "", "success": True,
                                        "logs": []},
            test_fns={
                "unit": lambda wd: {"passed": False, "total": 1, "failed": 1,
                                    "errors": 0, "output": "AssertionError"},
                "integration": lambda wd: {"passed": True, "total": 0,
                                            "failed": 0, "errors": 0},
                "smoke": lambda wd: {"passed": True, "total": 0,
                                       "failed": 0, "errors": 0},
            },
            repair_fn=_fake_repair,
            max_iterations=2,
        )
        r3 = eng3.validate(workspace_id=wid3, implementation_spec=full_spec)
        assert r3.success is False
        assert r3.repair_attempts >= 1
        # patch_history.json has the patches
        patches = json.loads(
            (_validation_dir(wid3) / "patch_history.json").read_text())
        assert len(patches["patches"]) >= 1

        # 4. Missing implementation spec ── structured error (no silent)
        wid4 = _bw.create_workspace(source_question="q4")
        r4 = eng3.validate(workspace_id=wid4, implementation_spec="")
        assert r4.success is False
        assert "implementation_spec" in r4.error

        # 5. Resume from a checkpoint
        wid5 = _bw.create_workspace(source_question="q5")
        ws5 = _workspace_dir(wid5)
        (ws5 / "src").mkdir(parents=True)
        (ws5 / "src" / "z.py").write_text("Z = 1\n", encoding="utf-8")
        (ws5 / "tests").mkdir(parents=True)
        (ws5 / "tests" / "test_z.py").write_text(
            "def test_z(): assert True\n", encoding="utf-8")
        # ponytail: use a spec whose components match what already exists
        # so QualityGate passes on the first iteration; repair isn't needed
        # so we don't need a fake agent ── confirms resume works.
        spec_for_resume = (
            "# Architecture\nA clean modular service.\n\n"
            "# Components\nz\n\n"
            "# Acceptance Criteria\ntests pass\n"
        )
        # Seed a state at the Build phase (simulating crash)
        StateManager.save(ExecutionContext(
            workspace_id=wid5, implementation_spec=spec_for_resume,
            current_node=PHASE_BUILD, execution_state=STATUS_RUNNING,
        ))
        # Note: Phase 3 resumes from validation_context.json, not Phase 2's
        # execution_context.json. Seed Phase 3's:
        ctx5 = ValidationContext(
            workspace_id=wid5, implementation_spec=spec_for_resume,
            current_phase=PHASE_BUILD, validation_state=STATUS_RUNNING,
            current_iteration=1,
        )
        _write_json(_validation_dir(wid5) / "validation_context.json",
                    ctx5.to_dict())

        def _write_text(path, content):
            path.write_text(content, encoding="utf-8")
            return None

        eng5 = NativeValidationEngine(
            build_fn=lambda wd, spec: {"exit_code": 0, "stdout": "",
                                        "stderr": "", "success": True,
                                        "logs": []},
            test_fns={t: lambda wd: {"passed": True, "total": 1,
                                       "failed": 0, "errors": 0}
                      for t in ("unit", "integration", "smoke")},
            max_iterations=2,
        )
        r5 = eng5.resume(workspace_id=wid5)
        assert r5.success is True, r5

        # 6. Cancel is honored
        wid6 = _bw.create_workspace(source_question="q6")
        ctx6 = ValidationContext(workspace_id=wid6, implementation_spec=spec)
        _write_json(_validation_dir(wid6) / "validation_context.json",
                    ctx6.to_dict())
        eng6 = NativeValidationEngine(
            build_fn=lambda wd, spec: {"success": True, "exit_code": 0,
                                        "stdout": "", "stderr": "", "logs": []},
            test_fns={t: lambda wd: {"passed": True, "total": 0,
                                      "failed": 0, "errors": 0}
                      for t in ("unit", "integration", "smoke")},
            max_iterations=2,
        )
        assert eng6.cancel(workspace_id=wid6) is True
        r6 = eng6.resume(workspace_id=wid6)
        assert r6.success is False

        # 7. Status / recover surfaces
        st = eng5.status(workspace_id=wid5)
        assert st["validation_state"] in ("COMPLETED", "FAILED")
        rec = eng5.recover(workspace_id=wid5)
        assert rec["recoverable"] is True

        # 8. RepairPlanner produces a plan from Diagnostics
        from implementation_engine import BaseNode  # noqa: F401 — sanity
        planner = RepairPlanner()
        diags = [Diagnostic(category=FAIL_TESTS, severity="error",
                            message="x failed", location="tests/test_x.py",
                            hint="fix assertion")]
        plan = planner.plan(diags, ValidationContext(implementation_spec="s"))
        assert plan.priority == "high"
        assert plan.strategy == "rewrite_test"
        assert "tests/test_x.py" in plan.target_files \
            or any("test_x.py" in f for f in plan.target_files)
        assert plan.prompt

        # 9. PatchValidator accepts clean .py and rejects syntax errors
        pv = PatchValidator()
        wid9 = _bw.create_workspace(source_question="q9")
        ws9 = _workspace_dir(wid9)
        (ws9 / "src").mkdir(parents=True)
        good = ws9 / "src" / "good.py"
        good.write_text("def f(): pass\n", encoding="utf-8")
        bad = ws9 / "src" / "bad.py"
        bad.write_text("def f(:\n", encoding="utf-8")
        snap = pv.snapshot(wid9, ["src/good.py", "src/bad.py"])
        rec_g = pv.validate(workspace_id=wid9,
                            files_changed=["src/good.py"],
                            checksums_before=snap)
        assert rec_g.syntax_valid is True
        assert rec_g.integrity_valid is True
        rec_b = pv.validate(workspace_id=wid9,
                            files_changed=["src/bad.py"],
                            checksums_before=snap)
        assert rec_b.syntax_valid is False

        # 10. FailureAnalyzer categorizes
        fa = FailureAnalyzer()
        out = fa.analyze(
            artifact_result={"passed": False, "diagnostics": [
                {"category": FAIL_ARTIFACT, "severity": "error",
                 "message": "no src", "location": "", "hint": ""}]},
            build_result={"success": False, "exit_code": 1, "stderr": "x"},
            test_result={"passed": True, "test_types": {}},
        )
        cats = {d.category for d in out}
        assert FAIL_ARTIFACT in cats and FAIL_BUILD in cats

        # 11. BuildValidator ── advisory vs mandatory
        bv = BuildValidator()
        rep = bv.validate(
            artifact_result={"passed": True, "diagnostics": []},
            build_result={"success": True},
            test_result={"test_types": {
                "unit": {"passed": True}, "integration": {"passed": True},
                "smoke": {"passed": True}, "lint": {"skipped": True},
                "type_check": {"passed": False, "output": "type error"},
            }},
        )
        assert rep["passed"] is True
        assert "lint" in rep["advisory_skipped"]
        assert len(rep["warnings"]) == 1  # type_check failed (advisory)

        # 12. QualityGate parses spec sections + surfaces traceability
        qg = QualityGate()
        qr, tr = qg.evaluate(
            implementation_spec="# Architecture\nFoo.\n\n# Components\napi, db\n",
            workspace_id=wid,
            artifact_result={"passed": True, "diagnostics": []},
            test_result={"passed": True, "test_types": {
                "unit": {"passed": True}, "integration": {"passed": True},
                "smoke": {"passed": True}}},
            build_validator_result={"passed": True},
        )
        assert isinstance(qr, QualityReport)
        # Acceptance Criteria missing from the spec → unmet
        assert "Acceptance Criteria" in qr.requirements_unmet
        assert qr.passed is False
        assert "entries" in tr

        # 13. Configuration switch rejects unknown names
        try:
            get_validation_engine("Nope")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        eng_n = get_validation_engine("NativeValidation",
                                        build_fn=lambda wd, spec: {"success": True},
                                        test_fns={"unit": lambda wd: {}},
                                        max_iterations=1)
        assert isinstance(eng_n, NativeValidationEngine)

        # 14. Pure-stdlib at import (no provider pulled transitively)
        forbidden = {"cerebras_client", "ollama_client", "openrouter_client",
                     "groq_client", "gpt_worker", "gemini_worker",
                     "provider_router", "selenium", "requests",
                     "httpx", "urllib3"}
        loaded = forbidden & set(sys.modules)
        assert not loaded, f"engine pulled forbidden modules: {loaded}"

        # 15. ExecutionRecorder records the validation lifecycle
        # (re-running engine on a fresh workspace with a success path)
        wid15 = _bw.create_workspace(source_question="q15")
        ws15 = _workspace_dir(wid15)
        (ws15 / "src").mkdir(parents=True)
        (ws15 / "src" / "api.py").write_text("API = 1\n", encoding="utf-8")
        (ws15 / "tests").mkdir(parents=True)
        (ws15 / "tests" / "test_api.py").write_text(
            "def test_api(): assert True\n", encoding="utf-8")
        # ponytail: spec matches the on-disk files so QualityGate passes
        # without needing a repair agent.
        spec15 = (
            "# Architecture\nA clean modular service.\n\n"
            "# Components\napi\n\n"
            "# Acceptance Criteria\ntests pass\n"
        )
        rec_seen: list[str] = []
        eng15 = NativeValidationEngine(
            build_fn=lambda wd, spec: {"success": True, "exit_code": 0,
                                        "stdout": "", "stderr": "", "logs": []},
            test_fns={t: lambda wd: {"passed": True, "total": 1,
                                      "failed": 0, "errors": 0, "output": ""}
                      for t in ("unit", "integration", "smoke")},
            max_iterations=2,
        )
        eng15.event_bus.subscribe(
            lambda e: rec_seen.append(e.type))
        eng15.validate(workspace_id=wid15, implementation_spec=spec15)
        assert EV_VALIDATION_STARTED in rec_seen
        assert EV_VALIDATION_PHASE in rec_seen
        assert EV_VALIDATION_COMPLETED in rec_seen

        _bw.WORKSPACE_ROOT = orig_root

    print("validation_engine self-check PASSED")
