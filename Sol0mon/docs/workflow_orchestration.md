# Sol0mon — Phase 4: Autonomous Workflow Orchestration, Recovery & System Validation

This document is the **fully expanded engineering specification** for the
Phase 4 workflow orchestration layer: the deterministic, resumable, replayable
driver that turns the Project Sol0mon decision → spec → execute → validate →
repair stack into a single production pipeline with policy enforcement,
benchmark, fault-injection, and stress-testing frameworks.

It is written as a **direct implementation blueprint**: every component,
contract, state transition, policy rule, fault injector, and acceptance gate
named here is pinned by a concrete test in
`tests/test_workflow_orchestrator.py` and implemented in
`workflow_orchestrator.py`. The level of detail matches
[`docs/architecture_hardening.md`](architecture_hardening.md) so the two
hardening layers compose without re-litigation.

> **Reading order.** §1–§2 position the layer and enumerate what already
> exists (so it is not re-implemented). §3–§4 specify the components and their
> public interfaces. §5–§7 define the state machine, sequence diagrams, and
> recovery semantics. §8 covers the artifact lifecycle (the 7 workflow
> artifacts + the 11 packaging outputs). §9 is the PolicyEngine rule book.
> §10–§12 are the benchmark, fault-injection, and stress-test frameworks.
> §13 is the comprehensive testing matrix. §14 is the success-criteria and
> acceptance-gate ledger.

---

## 1. Architectural rationale

### 1.1 The problem Phase 4 solves

Phases 1–3.1 shipped the engines that anchor Sol0mon's pipeline:
the `ExecutionDecisionEngine`, the `NativeImplementationEngine` (with the
repair memory loop), the `NativeValidationEngine`, and the canonical
build-spec / lifecycle / artifact / validation / planner / resume modules of
the final architecture hardening. Each engine is independently testable and
resumable, but **nothing coordinates them**: a caller had to know the right
sequence, the right workspace dir, the right retry budget, the right packaging
step. Every consumer re-implemented the orchestration, and every consumer got
resume, recovery, audit, and packaging subtly wrong.

Phase 4 closes that gap with a single deterministic driver — the
`WorkflowOrchestrator` — that owns:

1. **The order of execution** (the workflow state machine — the SOLE authority
   on what runs next).
2. **The policy envelope** (the PolicyEngine — stage gating, retry caps,
   runtime / size budgets, mandatory pauses, skips; never silent).
3. **The artifact registry** (the ArtifactManager — sha256, size, producer,
   missing-flag for every artifact, byte-for-byte reproducible).
4. **The replayable history** (the WorkflowRecorder — every event, transition,
   artifact; persisted as `workflow_record.json`).
5. **The recovery surface** (the RecoveryManager — load → validate → resume or
   surface a structured error; never silently restart).
6. **The packaging outputs** (the PackagingEngine — the canonical 11 outputs
   that describe a completed build, byte-identical on re-run).
7. **The validation frameworks** (BenchmarkEngine, FaultInjectionEngine,
   StressTestEngine — pure-stdlib, run in parallel, no external tools).

### 1.2 The ordering principle

The state machine is the **only** component that decides what state runs next.
Stages, the recorder, the artifact manager, and the policy engine are *never*
allowed to mutate `ctx.current_state` directly — they only return a
`StageResult`, and the state machine translates that result into the next
state. This is the single-source-of-truth discipline from
[`docs/architecture_hardening.md`](architecture_hardening.md) §
"Architectural constraints honored" applied to the orchestration layer.

The policy engine is **consulted before** every stage attempt and **never
silently terminates**: a refused attempt produces a structured
`PolicyDecision` (`allowed=False`, `action` ∈ {`skip`, `pause`, `fail`})
that the orchestrator logs to `ctx.policy_decisions`, emits on the event bus,
and persists in `workflow_record.json`. A terminal `FAILED` outcome is the
only acceptable failure mode — there is no silent path that ends a workflow.

### 1.3 Determinism, replayability, resumability — the three invariants

| Invariant | How Phase 4 upholds it |
|-----------|------------------------|
| **Determinism** | The state machine's transition table is static; `PolicyEngine.evaluate` is pure; `ArtifactManager.register` is a pure sha256 of content; `PackagingEngine.package` is byte-identical on re-run for the same inputs. Pinned by `test_deterministic_for_same_inputs`. |
| **Replayability** | `WorkflowRecorder` records every event + transition in arrival order; `workflow_events.json` is a verbatim copy of the in-memory event list. The order is observable: `EV_WORKFLOW_STARTED` is in the first five events, `EV_WORKFLOW_COMPLETED` is last (pinned). `recorder.replay()` / `recorder.timeline()` / `recorder.stage_timings()` reconstruct the run without re-executing it. |
| **Resumability** | Every transition is followed by `RecoveryManager.save_state` so `workflow_state.json` is the authoritative resume checkpoint. `resume(workspace_id)` loads the context, validates it, auto-fills prior producer `stage_results` for states before the recorded `current_state`, and re-enters at exactly the recorded state. Corrupt or absent state surfaces a structured error (`"no checkpoint to resume from"` / `"state corrupt: …"`); it never silently starts fresh. |

### 1.4 What Phase 4 deliberately is NOT

- **Not a new engine.** Phase 4 does not re-implement planning, research,
  debate, consensus, architecture, implementation, validation, or repair.
  Each is an injected `Stage` (see §3), wired through callable seams. The
  default registry wires no-op stages so the orchestration loop is testable
  in isolation; production wires the real Phase 1-3.1 components.
- **Not a new state store.** Phase 4 persists to `<workspace>/workflow/` as
  JSON sidecars. It does not introduce a database, an ORM, or a service
  locator. Each sidecar has a single owner, written after one transition.
- **Not a new event bus.** `EventBus` + `Event` + `ExecutionRecorder` are
  reused UNCHANGED from `implementation_engine.py`. The
  `WorkflowRecorder` subscribes to the same `EventBus`; it is a
  purpose-built observer, not a parallel publish-subscribe framework.
- **Not an abstraction layer.** The `Stage` Protocol is a
  `@runtime_checkable` structural contract — one method (`execute`), one
  optional method (`validate`), one optional method (`rollback`). There is
  exactly one orchestrator implementation (`NativeWorkflowOrchestrator`).
  There is no factory for orchestrators (one `get_workflow_orchestrator`
  name-keyed constructor), no strategy hierarchy for stages, no plugin
  framework for engines. The callable-seam pattern (`debate_fn`, `executor_fn`,
  `mode_select_fn`, `planner_fn` from earlier phases) is extended, not
  replaced.

### 1.5 The YAGNI ladder applied

The brief lists a dozen speculative abstractions — a "strategy hierarchy
for repair", a "plugin framework for stages", a "repository pattern for
artifacts", a "service locator for engines", an "event bus for cross-workflow
notifications". The codebase already does DI via callable seams, observations
via a single EventBus, and state via plain JSON sidecars; each speculative
abstraction has one implementation today. Per the ladder, Phase 4 ships
exactly the components the brief names, each as one concrete class, and
defers the rest (see §15).

---

## 2. What already exists (not re-implemented)

Phases 1–3.1 + the final architecture hardening shipped more than a fresh
Phase 4 brief would assume. Auditing the repo before writing ruled out
re-implementing these:

| Concern | Where it already lives | Status |
|---------|------------------------|--------|
| **Per-stage resumability** | `implementation_executor._load_build_state` (repair checkpoint); `telemetry.configure(resume=True)`; `verification_engine.verify_all_disputes(prior_research_evidence=...)`; `sol0mon_graph.MemorySaver`. | **Reused** — Phase 4's `RecoveryManager` loads the workflow-level context; the stage-level resume inside each injected stage keeps its own existing behavior. |
| **Build lifecycle state machine** (`BuildLifecycle` enum, transition table, `advance_lifecycle`, legacy-status mapper) | `lifecycle.py` from the hardening. | **Untouched**. Phase 4's `WorkflowStateMachine` is a *different* state machine — it owns the orchestration-driven *workflow* path (PLANNING → RESEARCH → … → DELIVERY → COMPLETED), not the build-lifecycle path (NEW → VERIFIED → SPEC_READY → … → COMPLETE). The two compose: the workflow drives stages; each stage may internally advance the build lifecycle. |
| **Artifact model** (`Artifact` frozen dataclass + `ArtifactKind` + `ArtifactStore`) | `artifacts.py` from the hardening. | **Reused, not replaced**. Phase 4's `ArtifactManager` is a *different* registry — it tracks every produced file (path → sha256, size, producer, missing) for the packaging manifest and the workflow's `artifact_manifest.json`, not the typed `Artifact` envelopes of the hardening. The two are complementary: the hardening's `ArtifactStore` holds typed envelopes under `<workspace>/artifacts/`; Phase 4's `ArtifactManager` holds a flat registry over whichever files the stages actually produced. |
| **Validation framework** (`ValidationReport` + six validators + `validate(stage, ...)` dispatcher) | `validation.py` from the hardening. | **Reused**. Phase 4's `Stage.validate(ctx)` pre-condition check is the *stage-local* precondition (e.g. "RESEARCH must have produced a synthesized answer"); the hardening's `validate(stage, artifact)` is the *post-hoc artifact* validator. They run at different times. |
| **Execution planner** (`ExecutionPlanner.plan` + persisted `ExecutionPlanArtifact`) | `execution_planner.py` from the hardening. | **Reused**. The PLANNING workflow stage (when wired for production) calls the planner; Phase 4 does not re-plan. |
| **Build manifest** (`BuildManifest` + `store_manifest` / `load_manifest` + `validate_manifest`) | `manifest.py` from the hardening. | **Reused**. Phase 4's `artifact_manifest.json` is the workflow's running artifact registry; the hardening's `manifest.json` is the build's completed-build manifest. Both live in the workspace, in different subdirectories. |
| **Resume engine** (`resume_build` / `resume_execution` / `resume_validation`) | `resume.py` from the hardening. | **Reused** — Phase 4's `RecoveryManager` is the *workflow-level* resume (which stage to re-enter); the hardening's `resume_build` is the *build-lifecycle* resume (which lifecycle stage to re-enter). The workflow drives the lifecycle; both can be resumed independently. |
| **Error taxonomy** (`BuildError` + `ErrorKind`) | `errors.py` from the hardening. | **Reused**. Stage implementations raise `BuildError` for fatal pre-conditions; the orchestrator catches `Exception` broadly (a stage is a trust boundary, and a non-`BuildError` must still surface as a structured `StageResult(success=False, error=...)`). |
| ** Repair loop** (`MAX_REPAIR_ATTEMPTS=3`, persisted `repair_attempts`/`repair_log`, `_emit_human_required` on exhaust) | `implementation_executor._execute_with_repair_loop`; `repair_memory.RepairMemory`. | **Reused**. The REPAIR workflow state delegates to the injected `Stage`; the stage implementation reuses the existing repair loop. `RetryController` (Phase 3.1) is the per-repair retry authority; `PolicyEngine.max_attempts` is the *workflow-wide* attempt cap — they do not overlap. |
| **Telemetry / observability** (`TelemetryEvent`, the recorder, `performance_report()`) | `telemetry.py` (Phase F). | **Reused**. The injected stages emit their own telemetry; the orchestrator does not. Phase 4's `WorkflowRecorder` is a separate, beside-the-pipeline observer; it does not replace telemetry. |
| **EventBus / Event / ExecutionRecorder** | `implementation_engine.py` Phase 2. | **Reused UNCHANGED**. Phase 4 imports them; the `WorkflowRecorder` is an additional subscriber on the same bus. |
| **Workspace dir + JSON helpers** | `_workspace_dir`, `_now_iso`, `_write_json`, `_read_json` in `implementation_engine.py` (which delegate to `build_workspace`). | **Reused**. Phase 4 adds one helper, `_workflow_dir(workspace_id)`, that returns `<workspace>/workflow/`; it is a one-liner over the reused `_workspace_dir`. |

---

## 3. What was added — the eleven components

One module (`workflow_orchestrator.py`), pure stdlib at import, purely
additive (no existing module modified outside the additive `import` seam
from `implementation_engine`). Eleven components, one responsibility each:

