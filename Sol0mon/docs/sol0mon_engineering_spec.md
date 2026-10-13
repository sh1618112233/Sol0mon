# Project Sol0mon — Unified Engineering Specification

This is the **direct implementation blueprint** for the entire Project Sol0mon
platform — the autonomous, consensus-driven architecture-design system that
runs structured multi-model debates to produce implementation-ready
architecture decisions outperforming standalone frontier models. It
consolidates every layer — Phase 1 (debate / verification / synthesis /
memory / the canonical Decision Model), Phase 2 (implementation engine),
Phase 3 (validation engine), Phase 3.1 (repair memory + retry policy), the
Execution Decision Gate layer, the Final Architecture Hardening, and Phase 4
(Autonomous Workflow Orchestration, Recovery & System Validation) — into a
single document suitable as a port specification for OpenCode.

Every contract, signature, state name, threshold, persistence path, and
acceptance gate named here is pinned by a concrete test in
`tests/` and implemented in one of the modules enumerated in §3. The level
of detail matches the two pre-existing canonical specs —
[`docs/architecture_hardening.md`](architecture_hardening.md) (the
lifecycle/artifact/validation/planner/resume hardening layer) and
[`docs/workflow_orchestration.md`](workflow_orchestration.md) (Phase 4
orchestration) — and this document composes those by reference rather than
duplicating their content. Where a contract is *already* exhaustively
specified in one of those two documents, this spec says so and points to
the precise section. Where it is *not*, this spec pins it byte-for-byte.

> **Reading order.** §1–§2 position the platform and enumerate what is
> already fully specified elsewhere. §3 is the layered component inventory.
> §4–§5 are the rationale and the single-source-of-truth invariants. §6–§7
> are the public-interface contracts and the persistence surfaces.
> §8–§10 are the state machines, sequence diagrams, and recovery semantics
> across every layer. §11 is the artifact lifecycle (from research evidence
> to packaged delivery). §12 is the PolicyEngine rule book (the execution
> decision gate AND the workflow-level policy envelope). §13–§15 are the
> benchmark, fault-injection, and stress-testing frameworks. §16 is the
> comprehensive testing matrix. §17 is the success criteria and acceptance
> gates ledger. §18 is the deliberately-deferred work. §19 is the
> migration / backward-compatibility notes. §20 is the developer handbook.

---

## 1. Architectural rationale

### 1.1 The problem Project Sol0mon solves

Single frontier models produce architecture decisions that are inconsistent
across runs,ignore their own earlier conclusions, recommend options they
have just rejected, and silently agree ("artificial consensus") on questions
where real technical disagreement exists. Sol0mon solves these by:

1. **Decoupling research from debate** — two LLMs (GPT via ChatGPT.com, Gemini
   via gemini.google.com) independently research a topic before debating.
2. **Extracting conflicts once** — the conflict extractor pulls a structured
   set of disagreements from the two research positions, prioritized by
   domain.
3. **Debating one dispute at a time** — a single-dispute consensus loop with
   duplicate-response detection, stagnation tracking, and stale-position
   detection.
4. **Verifying before resolving** — a third inference tier (Cerebras →
   Groq → OpenRouter → Ollama) scores consensus, validates disputes, verifies
   claims, and synthesizes the final output.
5. **Accumulating verified decisions as immutable constraints** — resolved
   disputes are injected into all subsequent disputes; never re-debated.
6. **Preserving tradeoffs** — `VERIFIED_TRADEOFF` outcomes keep both options
   with a recommendation; consensus does NOT require eliminating valid
   disagreement.
7. **Escalating to humans** — `HUMAN_REQUIRED` outcomes produce a structured
   decision queue with options, reasoning, affected disputes, priority.
8. **Canonizing the Decision Model** — a single module owns all
   interpretation of verification results; no other component parses raw
   verification prose.
9. **Deterministically rendering the build spec** — the Architecture
   Renderer and the Build Spec Engine produce reproducible output from the
   canonical Decision Model; no LLM in the rendering path.
10. **Gating execution on the verification outcome** — the Execution
    Decision Engine classifies the depth of further work (research-only /
    architecture-only / implementation-required / hard-stop); the build
    pipeline enforces this single decision as the gate.
11. **Resuming at any layer** — every stage persists to a sidecar or SQLite;
    resume reconstructs the context from disk and re-enters at exactly the
    recorded state, never silently restarting.
12. **Packaging the result** — a completed build produces 11 reproducible
    packaging outputs (research / architecture / spec / source tree /
    validation / quality / repair / execution / workflow / manifest /
    self-referential manifest) under one sealed directory.

### 1.2 The five-layer architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Layer 5: Phase 4 — Workflow Orchestration                                    │
│           PLANNING → RESEARCH → DEBATE → CONSENSUS → ARCHITECTURE →           │
│           IMPLEMENTATION → VALIDATION → REPAIR → QUALITY_GATE →              │
│           PACKAGING → DELIVERY → COMPLETED                                    │
│           PolicyEngine (workflow-level envelope)                              │
│           RecoveryManager + BenchmarkEngine + FaultInjection + StressTest    │
│           (spec: docs/workflow_orchestration.md)                              │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 4: Final Architecture Hardening                                        │
│           BuildLifecycle state machine → Artifact → Validation →             │
│           ExecutionPlanner → BuildManifest → resume_build                     │
│           (spec: docs/architecture_hardening.md)                              │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 3: Integration — Project Planner, Execution Authority,                 │
│           Pipeline Scheduler, Stage Logger, Workspace Knowledge,             │
│           Build Pipeline, Provider Router                                     │
│           (specified here in §6 – §12)                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 2: Execution / Validation / Repair                                     │
│           ExecutionDecisionEngine + ExecutionModeGate +                        │
│           NativeImplementationEngine + NativeValidationEngine +                │
│           RepairMemory + RetryPolicy                                          │
│           (specified here in §6 – §12)                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 1: Consensus — Debate / Verification / Synthesis / Memory /          │
│           Canonical Decision Model                                             │
│           (specified here in §6 – §12)                                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

Each layer is **additive** over the layers below it. No layer modifies a
lower layer's public surface. Each layer's resumability is independent — the
workflow can be resumed (Phase 4), the build lifecycle can be resumed
(Hardening), the implementation engine can be resumed (Phase 2's
`_load_build_state`), and the debate can be resumed (Phase 1's
`WorkspaceDebateEngine.resume_workspace`).

### 1.3 The single-source-of-truth discipline

The single architectural invariant that runs through every layer is:
**there is exactly ONE authority for each kind of decision**, and every
other component consumes that authority's output over its own
re-derivation. The authorities are:

| Decision kind | Authority | Consumers |
|----------------|-----------|----------|
| Verification interpretation (winner / rejected / tokens) | `decision_model.decision_records(verification_result)` | Architecture Renderer, Build Spec Engine, Build Pipeline, Execution Decision Engine, Execution Planner, Resume, Validation |
| Whether a build may proceed past the gate | The Execution Decision Gate (`build_pipeline._resolve_execution_decision` reading `execution_decision_engine.decide` / `execution_decision.classify_execution_decision`) | `run_build_pipeline`, `execute_build_spec`, Phase 4 IMPLEMENTATION stage |
| The build lifecycle state | `lifecycle.advance_lifecycle` (Hardening) | every Hardening component, resume |
| The workflow state | `WorkflowStateMachine.next_state` (Phase 4) | every Phase 4 stage |
| The policy envelope | `PolicyEngine.evaluate` (Phase 4) at the workflow level; `ExecutionDecisionEngine.decide` at the build level | the orchestrator; the build pipeline |
| Where to resume | `RecoveryManager` (Phase 4) at the workflow level; `resume.resume_build` (Hardening) at the build level; `_load_build_state` (Phase 2) at the implementation level | the respective resume entry points |

Nothing re-derives what an authority owns. The canonical Decision Model
invariant is the most important: parsing verification prose for a winner or
for rejected options anywhere outside `decision_model` is a regression —
the answer-156 "rejected option leak" bug is what this invariant prevents.

### 1.4 The three compositional invariants

| Invariant | What it means | Where pinned |
|------------|---------------|---------------|
| **Determinism First** | Stages that CAN be deterministic ARE deterministic. The build spec renderer (Path 3) uses no LLM. The execution planner uses no LLM. The decision model is pure stdlib. The state machines are static transition tables. | `tests/test_deterministic_synthesis.py`, `tests/test_execution_planner.py::test_plan_is_deterministic`, `tests/test_decision_model_canonical.py`, `tests/test_workflow_orchestrator.py::test_state_machine_*` |
| **Explicit State** | Every state advance is recorded by an authority and persisted before the next step. No state is inferred from absence. | `advance_lifecycle` writes `lifecycle_stage`; `RecoveryManager.save_state` writes `workflow_state.json`; `StateManager.save` writes `execution_context.json`; `write_build_state` writes `build_state.json`. |
| **Resumability** | Every stage can be resumed from its persisted state. Resume re-enters at the recorded state; it never silently restarts. Absent/corrupt state is a structured error. | `resume.resume_build`, `RecoveryManager.load_state`, `ExecutionPlanner.load_state`, `WorkspaceDebateEngine.resume_workspace`. |

### 1.5 What Project Sol0mon deliberately is NOT

- **Not an autonomous agent.** Sol0mon does not decide what to research;
  the user's prompt does. It does not pick technologies; the verifier does.
  It does not pick a single best option when both are valid; it preserves
  the tradeoff. It does not implement when the verified decision says
  research-only; it stops.
- **Not a single-LLM system.** It is structurally multi-model: two debaters
  (GPT + Gemini), one verifier tier (Cerebras → Groq → OpenRouter →
  Ollama with key rotation and health tracking), one planner (Cerebras
  GPT-OSS-120B), the architecture renderer (no LLM), the synthesis engine
  (no LLM on the default path), the build spec engine (no LLM on Path 3).
- **Not an event-driven system.** Phase 4 has an in-process EventBus, but
  it is observational-only; no event ever affects execution. State advances
  via direct calls to the authority, not via event subscription.
- **Not an abstraction layer.** There are no ABCs for one implementation,
  no Protocol interfaces that wrap a single concrete class, no factory /
  strategy / repository / plugin / event-bus / service-locator / visitor /
  builder hierarchies. DI is via **callable seams** (`debate_fn`,
  `executor_fn`, `agent_fn`, `test_fn`, `mode_select_fn`, `planner_fn`,
  `input_fn`, `research_fn`, `coding_agent_fn`, `testing_agent_fn`,
  `repair_agent_fn`). See §18 for the deliberately-deferred abstractions.

---

## 2. What is already fully specified (composed by reference)

Two pre-existing documents fully specify the top two layers. This spec
depends on them and does not duplicate their content.

### 2.1 The Final Architecture Hardening — [`docs/architecture_hardening.md`](architecture_hardening.md)

Covers Layer 4 (BuildLifecycle, Artifact, Validation, ExecutionPlanner,
BuildManifest, Resume, Errors). Key contracts in that document:

- The `BuildLifecycle` state machine: `NEW → VERIFIED → SPEC_READY →
  EXECUTION_ALLOWED → EXECUTING → ARTIFACTS_VALIDATED → TESTS_VALIDATED →
  COMPLETE`, plus `REPAIRING / FAILED / BLOCKED / ABORTED`. Full transition
  table + `transition(current, target)` + `advance_lifecycle(...)`.
- The `Artifact` frozen dataclass (`id` content-addressed via `uuid5` of
  `"<kind>:<sha256>"`; 8 `ArtifactKind` values; `ArtifactStore` sidecar
  persistence under `<workspace>/artifacts/`).
- The `validation.py` central framework: `ValidationReport` + six
  validators (`validate_decision_model`, `validate_build_spec`,
  `validate_execution_plan`, `validate_generated_code`,
  `validate_tests`, `validate_final_build`) + `validate(stage, artifact,
  halt_on_failure=False)` dispatcher + `validate_manifest` (10 rules).
- The `ExecutionPlanner.plan` deterministic task DAG (no LLM;
  `plan_from_workspace` reads `decisions.json` as the single source of
  truth; `export_json` / `export_mermaid` / `export_dot`).
- The `BuildManifest` references-only frozen dataclass +
  `build_manifest_from_workspace` + `store_manifest` / `load_manifest`.
- The `resume_build / resume_execution / resume_validation` entry points +
  manifest-aware reuse of persisted deterministic artifacts.
- The `BuildError` + `ErrorKind` discriminator (8 kinds; one class, no
  subclass hierarchy).
- 21/21 `run_all_tests.py` pass; 68 new tests in
  `tests/test_lifecycle.py`, `tests/test_artifacts.py`,
  `tests/test_validation.py`, `tests/test_execution_planner.py`,
  `tests/test_resume.py` + 25 in `tests/test_consolidation.py`.

### 2.2 Phase 4 — [`docs/workflow_orchestration.md`](workflow_orchestration.md)

Covers Layer 5 (WorkflowOrchestrator, WorkflowStateMachine, PolicyEngine,
RecoveryManager, ArtifactManager, WorkflowRecorder, PackagingEngine +
BenchmarkEngine, FaultInjectionEngine, StressTestEngine). Key contracts in
that document:

- The 15 workflow states + `WorkflowStateMachine.next_state` as the sole
  authority on transitions; the repair loop (`S_VALIDATION` failing →
  `S_REPAIR` → `S_VALIDATION` re-entry); terminal states
  (`S_COMPLETED / S_FAILED / S_CANCELLED`).
- The `PolicyEngine` rule book (9 ordered rules: skip → terminal →
  cancelled → stage gating → runtime budget → workspace-size budget →
  attempt budget → mandatory pause → default allow).
- The 8 workflow artifacts under `<workspace>/workflow/`
  (`workflow_state.json`, `workflow_record.json`, `workflow_metrics.json`,
  `workflow_events.json`, `workflow_manifest.json`,
  `artifact_manifest.json`, `benchmark_results.json`,
  `stress_results.json`).
- The 11 packaging outputs under `<workspace>/package/` (research.md,
  architecture.md, implementation_spec.md, implementation/, validation_*.json,
  quality_report.json, repair_history.json, execution_record.json,
  workflow_record.json, manifest.json).
- `RecoveryManager` (load → validate → resume or structured error; never
  silently restart) + the no-silent-termination / no-silent-restart
  invariants.
- `BenchmarkEngine` (3 default workloads: small / medium / large),
  `FaultInjectionEngine` (4 default injectors: corrupt state, delete state,
  malformed prompt, interrupt before packaging), `StressTestEngine`
  (`run_concurrent` re-entrant; no shared mutable state).
- 63/63 `tests/test_workflow_orchestrator.py` pass; 20 numbered sections.

This document specifies Layers 1, 2, and 3, and the cross-layer invariants
that bind all five layers. Quotations from the source are verbatim.

---

## 3. Component inventory (the layered map)

The full Project Sol0mon platform. Files marked **§hardening** are
specified in `architecture_hardening.md`; **§orch** in
`workflow_orchestration.md`; the rest are specified here.

### 3.1 Layer 1 — Consensus (debate / verification / synthesis / memory)

| File | Lines | Role | Spec |
|------|-------|------|-----|
| `decision_model.py` | 617 | The SINGLE OWNER of decision interpretation. `decision_records(verification_result)` is the canonical return shape every consumer reads. Pure stdlib. | §6.1 |
| `debate_engine.py` | 1191 | Single-dispute consensus resolution with governance. `CONSENSUS_THRESHOLD=92`, `MAX_ROUNDS_PER_DISPUTE=15`, `MAX_CONSTRAINTS=10`. | §6.2 |
| `verification_engine.py` | 571 | Verifier over batch disputes with research escalation. `MAX_RESEARCH_ESCALATIONS=3`, `MAX_BATCH_DISPUTES=15`. | §6.3 |
| `synthesis_engine.py` | 436 | Composes the deterministic narrative sections + an LLM seam. Rule 16 + FIX 6 contradiction guard. | §6.4 |
| `conflict_extractor.py` | 284 | Extracts structured conflicts from GPT+Gemini positions via Cerebras. | §6.5 |
| `governance.py` | 509 | The `OutcomeType` enum (VERIFIED_DECISION / VERIFIED_TRADEOFF / HUMAN_REQUIRED) + `GovernedOutcome` + `GovernanceResult` + `InformationState`. | §6.6 |
| `consensus_engine.py` | ~300 | Consensus scoring over positions per round. (Quoted in §6.7.) | §6.7 |
| `answer_memory.py` | 602 | SQLite (`answer_memory.db`) long-term memory of every debate, with workspace_id + parent_answer_id chaining. | §7.1 |
| `human_decision_queue.py` | ~180 | In-memory shim over `human_decision_store`. `enqueue_human_decision`, `get_pending_count`, `resolve`. | §7.2 |
| `human_decision_store.py` | ~240 | Single source of truth for human-required decisions (`unified_human_decisions` SQLite table). | §7.3 |
| `human_decision_manager.py` | ~200 | `save_pending`, `resolve`, `pending`, `is_resolved` over the store. | §7.3 |
| `human_synthesis_renderer.py` | ~150 | Renders the `# Human Decisions Required` section deterministically. | §6.4 |
| `workspace_debate_engine.py` | 1018 | `WorkspaceDebateEngine(DebateEngine)` — `run_in_workspace`, `resume_workspace` (affected-dispute re-debate), `reopen_dispute`. | §6.8 |
| `workspace_impact_analyzer.py` | ~250 | Computes which disputes are affected by a follow-up. | §6.8 |

### 3.2 Layer 2 — Execution / validation / repair

| File | Lines | Role | Spec |
|------|-------|------|-----|
| `execution_decision.py` | 630 | The V2 deterministic classifier. `classify_execution_decision(...)`, `from_verification_result`, `is_blocked_decision`, `block_status`. 5 decision strings + the 6-rung hierarchy. | §6.9 |
| `execution_decision_engine.py` | 1228 | The V3 engine separating recommendation (verifier) from decision (engine). `decide(...)`, 7 modes, 6 stages, 6-source precedence ladder, `ExecutionPlanner` (4 persisted artifacts under `<workspace>/execution/`). | §6.10 |
| `execution_mode_gate.py` | 423 | User-facing depth CHOICE before build; pure wrapper over `execution_decision`. `resolve_mode`, `prompt_execution_mode`, `fixed_selection_fn`, `save_mode_selection`. | §6.11 |
| `execution_authority.py` | 400 | The SINGLE decision-resolution authority. `resolve(objective, ...)`, precedence `override > policy > planner > default`, `[AUTHORITY]` log lines. | §6.12 |
| `implementation_engine.py` | 1628 | Phase 2 `NativeImplementationEngine` + `LegacyImplementationEngine`. 5-node execution graph, `TransitionEngine`, `RetryPolicy`, `ExecutionRecorder`, `AgentManager`, `StateManager`. Pure stdlib at import. | §6.13 |
| `implementation_executor.py` | 554 | The legacy repair loop wrapped by `LegacyImplementationEngine`. `MAX_REPAIR_ATTEMPTS=3`, `_execute_with_repair_loop`, `_emit_human_required`, `_load_build_state`, `_tests_passed` (Phase 2 strengthened). | §6.14 |
| `validation_engine.py` | ~750 | Phase 3 `NativeValidationEngine`: `build_fn`, `test_fns` (unit/integration/smoke), `max_iterations`. `ValidationReport` shape. | §6.15 |
| `validation.py` | 819 | Central Validation Framework (Phase C, reuse over the hardening): `ValidationReport` + 6 validators + dispatcher. **§hardening** | (ref) |
| `repair_memory.py` | ~200 | Phase 3.1 `RepairMemory` SQLite: `record_attempt`, `is_blacklisted(strategy, target_files, failure_signature_value)`. `RepairAttempt`. | §6.16 |
| `telemetry.py` | ~250 | Observational-only recorder (`TelemetryEvent`, ETA, `performance_report`). **§hardening** Phase F. | (ref) |

### 3.3 Layer 3 — Integration

| File | Lines | Role | Spec |
|------|-------|------|-----|
| `project_planner.py` | 599 | AI Project Planner — decomposes objective into a work-package DAG via Cerebras GPT-OSS-120B. `validate_execution_graph`, `topological_order`, `save_execution_graph`. | §6.17 |
| `pipeline_scheduler.py` | 579 | Dependency-ordered dispatcher. `PENDING/READY/RUNNING/COMPLETED/FAILED/BLOCKED`. `run_scheduled_pipeline(max_workers=1)`. | §6.18 |
| `pipeline_runner.py` | 366 | Single entry: `execute(prompt)`, `execute_scheduled(prompt, ...)`. | §6.19 |
| `stage_logger.py` | 187 | `[PLANNER][GRAPH][SCHEDULER][RESEARCH][ARCHITECTURE][IMPLEMENTATION][BUILD][TEST][REPAIR]` tagged logging. `stage(name)` context manager. | §6.20 |
| `workflow_classifier.py` | 321 | Workflow classification: DISCUSSION / RESEARCH / ARCHITECTURE / IMPLEMENTATION. `classify(prompt)`. | §6.21 |
| `build_pipeline.py` | 997 | `run_build_pipeline`, `generate_build_spec`, `execute_build_spec`, `_resolve_execution_decision`, `resurrect_answer`, `aggregate_human_decisions`. | §6.22 |
| `build_spec_engine.py` | 577 | `build_spec(source_answer, debate_fn=None, source_result=None, ...)` → structured dict (Path 3 deterministic renderer). | §6.23 |
| `build_prompt_generator.py` | 252 | `extract_decisions/constraints/tradeoffs`, `generate_build_prompt`, `validate_spec_sections`. `SPEC_SECTIONS` (7 sections). | §6.24 |
| `build_workspace.py` | ~250 | `create_workspace(source_question=...)`, `_workspace_dir(workspace_id)`, `write_build_state`, `read_build_state`. Configurable `WORKSPACE_ROOT`. | §7.4 |
| `decision_renderer.py` | 1233 | Phase-1 deterministic renderer. `_architecture_components` is the SOLE builder of architecture. `render_*` functions. `validate_synthesis_consistency`, `contradiction_warning`. | §6.25 |
| `manifest.py` | ~280 | `BuildManifest` (references-only). **§hardening** | (ref) |
| `resume.py` | ~200 | `resume_build / resume_execution / resume_validation`. **§hardening** | (ref) |
| `lifecycle.py` | ~200 | `BuildLifecycle` enum + transition table. **§hardening** | (ref) |
| `artifacts.py` | ~250 | `Artifact` + `ArtifactKind` + `ArtifactStore`. **§hardening** | (ref) |
| `errors.py` | ~120 | `BuildError` + `ErrorKind`. **§hardening** | (ref) |
| `execution_planner.py` | ~350 | Deterministic `plan(spec) -> ExecutionPlan`. **§hardening** | (ref) |

### 3.4 Layer 3 — Provider + workspace infrastructure

| File | Lines | Role | Spec |
|------|-------|------|-----|
| `provider_router.py` | 366 | The 4-tier failover chain (Cerebras → Groq → OpenRouter → Ollama). Key rotation, `ProviderHealth`, failover / non-failover patterns. | §6.26 |
| `cerebras_client.py` | ~80 | `generate(prompt, module=...)` → `provider_router.generate`. Backward-compat. | §6.26 |
| `groq_client.py` | ~120 | Groq direct client (used by router). Key rotation. | §6.26 |
| `openrouter_client.py` | ~150 | OpenRouter direct client (used by router). Key rotation + many models. | §6.26 |
| `ollama_client.py` | ~80 | Ollama local client (used by router fallback). | §6.26 |
| `workspace_orchestrator.py` | 309 | `submit_workspace`, `follow_up_workspace`, `resolve_human_requirement`, `get_workspace_status`. | §6.27 |
| `workspace_manager.py` | 867 | `Workspace` dataclass + `WorkspaceManager` CRUD over `database/workspaces.db`. `WorkspaceStatus` enum. | §7.5 |
| `workspace_knowledge.py` | 259 | Per-stage knowledge persistence under `<workspace>/{research,verified,architecture,implementation}/<package_id>.{md|json}`. `load_stage_input`. | §7.6 |
| `job_store.py` | ~150 | SQLite `jobs` table for the legacy `orchestrator.submit_job` API. | §7.7 |
| `orchestrator.py` | ~120 | Legacy `submit_job` / `get_result` / `get_status` job API. | §6.28 |
| `sol0mon_graph.py` | ~250 | LangGraph workflow + `MemorySaver` checkpointer. (Optional.) | §6.29 |

### 3.5 Layer 5 — Phase 4 (one module, fifteen components)

`workflow_orchestrator.py` — 2043 lines, 15 components — fully specified in
`workflow_orchestration.md` (§3 of that document).

### 3.6 The test suite

`run_all_tests.py` is the consolidated runner; `test_suite.py` is the
suite-wide smoke (the `>120s` live-Cerebras SKIP there is pre-existing and
unrelated to any layer here). Per-layer regression tests live in
`tests/test_<layer>.py`:

- Layer 1: `tests/test_decision_model_canonical.py`,
  `test_phase1_root_cause_regression.py`, `test_research_verification_integration.py`,
  `test_research_verification_integration_v2.py`, `test_deterministic_synthesis.py`,
  `test_synthesis_consistency.py`, `test_synthesis_verified_decisions.py`,
  `test_deterministic_decisions.py`, `test_contradiction_validator_v2.py`,
  `test_workspace_followup_verification.py`, `test_human_escalation_e2e.py`,
  `test_research_v2.py`, `test_research_driven_debate.py`,
  `test_decision_model_persistence.py`.
- Layer 2: `tests/test_execution_decision.py`, `test_execution_decision_v2.py`,
  `test_execution_decision_engine.py`, `test_execution_mode_gate.py`,
  `test_implementation_engine.py`, `test_repair_loop.py`,
  `test_validation_engine.py`, `test_repair_memory.py`.
- Layer 3: `tests/test_workspace_flow.py`, `test_build_prompt_generator.py`,
  `test_project_planner.py`, `test_pipeline_scheduler.py`,
  `test_build_spec_pipeline.py`, `test_build_spec_artifact`,
  `test_human_decision_manager.py`, `test_workspace_orchestrator.py` (legacy),
  `test_answer_resurrection_pipeline.py`.
- Hardening: `tests/test_lifecycle.py`, `test_artifacts.py`,
  `test_validation.py`, `test_execution_planner.py`, `test_resume.py`,
  `test_consolidation.py`.
- Phase 4: `tests/test_workflow_orchestrator.py` (63 tests, 20 sections).

---

End of Batch 1. Next batch: §4 single-source-of-truth invariants + §5 architectural rationale continuation.

---

## 4. Single-source-of-truth invariants (the cross-layer contract)

Every layer below depends on the exact invariants in this section. A
violation of any of them is a regression.

### 4.1 The Canonical Decision Model invariant

> `decision_model.decision_records(verification_result)` is the SOLE owner
> of decision interpretation. Every downstream consumer — Architecture
> Renderer, Build Spec Engine, Executor, Human Synthesis, Persistence,
> Telemetry, and the contradiction validator — MUST read a verified
> decision through this module. Never parse `verification_result`
> directly, and never read previously-rendered markdown.

— `decision_model.py:4-9`.

The accessors `winner_text(claim)`, `winner_tokens(claim)`,
`rejected_options(claim)`, `derive_rejected_options(finding)` are the only
allowed interpretation primitives. `decision_renderer` re-exports them
for back-compat; the parsing logic is no longer reducible to two divergent
copies anywhere in the codebase (`decision_model.py:16-22`).

Pinned by:
- `tests/test_phase1_root_cause_regression.py::test_decision_model_does_not_import_decision_renderer`
  — `decision_renderer` MUST NOT be in `sys.modules` after importing
  `decision_model` (the sole-ownership inversion).
- `tests/test_decision_model_canonical.py::test_answer156_no_postgresql_in_any_winner_tokens`
  — across EVERY record, `postgresql` is absent from `winner_tokens`;
  record count is 2 (the answer-156 fixture).

### 4.2 The SQLite/PostgreSQL invariant (extended into the hardening)

No rejected option (e.g. `postgresql`) may ever appear in a downstream
consumer's recommendation, components list, or winner field. This is the
"answer-156 rejected-option-leak" regression. Defense-in-depth:

1. `decision_model.rejected_options(claim)` recognizes the enriched
   reject grammar (`reject|rejects|rejecting|rejected|rules out|ruled
   out|argues against|argued against|discards|discarded|excludes|
   excluded|off-limits|no longer|explicitly (rejects|rules out|discards|
   excludes)`) case-insensitively AND strips markdown emphasis
   (`*_\\\``) before matching — so `argues *against* PostgreSQL` is caught.
2. `decision_model._winner_raw(d)` NEVER falls back to `dispute` /
   `dispute_text` (those name BOTH options and were the leak source).
3. `decision_renderer._architecture_components` matches component aliases
   against `winner_tokens` ONLY, and suppresses any component whose alias
   appears in `rejected_options(d)` (defense-in-depth).
4. `validate_synthesis_consistency` (FIX 6) catches the leak in any
   LLM-narrative section, appends a `# Contradiction Warning`; the
   deterministic sections stand regardless (verifier wins).
5. `validate_decision_model` (Hardening Phase C) adds a LIVE check
   (`test_validation.py::test_decision_model_fails_on_winner_is_rejected`).

### 4.3 The Crashed-Stage invariant

A persisted `current_state` always reflects the last state we
transitioned *into*, saved *after* the advance. A crash mid-stage leaves
the state at the *prior* state, so resume correctly re-runs the prior
stage. This holds in Phase 2 (`StateManager.save` after every node),
Phase 4 (`RecoveryManager.save_state` after every transition), and the
debate engine (`answer_memory.save_debate` after every dispute).

### 4.4 The No-Silent-Termination invariant (Phase 4)

Every non-`COMPLETED` terminal workflow state has a non-empty `error` in
the result dict. The PolicyEngine never silently terminates — every
refusal is a structured `PolicyDecision`. `test_every_failure_surfaces_an_error`.

### 4.5 The No-Silent-Restart invariant (Phase 4)

`resume` on a workspace without a checkpoint returns a structured error;
it NEVER starts a fresh workflow. `test_resume_no_state_returns_structured_error`,
`test_fault_injection_delete_state_returns_no_checkpoint_error`.

### 4.6 The Re-entrancy invariant (Phase 4)

