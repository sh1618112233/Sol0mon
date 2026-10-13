# Sol0mon Build Architecture — Canonical Reference

**Status:** Frozen for feature development after the Build Finalization hardening audit.
**Audience:** Engineers extending Sol0mon. This document is the single authority on
ownership rules, persistence model, and lifecycle semantics. Code is the
implementation; this document is the contract.

---

## 1. System Lifecycle

```
Research (out of build lifecycle)
   |
   v
Debate -----> Verification -----> [answer_memory INSERT]
                                   |
                                   v
                          (CLI asks: FULL_BUILD?)
                                   |
                                   v
                          build_pipeline.run_build_pipeline
                                   |
   +-------------------------------+-----------------------------+
   | Stage                 Owner          Transition                Writer         |
   |----------------------------------------------------------------------------|
   | New              build_pipeline    (workspace created)       create_workspace|
   | Verified         decision_model    (verification_result)    save_debate      |
   | SpecReady        build_spec_engine (spec complete)          store_impl_spec  |
   | ExecutionAllowed execution_decision+human gate              advance_lifecycle|
   | Executing        implementation_executor                     advance_lifecycle|
   | ArtifactsValidated implementation_executor                  advance_lifecycle|
   | TestsValidated   implementation_executor                    advance_lifecycle|
   | Published        build_finalizer (SOLE OWNER)               advance_lifecycle|
   | Complete         build_finalizer (SOLE OWNER)               advance_lifecycle|
   +----------------------------------------------------------------------------+

Terminal states:
   - COMPLETE: reached only by build_finalizer.stage_exit
   - ABORTED:  reached by operator cancel from any state
   - FAILED:   reached from SPEC_READY / EXECUTION_ALLOWED / EXECUTING / REPAIRING

Re-publication edge: COMPLETE -> PUBLISHED is the SOLE outgoing edge from
COMPLETE; it exists so the idempotent finalizer can re-run after a build that
reached COMPLETE via the legacy "executed" status map.
```

**Lifecycle invariants:**
- Every non-terminal forward movement is one of the entries in
  `lifecycle.BUILD_LIFECYCLE`.
- `PUBLISHED` is owned by `build_finalizer.finalize_build`. No other module
  may call `advance_lifecycle(_, BuildLifecycle.PUBLISHED)`.
- `COMPLETE` is owned by `build_finalizer.finalize_build` (stage exit). The
  sole legacy direct jump `TESTS_VALIDATED -> COMPLETE` (e.g. in test
  fixtures that pre-date the finalizer) remains legal for backward
  compatibility; production paths route through PUBLISHED.
- A build that reaches `TESTS_VALIDATED` MUST pass through the Build
  Finalizer before reaching `COMPLETE` on the production path.

## 2. Ownership Rules — Single Source of Truth

Every persisted value has exactly ONE authoritative writer. Other writes
that exist are temporal-stage predecessors, not duplicates.

| Persisted value | Sole authority | Writer (function) | Other writers (allowed temporal predecessors) |
|---|---|---|---|
| `debates.<id>.workspace_id` (UPDATE) | Build Finalizer STEP 1 | `answer_memory.update_build_result` | — (no other UPDATE path) |
| `debates.<id>.verification_result.execution_decision/reason/confidence/source` (UPDATE) | Build Finalizer STEP 1 | `answer_memory.update_build_result` (merges into JSON) | — (no other UPDATE path) |
| `debates.<id>` (INSERT) | debate engine | `answer_memory.save_debate` | — (INSERT-only; debate fills workspace_id for follow-up-debate workspace, not build) |
| `workspaces` SQL row (build) | Build Finalizer STEP 2 | `workspace_mgr.upsert_build_workspace` | — (no other INSERT path for build workspaces) |
| `workspaces` SQL row (debate-context) | workspace_debate_engine | `workspace_mgr.save_workspace` | (different ID space; debate-context, not build) |
| `PUBLISHED` lifecycle stage | Build Finalizer | `advance_lifecycle(_, PUBLISHED)` at `build_finalizer.py:222` | — (sole call site in the repo) |
| `workspace_status.json` (CWD, build-phase snapshot) | Build Finalizer STEP 3 | `rec.save_status()` at `build_finalizer.py:434` | `pipeline_runner.execute` writes the debate-phase snapshot (pre-finalizer; finalizer overwrites) |
| `performance_report.json` (CWD, build-phase snapshot) | Build Finalizer STEP 3 | `rec.save_report()` at `build_finalizer.py:435` | `pipeline_runner.execute` writes the debate-phase snapshot (pre-finalizer; finalizer overwrites) |
| `manifest.json` (post-state-change writer) | `manifest.refresh_manifest` | `manifest.refresh_manifest` (sole canonical function) | `build_pipeline._persist_build_manifest` and `resume._refresh_manifest` both delegate here |
| `build_state.json` (workspace-local) | build_pipeline (per stage) | `build_workspace.write_build_state` | resume.py per-transition (same direction, no divergence) |
| `implementation_plan.md` | build_spec_engine | `store_implementation_spec` | — (single writer) |
| `execution_plan.json` | execution_planner | `store_execution_plan` | — (single writer) |
| `workspace.json` (filesystem manifest) | build_workspace | `save_workspace` (writes BuildWorkspace) | — (single writer) |
| `decisions.json` | build_pipeline (resurrect_answer) | inline write at `build_pipeline.py:131` | — (single writer) |
| `answer.json` | build_pipeline (resurrect_answer) | inline write at `build_pipeline.py:122` | — (single writer) |

