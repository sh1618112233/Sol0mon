"""
Project Sol0mon — Stage Logger.

Explicit, stage-tagged logging for the orchestrated pipeline:

    [PLANNER] [GRAPH] [SCHEDULER] [RESEARCH] [ARCHITECTURE]
    [IMPLEMENTATION] [BUILD] [TEST] [REPAIR]

Every stage reports, on completion:
    - Start timestamp
    - Completion timestamp
    - Duration (seconds, 2 decimals)
    - Output artifact (path or label), when one was produced

Reuses the existing ``telemetry`` recorder (observational) for the
performance report; the stage-tagged lines are the user-facing surface.
Ponytail: a thin wrapper, not a parallel logging system. ``telemetry`` stays
the single source of truth for machine-readable observability; this is the
human-readable companion.

Usage::

    with stage_logger.stage("PLANNER", workspace_id=wid) as s:
        graph = plan_objective(...)
        s.artifact = str(graph_path)
    # logs: [PLANNER] start ...
    # logs: [PLANNER] complete (1.23s) -> /path/to/execution_graph.json

The context manager returns a ``Stage`` instance whose ``.elapsed``,
``.status``, ``.artifact`` fields are populated on exit. Failures inside the
``with`` block are logged as ``[STAGE] FAILED (<error>)`` and re-raised.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator


# Canonical stage names (the spec's exact list). Used as the bracketed tag.
STAGES = (
    "PLANNER",
    "GRAPH",
    "SCHEDULER",
    "RESEARCH",
    "ARCHITECTURE",
    "IMPLEMENTATION",
    "BUILD",
    "TEST",
    "REPAIR",
)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(line: str) -> None:
    # Reuse the existing pipeline_runner.log style so the user sees one
    # consistent timestamped stream (not two interleaved formats).
    try:
        from pipeline_runner import log as _plog
        _plog(line)
    except Exception:
        # Defensive: if pipeline_runner is unavailable (e.g. test imports this
        # module in isolation), still emit a timestamped line.
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {line}", flush=True)


@dataclass
class Stage:
    """One stage's run record. Populated by the ``stage`` context manager."""

    name: str
    workspace_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    elapsed: float = 0.0
    status: str = "running"
    artifact: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "workspace_id": self.workspace_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_s": round(self.elapsed, 3),
            "status": self.status,
            "artifact": self.artifact,
            "error": self.error,
        }


@contextmanager
def stage(
    name: str,
    *,
    workspace_id: str = "",
) -> Iterator[Stage]:
    """Wrap a stage with start/complete logging.

    Logs ``[NAME] start`` on entry (with workspace_id when supplied). On
    normal exit, logs ``[NAME] complete (X.XXs) -> <artifact>``. On
    exception, logs ``[NAME] FAILED (<err>)`` and re-raises.

    Also pokes the existing ``telemetry`` recorder so the stage lands in the
    performance report (the recorder is a no-op when unconfigured, so this
    is safe to call from tests that never configure telemetry)."""
    s = Stage(name=name, workspace_id=workspace_id, started_at=_ts())
    tag = f"[{name}]"
    ws_tag = f" ws={workspace_id[:8]}" if workspace_id else ""
    _log(f"{tag} start{ws_tag}")
    try:
        from telemetry import recorder as _telemetry
        _telemetry.start_stage(name)
    except Exception:
        pass

    t0 = time.time()
    try:
        yield s
    except Exception as e:
        s.elapsed = time.time() - t0
        s.completed_at = _ts()
        s.status = "failed"
        s.error = str(e)
        _log(f"{tag} FAILED ({s.elapsed:.2f}s) {e}")
        try:
            from telemetry import recorder as _telemetry
            _telemetry.end_stage(name, status="failed")
        except Exception:
            pass
        raise
    else:
        s.elapsed = time.time() - t0
        s.completed_at = _ts()
        s.status = "complete"
        art_part = f" -> {s.artifact}" if s.artifact else ""
        _log(f"{tag} complete ({s.elapsed:.2f}s){art_part}")
        try:
            from telemetry import recorder as _telemetry
            _telemetry.end_stage(name, status="complete")
        except Exception:
            pass


def event(stage_name: str, message: str, *, artifact: str = "") -> None:
    """One-shot informational log inside a stage (no timingWrap).

    Use for sub-events that are not themselves stages:
    ``[SCHEDULER] dispatching 5 work packages``. Pure log; no telemetry call.
    """
    art_part = f" -> {artifact}" if artifact else ""
    _log(f"[{stage_name}] {message}{art_part}")


if __name__ == "__main__":
    # ponytail self-check: start/complete timing, artifact, failure path.
    with stage("PLANNER", workspace_id="ws-test-1234") as s:
        time.sleep(0.01)
        s.artifact = "/tmp/execution_graph.json"
    assert s.status == "complete"
    assert s.elapsed >= 0.01
    assert s.artifact == "/tmp/execution_graph.json"
    assert "PLANNER" in s.to_dict()["name"]

    # Failure path re-raises and logs FAILED.
    saw_failure = False
    try:
        with stage("TEST") as s:
            raise RuntimeError("tests blew up")
    except RuntimeError:
        saw_failure = True
    assert saw_failure
    assert s.status == "failed"
    assert "tests blew up" in s.error

    # event() one-shot info log
    event("SCHEDULER", "dispatching 3 work packages", artifact="scheduler_state.json")

    print("stage_logger self-check PASSED")