Each `StressTestEngine` thread builds a fresh orchestrator (via
`orchestrator_factory`), a fresh recorder, a fresh state machine, and a
fresh artifact manager. No shared mutable state crosses workspace
boundaries. `test_concurrent_workflows_distinct_workspaces`.

### 4.7 The Telemetry Is Observational invariant

No telemetry value is ever read back into control flow.
`telemetry.recorder` is disabled by default; every emit/observe/start_stage
/end_stage call is a strict no-op when unconfigured AND swallows its own
errors (`debate_engine.py:32-38`); `tests/test_telemetry.py` pins it.

### 4.8 The Pure-Stdlib-At-Import invariant (Layers 2, 4, 5)

Phase 4's `workflow_orchestrator.py` transitively imports no provider
module (`cerebras_client`, `ollama_client`, `openrouter_client`,
`groq_client`, `gpt_worker`, `gemini_worker`, `provider_router`,
`selenium`, `requests`, `httpx`, `urllib3`). Pinned by
`test_no_forbidden_modules_transitively_imported`. Same invariant for
`decision_model.py` (`test_decision_model_does_not_import_decision_renderer`).
Provider modules are an engine-layer concern, injected via callable seams.

### 4.9 The Non-Negotiable Abstention invariant

Only `IMPLEMENTATION_REQUIRED` (and `STOP_HUMAN_REQUIRED`, which defers to
the pending-decision gate) may reach the build pipeline's spec step; every
other execution decision is a hard stop at the gate
(`execution_decision.py:20-22`). The verifier's emit is *demoted to a
recommendation*: only `STOP_UNRESOLVED_DISPUTES` retains hard-stop authority
(a true governance constraint — the debate outcome is incomplete). Every
other emit becomes non-binding; the engine may override per user intent
(`execution_decision_engine.py:311-342`). Research signals never override
explicit user intent.

### 4.10 The Deterministic-Rendering invariant

The seven decision/narrative sections in the synthesized answer —
`# Verified Decisions`, `# Verified Tradeoffs`, `# Human Decisions
Required`, `# Architecture`, `# Implementation Plan`, `# Migration Path`,
`# Risks` — are generated DETERMINISTICALLY from `verification_result` +
`governance_context` by `decision_renderer`; NO LLM. The verifier is the
source of truth and the final answer is fully reproducible from the
decision model (`synthesis_engine.py:293-312`). When a seam narrative
contradicts the verifier (FIX 6), a `contradiction_warning` is appended;
the deterministic sections stand regardless.

Pinned by `tests/test_deterministic_synthesis.py::test_determinism_same_inputs_identical_output`,
`test_no_model_call_on_default_path`, `test_seven_sections_present_in_canonical_order`,
`test_debate_55_renders_seven_decisions_deterministically`.

---

## 5. Architectural rationale (continued)

### 5.1 Why a single authority per concern

Every prior incident in the project's history traces to two components
re-deriving the same fact and diverging:

- **Answer 156**: `decision_renderer` parsed the finding prose for
  `postgresql` differently than the verifier stored it; it recommended the
  rejected option in the Architecture section. Fix: `decision_model`
  is the SOLE owner; the renderer reads `winner_tokens` /
  `rejected_options` from it and never reparses prose.
- **The "no silent restart" rule**: a workspace without a checkpoint
  was sometimes silently starting a fresh workflow. Fix: `resume`
  returns a structured error, never starts fresh.
- **The "artificial consensus" rule**: consensus rose despite
  disagreement because `_eval_consensus` was permissive. Fix: the
  conflict extractor pulls structured disagreements ONCE; the debate
  engine debates each one individually; stagnation tracking + the verifier
  review break deadlocks instead of false-advancing consensus.
- **The "lost decision" rule**: resolved disputes reappeared later because
  memory was per-debate, not cumulative. Fix: `ArchitectureMemory`
  (`debate_engine`) accumulates verified decisions and injects them as
  immutable constraints into subsequent disputes; "SETTLED. Do not
  contradict or re-debate them."
- **The "research-after-architecture" rule**: the pipeline ran
  architecture before research, so the architecture took positions on
  unsettled questions. Fix: the Project Planner (Layer 3) decomposes the
  objective into a work-package DAG where research precedes architecture;
  the Pipeline Scheduler's `stage_gates` enforce that architecture may not
  run before its producers.

### 5.2 Why callable seams instead of ABC hierarchies