**No duplicate writers exist for any reconciled value.** Build Finalizer is
the sole owner of every post-build reconciliation field.

## 3. Persistence Model

Sol0mon has three independent persistence silos. They were independent by
design (different lifecycles, different scopes); the Build Finalizer is the
single authority that reconciles them after a successful build.

```
+------------------------------+   +----------------------------+
|  answer_memory.db (SQLite)   |   |  database/workspaces.db    |
|  debates table               |   |  workspaces table         |
|  - id (INTEGER PK)           |   |  - workspace_id (TEXT PK) |
|  - workspace_id (TEXT)       |   |  - original_prompt         |
|  - verification_result (JSON)|   |  - status                  |
|  - execution_decision*       |   |  - consensus_score         |
|  - human_answer              |   |  - answer_history (JSON)  |
|  - synthesized_answer        |   |  - verified_decisions      |
|  - resolved_decisions        |   |  - ...                    |
|  - parent_answer_id (chain)  |   +----------------------------+
+------------------------------+
                 ^
                 | Build Finalizer STEP 1 (UPDATE) reconciles these after build
                 | Build Finalizer STEP 2 (UPSERT) registers build workspace

+--------------------------------------------------+
|  workspaces/<id>/ (filesystem JSON)               |
|  - workspace.json   (BuildWorkspace manifest)    |
|  - build_state.json (live pipeline progress)     |
|  - answer.json       (resurrected answer)         |
|  - decisions.json    (verification_result + ...) |
|  - implementation_plan.md (the build guide)      |
|  - execution_plan.json                            |
|  - manifest.json    (BuildManifest, references)   |
|  - synthesized_answer.md                          |
|  - src/<slug>/                                   |
|  - tests/                                        |
+--------------------------------------------------+
                 ^
                 | build_pipeline writes the build itself
                 | Build Finalizer STEP 3+4 publish/verify
                 | manifest.refresh_manifest re-stamps (sole writer)

+----------------------------------+
|  workspace_status.json (CWD)      |
|  performance_report.json (CWD)    |
+----------------------------------+
                 ^
                 | pipeline_runner writes the debate-phase snapshot
                 | Build Finalizer STEP 3 overwrites with the build-phase snapshot
```

**The Build Finalizer is the ONLY module that writes across silos.**
Every other module writes to exactly ONE silo.

## 4. Workspace Model

Sol0mon has TWO workspace dataclasses, intentionally separate:

| Dataclass | File | Persistence | Used by |
|---|---|---|---|
| `BuildWorkspace` | `build_workspace.py` | Filesystem JSON (`workspace.json`) | build_pipeline, resume, build_finalizer |
| `Workspace` | `workspace_manager.py` | SQL (`database/workspaces.db`) | workspace_debate_engine, workspace_orchestrator, resume/status CLI |

These are NOT duplicates — they live in different persistence systems with
different scopes. Build Finalizer does NOT merge them; it bridges them:

- Filesystem `BuildWorkspace.source_answer_id` ← links to → debate row.
- Build Finalizer STEP 2 UPSERTs a SQL `Workspace` row to mirror the
  filesystem `BuildWorkspace` so the SQL-backed resume/status CLI can find
  the filesystem-backed build workspace.

A `workspaces/` directory (filesystem) and a `workspaces` SQL table
(database) coexist; one is for build workspaces, the other is for
debate-context workspaces. The Build Finalizer is the only path that
writes from one into the other.

## 5. Publication Model

Publication is the lifecycle stage between TESTS_VALIDATED and COMPLETE.
It is owned by `build_finalizer.finalize_build`, runs ONCE per successful
build, and performs exactly 5 reconciliation steps in order:

```
STEP 1  answer_memory.update_build_result  (UPDATE debates row)
STEP 2  workspace_mgr.upsert_build_workspace (UPSERT SQL workspaces row)
STEP 3  telemetry reconfigure + observe + save_status / save_report
STEP 4  publish_artifacts   (verify required files + manifest ids)
STEP 5  reconcile_state     (cross-store consistency check)
STAGE EXIT on success: advance_lifecycle(_, COMPLETE)
                     + manifest.refresh_manifest (re-stamp post-COMPLETE)
        on failure: stay at PUBLISHED (NOT terminal; idempotent re-run via resume)
```

**Transaction contract:** each step is independent. A failed step is
reported in `result["finalization"]["steps"]` and does NOT roll back the
build (which is the on-disk truth). The pipeline's success return path
is unchanged when finalization fails — only the audit trail grows.