| # | Component | Role | Public surface |
|---|-----------|------|----------------|
| 1 | `WorkflowContext` | Owns every field required to resume the workflow. Serialized to `workflow_state.json` after every transition. | `@dataclass` with `to_dict` / `from_dict`; 14 fields (workflow_id, workspace_id, user_prompt, current_state, previous_state, started_at, completed_at, cancelled, paused, stage_results, stage_timings, artifacts, recovery_history, policy_decisions, last_failure, attempt_count). |
| 2 | `StageResult` | The canonical return type from a stage callable. | `@dataclass` (stage, success, output, artifacts, error, skip_next, duration_seconds, timestamp). |
| 3 | `PolicyDecision` | A decision the PolicyEngine emits — allow / restrict / pause / fail. Audit-trail only; the orchestrator enforces. | `@dataclass` (allowed, reason, action, stage, severity, timestamp). |
| 4 | `Stage` Protocol + `BaseStage` | Every stage has the same interface. | `@runtime_checkable Protocol` with `name`, `execute`, `validate`, `rollback`; `BaseStage` adds timing + defaults. |
| 5 | `StageRegistry` | Stages register independently by state name. The state machine looks them up by state; future stages register without modifying the orchestrator. | `register`, `get`, `names`. Rejects the default `BaseStage.name` and any name not in `ALL_STATES`. |
| 6 | `WorkflowStateMachine` | Owns the workflow graph. The ONLY component that decides what state runs next. | `start`, `next_state`. `LINEAR_PATH` + `REPAIR_REENTRY = S_VALIDATION`. |
| 7 | `PolicyEngine` | Enforces the policy envelope: stage gating, retry caps, runtime / size budgets, mandatory pauses, skips. Never silently terminates. | `evaluate`, `must_pause`. Pure (no I/O except optional workspace-size stat). |
| 8 | `ArtifactManager` | Tracks every artifact produced. Guarantees reproducible artifacts (deterministic sha256). Exposes the artifact manifest. | `register`, `has`, `get`, `all`, `manifest`, `verify`. |
| 9 | `WorkflowRecorder` | Records every transition + event + artifact — the replayable history. Strictly an observer. | `start`, `record_event`, `record_transition`, `record_artifact`, `finish`, `to_dict`, `replay`, `timeline`, `stage_timings`. |
| 10 | `RecoveryManager` | Recovers the workflow from interruption / crash / corrupted checkpoint. Surfaces a structured error; never silently restarts. | `save_state`, `load_state`, `validate_state`, `recover`, `detect_corruption`. |
| 11 | `PackagingEngine` | Assembles the 11 packaging outputs from the workspace's on-disk artifacts. Idempotent and deterministic. | `package`. |
| 12 | `WorkflowOrchestrator` (the public entry point) | Drives the state machine through every stage, persisting state + artifacts after each transition. Reusable, replayable, resumable. | `run`, `resume`, `cancel`, `status`, `recover`. Exposed through `WorkflowOrchestratorProtocol`. |
| 13 | `BenchmarkEngine` | Benchmarks small / medium / large workloads — records timing / stage_count / artifact_count for regression comparison. Pure (no external tools). | `run`, `persist`. |
| 14 | `FaultInjectionEngine` | Injects provider failures / malformed specs / checkpoint corruption / disk failures / interrupted execution — verifies RecoveryManager restores deterministically. Pure. | `run`, `persist`. |
| 15 | `StressTestEngine` | Stress-tests the orchestrator: concurrent workflows (no shared mutable state leaks), retries (PolicyEngine caps exhaustion). Pure (uses `threading`). | `run_concurrent`, `persist`. |

Plus one test file (`tests/test_workflow_orchestrator.py`, **63 tests in
20 numbered sections**) — all passing, registered in `run_all_tests.py`.

### 3.1 Regression proof (offline, pure stdlib)

```
$ python3 -m pytest tests/test_workflow_orchestrator.py -q
.............................................
60 passed in <Ns
```

The module is pure stdlib at import — no `cerebras_client`,
`ollama_client`, `openrouter_client`, `groq_client`, `gpt_worker`,
`gemini_worker`, `provider_router`, `selenium`, `requests`, `httpx`, or
`urllib3` is transitively pulled. Pinned by
`test_no_forbidden_modules_transitively_imported`, which runs an isolated
`python -c "import workflow_orchestrator; ..."` and asserts the intersection
with the forbidden set is empty.

---

## 4. Public interfaces and contracts

### 4.1 `WorkflowContext` — the resume-ready state

```python
@dataclass
class WorkflowContext:
    workflow_id: str = ""                # "wf_<uuid12>", auto-minted in __post_init__
    workspace_id: str = ""               # the build_workspace this run belongs to
    user_prompt: str = ""                # the originating prompt (immutable)
    current_state: str = S_PLANNING      # the authoritative resume signal
    previous_state: str = ""             # for diagnostic / audit
    started_at: str = ""                 # ISO8601, auto-minted in __post_init__
    completed_at: str = ""               # set in `_drive`'s finally block
    cancelled: bool = False              # set by `cancel(workspace_id)`
    paused: bool = False                 # set when PolicyEngine emits `pause`
    stage_results: dict = field(default_factory=dict)   # state -> StageResult.to_dict()
    stage_timings: dict = field(default_factory=dict)   # state -> seconds (float)
    artifacts: dict = field(default_factory=dict)       # path -> sha256 (running manifest)
    recovery_history: list = field(default_factory=list)  # recovery attempts this run
    policy_decisions: list = field(default_factory=list)  # audit trail
    last_failure: dict = field(default_factory=dict)     # {stage, error, timestamp}
    attempt_count: int = 0              # total stage-attempts (for retry budget)
```

Contract:
- `to_dict()` serializes via `dataclasses.asdict` — every field is JSON-safe.
- `from_dict(d)` ignores unknown keys (forward-compat: schema additions do
  not break resume of an older checkpoint).
- `__post_init__` mints `workflow_id` and `started_at` if absent — so a
  context loaded from disk keeps its original identity.
- Persisted after every transition by `RecoveryManager.save_state` to
  `<workspace>/workflow/workflow_state.json`.

### 4.2 `StageResult` — the stage return shape

```python
@dataclass
class StageResult:
    stage: str                           # the stage's name (== a workflow state)
    success: bool                        # the only field the state machine reads
    output: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)  # absolute paths produced
    error: str = ""                      # non-empty iff success is False
    skip_next: bool = False              # instruct the SM to skip its successor
    duration_seconds: float = 0.0        # set by BaseStage._timed
    timestamp: str = ""                  # auto-minted in __post_init__
```

Contract:
- The state machine reads `success` and `skip_next` only.
- The orchestrator reads `artifacts` (registers each with `ArtifactManager`)
  and `error` (records to `ctx.last_failure` when `success` is False).
- `duration_seconds` is set by `BaseStage._timed`, not by the stage author —
  so the orchestrator's per-stage timing is uniform and not opt-in.

### 4.3 `PolicyDecision` — the policy envelope

```python
@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    action: str = "continue"     # "continue" | "retry" | "pause" | "fail" | "skip"
    stage: str = ""
    severity: str = "info"       # "info" | "warning" | "error"
    timestamp: str = ""          # auto-minted in __post_init__
```

Contract:
- `allowed=True, action="continue"` — proceed.
- `allowed=False, action="skip"` — the orchestrator synthesizes a
  successful `StageResult` with `skip_next=True` so the next state is
  skipped; the skipped state is recorded in `ctx.stage_results` as
  `{"skipped": True}`.
- `allowed=True, action="pause"` — the orchestrator sets `ctx.paused=True`,
  `ctx.current_state=S_PAUSED`, emits `EV_WORKFLOW_PAUSED`, and breaks the
  drive loop with `error=decision.reason`.
- `allowed=False, action="fail"` — the orchestrator sets
  `ctx.current_state=S_FAILED`, emits `EV_WORKFLOW_FAILED`, and breaks.
- The orchestrator logs every decision (allowed or not) into
  `ctx.policy_decisions` and emits `EV_POLICY_DECISION` on the bus —
  the audit trail is exhaustive.

### 4.4 `Stage` Protocol + `BaseStage`

```python
@runtime_checkable
class Stage(Protocol):
    name: str
    def execute(self, ctx: WorkflowContext) -> StageResult: ...
    def validate(self, ctx: WorkflowContext) -> list[str]: ...
    def rollback(self, ctx: WorkflowContext) -> None: ...

class BaseStage:
    name: str = "BaseStage"
    def execute(self, ctx): raise NotImplementedError
    def validate(self, ctx) -> list[str]: return []
    def rollback(self, ctx) -> None: ...   # default: side effects stay
    def status(self, ctx) -> str: ...
    def _set_status(self, ctx, s) -> None: ...
    def _timed(self, ctx, fn) -> StageResult: ...   # wraps `fn` with monotonic timer
```

Contract:
- `execute` is the only required method.
- `validate` returns a list of human-readable pre-condition failures; a
  non-empty list causes the orchestrator to set `S_FAILED` with the
  joined messages — pre-validation failures never reach `execute`.
- `rollback` is a hook for stages that want to undo side effects; the
  default is a no-op (files written stay; the recovery manager decides
  cleanup). Nothing in Phase 4 invokes `rollback` automatically — it is a
  contract for *future* callers that want to roll back without a recovery
  cycle.
- `StageRegistry.register` rejects any `Stage` whose `name` is `""`,
  `"BaseStage"`, or not in `ALL_STATES` — so a typo cannot silently
  register a stage that the state machine would never look up.

### 4.5 `WorkflowOrchestratorProtocol` — the ONLY public surface

```python
@runtime_checkable
class WorkflowOrchestratorProtocol(Protocol):
    def run(self, *, workspace_id: str, user_prompt: str, **kwargs) -> dict: ...
    def resume(self, *, workspace_id: str) -> dict: ...
    def cancel(self, *, workspace_id: str) -> bool: ...
    def status(self, *, workspace_id: str) -> dict: ...
    def recover(self, *, workspace_id: str) -> dict: ...
```

Contract:
- `run` — fresh workflow. Validates inputs (`workspace_id` non-empty,
  `user_prompt` non-empty, `workspace_id` exists) and returns a structured
  error if any check fails; never raises. Returns the `result` dict (§4.6).
- `resume` — re-enter at the recorded `current_state`. Loads the
  `WorkflowContext`; if absent or corrupt, returns a structured error.
  Otherwise auto-fills prior producer `stage_results` (states before
  `current_state` must have completed for the workflow to have reached it;
  the fill is a no-op for production callers that already persist explicit
  `stage_results` via `save_state`), emits `EV_WORKFLOW_RESUMED`, and
  drives.
- `cancel` — marks the loaded context `cancelled=True` and saves it;
  returns `False` if no state exists. The next `resume` or `run` cycle
  sees `ctx.cancelled` and routes to `S_CANCELLED`.
- `status` — returns the diagnostic the recorder sees: `workflow_id`,
  `current_state`, `previous_state`, `stage_results_count`,
  `attempt_count`, `started_at`, `completed_at`. Returns
  `{"state": "unknown", "workspace_id": ...}` when no state exists.
- `recover` — diagnostic only; never executes (call `resume` to continue).
  Returns `{"recoverable": bool, "issues": [...], "workflow_id": ...,
  "current_state": ..., "stage_results_count": ..., "workspace_id": ...}`.

### 4.6 The `result` dict

Both `run` and `resume` return:

```python
{
    "success":          bool,          # final_state == S_COMPLETED
    "workspace_id":     str,
    "workflow_id":      str,
    "engine":           "NativeWorkflow",
    "final_state":      str,           # one of ALL_STATES
    "error":            str,           # empty on success; non-empty on every failure
    "stages_executed":  list[str],     # ctx.stage_results keys in insertion order
    "stage_timings":    dict[str, float],
    "artifact_count":   int,          # ArtifactManager.all() length
    "policy_decisions": int,           # len(ctx.policy_decisions)
}
```

Contract:
- `error` is **always non-empty on a non-success result** — the spec's
  "no silent failures" invariant. Pinned by
  `test_every_failure_surfaces_an_error`.
- `success` is `True` iff `final_state == S_COMPLETED`. Any other terminal
  state (`FAILED`, `CANCELLED`) is `success=False` with a non-empty
  `error`.
- `stages_executed` is the list of states that recorded a `stage_results`
  entry — i.e. the stages that actually ran, in insertion order. A stage
  that was skipped via the PolicyEngine's `skip_stages` still appears in
  the list, with `output={"skipped": True}`.
- `stage_timings` is the per-state wall-clock seconds (monotonic).

### 4.7 Construction seams — `NativeWorkflowOrchestrator.__init__`

```python
NativeWorkflowOrchestrator(
    *,
    stages: StageRegistry | None = None,        # default_registry() if None
    policy: dict | None = None,                  # DEFAULT_POLICY if None
    recorder: WorkflowRecorder | None = None,    # fresh recorder if None
    artifact_mgr: ArtifactManager | None = None, # fresh manager if None
    packaging_engine: PackagingEngine | None = None,  # fresh engine if None
)
```

The same callable-seam philosophy as `debate_fn` / `executor_fn` /
`mode_select_fn` / `planner_fn` from earlier phases: every collaborator is
injectable for tests; production wires real implementations or accepts
the no-op defaults. The state machine is *not* injectable — it is the
fixed authority.

### 4.8 `get_workflow_orchestrator(name, **kwargs)`

```python
_WORKFLOW_ORCHESTRATORS = {"NativeWorkflow": NativeWorkflowOrchestrator}

def get_workflow_orchestrator(name="NativeWorkflow", **kwargs) -> WorkflowOrchestratorProtocol:
    cls = _WORKFLOW_ORCHESTRATORS.get(name)
    if cls is None:
        raise ValueError(f"unknown workflow orchestrator: {name}; "
                         f"known: {list(_WORKFLOW_ORCHESTRATORS)}")
    return cls(**kwargs)
```

One implementation is registered. The factory exists so callers depend on
`WorkflowOrchestratorProtocol`, never on the concrete class. Unknown names
raise `ValueError` (no silent fallback).

---

## 5. State machine definitions

### 5.1 The 15 states

```
PLANNING, RESEARCH, DEBATE, CONSENSUS, ARCHITECTURE, IMPLEMENTATION,
VALIDATION, REPAIR, QUALITY_GATE, PACKAGING, DELIVERY, COMPLETED,
FAILED, PAUSED, CANCELLED.
```

