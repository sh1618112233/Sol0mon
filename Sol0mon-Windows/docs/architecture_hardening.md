# Sol0mon — Final Architecture Hardening

This document captures the **Final Architecture Hardening** layer + the
**Consolidation Phase** that follows it: a deterministic, auditable, resumable
build platform layered on top of the existing decision/verification/spec/
execution stack. It preserves every earlier invariant (Canonical Decision
Model as sole owner of decision interpretation; deterministic spec rendering;
the SQLite/PostgreSQL contradiction class eliminated) and adds the layered
concerns described here, plus an explicit ledger of what already existed (so
it is not re-built) and what was deliberately deferred (so it is not silently
re-litigated).

> **Section organization.** The first half documents the hardening layer
> (`lifecycle.py`, `artifacts.py`, `validation.py`, `execution_planner.py`,
> `resume.py`, `errors.py`). The second half (§ *Consolidation Phase*)
> documents the opt-in additions the consolidation phase made ON TOP of those:
> the `planner_fn` pipeline seam, persisted `execution_plan.json`,
> `BuildManifest`, `validate_manifest`, manifest-aware resume, and the
> JSON/Mermaid/DOT task graph exports.

---

## What already existed (not re-implemented)

The earlier phases shipped more than this hardening's brief assumed. Auditing
the repo before writing ruled out re-implementing these:

| Concern | Where it already lives | Status |
|---------|------------------------|--------|
| **Canonical Decision Model** (Phase A of the brief, invariants) | `decision_model.py` — single owner of verification interpretation; `decision_records(verification_result)` is the sole entry every consumer reads | **Done**. No structural change. |
| **Telemetry / Observability** (the brief's Phase F) | `telemetry.py` — process-level recorder, `TelemetryEvent` frozen dataclass, stage durations, ETA, `performance_report()`, resume-safe `configure(resume=True)`. Module docstring already enforces **observational only** | **Done**. |
| **Repair loop** (the brief's Phase K) | `implementation_executor._execute_with_repair_loop` — `MAX_REPAIR_ATTEMPTS=3`, persisted `repair_attempts`/`repair_log` after every attempt, `_emit_human_required` on exhaust (no silent failure), resume from `_load_build_state` checkpoint | **Done**. Already structured + resume-safe. |
| **Performance instrumentation** (the brief's Phase L) | `telemetry.performance_report()` time buckets (research/debate/verification/architecture/human_synthesis/build/repair) + `save_report` | **Done**. |
| **BuildSpec structured object** (the brief's Phase H) | `build_spec_engine.build_spec()` already returns a structured dict (`spec_text`, `decisions`, `constraints`, `tradeoffs`, `verified_decisions`, `verification_result`, `decision_gaps`, `status`, `missing_sections`, …). The markdown `spec_text` is one rendering, not the source of truth | **Done** at the engine level; the hardening wraps it in a typed `Artifact` envelope (Phase B). |
| **Scattered validators** (the brief's Phase C "before") | `build_prompt_generator.validate_spec_sections`, `decision_renderer.validate_synthesis_consistency`, `dispute_validator.validate_disputes`, `implementation_executor._tests_passed`, `build_spec_engine._spec_decision_gaps` | **Reused, not replaced** — the new `validation.py` delegates to all of them. |
| **Per-module resumability** | `execution_decision.from_verification_result` (immutable), `implementation_executor._load_build_state` (repair checkpoint), `telemetry.configure(resume=True)`, `verification_engine.verify_all_disputes(prior_research_evidence=...)`, `sol0mon_graph.MemorySaver` checkpointer | **Reused** — the new `resume.py` provides a single entry point that dispatches to these. |

---

## What was added

Six new modules (pure stdlib; purely additive; no existing module modified):

| Module | Role | Phase |
|--------|------|-------|
| `errors.py` | `BuildError` + `ErrorKind` discriminator + `Severity`; raised by the lifecycle guard and the validation dispatcher (real catchers, not dead code) | J (lazy) |
| `lifecycle.py` | `BuildLifecycle` state machine + transition table + guard + legacy-status mapper | A |
| `artifacts.py` | One frozen `Artifact` + `ArtifactKind` (8 named kinds) + `ArtifactStore`; id/version/checksum/provenance/dependencies | B |
| `validation.py` | `ValidationReport` + six validators delegating to existing ones + `validate(stage, artifact, halt_on_failure=False)` dispatcher | C |
| `execution_planner.py` | Deterministic `ExecutionPlanner.plan(spec) -> ExecutionPlan` task DAG + topological order + `ready()` | D + E |
| `resume.py` | `resume_build / resume_execution / resume_validation` — single entry points keyed off the lifecycle stage | G |

Plus five regression-test files (`tests/test_lifecycle.py`,
`tests/test_artifacts.py`, `tests/test_validation.py`,
`tests/test_execution_planner.py`, `tests/test_resume.py`) — **68 new tests**,
all passing, registered in `run_all_tests.py`.

### Regression proof

```
$ python3 run_all_tests.py
-- Integration Pipeline Tests --
  PASS  tests/test_workflow_pipeline.py
  PASS  tests/test_answer_resurrection_pipeline.py
  PASS  tests/test_build_spec_pipeline.py
  PASS  tests/test_implementation_pipeline.py
  PASS  tests/test_build_answer_55_e2e.py
  PASS  tests/test_build_answer_55_opencode_live_e2e.py
  PASS  tests/test_resurrection_constraints.py
  PASS  tests/test_human_synthesis.py
  PASS  tests/test_lifecycle.py            (11)
  PASS  tests/test_artifacts.py             (10)
  PASS  tests/test_validation.py            (23)
  PASS  tests/test_execution_planner.py      (10)
  PASS  tests/test_resume.py                (14)
-- Module Tests (Phase 1-4) -- 6 PASS
RESULTS: 20 passed, 0 failed
```

All earlier phases (`test_phase1_root_cause_regression`,
`test_phase2_pipeline_integrity`, `test_phase3_build_spec_artifact`,
`test_decision_model_canonical`, `test_repair_loop`, `test_telemetry`, …)
remain green. **No existing module was modified.** No existing test was changed.

---

## Phase A — BuildLifecycle state machine

`lifecycle.py` defines the authoritative states and the legal transitions.

### States

```
NEW
  │
  ▼
VERIFIED
  │
  ▼
SPEC_READY
  │
  ▼
EXECUTION_ALLOWED
  │
  ▼
EXECUTING
  │
  ▼
ARTIFACTS_VALIDATED
  │
  ▼
TESTS_VALIDATED
  │
  ▼
COMPLETE
```

Side / terminal states reachable from many points: **FAILED**,
**REPAIRING**, **BLOCKED**, **ABORTED**.

Each `BuildLifecycle` member carries documented metadata in
`lifecycle.STATES`: `owner`, `entry`, `exit`, `telemetry` (the
`Lifecycle.<STATE>` event), `recovery`.

### Transition table (excerpt)

```
NEW              → {VERIFIED, BLOCKED, ABORTED}
VERIFIED         → {SPEC_READY, BLOCKED, ABORTED}
SPEC_READY       → {EXECUTION_ALLOWED, FAILED, BLOCKED, ABORTED}
EXECUTION_ALLOWED→ {EXECUTING, BLOCKED, FAILED, ABORTED}
EXECUTING        → {ARTIFACTS_VALIDATED, REPAIRING, FAILED, ABORTED}
ARTIFACTS_VALIDATED → {TESTS_VALIDATED, REPAIRING, FAILED, ABORTED}
TESTS_VALIDATED  → {COMPLETE, ABORTED}
REPAIRING        → {EXECUTING, FAILED, ABORTED}
BLOCKED          → {SPEC_READY, EXECUTION_ALLOWED, NEW, ABORTED}
FAILED           → {NEW, SPEC_READY (force-retry recovery edge), ABORTED}
COMPLETE         → {}    (terminal)
ABORTED          → {}    (terminal)
```

`transition(current, target)` validates a transition and raises
`BuildError(kind=LifecycleError, recoverable=False)` on an illegal one.
`advance_lifecycle(workspace_id, target, **extra)` validates + persists a
transition.

### Backwards compatibility — the parallel `lifecycle_stage` field

`build_workspace.write_build_state` is **unchanged** (existing tests pin its
output byte-for-byte). The lifecycle writes a **parallel** `lifecycle_stage`
field into `build_state.json` alongside the legacy ad-hoc `status` string:

```json
{
  "status":           "spec_generated",   // legacy (existing tests pin)
  "lifecycle_stage":  "SPEC_READY",        // new authoritative state
  "lifecycle_updated_at": "2026-07-18T..."
}
```

`current_lifecycle(workspace_id)` reads the new field, falling back to
`lifecycle_stage_from_status(legacy_status)` for workspaces created before
this layer (which carry only the legacy string). Every one of the ~30 legacy
ad-hoc status strings maps to a lifecycle state, so **nothing bypasses the
lifecycle** — a legacy `status` IS its lifecycle stage, spelled differently;
the mapper bridges the two. Arbitrary `blocked_*` prefixes (built dynamically
by `run_build_pipeline` as `f"blocked_{block_reason}"`) map to `BLOCKED`.

---

## Phase B — Artifact system

`artifacts.py` makes every pipeline output a first-class, immutable object.

### One class + kind discriminator

The brief lists eight artifact classes (VerificationArtifact, DecisionArtifact,
BuildSpecArtifact, ExecutionPlanArtifact, ImplementationArtifact,
ValidationArtifact, TestArtifact, FinalBuildArtifact). They have identical
shape (id / version / producer / created_at / checksum / dependencies /
validation_status / lifecycle_stage / payload). Eight subclasses with identical
fields is the "factory for one product" anti-pattern, so they collapse to ONE
frozen `Artifact` + an `ArtifactKind` enum. The eight named artifacts are
instances of `Artifact` with the matching kind:

```python
from artifacts import build_spec_artifact, ArtifactKind, ArtifactStore
spec_art = build_spec_artifact(spec_dict, producer="build_spec_engine",
                               dependencies=(verification_art.id,))
```

### Fields (the brief's contract)

`id` (content-addressed: `uuid5` of `"<kind>:<sha256>"` — deterministic, so
resume re-derives the same record), `version`, `producer`, `created_at`,
`checksum` (sha-256 of the canonical JSON of the payload; dicts serialized
`sort_keys=True` so key order doesn't change the hash), `dependencies`
(tuple of upstream artifact ids — provenance graph), `validation_status`,
`lifecycle_stage`, `payload` (the structured content — markdown is a
rendering of it via `Artifact.render_markdown()`).

### Immutability + provenance

`Artifact` is `@dataclass(frozen=True)`. The only "mutation" is
`with_validation(status, report_id)` which returns a NEW artifact (version
bumped); the original never changes — the record of what was produced never
silently mutates. Every downstream artifact references its upstreams via
`dependencies`, so an audit can walk the graph from `FinalBuildArtifact` back
to `VerificationArtifact`.

### Storage

`ArtifactStore(workspace_id)` writes a sidecar per artifact under
`<workspace>/artifacts/<id>.json` + an `index.json` summary (one row per id
— the latest version). Re-putting the same id (validation bump) replaces the
index entry; re-deriving an identical payload is a no-op (deterministic id).

---

## Phase C — Central validation framework

`validation.py` defines one `ValidationReport` shape and six validators that
**delegate to the existing ones** — they do not re-implement logic.

### The report

```python
ValidationReport(passed, stage, artifact_id, issues, dependent_artifact_id,
                suggested_repair)
```

`passed` is `True` iff no issue is `ERROR`/`FATAL`. **WARNINGS are advisory —
they do not fail the report** (e.g. `decisions_honored` gaps: the spec is
still usable but should be amended). `severity` aggregates the worst issue;
`warnings` / `failures` partition the issues.

### The six validators

| Validator | Delegates to | Checks |
|-----------|-------------|--------|
| `validate_decision_model(verification_result)` | `decision_model.decision_records` ( Sole owner ) | Winner present per record; **SQLite/PostgreSQL invariant live**: no rejected option appears in `winner_tokens` (FATAL on violation — extends the invariant into the new layer) |
| `validate_build_spec(spec)` | `validate_spec_sections` + `_spec_decision_gaps` | All 7 sections present; `status == "completed"`; WARNING on decision gaps |
| `validate_execution_plan(plan)` | (DAG check — stdlib) | Non-empty; all deps resolvable; **acyclic** (FATAL on a cycle) |
| `validate_generated_code(files)` | (artefact-presence gate) | At least one file produced (the answer-156 non-answer class produces none) |
| `validate_tests(test_results)` | `implementation_executor._tests_passed` | Phase 2 strengthened contract: `runner != 'none'`, `passed>=1`, `failed==0`, `exit_code==0` |
| `validate_final_build(build_state)` | `lifecycle.current_lifecycle` (+ the above) | `spec_status == completed` AND `execution_success == True` AND lifecycle `COMPLETE` |

### Dispatcher + halt

`validate(stage, artifact, halt_on_failure=False, **ctx)` is the single
entry point. **An unknown stage raises `BuildError`** (never silently skipped
— "no stage may infer state without validation"). `halt_on_failure=True`
raises `BuildError(kind=ValidationError, recoverable=True)` so the pipeline
stops instead of continuing on bad inputs.

### Validation ordering (Phase M invariant)

A build-spec report that fails its own validator must NOT be planned as if it
passed. The contract callers (the resume engine) enforce: consult
`validate_build_spec` first; only on `passed=True` build+validate the plan.
Pinned by `test_validation.py::test_validation_ordering_spec_must_pass_before_plan_is_trusted`.

---

## Phases D + E — Execution Planner + Task Graph

`execution_planner.py` inserts a deterministic planning stage:

```
Build Spec  ->  Execution Planner  ->  Task Graph (DAG)  ->  Coding Agent
```

### Determinism (no new AI capabilities)

`ExecutionPlanner.plan(spec) -> ExecutionPlan` reuses the canonical decision
model already captured in the BuildSpec (`decision_renderer._architecture_components`
— the **same** matcher the deterministic spec renderer uses — plus the spec's
`verified_decisions`). It does not invent new structure; it does not call an
LLM. Same spec → byte-identical `plan.to_dict()`, byte-identical `plan.id`
(pinned by `test_execution_planner.py::test_plan_is_deterministic`).

### Decomposition

1. One `component` task per platform component matched by the canonical model
   (rejected options never produce a component task — `test_rejected_option_never_becomes_a_component_task`).
2. One `decision` task per verified decision, depending on all component
   tasks (the decision is realized by the components as a whole).
3. A `tests` task depending on every component + every decision.
4. An `integration` task depending on every component task.
5. A `validation` task depending on (tests, integration) → `final_build`.
6. An `acceptance` task depending on validation (terminal task of the DAG).

Fallback: an empty spec still yields a single fallback IMPLEMENTATION task
(the DAG is never empty — `test_empty_spec_still_yields_non_empty_plan`).

### Task record (the brief's contract)

`Task(id, kind, inputs, outputs, dependencies, owner, status, retries,
acceptance_criteria, validation)` — every field the brief lists.
`status ∈ {PENDING, RUNNING, DONE, FAILED, SKIPPED}`. The `validation` field
names the validator stage that gates the task (matches `validation.VALIDATORS`).

### Resume + future parallelism

`ExecutionPlan.ready()` returns task ids whose dependencies are all `DONE`
— supporting partial completion, resume, and future parallel execution (any
subset of `ready()` can run concurrently; the DAG is the only ordering
constraint). Pinned by `test_ready_progressive_release_as_deps_done`.

`plan_from_workspace(workspace_id)` reads the authoritative `decisions.json`
(single source of truth — never re-parses the markdown).

---

## Phase G — Resume engine

`resume.py` provides the single entry points the brief asked for.

### Why resume doesn't re-call `run_build_pipeline`

`resurrect_answer` calls `create_workspace`, which mints a NEW `workspace_id`
every time — there is no way to resume the same workspace by re-running the
top-level pipeline. The resumable units are the **stage functions**
(`generate_build_spec`, `execute_build_spec`) which operate on an existing
`workspace_id`. Resume dispatches to those, and the existing per-module resume
takes over (immutable `execution_decision`, repair-loop checkpoint).

### Dispatch table

| `current_lifecycle` | Resume action |
|---------------------|---------------|
| `NEW` (empty) | `no_source_to_resume_from` (nothing to build) |
| `NEW`/`VERIFIED` | regenerate the spec deterministically, validate + plan, hand to execution |
| `SPEC_READY`/`EXECUTION_ALLOWED` | bridge to `execute_build_spec` (repairs resume from checkpoint) |
| `EXECUTING`/`ARTIFACTS_VALIDATED`/`TESTS_VALIDATED`/`REPAIRING` | re-enter the repair loop via `execute_build_spec` |
| `BLOCKED` | if pending humans exist → surface them (`pending_count`); if `incomplete_spec` → regenerate; else proceed |
| `FAILED` | without `force` → advisory; with `force=True` → retry from `SPEC_READY` (the `FAILED→SPEC_READY` recovery edge) |
| `COMPLETE`/`ABORTED` | no-op (terminal) |

### Safety

`use_claude_code=False` by default (same as `run_build_pipeline`) — a resume
on a `SPEC_READY` workspace just regenerates the spec + plan + validation and
**skips execution cleanly**, **never a model call**. Skipped execution holds
the build at `EXECUTION_ALLOWED` (ready and waiting), never retreating in the
lifecycle — the guard prevents any illegal back-transition
(`test_skipped_execution_never_moves_lifecycle_backwards`).

### Three entry points

`resume_build(workspace_id, *, use_claude_code=False, force=False)` — the
full resume. `resume_execution(workspace_id, *, use_claude_code=False)` —
just the execution/repair stage (refuses before spec is ready). 
`resume_validation(workspace_id)` — pure re-run of the validators against the
workspace's current artifacts + state (no mutation).

---

## Phase H — BuildSpec v2 (structured is authoritative, markdown is a rendering)

This was already true at the engine level: `build_spec_engine.build_spec()`
returns a structured dict and the markdown `spec_text` is one rendering. The
hardening makes the contract explicit by wrapping the dict in a
`BuildSpecArtifact` (`Artifact(kind=ArtifactKind.BUILD_SPEC, payload=spec_dict)
`) — the structured `payload` is the source of truth; `Artifact.render_markdown()`
returns the markdown view. No byte-for-byte change to `build_spec_engine` or
its existing tests (`test_phase3_build_spec_artifact` still pins the markdown
output of the deterministic Path 3 renderer).

---

## Phase J — Error taxonomy (lazy)

`errors.py`: ONE `BuildError(Exception)` carrying the full Phase J contract
(`kind`, `stage`, `message`, `artifact_id`, `severity`, `recoverable`,
`suggested_repair`) + an `ErrorKind` discriminator (DecisionError /
ValidationError / ArtifactError / PlanningError / ExecutionError /
TelemetryError / RepairError / LifecycleError). A 7-subclass hierarchy with
identical fields and identical handlers is dead weight; the discriminator
gives the same information at one class's cost. The two factories
`illegal_transition(...)` (lifecycle guard, `recoverable=False`) and
`validation_failed(report, stage)` (validation dispatcher, `recoverable=True`)
give `BuildError` **real catchers** — it is raised by the guard and the
dispatcher, not sitting unused.

---

## Phases F / K / L — Already complete (no new code)

| Brief phase | Already-shipped location |
|-------------|--------------------------|
| F. Observability | `telemetry.py` — recorder, `TelemetryEvent` frozen event, stage durations, ETA, `performance_report()`, `save_status` / `save_report`, resume-safe `configure(resume=True)`. Observational-only enforced in the module docstring and `tests/test_telemetry.py`. |
| K. Repair framework | `implementation_executor._execute_with_repair_loop` — `MAX_REPAIR_ATTEMPTS=3`, persisted `repair_attempts`/`repair_log` after every attempt, `_emit_human_required` on exhaust (no silent failure — `tests/test_repair_loop`), resume from `_load_build_state`. Already "track repair attempts / repaired artifacts / remaining failures". |
| L. Performance layer | `telemetry.performance_report()` time buckets + `tests/test_telemetry.py`. |

The hardening does not duplicate these. The new `validation.py` enriches
repair reporting (via `validate_tests` → `Repair Plan` for the dispatcher's
`suggested_repair`) without re-abstracting the existing loop.

---

## Deliberately deferred (with rationale)

| Brief phase | Deferred piece | Reason (ponytail) | When to add |
|-------------|---------------|--------------------|-------------|
| **I. Dependency Injection** (Protocol/ABC interfaces: DecisionProvider, ArtifactStore, ValidationEngine, TelemetryRecorder, ExecutionPlanner, TaskScheduler, CodingAgent, BuildRenderer) | ABC/Protocol layers | The codebase already does DI via **callable seams** (`debate_fn`, `executor_fn`, `agent_fn`, `test_fn`, `mode_select_fn`, `input_fn`, `research_fn`). Single-interface-with-one-implementation ABCs are the canonical YAGNI anti-pattern the skill forbids. | When a second concretely-swappable implementation appears (e.g. a second `CodingAgent` or a remote `ArtifactStore`). Until then the callable seams + `coding_agents/base_agent.BaseAgent` already provide swappability. |
| **J. Full error sub-hierarchy** (a subclass per Decision/Validation/Artifact/Planning/Execution/Telemetry/Repair) | The 7 subclasses | Identical fields + identical handlers = the discriminator does the job at one class's cost. | When a caller needs `except ValidationError` distinct from `except LifecycleError` — at which point the discriminator already names the kind, so the `except` can branch on `e.kind`. |
| **Wiring the planner into `run_build_pipeline` as a mandatory stage** | The opt-in seam | The existing pipeline's byte-for-byte tests pin execution semantics; a mandatory stage is a breaking change. The planner IS exercised by `resume_build` (validates + plans after generating the spec), so there is already a non-bypassing path. | As an opt-in `planner_fn` seam (matching how `mode_select_fn` was added in Phase 4) — say the word and it ships in ~10 lines with zero test changes. Making it mandatory requires updating the byte-for-byte test set. |

---

## Migration + backward-compatibility notes

1. **No existing module was modified.** `build_pipeline.py`,
   `build_spec_engine.py`, `build_workspace.py`, `implementation_executor.py`,
   `decision_model.py`, `telemetry.py`, `execution_decision.py`,
   `execution_mode_gate.py` are byte-for-byte identical.
2. **`write_build_state` is unchanged.** The lifecycle writes a parallel
   `lifecycle_stage` field; the legacy `status` string is preserved verbatim.
3. **Pre-hardening workspaces resume.** `current_lifecycle` falls back to
   `lifecycle_stage_from_status(legacy_status)` when `lifecycle_stage` is
   absent; the 30+ legacy ad-hoc strings all map to a lifecycle state.
4. **The SQLite/PostgreSQL invariant is untouched and extended.**
   `test_decision_model_canonical`, `test_phase1_root_cause_regression`,
   `test_phase3_build_spec_artifact`, `test_answer55_regression` all pass
   unchanged. The new `validate_decision_model` adds a live check of the same
   invariant in the new validation layer (`test_validation.py::test_decision_model_fails_on_winner_is_rejected`).
5. **Existing tests untouched.** 20/20 `run_all_tests.py` green; the
   `test_suite.py` SKIP (live Cerebras calls) is pre-existing.

---

## Architectural constraints honored

| Constraint | How |
|-----------|-----|
| Single Source of Truth (Canonical Decision Model) | `validate_decision_model` and the planner both read `decision_model.decision_records` / `decision_renderer._architecture_components` — never parse `verification_result` directly. |
| Determinism First | The planner is deterministic (no LLM); the spec was already deterministic (Path 3); the lifecycle transition table is static. |
| Explicit State | `BuildLifecycle` enum + transition table + `advance_lifecycle`; no inferred state — `current_lifecycle` reads, `advance_lifecycle` writes. |
| Artefacts Are First-Class Objects | `Artifact` frozen dataclass; the structured `payload` is authoritative; markdown is `render_markdown()`. |
| No telemetry affects execution | `telemetry.py` is unchanged and observational-only; the new modules do not import or depend on it for execution. |
| No hidden state | All new state is on disk (`lifecycle_stage` in `build_state.json`; artifacts in `artifacts/*.json`). |
| No bypassing the lifecycle / validators / planner | Legacy `status` IS its lifecycle stage via the mapper; the new modules validate + plan before declaring completion; `resume_build` goes through validation + planning. |

---

## Updated pipeline diagram

```
User Prompt
    │
    ▼
Workflow Classification
    │
    ▼
[ Decision Model (canonical)  ── Verification ── ]   unchanged
    │
    ▼  decision_records (SOLE interpretation)
Answer Resurrection → Workspace + decisions.json
    │
    ▼  lifecycle: NEW → VERIFIED
Build Spec Engine (deterministic, Path 3)  →  BuildSpecArtifact
    │                                            (structured payload authoritative;
    ▼                                             markdown = render_markdown())
    │  lifecycle: VERIFIED → SPEC_READY
    ▼
[ Validation: validate_decision_model → validate_build_spec ]   halt-on-failure
    │
    ▼  lifecycle: SPEC_READY
Execution Planner → ExecutionPlanArtifact (Task DAG)
    │                                            (deterministic; no LLM)
    ▼  validate_execution_plan (DAG, acyclic, deps resolvable)
    │  lifecycle: SPEC_READY → EXECUTION_ALLOWED
    ▼
[ Human-escalation gate + Execution-decision gate ]   unchanged
    │
    ▼  lifecycle: EXECUTION_ALLOWED → EXECUTING
Coding Agent → Tests → (Repair loop on failure)        unchanged repair loop
    │
    ├─ artefact-presence gate → ARTIFACTS_VALIDATED
    ├─ tests passed          → TESTS_VALIDATED
    └─ halt                  → COMPLETE   (validate_final_build → FinalBuildArtifact)

Side / terminal: REPAIRING, FAILED (→ SPEC_READY on force-retry), BLOCKED (→ resolve_human_requirement → resume_build), ABORTED.

Resume: resume_build(workspace_id) reads lifecycle_stage (or maps legacy
status) and re-enters at the right stage on the SAME workspace.
```

---

## Updated sequence diagram — resume flow

```
operator                resume_build            lifecycle          build_pipeline.stage_fn    implementation_executor
   │                         │                       │                      │                           │
   │── resume_build(wsid) ──►│                       │                      │                           │
   │                         │── current_lifecycle ─►│                      │                           │
   │                         │◄── stage ─────────────│                      │                           │
   │                         │                       │                      │                           │
   │            [if SPEC_READY/EXECUTION_ALLOWED: hand to execution]      │                           │
   │                         │── advance EXECUTION_ALLOWED ─►             │                           │
   │                         │── execute_build_spec(ws) ──────────────────►│                           │
   │                         │                       │                      │── execute_implementation ─►│
   │                         │                       │                      │                           │── _load_build_state (checkpoint)
   │                         │                       │                      │                           │── repair loop resumes
   │                         │                       │                      │◄── result ─────────────────│
   │                         │◄── exec_result ──────│                      │                           │
   │                         │── classify → advance ARTIFACTS_VALIDATED ─►  │                           │
   │                         │                → advance TESTS_VALIDATED ───►                           │
   │                         │                → advance COMPLETE ─────────►                            │
   │◄── resume report ───────│                                                                        │
```

---

## Developer documentation — using the new modules

```python
# 1. Lifecycle — replace ad-hoc status reasoning
from lifecycle import BuildLifecycle, advance_lifecycle, current_lifecycle
advance_lifecycle(workspace_id, BuildLifecycle.SPEC_READY, spec_status="completed")
stage = current_lifecycle(workspace_id)  # BuildLifecycle.SPEC_READY

# 2. Artifacts — wrap a pipeline output
from artifacts import build_spec_artifact, ArtifactStore
art = build_spec_artifact(spec_dict, producer="build_spec_engine",
                          dependencies=(verification_art.id,))
store = ArtifactStore(workspace_id)
store.put(art)
validated = art.with_validation(ValidationStatus.PASSED, report_id=...)  # version=2
store.put(validated)  # replaces the index entry

# 3. Validation — central entry point
from validation import validate
report = validate("build_spec", spec_dict)
if not report.passed: ...
# halt-on-failure:
validate("execution_plan", plan.to_dict(), halt_on_failure=True)  # raises BuildError

# 4. Plan — deterministic task graph
from execution_planner import plan
ep = plan(spec_dict, build_spec_artifact_id=art.id)
ready_task_ids = ep.ready()  # for resume / parallel execution

# 5. Resume — single entry point
from resume import resume_build
report = resume_build(workspace_id, use_claude_code=True)  # opts in to the real executor
# or, offline (no model):
report = resume_build(workspace_id)  # regenerates spec + validates + plans, skips execution
```

---

## Success criteria — verification matrix

| "The implementation is complete only if…" | Where |
|---|---|
| Every stage is deterministic where possible. | `execution_planner.plan`, `build_spec_engine` Path 3 (unchanged), `lifecycle` transition table. |
| Every transition is explicit. | `lifecycle.transition` + `BUILD_LIFECYCLE`; illegal transitions raise. |
| Every artifact has provenance. | `Artifact.dependencies` (tuple of upstream ids); `ArtifactStore.index.json`. |
| Every validation is centralized. | `validation.validate(stage, artifact)` + `VALIDATORS` registry. |
| Every execution can resume. | `resume.resume_build` / `resume_execution`/`resume_validation` read the lifecycle stage. |
| Every repair is structured. | `implementation_executor` repair loop (unchanged) + `validate_tests` `suggested_repair`. |
| Every lifecycle event is observable. | `lifecycle.STATES[state]["telemetry"] = "Lifecycle.<STATE>"` emitted on every advance. |
| Every downstream component consumes the Canonical Decision Model. | `validate_decision_model` + `execution_planner.plan` both read `decision_model.decision_records` / `decision_renderer._architecture_components`. |
| Existing functionality + regression tests intact. | 20/20 `run_all_tests.py`; existing modules unchanged. |

---

# Consolidation Phase

The hardening above shipped the lifecycle, artifact model, validation
framework, planner, and resume engine. The consolidation phase turns those
into a **production pipeline** by integrating the planner via the same seam
philosophy as `debate_fn` / `executor_fn` / `mode_select_fn`, persisting the
plan as a first-class artifact, introducing a build manifest, validating the
manifest, making resume manifest-aware, and adding task-graph visualization
exports — every addition a small concrete function or immutable dataclass,
zero speculative abstractions (no ABCs, no Protocol interfaces, no plugin
framework, no factory/strategy/builder hierarchies).

## The nine changes

| # | Change | File(s) | Net-new lines (excluding docstrings/blank) |
|---|--------|---------|--------------------------------------------|
| 1 | `planner_fn` seam in `run_build_pipeline`; default = deterministic `execution_planner.plan`; result dict gains `result["plan"]`; persisted first-class | `build_pipeline.py` (signature + Step 4.5) | ~30 |
| 2 | `store_execution_plan` / `load_execution_plan` / `verify_execution_plan_checksum` persist the `ExecutionPlanArtifact` envelope at `<workspace>/execution_plan.json` | `execution_planner.py` | ~70 |
| 3 | `BuildManifest` frozen dataclass + `store_manifest` / `load_manifest` / `build_manifest_from_workspace`; references only, no embedded content | `manifest.py` (~280 incl. self-check) | new file |
| 4 | `export_json` / `export_mermaid` / `export_dot` pure functions; diagnostic only — no runtime depends on them | `execution_planner.py` | ~50 |
| 5 | `validate_manifest(manifest, *, workspace_id=…)` in `validation.VALIDATORS` — flags missing artifact, checksum drift, lifecycle mismatch, plan-id mismatch, plan-spec drift | `validation.py` | ~150 |
| 6 | `resume_build` is manifest-aware (manifest → artifacts → lifecycle → resume point); `_load_or_rebuild_spec_result` and `_load_or_rebuild_plan` reuse on-disk deterministic artifacts when their checksums match; `_refresh_manifest` keeps `manifest.json` fresh after each stage | `resume.py` | ~80 |
| 7 | 25 new regression tests in `tests/test_consolidation.py` covering Parts 1-7 | new test file | new file |
| 8 | Docs + README updates | this file, `README.md` | (this section) |
| — | Regression suite registration | `run_all_tests.py` | 3 |

## Part 1 — Planner integration

`run_build_pipeline(..., planner_fn: Callable[…, ExecutionPlan | dict] | None = None)`.
Same seam philosophy as the other pipeline seams:

```
generate_build_spec (existing)            → spec_result
        |
        v
planner_fn(spec_result)                   → ExecutionPlan   [default: execution_planner.plan]
        |                                                    [override: a callable for tests]
        v
store_execution_plan(...)                  → <workspace>/execution_plan.json   (the ExecutionPlanArtifact)
        |
        v
execute_build_spec (existing)             → exec_result
```

`result["plan"] = plan_payload` (additive — no existing key removed; readers
using `.get(...)` are unaffected). Planning failure is **observational**, not
a pipeline block: the spec is still the agent's primary input; the plan is an
additional structured artifact for resume / visualization. Planning remains
**deterministic** — the seam only allows tests / diagnostic callers to inject
a canned plan; no LLM is invoked.

## Part 2 — Persisted ExecutionPlanArtifact

**`<workspace>/execution_plan.json`**, beside `implementation_plan.md`,
`build_state.json`, `decisions.json`. Envelope (the brief's contract):

```json
{
  "kind":            "ExecutionPlanArtifact",
  "id":              "<uuid5(kind:checksum)>",   // deterministic over payload
  "plan_id":         "plan_<sha16>",              // the inside-plan id
  "checksum":        "<sha256 of canonical payload>",
  "version":         1,
  "producer":        "execution_planner" | "planner_fn",
  "created_at":      "ISO8601",
  "lifecycle_stage": "EXECUTION_ALLOWED",
  "validation_status": "unvalidated",
  "dependencies":   ["<build_spec_artifact_id>"],
  "payload":        { "id": ..., "order": [...], "tasks": {...},
                       "spec_checksum": ..., "build_spec_artifact_id": ... }
}
```

**Immutability** is content-addressing: re-storing the same plan yields the
same `id` and `checksum` (verified by `tests/test_consolidation.py::
test_execution_plan_artifact_immutability_idempotent_rederivation`). **Resume
reuses persisted plans** (Part 6) instead of regenerating whenever their
checksum still validates.

## Part 3 — Build Manifest

**`manifest.py`** — one small immutable `@dataclass(frozen=True)`. Pure
references, no embedded content:

```json
{
  "workspace_id":       "...",
  "manifest_version":    1,
  "id":                  "<uuid5 over workspace_id+state+checksums>",
  "created_at":          "ISO8601",
  "lifecycle":           "SPEC_READY",       // current_lifecycle at build time
  "decision_artifact":   "<artifact_id>" | null,
  "build_spec":          "<artifact_id>" | null,
  "execution_plan":      "<artifact_id>" | null,
  "implementation":      "<artifact_id>" | null,
  "validation":          "<artifact_id>" | null,
  "tests":               "<artifact_id>" | null,
  "checksums": {
    "build_spec":         "sha256...",
    "execution_plan":     "sha256...",
    "decision_artifact":  "sha256...",
    ...
  }
}
```

The manifest is the **canonical description of a (possibly partial)
completed build**. It is written at the end of `run_build_pipeline` (via the
top-level `_persist_build_manifest` helper) and refreshed by every resume
dispatch.

Immutability is enforced at the *producer* level: `with_section()` and
`with_lifecycle()` return NEW manifests (forcing `id=""` so `__post_init__`
re-derives content-addressed id), the original is unchanged. Unknown section
names raise (no silent typo dormancy).

## Part 4 — Task graph exports

**Pure functions**, no I/O, no runtime dependencies on the output text. The
persisted `execution_plan.json` does **not** embed any of these formats (the
test `test_export_no_runtime_dependency_fixtures` pins this).

```python
from execution_planner import export_json, export_mermaid, export_dot
plan_obj = plan(spec)
json_text   = export_json(plan_obj)    # indented, sort_keys=True
mermaid_text = export_mermaid(plan_obj) # "graph TD\n  a[a]\n  ...\n  a --> b"
dot_text    = export_dot(plan_obj)      # "digraph G {\n  \"a\" ...\n  \"a\" -> \"b\";\n}"
```

Outputs are byte-deterministic for a given plan (pinned). Empty plans still
produce well-formed diagrams.

## Part 5 — Manifest validation

`validate_manifest(manifest, *, workspace_id=…)` returns the standard
`ValidationReport`. Registered in `VALIDATORS["manifest"]`, so it works
through the dispatcher (`validation.validate("manifest", None,
workspace_id=ws)`).

Rules it enforces:

| Rule | Severity | Trigger |
|------|---------|---------|
| `manifest_present` | ERROR | No manifest + no workspace_id |
| `manifest_version` | ERROR | Schema version mismatch |
| `lifecycle_consistent` | ERROR | manifest.lifecycle != current_lifecycle(workspace_id) — stale manifest |
| `artifact_exists` | ERROR | Manifest references an artifact id but the file is missing |
| `checksum_match` | ERROR | On-disk checksum != manifest's recorded checksum (drift / tampering) |
| `plan_id_match` | ERROR | manifest.execution_plan != persisted execution_plan.json's stored id |
| `plan_matches_spec` | ERROR | The plan's `spec_checksum` differs from the manifest's `build_spec` checksum (plan built from a different spec than the one on disk now) |
| `workspace_id_match` | FATAL | A manifest loaded from a different workspace than caller supplied |
| `validation_artifact_exists` | ERROR | lifecycle = TESTS_VALIDATED/COMPLETE but no validation referenced |
| `tests_artifact_exists` | ERROR | lifecycle = TESTS_VALIDATED/COMPLETE but no tests referenced |
| `implementation_artifact_exists` | ERROR | lifecycle = COMPLETE but no implementation referenced |

These rules satisfy the brief's manifest-validation contract:
every referenced artifact exists, checksums match, lifecycle is consistent,
validation artifacts exist when expected, the execution plan matches the
implementation (via `plan_matches_spec` + `plan_id_match`), and the
implementation belongs to the manifest (via `workspace_id_match` /
`checksum_match`).

## Part 6 — Manifest-aware resume

New flow:

```
load_manifest(workspace_id)            // None if absent
   ↓
manifest → artifacts → lifecycle → resume point
   ↓
_validate_or_repair: reuse on-disk deterministic
   artifacts when checksums match; regenerate missing/invalid only
   ↓
lifecycle-based dispatch (unchanged)
   ↓
refresh_manifest(workspace_id)         // keeps the manifest fresh
```

**Reuse is the default.** `_load_or_rebuild_spec_result` checks the on-disk
implementation_plan.md sha256 against the manifest's `build_spec` checksum and
returns a minimal `spec_result` if they match (no regeneration). When they
don't, it regenerates deterministically (Path 3) and stores the new spec.
Symmetric logic for the plan in `_load_or_rebuild_plan`, which uses
`verify_execution_plan_checksum` to detect tampering.

**Back-compat:** when no manifest exists (pre-hardening workspaces, brand-new
ones), the behavior is byte-for-byte the pre-Part-6 resume — the failure mode
of "manifest absent" is "regenerate deterministically", which is what the old
resume did.

The lifecycle stage is **still the authoritative resume signal**; the
manifest points at artifacts but does not replace `build_state.json`'s record
of the live stage. The manifest lifecycle consistency check (`rule=
lifecycle_consistent`) flags stale-state drift.

## Part 7 — Regression coverage

`tests/test_consolidation.py` (25 tests) pins every contract above:

| Area | Tests |
|------|------|
| **Planner** | `test_planner_fn_default_is_deterministic_and_set_on_result`, `test_planner_fn_seam_overrides_default`, `test_planner_fn_default_no_llm_invocation_signature_change` |
| **Planner persistence** | `test_execution_plan_persisted_after_run_build_pipeline`, `test_execution_plan_artifact_immutability_idempotent_rederivation`, `test_load_execution_plan_returns_none_for_missing`, `test_verify_execution_plan_checksum_detects_tampering` |
| **Manifest creation** | `test_manifest_creation_references_only`, `test_manifest_with_section_returns_new_manifest_original_intact`, `test_manifest_unknown_section_raises`, `test_manifest_id_deterministic_over_workspace_records` |
| **Visualization (exports)** | `test_exports_json_mermaid_dot_deterministic`, `test_export_no_runtime_dependency_fixtures` |
| **Manifest validation** | `test_validate_manifest_passes_for_consistent_workspace`, `test_validate_manifest_flags_missing_referenced_artifact`, `test_validate_manifest_flags_checksum_mismatch`, `test_validate_manifest_flags_plan_checksum_mismatch`, `test_validate_manifest_flags_lifecycle_mismatch`, `test_validate_manifest_flags_missing_validation_tests_when_complete`, `test_validate_manifest_plan_id_mismatch` |
| **Resume (manifest-aware)** | `test_resume_reuses_persisted_plan_no_regeneration`, `test_resume_corrupted_artifact_triggers_regeneration`, `test_resume_completed_workspace_is_noop_with_manifest`, `test_resume_partial_build_proceeds_when_manifest_present`, `test_end_to_end_resume_with_manifest_after_pipeline` |

All 25 pass; the full suite (`run_all_tests.py`) is now **21/21 PASS, 0
failures**, including the existing Phase 1-4 tests, the Phase M lifecycle /
artifact / validation / planner / resume tests, and the consolidation tests.
`test_suite.py` SKIP (>120s live Cerebras) is pre-existing, unrelated.

## Updated pipeline diagram (post-consolidation)

```
User Prompt
    ↓
Workflow Classification
    ↓
[ Decision Model (canonical) ── Verification ]   unchanged
    ↓  decision_records (SOLE interpretation)
Answer Resurrection → Workspace + decisions.json
    ↓  lifecycle: NEW → VERIFIED
Build Spec Engine (deterministic, Path 3)        → BuildSpecArtifact on disk (implementation_plan.md)
    ↓  validate_build_spec
Execution Planner (deterministic; planner_fn seam)  → ExecutionPlanArtifact (execution_plan.json)
    ↓  validate_execution_plan (DAG, acyclic, deps resolvable)
    ↓  lifecycle: SPEC_READY → EXECUTION_ALLOWED
[ Human-escalation + Execution-decision gate ]   unchanged
    ↓  lifecycle: EXECUTION_ALLOWED → EXECUTING
Coding Agent → Tests → Repair loop                unchanged
    ↓  artefact-presence gate → ARTIFACTS_VALIDATED
    ↓  tests passed → TESTS_VALIDATED
    ↓  → COMPLETE   (validate_final_build → FinalBuildArtifact, written to manifest)

+_persist_build_manifest at end of run_build_pipeline
+refresh_manifest after every resume dispatch
+validate_manifest(manifest, workspace_id=…) verifies references + checksums + lifecycle + plan_id

Side / terminal: REPAIRING, FAILED (→ SPEC_READY on force-retry), BLOCKED (→ resolve_human → resume_build), ABORTED

Manifest-aware resume: manifest → artifacts → lifecycle → resume point.
```

---

## Non-negotiable constraints honored

Every speculative abstraction in the brief's "Do NOT implement" list is
absent:

| Forbidden | Where it would tempt this work | Where it isn't |
|-----------|----------------|-----------------|
| ABC interfaces | `planner_fn`, `executor_fn`, `manifest producer` | Callable seams (the existing pattern); one function each |
| Protocol interfaces | `ValidationEngine` | One `validate(stage, …)` dispatcher + sibling `*_validate_*` functions |
| Repository pattern | `ArtifactStore`, `ExecutionPlanArtifact`, `BuildManifest` persistence | Plain `store_*` / `load_*` helpers at module scope |
| Service locator | "the build manifest registry" | None exists |
| Plugin framework | "pluggable planners / stores / validators" | Two `VALIDATORS["manifest"]` entries; nothing else is pluggable |
| Event bus | resume notifications | None |
| Dependency container | "the build registry" | None |
| Message queue | "repair scheduling" | Synchronous repair loop (unchanged) |
| Observer framework | "manifest change listeners" | None |
| Strategy hierarchy | `RepairStrategy` subclasses | One fixed `MAX_REPAIR_ATTEMPTS=3` loop (unchanged from before this phase) |
| Visitor pattern | task graph traversal | `_topo_order` + `_find_cycle` free functions |
| Factory hierarchy | `ArtifactFactory` subclasses | 8 named free functions (`build_spec_artifact(…)`, …) — one liner each |
| Builder hierarchy | `ManifestBuilder` fluent chain | One `BuildManifest` dataclass with `with_section`/`with_lifecycle` |
| Generic pipeline engine | "header for the planner" | Direct call sites in `run_build_pipeline` |
| Reflection / metaclasses / config DSL / runtime code-gen | "manifest schema" | Plain `@dataclasses.dataclass(frozen=True)` |
| Multiple planners / stores / validators | "swappable components" | One each |

The YAGNI ladder held: "If there is only one implementation today, do not
create an abstraction for it." There is exactly one planner, one manifest
producer, one validator dispatcher.

## Architectural layering stays shallow

The brief's maximum desired layering holds:

```
Decision Model → Renderer → Build Spec → Execution Planner → Executor
              → Validation → Manifest
```

No intermediate layers were introduced. `manifest.py` imports `execution_planner`
and `build_spec_engine`. `resume.py` imports `manifest`, `execution_planner`,
`lifecycle`. No cyclical coupling. The new layer sits to the *side* of the
existing pipeline (process-time persistence + post-hoc validation), it does
not insert itself into the decision/spec/execute flow beyond the planner
stage explicitly listed in the brief.

## Updated success criteria — verification matrix

| "The implementation is complete only if…" | Where |
|---|---|
| planner integrated | `build_pipeline.run_build_pipeline(..., planner_fn=…)`, default `execution_planner.plan`, persisted `execution_plan.json` |
| execution plan persisted | `execution_planner.store_execution_plan` + `load_execution_plan` write the envelope to `<workspace>/execution_plan.json` |
| build manifest implemented | `manifest.BuildManifest` + `build_manifest_from_workspace` + `store_manifest` / `load_manifest` |
| manifest validation implemented | `validation.validate_manifest` (registered in `VALIDATORS`) |
| resume uses manifest | `resume.resume_build` reads the manifest, `_load_or_rebuild_spec_result` + `_load_or_rebuild_plan` reuse persisted artifacts when checksums match |
| task graph export implemented | `execution_planner.export_json` / `export_mermaid` / `export_dot` |
| all regression tests pass | 21/21 `run_all_tests.py` pass (incl. all earlier phases); 25 new tests in `tests/test_consolidation.py` all pass |
| no existing architecture invariants broken | All earlier-phase tests green; Canonical Decision Model still the SOLE interpretation; SQLite/PostgreSQL invariant preserved (extended into `validate_decision_model`) |
| no speculative abstractions introduced | No ABCs, Protocols, factories, repositories, plugin frameworks, event buses, service locators, strategy/visitor/builder hierarchies — see the "Non-negotiable constraints honored" table above |