The codebase does DI via **callable seams** — every collaborator is an
optional `Callable` kwarg:
- `debate_fn` (build_spec_engine)
- `executor_fn` (build_pipeline)
- `agent_fn` (implementation_executor)
- `test_fn` (implementation_executor)
- `coding_agent_fn`, `testing_agent_fn`, `repair_agent_fn` (implementation_engine)
- `mode_select_fn` (execution_decision_engine / build_pipeline)
- `input_fn` (execution_mode_gate's `prompt_execution_mode`)
- `research_fn` (verification_engine)
- `planner_fn` (build_pipeline — Phase 4 consolidation)
- `build_fn`, `test_fns` (validation_engine)
- `mode_select_fn`, `orchestrator_factory`, `workspace_id_factory`,
  `injectors`, `workloads` (Phase 4 engines)

Each accepts `None` and falls back to a default. Tests inject fakes;
production wires real components. There is exactly ONE concrete
implementation of each collaborator today, so a Protocol/ABC layer with one
implementation would be the canonical YAGNI anti-pattern the ponytail
ladder forbids (see §18).

### 5.3 Why the layers compose, not overlap

| Concern | Layer 4 (Hardening) owns | Layer 5 (Phase 4) owns |
|---------|---------------------------|-------------------------|
| State machine | `BuildLifecycle` (NEW → VERIFIED → SPEC_READY → … → COMPLETE) | `WorkflowStateMachine` (PLANNING → RESEARCH → … → COMPLETED) |
| Resume | `resume.resume_build` (build-lifecycle-level) | `RecoveryManager` (workflow-level) |
| Artifacts | `ArtifactStore` (typed `Artifact` envelopes under `<workspace>/artifacts/`) | `ArtifactManager` (flat file registry, `artifact_manifest.json` under `<workspace>/workflow/`) |
| Validation | `validation.validate(stage, artifact)` (post-hoc cross-artifact) | `Stage.validate(ctx)` (per-stage pre-condition) |
| Manifest | `<workspace>/manifest.json` (build's completed manifest) | `<workspace>/package/manifest.json` (11 packaging outputs) |
| Errors | `BuildError` + `ErrorKind` | The orchestrator's structured result dict (errors-as-data) |

A production IMPLEMENTATION stage internally calls
`lifecycle.advance_lifecycle`; Phase 4 itself never touches the build
lifecycle. The hardening's `ArtifactStore` writes under
`<workspace>/artifacts/`; Phase 4's `ArtifactManager` writes
`artifact_manifest.json` under `<workspace>/workflow/`. Different files,
different subdirectories, different responsibilities — and the two resume
paths are independent and non-overlapping.

### 5.4 Why determinism is non-negotiable where it's possible

When a stage CAN be deterministic, it MUST be — the alternative is
"In the answer-156 fixture, the renderer recommended PostgreSQL 60% of the
time and SQLite 40% of the time." Specifically:
- `decision_model` is pure stdlib — no I/O, no LLM, no randomness.
- `decision_renderer._architecture_components` is pure given the
  verification result.
- `build_spec_engine.build_spec(debate_fn=None)` (Path 3) is pure given
  `source_result`.
- `execution_planner.plan(spec)` is pure given the spec.
- `WorkflowStateMachine.next_state` is a static transition table.
- `PolicyEngine.evaluate` is pure.

Pinned by: `test_plan_is_deterministic`,
`test_determinism_same_inputs_identical_output`,
`test_no_model_call_on_default_path`,
`test_deterministic_for_same_inputs`,
`test_benchmark_determinism_same_workload_same_state`.

---

End of Batch 2. Next batch: §6 Layer 1 contracts — Decision Model (6.1), Debate Engine (6.2), Verification Engine (6.3).

---

## 6. Layer 1–3 component specifications and public interfaces

### 6.1 `decision_model.py` — the Canonical Decision Model

Pure stdlib. No LLM. No I/O. The SINGLE OWNER of decision interpretation.

#### 6.1.1 The canonical record contract

```python
FIELDS = (
    "normalized_key",
    "dispute",
    "status",
    "confidence",
    "finding_raw",
    "winner_text",
    "winner_tokens",
    "rejected_options",
)

def decision_records(verification_result: dict | None) -> list[dict]:
    """Returns ONE record per RESOLVED claim in `_resolved_claims(...)`.
    Each record dict has EXACTLY the keys of `FIELDS`.
    """
```

Each record:

```python
{
    "normalized_key":   "_str(c.get('normalized_key'))",        # verifier's stable snake_case key
    "dispute":          "_str(c.get('dispute'))",                # the dispute title (names BOTH options; informational only; NEVER a winner)
    "status":           "_str(v.get('status'))",                # "RESOLVED" | "UNRESOLVED" | "EQUIVALENT"
    "confidence":       "v.get('confidence')",                  # int | None, 0-100
    "finding_raw":      "_str(v.get('finding'))",               # verbatim verifier prose
    "winner_text":      "winner_text(c)",                      # cleaned winner portion (CamelCase preserved)
    "winner_tokens":    "winner_tokens(c)",                    # lowercase significant tokens of winner_text
    "rejected_options": "rejected_options(c)",                 # lowercase tokens of the rejected option(s)
}
```

Field semantics (verbatim from `decision_model.py:23-48`):
- `winner_text` — the option the verifier chose. Computed from the WINNER
  PORTION of the finding ONLY (text after the winner marker, cut at the
  first REJECT signal — so the rejected option's name can never leak into
  the winner). NEVER falls back to the dispute title.
- `winner_tokens` — the significant tokens of `winner_text` (for matching
  against component alias tables).
- `rejected_options` — the options the verifier ruled out. Source order:
  1. `verification.rejected_options` (the structured field, persisted by
     `verification_engine._persist_rejected_options`),
  2. Fallback: `derive_rejected_options(finding)` (enriched grammar +
     emphasis-strip).

#### 6.1.2 Interpretation primitives

```python
def _tokens(text: str, stop: set[str] = _OPTION_STOP) -> list[str]: ...
def _first_reject_pos(finding_lower: str) -> int: ...
def _strip_emphasis(text: str) -> str: ...                  # removes "*_`" before matching
def _winner_phrase(finding: str) -> str: ...
def _clean_winner_name(phrase: str) -> str: ...
def _is_clean_label(name: str) -> bool: ...
def _winner_raw(d: dict) -> str: ...                          # NEVER falls back to dispute/dispute_text
def winner_text(claim: dict) -> str: ...                       # cleaned winner portion
def winner_tokens(claim: dict) -> list[str]: ...
def derive_rejected_options(finding: str) -> list[str]: ...
def rejected_options(claim: dict) -> list[str]: ...
```

#### 6.1.3 Constants

- `_STOPWORDS` (verbatim lines 100-119) — the stop-word set.
- `_GENERIC_OPTION_WORDS = frozenset({"architectural", "fundamental",
  "implementation", "approach", "strategy", "design", "system",
  "platform", "conflict", "resolved", "decision", "winner", "rationale"})`.
- `_OPTION_STOP = set(_STOPWORDS) | set(_GENERIC_OPTION_WORDS)`.
- `_REJECT_SIGNALS_RE` (lines 172-183):
  `r"\b(reject|rejects|rejecting|rejected|rules\s+out|ruled\s+out|argues\s+against|argued\s+against|discards|discarded|excludes|excluded|off[-\s]limits|no\s+longer|explicitly\s+(?:rejects|rules\s+out|discards|excludes))\b"`,
  flags `re.IGNORECASE`.
- `_WINNER_MARKERS` = `("in favor of", "in favour of", "selecting",
  "adopting", "choosing", "the selection of", "with the explicit
  selection of", "resolved by", "resolved through", "converges on",
  "the explicit selection of")`.
- `_EMPHASIS_CHARS = "*_\`"`.

#### 6.1.4 Invariants

- The rejected option is recognized as rejected (`test_rejected_option_is_recognized_for_argues_against_phrasing`).
- The rejected option does NOT leak into winner tokens (`test_rejected_option_does_not_leak_into_winner_tokens`).
- The actual winner IS present (`test_actual_winner_is_present_in_winner_tokens`).
- The structured field is preferred; the fallback is used only when absent (`test_structured_rejected_options_field_is_preferred`).
- `[]` structured field is treated as ABSENT, falls back to derivation (`test_fallback_used_when_structured_field_empty`).
- `_winner_raw` NEVER falls back to `dispute`/`dispute_text`.
- Pure-stdlib import guard: forbidden = `{"cerebras_client", "ollama_client",
  "openrouter_client", "groq_client", "gpt_worker", "gemini_worker",
  "decision_renderer"}`; asserts none are loaded (`__main__` lines 607-613).

#### 6.1.5 Tests pinning the contract

`tests/test_decision_model_canonical.py`:
- `test_rejected_option_is_recognized_for_argues_against_phrasing`
- `test_rejected_option_does_not_leak_into_winner_tokens`
- `test_actual_winner_is_present_in_winner_tokens`
- `test_rejected_option_absent_when_unification`
- `test_structured_rejected_options_field_is_preferred`
- `test_fallback_used_when_structured_field_absent`
- `test_fallback_used_when_structured_field_empty`
- `test_answer156_no_postgresql_in_any_winner_tokens`
- `test_derive_rejected_options_handles_reject_verb` / `_argues_against` / `_rules_out` / `_empty_for_unification` / `_empty_for_empty_finding`
- `test_winner_tokens_exclude_dispute_only_options`

`tests/test_phase1_root_cause_regression.py`:
- `test_decision_model_does_not_import_decision_renderer`
- `test_decision_records_expose_canonical_contract`
- `test_winner_text_uses_winner_portion_only_never_dispute_title`
- `test_winner_tokens_exclude_postgresql_for_156`
- `test_rejected_options_list_postgresql_for_156`
- `test_validator_flags_narrative_recommending_postgresql_for_156`
- `test_render_architecture_does_not_emit_postgresql_for_156`
- `test_render_architecture_for_156_emits_no_components`
- `test_debate55_deterministic_narrative_is_consistent`

`tests/test_decision_model_persistence.py`:
- `test_seven_resolved_disputes_survive_into_resolved_decisions` — 7 decisions survive `answer_memory.save_debate → get_debate`; each carries non-empty `decision/winner/rationale/finding`, `confidence > 0`, `decision != dispute`, `rationale == finding`.
- `test_verification_result_preserved_verbatim` — `verification_result` round-trips untouched through persistence (JSON-TEXT column).
- `test_shape_exposes_decision_model` — `answer_loader` exposes `verification_result`, `resolved_decisions`, `verified_decisions` (Schema-A).

---

### 6.2 `debate_engine.py` — single-dispute consensus resolution with governance

#### 6.2.1 Constants (verbatim lines 43-59)

```python
CONSENSUS_THRESHOLD      = 92
MAX_ROUNDS_PER_DISPUTE   = 15
MAX_MEMORY_CHARS         = 8000
STAGNATION_CHECKS        = 3
STAGNATION_MAX_WARNINGS  = 2
MAX_VERIFICATION_RETRIES = 1
MAX_CONSTRAINTS          = 10
PRIORITY_WEIGHTS = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}

DOMAIN_PRIORITY = {
    "HIGH":   ["database", "storage", "consistency", "architecture pattern",
               "scaling", "security", "deployment", "orchestration", "data",
               "infrastructure", "networking", "failover", "disaster"],
    "MEDIUM": ["framework", "service", "api", "messaging", "cache", "queue",
               "workflow", "pipeline", "observability", "monitoring"],
    "LOW":    ["library", "naming", "formatting", "tooling", "language", "sdk"],
}
```

Additional embedded constants:
- Batch validation confidence floor: `70` (`if conf < 70: continue`).
- Priority sort order: `{"HIGH": 0, "MEDIUM": 1, "LOW": 2}`.
- Consistency-audit pair ceiling: `max_pairs = 20`.
- Verification hard-failure gate: `v_score < 90 and unresolved > 0`.
- Consensus check fires every 3 rounds.
- Stagnation triggers when `max(recent) - min(recent) < 5`.

#### 6.2.2 `Dispute` dataclass

```python
@dataclass
class Dispute:
    dispute_text: str
    normalized_key: str
    gpt_source: str = ""
    gemini_source: str = ""
    gpt_current: str = ""
    gemini_current: str = ""
    priority: str = "MEDIUM"
    priority_weight: int = 3
    round_number: int = 0
    consensus_score: int = 0
    consensus_history: list = field(default_factory=list)
    stagnation_warnings: int = 0
    verification_retries: int = 0
    status: str = "active"     # active | pending_verification | resolved | deadlocked | tradeoff | human_required
    outcome_type: str = ""     # VERIFIED_DECISION | VERIFIED_TRADEOFF | HUMAN_REQUIRED
    governed_outcome: dict | None = None
    information_state: dict | None = None

    def to_dict(self) -> dict
```

#### 6.2.3 Public surface

```python
class ArchitectureMemory:
    def __init__(self) -> None
    def resolve(self, dispute: Dispute) -> None              # store a verified decision. Called only after verification passes.
    def is_resolved(self, normalized_key: str) -> bool
    def get_injection_context(self, current_dispute: Dispute = None) -> str
    def get_all(self) -> list[dict]

class DebateEngine:
    def run(self, question: str, memory_ids: list = None) -> dict
    def _run(self, question, gpt, gemini, memory_ids) -> dict
    def _build_dispute_queue(self, raw_disputes, gpt_source, gemini_source) -> list[Dispute]
    def _debate_dispute(self, question, dispute: Dispute, gpt, gemini) -> str
    def _verifier_review_dispute(self, dispute: Dispute, info_state) -> GovernedOutcome
    def _run_verification(self, question, disputes, gpt_current, gemini_current, gpt, gemini, arch_memory, final_consensus_score, gov_result=None) -> dict
    def _run_consistency_audit(self, arch_memory, disputes, research_context="") -> list[str]
    def _synthesize_and_return(self, question, gpt_current, gemini_current, disputes, verification_result, score, status, arch_memory=None, gov_result=None) -> dict
    def _attach_execution_decision(self, question, governance_context, verification_result, synthesized_answer) -> dict
```

#### 6.2.4 The `_debate_dispute` flow (returns one of `resolved` / `verified_decision` / `verified_tradeoff` / `human_required`)

Per round:
1. Critique-then-revise each side every round.
2. Consensus check every 3 rounds; if `score >= CONSENSUS_THRESHOLD` →
   return `"resolved"`.
3. `info_state.is_stagnating(threshold=STAGNATION_CHECKS)` → call
   `_verifier_review_dispute`; map:
   - `VERIFIED_DECISION` → set agreed position, return
     `"verified_decision"`.
   - `VERIFIED_TRADEOFF` → return `"verified_tradeoff"`.
   - else → return `"human_required"`.
4. `_detect_stagnation(dispute) == "deadlock"` → re-run verifier review
   with the same 3-way mapping.
5. `_detect_stagnation(dispute) == "warning"` → force-engagement extra
   prompt.
6. `MAX_ROUNDS_PER_DISPUTE` exhausted → verifier review with the same
   mapping.

#### 6.2.5 Outer pipeline status transitions (lines 466-486)

| Dispute terminal state | Pipeline action |
|------------------------|-----------------|
| `resolved` / `verified_decision` | `dispute.status = "pending_verification"` |
| `verified_tradeoff` | `dispute.status = "tradeoff"` |
| `human_required` | `dispute.status = "human_required"`; `_enqueue_human_required` called |
| else | `dispute.status = "deadlocked"` |

#### 6.2.6 `_run_verification` gate (lines 938-991)

Hard-failure when `v_score < 90 and unresolved > 0` — saves a
`[FAILED VERIFICATION]` row to `answer_memory` and returns
`{"_failed": True, "status": "verification_failed", ...}`. Otherwise maps
pending disputes: `arch_memory.resolve(d)` + `resolved` if verified;
`tradeoff` / `human_required` if escalated; `active` if a re-debate retry
(`d.verification_retries < MAX_VERIFICATION_RETRIES`).

#### 6.2.7 `ArchitectureMemory.get_injection_context`

Emits a `<resolved_constraints>` block with up to `MAX_CONSTRAINTS=10`
lines, dropped count surfaced, then:
> "The above decisions are SETTLED. Do not contradict or re-debate them."

#### 6.2.8 The execution-decision seam (lines 543-567)

Pure enrichment: no change to debate/verification/synthesis mechanics.
Attaches the deterministic `execution_decision` to the result dict AND
into `verification_result` so it persists through `answer_memory`
(`verification_result` is a JSON-TEXT column) and survives resurrection
and workspace resume.

#### 6.2.9 Pre-emitted outcome honoring (lines 833-842, 899-908)

Honor a pre-emitted verdict (`HUMAN_REQUIRED` from research-budget
exhaustion) without a fresh model call (Phase 4 wiring) before falling
back to `verifier_review`.

#### 6.2.10 Persistence surface

- Writes via `answer_memory.memory.save_debate(...)` (full result dict +
  `verification_result` JSON) — lines 977-985, 1156-1167.
- `human_decision_queue.enqueue_human_decision(...)` via
  `_enqueue_human_required`, line 332.
- No direct file/table writes from this module.

#### 6.2.11 Tests

- `tests/test_research_driven_debate.py` — `test_1_planner_runs` …
  `test_7_follow_up_reuse` (research planner, evidence injection,
  topic-keyed reuse, no-duplicate-calls, no model/browser calls, workspace
  persistence, follow-up reuse).
- `tests/test_research_v2.py` — `test_1_evidence_injected_into_dispute_prompts`
  through `test_10_deterministic_output` (10 numbered tests).
- `tests/test_workspace_followup_verification.py` —
  `test_workspace_followup_verification_no_name_error` (verify_all_disputes
  reachable on follow-up; `validated_disputes` from pending only;
  `use_external_research is False`),
  `test_pending_dispute_status_is_pending_verification`.

End of Batch 3. Next batch: §6.3 Verification Engine + §6.4 Synthesis Engine.

---

### 6.3 `verification_engine.py` — verifier over batch disputes with research escalation

#### 6.3.1 Constants

```python
MAX_RESEARCH_ESCALATIONS = 3    # Fix 2: Cap high-overhead research passes per session
MAX_BATCH_DISPUTES       = 15    # Fix 4: Hard limit to shield verifier prompts from token bloat
```

#### 6.3.2 Public surface

```python
def extract_json(text)
def extract_relevant_evidence(dispute, position_text, window_size=800)
def _normalize_research_output(output)
def _persist_rejected_options(res: dict) -> list[str]
def _default_research_fn(topic)
def attach_research_evidence(dispute_dicts, verification_result)
def extract_pre_emitted_outcome(verification_result, normalized_key)
def run_verification_batch_inference(question, batch_payload)
def verify_all_disputes(
    question,
    validated_disputes,
    gpt_position,
    gemini_position,
    use_external_research=False,
    research_fn=None,
    max_research_escalations=MAX_RESEARCH_ESCALATIONS,
    prior_research_evidence=None,
)
```

#### 6.3.3 Per-record status enum

`{"RESOLVED", "UNRESOLVED", "EQUIVALENT"}` — coerced to `"UNRESOLVED"` when
outside the enum. Bucketing:
- `RESOLVED` → `resolved_claims`
- `EQUIVALENT` → `equivalent_claims`
- else → `unresolved_claims`

#### 6.3.4 Research state machine per dispute

| Condition | Action |
|-----------|--------|
| `needs_research and prior_ev` | Reuse stored evidence; no escalation increment; no model/API cost; append to `re_evaluate_batch`. |
| `needs_research and budget_available` | `research_escalation_count += 1`; call `resolver`; build Pass 2 re-evaluation record. |
| `needs_research and not budget_available` | Emit `HUMAN_REQUIRED` `governance_outcome` with `reason_code="research_budget_exhausted"`, `confidence=0`, `verification.status="UNRESOLVED"`. |
| else | Record carries `verification.rejected_options = _persist_rejected_options(res)` plus Phase 5/7 audit fields (`used_research`, `research_dispute_key`, `research_summary`, `research_source_count`). |

#### 6.3.5 The returned `verification_result` shape (lines 544-553)

```python
{
    "resolved_claims":         [...],   # verification.status == RESOLVED
    "equivalent_claims":       [...],   # EQUIVALENT
    "unresolved_claims":       [...],   # UNRESOLVED (+ governance_outcome, evidence)
    "verification_score":     int,     # (resolved + equivalent) / total * 100
    "truncated_count":        int,
    "research_evidence":      [...],    # {dispute_key, normalized_key, sources, summary, timestamp}
    "research_escalation_count": int,
    "research_budget_exhausted": bool,
}
```

#### 6.3.6 Edge cases

- Zero-dispute early return (lines 264-270): all buckets empty, `verification_score = 100`, `truncated_count = 0`.
- Truncation ceiling (lines 282-287): disputes beyond `MAX_BATCH_DISPUTES=15` are sliced and counted in `truncated_count` as **UNVERIFIED** ("honest audit trail").
- Inference-failure fallback (lines 312-316): each unreported key → `{"status": "UNRESOLVED", "confidence": 50, "finding": "Inference network error fallback", "requires_external_evidence": False}`.

#### 6.3.7 `_persist_rejected_options` (lines 114-126)

Prefer the LLM's structured `rejected_options`; fall back to
`decision_model.derive_rejected_options(finding)`; canonical lowercase.
This is the structured field that the Decision Model reads.

#### 6.3.8 Invariants

- "`use_external_research` is retained for backward-compat … but no longer
  gates research — the trigger is the verifier's own
  `requires_external_evidence` flag."
- "`research_fn` is a seam: tests inject an offline fake; production leaves
  it None."
- The returned `verification_result` (a plain dict) is later persisted by
  `AnswerMemory.save_debate(... verification_result=verification_result)` as
  a JSON-TEXT column in `answer_memory.db` (debates table). Round-trips
  untouched through persistence (`test_verification_result_preserved_verbatim`).

#### 6.3.9 Tests

- `tests/test_research_verification_integration.py`:
  - `test_requires_external_evidence_triggers_research` — `requires_external_evidence=True` triggers `research_fn`.
  - `test_no_research_runs_when_evidence_sufficient` — `requires_external_evidence=False` skips research.
  - `test_research_evidence_shape_is_persistable` — records carry `{dispute_key, normalized_key, sources, summary, timestamp}`.
  - `test_workspace_resume_reloads_evidence` — `prior_research_evidence` produces Phase 5 reuse (no re-fetch).
  - `test_attach_research_evidence_merges_by_normalized_key`.
  - `test_verifier_receives_research_evidence_in_pass2`.
  - `test_research_changes_dispute_outcome` — research shifts a dispute from UNRESOLVED to RESOLVED.
  - `test_exhausted_research_becomes_human_required` — `research_escalation_count ≥ max` emits `HUMAN_REQUIRED` `governance_outcome`.
  - `test_extract_pre_emitted_outcue_pre_emitted` — surfaces a pre-emitted verdict for honored escalation.
  - `test_research_evidence_survives_answer_persistence` — round-trip via `answer_memory` works.
  - `test_default_research_path_uses_verification_research_v2` — lazy import of `verification_research_v2.research_claim`.

- `tests/test_research_verification_integration_v2.py`: 10 numbered v2 tests.

---

### 6.4 `synthesis_engine.py` — composed deterministic narrative + LLM seam

#### 6.4.1 Public surface

```python
def extract_json(text)
def build_synthesis_prompt(question, gpt_final, gemini_final, validated_disputes,
                           consensus_score, verification_result=None,
                           governance_context=None, deterministic_context=None)
def synthesize_answer(
    question,
    gpt_final,
    gemini_final,
    validated_disputes,
    consensus_score,
    verification_result=None,
    governance_context=None,
    disputes=None,
    narrative_fn=None,
)
```

#### 6.4.2 The seven canonical narrative sections (in order)

1. `# Verified Decisions` (deterministic)
2. `# Verified Tradeoffs` (deterministic)
3. `# Human Decisions Required` (deterministic)
4. `# Architecture` (deterministic — from `render_architecture`)
5. `# Implementation Plan` (deterministic — from `render_implementation_plan`)
6. `# Migration Path` (deterministic — from `render_migration_path`)
7. `# Risks` (deterministic — from `render_risks`)

The first three are the decision sections; the last four are the narrative
sections. ALL seven are deterministic; none calls an LLM on the default
path. The seam (`narrative_fn`) is the only path that may invoke an LLM
(`synthesize_answer(... narrative_fn=None)` returns a fully-formed answer
with no model call — `test_no_model_call_on_default_path`).

#### 6.4.3 Synthesis prompt rules (verbatim, lines 192-213)

Twenty numbered rules. The most critical:
- Rule 11: "The consensus score is informational only. Do not weight it
  above the verifier's findings."
- Rule 13: "The GOVERNANCE OUTCOMES represent final determinations on
  deadlocked disputes — they are settled fact."
- Rule 14: "TRADEOFF outcomes mean BOTH options are valid — preserve
  both in your narrative. Do not force a choice between them."
- Rule 16: "The DETERMINISTIC SECTIONS are authoritative and
  pre-rendered. Never contradict a verified winner. Never recommend a
  rejected option."

#### 6.4.4 The composition contract

The answer is `deterministic.rstrip() + "\n\n" + narrative` where `narrative`
is the four `render_*` sections concatenated (Architecture, Implementation
Plan, Migration Path, Risks). The seam chooses the narrative path:
`if narrative_fn is not None` → LLM/test seam; `else` → fully deterministic
via `decision_renderer`.

#### 6.4.5 FIX 6 contradiction guard (lines 363-376)

After composing the answer, `validate_synthesis_consistency(answer,
verification_result, governance_context)` runs. On any contradiction (a
rejected option recommended OR a winner negated), a `contradiction_warning`
section is appended. The deterministic sections stand regardless — the
verifier wins.

#### 6.4.6 Tests

- `tests/test_deterministic_synthesis.py`:
  - `test_determinism_same_inputs_identical_output` — `a == b == c`.
  - `test_determinism_independent_of_consensus_score_and_positions` — perturbing question/positions/consensus yields identical output.
  - `test_rejected_options_never_recommended` — `validate_synthesis_consistency == []` on default deterministic answer.
  - `test_no_model_call_on_default_path` — monkeypatched `generate` raises if called; the default path completes.
  - `test_decision_renderer_module_has_no_generate_symbol` — `not hasattr(dr, "generate"|"llm"|"complete"|"chat"|"prompt")`.
  - `test_seven_sections_present_in_canonical_order` — the seven headers appear exactly once in canonical order.
  - `test_deterministic_sections_match_renderer_output_verbatim` — the three decision sections are byte-identical to `render_*` output.
  - `test_debate_55_renders_seven_decisions_deterministically` — "Decision Count: 7"; each dispute + finding verbatim.

- `tests/test_synthesis_consistency.py`:
  - `test_synthesized_answer_has_no_contradiction` — debate-55 consistent narrative → no contradictions.
  - `test_synthesized_answer_flags_recommended_rejected_option` — recommending WebContainers/IndexedDB/FastAPI/CRDT triggers `# Contradiction Warning`.
  - `test_consistency_clean_deterministic_section` — `render_verified_decisions` alone is consistent by construction.
  - `test_consistency_flags_each_rejected_recommendation` — flags for `webcontainers/webassembly/indexeddb/fastapi/crdts`.
  - `test_consistency_passes_when_rejected_options_only_rejected`.
  - `test_consistency_no_contradiction_for_empty_narrative`.
  - `test_synthesize_answer_is_narrative_only_via_seam` — `narrative_fn` is the only seam.

- `tests/test_synthesis_verified_decisions.py`:
  - `test_synthesis_prompt_includes_winner_confidence_rationale` — the prompt's RESOLVED block contains the finding (winner), confidence, rationale.
  - `test_build_prompt_forbids_rejected_architecture` — verified winner listed as `## Constraints (immutable)` MUST; rejected option carried in reasoning.
  - `test_decision_gaps_detects_spec_ignoring_verification` — `_spec_decision_gaps` flags a spec ignoring all verified decisions.

---

### 6.5 `conflict_extractor.py` — structured conflict extraction

#### 6.5.1 Public surface

```python
def extract_json(text)
def normalize_string_list(values)
def extract_conflicts(gpt_final_position: str, gemini_final_position: str) -> dict
```

#### 6.5.2 List caps

```python
agreed_claims     = agreed_claims[:50]
disputed_claims   = disputed_claims[:30]
resolved_disputes = resolved_disputes[:30]
```

#### 6.5.3 Severity decision (lines 207-234)

| Condition | Severity |
|-----------|----------|
| `model_severity == "HIGH"` | `HIGH` |
| `dispute_count >= 8` | `HIGH` |
| `model_severity == "MEDIUM"` | `MEDIUM` |
| `dispute_count >= 3` | `MEDIUM` |
| else | `LOW` |

`model_severity` coerced into `{"LOW", "MEDIUM", "HIGH"}`; else `LOW`.

#### 6.5.4 Return shape

```python
{"severity": ..., "agreed_claims": [...], "disputed_claims": [...], "resolved_disputes": [...]}
```

Failure fallback:
```python
{"severity": "HIGH", "error": "conflict_extraction_failed",
 "agreed_claims": [], "disputed_claims": ["Conflict extraction system processing failed."],
 "resolved_disputes": []}
```

#### 6.5.5 Prompt rules (lines 109-138)

- "Focus heavily on architectural, infrastructure, database, security,
  workflow, orchestration, deployment, scaling, observability, storage,
  networking, and implementation selections."
- Concrete tech choices ARE conflicts: `Temporal vs Airflow`, `Kafka vs
  NATS`, `PostgreSQL vs DynamoDB`, `Kubernetes vs Nomad`.
- Ignore wording, formatting, style, document structure.
- H11: "Removed position truncation. Full positions are now sent to
  Cerebras."

#### 6.5.6 Tests

- `test_suite.py::test_conflict_extractor` — module smoke test asserting the four-key return shape and the canonical error envelope on network failure.

---

### 6.6 `governance.py` — the three outcomes

#### 6.6.1 `OutcomeType` enum (verbatim, with docstrings)

```python
class OutcomeType:
    VERIFIED_DECISION = "VERIFIED_DECISION"
    """A superior solution exists. Store decision. Remove from active queue."""
    VERIFIED_TRADEOFF = "VERIFIED_TRADEOFF"
    """Multiple valid solutions exist. Store both with recommendation."""
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    """Insufficient evidence. Verifier cannot distinguish options. Risk is significant."""
```

#### 6.6.2 `GovernedOutcome` dataclass (lines 30-60)

```python
@dataclass
class GovernedOutcome:
    outcome_type: str       # VERIFIED_DECISION | VERIFIED_TRADEOFF | HUMAN_REQUIRED
    dispute_key: str
    dispute_text: str
    decision: str | None = None        # VERIFIED_DECISION
    option_a: str | None = None        # VERIFIED_TRADEOFF
    option_b: str | None = None        # VERIFIED_TRADEOFF
    recommendation: str | None = None  # VERIFIED_TRADEOFF
    confidence: int = 50
    reasoning: str = ""
    evidence_quality: str = "unassessed"
    architectural_validity: str = "unassessed"
    implementation_impact: str = "unassessed"
    operational_impact: str = "unassessed"
    scalability_impact: str = "unassessed"

    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, data: dict) -> "GovernedOutcome"
```

#### 6.6.3 `InformationState` — stagnation tracking

```python
@dataclass
class InformationState:
    claims_seen: set[str] = field(default_factory=set)
    evidence_seen: set[str] = field(default_factory=set)
    rounds_since_novelty: int = 0
    last_gpt_position: str = ""
    last_gemini_position: str = ""
    total_rounds_tracked: int = 0
    def record_round(self, gpt_position: str, gemini_position: str) -> dict
    def is_stagnating(self, threshold: int = 3) -> bool
    def to_dict(self) -> dict
```

#### 6.6.4 `verifier_review` — the five evaluation dimensions

Verbatim from `VERIFIER_REVIEW_PROMPT` (lines 261-351), the verifier
evaluates across five dimensions:

| Dimension | Rate vocabulary |
|-----------|-----------------|
| `evidence_quality` | `strong \| moderate \| weak \| absent` |
| `architectural_validity` | `high \| moderate \| low \| unsuitable` |
| `implementation_impact` | `low \| moderate \| high \| prohibitive` |
| `operational_impact` | `low \| moderate \| high \| prohibitive` |
| `scalability_impact` | `strong \| moderate \| limited \| unknown` |

Cutoffs:
- `gpt_position[:5000]` / `gemini_position[:5000]` (lines 378-379).
- Verifier inference-failure fallback: `outcome_type=HUMAN_REQUIRED, confidence=0, reasoning="Verifier inference failed: ..."`.
- Unparseable-answer fallback: `outcome_type=HUMAN_REQUIRED, confidence=0, reasoning="Verifier returned unparseable response."`.
- Malformed `outcome_type` coerced to `HUMAN_REQUIRED` (lines 414-419).

#### 6.6.5 `GovernanceResult` partition

```python
@dataclass
class GovernanceResult:
    verified_decisions: list[GovernedOutcome] = field(default_factory=list)
    verified_tradeoffs: list[GovernedOutcome] = field(default_factory=list)
    human_required:    list[GovernedOutcome] = field(default_factory=list)
    def add(self, outcome: GovernedOutcome)     # partitions into the three buckets
    def to_dict(self) -> dict
    @property
    def total(self) -> int
```

#### 6.6.6 The seven rules of the Debate Deadlock Governance Framework

1. **Information State Rule** — a round is only useful if it introduces new
   evidence, reasoning, constraints, tradeoffs, or architectural
   implications.
2. **Stagnation Detection** — tracks repeated arguments, evidence,
   conclusions, and unchanged positions. If multiple rounds occur without
   meaningful change, the dispute is marked STAGNATING.
3. **Consensus Check Frequency** — consensus evaluation occurs every 3
   rounds.
4. **Maximum Dispute Window** — no single dispute may consume unlimited
   rounds.
5. **Verifier Review** — evaluates the five dimensions and determines one
   of three outcomes.
6. **Outcome Types** — `VERIFIED_DECISION` (a superior solution exists),
   `VERIFIED_TRADEOFF` (multiple valid solutions exist; store both),
   `HUMAN_REQUIRED` (insufficient evidence; defer to user).
7. **Tradeoff Preservation Policy** — consensus does not require
   elimination of all disagreement. If multiple architectural options are
   valid, store them. Do not discard them. Do not force agreement.

#### 6.6.7 Where each outcome is produced, and what it triggers

| Outcome | Produced by | Triggered action |
|---------|-------------|------------------|
| `VERIFIED_DECISION` | `governance.verifier_review` returns it; `DebateEngine._debate_dispute`/`_run_verification`/`_verifier_review_dispute` map the {stagnation, deadlock, max-rounds, verification-retries-exhausted} branches into `dispute.outcome_type = VERIFIED_DECISION`, `dispute.status = "resolved"`, `arch_memory.resolve(d)`, `gov_result.add(outcome)`. | `governance_context["verified_decisions"]` gets the entry; `synthesis_engine` emits a `# Verified Decisions` section; `execution_decision` consumes the verified decisions as immutable constraints. |
| `VERIFIED_TRADEOFF` | `governance.verifier_review` returns it; mapped to `dispute.status = "tradeoff"`, `dispute.outcome_type = VERIFIED_TRADEOFF`. | `governance_context["verified_tradeoffs"]` carries `option_a/option_b/recommendation/confidence/reasoning`; `synthesis_engine` renders the `# Verified Tradeoffs` section and the narrative preserves BOTH options (rule 14). |
| `HUMAN_REQUIRED` | (a) `governance.verifier_review` fallbacks (inference failure / unparseable / unmatched `outcome_type`). (b) `verification_engine.verify_all_disputes` when the research budget is exhausted — pre-emitted `governance_outcome.outcome_type == "HUMAN_REQUIRED"` with `reason_code = "research_budget_exhausted"`. (c) `DebateEngine._enqueue_human_required` is called. | `dispute.status = "human_required"`; `_enqueue_human_required` calls `human_decision_queue.enqueue_human_decision(workspace_id, question, options, reasoning, affected_disputes, confidence, priority)` and stamps `decision_id` on `dispute.governed_outcome`; `governance_context["human_required"]` carries the record; `synthesis_engine` renders `# Human Decisions Required`; the build pipeline (`build_pipeline.execute_build_spec`) BLOCKS with `blocked=True, blocked_reason="pending_human_decisions"` until `human_decision_manager.resolve` is called. |

#### 6.6.8 Tests

- `tests/test_human_escalation_e2e.py`:
  - `test_force_unresolved_dispute_produces_human_required` — forced verifier failure → `outcome.outcome_type == OutcomeType.HUMAN_REQUIRED`; aggregated into `GovernanceResult.to_dict()["human_required"]` of length 1.
  - `test_persistence_shape_after_aggregation`
  - `test_workspace_resume_picks_up_pending_decisions`
  - `test_build_pipeline_blocks_implementation_until_resolved`
  - `test_resolve_unblocks_build_pipeline`
  - `test_run_build_pipeline_records_blocked_state`
  - `test_full_escalation_cycle_end_to_end`

End of Batch 4. Next batch: §6.7 Consensus Engine + §6.8 Workspace Debate Engine + §7 persistence surfaces.

---

### 6.7 `consensus_engine.py` — consensus scoring

Wraps the consensus-checker used by `debate_engine._eval_consensus`.
Scores GPT vs Gemini positions per round against the dispute question.
The threshold is `debate_engine.CONSENSUS_THRESHOLD = 92`; the check fires
every 3 rounds (`round_num % 3 != 0`). The score is informational — rule 11
of the synthesis prompt says "Do not weight it above the verifier's
findings." Pinned indirectly by `test_suite.py::test_consensus_engine`.

### 6.8 `workspace_debate_engine.py` — workspace-aware debate

Inherits from `DebateEngine`. Adds three prime entry signatures used by
`workspace_orchestrator`:

```python
class WorkspaceDebateEngine(DebateEngine):                  # line 77
    _pre_debate_research_fn = None                          # injectable offline seam
    def run_in_workspace(self, prompt: str, memory_ids: list | None = None,
                         workspace_id: str | None = None) -> dict: ...    # line 95
    def resume_workspace(self, workspace_id: str, follow_up: str) -> dict: ...  # line 141
    def reopen_dispute(self, workspace_id: str, dispute_key: str,
                       reason: str = ...) -> dict: ...                     # line 469
```

`resume_workspace` runs affected-dispute analysis (via
`workspace_impact_analyzer`) and re-debates only the affected subset. It
does NOT rerun the full pipeline. This is the "follow-up reuses prior
verified decisions" contract documented in the README.

---

## 7. Persistence surfaces

### 7.1 `answer_memory.py` — long-term SQLite memory

```python
DB_FILE = "answer_memory.db"

class AnswerMemory:
    def __init__(self) -> None
    def save_debate(self, question, gpt_final, gemini_final, synthesized_answer,
                    consensus_score, conflict_severity="LOW", conflict_points=None,
                    validated_disputes=None, verification_result=None,
                    parent_answer_id=None, workspace_id=None, human_answer="") -> int
    def get_debate(self, debate_id) -> dict | None
    def get_human_answer(self, debate_id) -> str | None
    def get_recent_debates(self, limit=10) -> list[dict]
    def list_debates(self, limit=50) -> list[dict]
    def search_debates(self, query, limit=20) -> list[dict]
    def delete_debate(self, debate_id) -> bool
    def debate_exists(self, debate_id) -> bool
    def build_context(self, debate_ids) -> str
    def get_answers_by_workspace(self, workspace_id) -> list[dict]
    def get_answer_chain(self, answer_id) -> list[dict]
    def get_stats(self) -> dict

memory = AnswerMemory()   # module-level singleton
```

Schema (the `debates` SQLite table): `debate_id` (UUID, PK), `question`,
`gpt_final`, `gemini_final`, `synthesized_answer`, `consensus_score`,
`conflict_severity`, `conflict_points` (JSON), `validated_disputes` (JSON),
`verification_result` (JSON-TEXT), `parent_answer_id`, `workspace_id`,
`human_answer`, `created_at`. The `verification_result` is a JSON-TEXT
column so the canonical Decision Model + the `execution_decision` survive
resurrection and workspace resume unchanged.

Tests: `tests/test_decision_model_persistence.py` (7 decisions survive
round-trip; `verification_result` preserved verbatim).

### 7.2 `human_decision_queue.py` — in-memory shim

A thin in-memory shim that delegates to `human_decision_store`. The store
is the single source of truth.

```python
class HumanDecisionQueue:
    def enqueue_human_decision(self, workspace_id: str, question: str, options: list,
                               reasoning: str = "", affected_disputes: list = None,
                               confidence: int = 50, priority: str = "MEDIUM") -> str
    def get_pending(self, workspace_id: str) -> list[dict]
    def get_pending_count(self, workspace_id: str) -> int
    def resolve(self, decision_id: str, chosen_option: str) -> bool
```

### 7.3 `human_decision_store.py` + `human_decision_manager.py` — single source of truth

SQLite table `unified_human_decisions` in `database/workspaces.db`:

```sql
CREATE TABLE IF NOT EXISTS unified_human_decisions (
    decision_id   TEXT NOT NULL,
    workspace_id  TEXT NOT NULL,
    data          TEXT NOT NULL DEFAULT '{}',
    resolved      INTEGER NOT NULL DEFAULT 0,
    chosen_option TEXT NOT NULL DEFAULT '',
    resolved_at   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    priority      TEXT NOT NULL DEFAULT 'MEDIUM',
    PRIMARY KEY (workspace_id, decision_id)
)
```

Indexes: `idx_uhd_workspace`, `idx_uhd_resolved`.

`human_decision_manager` exposes `save_pending`, `resolve`, `pending`,
`is_resolved`. The `data` column carries the full decision dict
(`option_a`, `option_b`, `reasoning`, `affected_disputes`, `confidence`,
`priority`, `source`). `resolve_human_requirement` (in
`workspace_orchestrator`) calls `resolve_human_decision(decision_id,
chosen_option)`; if the pending count drops to 0 and the workspace has no
pending items, sets `ws.status = WorkspaceStatus.CONSENSUS_REACHED`.

### 7.4 `build_workspace.py` — workspace directory + build state

```python
WORKSPACE_ROOT: Path        # configurable (test fixtures monkeypatch it)

def create_workspace(source_question: str) -> str       # returns UUID; creates <WORKSPACE_ROOT>/workspace_<id>/
def _workspace_dir(workspace_id: str) -> Path          # the canonical resolver; reused everywhere
def write_build_state(workspace_id: str, status: str, **extra) -> None
def read_build_state(workspace_id: str) -> dict
```

`build_state.json` carries the legacy ad-hoc `status` string (byte-pinned
by existing tests) AND the hardening's parallel `lifecycle_stage` field.
See §`architecture_hardening.md` "Backwards compatibility — the parallel
`lifecycle_stage` field" for the full contract.

### 7.5 `workspace_manager.py` — `database/workspaces.db` SQLite ORM

```python
DB_FILE = os.environ.get("WORKSPACE_DB", "database/workspaces.db")
# Connection: sqlite3.connect(DB_FILE, timeout=30); PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON.

class WorkspaceStatus:
    CREATED           = "created"
    DEBATING          = "debating"
    CONSENSUS_REACHED = "consensus_reached"
    AWAITING_HUMAN    = "awaiting_human"
    COMPLETED         = "completed"
    ARCHIVED          = "archived"
```

#### 7.5.1 `workspaces` table (verbatim schema)

```sql
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    original_prompt TEXT NOT NULL,
    verified_decisions TEXT NOT NULL DEFAULT '[]',
    verified_tradeoffs TEXT NOT NULL DEFAULT '[]',
    human_required_items TEXT NOT NULL DEFAULT '[]',
    architecture_constraints TEXT NOT NULL DEFAULT '[]',
    consensus_history TEXT NOT NULL DEFAULT '[]',
    active_disputes TEXT NOT NULL DEFAULT '[]',
    resolved_disputes TEXT NOT NULL DEFAULT '[]',
    gpt_position TEXT NOT NULL DEFAULT '',
    gemini_position TEXT NOT NULL DEFAULT '',
    answer_history TEXT NOT NULL DEFAULT '[]',
    impact_audit_trail TEXT NOT NULL DEFAULT '[]',
    research_evidence TEXT NOT NULL DEFAULT '[]',
    research_audit_trail TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'created',
    current_job_id TEXT NOT NULL DEFAULT '',
    consensus_score INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0
)
```

Indexes: `idx_workspaces_status`, `idx_workspaces_updated`,
`idx_workspaces_created`. Migration adds missing columns from a fixed
list.

#### 7.5.2 `Workspace` dataclass

21 fields (see §3.4 of the integration contract report). Methods:
- `add_answer(answer_id, parent_answer_id, question, timestamp)`
- `add_verified_decision(d)` (dedup by `normalized_key`)
- `add_verified_tradeoff(d)` (requires `option_a` + `option_b`; dedup by pair)
- `add_human_required(d)` (delegates to store)
- `resolve_human_item(...)` (store.resolve_by_key)
- `get_constraint_context()` (last 10 verified decisions wrapped in
  `<workspace_constraints>...</workspace_constraints>` with a "do not
  contradict" footer)
- `mark_updated()`, `add_impact_audit`, `add_research_audit`

#### 7.5.3 `WorkspaceManager` and module convenience functions

```python
class WorkspaceManager:
    def create_workspace(self, prompt: str, memory_ids: list | None = None) -> str
    def load_workspace(self, workspace_id: str) -> Workspace | None
    def save_workspace(self, workspace: Workspace) -> bool    # UPDATE/INSERT; NEVER INSERT OR REPLACE (avoids cascade delete)
    def update_status(self, workspace_id: str, status: str, error: str = "") -> bool
    def delete_workspace(self, workspace_id: str) -> bool
    def list_workspaces(self, limit: int = 50, status: str | None = None) -> list[dict]
    def list_pending_human_items(self, workspace_id: str) -> list[dict]
    def search_workspaces(self, query: str, limit: int = 20) -> list[dict]
    def get_stats(self) -> dict

workspace_mgr = WorkspaceManager()    # singleton
```

Module-level: `create_workspace`, `load_workspace`, `save_workspace`,
`list_workspaces`, `delete_workspace`, `get_pending_human_items`,
`resolve_human_item`.

### 7.6 `workspace_knowledge.py` — per-stage knowledge persistence

Layout under `<workspace>/`:

```
project.json                 # planner metadata
planner/execution_graph.json  # the validated graph
research/<package_id>.md
verified/<package_id>.json
architecture/<package_id>.md
implementation/<package_id>.md
```

```python
STAGE_DIRS = ("research", "verified", "architecture", "implementation")

def stage_dir(workspace_id: str, stage: str, *, create: bool = False) -> Path
                            # raises ValueError(f"unknown knowledge stage: {stage!r}") for unknown stage
def save_artifact(workspace_id, stage, package_id, content) -> Path
                            # stage=="verified" → <safe>.json (validates JSON)
                            # other stage       → <safe>.md (safe = slug of package_id)
def load_artifact(workspace_id, stage, package_id) -> str | None
def list_artifacts(workspace_id, stage) -> list[str]
def save_debate_result(workspace_id, package_id, package_type, result) -> Path | None
def stage_for_type(package_type) -> str | None
                            # {"research":"research","architecture":"architecture","implementation":"implementation"}.get(t)
def load_stage_input(workspace_id, stage) -> str
                            # merges prior-stage artefacts for `stage`
```

`load_stage_input` precedences (the consumers dict):
- `architecture` consumes `("research", "verified")`
- `implementation` consumes `("architecture", "verified", "research")`
- `validation` / `documentation` consume `("implementation",)`
- any other stage → `""` (no prior input expected)

This is the "architecture consumes verified research rather than the
original user prompt whenever research exists" automation.

### 7.7 `job_store.py` — legacy job SQLite

The `database/jobs.db` table `jobs` (UUID, prompt, status
`PENDING/DEBATING/COMPLETED/FAILED`, consensus_score, result, error). Used
by `orchestrator.submit_job` (legacy). The workspace-based API
(`workspace_orchestrator.submit_workspace`) is the recommended path.

End of Batch 5. Next batch: §6.9–6.12 Execution Decision layer (V2 classifier, V3 engine, mode gate, execution authority).

---

### 6.9 `execution_decision.py` — the V2 deterministic classifier

The deterministic V2 classifier (pure stdlib). Decides execution depth
AFTER verification. Guaranteed by a self-check at L606-610.

#### 6.9.1 Constants

```python
RESEARCH_ONLY            = "RESEARCH_ONLY"
ARCHITECTURE_ONLY        = "ARCHITECTURE_ONLY"
IMPLEMENTATION_REQUIRED  = "IMPLEMENTATION_REQUIRED"
STOP_HUMAN_REQUIRED      = "STOP_HUMAN_REQUIRED"
STOP_UNRESOLVED_DISPUTES = "STOP_UNRESOLVED_DISPUTES"

ALL_DECISIONS = (RESEARCH_ONLY, ARCHITECTURE_ONLY, IMPLEMENTATION_REQUIRED,
                 STOP_HUMAN_REQUIRED, STOP_UNRESOLVED_DISPUTES)

# RESEARCH_ONLY / ARCHITECTURE_ONLY / STOP_UNRESOLVED_DISPUTES hard-stop at the gate.
# STOP_HUMAN_REQUIRED is DELIBERATELY EXCLUDED — defers to execute_build_spec's
# pending-decision gate (the existing human-escalation contract).
BLOCKED_DECISIONS = frozenset({RESEARCH_ONLY, ARCHITECTURE_ONLY, STOP_UNRESOLVED_DISPUTES})

KEY_DECISION    = "execution_decision"
KEY_REASON      = "execution_reason"
KEY_CONFIDENCE  = "execution_confidence"
KEY_SOURCE      = "execution_decision_source"

SOURCE_HUMAN_REQUIRED          = "human_required"
SOURCE_UNRESOLVED_DISPUTES     = "unresolved_disputes"
SOURCE_IMPLEMENTATION_ARTIFACTS = "implementation_artifacts"
SOURCE_ARCHITECTURE_DECISIONS   = "architecture_decisions"
SOURCE_RESEARCH_ANALYSIS        = "research_analysis"
SOURCE_PROMPT_FALLBACK          = "prompt_fallback"

# Rungs 3-5: word-boundary matched (`\b<phrase>\b`)
IMPLEMENTATION_SIGNALS = ("deploy", "build", "implement", "execute",
                          "create service", "create api", "create worker",
                          "create database", "migration step", "migrate",
                          "develop", "create code", "generate code")
ARCHITECTURE_SIGNALS   = ("orchestration", "database selection",
                          "deployment topology", "provider routing",
                          "storage design", "security model")
RESEARCH_SIGNALS       = ("comparison", "compare", "evaluate", "evaluation",
                          "benchmark", "analysis", "analyze", "survey", "research")

# Rung 6: prompt-fallback substring matched (V1 behavior preserved)
RESEARCH_KEYWORDS        = ("compare", "evaluate", "research", "analyze", "survey", "benchmark")
ARCHITECTURE_KEYWORDS    = ("design", "architecture", "blueprint", "system design", "platform design")
IMPLEMENTATION_KEYWORDS  = ("implement", "build", "create code", "generate code", "develop", "execute")

_BLOCK_STATUS = {
    RESEARCH_ONLY:           "research_complete",
    ARCHITECTURE_ONLY:       "architecture_complete",
    STOP_UNRESOLVED_DISPUTES: "unresolved_disputes",
    STOP_HUMAN_REQUIRED:      "human_required",
    IMPLEMENTATION_REQUIRED:  "",   # not blocked
}
```

#### 6.9.2 Public surface

```python
def classify_execution_decision(question=None, verified_decisions=None,
                                 verified_tradeoffs=None, human_required=None,
                                 synthesized_answer=None,
                                 verification_result=None) -> dict
                            # returns {KEY_DECISION, KEY_REASON, KEY_CONFIDENCE, KEY_SOURCE}
def attach_to_verification_result(verification_result: dict | None, decision: dict) -> dict
def from_verification_result(verification_result: dict | None) -> dict | None
                            # reads the keys back; None when no decision was stored
def is_implementation_required(decision: dict | str | None) -> bool
def is_blocked_decision(decision: dict | str | None) -> bool
def block_status(decision: dict | str | None) -> str
```

#### 6.9.3 The six-rung decision hierarchy (first rung that holds wins; never reverses)

| Rung | Condition | Decision | Confidence | Source |
|------|-----------|----------|------------|--------|
| 1 | `human_required` non-empty | `STOP_HUMAN_REQUIRED` | 95 | `SOURCE_HUMAN_REQUIRED` |
| 2 | `verification_result.unresolved_claims` non-empty | `STOP_UNRESOLVED_DISPUTES` | 95 | `SOURCE_UNRESOLVED_DISPUTES` |
| 3 | word-boundary `IMPLEMENTATION_SIGNALS` in `verified_decisions + verified_tradeoffs + synthesized_answer` | `IMPLEMENTATION_REQUIRED` | 90 | `SOURCE_IMPLEMENTATION_ARTIFACTS` |
| 4 | word-boundary `ARCHITECTURE_SIGNALS` in `verified_decisions` (no implementation signal on record) | `ARCHITECTURE_ONLY` | 85 | `SOURCE_ARCHITECTURE_DECISIONS` |
| 5 | word-boundary `RESEARCH_SIGNALS` in `_verification_text(verification_result)` (resolved-claim disputes + findings + research evidence + `research_evidence` blob; unresolved excluded) | `RESEARCH_ONLY` | 80 | `SOURCE_RESEARCH_ANALYSIS` |
| 6 | None of the above fired → V1 substring counting on the question text; implementation wins ties; **all-zero → `RESEARCH_ONLY` (safest-stop default — never auto-implement)** | varies | varies | `SOURCE_PROMPT_FALLBACK` |

Word-boundary semantics (L70-73, L199-213): rung 3-5 use `\b<phrase>\b`
regex so `"deploy"` matches `"Deploy PostgreSQL"` but NOT `"deployment
topology"` (architecture signal). Rung 6 keeps V1 substring matching.

#### 6.9.4 The non-negotiable abstention ledger (verbatim)

> "Only `IMPLEMENTATION_REQUIRED` (and `STOP_HUMAN_REQUIRED`, which defers
> to the pending gate) may reach the build pipeline's spec step; every
> other decision is a hard stop at the execution-decision gate." —
> `execution_decision.py:20-22`.

> "`STOP_HUMAN_REQUIRED` is deliberately excluded [from BLOCKED_DECISIONS]
> — it records that a human decision is pending but defers the actual
> block to `execute_build_spec`'s pending-decision gate (the existing
> human-escalation contract). Blocking here would prevent spec generation
> and break the resolve-then-proceed flow." — `execution_decision.py:108-113`
> (`# ponytail: re-evaluate STOP_HUMAN_REQUIRED immutability if a
> resume-after-human-resolution flow through the gate is needed; today the
> pending gate is the live authority for human holds.`).

> "`None` (no decision stored) is non-blocking — preserves behavior for
> any caller that loads a legacy answer without an `execution_decision`."
> — `is_blocked_decision`, L496-498.

#### 6.9.5 The from_verification_result contract (resume/immutability)

`build_pipeline._resolve_execution_decision` consults this FIRST so resume
never silently reclassifies (Phase 6 immutability — `build_pipeline.py`
L423-533). `from_verification_result` reads the four keys out of the
`verification_result` dict (which persists through `answer_memory`'s
JSON-TEXT column). Returns `None` when no decision was ever stored.

#### 6.9.6 Tests

- `tests/test_execution_decision.py` — research-only / architecture / implementation keyword routing, the "Benchmark in-memory queues" research-only fixture, immutability.
- `tests/test_execution_decision_v2.py` — `test_rung_*` tests: `_6_human_required` / `_5_unresolved` / `_4_implementation_artifacts` (winning on a "Design …" prompt) / `_3_architecture_decisions` / `_2_research_signal` / `_1_prompt_fallback` (substring + safe-default).

---

### 6.10 `execution_decision_engine.py` — the V3 engine (recommendation vs. decision)

Separates RECOMMENDATION (verifier) from DECISION (engine). Performs NO
execution itself. Returns an `ExecutionDecision` dataclass. Persists four
artifacts under `<workspace>/execution/` via `ExecutionPlanner`.

#### 6.10.1 Constants — the 7 execution modes + 6 stages + 6-source precedence