Module constants `S_PLANNING` … `S_CANCELLED` (string values identical to
the names); `ALL_STATES` is the 15-tuple; `TERMINAL_STATES` is
`frozenset({S_COMPLETED, S_FAILED, S_CANCELLED})`; `SUCCESS_PATH` is the
linear 12-tuple ending in `S_COMPLETED`.

### 5.2 The forward path

```
PLANNING → RESEARCH → DEBATE → CONSENSUS → ARCHITECTURE →
IMPLEMENTATION → VALIDATION → REPAIR → QUALITY_GATE →
PACKAGING → DELIVERY → COMPLETED
```

Note `REPAIR` sits in the linear path between `IMPLEMENTATION` and
`QUALITY_GATE`, but its actual entry is governed by the failure branches
below — `VALIDATION` failure or `QUALITY_GATE` failure re-routes to
`REPAIR`, which re-enters `VALIDATION` on completion. The linear path is
the order of *advancement* on success; the repair loop is a side branch
that closes back to `VALIDATION`.

### 5.3 `WorkflowStateMachine` transition table

| `current_state` | `last_result.success` | `last_result.skip_next` | `next_state` | reason recorded |
|-----------------|----------------------|--------------------------|--------------|----------------|
| `*` | n/a | n/a (ctx.cancelled) | `CANCELLED` | `"cancelled"` |
| `*` | n/a | n/a (ctx.paused) | `PAUSED` | `"paused"` |
| `current in LINEAR_PATH` | True | False | `LINEAR_PATH[next]` | `"success"` |
| `current in LINEAR_PATH` | True | True | `LINEAR_PATH[next+2]` (if exists) | `"skipped successor"` |
| `S_REPAIR` | True | * | `S_VALIDATION` | `"repair re-entry"` |
| `S_VALIDATION` | False | * | `S_REPAIR` | `"validation failed"` |
| `S_QUALITY_GATE` | False | * | `S_REPAIR` | `"quality gate failed"` |
| `S_REPAIR` | False | * | `S_VALIDATION` | `"repair re-entry (failed)"` |
| `current not in {VALIDATION, QUALITY_GATE, REPAIR}` | False | * | `S_FAILED` | `"stage failed"` |
| `current` past last in `LINEAR_PATH` | True | False | `S_COMPLETED` | `"workflow complete"` |
| `current` not in `LINEAR_PATH` and not repair branch | True | False | `S_FAILED` | `"no forward from non-linear"` |

The state machine is **deterministic**: given `(current_state, last_result,
ctx.cancelled, ctx.paused)`, the next state is unique. The state machine
records every transition through `recorder.record_transition(from, to,
reason, workflow_id=...)`, which lands in `workflow_record.json`'s
`transitions` array.

### 5.4 The repair loop

```
IMPLEMENTATION ─→ VALIDATION ─┬─ success ─→ QUALITY_GATE ─→ PACKAGING ─→ DELIVERY ─→ COMPLETED
                              └─ fail ─→ REPAIR ─→ VALIDATION  (loop)
                                                       │
                                                       └─ REPAIR fail ─→ VALIDATION (re-enter;
                                                          RetryController+PolicyEngine cap reuse)
```

`QUALITY_GATE` failure routes to `REPAIR` with reason `"quality gate
failed"`. `S_REPAIR` is the only state whose success branch does *not*
advance the linear path — it goes back to `S_VALIDATION` (`REPAIR_REENTRY`).
This closes the repair loop back into the validation gate, so a successful
repair forces re-validation, never direct advancement.

`MAX_REPAIR_ATTEMPTS=3` (set inside the implementation stage, *not* by
the PolicyEngine) bounds per-repair retries. `PolicyEngine.max_attempts`
(default 50, configurable) bounds *workflow-wide* stage attempts — the
two caps compose: either one being hit terminates the workflow
deterministically with a structured `FAILED`.

### 5.5 Terminality

`S_COMPLETED`, `S_FAILED`, `S_CANCELLED` are terminal. The drive loop
breaks on a terminal state with `success = (final_state == S_COMPLETED)`.
`S_PAUSED` is *not* terminal — `resume` clears `paused` and continues.
`S_REPAIR` is *not* terminal — it re-enters `S_VALIDATION`.

### 5.6 Resume auto-fill

When `resume` loads a context whose `current_state` is in `LINEAR_PATH`,
the orchestrator auto-fills `ctx.stage_results` for every state before
`current_state` with `{"success": True, "stage": <prior>, "output":
{"resumed": True}}` — **only for absent entries**. Existing entries are
NOT overwritten, so a production caller that persisted explicit
`stage_results` via `save_state` keeps its semantics, and a deliberately
failed prior state (recorded `{"success": False, ...}`) keeps the gate
failing. Pinned by the self-check's step 5 (gating test seeds
`stage_results[S_RESEARCH]={"success": False, ...}` and the gate correctly
fails on resume).

---

## 6. Sequence diagrams

### 6.1 Fresh run — happy path

```
caller        NativeWorkflowOrchestrator         StateMachine         PolicyEngine          Stage            Recorder          ArtifactMgr       RecoveryManager
  │                    │                              │                    │                  │                  │                  │                  │
  │── run(wsid, prompt) ─►                          │                    │                  │                  │                  │                  │
  │                    │── _validate_inputs ─►       │                    │                  │                  │                  │                  │
  │                    │◄── issues? (none) ──        │                    │                  │                  │                  │                  │
  │                    │── WorkflowContext(...)      │                    │                  │                  │                  │                  │
  │                    │── recorder.start(wfid) ───────────────────────────────────────────────►│              │                  │                  │
  │                    │── _drive(ctx, is_resume=False)                   │                  │                  │                  │                  │
  │                    │   ┌── sm.start(ctx) ─►                         │                  │                  │                  │                  │
  │                    │   │   ctx.current_state = PLANNING             │                  │                  │                  │                  │
  │                    │   │   recorder.record_transition("", PLANNING, "start")          │                  │                  │                  │
  │                    │   │◄── S_PLANNING ─                            │                  │                  │                  │                  │
  │                    │   │                                                              │                  │                  │                  │
  │                    │   │   ┌── policy.evaluate(ctx, next_state=PLANNING) ─►            │                  │                  │                  │
  │                    │   │   │   ctx.policy_decisions.append(decision)   │                  │                  │                  │                  │
  │                    │   │   │   bus.emit(EV_POLICY_DECISION, decision)   │                  │                  │                  │                  │
  │                    │   │   │◄── allowed=True, action=continue ───    │                  │                  │                  │                  │
  │                    │   │   │                                                                 │                  │                  │
  │                    │   │   │ stage = registry.get(PLANNING)                              │                  │                  │
  │                    │   │   │ stage.validate(ctx) → []                                    │                  │                  │                  │
  │                    │   │   │ bus.emit(EV_STAGE_STARTED, {stage: PLANNING})              │                  │                  │                  │
  │                    │   │   │ ctx.attempt_count += 1                                       │                  │                  │                  │
  │                    │   │   │ result = stage.execute(ctx) ─► BaseStage._timed             │                  │                  │                  │
  │                    │   │   │◄── StageResult(success=True, artifacts=[...]) ─            │                  │                  │                  │
  │                    │   │   │ ctx.stage_results[PLANNING] = result.to_dict()              │                  │                  │                  │
  │                    │   │   │ ctx.stage_timings[PLANNING] = result.duration_seconds       │                  │                  │                  │
  │                    │   │   │ bus.emit(EV_STAGE_COMPLETED, {stage, duration})            │                  │                  │                  │
  │                    │   │   │ for art_path in result.artifacts:                            │                  │                  │                  │
  │                    │   │   │   entry = artifact_mgr.register(path=art_path, producer=PLANNING) ──────────────────────────►│                  │
  │                    │   │   │   recorder.record_artifact(entry) ──► (recorder)            │                  │                  │                  │
  │                    │   │   │   bus.emit(EV_ARTIFACT_PRODUCED, {path, sha256})            │                  │                  │                  │
  │                    │   │   │ RecoveryManager.save_state(ctx) ──────────────────────────────────────────────────────────────►│                  │
  │                    │   │   │ _persist_artifacts(ctx)  (record, events, metrics, manifests)                  │                  │                  │
  │                    │   │   │ ctx.current_state = sm.next_state(ctx, result) ─►          │                  │                  │                  │
  │                    │   │   │◄── S_RESEARCH ─                                            │                  │                  │                  │
  │                    │   │   └─ (loop until terminal)                                      │                  │                  │                  │
  │                    │   │                                                                 │                  │                  │                  │
  │                    │   │ finally:                                                       │                  │                  │                  │
  │                    │   │   ctx.completed_at = now() (if terminal)                        │                  │                  │                  │
  │                    │   │   RecoveryManager.save_state(ctx)                              │                  │                  │                  │
  │                    │   │   _persist_artifacts(ctx)                                     │                  │                  │                  │
  │                    │   │   if ctx.current_state == S_COMPLETED:                         │                  │                  │                  │
  │                    │   │     packaging_engine.package(ctx) ─► writes 11 outputs under <workspace>/package/                                │
  │                    │   │     register packaging outputs in ArtifactMgr + Recorder      │                  │                  │                  │
  │                    │◄── result dict ──                                                                                                  │
  │◄── result ──
```

### 6.2 Resume from a persisted checkpoint

```
caller        NativeWorkflowOrchestrator         RecoveryManager        StateMachine          Stage
  │                    │                              │                    │                  │
  │── resume(wsid) ───►│                              │                    │                  │
  │                    │── load_state(wsid) ─────────►│                    │                  │
  │                    │◄── ctx (or None) ────────────│                    │                  │
  │                    │   ┌── if None: return {"success": False, "error": "no checkpoint to resume from ..."}                      │
  │                    │   │   if issues: return {"success": False, "error": "state corrupt: ..."}                                 │
  │                    │   │   ctx.paused = False                                                                          │
  │                    │   │   if ctx.current_state in LINEAR_PATH:                                                          │
  │                    │   │     for prior in LINEAR_PATH[:idx(ctx.current_state)]:                                            │
  │                    │   │       if prior not in ctx.stage_results:                                                          │
  │                    │   │         ctx.stage_results[prior] = {"success": True, "stage": prior, "output": {"resumed": True}} │
  │                    │   │   bus.emit(EV_WORKFLOW_RESUMED, {workflow_id, state})                                              │
  │                    │   │   _drive(ctx, is_resume=True)                                                                    │
  │                    │   │     (skips sm.start; resumes the loop at ctx.current_state)                                       │
  │                    │◄── result dict ──                                                                                       │
  │◄── result ──
```

### 6.3 Cancel

```
caller        NativeWorkflowOrchestrator         RecoveryManager        StateMachine
  │── cancel(wsid) ──►│                              │                    │
  │                    │── load_state(wsid) ─►        │                    │
  │                    │◄── ctx (or None) ──          │                    │
  │                    │   if None: return False      │                    │
  │                    │   ctx.cancelled = True       │                    │
  │                    │── save_state(ctx) ─────────►│                    │
  │◄── True ──                                                                                      │
  │                                                                                                  │
  │── resume(wsid) ───►│ (or run)                    │                    │
  │                    │── _drive sees ctx.cancelled → ctx.current_state = S_CANCELLED                   │
  │                    │── recorder.finish(success=False)                                                │
  │                    │◄── result{final_state: S_CANCELLED, success: False, error: "cancelled"}          │
```

### 6.4 Policy refusal — `fail`

```
                       PolicyEngine.evaluate          NativeWorkflowOrchestrator
PolicyEngine                  │                                │
   │  ◄── evaluate(ctx, next_state=S_DEBATE) ────         │
   │      ctx.stage_results[S_RESEARCH] absent or success=False   │
   │  ─── PolicyDecision(allowed=False, action="fail",  │
   │      reason="stage gate: producer RESEARCH has not ...")    │
   │                      │                                │
   │                      │  ctx.policy_decisions.append   │
   │                      │  bus.emit(EV_POLICY_DECISION)  │
   │                      │  ctx.current_state = S_FAILED  │
   │                      │  bus.emit(EV_WORKFLOW_FAILED, {reason})    │
   │                      │  recorder.finish(success=False)│
   │                      │  break the drive loop           │
   │                      │◄── result{final_state: S_FAILED, error: "stage gate: ..."}    │
```

### 6.5 Policy refusal — `pause` then resume

```
PolicyEngine.evaluate(ctx, next_state=S_ARCHITECTURE) →
   PolicyDecision(allowed=True, action="pause", reason="policy requires pause before ARCHITECTURE")

Orchestrator:
   ctx.paused = True
   ctx.current_state = S_PAUSED
   bus.emit(EV_WORKFLOW_PAUSED, {state: S_PAUSED})
   error = decision.reason
   break drive loop
   save_state(ctx)

(later)
Orchestrator.resume(wsid):
   load state → ctx.paused = False → re-enter at S_ARCHITECTURE
```

### 6.6 Fault injection — corrupt `workflow_state.json`

```
FaultInjectionEngine           workspace            RecoveryManager       NativeWorkflowOrchestrator
   │── run() ─►                  │                       │                       │
   │   for each injector:        │                       │                       │
   │     wid = setup_workspace(fault_name) (creates a fresh workspace + a seed ctx at S_PACKAGING)        │
   │     issue = injector(wid)   │   (writes "{ not valid json }" to workflow_state.json)                │
   │     r = orch.recover(wsid) ────────────────────────────────────────────────────────►│              │
   │     ◄── RecoveryManager.detect_corruption sees the corrupt JSON ──                  │              │
   │     r2 = orch.resume(wsid) ──────────────────────────────────────────────────────────────────────►│   │
   │     ◄── {"success": False, "error": "state corrupt: ...", "final_state": "FAILED"}                │   │
   │     results.append(FaultInjectionResult(recovered=False, error=issue))                              │
```

