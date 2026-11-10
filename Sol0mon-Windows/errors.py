"""
Project Sol0mon - Error Taxonomy (Final Architecture Hardening - Phase J).

Ponytail: one class + a ``kind`` discriminator instead of a 7-class hierarchy
nobody catches. The codebase signals failures by return-dicts, not exceptions,
so a deep hierarchy would be dead weight. ``BuildError`` is raised only where
an exception is the right tool:
  - the lifecycle transition guard rejects an illegal state change
  - the validation dispatcher is asked to halt on a failed validation
Both give ``BuildError`` real catchers, so the metadata it carries is actually
read.

Every ``BuildError`` carries the Phase J contract:
  stage            — lifecycle stage / pipeline phase that raised
  artifact_id      — id of the artifact involved (or None)
  severity         — ERROR / FATAL
  recoverable      — can a resume retry fix this?
  suggested_repair — one-line human-actionable fix

``ErrorKind`` enumerates the named categories from the spec
(DecisionError, ValidationError, ArtifactError, PlanningError, ExecutionError,
TelemetryError, RepairError) as a discriminator on the single class — cheaper
than seven subclasses with identical fields and identical handlers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class ErrorKind(str, enum.Enum):
    DECISION = "DecisionError"
    VALIDATION = "ValidationError"
    ARTIFACT = "ArtifactError"
    PLANNING = "PlanningError"
    EXECUTION = "ExecutionError"
    TELEMETRY = "TelemetryError"
    REPAIR = "RepairError"
    LIFECYCLE = "LifecycleError"


class Severity(str, enum.Enum):
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


@dataclass
class BuildError(Exception):
    """Single error type for the build platform. Carries the Phase J contract.

    ``kind`` discriminates the named categories from the spec without the
    seven-subclass tax. ``recoverable=True`` means a ``resume_build`` retry can
    plausibly clear the failure (e.g. transient agent failure, pending human
    decision); ``False`` means the failure is structural (illegal lifecycle
    transition, validation that cannot be auto-repaired).
    """

    kind: ErrorKind
    stage: str
    message: str
    artifact_id: str | None = None
    severity: Severity = Severity.ERROR
    recoverable: bool = True
    suggested_repair: str = ""

    def __post_init__(self) -> None:
        # Populate Exception's args so str(self) is the message, not a
        # dataclass repr (keeps `raise BuildError(...)` printing cleanly).
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "stage": self.stage,
            "message": self.message,
            "artifact_id": self.artifact_id,
            "severity": self.severity.value,
            "recoverable": self.recoverable,
            "suggested_repair": self.suggested_repair,
        }


def illegal_transition(current: str, target: str, stage: str = "lifecycle") -> BuildError:
    """Factory for the lifecycle guard. Structural failure — never recoverable
    by a retry; the caller must correct the transition request."""
    return BuildError(
        kind=ErrorKind.LIFECYCLE,
        stage=stage,
        message=f"Illegal lifecycle transition: {current} -> {target}",
        severity=Severity.FATAL,
        recoverable=False,
        suggested_repair=(
            f"Transition {current} -> {target} is not in the BUILD_LIFECYCLE "
            "transition table. Correct the calling stage or extend the table."
        ),
    )


def validation_failed(report: Any, stage: str) -> BuildError:
    """Factory for the validation dispatcher when ``halt_on_failure=True``.
    Recoverable — a repair or human fix can clear it; resume can retry."""
    suggested = ""
    try:
        issues = getattr(report, "issues", []) or []
        if issues:
            suggested = issues[0].suggested_repair or ""
    except Exception:
        pass
    return BuildError(
        kind=ErrorKind.VALIDATION,
        stage=stage,
        message=f"Validation failed at stage '{stage}'",
        artifact_id=getattr(report, "artifact_id", None),
        severity=Severity.ERROR,
        recoverable=True,
        suggested_repair=suggested or "Run the named validator; fix the first issue.",
    )


if __name__ == "__main__":
    # ponytail self-check: the only thing that breaks if the contract regresses.
    e = BuildError(
        kind=ErrorKind.VALIDATION, stage="build_spec",
        message="missing sections", artifact_id="art_1",
        severity=Severity.ERROR, recoverable=True,
        suggested_repair="regenerate spec via build_spec()",
    )
    assert e.kind is ErrorKind.VALIDATION
    assert e.to_dict()["recoverable"] is True
    assert str(e) == "missing sections", str(e)

    it = illegal_transition("COMPLETE", "EXECUTING")
    assert it.recoverable is False and it.kind is ErrorKind.LIFECYCLE
    raised = False
    try:
        raise it
    except BuildError as caught:
        raised = True
        assert caught.kind is ErrorKind.LIFECYCLE
    assert raised
    print("errors smoke test PASSED")