```python
# 7 modes
RESEARCH_ONLY        = "RESEARCH_ONLY"
ARCHITECTURE_ONLY    = "ARCHITECTURE_ONLY"
IMPLEMENTATION_ONLY  = "IMPLEMENTATION_ONLY"
BUILD_ONLY           = "BUILD_ONLY"
BUILD_AND_VALIDATE   = "BUILD_AND_VALIDATE"
FULL_BUILD           = "FULL_BUILD"
CUSTOM               = "CUSTOM"
ALL_MODES = (RESEARCH_ONLY, ARCHITECTURE_ONLY, IMPLEMENTATION_ONLY,
             BUILD_ONLY, BUILD_AND_VALIDATE, FULL_BUILD, CUSTOM)

# 6 stages
STAGE_RESEARCH              = "research"
STAGE_ARCHITECTURE          = "architecture"
STAGE_IMPLEMENTATION_SPEC   = "implementation_spec"
STAGE_IMPLEMENTATION        = "implementation"
STAGE_TESTING               = "testing"
STAGE_REPAIR                = "repair"
ALL_STAGES = (STAGE_RESEARCH, STAGE_ARCHITECTURE, STAGE_IMPLEMENTATION_SPEC,
              STAGE_IMPLEMENTATION, STAGE_TESTING, STAGE_REPAIR)

# Precedence (highest first)
SOURCE_OVERRIDE            = "user_override"
SOURCE_PROMPT_INTENT       = "user_prompt_intent"
SOURCE_POLICY              = "execution_policy"
SOURCE_PLANNER             = "planner_recommendation"
SOURCE_VERIFICATION        = "verification_recommendation"
SOURCE_RESEARCH_HEURISTIC  = "research_heuristic"
PRECEDENCE = (SOURCE_OVERRIDE, SOURCE_PROMPT_INTENT, SOURCE_POLICY,
              SOURCE_PLANNER, SOURCE_VERIFICATION, SOURCE_RESEARCH_HEURISTIC)

DEFAULT_POLICY: dict = {
    "honor_unresolved_disputes_block": True,
    "honor_human_required_defer":     True,
    "max_mode": None,
    "require_explicit_custom":       True,
}

VERIFIER_BLOCKING = frozenset({ed.STOP_UNRESOLVED_DISPUTES})
```

#### 6.10.2 The 5+1 execution path map (`_MODE_STAGES`, verbatim)

```python
_MODE_STAGES = {
    RESEARCH_ONLY:       (STAGE_RESEARCH,),
    ARCHITECTURE_ONLY:   (STAGE_RESEARCH, STAGE_ARCHITECTURE),
    IMPLEMENTATION_ONLY: (STAGE_RESEARCH, STAGE_ARCHITECTURE,
                          STAGE_IMPLEMENTATION_SPEC, STAGE_IMPLEMENTATION),
    BUILD_ONLY:          (STAGE_RESEARCH, STAGE_ARCHITECTURE,
                          STAGE_IMPLEMENTATION_SPEC, STAGE_IMPLEMENTATION),
    BUILD_AND_VALIDATE:  (STAGE_RESEARCH, STAGE_ARCHITECTURE,
                          STAGE_IMPLEMENTATION_SPEC, STAGE_IMPLEMENTATION,
                          STAGE_TESTING),
    FULL_BUILD:          ALL_STAGES,
    # CUSTOM: built per-request via override — _stages_for_mode falls back to
    # FULL_BUILD if it slips through unresolved (never silently stop).
}
```

#### 6.10.3 Public surface

```python
@dataclass(frozen=True)
class Stage:
    name: str
    status: str  # "pending" | "running" | "completed" | "skipped" | "blocked"

@dataclass
class ExecutionDecision:
    execution_mode: str
    reason: str
    confidence: int
    source: str             # one of SOURCE_*
    stages: list[Stage] = field(default_factory=list)
    policy_decision: dict = field(default_factory=dict)
    recommended_mode: str | None = None
    selected_mode: str | None = None
    override_used: bool = False
    blocked: bool = False
    block_status: str = ""
    created_at: str = ""

class ExecutionDecisionEngine:
    def decide(self, *, user_prompt="", planner_output=None, debate_result=None,
               consensus_result=None, verification_result=None, policy=None,
               user_override=None, completed_stages=None, mode_select_fn=None) -> ExecutionDecision

class ExecutionPlanner:
    def persist(self, workspace_id, decision: ExecutionDecision) -> dict
    def load_plan(self, workspace_id) -> dict | None
    def load_state(self, workspace_id) -> dict | None
    def mark_stage(self, workspace_id, stage, status) -> dict | None
    def reconcile(self, decision, workspace_id) -> ExecutionDecision

def detect_prompt_intent(prompt: str) -> dict
def intent_to_mode(intent: dict) -> str | None
def planner_mode(graph: dict | None) -> str | None
def verification_mode(verification_result: dict | None) -> str | None
def verification_is_hard_block(verification_result: dict | None) -> bool
def normalize_policy(policy: dict | None) -> dict
def policy_allows(policy: dict, mode: str) -> tuple[bool, str]
def mode_to_legacy_decision(mode: str) -> str
def log_decision(decision: ExecutionDecision) -> None
def validate_inputs(*, user_prompt=None, planner_output=None, verification_result=None,
                    policy=None, user_override=None) -> list[str]
def validate_plan(plan: dict | None) -> list[str]
def decide(*, user_prompt="", planner_output=None, debate_result=None, consensus_result=None,
           verification_result=None, policy=None, user_override=None,
           completed_stages=None, mode_select_fn=None) -> ExecutionDecision
def persist_decision(workspace_id, decision) -> dict
def load_persisted_plan(workspace_id) -> dict | None
def reconcile_decision(decision, workspace_id) -> ExecutionDecision

class ExecutionDecisionError(Exception): ...
```

#### 6.10.4 Decision precedence ladder (highest wins, never reverses)

**User Override → User Prompt Intent → Execution Policy → Planner
Recommendation → Verification Recommendation → Research heuristics
(floor; inform only, never terminate on their own).**

Confidence per source:

```python
SOURCE_OVERRIDE           → 100
SOURCE_PROMPT_INTENT      → 99 (full_build) | 95
SOURCE_POLICY             → 90
SOURCE_PLANNER            → 85
SOURCE_VERIFICATION       → 70
SOURCE_RESEARCH_HEURISTIC → 60  (the floor)
```

Hard-block final check: unresolved-disputes overrides everything to
`RESEARCH_ONLY` with `block_status="unresolved_disputes"` and
`source=SOURCE_POLICY`; otherwise user override (Rule 6) wins.

#### 6.10.5 "Research signal stops the build" — demoted to a recommendation

> "The verifier's emit is demoted to a recommendation: only
> `STOP_UNRESOLVED_DISPUTES` retains its hard-stop authority (a true
> governance constraint — the debate outcome is incomplete). Every other
> emit (`RESEARCH_ONLY` / `ARCHITECTURE_ONLY` / `IMPLEMENTATION_REQUIRED`)
> becomes a non-binding recommendation the engine may override per user
> intent." — `verification_mode`, L311-342.

`verification_is_hard_block` (L345-352) returns True *only* for
`STOP_UNRESOLVED_DISPUTES`. "Verification success never terminates
execution on its own" (Rule 3). "Research signals never override explicit
user intent" (Rule 4).

#### 6.10.6 Persistence surface (4 artifacts under `<workspace>/execution/`)

`ExecutionPlanner.persist` writes:

| File | Content |
|------|---------|
| `execution/execution_plan.json` | `plan_payload` — stages + mode + reason + source + confidence + created_at + updated_at |
| `execution/execution_decision.json` | `decision.to_dict()` (full envelope) |
| `execution/execution_reason.json` | `{reason, source, mode}` |
| `execution/execution_state.json` | `{workspace_id, execution_mode, stages, updated_at}` — the checkpoint |

Namespaced under `execution/` deliberately to preserve the existing
`<workspace>/execution_plan.json` consumed by `manifest.py` / `resume.py`
/ `validation.py`.

`mark_stage` writes both `execution_plan.json` and `execution_state.json`
(atomic-on-stage-change). `reconcile` carries `completed` stages forward
on resume.

#### 6.10.7 Callable seams

```python
mode_select_fn: Callable[[str], tuple[str, bool]] | None = None
# Legacy user-override seam (recommended_mode) → (selected_mode, override_used).
# Production: CLI prompt. Tests: lambda. EOFError/KeyboardInterrupt → accept
# recommendation.
```

---

### 6.11 `execution_mode_gate.py` — user-facing depth CHOICE before the build

Pure wrapper. Three user-facing modes:

```python
RESEARCH_ONLY          = "RESEARCH_ONLY"           # Research → Debate → Verification → Answer
RESEARCH_ARCHITECTURE  = "RESEARCH_ARCHITECTURE"   # + Architecture → Stop
FULL_BUILD             = "FULL_BUILD"              # + Build Spec → OpenCode → Repair Loop

ALL_MODES = (RESEARCH_ONLY, RESEARCH_ARCHITECTURE, FULL_BUILD)
_MENU_CHOICE_TO_MODE = {"1": RESEARCH_ONLY, "2": RESEARCH_ARCHITECTURE, "3": FULL_BUILD}
```

#### 6.11.1 The pure-wrapper contract (Phase 2 + Phase 5)

- Phase 2 (recommendation mapping, no behavior change):
  - `execution_decision.RESEARCH_ONLY` → `RESEARCH_ONLY`
  - `execution_decision.ARCHITECTURE_ONLY` → `RESEARCH_ARCHITECTURE`
  - `execution_decision.IMPLEMENTATION_REQUIRED` → `FULL_BUILD`
- Phase 5 (enforcement — maps the selected user mode back to the
  `execution_decision` string the existing build-pipeline gate enforces,
  so the override rides the live, single authority rather than a parallel
  gate):
  - `RESEARCH_ONLY` → `execution_decision.RESEARCH_ONLY`
  - `RESEARCH_ARCHITECTURE` → `execution_decision.ARCHITECTURE_ONLY`
  - `FULL_BUILD` → `execution_decision.IMPLEMENTATION_REQUIRED`

#### 6.11.2 Public surface

```python
def decision_to_mode(decision: str | dict | None) -> str
def recommend_mode_from_prompt(prompt: str) -> str
def resolve_mode(recommended_mode: str, choice: str | int | None) -> tuple[str, bool]
                            # None/""/whitespace → accept recommendation
                            # 1/2/3 → matching mode; override = selected != recommended
                            # other invalid → accept recommendation
def prompt_execution_mode(recommended_mode, input_fn: Callable[[str], str] | None = None) -> tuple[str, bool]
                            # EOFError/KeyboardInterrupt → accept recommendation (non-interactive)
def fixed_selection_fn(selected_mode, override_used) -> Callable[[str], tuple[str, bool]]
                            # canned selection for chained build calls and tests
def mode_to_decision(selected_mode: str | None) -> str | None
def mode_allows_build(selected_mode: str | None) -> bool
def mode_allows_architecture(selected_mode: str | None) -> bool
def apply_mode_override(exec_decision: dict, selected_mode, recommended_mode,
                        override_used, source=SOURCE_MODE_GATE) -> dict
def decision_from_mode_selection(mode_selection: dict, source=SOURCE_MODE_GATE_PERSISTED) -> dict | None
def save_mode_selection(workspace_id, recommended_mode, selected_mode, override_used) -> dict
def load_mode_selection(workspace_id) -> dict | None
```

#### 6.11.3 Persistence (Phase 4)

`save_mode_selection` writes `recommended_mode` / `selected_mode` /
`override_used` to `<workspace>/build_state.json` via
`build_workspace.write_build_state` (merge semantics — co-exists with the
immutable `execution_decision` keys).

#### 6.11.4 Resume contract (Phase 5)

`decision_from_mode_selection` builds a legacy-shape decision dict
directly from the persisted mode — authoritative on resume, no re-prompt,
no auto-recompute; returns `None` when the selection does not map. The
caller falls back to `_resolve_execution_decision`, which consults
`load_mode_selection` FIRST before any other source
(`build_pipeline.py` L465-472).

---

### 6.12 `execution_authority.py` — the SINGLE decision-resolution authority

Pure wrapper over `execution_decision` + `execution_mode_gate`. No
parallel gate — produces the exact `execution_decision` string the
build-pipeline gate (`build_pipeline._resolve_execution_decision` /
`execute_build_spec`) enforces.

#### 6.12.1 Precedence and source labels

```python
SOURCE_OVERRIDE = "user_override"
SOURCE_POLICY    = "policy_engine"
SOURCE_PLANNER    = "planner_recommendation"
SOURCE_DEFAULT    = "prompt_fallback"
PRECEDENCE = (SOURCE_OVERRIDE, SOURCE_POLICY, SOURCE_PLANNER, SOURCE_DEFAULT)
```

The precedence contract:
1. Explicit user override (mode_select_fn / persisted mode selection)
2. Policy Engine — the immutable persisted `execution_decision` inside
   `verification_result` (the verifier settled this; it is policy, not
   preference)