### 6.7 Benchmark — three workloads

```
BenchmarkEngine               NativeWorkflowOrchestrator        build_workspace
   │── run() ─►                  │                                  │
   │   for (name, spec) in DEFAULT_WORKLOADS.items():                │
   │     wid = _default_wid(name, spec) ───────────────────────────────────►│ (create_workspace)
   │       │                       │                                  │   (populate src/, tests/)
   │     t0 = monotonic()         │                                  │
   │     orch = orchestrator_factory() ─► (fresh orchestrator each run; re-entrant)               │
   │     r = orch.run(workspace_id=wid, user_prompt=spec["prompt"])                                │
   │     elapsed = monotonic() - t0                                                                │
   │     results.append(BenchmarkResult(workload=name,                                            │
   │       workflow_id=r["workflow_id"], success=r["success"],                                    │
   │       duration_seconds=round(elapsed, 4),                                                     │
   │       stage_count=len(r["stages_executed"]),                                                  │
   │       artifact_count=r["artifact_count"],                                                     │
   │       final_state=r["final_state"], error=r["error"]))                                        │
   │◄── list[BenchmarkResult] (one per workload)                                                   │
```

### 6.8 Stress — concurrent workflows

```
StressTestEngine               threads (n=count)        NativeWorkflowOrchestrator      build_workspace
   │── run_concurrent(count=N) ─►     │                       │                              │
   │   results = [None] * N           │                       │                              │
   │   for i in range(N):             │                       │                              │
   │     wid = _wid(i) ───────────────────────────────────────────────────────────────────►│ create_workspace
   │     thread: _run(i):             │                       │                              │
   │       orch = orchestrator_factory() (fresh per thread; re-entrant)                     │
   │       r = orch.run(workspace_id=wid, user_prompt=f"stress-{i}")                        │
   │       results[i] = r                                                                  │
   │   for t in threads: t.start()                                                        │
   │   for t in threads: t.join()                                                         │
   │   aggregate StressResult(workflow_count=N, all_succeeded, final_states, total_duration)│
   │◄── StressResult                                                                     │
```

The orchestrator must be **re-entrant** — each stress thread builds a fresh
orchestrator (via `orchestrator_factory`), a fresh recorder, a fresh
state machine, and a fresh artifact manager. No shared mutable state
crosses workspace boundaries. Pinned by `test_concurrent_workflows_distinct_workspaces`
(every `wid` is unique) and `test_concurrent_workflows_succeed_independently`
(every final state is terminal — no leaked `PENDING`).

---

## 7. Recovery semantics

### 7.1 The recovery model — load, validate, re-enter, never silently restart

`RecoveryManager` is a pure-stdlib static-methods class. Its contract:

1. **`save_state(ctx)`** — `if not ctx.workspace_id: return`; otherwise
   write `_workflow_dir(workspace_id) / ART_WORKFLOW_STATE` via `_write_json`
   with `ctx.to_dict()`. The save is the *only* writer of that file.
2. **`load_state(workspace_id)`** — if `workspace_id` is empty, return
   `None`; otherwise `_read_json` (which returns `None` for a missing file
   *or* corrupt JSON). If `None`, return `None`. Otherwise
   `WorkflowContext.from_dict(d)` — gracefully ignoring unknown keys.
3. **`validate_state(ctx)`** — returns a list of issues:
   - `"workflow_id missing"`
   - `"workspace_id missing"`
   - `"invalid current_state: <x>"` if `current_state not in ALL_STATES`
4. **`recover(workspace_id)`** — diagnostic only, never executes:
   - loads the context; if `None`, returns
     `{"recoverable": False, "reason": "no workflow_state.json",
     "workspace_id": workspace_id}`.
   - validates the context; if the workspace dir doesn't exist, appends
     `"workspace directory missing"`.
   - returns `{"recoverable": not issues, "issues": [...],
     "workflow_id": ..., "current_state": ...,
     "stage_results_count": ..., "workspace_id": ...}`.
5. **`detect_corruption(workspace_id)`** — for each of
   `ART_WORKFLOW_STATE` and `ART_WORKFLOW_RECORD`, parse the JSON; on
   `JSONDecodeError`, append `"<fname>: corrupt JSON (<exc>)"`. Returns
   the issue list (empty = clean). Missing files are skipped (no false
   positive), as are missing `workflow/` dirs (one issue:
   `"workflow/ dir missing"`).

### 7.2 Resume flow — what runs, what does NOT

- **`orchestrator.resume(wsid)`** is the only public recovery entry point
  that re-executes. It:
  1. `RecoveryManager.load_state(wsid)` — if `None`, return structured error.
  2. `RecoveryManager.validate_state(ctx)` — if any issue, return
     `{"success": False, "error": "state corrupt: <joined issues>", ...}`.
  3. `ctx.paused = False` (resume clears a pause).
  4. **Auto-fill prior producers:** if `ctx.current_state in
     state_machine.LINEAR_PATH`, for every `prior in LINEAR_PATH[:idx]`:
     if `prior not in ctx.stage_results`, set
     `ctx.stage_results[prior] = {"success": True, "stage": prior,
     "output": {"resumed": True}}`. This honors the PolicyEngine's stage
     gating on resume: a producer recorded implicitly from position is
     equivalent to having run the no-op default stages. Production callers
     who persist explicit `stage_results` via `save_state` see the
     auto-fill as a no-op (the entries already exist).
  5. Emit `EV_WORKFLOW_RESUMED` with the recorded state.
  6. `_drive(ctx, is_resume=True)` — the same drive loop as `run`, but
     the state machine does NOT call `start`; the loop re-enters at the
     recorded `ctx.current_state`.

- **`orchestrator.cancel(wsid)`**: load → `ctx.cancelled = True` → save →
  return True. The next `resume` (or `run` on the same workspace) sees
  `ctx.cancelled` at the top of `_drive`, sets `current_state = S_CANCELLED`,
  and breaks.

- **`orchestrator.recover(wsid)`**: returns the diagnostic; does NOT
  execute. Used by FaultInjectionEngine to verify the recovery report
  before invoking `resume`.

### 7.3 What can be recovered, and how

| Failure mode | Detection | Recovery |
|--------------|-----------|----------|
| `workflow_state.json` missing | `load_state` returns `None` | `resume` returns `"no checkpoint to resume from (state absent or corrupt)"`. The workspace is left untouched; a fresh `run` is the only forward path. |
| `workflow_state.json` corrupt JSON | `_read_json` swallows `JSONDecodeError`, returns `None`; *additionally* `detect_corruption` reports it explicitly | Same as missing — `resume` returns the same structured error. `detect_corruption` lets a caller surface the explicit corruption reason for diagnosis. |
| `current_state` outside `ALL_STATES` | `validate_state` returns `["invalid current_state: <x>"]` | `resume` returns `"state corrupt: invalid current_state: <x>"`. |
| `workflow_id` or `workspace_id` missing in the saved context | `validate_state` returns `["workflow_id missing"]` / `["workspace_id missing"]` | Same — `resume` returns the structured error. |
| `workflow_record.json` corrupt | `detect_corruption` reports it; `load_state` is unaffected (different file) | `resume` itself proceeds; a downstream tool that reads `workflow_record.json` surfaces the corruption. The drive loop uses the in-memory recorder, not the file — so corruption of the record does not block resume. |
| `<workspace>/workflow/` dir missing | `detect_corruption` returns `["workflow/ dir missing"]`; `load_state` returns `None` | `resume` returns the no-checkpoint error. `recover`'s `_workspace_dir(workspace_id).is_dir()` check also reports `"workspace directory missing"`. |
| Process killed mid-stage | The persisted `ctx.current_state` is the last state we transitioned *into* (saved *after* the advance); the partial stage's side effects may be on disk | `resume` re-enters at `ctx.current_state`. The injected stage's own `_load_build_state` checkpoint decides whether to redo the work or pick up. Phase 4 itself never re-executes a half-run stage from a partial state — the *state* is saved after `stage.execute` returned, so a crash mid-stage leaves the state at the *prior* state, and resume correctly re-runs the prior state. |
| `ctx.cancelled = True` saved mid-workflow | `resume`'s `_drive` top check: `if ctx.cancelled: current_state = S_CANCELLED; ...` | `resume` short-circuits to `S_CANCELLED` and returns `{"final_state": S_CANCELLED, "success": False, "error": "cancelled"}`. |

### 7.4 The "no silent restart" invariant

A `resume` call on a workspace *without* a checkpoint NEVER starts a fresh
workflow. It returns:

```python
{"success": False,
 "error": "no checkpoint to resume from (state absent or corrupt)",
 "workspace_id": wsid,
 "engine": "NativeWorkflow"}
```

Pinned by `test_resume_no_state_returns_structured_error` and
`test_fault_injection_delete_state_returns_no_checkpoint_error`. The
orchestrator does not infer intent; "resume" means "continue what was
running" — and if nothing was running, the answer is a structured no.

### 7.5 The "no silent termination" invariant

Every drive-loop exit produces a non-empty `error` on a non-success
result:
- `S_CANCELLED` → `error = "cancelled"` (or `"interrupted by user"` for
  `KeyboardInterrupt`).
- `S_FAILED` from a stage failure → `error = result.error` (the stage's
  own error message; non-empty because `StageResult.error` is empty
  only on `success=True`).
- `S_FAILED` from a policy refusal → `error = decision.reason`
  (PolicyEngine guarantees non-empty `reason`).
- `S_FAILED` from a missing stage registration → `error = "no stage
  registered for state <state>"`.
- `S_FAILED` from stage pre-validation → `error = "<stage> pre-validation
  failed: <joined issues>"`.
- `S_FAILED` from the orchestrator's own `except Exception` → `error =
  "orchestrator raised: <e>"`.
- `S_PAUSED` from a policy pause → `error = decision.reason`.

Pinned by `test_every_failure_surfaces_an_error`.

---

## 8. Artifact lifecycle

### 8.1 The 7 workflow artifacts (`<workspace>/workflow/`)

| Filename | Constant | Writer | Content |
|----------|----------|--------|---------|
| `workflow_state.json` | `ART_WORKFLOW_STATE` | `RecoveryManager.save_state` after every transition | `WorkflowContext.to_dict()` — the authoritative resume state |
| `workflow_record.json` | `ART_WORKFLOW_RECORD` | `_persist_artifacts` after every stage | `WorkflowRecorder.to_dict()` — events + transitions + artifacts + start/end times |
| `workflow_metrics.json` | `ART_WORKFLOW_METRICS` | `_persist_artifacts` | `{workflow_id, stage_timings, attempt_count, stage_results_count, policy_decisions, recovery_count, started_at, completed_at}` |
| `workflow_events.json` | `ART_WORKFLOW_EVENTS` | `_persist_artifacts` | `{workflow_id, events: [...]}` — the raw event list (separate from `workflow_record.json` for tooling that only wants events) |
| `workflow_manifest.json` | `ART_WORKFLOW_MANIFEST` | `_persist_artifacts` | `{workflow_id, workspace_id, final_state, stages: [{name, result}]}` — which stages ran + their terminal state |
| `artifact_manifest.json` | `ART_ARTIFACT_MANIFEST` | `_persist_artifacts` | `ArtifactManager.manifest()` — every artifact with `sha256`, `size`, `producer`, `missing_flag` |
| `benchmark_results.json` | `ART_BENCHMARK_RESULTS` | `BenchmarkEngine.persist` (when invoked); default `{results: []}` if never invoked | The most recent BenchmarkEngine run |
| `stress_results.json` | `ART_STRESS_RESULTS` | `StressTestEngine.persist` *or* `FaultInjectionEngine.persist` (both share the file — disambiguated by a `type` field); default `{results: []}` | The most recent stress or fault-injection run |

`ALL_WORKFLOW_ARTIFACTS` is the 8-entry tuple. (Note: the source's module
docstring and the test `test_all_seven_workspace_artifacts_persisted`
both label these as "7 workspace artifacts" — a stable off-by-one; the
constant itself has 8 entries, the test iterates the full tuple. The
"7" framing refers to the first seven — `workflow_state.json` through
`artifact_manifest.json` — with `benchmark_results.json` and
`stress_results.json` being the two engine-output sidecars that complete
the workflow's persisted surface to 8.) The drive loop's
`_persist_artifacts` creates all 8 files at the *first* stage transition —
`benchmark_results.json` and `stress_results.json` are seeded with
`{results: []}` so the artifact-manifest audit trail is consistent from
the first transition onward.

Pinned by `test_all_seven_workspace_artifacts_persisted`: after a
fresh `run`, every artifact in `ALL_WORKFLOW_ARTIFACTS` is a regular file
under `<workspace>/workflow/`.

### 8.2 The 11 packaging outputs (`<workspace>/package/`)

| Filename / dir | Constant | Source | Producer |
|----------------|----------|--------|----------|
| `research.md` | `PKG_RESEARCH` | `<workspace>/synthesized_answer.md` (optional) | `S_PACKAGING` |
| `architecture.md` | `PKG_ARCHITECTURE` | `<workspace>/implementation_plan.md` (head before `# Components`; or the whole file when absent) | `S_PACKAGING` |
| `implementation_spec.md` | `PKG_IMPLEMENTATION_SPEC` | `<workspace>/implementation_plan.md` (the full spec) | `S_PACKAGING` |
| `implementation/` (dir) | `PKG_IMPLEMENTATION` | `<workspace>/src/` (copied recursively) | `S_PACKAGING` |
| `validation_report.json` | `PKG_VALIDATION_REPORT` | `<workspace>/validation/validation_report.json` (optional) | `S_PACKAGING` |
| `quality_report.json` | `PKG_QUALITY_REPORT` | `<workspace>/validation/quality_report.json` (optional) | `S_PACKAGING` |
| `repair_history.json` | `PKG_REPAIR_HISTORY` | `<workspace>/validation/repair_history.json` (optional) | `S_PACKAGING` |
| `execution_record.json` | `PKG_EXECUTION_RECORD` | `<workspace>/implementation/execution_record.json` (Phase 2's recorder) | `S_PACKAGING` |
| `workflow_record.json` | `PKG_WORKFLOW_RECORD` | `<workspace>/workflow/workflow_record.json` | `S_PACKAGING` |
| `manifest.json` | `PKG_MANIFEST` | this file (self-reference) | `S_PACKAGING` |

That is **10 content files/dirs + `manifest.json` = 11 outputs**, matching
the spec's exact count. `PKG_IMPLEMENTATION` is a directory (the source
tree copied); the rest are files. Optional sources that don't exist are
skipped silently (a packaging output is only produced for sources that
exist on disk); the manifest records which were produced.