**Idempotency:** re-running the finalizer is safe and produces the same
final state:
- `update_build_result` (UPDATE) overwrites the same columns with the same values.
- `upsert_build_workspace` (UPSERT) is idempotent — UPDATE-existing or INSERT.
- `telemetry save_status/save_report` overwrite the same files.
- `publish_artifacts` is read-only.
- `reconcile_state` is read-only.
- `advance_lifecycle(_, PUBLISHED)` and `(_, COMPLETE)` allow re-advance
  to the same stage (resume convenience).

Callers:
- `build_pipeline.run_build_pipeline` (post-`_persist_build_manifest`)
- `resume._classify_execution_result` (post-success executor, instead of
  the legacy `TESTS_VALIDATED -> COMPLETE` direct jump)
- Future: a standalone `sol0mon publish` CLI command can call
  `finalize_build` directly to re-publish after manual artifact edits.

## 6. Execution Decision Model

The execution decision evolves through three phases. Each phase has one
owner; the final phase reconciles the others.

```
Phase A (verifier emit, debate phase)
  Owner:   verification_engine → decision_model
  Writes:  verification_result.execution_decision (RESEARCH_ONLY, etc.)
           + execution_decision_source (e.g. "research_analysis")
  Store:   answer_memory.debates.<id>.verification_result (JSON,
           IMMUTABLE at debate-time, via save_debate INSERT)

Phase B (override, build phase)
  Owner:   build_pipeline._maybe_engine_override
  Writes:  build_state.json.execution_decision (IMPLEMENTATION_REQUIRED)
           + execution_decision_source = "execution_decision_engine"
  Store:   filesystem build_state.json (workspace-local)
  Why:     the soft RESEARCH_ONLY emit was a heuristic; the user's
           explicit FULL_BUILD selection + the engine's prompt analysis
           override it for THIS build only.

Phase C (reconciliation, publication)
  Owner:   build_finalizer.finalize_build STEP 1
  Writes:  answer_memory.debates.<id>.workspace_id (UPDATE)
           answer_memory.debates.<id>.verification_result.execution_decision
             /reason/confidence/source (UPDATE — merges JSON keys)
  Store:   answer_memory SQL (the durable debate row)
  Why:     before publication, the debate row still carried the
           debate-time RESEARCH_ONLY emit; consumers navigating
           "latest answer -> decision" would see the stale emit. STEP 1
           mirrors the build-phase verdict into the durable row.
```

**Immutability contract:** Phase A's verification_result is immutable as
long as Phase C has not run. After Phase C, only the decision-verdict keys
(`execution_decision/reason/confidence/source`) are overridden; the
verifier's `resolved_claims`, `verification_score`, etc. are preserved.

## 7. Resume Semantics

```
resume_build(workspace_id)
  - Reads current_lifecycle; refuses to run from terminal (COMPLETE, ABORTED).
  - Drives the workspace forward stage-by-stage from its current point.
  - On successful execution, calls _classify_execution_result, which:
      EXECUTING -> ARTIFACTS_VALIDATED -> TESTS_VALIDATED
                   -> _finalize_after_resume  (NEW: routes through finalizer)
                   -> (inside finalizer) PUBLISHED -> publication steps
                                                   -> COMPLETE on success
  - Returns {"resumed": True, "lifecycle_stage": ..., "finalization": ...}
```

Resume paths DO NOT bypass Publication. Any successful executor run
through `resume_execution` publishes through the same Build Finalizer
that `run_build_pipeline` uses.

## 8. Naming Conventions (Stable)

These names are stable; do NOT rename in feature work without a
deprecation cycle:

- `workspace_id` — UUID string identifying a build workspace (filesystem)
  or a debate-context workspace (SQL). Always present on every artifact.
- `source_answer_id` — integer id of the originating debate row. Carried
  by `workspace.json` (filesystem workspace manifest), `answer.json` /
  `decisions.json` (resurrected answer model), and `manifest.json`
  (provenance; added by this audit).
- `lifecycle_stage` — the authoritative lifecycle marker, stored in
  `build_state.json`. Falls back to mapping legacy `status` strings when
  `lifecycle_stage` is absent (legacy workspaces).
- `execution_decision` — JSON-string verdict on whether to implement.

## 9. Finalization Audit (this document's source)

The findings in this document come from the architecture-hardening audit
performed after the Build Finalization feature. Three architectural
issues were identified and fixed:

1. `resume._classify_execution_result` jumped `TESTS_VALIDATED -> COMPLETE`
   bypassing the finalizer. Fixed: routes through `_finalize_after_resume`
   → `finalize_build`.
2. `_persist_build_manifest` (build_pipeline) and `_refresh_manifest`
   (resume) were byte-identical helpers under different names. Fixed:
   both delegate to the canonical `manifest.refresh_manifest`.
3. `BuildManifest` lacked `source_answer_id` provenance. Fixed: the
   manifest now carries it; `build_manifest_from_workspace` populates it
   from `workspace.json`; the manifest id digest includes it.

These are the post-audit architectural invariants this document describes.