3. Planner recommendation (derived from the planner's work-package graph)
4. Prompt fallback / default — `classify_execution_decision` over the
   user objective; safest-stop default

#### 6.12.2 Public surface

```python
def resolve(objective: str, *, graph: dict | None = None,
            verification_result: dict | None = None,
            workspace_state: dict | None = None,
            mode_select_fn: Callable[[str], tuple[str, bool]] | None = None,
            persisted_mode: dict | None = None) -> dict
def log_authority(resolution: dict) -> None
```

`resolve` returns:

```python
{
    "policy_decision":   {layer: decision_dict | None, ...},   # the audit trail
    "effective_decision": {...},                                # the winner
    "effective_source":   SOURCE_OVERRIDE | SOURCE_POLICY | ...,
    "recommended_mode":   recommended_mode,
    "resolved_at":        iso,
}
```

`effective_decision` is always non-None (the default layer is the floor).
It carries the standard contract keys the existing gate reads:
`KEY_DECISION`, `KEY_REASON`, `KEY_CONFIDENCE`, `KEY_SOURCE`, `"blocked"`,
`"block_status"`, plus `emg.KEY_RECOMMENDED_MODE` /
`emg.KEY_SELECTED_MODE` / `emg.KEY_OVERRIDE_USED` when the override was the
winner. The recommended-mode precedence for the menu is
`policy or planner or default`.

#### 6.12.3 Layer 3 planner decision-derivation rule (verbatim lines 180-200)

```python
# packages = graph.get("work_packages") or []
# types = {p.get("type") for p in packages}
if "implementation" in types:
    decision = ed.IMPLEMENTATION_REQUIRED
    reason = "Planner graph contains an implementation work package."
elif "architecture" in types:
    decision = ed.ARCHITECTURE_ONLY
    reason = "Planner graph has architecture but no implementation packages."
else:
    decision = ed.RESEARCH_ONLY
    reason = "Planner graph has only research/validation/documentation packages."
```
Returns `KEY_CONFIDENCE=85`, `KEY_SOURCE=SOURCE_PLANNER`.

#### 6.12.4 Override no-op rule (lines 113-115) — the audit-trail invariant

An override that maps to the SAME depth as the recommendation is NOT a
real override — returns `None` so lower layers speak. Eliminates false
"`RESEARCH_ONLY` + `FULL_BUILD` agreeing" conflicts.

#### 6.12.5 The `[AUTHORITY]` log contract

Exact emitted lines (lines 316-332):

```
[AUTHORITY] Policy Decision:
[AUTHORITY]   <layer>: <decision> (source=<source>, confidence=<n>)
... (one per non-None layer, in PRECEDENCE order; the winner last)
[AUTHORITY] EFFECTIVE: <decision> (source=<source>, recommended_mode=<>, selected_mode=<>, override_used=<bool>)
```

#### 6.12.6 Self-check (lines 335-399)

1. Override wins over policy+planner: policy says `RESEARCH_ONLY` but override → `IMPLEMENTATION_REQUIRED`.
2. No override → Policy Engine wins (`ARCHITECTURE_ONLY`).
3. No policy → Planner wins.
4. No planner → default safest-stop `RESEARCH_ONLY`.
5. Override that AGREES with recommendation → policy wins (no false conflict), no `KEY_OVERRIDE_USED` carried.
6. Persisted-resume override (`persisted_mode` with `KEY_OVERRIDE_USED: True`) wins.

End of Batch 6. Next batch: §6.13–6.16 Layer 2 implementation + validation + repair.

---

### 6.13 `implementation_engine.py` — the Phase 2 native engine

Phase 2 `NativeImplementationEngine` — the deterministic workflow engine
replacing the direct-to-OpenCode path. Pure stdlib at import
(`coding_agent_router` lazy-imported).

#### 6.13.1 The 5-node execution graph (lines 39-54)

```
START → GenerateCodeNode → RunTestsNode →
  tests_passed? ── YES → FinishNode → COMPLETED
    │
    NO
    ↓
  AnalyzeFailureNode → RepairNode →
  RetryPolicy decision:  retry → GenerateCodeNode ; stop → FinishNode(FAILED)
```

#### 6.13.2 Constants

```python
NODE_START            = "START"
NODE_GENERATE_CODE    = "GenerateCodeNode"
NODE_RUN_TESTS        = "RunTestsNode"
NODE_ANALYZE_FAILURE  = "AnalyzeFailureNode"
NODE_REPAIR           = "RepairNode"
NODE_FINISH           = "FinishNode"
ALL_NODE_NAMES = (NODE_START, NODE_GENERATE_CODE, NODE_RUN_TESTS,
                  NODE_ANALYZE_FAILURE, NODE_REPAIR, NODE_FINISH)

STATUS_PENDING    = "PENDING"
STATUS_RUNNING    = "RUNNING"
STATUS_COMPLETED  = "COMPLETED"
STATUS_FAILED     = "FAILED"
STATUS_SKIPPED    = "SKIPPED"
STATUS_CANCELLED  = "CANCELLED"
STATUS_BLOCKED    = "BLOCKED"
ALL_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED,
                STATUS_SKIPPED, STATUS_CANCELLED, STATUS_BLOCKED)

EV_EXECUTION_STARTED  = "ExecutionStarted"
EV_EXECUTION_PAUSED   = "ExecutionPaused"
EV_EXECUTION_RESUMED  = "ExecutionResumed"
EV_EXECUTION_COMPLETED = "ExecutionCompleted"
EV_EXECUTION_FAILED   = "ExecutionFailed"
EV_NODE_STARTED       = "NodeStarted"
EV_NODE_COMPLETED     = "NodeCompleted"
EV_NODE_FAILED        = "NodeFailed"
EV_CHECKPOINT_SAVED   = "CheckpointSaved"
EV_AGENT_INVOKED      = "AgentInvoked"
EV_AGENT_RESPONDED    = "AgentResponded"

DEFAULT_MAX_ATTEMPTS  = 3
DEFAULT_BACKOFF_SECONDS = 0.0
```

#### 6.13.3 `TransitionEngine.next_node` (L908-961) — the SOLE authority over the graph

- `ctx.cancelled` → `NODE_FINISH` (reason `"cancelled"`).
- `NODE_GENERATE_CODE` → `NODE_RUN_TESTS` (reason `"generate done"`).
- `NODE_RUN_TESTS` → `NODE_FINISH` if `ctx.test_passed` (reason `"tests passed"`) else `NODE_ANALYZE_FAILURE` (reason `"tests failed"`).
- `NODE_ANALYZE_FAILURE` → `NODE_REPAIR` (reason `"analysis complete"`).
- `NODE_REPAIR` → `retry.is_eligible(ctx)` ? `RetryPolicy.next(ctx)` then `NODE_GENERATE_CODE` (reason `"retry"`) : `NODE_FINISH` (reason `"retry budget exhausted"`).
- Unknown terminal → `NODE_FINISH` (reason `"terminal"`).

`RetryPolicy.is_eligible` returns `False` when `current_attempt >=
max_attempts` OR `failure_analysis.strip()` is empty (an empty analysis
means nothing to repair from — terminate).

#### 6.13.4 Public surface (key classes)

```python
@dataclass
class BuildResult:
    success: bool = False
    output: str = ""
    test_results: dict
    repair_attempts: int = 0
    repair_log: list
    error: str = ""
    executed_at: str = ""
    workspace_id: str = ""
    engine: str = ""
    final_node: str = ""
    execution_id: str = ""
    node_timings: dict
    generated_files: list
    modified_files: list
    execution_log: list

@dataclass
class ExecutionContext:
    execution_id: str = ""
    workspace_id: str = ""
    implementation_spec: str = ""
    current_node: str = NODE_START
    current_attempt: int = 1
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    execution_state: str = STATUS_PENDING
    # ... carries EVERY field required to resume (L210-224)

@dataclass
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    def is_eligible(self, ctx) -> bool
    def next(self, ctx) -> None    # current_attempt += 1; sleep(backoff)
    def should_terminate(self, ctx) -> bool

class ExecutionRecorder:
    def record_plan(self, plan, execution_mode) -> None
    def record_event(self, event: Event) -> None
    def record_node_transition(self, *, from_node, to_node, reason) -> None
    def record_provider_request(self, agent, prompt) -> None
    def record_provider_response(self, agent, response) -> None
    def record_test_execution(self, results) -> None
    def record_checkpoint(self, node: str, attempt: int) -> None
    def record_retry(self, attempt, max_attempts, eligible) -> None
    def record_repair(self, analysis: str) -> None
    def record_final_result(self, result: BuildResult) -> None
    def replay() -> list[dict]
    def timeline() -> list[dict]
    def failure_reconstruction() -> dict
    def persist(self, workspace_id: str) -> None   # writes execution_record.json

class AgentManager:
    def __init__(self, *, coding_agent_fn=None, testing_agent_fn=None, repair_agent_fn=None)
    def coding_agent(self) -> CodingAgent
    def run_tests(self, workspace_id) -> dict
    def repair(self, workspace_id, spec, failure_analysis) -> dict

class CodingAgent:
    def __init__(self, agent_fn: Callable[[str, str], dict] | None)
    def run(self, workspace_id, prompt, *, ctx=None) -> dict
    def _validate_pre(self, workspace_id, prompt, ctx) -> None    # raises CodingAgentError
    def _validate_post(self, result) -> None                   # raises CodingAgentError on silent failure

class NativeImplementationEngine:
    ENGINE_NAME = "Native"
    def __init__(self, *, coding_agent_fn=None, testing_agent_fn=None,
                 repair_agent_fn=None, retry: RetryPolicy | None = None,
                 max_attempts: int | None = None)
    def execute(self, *, workspace_id, implementation_spec, **kwargs) -> BuildResult
    def resume(self, *, workspace_id) -> BuildResult
    def cancel(self, *, workspace_id) -> bool
    def status(self, *, workspace_id) -> dict
    def recover(self, *, workspace_id) -> dict

class LegacyImplementationEngine:
    ENGINE_NAME = "Legacy"
    def execute(self, *, workspace_id, implementation_spec, **kwargs) -> BuildResult
    # resume/cancel/recover return unsupported / False / recoverable=False
```

#### 6.13.5 Callable seams

```python
coding_agent_fn:  Callable[[str, str], dict] | None = None   # callable(prompt, working_dir)
testing_agent_fn: Callable[[str], dict] | None = None         # callable(workspace_id)
repair_agent_fn: Callable[[str, str, str], dict] | None = None  # callable(workspace_id, spec, failure_analysis)
retry:           RetryPolicy | None = None
max_attempts:    int | None = None         # shorthand for RetryPolicy(max_attempts=N)
```

`AgentManager.run_tests` defaults to lazy
`implementation_executor._run_tests`. `AgentManager.repair` defaults to
dispatching through `coding_agent()` with a prompt built from
`failure_analysis` + `spec`.

`CodingAgent.run` pre-validates (workspace_id non-empty, prompt non-empty,
workspace is_dir + writable, ctx is `ExecutionContext`) and post-validates
(result is a dict with `success` AND at least one of `output`/`files`/
`message`/`logs`, OR `success=False`). **Raises `CodingAgentError` — never
silent** (L540-604).

#### 6.13.6 Persistence surface (7 artifacts under `<workspace>/implementation/`)

`CheckpointManager` (L647-684) and `StateManager` (L969-1015) write after
every node:

```
<workspace>/implementation/execution_context.json   # full ExecutionContext (StateManager.save)
<workspace>/implementation/engine_state.json       # {execution_id, current_node, current_attempt, execution_state, updated_at}
<workspace>/implementation/checkpoint.json         # live checkpoint {execution_id, node, attempt, execution_state, saved_at}
<workspace>/implementation/node_history.json      # {execution_id, history: [...]}
<workspace>/implementation/event_log.json          # raw EventBus events from this run
<workspace>/implementation/execution_record.json   # ExecutionRecorder.to_dict()
<workspace>/implementation/build_result.json        # final BuildResult
```

### 6.14 `implementation_executor.py` — the legacy repair loop (Phase 1)

Wrapped by `LegacyImplementationEngine`. The repair loop contract lives here.

#### 6.14.1 Constants

```python
TEST_TIMEOUT_SECONDS = 120
MAX_REPAIR_ATTEMPTS  = 3
```

#### 6.14.2 Public surface

```python
@dataclass
class ImplementationResult:
    success: bool
    output: str = ""
    test_results: dict
    repair_attempts: int = 0
    repair_log: list[str]
    error: str = ""
    executed_at: str = ""
    def to_dict(self) -> dict

def _run_tests(working_dir: str | None = None) -> dict
                            # {runner, passed, failed, exit_code, output}
def _tests_passed(test_results: dict) -> bool
                            # Phase 2 STRENGTHENED contract (see §6.14.4)
def _build_repair_prompt(plan, test_results, attempt, max_repairs) -> str
def _load_build_state(workspace_id: str) -> dict      # {} when absent/corrupt
def _list_workspace_files(working_dir: str | None) -> set[str] | None
                            # None when falsy/non-dir (test seams skip the artefact gate)
def _emit_human_required(workspace_id, plan, test_results,
                        repair_attempts, max_repairs) -> dict
def _execute_with_repair_loop(plan, workspace_id, working_dir, agent_fn,
                             test_fn, max_repairs) -> ImplementationResult
def _direct_implementation(plan: str, working_dir: str | None = None) -> ImplementationResult
def execute_implementation(plan, workspace_id="", working_dir=None,
                          use_claude_code=True, agent_fn=None, test_fn=None,
                          max_repairs=None) -> dict
```

#### 6.14.3 The repair loop contract — `_execute_with_repair_loop` (L261-427)

1. **Resume load** — read prior `repair_attempts` + `repair_log` out of
   `<workspace>/build_state.json` via `_load_build_state`.
2. **Already-exhausted short-circuit** — if `repair_attempts >= max_repairs`
   AND `prior["status"] == "repair_exhausted"` → re-emit
   `_emit_human_required` (NOT silently re-run) and return.
3. **Persist `"implementing"`** — `write_build_state(workspace_id,
   "implementing", repair_attempts=..., repair_log=...)`.
4. **Artefact-presence snapshot** — `files_before = _list_workspace_files(working_dir)`
   (None when working_dir falsy → test seams skip).
5. **Initial agent dispatch** — `agent_result = agent_fn(plan,
   working_dir=working_dir)`. Repair log appends `"[attempt 0] agent
   success=...; output=...[:300]"`. On failure → `write_build_state(workspace_id,
   "agent_failed", ...)` and return.
6. **Phase 2 ARTEFACT-PRESENCE GATE** — agent returned `success=True` BUT
   `files_after - files_before` is empty → `write_build_state(workspace_id,
   "agent_no_artefact", ..., execution_success=False)` and return
   `ImplementationResult(success=False, error="Agent returned success
   but produced no artefact in {working_dir}. The spec was likely not an
   actionable implementation plan — Phase 2 artefact-presence gate.")`.
7. **Initial test run** — `test_results = test_fn(working_dir)`. If
   `_tests_passed(test_results)` → `write_build_state(workspace_id,
   "executed", repair_attempts, repair_log, execution_success=True)` and
   return success.
8. **Repair loop** — `while repair_attempts < max_repairs:`:
   - `repair_attempts += 1`
   - `repair_prompt = _build_repair_prompt(plan, test_results, repair_attempts, max_repairs)`
     (verbatim `REPAIR ATTEMPT {attempt}/{max_repairs}\n\n...minimal change...\n\n
     ORIGINAL IMPLEMENTATION PLAN:\n{plan}\n\nFAILED TEST OUTPUT:\n{failed_output}\n\n
     Repair the failing tests now.`)
   - `repair_result = agent_fn(repair_prompt, working_dir=working_dir)`
   - Agent failure → `write_build_state(workspace_id, "repair_agent_failed", ...)`, `continue`.
   - `test_results = test_fn(working_dir)`; if `_tests_passed` →
     `write_build_state(workspace_id, "executed", ..., execution_success=True)`,
     return success with `repair_result.output`.
   - Else `write_build_state(workspace_id, "repair_attempt", repair_attempts,
     repair_log, last_test_output=test_results["output"][:500])`.
9. **Budget exhaustion** — `_emit_human_required(workspace_id, plan,
   test_results, repair_attempts, max_repairs)` and return
   `ImplementationResult(success=False, test_results, repair_attempts,
   repair_log, error="Repair loop exhausted (N/max) — HUMAN_REQUIRED emitted to human_decision_manager")`.

#### 6.14.4 `_tests_passed` — Phase 2 strengthened contract (L146-163)

Returns `True` **only** when ALL hold:
- `test_results["runner"] != "none"` (a real test runner was found)
- `test_results["passed"] >= 1` (at least one test actually ran and passed)
- `test_results["failed"] == 0`
- `test_results["exit_code"] == 0`

Replaces the legacy `failed == 0`-alone check (which false-passed on a
no-test-files workspace — the answer-156 incident).

#### 6.14.5 `_emit_human_required` contract (L219-258)

On exhaust, persists a `HUMAN_REQUIRED` decision via
`human_decision_manager.save_pending`:

```python
decision = {
    "decision_id": f"repair_exhausted_{workspace_id[:8] if workspace_id else 'no_ws'}",
    "dispute_key": "implementation_repair",
    "dispute_text": "Implementation repair loop exhausted — tests still failing",
    "option_a": "Manual code fix",
    "option_b": "Abort build",
    "reasoning": f"OpenCode agent exhausted its repair budget ({repair_attempts}/{max_repairs} attempts) "
                f"and could not make the failing tests pass. Manual intervention required.",
    "outcome_type": "HUMAN_REQUIRED",
    "source": "implementation_executor.repair_loop",
    "repair_attempts": repair_attempts,
    "last_test_output": last_output,  # truncated 500
}
```

Also `write_build_state(workspace_id, "repair_exhausted", repair_attempts,
human_required=True, last_test_output=last_output)`.

#### 6.14.6 Build-state transitions written by the loop

`"implementing"` → (`"agent_failed"` | `"agent_no_artefact"` |
`"repair_agent_failed"` | `"repair_attempt"` | `"executed"` (with
`execution_success=True`) | `"repair_exhausted"` (with
`human_required=True`)).

#### 6.14.7 `execute_implementation` routing

- `use_claude_code=True` AND `agent_fn is None` →
  `coding_agent_router.get_agent("opencode", workspace_id)`; if no agent
  registered → falls back to `_direct_implementation` (cerebras).
- `agent_fn is not None` → `_execute_with_repair_loop(...).to_dict()`
  (the test seam always wins).
- `use_claude_code=False` → `_direct_implementation(plan, working_dir)`
  (cerebras-based, no repair loop).

#### 6.14.8 Tests

- `tests/test_repair_loop.py` — covers all the loop states, exhaust → human-required, resume from `_load_build_state`.
- `tests/test_implementation_engine.py` — `NativeImplementationEngine.execute` with injected seams, `_validate_pre` raising `CodingAgentError`, the artefact-presence gate, retry budget, cancel, resume.

---

### 6.15 `validation_engine.py` — Phase 3 NativeValidationEngine

```python
class NativeValidationEngine:
    ENGINE_NAME = "NativeValidation"
    def __init__(self, *, build_fn=None, test_fns=None, max_iterations=1)
    def validate(self, *, workspace_id, implementation_spec) -> ValidationReport
```

`build_fn` is `Callable[[workspace_id, spec], dict]` returning
`{success, exit_code, stdout, stderr, logs}`. `test_fns` is a dict of
`{unit, integration, smoke}` → `Callable[[workspace_id], dict]` returning
`{passed, total, failed, errors, output}`. `max_iterations` bounds the
build-then-test retry loop.

The `ValidationReport` carries the actual test runner name, passed,
failed, exit_code, the build logs, the per-suite results, the iteration
count, and the engine name.

Pinned by `tests/test_validation_engine.py::test_phase3_validation_engine_still_importable`
(`NativeValidationEngine` with injected `build_fn`/`test_fns` succeeds,
`engine == "NativeValidation"`).

### 6.16 `repair_memory.py` — Phase 3.1 RepairMemory

SQLite-backed memory of every repair attempt across the project. A
repair attempt that fails is recorded; subsequent attempts at the same
(strategy, target_files, failure_signature) are blacklisted.

```python
@dataclass
class RepairAttempt:
    workspace_id: str
    iteration: int
    strategy: str        # "patch" | "rewrite" | etc.
    target_files: list[str]
    failure_signature: str
    success: bool

class RepairMemory:
    def record_attempt(self, attempt: RepairAttempt) -> None
    def is_blacklisted(self, *, strategy: str, target_files: list[str],
                       failure_signature_value: str) -> bool
    def close(self) -> None
```

`is_blacklisted` deterministically suppresses retry attempts that already
failed for the same (strategy, target_files, failure_signature) — saving
exploration budget on later workspaces. Pinned by
`tests/test_repair_memory.py` (record_attempt + is_blacklisted + close).

---

End of Batch 7. Next batch: §6.17–6.21 Layer 3 integration (project planner, pipeline scheduler, pipeline runner, stage logger, workflow classifier).

---

### 6.17 `project_planner.py` — AI Project Planner

Decomposes an objective into a work-package DAG via Cerebras
GPT-OSS-120B. Pure stdlib at import; provider call is lazy.

#### 6.17.1 Constants

```python
DEFAULT_MODEL          = os.getenv("SOL0MON_PLANNER_MODEL", "gpt-oss-120b")
DEFAULT_MAX_RETRIES    = 3
RETRY_BACKOFF_SECONDS  = 2.0

WORK_PACKAGE_TYPES = (
    "research",
    "architecture",
    "implementation",
    "validation",
    "documentation",
)

REQUIRED_PACKAGE_FIELDS = (
    "id",
    "title",
    "type",
    "description",
    "deliverable",
    "dependencies",
)
```

#### 6.17.2 `PlannerError`

```python
class PlannerError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, cause: Exception | None = None):
```

#### 6.17.3 `validate_execution_graph` — the five guarantees

```python
def validate_execution_graph(graph: Any) -> list[str]:
    """Returns issue strings; empty = valid. Pure; no I/O; no model calls."""
```

| # | Error string (verbatim) |
|---|--------|
| 1 | `"duplicate work package id: '<pid>'"` |
| 2 | `"circular dependency: <a> -> <b> -> ..."` (reuses `validation._find_cycle`) |
| 3 | `"work_package[<i>] ('<pid>') is missing a non-empty 'deliverable'"` |
| 4 | `"work package '<pid>' depends on unknown package '<d>'"` |
| 5 | `"work_package[<i>] ('<pid>') has unknown type '<ptype>'; expected one of [...]"` |

Structural floor: non-dict → `"execution graph must be a JSON object"`;
missing/empty `work_packages` → `"execution graph must have a non-empty
'work_packages' list"`. Per-package: missing/empty `id` →
`"work_package[<i>] has a non-empty string 'id'"`; missing `dependencies`
→ `"work_package[<i>] ('<pid>') is missing 'dependencies' (use [] for
none)"`; non-list-of-strings deps → `"... dependencies must be a list of
strings"`.

#### 6.17.4 `topological_order` (lines 206-231)

```python
def topological_order(graph: dict) -> list[str]:
    """Kahn's algorithm, insertion-stable. Raises PlannerError on a cycle."""
```

#### 6.17.5 The LLM call + retry loop

```python
def plan_objective(objective: str, *, generate_fn: Callable[..., str] | None = None,
                   model: str | None = None, max_retries: int = DEFAULT_MAX_RETRIES) -> dict
```

Retries on: malformed JSON, empty response, provider failure
(network/timeout/5xx), schema-validation failure. Backoff
`RETRY_BACKOFF_SECONDS * attempt`. Raises `PlannerError` after exhausting
retries. The provider call (`_call_llm`) routes via `cerebras_client.generate`
→ the provider router chain.

#### 6.17.6 Persistence layout

```python
def _workspace_dir(workspace_id: str) -> Path    # delegates to build_workspace._workspace_dir
def _project_path(workspace_id: str) -> Path:    # <workspace>/project.json
def _graph_path(workspace_id: str) -> Path:      # <workspace>/planner/execution_graph.json
def save_execution_graph(workspace_id, graph) -> dict    # idempotent
def load_execution_graph(workspace_id) -> dict | None    # None if absent/corrupt
def load_project(workspace_id) -> dict | None
def has_execution_graph(workspace_id) -> bool             # present AND validate_execution_graph()==[]
```

`project.json` carries `workspace_id`, `objective`, `model`, `planner`
(=`"project_planner"`), `planned_at`, `package_count`.

#### 6.17.7 Resume contract

```python
def project_planner(objective, *, workspace_id=None, force=False,
                    generate_fn=None, model=None, max_retries=DEFAULT_MAX_RETRIES) -> tuple[str, dict]
```

"Runs once per workspace, resumes unless `force=True`": if `not force and
has_execution_graph(workspace_id)` → returns the persisted graph WITHOUT
calling the LLM. When `workspace_id is None`, creates a fresh
`build_workspace` (`create_workspace(source_question=str(objective)[:500])`),
falling back to `uuid.uuid4()` on failure.

#### 6.17.8 Planner LLM prompt rules (lines 245-290)

The prompt requires:

```json
{
  "objective": "<...>",
  "work_packages": [
    {
      "id": "wp_<short_slug>",
      "title": "<short title>",
      "type": "research" | "architecture" | "implementation" | "validation" | "documentation",
      "description": "<what this package must accomplish>",
      "deliverable": "<the concrete artefact this package produces>",
      "dependencies": ["<id of a package that must complete first>", ...]
    }
  ]
}
```

Hard rules:
- 3-8 work packages total
- Every package MUST have all six fields
- Unique `id`s
- Acyclic graph
- `validation` depends on `implementation`
- `documentation` depends on `implementation`
- `architecture` depends on `research`
- Do NOT pick technologies / name libraries / write code

#### 6.17.9 Tests

`tests/test_project_planner.py` covers: validation rejects each of the
five error classes, topological order stability, the resume-shortcut
(no LLM call when `has_execution_graph`), `force=True` re-plans,
`PlannerError` after exhausted retries.

---

### 6.18 `pipeline_scheduler.py` — dependency-ordered dispatcher

#### 6.18.1 Status name constants (exact, lines 65-72)

```python
PENDING   = "PENDING"
READY     = "READY"
RUNNING   = "RUNNING"
COMPLETED = "COMPLETED"
FAILED    = "FAILED"
BLOCKED   = "BLOCKED"

ALL_STATUSES = (PENDING, READY, RUNNING, COMPLETED, FAILED, BLOCKED)
_UNFINISHED = (PENDING, READY, RUNNING)     # count as "unfinished" for readiness
_BLOCKING   = (FAILED, BLOCKED)              # block dependents
```

#### 6.18.2 The READY-only-execute rule

- `_initial_status(graph)` (lines 137-146): every package `PENDING`;
  zero-dependency packages immediately `READY`.
- `_refresh_readiness` (lines 149-164): promote `PENDING` → `READY` only
  when ALL deps are `COMPLETED`; demote `PENDING` → `BLOCKED` when ANY dep
  is in `_BLOCKING`.
- `ready_packages(status)` returns ids whose `status == READY` only.
- The dispatch loop ONLY ever enqueues `ready_packages(status)`. A
  `PENDING` package NEVER reaches `pipeline_fn`.

#### 6.18.3 Public surface

```python
def run_scheduled_pipeline(
    graph: dict,
    *,
    pipeline_fn: Callable[[str], dict] | None = None,
    on_package_start: Callable[[str], None] | None = None,
    on_package_complete: Callable[[str, dict], None] | None = None,
    workspace_id: str = "",
    max_workers: int = 1,                       # default = sequential
    require_implementation: bool = False,
    knowledge_fn: Callable[[str, str], str] | None = None,
) -> dict:
```

`max_workers=1` (default) = sequential, preserving the existing
side-effect-heavy debate pipeline (browser CDP, Cerebras rate limits).
`>1` requires `pipeline_fn` to be stateless; uses `ThreadPoolExecutor` from
stdlib `concurrent.futures`.

#### 6.18.4 BLOCKED reasons

Two `skipped_reason` strings:

- `"dependency_failed"` (line 357 / 403) — a dependency failed.
- `"no_open_implementation_decisions"` (line 377 / 417) — the
  implementation-optionality guard observed no open implementation
  decisions.

#### 6.18.5 Implementation-optionality guard

```python
def _open_implementation_decisions(workspace_id: str, package_id: str) -> bool:
    """True iff there are verified decisions recorded for package_id that
    still name a concrete implementation choice (deploy/build/implement/create)."""
```

Loads
`workspace_knowledge.load_artifact(workspace_id, "verified", package_id)`;
if non-JSON or empty → returns `True` (back-compat). Reads
`verified_decisions` list; uses `execution_decision.IMPLEMENTATION_SIGNALS`
+ `_count_word_hits` to detect an implementation signal. When False, the
package ends up `BLOCKED` with
`skipped_reason="no_open_implementation_decisions"` and is NEVER
dispatched. `require_implementation=True` bypasses the guard entirely
(tests assert `"wp_impl" in res_req["succeeded"]`).

#### 6.18.6 `scoped_prompt` — debate separation

```python
def scoped_prompt(package: dict, objective: str, *, prior_knowledge: str = "") -> str
```

Renders `Parent objective: ...`, `Work package: ...`, `Type: ...`,
`Description: ...`, `Expected deliverable: ...`; when `prior_knowledge`
non-empty appends `"Verified prior-stage knowledge (consume this; do not
re-debate what is already settled):"` + the knowledge text. This is the
single source of the per-package prompt — downstream packages consume
verified prior-stage knowledge, not the raw user prompt.

#### 6.18.7 Return shape (lines 440-449)

```python
{
    "objective":  str,
    "packages":   {id: {status, result|error|skipped_reason, ...}},
    "succeeded":  [ids...],
    "failed":     [ids...],
    "skipped":    [ids...],
    "started_at":  iso,
    "completed_at": iso,
    "max_workers": int,
}
```

On completion, `save_debate_result` from `workspace_knowledge` is called
(best-effort, never blocks) so the next stage can load the verified
outcomes.

#### 6.18.8 Default seams

- `_default_pipeline_fn(prompt)` → `pipeline_runner.execute(prompt)` (lazy import).
- `_default_knowledge_fn(workspace_id, package_type)` → `workspace_knowledge.load_stage_input(...)`.
- `schedule(graph)` re-validates via `project_planner.validate_execution_graph`
  (raises `ValueError` on invalid graph) and returns `topological_order(graph)`.

---

### 6.19 `pipeline_runner.py` — single entry

```python
def log(message): ...                                  # "[YYYY-MM-DD HH:MM:SS] <msg>"
def execute(prompt, memory_ids=None): ...              # single-package: DebateEngine.run
def execute_scheduled(prompt, *, memory_ids=None, generate_fn=None,
                      pipeline_fn=None, force_replan=False, workspace_id=None,
                      max_retries=3, mode_select_fn=None, max_workers=1,
                      require_implementation=False): ...
```

`execute_scheduled` orchestration stages:

1. **PLANNER** → `project_planner(prompt, workspace_id=?, force=force_replan, generate_fn, max_retries)`. On `PlannerError`, logs `[PLANNER] FAILED:` and returns the structured-failure payload early.
2. **GRAPH** → `validate_execution_graph(graph)`; raises `RuntimeError(f"invalid graph persisted: {issues[:3]}")` if invalid; logs `[GRAPH] <n> work packages validated`.
3. **Execution Decision Engine** → `execution_decision_engine.decide(...)`. On exception returns structured failure (`planner_error="execution_decision_engine: <e>"` — never silently defaults to `RESEARCH_ONLY`). Persists via `_persist_decision` + `_log_decision`; writes to `build_state.json` via `write_build_state(workspace_id, "authority_resolved", execution_decision=..., ...)`.
4. **SCHEDULER** → `pipeline_scheduler.run_scheduled_pipeline(graph, pipeline_fn=per_package_fn, on_package_start=_on_start, on_package_complete=_on_complete, workspace_id=workspace_id, max_workers=max_workers, require_implementation=require_implementation)`. Type → stage tag map: `{"research": "RESEARCH", "architecture": "ARCHITECTURE", "implementation": "IMPLEMENTATION", "validation": "TEST", "documentation": "BUILD"}`.

Returns:
```python
{
    "workspace_id":  str,
    "graph":         {...} | None,
    "plan":          {...} | None,
    "authority":     {...},
    "scheduled":     {...} | None,
    "planner_error": str | None,
    "stages":        [Stage, ...],
}
```

---

### 6.20 `stage_logger.py` — `[STAGE]`-tagged audit trail

#### 6.20.1 The canonical stage list (exact)

```python
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
```

#### 6.20.2 `Stage` dataclass

```python
@dataclass
class Stage:
    name: str
    workspace_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    elapsed: float = 0.0
    status: str = "running"
    artifact: str = ""
    error: str = ""

    def to_dict(self) -> dict   # {name, workspace_id, started_at, completed_at, elapsed_s, status, artifact, error}
```

#### 6.20.3 `stage` context manager + `event` one-shot

```python
@contextmanager
def stage(name: str, *, workspace_id: str = "") -> Iterator[Stage]:
    # Entry:  "[<name>] start"  (+ "ws=<workspace_id[:8]>" when workspace_id)
    # Normal exit: "[<name>] complete (<X.XX>s)"  (+ " -> <s.artifact>" when set)
    # Exception:  "[<name>] FAILED (<elapsed>s) <error>"  (then re-raises)
    # Telemetry recorder: start_stage / end_stage (no-op when unconfigured)

def event(stage_name: str, message: str, *, artifact: str = "") -> None:
    # logs: "[<stage_name>] <message>"  (+ " -> <artifact>" when set)
```

Used by the scheduler for `dispatching <n> work packages (max_workers=<n>)`,
per-package `start <pid>` / `complete <pid> (<status>)` lines.

---

### 6.21 `workflow_classifier.py` — request classification

```python
class WorkflowType:
    DISCUSSION     = "DISCUSSION"
    RESEARCH       = "RESEARCH"
    ARCHITECTURE   = "ARCHITECTURE"
    IMPLEMENTATION = "IMPLEMENTATION"
```

#### 6.21.1 Per-workflow stage flags (`WORKFLOW_STAGES`)

| Type | research | arch | verify | gov | synth | impl_plan | user_approval | exec |
|------|----------|------|--------|-----|-------|-----------|---------------|------|
| DISCUSSION | T | F | F | F | T | F | F | F |
| RESEARCH | T | F | F | F | T | F | F | F |
| ARCHITECTURE | T | T | T | T | T | F | F | F |
| IMPLEMENTATION | T | T | T | T | T | T | T | T |

#### 6.21.2 `WorkflowClassification` dataclass

```python
@dataclass
class WorkflowClassification:
    workflow_type: str
    confidence: float
    reasoning: str = ""
    requires_research: bool = True
    requires_architecture: bool = False
    requires_verification: bool = False
    requires_governance: bool = False
    requires_synthesis: bool = False
    requires_implementation_plan: bool = False
    requires_user_approval: bool = False
    requires_execution: bool = False
```

#### 6.21.3 Public API

```python
def classify(prompt: str) -> WorkflowClassification
                            # rule-based first; LLM fallback (provider_router) only when ambiguous.
                            # winner threshold: best_score>=2 AND best_score>=runner_up+2,
                            # OR best>=1 and runner_up==0.

def is_implementation(prompt: str) -> bool
def can_execute_code(prompt: str) -> bool
                            # Only IMPLEMENTATION workflows with requires_execution=True may invoke Claude Code.
```

End of Batch 8. Next batch: §6.22–6.26 Build pipeline + spec engine + decision renderer + provider router.

---

### 6.22 `build_pipeline.py` — the integrated build flow

The canonical `run_build_pipeline` + helpers compose the integration layer
into a single user-facing entry for the build path. The callable seams
match the existing philosophy: every collaborator is injectable for
tests.

#### 6.22.1 Canonical signature

```python
def run_build_pipeline(
    prompt: str,
    answer_spec: str | None = None,
    debate_fn: Callable[..., dict] | None = None,
    executor_fn: Callable[[str], dict] | None = None,
    source_result: dict | None = None,
    use_claude_code: bool = False,
    mode_select_fn: Callable[[str], tuple[str, bool]] | None = None,
    planner_fn: Callable[..., Any] | None = None,
) -> dict
```

#### 6.22.2 Step order (the seam names consumed)

1. **Classify** (`classify_prompt`) → non-`IMPLEMENTATION` short-circuits with `{"status":"not_implementation"}`.
2. **Determine source** — `resurrect_answer(answer_spec)` if the prompt references an answer (Path B) OR `create_workspace(source_question=prompt)` for a fresh build (Path C). Sets `user_intent_prompt` = the resurrected answer's `question` (resurrection) or the prompt itself (fresh).
3. **Aggregate human decisions** (`aggregate_human_decisions`) when `source_result.human_required`.
4. **Execution Decision Gate** (`_resolve_execution_decision`) — the authoritative terminator. Computed at most once; persists to `build_state.json`. If blocked → records `blocked_reason=execution_decision` and returns the structured payload with `spec_status="blocked"`. If `mode_select_fn` supplied, persists `recommended_mode`/`selected_mode`/`override_used` via `execution_mode_gate.save_mode_selection` and re-applies via `apply_mode_override`.
5. **Generate build spec** (`generate_build_spec` → `build_spec_engine.build_spec`) — the `debate_fn` seam.
6. **Plan** (Hardening Part 1) — `planner_fn(spec_result)`; default `execution_planner.plan`. Persists `execution_plan.json`. Failure is observational — the spec is still the agent's primary input.
7. **Execute** (`execute_build_spec`) — the `executor_fn` (test mock) → `use_claude_code=True` → `implementation_executor.execute_implementation` seam.
8. **Persist manifest** (`_persist_build_manifest` → `manifest.build_manifest_from_workspace`).

#### 6.22.3 `execute_build_spec` dispatch priority + halt-on-failure contract

```python
# Dispatch priority:
#   0. pending human decisions -> BLOCKED (Task 2 gate; before anything).
#   1. executor_fn(spec_text)  -> test mock (always invoked when provided).
#   2. use_claude_code=True    -> implementation_executor.execute_implementation.
#   3. otherwise              -> skipped with a structured result.
```

Three BLOCKED shaped returns, in evaluation order, each short-circuiting
before dispatch:

```python
# 0. HUMAN ESCALATION GATE (Task 2):
{"blocked": True, "blocked_reason": "pending_human_decisions",
 "pending_count": int, "pending_decisions": list,
 "workspace_id": str, "success": False}

# 1. EXECUTION DECISION GATE (defense-in-depth, reads _load_execution_decision_block):
{"blocked": True, "blocked_reason": "execution_decision",
 "execution_decision": str, "execution_reason": str, "execution_confidence": int,
 "status": str, "workspace_id": str, "success": False}

# 2. SPEC COMPLETENESS GATE (Phase 2, reads _load_workspace_state):
{"blocked": True, "blocked_reason": "incomplete_spec",
 "spec_status": str, "spec_missing_sections": list,
 "workspace_id": str, "success": False}

# Normal skip (no executor):
{"status": "spec_generated", "workspace_id": str, "success": None, "skipped": True}
```

`run_build_pipeline` translates each `blocked_reason` into a
`build_state.json` status `blocked_<reason>`
(`blocked_pending_human_decisions`, `blocked_incomplete_spec`).

#### 6.22.4 `_resolve_execution_decision` precedence + override (lines 423-533)

Phase 7 ordering (exact):
1. `execution_mode_gate.load_mode_selection(workspace_id)` →
   `decision_from_mode_selection` (resume override authority, searched FIRST).
2. Persisted in `source_result.verification_result` →
   `from_verification_result` (source `"persisted"`).
3. `_load_execution_decision_block(workspace_id)` (`build_state.json`
   then `answer.json`) → source `"workspace_state"`.
4. `classify_execution_decision(question=prompt, verified_decisions=…,
   verified_tradeoffs=…, human_required=…, synthesized_answer=…,
   verification_result=…)` → source `"computed"`.

`_maybe_engine_override` upgrades a soft-heuristic persisted decision
(source ∈ {`research_analysis`, `architecture_decisions`}) when the
user's original prompt carries explicit implementation/architecture intent
and the engine returns a non-`RESEARCH_ONLY` mode whose legacy decision
differs from the persisted one. Hard governance blocks
(`STOP_UNRESOLVED_DISPUTES` / `STOP_HUMAN_REQUIRED`) are immutable (engine
honors them). **Never downgrades.**

---

### 6.23 `build_spec_engine.py` — the structured spec (Path 3 deterministic)

```python
def build_spec(
    source_answer: str,
    debate_fn: Callable[..., dict] | None = None,
    source_result: dict | None = None,
    memory_ids: list | None = None,
    target_components: list[str] | None = None,
) -> dict
```

Returns:

```python
{
    "spec_text":           str,    # the implementation_plan.md body
    "prompt":             str,     # the build prompt (legacy) or "" (Phase 3 path)
    "decisions":          [...],   # extract_decisions -> [{key, decision, reasoning}]
    "constraints":        [...],   # extract_constraints -> [{key, constraint, reasoning}]
    "tradeoffs":          [...],   # extract_tradeoffs -> [{key, option_a, option_b, recommendation, reasoning}]
    "verified_decisions": [...],   # from source_result
    "verification_result":  dict,   # from source_result
    "decision_gaps":      [...],   # FIX 5 report-only (verified decisions not honoured by spec_text)
    "debate_result":         dict,  # raw result from legacy engine, or {"status":"completed"} (Phase 3)
    "status":            str,      # "completed" | "incomplete"
    "missing_sections":  [...],    # SPEC_SECTIONS not found in spec_text
}
```

#### 6.23.1 Path-3 deterministic renderer contract

When `debate_fn is None` (production default), `build_spec` calls:

```python
def _render_spec_sections(source_result: dict | None,
                          target_components: list[str] | None = None) -> str:
    """Phase 3: deterministically render the 7 SPEC_SECTIONS from the
    canonical decision model. Pure; no LLM, no I/O."""
```

The seven `SPEC_SECTIONS` come from `build_prompt_generator.SPEC_SECTIONS`:
`architecture`, `components`, `file structure`, `dependencies`,
`constraints`, `testing requirements`, `acceptance criteria`. The renderer
composes:
- `# Architecture` ← `decision_renderer.render_architecture(verification_result)`
  (Phase 1 corrected; never emits a rejected option)
- `# Components` ← `decision_renderer._architecture_components(verification_result)`
  (matched platform components + verified decisions; sentinel
  `"(no components inferred…)"` only when both lists are empty, telling
  the agent to derive from `# Constraints`)
- `# File Structure` ← `src/<slug>/  # <name>` per component + `tests/` + `README.md`
- `# Dependencies` ← `_PLATFORM_DEPENDENCIES` table + `_DECISION_DEPENDENCY_HINTS`
  (sqlite→stdlib `sqlite3`; postgresql→`psycopg2` OR `sqlite3`; redis→`redis-py`;
  fastapi/flask/django); floor `- Python 3.10+ standard library`
- `# Constraints` ← verified decisions as `- MUST: <rule> — <reasoning>` +
  verified tradeoffs as `- RECOMMENDED: <rec> (tradeoff: <key> — <a> vs
  <b>)`; floor `(no immutable constraints recorded; ...)`
- `# Testing Requirements` ← constant (pytest runner; every MUST has a
  test; 80% coverage; suite exits 0 with `>=1` passing test — Phase 2
  strengthened success contract)
- `# Acceptance Criteria` ← constant (`validate_spec_sections` returns `[]`;
  `decision_gaps` empty; suite exits 0 with `>=1` passing; no
  stub/placeholder tests; agent produced `>=1` new file — Phase 2
  artefact-presence gate)

Then `validate_spec_sections(spec_text)` is run; non-empty →
`status="incomplete"`. `_spec_decision_gaps(spec_text,
verified_decisions)` flags verified decisions whose `normalized_key` /
winner tokens are not in the spec (report-only; never blocks).

#### 6.23.2 Platform dependency table (verbatim, lines 96-108)

```python
_PLATFORM_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "Temporal durable workflows":         ("temporalio",),
    "PostgreSQL primary datastore":        ("psycopg2-binary", "sqlalchemy>=2.0", "alembic"),
    "Cloudflare R2 immutable storage":      ("boto3",),
    "Local scratch filesystem":            (),
    "Docker workers":                       ("docker",),
    "Firecracker microVMs":                 (),
    "LiteLLM":                              ("litellm",),
    "Sol0mon control plane":                (),
    "Hybrid BYOK model":                    (),
    "Provider abstraction layer":          (),
}
```

#### 6.23.3 Persistence

```python
def store_implementation_spec(workspace_id, spec_text) -> bool
                            # writes <workspace>/implementation_plan.md
def load_implementation_spec(workspace_id) -> str
def _default_debate_fn(prompt, memory_ids=None) -> dict
                            # LEGACY audit-debate path (pipeline_runner.execute). Not the production default.
```

---

### 6.24 `build_prompt_generator.py` — spec section validators

```python
SPEC_SECTIONS: list[str] = [
    "architecture", "components", "file structure", "dependencies",
    "constraints", "testing requirements", "acceptance criteria",
]

def extract_decisions(result: dict) -> list[dict]    # [{key, decision, reasoning}]
def extract_constraints(result: dict) -> list[dict]  # verified decisions as rules -> [{key, constraint, reasoning}]
def extract_tradeoffs(result: dict) -> list[dict]    # [{key, option_a, option_b, recommendation, reasoning}]
def generate_build_prompt(source_answer, decisions=None, constraints=None,
                          tradeoffs=None, target_components=None) -> str
def validate_spec_sections(spec_text: str) -> list[str]
                            # case-insensitive bare section match; returns missing
```

`_normalize_bucket(result, bucket, fallback=True)` falls back to scanning
`validated_disputes` by `outcome_type` when the named bucket is absent,
mapping `VERIFIED_DECISION` / `VERIFIED_TRADEOFF` / `HUMAN_REQUIRED`.

Pinned by `tests/test_build_prompt_generator.py`.

---

### 6.25 `decision_renderer.py` — the Phase 1 deterministic renderer

The Phase 1 path-3 deterministic renderer. `_architecture_components` is
the SOLE builder of architecture; it computes `winner_text(d)` via
`decision_model` (text after the winner marker, cut at the first
enriched-grammar REJECT signal), matching aliases against the WINNER side
only (never the dispute title — which names BOTH options and was the
source of the answer-156 PostgreSQL leak).

#### 6.25.1 Public surface

```python
def render_verified_decisions(verification_result, governance_context=None) -> str
def render_verified_tradeoffs(verification_result, governance_context=None) -> str
def render_human_required(verification_result, governance_context=None) -> str
def render_architecture(verification_result=None, governance_context=None,
                         verified_decisions=None, verified_tradeoffs=None,
                         human_required=None) -> str
                            # "# Architecture" — "No verified architectural decisions recorded." when empty
def render_implementation_plan(...) -> str
def render_migration_path(...) -> str
def render_risks(...) -> str
def render_narrative_sections(...) -> str    # all four in canonical order; no LLM; byte-reproducible
def validate_synthesis_consistency(synthesized_answer, verification_result,
                                    governance_context=None) -> list[dict]
                            # FIX 6 + Phase 1 guard
def contradiction_warning(contradictions: list[dict]) -> str
```

#### 6.25.2 The component model

Hard-coded `_COMPONENTS` tuple of 12 platform components:
- Temporal durable workflows
- PostgreSQL primary datastore
- Cloudflare R2 immutable storage
- Local scratch filesystem
- Docker workers
- Firecracker microVMs
- LiteLLM
- Sol0mon control plane
- Hybrid BYOK model
- Provider abstraction layer
- Multi-region deployment
- Advanced governance

Each has `name, risk_name, layer, aliases, step, migration, phase, risk`.

`_ARCH_LAYERS` = `("Orchestration Layer", "Persistence Layer",
"Storage Layer", "Execution Layer", "LLM Gateway Layer", "Governance
Layer", "Cost Layer", "Infrastructure Layer")`.

#### 6.25.3 Defense-in-depth

Any platform component whose alias appears in `rejected_options(d)` is
suppressed. Generic classifier fallback (`_GENERIC_KEYWORDS` / `_GENERIC_CLASSIFY_ORDER`)
for verified decisions naming a concrete technology with no platform alias:
`Integration > Data > Application > Infrastructure` precedence; only emits
when `winner_text` is a clean short label (`_is_clean_label`) and no purely
-rejected token passes as the winner.

#### 6.25.4 `validate_synthesis_consistency`

Returns `[{"type": "REJECTED_OPTION_RECOMMENDED" | "WINNER_REJECTED",
"dispute", "normalized_key", "option", "evidence"}]`. Only the LLM
narrative is scanned (deterministic `# Verified Decisions / Tradeoffs /
Human Decisions Required` headers are stripped via `_narrative_of`).

Context verbs:
- `_RECOMMEND_CONTEXTS` = `use/adopt/choose/select/recommend/deploy/
  implement/build/power/built on/...`
- `_NEGATE_CONTEXTS` = `do not use/avoid/reject/instead of/rather than/
  off-limits/...`

Both compiled whole-word via `_compile_context_res`. Interpretation
primitives (`winner_tokens`, `rejected_options`, `winner_text`,
`derive_rejected_options`, `_REJECT_SIGNALS_RE`, etc.) are OWNED by
`decision_model` and re-exported from here — DO NOT re-add parsing here.

Constant: `LOW_CONF_THRESHOLD = 80`.

---

### 6.26 `provider_router.py` — the 4-tier inference failover chain

The single multi-provider inference system with automatic failover and
per-key health tracking.

#### 6.26.1 The single entry point

```python
def generate(prompt: str, module: str = "unknown") -> str:
    """Route through the provider hierarchy. On all-providers-failed,
    raises RuntimeError. 'cerebras_client.generate()' delegates here."""
```

The chain (lines 6, 305-310):

```
Cerebras  →  Groq  →  OpenRouter  →  Ollama
```

Each tier is `_try_cerebras` / `_try_groq` / `_try_openrouter` /
`_try_ollama`.

#### 6.26.2 Failover / non-failover conditions (exact)

```python
FAILOVER_ERROR_PATTERNS = [
    "queue_exceeded", "429", "rate limit", "too many requests", "timeout",
    "timed out", "connection", "network", "unreachable", "unavailable",
    "service unavailable", "internal server error", "503", "502", "504",
]

NON_FAILOVER_PATTERNS = [
    "401", "403", "400", "authentication", "unauthorized", "forbidden",
    "invalid request", "malformed",
]

def _should_failover(error: str) -> bool:
    # NON_FAILOVER check FIRST (returns False), then FAILOVER (True),
    # else "unknown errors → failover (conservative)" True.
```

Failover: `queue_exceeded` / 429 / rate-limit / timeout / network /
unreachable / unavailable / 5xx (502/503/504) / internal server error.
Non-failover (terminal, raises immediately, does NOT try other tiers):
400 / 401 / 403 / authentication / unauthorized / forbidden / invalid
request / malformed.

#### 6.26.3 Per-tier key rotation + provider health

```python
HEALTH_THRESHOLD_FAILURES = 3    # consecutive failures → unhealthy
HEALTH_COOLDOWN_SECONDS   = 60   # avoid unhealthy provider for this long
HEALTH_OBSERVATION_WINDOW = 60    # sliding window for success rate

@dataclass
class ProviderHealth:
    name: str
    total_requests: int = 0
    total_successes: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    last_failure: str = ""
    last_success: str = ""
    cooldown_until: float = 0.0
    is_healthy: bool = True
    def record_success(self) -> None    # resets consecutive_failures + cooldown_until=0
    def record_failure(self) -> None    # consecutive_failures += 1; if >= THRESHOLD → unhealthy + cooldown
    def should_skip(self, now: float) -> bool    # True when unhealthy AND now < cooldown_until
    def success_rate(self) -> float    # 0..1 in the observation window
```

Per tier, multiple keys via comma-separated env var (`*_API_KEYS`); if a
key fails, rotate to the next; only fail the provider after all keys
exhausted. Per tier, multiple models via comma-separated `*_MODELS`; try
in order; decommissioned / not-found / paid-only models are skipped
automatically within each provider.

#### 6.26.4 Verified working models (June 2026)

| Tier | Provider | Working models | Status |
|------|----------|---------------|--------|
| 1 | Cerebras | `zai-glm-4.7` (355B), `gpt-oss-120b` (120B) | FREE, rate-limited |
| 2 | Groq | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` | PAID only |
| 3 | OpenRouter | `google/gemini-2.5-flash`, `deepseek/deepseek-chat-v3-0324`, `llama-3.3-70b`, `qwen-2.5-72b`, `mistral-small-3.1-24b`, `gemini-2.5-flash-lite`, `llama-3.1-8b` | Many FREE |
| 4 | Ollama | `qwen3:8b` | FREE, local |

Decommissioned/trap models (do NOT use):
- Groq: `llama-3.3-70b-specdec`, `llama-3.2-1b-preview`, `llama-3.2-3b-preview`
- OpenRouter: `deepseek/deepseek-r1-distill-qwen-32b` (404)

#### 6.26.5 Per-tier client contracts

`cerebras_client.generate(prompt, module=...)` delegates to
`provider_router.generate` (backward-compat). `groq_client`,
`openrouter_client`, `ollama_client` have their own `generate` with key
rotation; the router calls them in order.

End of Batch 9. Next batch: §6.27–6.29 Workspace orchestrator + legacy orchestrator + LangGraph, then move to §8 state machines.

---

### 6.27 `workspace_orchestrator.py` — the workspace-facing API

```python
def submit_workspace(prompt: str, memory_ids: list | None = None) -> dict
                            # {"workspace_id", "job_id", "status": "DEBATING"}
def follow_up_workspace(workspace_id: str, follow_up: str) -> dict
                            # {"workspace_id", "job_id", "status": "DEBATING",
                            #  "existing_decisions", "existing_tradeoffs", "pending_human_items"}
                            # Only affected disputes are re-debated. Does NOT rerun the full pipeline.
def reopen_workspace_dispute(workspace_id, dispute_key, reason: str = "") -> dict
def resolve_human_requirement(workspace_id, decision_id, chosen_option) -> dict
                            # {"status":"resolved", "pending_remaining": int}
                            # If all pending cleared → ws.status = CONSENSUS_REACHED
def get_workspace_status(workspace_id) -> dict
def list_workspaces(status: str | None = None, limit: int = 50) -> list[dict]
def get_answer_chain(answer_id: int) -> list[dict]
```

`follow_up_workspace` calls
`WorkspaceDebateEngine.resume_workspace(workspace_id, follow_up)` which
runs affected-dispute analysis (`workspace_impact_analyzer`) and re-debates
only the affected subset. The wrapper marks the job COMPLETED/FAILED in
the SQLite `jobs` table (`job_store`).

### 6.28 `orchestrator.py` — legacy job API

```python
def submit_job(prompt: str, memory_ids: list | None = None) -> str    # returns job_id
def get_status(job_id: str) -> dict
def get_result(job_id: str) -> dict    # the result, including synthesized_answer
```

Wraps the legacy `job_store`. The workspace-based API (§6.27) is the
recommended path; `orchestrator` is preserved for back-compat with the
README's first example.

### 6.29 `sol0mon_graph.py` — LangGraph workflow orchestration

Optional. LangGraph-based orchestration with a `MemorySaver` checkpointer;
requires `pip install langgraph`. The Phase 4 `WorkflowOrchestrator`
(§`workflow_orchestration.md`) does NOT require LangGraph; it is
pure-stdlib. This module is for callers that want graph-native
checkpointing / replay via LangGraph's primitives.

---

## 8. Cross-layer state machine definitions

Every authority that owns a state machine is enumerated here, with its
states and the transitions each permits.

### 8.1 `Dispute.status` (Layer 1 — `debate_engine`)

States (verbatim `debate_engine.py:80`):
```
active | pending_verification | resolved | deadlocked | tradeoff | human_required
```

`outcome_type` (line 82):
```
VERIFIED_DECISION | VERIFIED_TRADEOFF | HUMAN_REQUIRED
```

The `_debate_dispute` returns one of `{resolved, verified_decision,
verified_tradeoff, human_required}`. The outer pipeline maps these to
`pending_verification / tradeoff / human_required / deadlocked`. The
verification loop maps `pending_verification` to `resolved / tradeoff /
human_required / active` (re-debate retry when `verification_retries <
MAX_VERIFICATION_RETRIES=1`).

### 8.2 Verifier per-record status (Layer 1 — `verification_engine`)

States: `RESOLVED | UNRESOLVED | EQUIVALENT` (coerced to `UNRESOLVED` when
outside the enum). Bucketing: `RESOLVED` → `resolved_claims`,
`EQUIVALENT` → `equivalent_claims`, else → `unresolved_claims`.

### 8.3 Workspace lifecycle (Layer 3 — `workspace_manager.WorkspaceStatus`)

States:
```
created → debating → consensus_reached → completed → archived
                                ↓
                        awaiting_human → consensus_reached (after resolution)
```

`resolve_human_requirement` clears `awaiting_human` → `consensus_reached`
when the pending count drops to 0.

### 8.4 Pipeline scheduler (Layer 3 — `pipeline_scheduler`)

States (exact):
```
PENDING → READY → RUNNING → COMPLETED | FAILED
PENDING → BLOCKED (when any dependency is `FAILED` or `BLOCKED`)
PENDING → BLOCKED (when `no_open_implementation_decisions` AND NOT require_implementation=True)
```

`ready_packages` returns `READY` packages only. A `PENDING` package
NEVER reaches `pipeline_fn`.

### 8.5 Execution node graph (Layer 2 — `implementation_engine`)

States (exact):
```
PENDING | RUNNING | COMPLETED | FAILED | SKIPPED | CANCELLED | BLOCKED
```

Nodes: `START → GenerateCodeNode → RunTestsNode → FinishNode |
(AnalyzeFailureNode → RepairNode → GenerateCodeNode loop | FinishNode(FAILED))`.
TransitionEngine is the SOLE authority (§6.13.2).

### 8.6 Execution planner (Layer 3 — `execution_decision_engine`)

Stage states (exact):
```
pending | running | completed | skipped | blocked
```

The `_MODE_STAGES` map (§6.10.2) specifies the per-mode ordered stages;
CUSTOM falls back to FULL_BUILD.

### 8.7 Build lifecycle (Layer 4 — Hardening, §`architecture_hardening.md` §A)

```
NEW → VERIFIED → SPEC_READY → EXECUTION_ALLOWED → EXECUTING →
ARTIFACTS_VALIDATED → TESTS_VALIDATED → COMPLETE
```

Side / terminal: `REPAIRING, FAILED, BLOCKED, ABORTED`. The full transition
table is in §A of the hardening doc.

### 8.8 Workflow state machine (Layer 5 — Phase 4, §`workflow_orchestration.md` §5)

```
PLANNING → RESEARCH → DEBATE → CONSENSUS → ARCHITECTURE →
IMPLEMENTATION → VALIDATION → REPAIR → QUALITY_GATE →
PACKAGING → DELIVERY → COMPLETED
```

Side / terminal: `FAILED, PAUSED, CANCELLED`. The full transition table is
in §5.3 of the Phase 4 doc.

### 8.9 Non-overlap guarantee

| Concern | Owner | Non-replacement |
|---------|-------|------------------|
| Workflow transition | `WorkflowStateMachine.next_state` | No other component writes `ctx.current_state` inside `_drive` |
| Build lifecycle transition | `lifecycle.transition` | No other component writes `lifecycle_stage` |
| Execution graph transition | `TransitionEngine.next_node` | No other component writes `ctx.current_node` |
| Dispute status | `DebateEngine._debate_dispute` / `_run_verification` | Stages never decide transitions |
| Scheduler status | `_refresh_readiness` | The dispatch loop only reads `ready_packages` |

---

## 9. Sequence diagrams (cross-layer)

### 9.1 The full pipeline (single-dispute path, fresh prompt)

```
caller → run_build_pipeline(prompt)
            │
            ↓  classify_prompt → IMPLEMENTATION
            │
            ↓  resurrect_answer(answer_spec) OR create_workspace(prompt)
            │
            ↓  aggregate_human_decisions (if source_result.human_required)
            │
            ↓  _resolve_execution_decision  → [AUTHORITY] EFFECTIVE: <decision>
            │     ├─ load_mode_selection (resume override, FIRST)
            │     ├─ from_verification_result (persisted in source_result)
            │     ├─ _load_execution_decision_block (build_state / answer.json)
            │     └─ classify_execution_decision (computed)
            │
            ↓  blocked? → return blocked payload
            │
            ↓  generate_build_spec → build_spec_engine.build_spec(debate_fn=debate_fn)
            │     └─ Path 3 deterministic renderer ← decision_renderer ← decision_model
            │
            ↓  planner_fn(spec_result) → execution_plan.json
            │
            ↓  execute_build_spec → halt-on-failure gates (human / decision / spec completeness)
            │     └─ executor_fn OR use_claude_code=True → implementation_executor
            │         └─ _execute_with_repair_loop (MAX_REPAIR_ATTEMPTS=3)
            │             └─ on exhaust → _emit_human_required → human_decision_store
            │
            ↓  _persist_build_manifest → manifest.build_manifest_from_workspace
            │
            ← result dict
```

### 9.2 The debate engine single-dispute loop

```
DebateEngine._debate_dispute(question, dispute, gpt, gemini)
   ├─ round 1..MAX_ROUNDS_PER_DISPUTE
   │    ├─ critique then revise each side (gpt, gemini)
   │    ├─ info_state.record_round(gpt_pos, gemini_pos)
   │    ├─ every 3 rounds: _eval_consensus → score >=92 → "resolved"
   │    ├─ info_state.is_stagnating(STAGNATION_CHECKS=3) → _verifier_review_dispute
   │    │      ├─ VERIFIED_DECISION → set agreed → "verified_decision"
   │    │      ├─ VERIFIED_TRADEOFF → "verified_tradeoff"
   │    │      └─ else → "human_required"
   │    ├─ _detect_stagnation =="deadlock" → re-run verifier review
   │    └─ _detect_stagnation =="warning" → force-engagement extra prompt
   └─ MAX_ROUNDS exhausted → verifier review with same 3-way mapping
```

### 9.3 `execute_build_spec` halt-on-failure gates

```
execute_build_spec(workspace_id, executor_fn=None, use_claude_code=False)
   ├─ _load_pending_human_decisions(workspace_id) → BLOCKED (pending_human_decisions)
   ├─ _load_execution_decision_block(workspace_id) → BLOCKED (execution_decision)
   │     └─ NOT IMPLEMENTATION_REQUIRED → blocked_reason=execution_decision
   ├─ _load_workspace_state(workspace_id) → BLOCKED (incomplete_spec)
   │     └─ spec_status != completed or missing_sections non-empty
   │
   ├─ executor_fn is not None → executor_fn(spec_text) → return
   ├─ use_claude_code=True → implementation_executor.execute_implementation
   │     → return
   └─ else → {"status": "spec_generated", "success": None, "skipped": True}
```

### 9.4 The research escalation loop (Layer 1)

```
verify_all_disputes(question, validated_disputes, gpt_pos, gemini_pos, research_fn=None, prior_research_evidence=None)
   ├─ for each dispute:
   │    ├─ needs_research AND prior_ev → reuse (no increment, no model) → Pass 2 record
   │    ├─ needs_research AND budget_available → research_fn(topic) → Pass 2 record (increment++)
   │    └─ needs_research AND no budget → HUMAN_REQUIRED (research_budget_exhausted)
   │
   ├─ run_verification_batch_inference → per-record {confidence, finding, status, requires_external_evidence}
   ├─ _persist_rejected_options per record → structured field read by decision_model
   ├─ attach_research_evidence (merges by normalized_key)
   └─ return {resolved_claims, equivalent_claims, unresolved_claims, verification_score,
              research_evidence, research_escalation_count, research_budget_exhausted, ...}
```

### 9.5 The provider router failover

```
provider_router.generate(prompt, module="unknown")
   ├─ Cerebras tier:
   │     ├─ for each model in CEREBRAS_MODELS:
   │     │     ├─ for each key in CEREBRAS_API_KEYS:
   │     │     │     ├─ if ProviderHealth.should_skip(now) → continue
   │     │     │     ├─ POST /responses → response or error
   │     │     │     ├─ error → ProviderHealth.record_failure; _should_failover?
   │     │     │     │     ├─ NON_FAILOVER pattern → raise (terminal)
   │     │     │     │     └─ FAILOVER pattern → rotate to next key
   │     │     │     └─ success → ProviderHealth.record_success; return text
   │     │     └─ rotate to next model
   │
   ├─ Groq tier (same pattern, multiple keys + multiple models)
   ├─ OpenRouter tier (same pattern, many models; free models preferred)
   └─ Ollama tier (local; last resort; no api key)
   │
   └─ all tiers exhausted → raise RuntimeError("All providers failed")
```

### 9.6 The pipeline scheduler dispatch loop

```
run_scheduled_pipeline(graph, pipeline_fn=per_package_fn, workspace_id, max_workers=1, require_implementation=False)
   ├─ status = _initial_status(graph)  # every package PENDING; zero-dep → READY
   ├─ _refresh_readiness(status, by_id)
   │     └─ PENDING with ALL deps COMPLETED → READY
   │     └─ PENDING with ANY dep in {FAILED, BLOCKED} → BLOCKED ("dependency_failed")
   ├─ while ready_packages(status):
   │     ├─ _open_implementation_decisions(workspace_id, pid) [if package is implementation + not require_implementation]
   │     │     └─ False → BLOCKED ("no_open_implementation_decisions")
   │     ├─ scoped_prompt(package, objective, prior_knowledge=knowledge_fn(workspace_id, type))
   │     ├─ ThreadPoolExecutor.submit(pipeline_fn, prompt)  (max_workers=1 default = sequential)
   │     ├─ on_package_start(pid) → sl.event(stage_tag, "start <pid>")
   │     ├─ result = pipeline_fn(prompt) → status[pid] = RUNNING → COMPLETED | FAILED
   │     └─ on_package_complete(pid, result) → sl.event(stage_tag, "complete <pid> (<status>)")
   │        └─ workspace_knowledge.save_debate_result(workspace_id, pid, type, result)
   │
   └─ until all packages are COMPLETED, FAILED, or BLOCKED
```

### 9.7 The Phase 4 wrap (cross-layer — Phase 4 drives Layer 1-3)

```
NativeWorkflowOrchestrator.run(workspace_id, user_prompt)
   ├─ WorkflowStateMachine.start(ctx) → PLANNING
   ├─ PolicyEngine.evaluate(ctx, next_state=PLANNING) → allowed
   ├─ StageRegistry.get(PLANNING).validate(ctx) → []
   ├─ StageRegistry.get(PLANNING).execute(ctx) → StageResult  (production wiring: project_planner)
   │     └─ persists <workspace>/planner/execution_graph.json
   ├─ ctx.current_state = sm.next_state(ctx, result) → RESEARCH
   ├─ (loop RESEARCH → DEBATE → CONSENSUS → ARCHITECTURE → IMPLEMENTATION → VALIDATION)
   │     └─ production wiring: each stage internally calls the corresponding Layer 1-3 component
   │         DEBATE → DebateEngine.run + VerificationEngine.verify_all_disputes + SynthesisEngine.synthesize_answer
   │         ARCHITECTURE → build_spec_engine.build_spec(debate_fn=None) [Path 3 deterministic]
   │         IMPLEMENTATION → implementation_executor.execute_implementation (use_claude_code=True)
   │         VALIDATION → validation_engine.NativeValidationEngine.validate
   │         REPAIR → implementation_executor._execute_with_repair_loop resumed
   │
   ├─ QUALITY_GATE → validation.validate_tests (strengthened Phase 2 contract)
   ├─ PACKAGING → PackagingEngine.package → 11 outputs under <workspace>/package/
   ├─ DELIVERY → no-op (or external ship)
   └─ COMPLETED → final result dict
```

### 9.8 The resurrection/resume across layers

```
resume_build(workspace_id)  # Hardening resume
   ├─ current_lifecycle(workspace_id)  → SPEC_READY | EXECUTING | ...
   ├─ _load_or_rebuild_spec_result (reuse if checksum matches; else regenerate Path 3)
   ├─ _load_or_rebuild_plan (reuse if checksum matches; else execution_planner.plan)
   ├─ validate("build_spec", spec_dict) + validate("execution_plan", plan.to_dict())
   └─ dispatch to execute_build_spec OR no-op (terminal)

RecoveryManager.recover(workspace_id)  # Phase 4 diagnostic
   ├─ load_state(workspace_id) → ctx or None
   ├─ validate_state(ctx) → issues
   └─ return {recoverable: not issues, issues, workflow_id, current_state, stage_results_count, workspace_id}

NativeWorkflowOrchestrator.resume(workspace_id)  # Phase 4 execute
   ├─ RecoveryManager.load_state(workspace_id)
   │     ├─ None → structured error ("no checkpoint to resume from ...")
   │     └─ ctx
   ├─ RecoveryManager.validate_state(ctx) → issues
   │     └─ non-empty → structured error ("state corrupt: ...")
   ├─ ctx.paused = False
   ├─ auto-fill prior producer stage_results (LINEAR_PATH[:idx])
   ├─ EV_WORKFLOW_RESUMED emit
   └─ _drive(ctx, is_resume=True)  → re-enter at ctx.current_state
```

```
DebateEngine._load_build_state(workspace_id)  # Phase 2 implementation resume
   ├─ read <workspace>/build_state.json → {} if absent/corrupt
   ├─ read prior repair_attempts + repair_log → resume from checkpoint
   └─ if repair_attempts >= MAX_REPAIR_ATTEMPTS AND status == "repair_exhausted"
        → re-emit human_required (never silently re-run)
```

```
WorkspaceDebateEngine.resume_workspace(workspace_id, follow_up)  # Phase 1 workspace resume
   ├─ load workspace (decisions.json, verified_decisions, verified_tradeoffs, research_evidence)
   ├─ workspace_impact_analyzer → affected disputes only
   ├─ re-debate ONLY affected subset
   └─ merge results back into workspace; persist
```

End of Batch 10. Next batch: §10 recovery semantics + §11 artifact lifecycle.

---

## 10. Recovery semantics (cross-layer)

### 10.1 The "re-entrant at the recorded state" principle

Every layer's resume re-enters at exactly the recorded state. None of them
silently restart; none of them re-derive intent from absence.

| Layer | Resume authority | Recorded state source | Re-entry target |
|-------|------------------|------------------------|------------------|
| Phase 1 workspace (debate) | `WorkspaceDebateEngine.resume_workspace` | `<workspace>/decisions.json` + `database/workspaces.db` | affected-dispute subset (via `workspace_impact_analyzer`) |
| Phase 2 implementation | `implementation_executor._load_build_state` | `<workspace>/build_state.json` | the recorded `repair_attempts` / `repair_log` checkpoint |
| Phase 2 execution context | `StateManager.load` | `<workspace>/implementation/execution_context.json` | the recorded `current_node` + `current_attempt` |
| Hardening build | `resume.resume_build` / `resume_execution` / `resume_validation` | `<workspace>/build_state.json` `lifecycle_stage` (maps legacy `status` when absent) | the recorded lifecycle state |
| Phase 4 workflow | `RecoveryManager.load_state` + `NativeWorkflowOrchestrator.resume` | `<workspace>/workflow/workflow_state.json` | the recorded `current_state` (auto-fills prior producers) |

### 10.2 The "no silent restart" invariant (cross-layer)

| Layer | Failure mode | Recovery behavior |
|-------|-------------|-------------------|
| Phase 1 debate | No checkpoint (`build_state.json` absent) | `WorkspaceDebateEngine` raises a structured error or creates a fresh workspace (per the README's `submit_workspace` flow — `resume_workspace` is the only path that requires an existing workspace). |
| Phase 2 implementation | `build_state.json` absent | `_load_build_state` returns `{}` — the loop reads `repair_attempts=0`, `repair_log=[]` and proceeds from zero. This is **not** silent restart in the Phase 4 sense because there's no "resume-from-checkpoint" being requested; a fresh `execute_implementation` call is the only entry path. |
| Phase 2 execution context | `execution_context.json` absent | `StateManager.load` returns `None`; the engine constructs a fresh `ExecutionContext` from caller args. |
| Hardening build | `lifecycle_stage` absent | `current_lifecycle` falls back to `lifecycle_stage_from_status(legacy_status)`; the ~30 legacy ad-hoc status strings all map to a lifecycle state — **nothing bypasses the lifecycle**. When there is no `build_state.json` at all, the workspace is treated as `NEW`. |
| Phase 4 workflow | `workflow_state.json` absent or corrupt | `resume` returns `{"success": False, "error": "no checkpoint to resume from (state absent or corrupt)"}`. **NEVER starts fresh.** |

### 10.3 "No silent termination" (cross-layer)

| Layer | When failure occurs | What happens |
|-------|---------------------|---------------|
| Phase 1 debate | Verifier hard-failure (`v_score < 90 AND unresolved > 0`) | Saves `[FAILED VERIFICATION]` to `answer_memory`; returns `{"_failed": True, "status": "verification_failed"}`. |
| Phase 1 debate | Research budget exhausted | Pre-emitted `HUMAN_REQUIRED` with `reason_code="research_budget_exhausted"`; surfaces via `human_decision_store`; build pipeline BLOCKS. |
| Phase 2 implementation | Agent dispatch fails | `write_build_state(workspace_id, "agent_failed", ...)` + return `ImplementationResult(success=False, error="Agent failed: ...")`. |
| Phase 2 implementation | Agent success but no artefact | `write_build_state(workspace_id, "agent_no_artefact", ...)` + structured error return. |
| Phase 2 implementation | Repair loop exhausts | `_emit_human_required` + `write_build_state(workspace_id, "repair_exhausted", human_required=True, ...)` + structured return. |
| Hardening lifecycle | Illegal transition | `BuildError(kind=LifecycleError, recoverable=False)` raised by `lifecycle.transition`. |
| Hardening validation | `halt_on_failure=True` | `BuildError(kind=ValidationError, recoverable=True)` raised by the dispatch. |
| Phase 4 policy | Any refusal | `PolicyDecision(allowed=False, action="skip"|"pause"|"fail", reason=...)` — the orchestrator logs it, emits the event, terminates with `final_state=S_FAILED`. |

### 10.4 Corrupted-state detection

| Layer | Detector | Pass behavior |
|-------|----------|---------------|
| Phase 4 | `RecoveryManager.detect_corruption(workspace_id)` | Parses `workflow/workflow_state.json` and `workflow/workflow_record.json`; appends `"<fname>: corrupt JSON (<exc>)"` on `JSONDecodeError`. Missing files are skipped (no false positive); missing `workflow/` dir → `"workflow/ dir missing"`. |
| Hardening | `validation.validate("manifest", manifest, workspace_id=ws)` | 10 rules: `manifest_present`, `manifest_version`, `lifecycle_consistent`, `artifact_exists`, `checksum_match`, `plan_id_match`, `plan_matches_spec`, `workspace_id_match` (FATAL), `validation_artifact_exists`, `tests_artifact_exists`, `implementation_artifact_exists`. |
| Phase 1 | `answer_memory.get_debate(debate_id)` returns `None` if absent; JSON-TEXT column preserves `verification_result` round-trip (`test_verification_result_preserved_verbatim`). |

---

## 11. Artifact lifecycle (cross-layer)

The full asset graph across every layer. Each stage names the artifact,
its writer, its location, and the consumer of the next stage.

### 11.1 Layer 1 — research → debate → verification → synthesis

| Stage | Artifact | Writer | Location |
|-------|----------|--------|----------|
| pre-debate research | per-topic evidence blob | `verification_research_v2.research_claim` (via `research_fn` seam) | in-memory, then persisted into the debate result (`verification_result.research_evidence`) |
| debate | positions (`gpt_current`, `gemini_current`) | `DebateEngine._debate_dispute` round-by-round | in `Dispute` dataclass, then persisted via `answer_memory.save_debate` |
| verification | `verification_result` dict (resolved/equivalent/unresolved claims, score, research evidence, audit fields) | `verification_engine.verify_all_disputes` | `answer_memory.db` `debates.verification_result` JSON-TEXT column; round-trips verbatim |
| verification | `governance_context` dict | `DebateEngine._run_verification` | persisted into `validated_disputes` in `answer_memory`; also drives the HUMAN_REQUIRED queue |
| synthesis | `synthesized_answer` (markdown) | `synthesis_engine.synthesize_answer` (Path deterministic by default) | `answer_memory.db` `debates.synthesized_answer`; also written to `<workspace>/synthesized_answer.md` when a workspace is in play |
| human requirement | `unified_human_decisions` row | `human_decision_store.save_pending` (via `_enqueue_human_required`) | `database/workspaces.db.unified_human_decisions` |
| memory-rich answer | `verified_decisions` (legacy audit-debate path) | `human_decision_queue` + `Workspace.add_verified_decision` | `database/workspaces.db.workspaces.verified_decisions` JSON-TEXT |

### 11.2 Layer 2 — implementation → validation → repair

| Stage | Artifact | Writer | Location |
|-------|----------|--------|----------|
| execution decision | `execution_decision.json` payload | `execution_decision_engine.ExecutionPlanner.persist` | `<workspace>/execution/execution_decision.json`, `execution_plan.json`, `execution_reason.json`, `execution_state.json` |
| build spec | `implementation_plan.md` (markdown) | `build_spec_engine.store_implementation_spec` | `<workspace>/implementation_plan.md` |
| execution plan (Hardening) | `execution_plan.json` envelope | `execution_planner.store_execution_plan` | `<workspace>/execution_plan.json` |
| implementation context | `execution_context.json` | `StateManager.save` | `<workspace>/implementation/` |
| engine state | `engine_state.json` | `StateManager.save` | `<workspace>/implementation/engine_state.json` |
| node history | `node_history.json` | `StateManager.save` | `<workspace>/implementation/node_history.json` |
| event log | `event_log.json` | EventBus recorder | `<workspace>/implementation/event_log.json` |
| checkpoint | `checkpoint.json` | `CheckpointManager.save_checkpoint` | `<workspace>/implementation/checkpoint.json` |
| execution record | `execution_record.json` | `ExecutionRecorder.persist` | `<workspace>/implementation/execution_record.json` |
| build result | `build_result.json` | `NativeImplementationEngine.execute` | `<workspace>/implementation/build_result.json` |
| build state | `build_state.json` (legacy ad-hoc status + parallel `lifecycle_stage`) | `build_workspace.write_build_state` | `<workspace>/build_state.json` |
| validation report | `validation_report.json` | `NativeValidationEngine.validate` | `<workspace>/validation/validation_report.json` |
| quality report | `quality_report.json` | `NativeValidationEngine.validate` | `<workspace>/validation/quality_report.json` |
| repair history | `repair_history.json` | `implementation_executor._execute_with_repair_loop` (via `write_build_state`) | `<workspace>/validation/repair_history.json` |
| source tree | project files | `implementation_executor.execute_implementation` (via `agent_fn`) | `<workspace>/src/...` |
| tests | test files | same as source | `<workspace>/tests/...` |

### 11.3 Layer 3 — planner → scheduler → workspace knowledge

| Stage | Artifact | Writer | Location |
|-------|----------|--------|----------|
| planner metadata | `project.json` | `project_planner.save_execution_graph` | `<workspace>/project.json` |
| execution graph | `execution_graph.json` | `project_planner.save_execution_graph` | `<workspace>/planner/execution_graph.json` |
| research output | `<package_id>.md` | `workspace_knowledge.save_artifact` | `<workspace>/research/<package_id>.md` |
| verified decisions | `<package_id>.json` | `workspace_knowledge.save_artifact` (validates JSON) | `<workspace>/verified/<package_id>.json` |
| architecture output | `<package_id>.md` | `workspace_knowledge.save_artifact` | `<workspace>/architecture/<package_id>.md` |
| implementation output | `<package_id>.md` | `workspace_knowledge.save_artifact` | `<workspace>/implementation/<package_id>.md` (also `<workspace>/implementation_plan.md` from `build_spec_engine`) |
| answer | `answer.json` | `build_pipeline.resurrect_answer` | `<workspace>/answer.json` (the `decisions.json` in older commits — the canonical decision dump) |
| decisions | `decisions.json` | `build_pipeline._resolve_execution_decision` | `<workspace>/decisions.json` (the SOLE source of truth consumed by `execution_planner.plan_from_workspace`) |

### 11.4 Layer 4 — Hardening (architecture_hardening.md)

| Stage | Artifact | Writer | Location |
|-------|----------|--------|----------|
| typed Artifact envelopes | `<artifact_id>.json` | `ArtifactStore.put` | `<workspace>/artifacts/<artifact_id>.json` |
| artifact index | `index.json` | `ArtifactStore` | `<workspace>/artifacts/index.json` |
| build manifest | `manifest.json` | `manifest.store_manifest` / `_persist_build_manifest` | `<workspace>/manifest.json` (references-only) |

### 11.5 Layer 5 — Phase 4 (workflow_orchestration.md)

| Stage | Artifact | Writer | Location |
|-------|----------|--------|----------|
| workflow state | `workflow_state.json` | `RecoveryManager.save_state` | `<workspace>/workflow/workflow_state.json` |
| workflow record | `workflow_record.json` | `_persist_artifacts` | `<workspace>/workflow/workflow_record.json` |
| workflow events | `workflow_events.json` | `_persist_artifacts` | `<workspace>/workflow/workflow_events.json` |
| workflow metrics | `workflow_metrics.json` | `_persist_artifacts` | `<workspace>/workflow/workflow_metrics.json` |
| workflow manifest | `workflow_manifest.json` | `_persist_artifacts` | `<workspace>/workflow/workflow_manifest.json` |
| artifact manifest | `artifact_manifest.json` | `ArtifactManager` via `_persist_artifacts` | `<workspace>/workflow/artifact_manifest.json` |
| benchmark results | `benchmark_results.json` | `BenchmarkEngine.persist` (default `{results: []}` if never run) | `<workspace>/workflow/benchmark_results.json` |
| stress results | `stress_results.json` | `StressTestEngine.persist` OR `FaultInjectionEngine.persist` (disambiguated by `type`) | `<workspace>/workflow/stress_results.json` |
| packaging outputs (11) | see §`workflow_orchestration.md` §8.2 | `PackagingEngine.package` | `<workspace>/package/` (research.md, architecture.md, implementation_spec.md, implementation/, validation_report.json, quality_report.json, repair_history.json, execution_record.json, workflow_record.json, manifest.json) |

### 11.6 The full asset graph (consumer chain)

A consumer reading "what did this workflow produce and where is it" reads
in increasing specificity:

```
workspace_state.json  → final state (Phase 4 recovery)
  ↓
workflow_manifest.json → which stages ran + terminal state (Phase 4)
  ↓
artifact_manifest.json → every file the stages produced + sha256 (Phase 4)
  ↓
manifest.json → build's completed-build references (Hardening)
  ↓
package/manifest.json → the sealed, delivered bundle (Phase 4 packaging)
```

Decision-model reverse-trace (provenance):

```
FinalBuildArtifact
  → dependencies[: TestArtifact, ImplementationArtifact, ValidationArtifact, ExecutionPlanArtifact]
  → ExecutionPlanArtifact.dependencies[: BuildSpecArtifact]
  → BuildSpecArtifact.dependencies[: VerificationArtifact, DecisionArtifact]
  → VerificationArtifact  → decisions.json (L3) → verification_result (L1)
  → DecisionArtifact       → decisions.json (L3) → decision_model.decision_records (L1)
```

Every downstream artifact references its upstreams via the Hardening's
`dependencies` tuple (artifact id list). An audit can walk from
`FinalBuildArtifact` back to `VerificationArtifact` AND back to
`answer_memory.db.verification_result`.

### 11.7 Reproducibility invariants

| Invariant | What it means | What breaks it |
|-----------|---------------|----------------|
| Content-addressing | Hardening's `Artifact.id = uuid5("<kind>:<sha256>")` — same payload → same id. Re-storing an identical payload is a no-op. | Non-deterministic payload JSON (sort_keys=False). |
| Byte-for-byte packaging | `PackagingEngine.package` produces byte-identical outputs on re-run for the same workspace contents. | Non-deterministic ordering of dict iteration; timestamps inside file content (timestamps stay in registered_at metadata, not in file content). |
| Plan id determinism | `execution_planner.plan(spec)` is deterministic given the spec; `test_plan_is_deterministic` pins it. | LLM call inside the planner; non-deterministic dict iteration. |
| Spec determinism | `build_spec_engine.build_spec(debate_fn=None)` (Path 3) is deterministic given `source_result`. | LLM in the rendering path. |
| Synthesis determinism | `synthesize_answer(narrative_fn=None)` returns byte-identical output across calls given the same inputs. | LLM on the default path. |
| Decision-model identity | `decision_records(verification_result)` is pure; same input → same output. | I/O, LLM, randomness inside the model. |

End of Batch 11. Next batch: §12 PolicyEngine rules (cross-layer).

---

## 12. PolicyEngine rules (cross-layer)

There are TWO policy authorities in Project Sol0mon. They compose, not
overlap:

1. **Layer 4 build-level execution decision gate** — the
   `execution_decision_engine` + `execution_decision` V2 classifier +
   `execution_authority` resolve ladder. It controls WHICH build stages
   the project may run (research / architecture / implementation). It
   runs ONCE at the start of `run_build_pipeline`, persists its outcome
   through `verification_result`, and is the authoritative gate inside
   `execute_build_spec`.

2. **Layer 5 workflow-level PolicyEngine** — the `PolicyEngine` from
   Phase 4. It controls WHICH workflow states may run, when to pause, when
   to fail, attempt caps, runtime/size budgets. It runs INSIDE every
   `NativeWorkflowOrchestrator._drive` loop iteration, before each stage
   attempt.

### 12.1 The build-level gate — `execution_decision_engine.decide`

#### 12.1.1 Precedence ladder (highest wins; never reverses)

1. **User Override** (`user_override` param or `mode_select_fn` returning
   the chosen mode) — confidence 100.
2. **User Prompt Intent** (`detect_prompt_intent(prompt)` →
   `intent_to_mode`) — confidence 99 for `full_build`, 95 otherwise.
3. **Execution Policy** (`DEFAULT_POLICY` + the verifier's emitted
   recommendation) — confidence 90.
4. **Planner Recommendation** (`planner_output.work_packages` types →
   mode by rule of §6.12.3) — confidence 85.
5. **Verification Recommendation** (`verification_mode(verification_result)`)
   — confidence 70.
6. **Research Heuristics** (floor; `RESEARCH_ONLY`) — confidence 60.

If `verification_is_hard_block(verification_result)` (`STOP_UNRESOLVED_DISPUTES`)
→ overrides everything to `RESEARCH_ONLY` with
`block_status="unresolved_disputes"` and `source=SOURCE_POLICY`.
Otherwise user override (Rule 6 in the docstring) wins.

#### 12.1.2 Effective decision, blocked status

`decide` returns `ExecutionDecision`:
- `execution_mode` — the chosen mode
- `reason` — human-readable
- `confidence` — per the source ladder
- `source` — the winning `SOURCE_*`
- `stages` — `list[Stage]` of stages the mode will run
- `policy_decision` — the audit (a dict of layer→candidate)
- `recommended_mode`, `selected_mode`, `override_used` — the override audit
- `blocked`, `block_status` — the gate's view

`is_blocked_decision(decision)` returns `True` iff `BLOCKED_DECISIONS`
contains the decision (= `RESEARCH_ONLY`, `ARCHITECTURE_ONLY`, or
`STOP_UNRESOLVED_DISPUTES`). `STOP_HUMAN_REQUIRED` is INTENTIONALLY
non-blocking — it defers to the pending-decision gate.

`block_status(decision)` returns `_BLOCK_STATUS[decision]` —
`"research_complete"`, `"architecture_complete"`,
`"unresolved_disputes"`, `"human_required"` (for
`STOP_HUMAN_REQUIRED` even though it's not blocked), or `""` for
`IMPLEMENTATION_REQUIRED`.

#### 12.1.3 The five guarantees

| Rule | What happens |
|------|--------------|
| Rule 1 — Verification is a recommendation, not the decision | `verification_mode` returns a non-binding mode; only `STOP_UNRESOLVED_DISPUTES` is hard-block. |
| Rule 2 — User override rides the live gate | `apply_mode_override` writes the override to `build_state.json`; `_resolve_execution_decision` consults `load_mode_selection` FIRST on resume. |
| Rule 3 — Verification success never terminates execution | A successful verification alone doesn't force a stop; the user's intent controls. |
| Rule 4 — Research signals never override explicit user intent | A "build it" prompt with research-only verification still goes to BUILD ONLY (or BUILD_AND_VALIDATE if "test" is also present). |
| Rule 5 — Hard governance blocks win | `STOP_UNRESOLVED_DISPUTES` overrides anything with confidence 90 (the policy layer). |
| Rule 6 — Override no-op | An override mapping to the SAME depth as the recommendation is NOT a real override; lower layers speak (eliminates false conflicts). |

#### 12.1.4 The non-negotiable abstention contract (recap of §4.9)

Only `IMPLEMENTATION_REQUIRED` and `STOP_HUMAN_REQUIRED` may reach the
build pipeline's spec step; the rest are hard stops. The verifier's emit
is a recommendation; only `STOP_UNRESOLVED_DISPUTES` retains hard-stop
authority.

### 12.2 The workflow-level PolicyEngine (Phase 4)

The default policy:

```python
DEFAULT_POLICY = {
    "max_attempts":           50,
    "max_runtime_seconds":   None,
    "max_workspace_bytes":   None,
    "stage_gates": {
        S_RESEARCH:           (),
        S_DEBATE:              (S_RESEARCH,),
        S_CONSENSUS:           (S_DEBATE,),
        S_ARCHITECTURE:        (S_CONSENSUS,),
        S_IMPLEMENTATION:      (S_ARCHITECTURE,),
        S_VALIDATION:          (S_IMPLEMENTATION,),
        S_QUALITY_GATE:        (S_VALIDATION,),
        S_PACKAGING:           (S_QUALITY_GATE,),
        S_DELIVERY:            (S_PACKAGING,),
    },
    "pause_before":          (),
    "skip_stages":           (),
}
```

The nine-rule evaluate algorithm (in evaluation order; first that fires
returns its `PolicyDecision`):

| # | Rule | Condition | Decision | Severity |
|---|------|-----------|----------|----------|
| 1 | `skip_stages` | `next_state in policy["skip_stages"]` | `allowed=False, action="skip"` | info |
| 2 | terminal short-circuit | `next_state in TERMINAL_STATES` | `allowed=True, action="continue"` | info |
| 3 | cancelled | `ctx.cancelled` | `allowed=False, action="fail"` | error |
| 4 | stage gating | for `producer` in `policy["stage_gates"][next_state]`: `ctx.stage_results.get(producer, {}).get("success") is not True` | `allowed=False, action="fail"` | error |
| 5 | runtime budget | `policy["max_runtime_seconds"]` set AND `(now - ctx.started_at) > max_runtime_seconds` | `allowed=False, action="fail"` | error |
| 6 | workspace size budget | `policy["max_workspace_bytes"]` set AND `sum(file sizes under <ws>) > max_workspace_bytes` (best-effort; `OSError` swallowed) | `allowed=False, action="fail"` | error |
| 7 | attempt budget | `policy["max_attempts"]` set AND `ctx.attempt_count >= policy["max_attempts"]` | `allowed=False, action="fail"` | error |
| 8 | mandatory pause | `next_state in policy["pause_before"]` | `allowed=True, action="pause"` | info |
| 9 | default allow | none of the above fired | `allowed=True, action="continue"` | info |

### 12.3 How the two policies compose

| Concern | Build-level gate (L4) | Workflow PolicyEngine (L5) |
|---------|------------------------|-----------------------------|
| Decides | Whether to enter the build (RESEARCH/A/B/IMPL) AND the depth (RESEARCH_ONLY → FULL_BUILD, CUSTOM) | Whether a given workflow state may run, attempt caps, runtime/size budgets, mandatory pauses, skips |
| When | ONCE at the start of `run_build_pipeline`; persisted into `verification_result` and `build_state.json` | BEFORE every workflow stage attempt in Phase 4's `_drive` loop |
| Authority | `execution_decision_engine.decide` (V3) — single authority for the build | `PolicyEngine.evaluate` (Phase 4) — single authority for the workflow |
| Failure | Hard stop at the gate (`blocked=True`); structured return; no spec generated | Same — `PolicyDecision(allowed=False, action="fail")`; structured return |
| Recovery | User override (`mode_select_fn` / persisted selection / `apply_mode_override`); user_resolve (`resolve_human_requirement`) | Resume clears `paused`; skip-rules apply per-state; attempt count decrements on resume |
| Audit | `policy_decision` dict + the `[AUTHORITY] EFFECTIVE:` log line | `ctx.policy_decisions` list + the `EV_POLICY_DECISION` event |

A production IMPLEMENTATION workflow stage internally calls
`lifecycle.advance_lifecycle` (L4); the Phase 4 PolicyEngine itself never
touches the build lifecycle. The two policies are layered: the build-level
gate decides IF/WHERE the build starts; the workflow-level PolicyEngine
decides IF the next workflow state may run. They never fire on the same
question.

### 12.4 The attempt-budget architectural symmetry

| Layer | Cap | What it bounds |
|-------|-----|----------------|
| Phase 2 implementation | `MAX_REPAIR_ATTEMPTS=3` (`implementation_executor`) | Per-repair retries inside one `execute_implementation` call |
| Hardening validation | `validate_tests` once; `halt_on_failure` stops | The validation pass; not retried |
| Build level decision | `attempt_count` defaulted at 0; there is no execution-wide cap in the build-level gate | n/a |
| Workflow level (Phase 4) | `PolicyEngine.max_attempts=50` (default) | Workflow-wide stage-attempts in one `run` |

The two per-repair caps (Phase 2 + Phase 4) compose: either being hit
terminates the workflow deterministically with a structured FAILED
result. The `RetryController` (Phase 3.1) handles per-repair retries; the
`PolicyEngine` caps the *workflow-wide* attempts — they don't overlap.

End of Batch 12. Next batch: §13–§15 benchmark + fault injection + stress test frameworks.

---

## 13. Benchmark framework

Fully specified in §10 of
[`docs/workflow_orchestration.md`](workflow_orchestration.md). Recap:

### 13.1 `BenchmarkEngine` (Phase 4)

```python
BenchmarkEngine.DEFAULT_WORKLOADS = {
    "small":  {"prompt": "Build a hello-world module", "files": [...]},
    "medium": {"prompt": "Build a 3-component service with tests", "files": [...]},
    "large":  {"prompt": "Build a 10-component platform with full tests", "files": [...]},
}

@dataclass
class BenchmarkResult:
    workload: str          # "small" | "medium" | "large"
    workflow_id: str
    success: bool
    duration_seconds: float    # monotonic wall clock
    stage_count: int           # len(stages_executed)
    artifact_count: int
    final_state: str
    error: str = ""
```

Contract: pure stdlib (`time.monotonic` + `build_workspace` lazy-imported);
each run mints a fresh orchestrator (re-entrant); 3 default workloads; same
inputs → same final state (`test_benchmark_determinism_same_workload_same_state`).

### 13.2 Cross-layer additions

A production benchmark harness wires the `BenchmarkEngine` with a real
production orchestrator, populated with the actual Layer 1-3 stage
implementations. The seams:

```python
BenchmarkEngine(
    workloads={"<name>": {"prompt": ..., "files": [...]}},
    orchestrator_factory=lambda: NativeWorkflowOrchestrator(stages=production_registry()),
    build_workspace_id=lambda spec: build_workspace.create_workspace(source_question=spec["prompt"]),
)
```

The benchmark output is *the regression-comparison surface*: a run
records `[workload, workflow_id, success, duration_seconds, stage_count,
artifact_count, final_state, error]` so a baseline vs. a candidate
implementation can be diffed. The persisted
`<workspace>/workflow/benchmark_results.json` survives across runs for
trend analysis.

### 13.3 Acceptance contract (recap)

| Property | Test |
|----------|------|
| Three workloads run | `test_benchmark_runs_three_workloads` |
| Each succeeds deterministically with no-op stages | `test_benchmark_runs_three_workloads` |
| Same workload twice → same final state | `test_benchmark_determinism_same_workload_same_state` |

---

## 14. Fault injection framework

Fully specified in §11 of
[`docs/workflow_orchestration.md`](workflow_orchestration.md). Recap:

### 14.1 `FaultInjectionEngine` (Phase 4) + default injectors

```python
FaultInjectionEngine._default_injectors() = {
    "corrupt_workflow_state":      _inject_corrupt_workflow_state,
    "delete_workflow_state":      _inject_delete_workflow_state,
    "malformed_user_prompt":      _inject_malformed_user_prompt,
    "interrupt_before_packaging":  _inject_interrupt_before_packaging,
}

@dataclass
class FaultInjectionResult:
    fault: str
    workspace_id: str
    recovered: bool       # recover()["recoverable"] OR final_state != FAILED
    final_state: str
    recovery_issues: list
    error: str = ""
```

Contract: every injector mutates the workspace, then `orch.recover` +
`orch.resume` is run; the result captures `recovered` AND a non-empty
`error` OR `recovered=True` (no silent outcome).

### 14.2 Cross-layer fault surface

Each fault maps to a specific cross-layer recovery path:

| Fault | Phase 4 detection | Layer below that fires | What happens |
|-------|--------------------|------------------------|--------------|
| `corrupt_workflow_state` | `RecoveryManager.detect_corruption` parses JSON → `JSONDecodeError` | Phase 4 `RecoveryManager.load_state` returns `None` (corrupt JSON == absent) | `resume` returns `{"success": False, "error": "no checkpoint to resume from ..."}` |
| `delete_workflow_state` | `load_state` returns `None` | Phase 4 | Same as corrupt — structured error |
| `malformed_user_prompt` | `NativeWorkflowOrchestrator._validate_inputs` | Phase 4 entry-validation | `run` returns `{"success": False, "error": "user_prompt is required and non-empty"}` BEFORE any stage runs |
| `interrupt_before_packaging` | seeded `WorkflowContext(current_state=S_PAUSED, paused=True)` | Phase 4 + Phase 2 build-state (the interrupted stage's own `build_state.json` checkpoint) | `resume` clears `paused`, re-enters at `S_PAUSED`, the drive loop sees `ctx.paused`, terminates, sets `S_PAUSED`; a second `resume` proceeds |

### 14.3 Extension surface

A production fault-injection harness adds injectors for cross-layer faults:

```python
FaultInjectionEngine(injectors={
    **FaultInjectionEngine()._default_injectors(),
    "provider_router_all_failed":   _inject_provider_router_all_failed,
    "debate_engine_verification_failed": _inject_verification_failed,
    "implementation_repair_exhausted": _inject_repair_exhausted,
    "human_decision_unresolved":    _inject_human_unresolved,
    "build_spec_incomplete":        _inject_incomplete_spec,
})
```

Each of these maps to a specific documented failure mode in the Layer
1-3 contracts (see §10.3). The harness verifies that the cross-layer
recovery contract produces the right structured outcome (e.g.
`HUMAN_REQUIRED` row in `unified_human_decisions`, `blocked_human_decisions`
in `build_state.json`, etc.).

### 14.4 Acceptance contract (recap)

| Property | Test |
|----------|------|
| Every default injector produces a structured result | `test_fault_injection_runs_every_injector` |
| Corrupt state detected | `test_fault_injection_corrupt_state_detected` |
| Deleted state → structured error | `test_fault_injection_delete_state_returns_no_checkpoint_error` |

---

## 15. Stress-testing framework

Fully specified in §12 of
[`docs/workflow_orchestration.md`](workflow_orchestration.md). Recap:

### 15.1 `StressTestEngine.run_concurrent` (Phase 4)

```python
def run_concurrent(*, count: int = 5) -> StressResult

@dataclass
class StressResult:
    name: str                       # f"concurrent_{count}"
    workflow_count: int
    all_succeeded: bool
    total_duration_seconds: float
    final_states: list
    error: str = ""
```

Contract: each thread builds a fresh orchestrator + workspace; re-entrant;
no shared mutable state across workspace boundaries.

### 15.2 Cross-layer stress surface

| Stress vector | What it stresses | Cross-layer contract |
|---------------|-------------------|----------------------|
| Concurrent workflows (Phase 4) | Workspaces do not share mutable state — every orchestrator + recorder + state machine is per-workspace | `test_concurrent_workflows_distinct_workspaces` (5 distinct wids) |
| Retry exhaustion (Phase 4 PolicyEngine `max_attempts=1`) | `PolicyEngine` enforces the cap deterministically | `test_policy_engine_attempt_cap_blocks_after_exhaustion` |
| Repair loop exhaustion (L2 `MAX_REPAIR_ATTEMPTS=3`) | `_emit_human_required` is reached; the human_decision_store row is persisted | `tests/test_repair_loop.py` (the exhaust case) |
| Verification hard-failure (`v_score < 90 AND unresolved > 0`) | `[FAILED VERIFICATION]` row written to `answer_memory`; structured return | `tests/test_decision_model_persistence.py` and the verifier tests |
| Provider router all-tiers-failed | `RuntimeError("All providers failed")` raised; the caller surfaces the error | (no dedicated test; relies on the per-tier client tests) |
| Human-decision queue pile-up | `human_decision_queue.get_pending_count` tracks each workspace; `resolve_human_requirement` clears when count drops to 0 → `CONSENSUS_REACHED` | `tests/test_human_decision_manager.py`, `tests/test_human_escalation_e2e.py` |

### 15.3 Extension surface

```python
StressTestEngine(
    orchestrator_factory=lambda: NativeWorkflowOrchestrator(stages=production_registry()),
    workspace_id_factory=lambda i: build_workspace.create_workspace(source_question=f"stress-{i}"),
)

ste = StressTestEngine(...)
ste.run_concurrent(count=8)        # default 5; production goes higher with a stateless pipeline_fn
```

End of Batch 13. Next batch: §16 comprehensive testing matrix.

---

## 16. Comprehensive testing matrix

`run_all_tests.py` is the consolidated runner. Per-layer regression tests
live in `tests/test_<layer>.py`. Every contract named in §6-§15 of this
spec is pinned by a test named here.

### 16.1 Layer 1 — Consensus

#### 16.1.1 Decision Model (`decision_model.py`)

| Test | Assertion |
|------|-----------|
| `test_decision_model_canonical.py::test_rejected_option_is_recognized_for_argues_against_phrasing` | `"postgresql" in rec["rejected_options"]` for answer-156 finding |
| `test_decision_model_canonical.py::test_rejected_option_does_not_leak_into_winner_tokens` | `"postgresql" not in rec["winner_tokens"]` |
| `test_decision_model_canonical.py::test_actual_winner_is_present_in_winner_tokens` | `"sqlite" in rec["winner_tokens"]` |
| `test_decision_model_canonical.py::test_answer156_no_postgresql_in_any_winner_tokens` | across EVERY record `postgresql` not in `winner_tokens`; record count is 2 |
| `test_phase1_root_cause_regression.py::test_decision_model_does_not_import_decision_renderer` | `decision_renderer` MUST NOT be in `sys.modules` after importing `decision_model` |
| `test_phase1_root_cause_regression.py::test_decision_records_expose_canonical_contract` | `set(r.keys()) == set(FIELDS)` |
| `test_phase1_root_cause_regression.py::test_winner_text_uses_winner_portion_only_never_dispute_title` | `winner_text` excludes "postgresql" for the 156 claim |
| `test_phase1_root_cause_regression.py::test_validator_flags_narrative_recommending_postgresql_for_156` | defense-in-depth validation layer catches a future regression |
| `test_phase1_root_cause_regression.py::test_render_architecture_does_not_emit_postgresql_for_156` | `render_*` exclude "postgresql" for the 156 fixture |
| `test_decision_model_persistence.py::test_seven_resolved_disputes_survive_into_resolved_decisions` | 7 decisions survive round-trip; each carries non-empty decision/winner/rationale/finding |
| `test_decision_model_persistence.py::test_verification_result_preserved_verbatim` | `verification_result` round-trips untouched |

#### 16.1.2 Synthesis + Determinism

| Test | Assertion |
|------|-----------|
| `test_deterministic_synthesis.py::test_determinism_same_inputs_identical_output` | `a == b == c` |
| `test_deterministic_synthesis.py::test_determinism_independent_of_consensus_score_and_positions` | perturbing question/positions/consensus yields identical output |
| `test_deterministic_synthesis.py::test_rejected_options_never_recommended` | `validate_synthesis_consistency == []` on default deterministic answer |
| `test_deterministic_synthesis.py::test_no_model_call_on_default_path` | monkeypatched `generate` raises if called; `synthesize_answer(narrative_fn=None)` completes |
| `test_deterministic_synthesis.py::test_seven_sections_present_in_canonical_order` | the seven headers appear exactly once in canonical order |
| `test_deterministic_synthesis.py::test_debate_55_renders_seven_decisions_deterministically` | "Decision Count: 7"; each dispute + finding verbatim |
| `test_synthesis_consistency.py::test_synthesized_answer_flags_recommended_rejected_option` | recommending WebContainers/IndexedDB/FastAPI/CRDT triggers `# Contradiction Warning` |
| `test_synthesis_consistency.py::test_synthesize_answer_is_narrative_only_via_seam` | `narrative_fn` is the only seam |

#### 16.1.3 Verification Engine

| Test | Assertion |
|------|-----------|
| `test_research_verification_integration.py::test_requires_external_evidence_triggers_research` | `requires_external_evidence=True` triggers `research_fn` |
| `test_research_verification_integration.py::test_no_research_runs_when_evidence_sufficient` | `requires_external_evidence=False` skips research |
| `test_research_verification_integration.py::test_workspace_resume_reloads_evidence` | `prior_research_evidence` produces Phase 5 reuse |
| `test_research_verification_integration.py::test_exhausted_research_becomes_human_required` | `research_escalation_count >= max` emits `HUMAN_REQUIRED` `governance_outcome` |
| `test_research_verification_integration.py::test_research_evidence_survives_answer_persistence` | round-trip via `answer_memory` works |

#### 16.1.4 Governance + Escalation

| Test | Assertion |
|------|-----------|
| `test_human_escalation_e2e.py::test_force_unresolved_dispute_produces_human_required` | forced verifier failure → OutcomeType.HUMAN_REQUIRED; aggregated into result.to_dict()["human_required"] of length 1 |
| `test_human_escalation_e2e.py::test_build_pipeline_blocks_implementation_until_resolved` | `execute_build_spec` returns `blocked_reason="pending_human_decisions"` |
| `test_human_escalation_e2e.py::test_resolve_unblocks_build_pipeline` | after `resolve_human_requirement`, `execute_build_spec` proceeds |
| `test_human_escalation_e2e.py::test_full_escalation_cycle_end_to_end` | full cycle: escalation → persistence → resolve → build resumes |

#### 16.1.5 Workspace follow-up

| Test | Assertion |
|------|-----------|
| `test_workspace_followup_verification.py::test_workspace_followup_verification_no_name_error` | `verify_all_disputes` reachable on follow-up; `validated_disputes` from pending only |
| `test_research_driven_debate.py::test_1_planner_runs` ... `test_7_follow_up_reuse` | seven numbered tests pinning pre-debate research injection, topic-keyed reuse, no-duplicate-calls, no model/browser calls, workspace persistence, follow-up reuse |

### 16.2 Layer 2 — Execution / Validation / Repair

#### 16.2.1 Execution Decision (V2)

| Test | Assertion |
|------|-----------|
| `test_execution_decision_v2.py::test_rung_6_human_required` | human_required list non-empty → `STOP_HUMAN_REQUIRED` |
| `test_execution_decision_v2.py::test_rung_5_unresolved` | unresolved_claims non-empty → `STOP_UNRESOLVED_DISPUTES` |
| `test_execution_decision_v2.py::test_rung_4_implementation_artifacts` | word-boundary IMPLEMENTATION_SIGNALS wins on a "Design …" prompt |
| `test_execution_decision_v2.py::test_rung_3_architecture_decisions` | ARCHITECTURE_SIGNALS win when no implementation signal |
| `test_execution_decision_v2.py::test_rung_2_research_signal` | RESEARCH_SIGNALS use the verifier text (resolved/evidence blob) |
| `test_execution_decision_v2.py::test_rung_1_prompt_fallback` | all-zero → `RESEARCH_ONLY` safe-default |

#### 16.2.2 Implementation Engine + Repair Loop

| Test | Assertion |
|------|-----------|
| `test_implementation_engine.py` (engine suite) | `NativeImplementationEngine.execute` succeeds with injected `coding_agent_fn`; `engine == "Native"`; `CodingAgentError` raised on silent-failure |
| `tests/test_repair_loop.py` | all loop states; exhaust → human-required; resume from `_load_build_state`; `_tests_passed` strengthened contract; artefact-presence gate |

#### 16.2.3 Validation Engine + Repair Memory

| Test | Assertion |
|------|-----------|
| `test_workspace_orchestrator.py::test_phase3_validation_engine_still_importable` (covers the L3 engine test too) | `NativeValidationEngine` with injected `build_fn`/`test_fns` succeeds; `engine == "NativeValidation"` |
| `test_phase31_repair_memory_still_importable` | `RepairMemory.record_attempt` + `is_blacklisted` works; `close()` succeeds |

### 16.3 Layer 3 — Integration

| Test | Assertion |
|------|-----------|
| `test_build_prompt_generator.py::test_build_prompt_forbids_rejected_architecture` | verified winner listed as Constraints (immutable); rejected option in reasoning only |
| `test_build_prompt_generator.py::test_decision_gaps_detects_spec_ignoring_verification` | `_spec_decision_gaps` flags a spec ignoring all verified decisions |
| `test_project_planner.py` | validate rejects each of the five error classes; `topological_order` stability; the resume-shortcut (`has_execution_graph`); `force=True`; `PlannerError` after exhausted retries |
| `test_pipeline_scheduler.py` | READY-only-execute; `BLOCKED` on `dependency_failed` / `no_open_implementation_decisions`; `require_implementation=True` override; `max_workers=1` sequential |
| `test_build_spec_pipeline.py` | Path 3 deterministic spec returns the structured dict; validate_spec_sections returns `[]` on a complete spec |
| `test_build_spec_artifact.py::test_phase3_build_spec_artifact` | the BuildSpecArtifact markdown output is byte-identical to the deterministic Path 3 rendering |
| `test_human_decision_manager.py` | `save_pending` + `resolve` + `pending` + `is_resolved` work against `unified_human_decisions` |
| `test_workspace_orchestrator.py` (legacy) | `submit_workspace` / `follow_up_workspace` / `resolve_human_requirement` / `get_workspace_status` |
| `test_answer_resurrection_pipeline.py` | `resurrect_answer` retrieves the verified decisions, verification_result, decisions survive round-trip |
| `test_workspace_flow.py` | happy-path workspace creation through packaging |

### 16.4 Layer 4 — Hardening (architecture_hardening.md)

| Test | Assertion |
|------|-----------|
| `test_lifecycle.py` (11 tests) | State transitions; illegal transitions raise `BuildError`; `lifecycle_stage_from_status` maps the ~30 legacy ad-hoc strings |
| `test_artifacts.py` (10 tests) | Artifact content-addressing; `with_validation` returns a new artifact (version bump); `ArtifactStore.put` idempotent |
| `test_validation.py` (23 tests) | All six validators + `validate_manifest` (10 rules); `halt_on_failure=True` raises; validation ordering (spec must pass before plan is trusted) |
| `test_execution_planner.py` (10 tests) | Plan determinism; rejected option never becomes a component task; empty spec still yields a non-empty plan; `ready()` progressive release |
| `test_resume.py` (14 tests) | Three resume entry points; manifest-aware reuse; skipped execution never moves lifecycle backwards |
| `test_consolidation.py` (25 tests) | `planner_fn` seam; persisted `ExecutionPlanArtifact`; `BuildManifest` references-only; task graph exports JSON/Mermaid/DOT; manifest validation rules; manifest-aware resume end-to-end |

### 16.5 Layer 5 — Phase 4 (workflow_orchestration.md)

| Section | Tests |
|---------|-------|
| §1 Complete workflow execution | `test_complete_workflow_reaches_completed`, `test_missing_workspace_returns_structured_error`, `test_missing_user_prompt_returns_structured_error` |
| §2 Workspace artifacts | `test_all_seven_workspace_artifacts_persisted`, `test_workflow_state_artifact_carries_resume_state`, `test_workflow_record_artifact_carries_events` |
| §3 Packaging outputs | `test_all_eleven_packaging_outputs_persisted`, `test_package_manifest_records_checksums` |
| §4 Replay | `test_replay_returns_all_events_in_order`, `test_timeline_returns_all_state_transitions`, `test_stage_timings_reconstructs_per_state` |
| §5 Recovery | `test_resume_continues_from_persisted_checkpoint`, `test_resume_no_state_returns_structured_error`, `test_resume_corrupt_state_returns_structured_error`, `test_cancel_marks_context_cancelled`, `test_cancel_returns_false_when_no_state`, `test_cancel_is_honored_on_resume` |
| §6 Recovery surfaces | `test_status_returns_unknown_when_no_state`, `test_status_reports_current_state`, `test_recover_returns_diagnostic`, `test_recover_returns_false_when_no_state`, `test_detect_corruption_flags_corrupt_json`, `test_detect_corruption_clear_when_clean` |
| §7 Event ordering | `test_event_log_artifact_carries_events_in_order`, `test_event_log_artifact_carries_artifact_events` |
| §8 Concurrent workflows | `test_concurrent_workflows_succeed_independently`, `test_concurrent_workflows_distinct_workspaces` |
| §9 Artifact integrity | `test_artifact_manager_records_sha256`, `test_artifact_manager_detects_missing_files`, `test_artifact_manager_verify_detects_checksum_mismatch`, `test_artifact_manager_reproducible_checksum` |
| §10 Benchmark | `test_benchmark_runs_three_workloads`, `test_benchmark_determinism_same_workload_same_state` |
| §11 Fault injection | `test_fault_injection_runs_every_injector`, `test_fault_injection_corrupt_state_detected`, `test_fault_injection_delete_state_returns_no_checkpoint_error` |
| §12 Stress | `test_stress_concurrent_completes_all_workflows` |
| §13 PolicyEngine | `test_policy_engine_stage_gate_blocks_when_producer_not_run`, `test_policy_engine_stage_gate_passes_when_producer_succeeded`, `test_policy_engine_attempt_cap_blocks_after_exhaustion`, `test_policy_engine_skip_stages_skipped`, `test_policy_engine_pause_before_yields_pause_decision`, `test_policy_engine_cancelled_workflow_blocks`, `test_policy_engine_terminal_states_allowed` |
| §14 State machine | `test_state_machine_validation_failed_goes_to_repair`, `test_state_machine_repair_succeeded_reenters_validation`, `test_state_machine_quality_gate_failed_goes_to_repair`, `test_state_machine_validation_succeeded_advances_to_quality_gate`, `test_state_machine_cancelled_goes_to_cancelled`, `test_state_machine_non_repair_failure_goes_to_failed` |
| §15 StageRegistry | `test_registry_rejects_default_name`, `test_registry_rejects_invalid_state_name`, `test_registry_registers_real_stage`, `test_default_registry_has_eleven_workflow_states` |
| §16 Configuration switch | `test_get_workflow_orchestrator_native_default`, `test_get_workflow_orchestrator_unknown_raises` |
| §17 Pure-stdlib at import | `test_no_forbidden_modules_transitively_imported` |
| §18 Backward compatibility | `test_phase1_execution_decision_engine_still_importable`, `test_phase2_implementation_engine_still_importable`, `test_phase3_validation_engine_still_importable`, `test_phase31_repair_memory_still_importable` |
| §19 Determinism | `test_deterministic_for_same_inputs` |
| §20 No silent failures | `test_every_failure_surfaces_an_error` |

### 16.6 Layer 4 ↔ Layer 5 composition

`run_all_tests.py` runs both layers' tests in order. The Phase 4
`test_phase1231_*_still_importable` tests pin the cross-layer boundary:
importing and using each Layer 1-3.1 component directly still succeeds
under Phase 4.

```
$ python3 run_all_tests.py
-- Integration Pipeline Tests --
  PASS tests/test_workflow_pipeline.py
  PASS tests/test_answer_resurrection_pipeline.py
  PASS tests/test_build_spec_pipeline.py
  PASS tests/test_implementation_pipeline.py
  PASS tests/test_build_answer_55_e2e.py
  PASS tests/test_build_answer_55_opencode_live_e2e.py
  PASS tests/test_resurrection_constraints.py
  PASS tests/test_human_synthesis.py
  PASS tests/test_lifecycle.py            (11)
  PASS tests/test_artifacts.py             (10)
  PASS tests/test_validation.py            (23)
  PASS tests/test_execution_planner.py      (10)
  PASS tests/test_resume.py                (14)
  PASS tests/test_consolidation.py         (25)
  PASS tests/test_workflow_orchestrator.py (63)
-- Module Tests (Phase 1-4) -- 6 PASS
RESULTS: 21+ passed, 0 failed (plus the 63-test Phase 4 suite)
```

End of Batch 14. Next batch: §17 success criteria + acceptance gates.

---

## 17. Success criteria and acceptance gates

### 17.1 The verification matrix (the implementation is complete only if…)

| "…the implementation is complete only if…" | Where | Pinned by |
|---|---|---|
| The Decision Model is the SOLE owner of decision interpretation; the SQLite/PostgreSQL rejected-option leak is impossible. | `decision_model.decision_records`; `decision_renderer._architecture_components`. | `test_decision_model_does_not_import_decision_renderer`; `test_answer156_no_postgresql_in_any_winner_tokens`; `test_render_architecture_does_not_emit_postgresql_for_156` |
| The decision/narrative sections are deterministic; same inputs → byte-identical output. | `decision_renderer.render_*`; `synthesis_engine.synthesize_answer(narrative_fn=None)`. | `test_determinism_same_inputs_identical_output`; `test_no_model_call_on_default_path`; `test_seven_sections_present_in_canonical_order` |
| A rejected-option recommendation in the narrative is caught and warned; the deterministic sections stand. | `synthesis_engine.validate_synthesis_consistency`. | `test_synthesized_answer_flags_recommended_rejected_option` |
| The execution decision gate is the authoritative terminator; only `IMPLEMENTATION_REQUIRED` / `STOP_HUMAN_REQUIRED` reach spec. | `execution_decision_engine.decide`; `execution_decision.is_blocked_decision`; `build_pipeline._resolve_execution_decision`; `execute_build_spec`. | `test_rung_6_human_required` ... `test_rung_1_prompt_fallback`; `test_full_escalation_cycle_end_to_end` |
| Research signals never override explicit user intent; `STOP_UNRESOLVED_DISPUTES` is the only hard block. | `execution_decision_engine.verification_is_hard_block`. | `test_execution_decision_v2.py::test_rung_*` |
| The repair loop never silently succeeds; artefact-presence + strengthened tests-passed. | `implementation_executor._execute_with_repair_loop`, `_tests_passed`. | `tests/test_repair_loop.py` |
| Repair exhaustion escalates to a human decision; never silent. | `implementation_executor._emit_human_required`. | `tests/test_repair_loop.py::exhaust` |
| Every lifecycle transition is explicit; illegal transitions raise `BuildError(kind=LifecycleError, recoverable=False)`. | `lifecycle.transition` + `BUILD_LIFECYCLE`. | `tests/test_lifecycle.py` (11) |
| Every artifact has provenance + content-addressing; the same payload → same id. | `ArtifactStore`; `Artifact.dependencies`; `index.json`. | `tests/test_artifacts.py` (10) |
| Every validation is centralized; an unknown stage raises `BuildError`. | `validation.validate(stage, artifact)` + `VALIDATORS` registry. | `tests/test_validation.py` (23) |
| The planner is deterministic; no LLM; rejected options never become component tasks. | `execution_planner.plan`. | `tests/test_execution_planner.py` (10) |
| Resume re-enters at the recorded state; absent/corrupt state is a structured error, never silent restart. | `resume.resume_build`; `RecoveryManager.load_state`; `WorkspaceDebateEngine.resume_workspace`; `_load_build_state`. | `tests/test_resume.py` (14); `test_resume_no_state_returns_structured_error`; `test_resume_corrupt_state_returns_structured_error` |
| Every non-COMPLETED terminal workflow state has a non-empty `error`. | `_drive` exit conditions. | `test_every_failure_surfaces_an_error` |
| The workflow state machine is the SOLE authority on `current_state`. | `WorkflowStateMachine.next_state`. | `test_state_machine_*` |
| The PolicyEngine is consulted before every stage and never silently terminates. | `_drive` calls `policy_engine.evaluate` at the top of every iteration. | `test_policy_engine_*` |
| Every artifact produced is registered with a reproducible sha256. | `ArtifactManager.register`. | `test_artifact_manager_records_sha256`; `test_artifact_manager_reproducible_checksum` |
| The 11 packaging outputs are produced on COMPLETED; byte-for-byte reproducible. | `PackagingEngine.package`. | `test_all_eleven_packaging_outputs_persisted`; `test_package_manifest_records_checksums` |
| Provider router failover follows the documented tier chain; non-failover conditions are terminal. | `provider_router.generate`; `_should_failover`. | `tests/test_cerebras_client.py`, `tests/test_cerebras_test.py`, the per-tier client tests |
| Pure-stdlib at import for Phase 4 + the Decision Model. | `workflow_orchestrator` + `decision_model`. | `test_no_forbidden_modules_transitively_imported`; `test_decision_model_does_not_import_decision_renderer` |
| Determinism — same inputs → same final state and stages_executed. | Phase 4 + the decision renderer + the planner + the spec engine. | `test_deterministic_for_same_inputs`; `test_benchmark_determinism_same_workload_same_state` |
| Concurrent workflows are re-entrant; no shared mutable state. | `StressTestEngine.run_concurrent`. | `test_concurrent_workflows_distinct_workspaces`; `test_concurrent_workflows_succeed_independently` |
| Every Phase 1-3.1 component still imports and works unchanged. | The Phase 4 + Hardening layers are additive. | `test_phase1_execution_decision_engine_still_importable`, `test_phase2_implementation_engine_still_importable`, `test_phase3_validation_engine_still_importable`, `test_phase31_repair_memory_still_importable` |

### 17.2 Acceptance gates (in evaluation order)

A change is acceptable only if:

1. **`python3 run_all_tests.py` passes.** The consolidated runner covers
   every layer; no skipped / xfailed tests (the suite is offline-only,
   with the pre-existing `>120s` SKIP in `test_suite.py` for live Cerebras
   being unrelated).
2. **`python3 workflow_orchestrator.py` prints
   `workflow_orchestrator self-check PASSED`.**
3. **`python3 decision_model.py` prints
   `decision_model self-check PASSED`** (the invariant self-check at the
   `__main__` block).
4. **The forbidden-modules probe returns `[]`** for `workflow_orchestrator`
   and `decision_model`. (Acceptance Gate §13.17 of the Phase 4 matrix +
   the Decision Model's `__main__` block.)
5. **The packaging outputs survive a re-run** — `PackagingEngine.package`
   produces the same `manifest_sha256` for the same workspace contents.
6. **Every new stage added passes `StageRegistry.register`'s validation.**
7. **Every new default workload / injector ships with a regression test.**
8. **No new speculative abstraction is introduced.** No ABCs, Protocol
   interfaces for one implementation, factory / strategy / repository /
   plugin / event-bus / service-locator / visitor / builder hierarchies,
   no AbstractFactory, no AbstractBuilder, no callback DSL, no
   runtime code-gen, no reflection. Callable seams + concrete classes
   only. (See §18.)
9. **No existing module is modified outside an additive `import` seam.**
   Verified by `git diff` showing only additions to existing files (the
   call-site of a new seam) and new files.

### 17.3 The non-negotiable invariants (the cross-layer contracts)

| Invariant | What breaks it (regression) |
|-----------|-----------------------------|
| Single authority per concern | A second component parsing `verification_result`; a second component writing `ctx.current_state`; etc. |
| Canonical Decision Model is SOLE owner | Any re-parsing of verification prose for winner/rejected elsewhere |
| No silent termination | A path that breaks a drive loop without setting `error` |
| No silent restart | Any resume path that infers intent instead of consulting the recorded state |
| Re-entrancy | Any module-level recorder, artifact manager, or policy engine shared across threads |
| Pure stdlib at import | A new top-level `import` of a provider module |
| Byte-for-byte reproducibility | Non-deterministic dict iteration, timestamps inside file content |
| Determinism | A time-based or random branch in any pure component |
| Non-negotiable abstention | Any path that lets `RESEARCH_ONLY` or `ARCHITECTURE_ONLY` reach `execute_build_spec`'s dispatch |
| Backward compatibility | An additive `import` that re-exports a Phase 1-3.1 symbol under a new name; a side-effect at import time |

---

## 18. Deliberately deferred (with rationale)

Per the YAGNI ladder, the following abstractions are deferred — not
omitted. Each has a concrete upgrade trigger.

| Deferred piece | Reason | When to add |
|----------------|--------|-------------|
| ABCs / Protocol interfaces for one implementation | The codebase does DI via callable seams (`debate_fn`, `executor_fn`, `agent_fn`, `test_fn`, `mode_select_fn`, `planner_fn`, `input_fn`, `research_fn`, `coding_agent_fn`, `testing_agent_fn`, `repair_agent_fn`). Single-interface-with-one-implementation ABCs are the canonical YAGNI anti-pattern. | When a second concrete implementation appears (e.g. `RemoteWorkspaceOrchestrator` + `NativeWorkflowOrchestrator`). |
| Error sub-hierarchy (subclass per Decision/Validation/Artifact/Planning/Execution/Telemetry/Repair) | Identical fields + identical handlers = the discriminator does the job at one class's cost. | When a caller needs `except ValidationError` distinct from `except LifecycleError`; today the caller branches on `e.kind`. |
| Factory for orchestrators | One entry in `_WORKFLOW_ORCHESTRATORS`; a name-keyed constructor suffices. | When a second concrete orchestrator appears. |
| Strategy hierarchy for `Stage` | `BaseStage` + the `Stage` Protocol already provide swappability; a subclass per stage type is over-engineering. | When two interchangeable behaviors for the *same state* appear. |
| Repository pattern for `ArtifactManager` | An in-memory dict + `artifact_manifest.json` is the single backing store. | When a second backing store appears (PostgreSQL). |
| Cross-workflow event bus | The existing `EventBus` is per-orchestrator. Cross-workflow notification is a service-locator. | When a concrete cross-workflow consumer appears. |
| A plugin framework for `BenchmarkEngine` / `FaultInjectionEngine` / `StressTestEngine` | Each accepts `workloads` / `injectors` / `orchestrator_factory` as constructor kwargs. A plugin framework is a registry on top of a registry. | When a second producer of workloads/injectors must be discovered at runtime. |
| A checkpointing strategy (per-stage deltas) | `save_state` writes the full context after every transition. Small payload, once per stage. | When checkpoint cost is profiled to be a bottleneck (millions of transitions/min or >1MB contexts). |
| `Stage.rollback` auto-invocation | Contract exists; no caller invokes it. Wiring auto-rollback changes `next_state`'s contract. | When a stage needs rollback semantics the repair loop doesn't cover. |
| A visitor for the workflow graph | The graph is `LINEAR_PATH` + one repair branch. Two `if`s. A visitor wraps two `if`s in a walker. | When the workflow graph gains branches. |
| DI interfaces as ABCs | Callable seams already provide swappability. | When a second concrete strategy appears. Adding ABCs before then pins the API prematurely. |

---

## 19. Migration + backward-compatibility notes

1. **No existing module is modified outside an additive `import` seam.**
   Phase 4's `workflow_orchestrator.py` imports `EventBus`, `Event`,
   `ExecutionRecorder`, `_workspace_dir`, `_now_iso`, `_write_json`,
   `_read_json` from `implementation_engine` — read-only uses. The
   Hardening's modules are additive.
2. **Phase 1-3.1 components still directly importable.** The four
   `test_phase1231_*_still_importable` tests in
   `tests/test_workflow_orchestrator.py` pin the boundary.
3. **The architecture hardening is untouched** by Phase 4 and vice-versa.
4. **The build-lifecycle state machine and the workflow state machine
   compose without overlap.** Different concerns, different resume
   paths, different state enums. A production IMPLEMENTATION stage will
   call `lifecycle.advance_lifecycle` internally; Phase 4 never touches
   build lifecycle.
5. **The artifact model hardening and the ArtifactManager compose without
   overlap.** Different files, different subdirectories, different
   responsibilities.
6. **Pre-Phase-4 workspaces have no `workflow/` dir.** `resume` on such a
   workspace returns the structured "no checkpoint to resume from"
   error; a fresh `run` creates the `workflow/` dir on the first
   transition.
7. **Pre-Hardening workspaces resume** via the legacy-status mapper
   (`lifecycle_stage_from_status`); the ~30 legacy ad-hoc status
   strings all map to a lifecycle state.
8. **The legacy `orchestrator.submit_job` API still works**; the
   recommended path is `workspace_orchestrator.submit_workspace`.
9. **The legacy `pipeline_runner.execute(prompt)` (single-package) is
   unchanged**; the integrated entry is `execute_scheduled`.
10. **`build_workspace.write_build_state` is byte-identical**; the
    hardening writes a parallel `lifecycle_stage` field.

---

## 20. Developer handbook — using Project Sol0mon

```python
# 1. The single integrated entry — Phase 3.6 + the hardening + Phase 4
from pipeline_runner import execute_scheduled
result = execute_scheduled(
    "Build a planet-scale consensus platform with PostgreSQL + Temporal",
    workspace_id="<existing>",   # optional; None creates fresh
)
# result["authority"]   → the Effective Execution Decision
# result["scheduled"]   → the per-package scheduler results
# result["stages"]      → the stage-logger audit trail

# 2. The legacy single-package entry
from pipeline_runner import execute
result = execute("Design a planet-scale consensus platform")

# 3. The workspace-based API (recommended for follow-ups)
from workspace_orchestrator import submit_workspace, follow_up_workspace, get_workspace_status
r = submit_workspace("Design a planet-scale consensus platform")
ws_id = r["workspace_id"]
# ... later:
fu = follow_up_workspace(ws_id, "Support 100,000 concurrent users instead of 10,000")
# Only affected disputes are re-debated.

# 4. Resolve a HUMAN_REQUIRED decision
from workspace_orchestrator import resolve_human_requirement
resolve_human_requirement(ws_id, decision_id, "PostgreSQL")
follow_up_workspace(ws_id, "Continue with PostgreSQL as the database")

# 5. Run the full build pipeline (the gate is authoritative)
from build_pipeline import run_build_pipeline
result = run_build_pipeline(
    prompt="Build a 3-component service with tests",
    use_claude_code=True,
)
# blocked? → result["blocked_reason"] == "execution_decision"
# OR      → "pending_human_decisions"
# OR      → "incomplete_spec"

# 6. Resume a Phase 4 workflow (cross-layer recovery)
from workflow_orchestrator import NativeWorkflowOrchestrator
orch = NativeWorkflowOrchestrator(stages=production_registry())
r = orch.resume(workspace_id=ws_id)
# Absent/corrupt → structured error; never silent restart.

# 7. Resume a build (Hardening)
from resume import resume_build
report = resume_build(workspace_id=ws_id, use_claude_code=True)
# Reads lifecycle_stage; re-enters at the right stage.

# 8. The deterministic Decision Model interpretation (single source of truth)
from verification_engine import verify_all_disputes
from decision_model import decision_records
v = verify_all_disputes(question, validated_disputes, gpt_pos, gemini_pos)
records = decision_records(v)   # one per RESOLVED claim
# records[i]["winner_text"], ["winner_tokens"], ["rejected_options"]

# 9. The build-spec deterministic renderer (Path 3 — no LLM)
from build_spec_engine import build_spec
spec = build_spec(source_answer, source_result=result_dict, debate_fn=None)
# spec["spec_text"] is byte-deterministic given source_result.

# 10. The Execution Decision Gate (V3)
from execution_decision_engine import decide
d = decide(user_prompt="Build foo", verification_result=v)
# d.execution_mode ∈ {RESEARCH_ONLY, ARCHITECTURE_ONLY, IMPLEMENTATION_ONLY,
#                      BUILD_ONLY, BUILD_AND_VALIDATE, FULL_BUILD, CUSTOM}
# d.blocked, d.block_status, d.source, d.confidence

# 11. The Provider Router (4-tier failover)
from provider_router import generate
text = generate(prompt, module="my_module")
# Failover: Cerebras → Groq → OpenRouter → Ollama. Key rotation per tier.
# Non-failover (raises immediately): 400, 401, 403, malformed.

# 12. The offline test suite (no browser, no external API)
# $ python3 run_all_tests.py
# $ python3 -m pytest tests/test_workflow_orchestrator.py -q  # 63 tests

# 13. The self-checks (fast pre-CI smoke)
# $ python3 workflow_orchestrator.py    # "workflow_orchestrator self-check PASSED"
# $ python3 decision_model.py          # "decision_model self-check PASSED"
# $ python3 build_spec_engine.py       # Path 3 render self-check
```

---

## 21. Summary — what Project Sol0mon is, in one paragraph

Project Sol0mon is an **autonomous, consensus-driven architecture-design
platform** that runs structured two-model (GPT + Gemini) debates with a
third-tier (Cerebras → Groq → OpenRouter → Ollama) verifier to produce
implementation-ready architecture decisions outperforming standalone
frontier models. It is built as five additive, independently-resumable
layers: Layer 1 (the consensus engine + the Canonical Decision Model),
Layer 2 (the execution / validation / repair loop), Layer 3 (integration
— AI planner, dependency scheduler, execution authority, provider
router, workspace system), Layer 4 (the architecture hardening —
lifecycle state machine, typed artifacts, validation framework,
deterministic planner, manifest), Layer 5 (Phase 4 orchestration —
state machine, policy engine, recovery, packaging, benchmark, fault
injection, stress test). Every layer has exactly one authority per
concern; every state advance is explicit and persisted; every failure
surfaces a structured error; every resume re-enters at the recorded
state, never silently restarting. The codebase adds fifteen Phase-4
components in one new module, six Hardening components in six new
modules, and ~250 + 68 + 25 + 63 regression tests across twenty-five
test files — all offline, all pure stdlib at the orchestration layer,
all additive over the existing engines. No speculative abstraction is
introduced: no ABC with one implementation, no Protocol interface for
one class, no factory / strategy / repository / plugin / event-bus /
service-locator / visitor / builder hierarchy; DI is via callable seams
(`debate_fn`, `executor_fn`, `agent_fn`, `test_fn`, `mode_select_fn`,
`planner_fn`, `input_fn`, `research_fn`, `coding_agent_fn`,
`testing_agent_fn`, `repair_agent_fn`). The single architectural
invariant that runs through every layer is: there is exactly one
authority for each kind of decision, and every other component consumes
that authority's output over its own re-derivation — the answer-156
"rejected-option leak" bug is what this discipline prevents.

---

## Appendix A. Where each contract lives

| Contract | Live spec section |
|----------|-----|
| Decision Model canonical return shape | §6.1 |
| Debate Engine constants + Dispute state | §6.2 |
| Verification Engine research escalation | §6.3 |
| Synthesis Engine composition + FIX 6 | §6.4 |
| Conflict Extractor severity | §6.5 |
| Governance — three outcomes + five dimensions | §6.6 |
| Answer memory + human decision store | §7.1-7.3 |
| Build workspace + workspace manager | §7.4-7.5 |
| Workspace knowledge layout | §7.6 |
| V2 classifier six-rung hierarchy + non-negotiable abstention | §6.9 |
| V3 engine — 7 modes, 6 stages, 6-source precedence | §6.10 |
| Mode gate (user-facing depth) | §6.11 |
| Execution Authority — 4-source precedence + AUTHORITY log | §6.12 |
| Implementation engine graph + RetryPolicy + recorder | §6.13 |
| Repair loop contract + Phase 2 strengthened tests-passed | §6.14 |
| Validation engine | §6.15 |
| Repair memory | §6.16 |
| Project Planner + validation rules | §6.17 |
| Pipeline scheduler + READY-only-execute + BLOCKED reasons | §6.18 |
| Pipeline runner + Stage logger | §6.19, §6.20 |
| Workflow classifier | §6.21 |
| Build pipeline — Callable seams + gate | §6.22 |
| Build spec engine — Path 3 deterministic | §6.23 |
| Build prompt generator — SPEC_SECTIONS | §6.24 |
| Decision renderer — `_architecture_components` + contradiction validator | §6.25 |
| Provider router — 4-tier + key rotation + health | §6.26 |
| Workspace orchestrator public API | §6.27 |
| Cross-layer state machines | §8 |
| Cross-layer sequence diagrams | §9 |
| Cross-layer recovery semantics | §10 |
| Cross-layer artifact lifecycle | §11 |
| PolicyEngine rules cross-layer (build-level + workflow-level) | §12 |
| Benchmark framework | §13 |
| Fault injection framework | §14 |
| Stress test framework | §15 |
| Comprehensive testing matrix | §16 |
| Success criteria + acceptance gates | §17 |
| Deferred abstractions | §18 |
| Migration / back-compat | §19 |
| Developer handbook | §20 |

## Appendix B. Pull in by reference

The two canonical pre-existing specs:

- `docs/architecture_hardening.md` — Layer 4 (lifecycle, artifacts,
  validation, planner, resume, errors). Cited inline throughout.
- `docs/workflow_orchestration.md` — Layer 5 (workflow orchestrator,
  state machine, policy engine, recovery, packaging, benchmark, fault
  injection, stress test). Cited inline throughout.

This document composes those by reference and specifies Layers 1-3 plus
the cross-layer invariants. To port Project Sol0mon to OpenCode, the
porting team should treat this single document as the implementation
blueprint; the parent specs are pulled in where cited.

---

End of spec.