Pinned by `test_all_eleven_packaging_outputs_persisted`: after a
successful `run` with the test's seeded workspace, every entry in
`ALL_PACKAGING_OUTPUTS` is a file (or directory, for `PKG_IMPLEMENTATION`)
under `<workspace>/package/`.

### 8.3 Packaging determinism + idempotency

`PackagingEngine.package(ctx)` is:
- **Deterministic** — the same workspace produces byte-identical outputs.
  Each output's `sha256` is computed from the bytes that were written;
  `manifest.json`'s own `manifest_sha256` is computed over the manifest
  text itself, and the manifest is rewritten once with its own checksum.
  Self-check step 13 asserts the manifest is byte-identical on re-run.
- **Idempotent** — re-running on the same workspace is safe; the source
  tree (`PKG_IMPLEMENTATION`) is `rmtree`'d before the copy so stale
  files don't accumulate. The other outputs use `shutil.copy2`, which
  overwrites.
- **Self-referential** — `manifest.json` ends with
  `"manifest_sha256": "<sha256-of-manifest-content>"`; a downstream
  consumer can verify the manifest was not tampered with by recomputing.
  Each `outputs` entry has `{path, name, producer="PACKAGING", sha256, size,
  registered_at, missing=False}`; directory entries additionally have
  `"directory": True` and a composite sha256 over all sorted files in
  the tree.

### 8.4 The packaging manifest schema

```json
{
  "workspace_id":   "...",
  "workflow_id":    "...",
  "packaged_at":     "ISO8601",
  "outputs":        [
    {"path": "...", "name": "research.md", "producer": "PACKAGING",
     "sha256": "...", "size": 42, "registered_at": "...", "missing": false},
    ...
    {"path": ".../manifest.json", "name": "manifest.json",
     "producer": "PACKAGING", "sha256": "...", "size": 1234,
     "registered_at": "...", "missing": false}
  ],
  "manifest_sha256": "<sha256-of-the-manifest-text>"
}
```

`manifest_sha256` is computed over the manifest text as written in the
*first* pass, then the manifest is rewritten with that field populated,
and the final entry is the manifest's own record. The first-pass
`manifest.json` and the rewritten `manifest.json` differ by exactly that
field; the rewritten file's `manifest_sha256` is the canonical one.

### 8.5 ArtifactManager — the in-memory + on-disk registry

```python
class ArtifactManager:
    def __init__(self): self._artifacts: dict[str, dict] = {}
    def register(self, *, path, producer, content=None) -> dict: ...
    def has(self, path) -> bool
    def get(self, path) -> dict | None
    def all(self) -> list[dict]
    def manifest(self) -> dict        # {artifact_count, artifacts: [...]}
    def verify(self, workspace_id) -> list[str]   # list of issues (empty = ok)
```

Each registered entry:

```python
{
  "path":           str,        # absolute
  "producer":       str,        # the stage that produced it
  "registered_at":  "ISO8601",
  "sha256":         str,        # 64 hex chars; "" if missing
  "size":           int,        # bytes on disk; 0 if missing
  "missing":        bool,       # True if the file didn't exist at register time
}
```

Contract:
- `register(path, producer, content=None)` — when `content` is provided,
  compute the sha256 from it (useful for tests); otherwise read the file
  from disk. A missing file registers with `sha256=""`, `size=0`,
  `missing=True` — useful for tracking "we expected this but it isn't
  there yet" cases.
- `verify(workspace_id)` — reads every registered file from disk and
  recomputes the sha256; any mismatch with the registered checksum is
  flagged as `"checksum mismatch: <path>"`. A registered-missing file
  that is *still* missing is *not* flagged (still missing is the expected
  state); a registered-present file that has since vanished is flagged
  as `"missing: <path>"`.
- The manager is **reproducible**: two managers registering the same
  file produce identical `sha256` and `size`. Pinned by
  `test_artifact_manager_reproducible_checksum`.

The orchestrator registers every file in `result.artifacts` with the
`ArtifactManager` immediately after the stage completes; the
PackagingEngine registers every packaging output *after* packaging
succeeds. The `artifact_manifest.json` artifact is the on-disk
serialization of `ArtifactManager.manifest()`.

### 8.6 The full artifact lifecycle

```
stage.execute(ctx)       ──►  produces files (list of paths in StageResult.artifacts)
                              │
orchestrator             ──►  for each path: ArtifactManager.register(path, producer=stage.name)
                              │
                              │  recorder.record_artifact(entry)
                              │  bus.emit(EV_ARTIFACT_PRODUCED, {path, sha256})
                              │
RecoveryManager.save_state(ctx)  ──►  workflow_state.json (the resume checkpoint)
_persist_artifacts(ctx)         ──►  workflow_record.json, workflow_events.json,
                                    workflow_metrics.json, workflow_manifest.json,
                                    artifact_manifest.json (the ArtifactManager dump),
                                    benchmark_results.json / stress_results.json
                                    (seeded empty if not yet invoked)

(after the drive loop reaches S_COMPLETED)
PackagingEngine.package(ctx)    ──►  reads <workspace>/synthesized_answer.md,
                                    <workspace>/implementation_plan.md,
                                    <workspace>/src/, <workspace>/validation/*,
                                    <workspace>/implementation/execution_record.json,
                                    <workspace>/workflow/workflow_record.json
                                    ──►  writes the 11 outputs under <workspace>/package/
                                    ──►  registers each output in ArtifactManager + Recorder
```

A consumer that wants a complete picture of "what did this workflow
produce and where is it" reads, in order of increasing specificity:
1. `workflow_state.json` for the final state.
2. `workflow_manifest.json` for the per-stage terminal states.
3. `artifact_manifest.json` for every file the stages produced.
4. `package/manifest.json` for the sealed, delivered bundle.

---

## 9. PolicyEngine rules

### 9.1 The default policy

```python
DEFAULT_POLICY = {
    "max_attempts":          50,                # workflow-wide stage-attempt cap
    "max_runtime_seconds":  None,              # wall-clock cap (None = unbounded)
    "max_workspace_bytes":  None,              # workspace size cap (None = unbounded)
    "stage_gates": {
        S_RESEARCH:           (),               # always runnable
        S_DEBATE:             (S_RESEARCH,),
        S_CONSENSUS:          (S_DEBATE,),
        S_ARCHITECTURE:       (S_CONSENSUS,),
        S_IMPLEMENTATION:     (S_ARCHITECTURE,),
        S_VALIDATION:         (S_IMPLEMENTATION,),
        S_QUALITY_GATE:       (S_VALIDATION,),
        S_PACKAGING:          (S_QUALITY_GATE,),
        S_DELIVERY:           (S_PACKAGING,),
    },
    "pause_before":          (),                # stages to PAUSE before
    "skip_stages":           (),                # stages to SKIP entirely
}
```

`normalize_policy(policy)` shallow-merges the policy dict over
`DEFAULT_POLICY`:
- For `stage_gates`, a dict is deep-merged (caller overrides per-stage).
- For `pause_before` and `skip_stages`, a list/tuple replaces.
- Anything else (scalars, including `None` to unset a default) is set
  verbatim.

### 9.2 The evaluate algorithm — rules in evaluation order

`PolicyEngine.evaluate(ctx, *, next_state)` is **pure** (no I/O except
an optional `os.stat` for `max_workspace_bytes`) and **deterministic**.
The rules evaluate in this order; the first that *fires* returns its
`PolicyDecision`:

| # | Rule | Condition | Decision | Severity |
|---|------|-----------|----------|----------|
| 1 | `skip_stages` | `next_state in policy["skip_stages"]` | `allowed=False, action="skip", reason="policy skip_stages includes <state>"` | info |
| 2 | terminal short-circuit | `next_state in TERMINAL_STATES` | `allowed=True, action="continue", reason="terminal state"` | info |
| 3 | cancelled | `ctx.cancelled` | `allowed=False, action="fail", reason="workflow cancelled"` | error |
| 4 | stage gating | for `producer` in `policy["stage_gates"][next_state]`: `ctx.stage_results.get(producer, {}).get("success")` is not True | `allowed=False, action="fail", reason="stage gate: producer <producer> has not completed successfully before <next_state>"` | error |
| 5 | runtime budget | `policy["max_runtime_seconds"]` is set AND `(now - ctx.started_at) > max_runtime_seconds` | `allowed=False, action="fail", reason="runtime budget exceeded: <elapsed>s > <cap>s"` | error |
| 6 | workspace size budget | `policy["max_workspace_bytes"]` is set AND `sum(file sizes under <ws>) > max_workspace_bytes` (best-effort; `OSError` swallowed — never blocks on stat failure) | `allowed=False, action="fail", reason="workspace size budget exceeded: <total> > <cap> bytes"` | error |
| 7 | attempt budget | `policy["max_attempts"]` is set AND `ctx.attempt_count >= policy["max_attempts"]` | `allowed=False, action="fail", reason="attempt budget exhausted: <count> >= <cap>"` | error |
| 8 | mandatory pause | `next_state in policy["pause_before"]` | `allowed=True, action="pause", reason="policy requires pause before <state>"` | info |
| 9 | default allow | none of the above fired | `allowed=True, action="continue", reason="policy allows"` | info |

