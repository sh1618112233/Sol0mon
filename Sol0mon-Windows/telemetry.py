"""
Project Sol0mon — Telemetry & Runtime Observability V1 (telemetry).

A passive observability layer. Telemetry is OBSERVATIONAL ONLY: it records
what the pipeline does and estimates remaining work. It never changes debate
logic, verification, consensus, research, execution decisions, architecture
classification, human synthesis, the repair loop, the implementation executor,
or any provider behavior. The DecisionEngine / VerificationEngine / etc. are
touched only to ADD observation calls (``recorder.observe`` / ``recorder.emit``)
that are strict no-ops when the recorder is not configured — so existing tests
and the live path are byte-for-byte unchanged when no recorder is active.

Design:
  - ``recorder`` is a process-level singleton. It is DISABLED until
    ``configure()`` is called, after which every ``emit``/``observe``/
    ``start_stage``/``end_stage`` records state and writes
    ``workspace_status.json`` (resume-safe: ``configure(resume=True)`` reads
    an existing status file to restore ``pipeline_started_at`` and live
    fields, so a resumed run keeps its original elapsed clock).
  - All public methods swallow their own errors (telemetry must NEVER crash
    the pipeline) and are no-ops when disabled.
  - Events are immutable frozen dataclasses (``TelemetryEvent``) serialized
    to JSON-able dicts for persistence.

Stage taxonomy (matches the spec's events):
    Pipeline (Started/Completed)
    Research (Started/Completed)
    Debate (Started/Completed)
    Round (Completed)
    Dispute (Resolved)            -- one per dispute reaching a terminal state
    Verification (Started/Completed)
    Architecture (Completed)
    HumanSynthesis (Completed)
    Build (Started/Completed)
    BuildSpec (Started/Completed)
    Execution (Started/Completed) -- wraps the executor (+ repair loop)
    Repair (Started/Completed)

Performance report (``performance_report.json``) keys:
    research_time, debate_time, verification_time, architecture_time,
    human_synthesis_time, build_time, repair_time, total_runtime,
    disputes_resolved, average_round_duration, average_verification_duration.

Pure stdlib at import: ``json``, ``os``, ``time``, ``uuid``, ``statistics``,
``dataclasses``. No provider / browser / network client.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Stage + event-type constants
# ---------------------------------------------------------------------------

STAGE_PIPELINE = "Pipeline"
STAGE_RESEARCH = "Research"
STAGE_DEBATE = "Debate"
STAGE_ROUND = "Round"
STAGE_DISPUTE = "Dispute"
STAGE_VERIFICATION = "Verification"
STAGE_ARCHITECTURE = "Architecture"
STAGE_HUMAN_SYNTHESIS = "HumanSynthesis"
STAGE_BUILD = "Build"
STAGE_BUILD_SPEC = "BuildSpec"
STAGE_EXECUTION = "Execution"
STAGE_REPAIR = "Repair"

EVENT_STARTED = "Started"
EVENT_COMPLETED = "Completed"
EVENT_ROUND_COMPLETED = "RoundCompleted"
EVENT_DISPUTE_RESOLVED = "DisputeResolved"

# Stages whose durations are reported as top-level time buckets.
TIME_BUCKETS = (
    STAGE_RESEARCH, STAGE_DEBATE, STAGE_VERIFICATION, STAGE_ARCHITECTURE,
    STAGE_HUMAN_SYNTHESIS, STAGE_BUILD, STAGE_REPAIR,
)


# ---------------------------------------------------------------------------
# Immutable event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelemetryEvent:
    """A single runtime observation. Frozen so a recorded event can never be
    mutated after the fact."""

    timestamp: float
    event_type: str
    stage: str
    duration: float = 0.0
    job_id: str = ""
    workspace_id: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    # ponytail: stdlib ISO format; no datetime import needed beyond what
    # dataclasses already pull. Cheap and good enough for a status file.
    import datetime
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class Recorder:
    """Process-level telemetry recorder. Disabled by default; a no-op until
    ``configure()`` is called. Every method is safe to call when disabled
    (returns None / empty dict). Every method swallows its own errors so a
    telemetry fault can never propagate into the pipeline."""

    def __init__(self) -> None:
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Return to the disabled, empty state. Safe to call repeatedly."""
        self._enabled = False
        self._job_id = ""
        self._workspace_id = ""
        self._status_path: str | None = None
        self._report_path: str | None = None
        self._pipeline_started_at: float = 0.0
        self._events: list[TelemetryEvent] = []
        self._stage_starts: dict[str, float] = {}
        self._live: dict[str, Any] = {
            "stage": "",
            "current_round": 0,
            "active_dispute": "",
            "resolved_disputes": 0,
            "remaining_disputes": 0,
            "total_disputes": 0,
            "consensus": 0,
            "research_requests": 0,
            "verification_runs": 0,
            "execution_mode": "",
        }

    def configure(
        self,
        job_id: str | None = None,
        workspace_id: str = "",
        status_path: str | None = None,
        report_path: str | None = None,
        resume: bool = True,
    ) -> "Recorder":
        """Enable the recorder for a run. Generates a job_id when none is
        supplied. When ``resume`` is True and ``status_path`` exists, the
        prior ``pipeline_started_at`` and live status fields are restored so
        a resumed run keeps its original elapsed clock — Phase 4 resume-safety.
        Returns self for chaining."""
        self.reset()
        self._enabled = True
        self._job_id = job_id or str(uuid.uuid4())
        self._workspace_id = workspace_id or ""
        self._status_path = status_path
        self._report_path = report_path
        self._pipeline_started_at = _now()
        if resume and status_path and os.path.isfile(status_path):
            self._load_state(status_path)
        self._live.setdefault("job_id", self._job_id)
        self._live.setdefault("workspace_id", self._workspace_id)
        self.emit(EVENT_STARTED, STAGE_PIPELINE)
        self.save_status()
        return self

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def job_id(self) -> str:
        return self._job_id

    # -- core: emit + stage timing ----------------------------------------

    def emit(
        self,
        event_type: str,
        stage: str,
        duration: float = 0.0,
        **metadata: Any,
    ) -> TelemetryEvent | None:
        """Append an event. No-op when disabled. Returns the event (or None
        when disabled) so callers that want the timestamp can use it."""
        if not self._enabled:
            return None
        try:
            ev = TelemetryEvent(
                timestamp=_now(),
                event_type=event_type,
                stage=stage,
                duration=float(duration or 0.0),
                job_id=self._job_id,
                workspace_id=self._workspace_id,
                metadata=dict(metadata),
            )
            self._events.append(ev)
            self._reduce_event(ev)
            self.save_status()
            return ev
        except Exception:
            # Telemetry must never crash the pipeline (Phase 4 contract).
            return None

    def start_stage(self, stage: str, **metadata: Any) -> None:
        """Record a stage start. Pair with ``end_stage`` to emit a
        ``Completed`` event carrying the elapsed duration."""
        if not self._enabled:
            return
        try:
            self._stage_starts[stage] = _now()
            self._live["stage"] = stage
            self.emit(EVENT_STARTED, stage, **metadata)
        except Exception:
            return

    def end_stage(self, stage: str, **metadata: Any) -> float:
        """Emit ``<stage>/Completed`` with the elapsed duration since the
        matching ``start_stage`` call. Returns the duration (0 when disabled
        or no matching start)."""
        if not self._enabled:
            return 0.0
        try:
            start = self._stage_starts.pop(stage, None)
            duration = 0.0
            if start is not None:
                duration = max(0.0, _now() - start)
            self.emit(EVENT_COMPLETED, stage, duration=duration, **metadata)
            return duration
        except Exception:
            return 0.0

    def observe(self, **fields: Any) -> None:
        """Update live status fields (current_round, active_dispute,
        consensus, ...) without emitting a timed event. No-op when disabled."""
        if not self._enabled:
            return
        try:
            self._live.update(fields)
            self.save_status()
        except Exception:
            return

    # -- reduction: keep live counters from the event stream ------------

    def _reduce_event(self, ev: TelemetryEvent) -> None:
        """Fold an event into the live status dict. Keeps resolved/remaining
        dispute counts, research/verification counts, and the consensus score
        in sync without the instrumented call sites having to manage them."""
        try:
            m = ev.metadata or {}
            if ev.stage == STAGE_DISPUTE and ev.event_type == EVENT_DISPUTE_RESOLVED:
                self._live["resolved_disputes"] = int(
                    self._live.get("resolved_disputes", 0)) + 1
                if "remaining" in m:
                    self._live["remaining_disputes"] = int(m["remaining"])
                if "total" in m:
                    self._live["total_disputes"] = int(m["total"])
                # Keep the last-known active_dispute — the Phase-4 example
                # shows active_dispute populated alongside resolved_disputes,
                # and the debate loop's next observe() overwrites it when the
                # next dispute starts. Clearing here would lose the last
                # value before the CLI status snapshot.
            elif ev.stage == STAGE_ROUND and ev.event_type == EVENT_ROUND_COMPLETED:
                if "round" in m:
                    self._live["current_round"] = int(m["round"])
                if "dispute" in m:
                    self._live["active_dispute"] = str(m["dispute"])
                if "consensus" in m:
                    try:
                        self._live["consensus"] = int(m["consensus"])
                    except (TypeError, ValueError):
                        pass
            elif ev.stage == STAGE_RESEARCH and ev.event_type == EVENT_STARTED:
                self._live["research_requests"] = int(
                    self._live.get("research_requests", 0)) + 1
            elif ev.stage == STAGE_VERIFICATION and ev.event_type == EVENT_STARTED:
                self._live["verification_runs"] = int(
                    self._live.get("verification_runs", 0)) + 1
            elif ev.stage == STAGE_PIPELINE and ev.event_type == EVENT_COMPLETED:
                self._live["stage"] = "Completed"
            else:
                self._live["stage"] = ev.stage
        except Exception:
            return

    # -- status + ETA -----------------------------------------------------

    def status(self) -> dict:
        """Return the Phase-4 status dict. ``elapsed_seconds`` is computed
        from ``pipeline_started_at``. Always returns a dict (never raises)."""
        if not self._enabled:
            return {}
        try:
            elapsed = max(
                0.0,
                _now() - self._pipeline_started_at,
            ) if self._pipeline_started_at else 0.0
            s = dict(self._live)
            s.update({
                "job_id": self._job_id,
                "workspace_id": self._workspace_id,
                "elapsed_seconds": round(elapsed, 3),
                "updated_at": _iso(_now()),
            })
            return s
        except Exception:
            return {}

    def eta(self) -> dict:
        """Estimate remaining work (Phase 5). NEVER raises — returns an
        estimate with ``estimate_minutes`` (float or None) and a clearly
        marked ``human_readable`` string. The estimate is derived from the
        observed event stream (average round / dispute / verification
        durations × remaining counts)."""
        empty = {
            "estimate_minutes": None,
            "human_readable": "Estimate unavailable (pipeline not running "
                              "or insufficient runtime data).",
            "basis": "no_data",
            "confidence": "none",
        }
        if not self._enabled:
            return empty
        try:
            round_durations = [
                e.duration for e in self._events
                if e.stage == STAGE_ROUND and e.event_type == EVENT_ROUND_COMPLETED
                and e.duration > 0
            ]
            ver_durations = [
                e.duration for e in self._events
                if e.stage == STAGE_VERIFICATION and e.event_type == EVENT_COMPLETED
                and e.duration > 0
            ]
            remaining_rounds = 0
            remaining_disputes = int(self._live.get("remaining_disputes", 0))
            # Each remaining dispute is expected to take ~3 rounds (the
            # consensus-check cadence in _debate_dispute) when we have no
            # observed rounds; otherwise the observed average per dispute.
            if remaining_disputes > 0:
                if round_durations:
                    rounds_per_dispute = max(
                        1, round(statistics.mean(round_durations) and 3 or 3))
                else:
                    rounds_per_dispute = 3
                remaining_rounds = remaining_disputes * rounds_per_dispute

            seconds = 0.0
            basis_parts = []
            if round_durations and remaining_rounds:
                avg_round = statistics.mean(round_durations)
                contrib = avg_round * remaining_rounds
                seconds += contrib
                basis_parts.append(
                    f"{remaining_rounds} round(s) @ {avg_round:.0f}s")
            if ver_durations:
                # At most one more verification pass is expected for the
                # remaining disputes.
                remaining_ver = 1 if remaining_disputes > 0 else 0
                if remaining_ver:
                    avg_ver = statistics.mean(ver_durations)
                    seconds += avg_ver * remaining_ver
                    basis_parts.append(
                        f"{remaining_ver} verification @ {avg_ver:.0f}s")
            if not basis_parts:
                return empty
            minutes = seconds / 60.0
            confidence = "low" if len(round_durations) < 3 else "medium"
            return {
                "estimate_minutes": round(minutes, 1),
                "human_readable": f"~{round(minutes)} minutes (estimate)",
                "basis": "; ".join(basis_parts),
                "confidence": confidence,
            }
        except Exception:
            return empty

    # -- performance report (Phase 7) -------------------------------------

    def _stage_time(self, stage: str) -> float:
        """Sum of all ``<stage>/Completed`` durations."""
        return sum(
            e.duration for e in self._events
            if e.stage == stage and e.event_type == EVENT_COMPLETED
        )

    def performance_report(self) -> dict:
        """Build the Phase-7 performance report. Never raises; missing stages
        report 0.0. ``total_runtime`` is the wall-clock time from
        ``pipeline_started_at`` to now (more accurate than the sum of stage
        buckets, which overlap when stages nest)."""
        if not self._enabled:
            return {}
        try:
            round_durations = [
                e.duration for e in self._events
                if e.stage == STAGE_ROUND and e.event_type == EVENT_ROUND_COMPLETED
                and e.duration > 0
            ]
            ver_durations = [
                e.duration for e in self._events
                if e.stage == STAGE_VERIFICATION and e.event_type == EVENT_COMPLETED
                and e.duration > 0
            ]
            total = 0.0
            if self._pipeline_started_at:
                total = max(0.0, _now() - self._pipeline_started_at)
            return {
                "job_id": self._job_id,
                "workspace_id": self._workspace_id,
                "research_time": round(self._stage_time(STAGE_RESEARCH), 3),
                "debate_time": round(self._stage_time(STAGE_DEBATE), 3),
                "verification_time": round(
                    self._stage_time(STAGE_VERIFICATION), 3),
                "architecture_time": round(
                    self._stage_time(STAGE_ARCHITECTURE), 3),
                "human_synthesis_time": round(
                    self._stage_time(STAGE_HUMAN_SYNTHESIS), 3),
                "build_time": round(self._stage_time(STAGE_BUILD), 3),
                "repair_time": round(self._stage_time(STAGE_REPAIR), 3),
                "total_runtime": round(total, 3),
                "disputes_resolved": int(self._live.get("resolved_disputes", 0)),
                "average_round_duration": round(
                    statistics.mean(round_durations), 3) if round_durations else 0.0,
                "average_verification_duration": round(
                    statistics.mean(ver_durations), 3) if ver_durations else 0.0,
                "generated_at": _iso(_now()),
            }
        except Exception:
            return {}

    # -- persistence ------------------------------------------------------

    def save_status(self) -> None:
        """Write ``workspace_status.json`` to ``status_path``. No-op when
        disabled or no path configured."""
        if not self._enabled or not self._status_path:
            return
        try:
            os.makedirs(os.path.dirname(self._status_path) or ".", exist_ok=True)
            payload = self.status()
            payload["pipeline_started_at"] = self._pipeline_started_at
            payload["eta"] = self.eta()
            with open(self._status_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception:
            return

    def save_report(self, path: str | None = None) -> str | None:
        """Write ``performance_report.json`` (Phase 7). Returns the path
        written, or None when disabled / no path. ``path`` overrides the
        configured ``report_path``."""
        if not self._enabled:
            return None
        out = path or self._report_path
        if not out:
            return None
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(self.performance_report(), f, indent=2, default=str)
            return out
        except Exception:
            return None

    def _load_state(self, path: str) -> None:
        """Resume: restore ``pipeline_started_at`` and live fields from a
        prior ``workspace_status.json`` so the elapsed clock is continuous
        across a restart. Phase 4 resume-safety."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            psa = data.get("pipeline_started_at")
            if isinstance(psa, (int, float)) and psa > 0:
                self._pipeline_started_at = float(psa)
            for k in (
                "stage", "current_round", "active_dispute", "resolved_disputes",
                "remaining_disputes", "total_disputes", "consensus",
                "research_requests", "verification_runs", "execution_mode",
            ):
                if k in data:
                    self._live[k] = data[k]
        except Exception:
            return


# ---------------------------------------------------------------------------
# Module-level singleton + convenience
# ---------------------------------------------------------------------------

recorder = Recorder()


def configure(**kwargs: Any) -> Recorder:
    """Convenience: configure the module-level ``recorder`` singleton."""
    return recorder.configure(**kwargs)


def reset() -> None:
    """Convenience: reset the module-level ``recorder`` singleton."""
    recorder.reset()


if __name__ == "__main__":
    # Deterministic smoke self-check — no I/O side effects beyond a tmp file.
    import tempfile
    tmp = tempfile.mkdtemp()
    status_p = os.path.join(tmp, "workspace_status.json")
    report_p = os.path.join(tmp, "performance_report.json")
    configure(
        job_id="smoke", workspace_id="ws-smoke",
        status_path=status_p, report_path=report_p,
    )
    try:
        recorder.start_stage(STAGE_DEBATE)
        for r in range(1, 4):
            recorder.observe(current_round=r, active_dispute="Exchange Selection",
                             consensus=70 + r * 5)
            recorder.emit(EVENT_ROUND_COMPLETED, STAGE_ROUND, duration=12.0,
                          round=r, dispute="Exchange Selection",
                          consensus=70 + r * 5)
        recorder.emit(EVENT_DISPUTE_RESOLVED, STAGE_DISPUTE,
                      remaining=1, total=3)
        recorder.end_stage(STAGE_DEBATE)
        s = recorder.status()
        assert s["stage"] == STAGE_DEBATE
        assert s["resolved_disputes"] == 1
        assert s["remaining_disputes"] == 1
        assert s["current_round"] == 3
        assert s["consensus"] == 85
        # workspace_status.json was written and is resume-loadable.
        assert os.path.isfile(status_p)
        with open(status_p, encoding="utf-8") as f:
            persisted = json.load(f)
        assert persisted["resolved_disputes"] == 1

        # ETA never crashes and produces a marked estimate.
        eta = recorder.eta()
        assert eta["estimate_minutes"] is not None
        assert "estimate" in eta["human_readable"]

        # Performance report carries the Phase-7 buckets.
        rep = recorder.performance_report()
        for k in ("research_time", "debate_time", "verification_time",
                  "architecture_time", "human_synthesis_time", "build_time",
                  "repair_time", "total_runtime", "disputes_resolved",
                  "average_round_duration", "average_verification_duration"):
            assert k in rep, f"missing perf key: {k}"
        assert rep["disputes_resolved"] == 1
        assert rep["average_round_duration"] == 12.0  # 3 rounds @ 12s each
        path = recorder.save_report()
        assert path == report_p and os.path.isfile(report_p)

        # Resume: a fresh recorder restores the elapsed clock + live fields.
        started_before = recorder._pipeline_started_at
        reset()
        configure(job_id="smoke2", status_path=status_p, report_path=report_p)
        assert recorder._pipeline_started_at == started_before, \
            "resume should restore pipeline_started_at"
        assert recorder.status()["resolved_disputes"] == 1
    finally:
        reset()
    print("telemetry smoke test PASSED")