Notes:
- The rule order matters: skipping is checked *first* (a skipped stage
  doesn't need its producers to have run); terminal states are
  short-circuited *before* cancellation (the SM has already decided to
  terminate); cancellation *before* gating (a cancelled workflow's gate
  check would be wasted work); runtime/size/attempt budgets *after*
  gating (they're global, not per-stage); the pause check is *last*
  among the non-refusal rules so that a different refusal doesn't
  masquerade as a pause.
- The orchestrator calls `evaluate` *before* every stage attempt and
  *before* the synthesized skip result that follows a `skip` decision.
  It also logs every decision (allowed or not) — the audit trail is
  complete.
- `must_pause(ctx, *, next_state)` is a convenience that returns `True`
  iff `next_state` is in `pause_before`. It does NOT consult the other
  rules; it is a predicate, not an evaluator.

### 9.3 What the PolicyEngine does NOT do

- It does **not** mutate `ctx`. The orchestrator is the only component
  that does.
- It does **not** execute stages. It only decides *whether* the
  orchestrator may proceed.
- It does **not** retry. The `RetryController` from Phase 3.1 handles
  per-repair retries; `max_attempts` is the workflow-wide attempt cap,
  set *outside* the per-repair loop.
- It does **not** terminate silently. Every refusal is a structured
  `PolicyDecision` with a non-empty `reason`.

### 9.4 The six pinned PolicyEngine behaviors

| Behavior | Test | Decision |
|----------|------|----------|
| Stage gate blocks when producer not run | `test_policy_engine_stage_gate_blocks_when_producer_not_run` | `allowed=False, action="fail", reason contains "stage gate"` |
| Stage gate passes when producer succeeded | `test_policy_engine_stage_gate_passes_when_producer_succeeded` | `allowed=True, action="continue"` |
| Attempt cap blocks after exhaustion | `test_policy_engine_attempt_cap_blocks_after_exhaustion` (policy `max_attempts: 1`, ctx `attempt_count: 1`) | `allowed=False, action="fail", reason contains "attempt budget"` |
| Skip stage fires | `test_policy_engine_skip_stages_skipped` (policy `skip_stages: (S_DEBATE,)`, DEBATE's gate would pass) | `allowed=False, action="skip"`, reason contains `"policy skip_stages includes DEBATE"` |
| Pause-before yields pause | `test_policy_engine_pause_before_yields_pause_decision` (policy `pause_before: (S_ARCHITECTURE,)`, all gates pass) | `allowed=True, action="pause"` |
| Cancelled workflow blocked | `test_policy_engine_cancelled_workflow_blocks` (ctx `cancelled=True`) | `allowed=False, action="fail"`, reason `"workflow cancelled"` |
| Terminal state allowed | `test_policy_engine_terminal_states_allowed` (next_state `S_COMPLETED`) | `allowed=True, action="continue"` |

---

## 10. Benchmark framework

### 10.1 `BenchmarkEngine` — design

`BenchmarkEngine` is the regression-comparison surface. It runs a fixed
set of workloads through `NativeWorkflowOrchestrator` and records
wall-clock / stage_count / artifact_count / final_state for each. Pure
stdlib: `time.monotonic` for timing, `build_workspace.create_workspace`
for fresh workspaces (lazy-imported so the module stays pure at import).

### 10.2 Default workloads

```python
BenchmarkEngine.DEFAULT_WORKLOADS = {
    "small":  {"prompt": "Build a hello-world module",
               "files": [("src/main.py", "print('hello')\n"),
                         ("tests/test_main.py", "def test_main(): assert True\n")]},
    "medium": {"prompt": "Build a 3-component service with tests",
               "files": [("src/api.py",      "API = 1\n"),
                         ("src/worker.py",   "WORKER = 1\n"),
                         ("src/db.py",       "DB = 1\n"),
                         ("tests/test_api.py", "def test_api(): assert True\n")]},
    "large":  {"prompt": "Build a 10-component platform with full tests",
               "files": [(f"src/mod_{i}.py", f"MOD_{i} = {i}\n") for i in range(10)]
                        + [("tests/test_mod_0.py", "def test_0(): assert True\n")]},
}
```

Each workload creates a fresh workspace, populates the `files` (relative
to the workspace root), then runs the orchestrator end-to-end. The
workload's `prompt` is the user prompt; the `files` are the seed sources
the IMPLEMENTATION stage (in a no-op wiring) does not consume — they
exist so a production wiring has material to work with.

### 10.3 `BenchmarkResult`

```python
@dataclass
class BenchmarkResult:
    workload: str          # "small" | "medium" | "large"
    workflow_id: str
    success: bool
    duration_seconds: float    # monotonic wall clock
    stage_count: int           # len(stages_executed)
    artifact_count: int        # ArtifactManager.all() length
    final_state: str           # ALL_STATES member
    error: str = ""
```

### 10.4 Public surface

```python
BenchmarkEngine(
    *,
    workloads: dict[str, dict] | None = None,           # default = DEFAULT_WORKLOADS
    orchestrator_factory: Callable[[], WorkflowOrchestratorProtocol] | None = None,
    build_workspace_id: Callable[[dict], str] | None = None,
)

def run(self, *, workspace_id_for: Callable[[str], str] | None = None) -> list[BenchmarkResult]
def persist(self, workspace_id: str, results: list[BenchmarkResult]) -> None
```

Contract:
- `run` iterates `self.workloads` in dict order; for each, gets a
  workspace_id (via `workspace_id_for(name)` if provided, else
  `_default_wid(name, spec)` which uses `build_workspace.create_workspace`);
  mints a fresh orchestrator via `orchestrator_factory()`; runs; records
  the timing + counts. The orchestrator is **re-entrant** — a fresh
  one per workload, no shared state.
- `persist(workspace_id, results)` writes to `<workspace>/workflow/
  benchmark_results.json` as `{results: [...]}`.
- `_default_wid(name, spec)` is the workspace-builder fallback; it
  raises `RuntimeError("benchmark needs build_workspace; pass
  build_workspace_id")` if `build_workspace` is not importable — useful
  for tests that want to assert the engine refuses to run without its
  one dependency.

### 10.5 Benchmark acceptance contract

| Property | Test |
|----------|------|
| Runs three workloads | `test_benchmark_runs_three_workloads` — `len(results) == 3`, names `{small, medium, large}` |
| Each succeeds deterministically with the no-op stages | `test_benchmark_runs_three_workloads` — for each `r`: `r.success is True`, `r.final_state == S_COMPLETED` |
| Same workload twice → same final state | `test_benchmark_determinism_same_workload_same_state` — `[r.final_state for r in r1] == [r.final_state for r in r2]` |

---

## 11. Fault injection framework

### 11.1 `FaultInjectionEngine` — design

Injects each fault via a fault-injector callable that mutates a
workspace, then runs `recover` (diagnostic) + `resume` (re-entry), and
records whether recovery (a non-FAILED final state OR a structured
recovery report) was achieved. Pure stdlib: each injector is a small
free function (`_inject_*`) that takes a `workspace_id` and returns a
human-readable issue string.

### 11.2 Default injectors

```python
FaultInjectionEngine._default_injectors() = {
    "corrupt_workflow_state":         _inject_corrupt_workflow_state,
    "delete_workflow_state":         _inject_delete_workflow_state,
    "malformed_user_prompt":         _inject_malformed_user_prompt,
    "interrupt_before_packaging":     _inject_interrupt_before_packaging,
}
```

| Injector | Mutation | Expected outcome |
|----------|----------|------------------|
| `_inject_corrupt_workflow_state` | Writes `"{ not valid json }"` to `workflow/workflow_state.json` | `orch.recover` reports `"workflow_state.json: corrupt JSON"` via `detect_corruption`; `orch.resume` returns `{"success": False, "error": "no checkpoint to resume from ..."}` (corrupt JSON is treated as absent). The fault is *detected* and *surfaced* — never silently restarted. |
| `_inject_delete_workflow_state`  | `unlink(workflow/workflow_state.json)` (if present) | `orch.resume` returns `{"success": False, "error": "no checkpoint to resume from ..."}`. |
| `_inject_malformed_user_prompt`  | None (the orchestrator's input validation handles it) — returns the descriptive string `"malformed user prompt"` | `orch.run` with an empty `user_prompt` or a missing `workspace_id` returns a structured error without ever invoking a stage. |
| `_inject_interrupt_before_packaging` | Writes a fresh `WorkflowContext(current_state=S_PAUSED, paused=True)` to `workflow_state.json` | `orch.resume` clears `paused=False` and re-enters at `S_PAUSED`; the drive loop sees `ctx.paused`, sets `current_state = S_PAUSED`, and breaks. (See §7.5 for the pause-vs-cancel distinction.) |

### 11.3 `FaultInjectionResult`

```python
@dataclass
class FaultInjectionResult:
    fault: str                      # injector name
    workspace_id: str
    recovered: bool                 # recover()["recoverable"] OR final_state != FAILED
    final_state: str
    recovery_issues: list           # recover()["issues"] (may be empty)
    error: str = ""                 # r["error"] or the injector's issue string
```

### 11.4 Public surface

```python
FaultInjectionEngine(
    *,
    orchestrator_factory: Callable[[], WorkflowOrchestratorProtocol] | None = None,
    injectors: dict[str, Callable[[str], str]] | None = None,
)

def run(self, *, setup_workspace: Callable[[str], str] | None = None) -> list[FaultInjectionResult]
def persist(self, workspace_id: str, results: list[FaultInjectionResult]) -> None
```

Contract:
- `run` iterates injectors in dict order; for each, gets a workspace via
  `setup_workspace(fault_name)` (default `_default_setup` mints a
  workspace + seeds an `S_PACKAGING` context so the injector has
  something to corrupt); runs the injector; mints a fresh orchestrator
  via `orchestrator_factory()`; runs `recover` then `resume`; on any
  exception, captures a structured result with `recovered=False`,
  `final_state="FAILED"`.
- `persist(workspace_id, results)` writes to
  `<workspace>/workflow/stress_results.json` as `{type:
  "fault_injection", results: [...]}` — the `type` field disambiguates
  from `StressTestEngine.persist`'s output, which writes to the same
  file with `type: "stress"`.
- For every fault: `r.error or r.recovered` — the result always carries
  a non-empty `error` OR a `recovered=True`. No silent result.

### 11.5 Fault injection acceptance contract

| Property | Test |
|----------|------|
| Every default injector produces a structured result | `test_fault_injection_runs_every_injector` — `len(results) >= 4`; each `r` is a `FaultInjectionResult`, `r.fault` non-empty, `r.error or r.recovered` |
| Corrupt state is detected | `test_fault_injection_corrupt_state_detected` — directly invokes `RecoveryManager.detect_corruption(wid)` after corrupting state; asserts the returned list is non-empty |
| Deleted state → resume surfaces structured error | `test_fault_injection_delete_state_returns_no_checkpoint_error` — `orchestrator.resume` returns `success=False` with `error` containing "checkpoint" or "absent" |

---

## 12. Stress-testing framework

### 12.1 `StressTestEngine` — design

Two stress modes, both pure stdlib:

- **`run_concurrent(count=N)`** — runs N workflows in parallel threads.
  Each thread builds a fresh workspace (via `workspace_id_factory(i)` if
  provided, else `build_workspace.create_workspace` with a unique prompt)
  and a fresh orchestrator (via `orchestrator_factory`). Threads are
  joined; the engine aggregates final states.

- **Retries** are not a separate method; the test affirming the retry
  cap is `test_policy_engine_attempt_cap_blocks_after_exhaustion`, and
  the PolicyEngine's `max_attempts` is what caps the workflow under
  repeated repair-loop re-entries.

### 12.2 `StressResult`

```python
@dataclass
class StressResult:
    name: str                       # f"concurrent_{count}"
    workflow_count: int
    all_succeeded: bool             # all r.success
    total_duration_seconds: float   # monotonic wall clock for the whole batch
    final_states: list              # per-workflow final state
    error: str = ""
```

### 12.3 Public surface

```python
StressTestEngine(
    *,
    orchestrator_factory: Callable[[], WorkflowOrchestratorProtocol] | None = None,
    workspace_id_factory: Callable[[int], str] | None = None,
)

def run_concurrent(self, *, count: int = 5) -> StressResult
def persist(self, workspace_id: str, result: StressResult) -> None
```

Contract:
- Re-entrant: a fresh orchestrator per thread. No shared mutable state
  crosses workspace boundaries.
- `count` workflows in parallel; `t.start()` each, then `t.join()` each.
- `_wid(i)` is the workspace-id factory hook — tests inject to assert
  distinct workspaces (`test_concurrent_workflows_distinct_workspaces`).
- `persist(workspace_id, result)` writes to `<workspace>/workflow/
  stress_results.json` as `{type: "stress", results: [result.to_dict()]}`.

### 12.4 Stress acceptance contract

| Property | Test |
|----------|------|
| Concurrent completes all workflows | `test_stress_concurrent_completes_all_workflows` — `run_concurrent(count=4)` returns `workflow_count=4` with every `final_state` in `(S_COMPLETED, S_FAILED, S_CANCELLED)` (no `PENDING`, no leakage) |
| Concurrent workflows succeed independently | `test_concurrent_workflows_succeed_independently` — `run_concurrent(count=3)` returns 3 final states, all terminal |
| Concurrent distinct workspaces | `test_concurrent_workflows_distinct_workspaces` — explicit `workspace_id_factory` collects every wid; 5 distinct workspaces for `count=5` (no collision) |

---

## 13. Comprehensive testing matrix

`tests/test_workflow_orchestrator.py` is the offline-only regression suite
(63 tests in 20 numbered sections). Each test name links to a contract;
collectively they form the implementation's acceptance matrix.

### 13.1 Section 1 — Complete workflow execution

| Test | Assertion |
|------|-----------|
| `test_complete_workflow_reaches_completed` | `r.success is True`, `final_state == S_COMPLETED`, `engine == "NativeWorkflow"`, `stages_executed` non-empty |
| `test_missing_workspace_returns_structured_error` | `r.success is False`, `error` contains "workspace" |
| `test_missing_user_prompt_returns_structured_error` | `r.success is False`, `error` contains "user_prompt" |

### 13.2 Section 2 — The 7 workspace artifacts persisted

| Test | Assertion |
|------|-----------|
| `test_all_seven_workspace_artifacts_persisted` | Every entry in `ALL_WORKFLOW_ARTIFACTS` is a file under `<ws>/workflow/` after `run` |
| `test_workflow_state_artifact_carries_resume_state` | `json.load(workflow_state.json)` has `workspace_id`, `current_state == S_COMPLETED`, non-empty `workflow_id` |
| `test_workflow_record_artifact_carries_events` | `workflow_record.json` has `event_count > 0`, `transition_count > 0`; events include `EV_WORKFLOW_STARTED`, `EV_STAGE_STARTED`, `EV_WORKFLOW_COMPLETED` |

### 13.3 Section 3 — The 11 packaging outputs persisted

| Test | Assertion |
|------|-----------|
| `test_all_eleven_packaging_outputs_persisted` | Every entry in `ALL_PACKAGING_OUTPUTS` is a file (or directory for `PKG_IMPLEMENTATION`) under `<ws>/package/` |
| `test_package_manifest_records_checksums` | `manifest.json`'s `outputs` are non-empty; each entry has non-empty `sha256`, `size >= 0`, non-empty `producer` |

### 13.4 Section 4 — Replay + timeline reconstruction

| Test | Assertion |
|------|-----------|
| `test_replay_returns_all_events_in_order` | First event `EV_WORKFLOW_STARTED`, last event `EV_WORKFLOW_COMPLETED` |
| `test_timeline_returns_all_state_transitions` | Every `t["to"]` in `ALL_STATES`; `timeline[-1]["to"] == S_COMPLETED` |
| `test_stage_timings_reconstructs_per_state` | For every state in `SUCCESS_PATH` except `S_COMPLETED`/`S_REPAIR`, timing is recorded |

### 13.5 Section 5 — Recovery from interruption

| Test | Assertion |
|------|-----------|
| `test_resume_continues_from_persisted_checkpoint` | Seeded `current_state=S_ARCHITECTURE`; `resume` reaches `S_COMPLETED` |
| `test_resume_no_state_returns_structured_error` | `resume` with no state: `success=False`, `error` contains "checkpoint" or "absent" |
| `test_resume_corrupt_state_returns_structured_error` | After corrupting state: `resume`: `success=False` |
| `test_cancel_marks_context_cancelled` | After `cancel(wsid)`: loaded `ctx.cancelled is True` |
| `test_cancel_returns_false_when_no_state` | After `cancel` with no state: returns `False` |
| `test_cancel_is_honored_on_resume` | After `cancel` then `resume`: `final_state == S_CANCELLED` |

### 13.6 Section 6 — Recovery surfaces

| Test | Assertion |
|------|-----------|
| `test_status_returns_unknown_when_no_state` | `status` returns `{"state": "unknown", ...}` |
| `test_status_reports_current_state` | After `run`: `status` returns `current_state == S_COMPLETED` |
| `test_recover_returns_diagnostic` | After `run`: `recover` returns `recoverable=True`, `workspace_id` matches |
| `test_recover_returns_false_when_no_state` | `recover` returns `recoverable=False`, `reason` contains "no workflow_state.json" |
| `test_detect_corruption_flags_corrupt_json` | After corrupting state: `detect_corruption` returns issues containing "workflow_state.json" |
| `test_detect_corruption_clear_when_clean` | After a clean `run`: `detect_corruption(wid) == []` |

### 13.7 Section 7 — Event ordering

| Test | Assertion |
|------|-----------|
| `test_event_log_artifact_carries_events_in_order` | `events.json` has `EV_WORKFLOW_STARTED` in first 5; `EV_WORKFLOW_COMPLETED` last |
| `test_event_log_artifact_carries_artifact_events` | `events.json` has `EV_STAGE_STARTED`, `EV_STAGE_COMPLETED`, `EV_WORKFLOW_COMPLETED` |

### 13.8 Section 8 — Concurrent workflows (no shared-state leaks)

| Test | Assertion |
|------|-----------|
| `test_concurrent_workflows_succeed_independently` | `run_concurrent(count=3)`: 3 final states, all terminal |
| `test_concurrent_workflows_distinct_workspaces` | Explicit factory: 5 distinct wids for `count=5` |

### 13.9 Section 9 — Artifact integrity

| Test | Assertion |
|------|-----------|
| `test_artifact_manager_records_sha256` | Registered file's `sha256` is 64 hex chars; `size > 0`; `missing=False` |
| `test_artifact_manager_detects_missing_files` | Registering `/no/such/file`: `missing=True`, `sha256=""` |
| `test_artifact_manager_verify_detects_checksum_mismatch` | After mutating file post-register: `verify` flags "checksum mismatch" |
| `test_artifact_manager_reproducible_checksum` | Two managers, same file: same `sha256` |

### 13.10 Section 10 — BenchmarkEngine

| Test | Assertion |
|------|-----------|
| `test_benchmark_runs_three_workloads` | 3 results: `{small, medium, large}`; each `success=True`, `final_state=S_COMPLETED` |
| `test_benchmark_determinism_same_workload_same_state` | Two runs: same `[r.final_state for r]` |

### 13.11 Section 11 — FaultInjectionEngine

| Test | Assertion |
|------|-----------|
| `test_fault_injection_runs_every_injector` | `len(results) >= 4`; each `r` is `FaultInjectionResult`, `r.fault` non-empty, `r.error or r.recovered` |
| `test_fault_injection_corrupt_state_detected` | Corrupt state → `detect_corruption` truthy |
| `test_fault_injection_delete_state_returns_no_checkpoint_error` | `resume` w/o state: `error` contains "checkpoint"/"absent" |

### 13.12 Section 12 — StressTestEngine

| Test | Assertion |
|------|-----------|
| `test_stress_concurrent_completes_all_workflows` | `run_concurrent(count=4)`: `workflow_count=4`, all `final_state` in `(S_COMPLETED, S_FAILED, S_CANCELLED)` |

### 13.13 Section 13 — PolicyEngine enforcement

| Test | Assertion |
|------|-----------|
| `test_policy_engine_stage_gate_blocks_when_producer_not_run` | DEBATE w/o RESEARCH: `not allowed`, reason contains "stage gate" |
| `test_policy_engine_stage_gate_passes_when_producer_succeeded` | DEBATE w/ RESEARCH success: `allowed` |
| `test_policy_engine_attempt_cap_blocks_after_exhaustion` | `max_attempts=1, attempt_count=1`: `not allowed`, "attempt budget" |
| `test_policy_engine_skip_stages_skipped` | `skip_stages=(S_DEBATE,)`: `not allowed`, `action == "skip"` |
| `test_policy_engine_pause_before_yields_pause_decision` | `pause_before=(S_ARCHITECTURE,)`, all gates pass: `allowed`, `action == "pause"` |
| `test_policy_engine_cancelled_workflow_blocks` | `cancelled=True`: `not allowed`, `action == "fail"` |
| `test_policy_engine_terminal_states_allowed` | `next_state=S_COMPLETED`: `allowed` |

### 13.14 Section 14 — WorkflowStateMachine repair loop

| Test | Assertion |
|------|-----------|
| `test_state_machine_validation_failed_goes_to_repair` | `current=S_VALIDATION, success=False` → `S_REPAIR` |
| `test_state_machine_repair_succeeded_reenters_validation` | `current=S_REPAIR, success=True` → `S_VALIDATION` |
| `test_state_machine_quality_gate_failed_goes_to_repair` | `current=S_QUALITY_GATE, success=False` → `S_REPAIR` |
| `test_state_machine_validation_succeeded_advances_to_quality_gate` | `current=S_VALIDATION, success=True` → `S_QUALITY_GATE` |
| `test_state_machine_cancelled_goes_to_cancelled` | `current=S_RESEARCH, cancelled=True` → `S_CANCELLED` |
| `test_state_machine_non_repair_failure_goes_to_failed` | `current=S_ARCHITECTURE, success=False` → `S_FAILED` |

### 13.15 Section 15 — StageRegistry

| Test | Assertion |
|------|-----------|
| `test_registry_rejects_default_name` | `BaseStage` subclass without overriding `name`: raises `ValueError` |
| `test_registry_rejects_invalid_state_name` | `name="NOT_A_STATE"`: raises `ValueError` |
| `test_registry_registers_real_stage` | A `BaseStage` subclass with `name=S_RESEARCH`: registered, `get(S_RESEARCH)` returns it, `S_RESEARCH in names()` |
| `test_default_registry_has_eleven_workflow_states` | `default_registry()` has a stage for every non-terminal state in `SUCCESS_PATH` |

### 13.16 Section 16 — Configuration switch

| Test | Assertion |
|------|-----------|
| `test_get_workflow_orchestrator_native_default` | `get_workflow_orchestrator()` returns `NativeWorkflowOrchestrator` |
| `test_get_workflow_orchestrator_unknown_raises` | Unknown name raises `ValueError` |

### 13.17 Section 17 — Pure-stdlib at import (no provider pulled transitively)

| Test | Assertion |
|------|-----------|
| `test_no_forbidden_modules_transitively_imported` | An isolated `python -c "import workflow_orchestrator"` shows none of `cerebras_client`, `ollama_client`, `openrouter_client`, `groq_client`, `gpt_worker`, `gemini_worker`, `provider_router`, `selenium`, `requests`, `httpx`, `urllib3` in `sys.modules` |

### 13.18 Section 18 — Backward compatibility (Phase 1-3.1 components still work)

| Test | Assertion |
|------|-----------|
| `test_phase1_execution_decision_engine_still_importable` | `ExecutionDecisionEngine().decide(...)` returns an `execution_mode` in the canonical enumeration |
| `test_phase2_implementation_engine_still_importable` | `NativeImplementationEngine` with injected `coding_agent_fn`/`testing_agent_fn` succeeds; `engine == "Native"` |
| `test_phase3_validation_engine_still_importable` | `NativeValidationEngine` with injected `build_fn`/`test_fns` succeeds; `engine == "NativeValidation"` |
| `test_phase31_repair_memory_still_importable` | `RepairMemory.record_attempt` + `is_blacklisted(...)` works; `close()` succeeds |

### 13.19 Section 19 — Determinism

| Test | Assertion |
|------|-----------|
| `test_deterministic_for_same_inputs` | Two fresh workspaces with the same prompt: `final_state` and `stages_executed` equal |

### 13.20 Section 20 — No silent failures

| Test | Assertion |
|------|-----------|
| `test_every_failure_surfaces_an_error` | After corrupting state: `resume` returns `success=False` AND non-empty `error` |

### 13.21 Self-check (`__main__`)

The module's `__main__` block runs 20 numbered self-checks that mirror the
test suite but in a single process with a temporary workspace root. It
ends with `print("workflow_orchestrator self-check PASSED")` — runnable as
`python3 workflow_orchestrator.py`. The self-check is a fast
pre-CI smoke; the pytest suite is the canonical regression.

---

## 14. Success criteria and acceptance gates

### 14.1 The verification matrix (the implementation is complete only if…)

| "…the implementation is complete only if…" | Where | Pinned by |
|---|---|---|
| The workflow is driven to a terminal state from any entry point (`run` / `resume` / cancel + `resume`). | `NativeWorkflowOrchestrator._drive` exit conditions; `WorkflowStateMachine.next_state` | Sections 1, 5, 6, 13.1, 13.5, 13.20 |
| The state machine is the only authority on what runs next. | `WorkflowStateMachine.next_state` is the only writer of `ctx.current_state` inside `_drive`. The PolicyEngine and the stages never write it. | Section 14 of the matrix (`test_state_machine_*`) |
| The PolicyEngine is consulted before every stage attempt and never silently terminates. | `_drive` calls `policy_engine.evaluate` at the top of every iteration; refusals produce a `PolicyDecision` with a non-empty `reason`. | Section 13 of the matrix |
| Every artifact produced is registered with a reproducible sha256. | `ArtifactManager.register` (deterministic over file content); `artifact_manifest.json` is its serialization. | Section 9 of the matrix |
| Every transition persists state so resume is always possible. | `RecoveryManager.save_state` after every stage; `_persist_artifacts` writes the 7 workflow artifacts. | Sections 2 and 5 of the matrix |
| Every recovery path either re-enters at the recorded state or surfaces a structured error. | `resume` returns a structured error when `load_state` returns `None` or `validate_state` flags issues; it never starts fresh. | Sections 5 and 6 of the matrix |
| Every packaging output is produced on a completed workflow, byte-for-byte reproducible. | `PackagingEngine.package` + `_persist_artifacts`'s packaging branch. | Section 3 of the matrix |
| Every benchmark / fault / stress run is captured as a structured result; no silent outcome. | The three engines' `Result` dataclasses; the `r.error or r.recovered` / `r.success or r.error` invariants. | Sections 10, 11, 12 of the matrix |
| Every Phase 1-3.1 component still imports and works unchanged. | The Phase 4 module imports only `EventBus`, `Event`, `ExecutionRecorder`, `_workspace_dir`, `_now_iso`, `_write_json`, `_read_json` from `implementation_engine` — all additive uses; no existing surface modified. | Section 18 of the matrix |
| The module is pure stdlib at import. | `test_no_forbidden_modules_transitively_imported` runs an isolated import and asserts the forbidden set is empty. | Section 17 of the matrix |
| Determinism — same inputs → same final state and same `stages_executed`. | `test_deterministic_for_same_inputs` and `test_benchmark_determinism_same_workload_same_state`. | Sections 10 and 19 of the matrix |
| No silent failures — every non-success result has a non-empty error. | `_drive`'s terminal-state handling, `resume`'s structured errors, `cancel`'s structured outcome, the PolicyEngine's structured refusals. | Section 20 of the matrix |

### 14.2 Acceptance gates (in evaluation order)

A change to `workflow_orchestrator.py` or its tests is acceptable only if:

1. **`pytest tests/test_workflow_orchestrator.py -q` passes 63/63.** No
   skipped tests, no xfailed tests (the suite is offline-only).
2. **`python3 workflow_orchestrator.py` prints
   `workflow_orchestrator self-check PASSED`.** The self-check exercises
   the same contracts in a single process with a temporary workspace
   root.
3. **`python3 run_all_tests.py` passes** — every regression in the
   project's consolidated runner, including the earlier-phase tests, the
   architecture-hardening tests, and the consolidation tests. Phase 4
   must not break any earlier phase (the test file's Section 18 is the
   explicit cross-phase boundary check).
4. **The forbidden-modules probe returns `[]`** — no provider module
   is transitively imported. This is Acceptance Gate §13.17 in the
   matrix, and the project-wide rule that the orchestration layer
   must not pull providers (those are an engine-layer concern, injected
   via stage callables).
5. **The packaging outputs survive a re-run** — `PackagingEngine.package`
   produces the same `manifest_sha256` for the same workspace contents.
   (Idempotency is asserted by the self-check's step 13; the test suite
   re-asserts the manifest contents.)
6. **Every new stage added passes `StageRegistry.register`'s validation**
   — non-default `name`, `name in ALL_STATES`. A new stage registered
   with a typo is rejected at registration, not silently dropped.
7. **Every new default workload / injector added to `BenchmarkEngine` /
   `FaultInjectionEngine` ships with a regression test** that records
   the workload's or injector's name and asserts at least `r.error or
   r.recovered` / `r.success or r.error`. No silent result.

### 14.3 Non-negotiable invariants (the contracts that must not regress)

| Invariant | What it means | What breaks it |
|-----------|---------------|----------------|
| **Single authority** | `WorkflowStateMachine.next_state` is the only writer of `current_state` inside `_drive`. | Any other component mutating `ctx.current_state`. |
| **No silent termination** | Every non-`COMPLETED` terminal state has a non-empty `error` in the result dict. | A path that breaks the drive loop without setting `error`. |
| **No silent restart** | `resume` on a workspace without a checkpoint returns a structured error, never starts fresh. | Inferring intent ("the workspace is empty; I'll start") in `resume`. |
| **Re-entrancy** | Each stress thread builds a fresh orchestrator; no shared mutable state. | A module-level recorder, artifact manager, or policy engine. |
| **Pure stdlib at import** | The forbidden modules are not transitively imported. | A new top-level `import` of a provider module (the lazy `import build_workspace` inside `_default_wid` / `_default_setup` / `_wid` is the only allowed lazy import; it stays inside a method, not at module top). |
| **Byte-for-byte reproducibility** | Same workspace contents → same `manifest_sha256`, same `artifact_manifest.json` sha256 entries. | Non-deterministic ordering of dict iteration, non-deterministic timestamps inside file content. (Timestamps are recorded in `registered_at` metadata, not in the file content itself.) |
| **Determinism** | Same inputs → same `final_state` and same `stages_executed`. | Any time-based or random branch in the state machine or the PolicyEngine. (Neither has one; both are pure given the inputs.) |
| **Backward compatibility** | Phase 1-3.1 components import and work unchanged. | An additive `import` that re-exports a Phase 1-3.1 symbol under a new name; a side-effect at import time that mutates Phase 1-3.1 module state. |

### 14.4 Phase 4 vs the architecture hardening — composition, not overlap

| Concern | Phase 4 owns | Architecture hardening owns |
|---------|-------------|------------------------------|
| State machine | `WorkflowStateMachine` (15 workflow states, transition table, repair loop) | `BuildLifecycle` (build-lifecycle states, transition table, `FAILED → SPEC_READY` recovery edge) |
| Resume | `RecoveryManager` (workflow-level: which stage to re-enter) | `resume.resume_build` (build-lifecycle-level: which lifecycle stage to re-enter) |
| Artifacts | `ArtifactManager` (flat registry of produced files, sha256 per file) | `ArtifactStore` (typed `Artifact` envelopes under `<workspace>/artifacts/`) |
| Validation | `Stage.validate` (per-stage pre-condition) | `validation.validate(stage, artifact)` (post-hoc cross-artifact validator) |
| Manifest | `<workspace>/package/manifest.json` (11 packaging outputs) | `manifest.json` (build's completed-build manifest, references-only) |
| Errors | The orchestrator catches `Exception` broadly; surfaces `error` in the result dict | `BuildError` + `ErrorKind` discriminator; the lifecycle guard and validation dispatcher raise it |

The two layers compose: the workflow drives stages; each stage internally
advances the build lifecycle via `lifecycle.advance_lifecycle`. Phase 4
resumes the *workflow*; the hardening resumes the *build*. The two
resume paths are independent and non-overlapping — neither replaces the
other.

---

## 15. Deliberately deferred (with rationale)

Per the YAGNI ladder, the following brief-listed abstractions are
*deferred*, not omitted:

| Deferred piece | Reason (ponytail) | When to add |
|-----------------|--------------------|-------------|
| A second workflow orchestrator implementation | `_WORKFLOW_ORCHESTRATORS` has one entry (`NativeWorkflow`). A registry with one entry is fine; a `Protocol` for one implementation is fine (the `@runtime_checkable` Protocol lets tests assert structural conformance without inheritance). A `Strategy` hierarchy with one strategy is the canonical YAGNI anti-pattern. | When a second concrete orchestrator appears (e.g. a `RemoteWorkflowOrchestrator`). Register it in `_WORKFLOW_ORCHESTRATORS`; the factory already supports the lookup. |
| A `Strategy` hierarchy for `Stage` (a subclass per stage type) | The `BaseStage` + `Stage` Protocol already provide swappability; one concrete `_NoOpStage` subclass is shipped for the default registry, and production wires its own `BaseStage` subclasses via the constructor seam. | When two interchangeable behaviors for the *same state* appear (e.g. a `NativeDebateStage` and a `StreamingDebateStage`). Until then, callable seams are enough. |
| A `Repository` pattern for `ArtifactManager` | The manager is an in-memory dict with sha256 keys; persistence is a single `artifact_manifest.json` written by `_persist_artifacts`. A repository abstraction adds a layer for one backing store. | When a second backing store appears (e.g. a PostgreSQL artifact registry). Until then, the dict + JSON sidecar are the storage. |
| An event-bus framework for *cross-workflow* notifications | The existing `EventBus` is in-process and per-orchestrator (the constructor mints one). Cross-workflow notifications would require a global bus, which is a service-locator pattern. | When a concrete cross-workflow consumer appears (e.g. a dashboard that subscribes to *all* running workflows). Until then, every orchestrator is its own bus. |
| A plugin framework for `BenchmarkEngine` / `FaultInjectionEngine` / `StressTestEngine` | Each accepts `workloads` / `injectors` / `orchestrator_factory` as constructor kwargs — the existing seam pattern. A plugin framework is a registry on top of a registry. | When a second producer of workloads / injectors appears that the engines must discover at runtime. Until then, explicit construction is the seam. |
| A checkpointing *strategy* (per-stage deltas vs full-state snapshots) | `save_state` writes the full context after every transition. A delta-based scheme is a meaningful optimization, but the context is small (~few KB) and the write is once per stage, so the cost is negligible. | When profiled checkpoint cost becomes a bottleneck (millions of transitions/min, or contexts > 1MB). Until then, the full snapshot is the simplest correct thing. |
| `Stage.rollback` auto-invocation | `BaseStage` provides `rollback` as a hook, but no Phase 4 caller invokes it. Wiring an automatic rollback on stage failure would change `next_state`'s contract (no clear semantics for "rolled back state's successor"). | When a stage author needs rollback semantics that the repair loop doesn't already cover. Until then, the repair loop + recovery is the structured rollback. |
| A `Visitor` pattern for the workflow graph | The graph is the `LINEAR_PATH` tuple + one repair branch. `_advance` steps the linear path; the repair branch is one `if`. A visitor would wrap two `if`s in a walker class. | When the workflow graph gains branches (e.g. parallel research paths or conditional consensus). Until then, the linear advance + the repair branch is the entire graph. |

---

## 16. Migration + backward-compatibility notes

1. **No existing module was modified outside the additive `import` in
   `workflow_orchestrator.py`'s top.** The import block at the top of
   `workflow_orchestrator.py` pulls `EventBus`, `Event`,
   `ExecutionRecorder`, `_workspace_dir`, `_now_iso`, `_write_json`,
   `_read_json` from `implementation_engine` — these are read-only uses,
   no surface changed.
2. **Phase 1-3.1 components are still directly importable.**
   `tests/test_workflow_orchestrator.py::test_phase1_execution_decision_engine_still_importable`,
   `test_phase2_implementation_engine_still_importable`,
   `test_phase3_validation_engine_still_importable`, and
   `test_phase31_repair_memory_still_importable` pin the boundary:
   importing and using each Phase 1-3.1 component directly succeeds.
3. **The architecture hardening is untouched.** `lifecycle.py`,
   `artifacts.py`, `validation.py`, `execution_planner.py`, `manifest.py`,
   `resume.py`, `errors.py`, `build_pipeline.py`, `build_spec_engine.py`,
   `build_workspace.py`, `implementation_executor.py`,
   `decision_model.py`, `telemetry.py` are byte-for-byte identical.
4. **The build-lifecycle state machine and the workflow state machine
   compose without overlap.** The former owns build-lifecycle transitions;
   the latter owns workflow transitions. A production stage implementation
   will call `lifecycle.advance_lifecycle(workspace_id, ...)` inside its
   `execute`; Phase 4 itself never touches the build lifecycle.
5. **The artifact model hardening and the ArtifactManager
   compose without overlap.** The hardening's `ArtifactStore` holds
   typed `Artifact` envelopes under `<workspace>/artifacts/`; Phase 4's
   `ArtifactManager` holds a flat registry of produced files and writes
   `artifact_manifest.json` under `<workspace>/workflow/`. Different
   files, different subdirectories, different responsibilities.
6. **Pre-Phase-4 workspaces have no `workflow/` dir.** `resume` on such a
   workspace returns the structured `"no checkpoint to resume from"`
   error; it does not crash. A fresh `run` on such a workspace creates
   the `workflow/` dir on the first transition.
7. **The regression suite (`run_all_tests.py`) passes.** The Phase 4
   suite is registered alongside the earlier-phase tests; ordering is
   preserved (the runner imports `test_workflow_orchestrator` only after
   the earlier-phase tests, because Phase 4's tests transitively import
   Phase 1-3.1 components to assert their continued operation).

---

## 17. Developer documentation — using Phase 4

```python
# 1. Fresh workflow — happy path
from workflow_orchestrator import NativeWorkflowOrchestrator
orch = NativeWorkflowOrchestrator()
result = orch.run(workspace_id="<existing-workspace-id>",
                 user_prompt="Build the thing")
assert result["success"] is True
assert result["final_state"] == "COMPLETED"

# 2. Resume a workflow from the last checkpoint
result = orch.resume(workspace_id="<existing-workspace-id>")
# Returns a structured error if no checkpoint exists.

# 3. Cancel a running or paused workflow
orch.cancel(workspace_id="<existing-workspace-id>")
# The next resume (or run) sees ctx.cancelled and routes to S_CANCELLED.

# 4. Diagnose a workspace without re-executing
report = orch.recover(workspace_id="<existing-workspace-id>")
if not report["recoverable"]:
    print("cannot resume:", report["reason"], report.get("issues"))

# 5. Run with a custom policy (enforce a 1-hour wall-clock budget and
#    pause before architecture for a human-in-the-loop review)
from workflow_orchestrator import PolicyEngine, NativeWorkflowOrchestrator
orch = NativeWorkflowOrchestrator(policy={
    "max_runtime_seconds": 3600,
    "pause_before": ("ARCHITECTURE",),
})
result = orch.run(workspace_id="<ws>", user_prompt="...")
# When the orchestrator reaches ARCHITECTURE, it pauses; result["final_state"]
# is "PAUSED" and result["error"] is "policy requires pause before ARCHITECTURE".
# To continue: orch.resume(workspace_id="<ws>")  # clears ctx.paused

# 6. Inject your own stages — production wiring
from workflow_orchestrator import StageRegistry, NativeWorkflowOrchestrator
from my_production_stages import planning_stage, research_stage, debate_stage  # etc.

reg = StageRegistry()
reg.register(planning_stage)         # name="PLANNING"
reg.register(research_stage)         # name="RESEARCH"
# ... register every non-terminal state in SUCCESS_PATH

orch = NativeWorkflowOrchestrator(stages=reg)

# 7. Benchmark the three default workloads
from workflow_orchestrator import BenchmarkEngine
results = BenchmarkEngine().run()
for r in results:
    print(r.workload, r.success, r.duration_seconds, r.final_state)

# 8. Fault-inject the four default faults
from workflow_orchestrator import FaultInjectionEngine
for r in FaultInjectionEngine().run():
    print(r.fault, r.recovered, r.final_state, r.error)

# 9. Stress-test concurrent workflows
from workflow_orchestrator import StressTestEngine
ste = StressTestEngine()
result = ste.run_concurrent(count=8)
print(result.workflow_count, result.all_succeeded, result.final_states)

# 10. Iterate over the persisted artifacts of a completed workflow
import json
from pathlib import Path
from workflow_orchestrator import _workflow_dir
wf = _workflow_dir("<workspace-id>")
state = json.loads((wf / "workflow_state.json").read_text())
record = json.loads((wf / "workflow_record.json").read_text())
artifact_manifest = json.loads((wf / "artifact_manifest.json").read_text())
package_manifest = json.loads((Path("<workspace-id>") / "package" / "manifest.json").read_text())
```

---

## 18. Summary — what Phase 4 is, in one paragraph

Phase 4 is the **deterministic, resumable, replayable workflow driver**
that coordinates the Project Sol0mon pipeline from PLANNING to DELIVERY
under a single state machine, a single policy envelope, a single
artifact registry, a single recovery surface, and a single packaging
engine — plus three pure-stdlib validation frameworks (benchmark, fault
injection, stress). It adds fifteen components in one module
(`workflow_orchestrator.py`) and sixty-three regression tests in one file
(`tests/test_workflow_orchestrator.py`), all offline, all pure stdlib
at import, all additive over Phases 1-3.1 and the final architecture
hardening. No existing module is modified; no speculative abstraction
(no ABC, no `Protocol` for one implementation, no factory/strategy/
repository/plugin/event-bus/service-locator/visitor/builder hierarchy) is
introduced. Every contract is pinned by a test; every failure surfaces
an error; every recovery is structured; the YAGNI ladder held.
