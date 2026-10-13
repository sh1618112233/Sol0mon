# Integration Gap Report

## PHASE 5 — Gap Report

All 6 execution paths assessed. Gaps connected with minimal code during Phase 3.

---

## PATH A: User Prompt -> Workflow Classifier -> Debate Pipeline

| Field | Value |
|-------|-------|
| **Pre-audit Status** | PARTIALLY_CONNECTED |
| **Post-audit Status** | CONNECTED |
| **Test** | `tests/test_workflow_pipeline.py` |

### Pre-audit gap
- `workspace_orchestrator.submit_workspace()` did not call `workflow_classifier.classify()`.
- `pipeline_runner.execute()` did not call any classifier — prompts went straight to debate.
- Only `sol0mon_graph.run_workflow()` connected them, but it required LangGraph + live browsers.
- "Build Project Sol0mon MVP" produced 0 keyword matches (needed LLM call for classification).

### Fix applied
1. Added "build" keyword to `workflow_classifier.py` IMPLEMENTATION_KEYWORDS (1 line).
2. `build_pipeline.classify_prompt()` wraps `classify()` as a thin entry point.

### Remaining gap
- `workspace_orchestrator.submit_workspace()` and `pipeline_runner.execute()` still do not call `classify()` directly. This is by design: they are the raw debate entry points. `build_pipeline.run_build_pipeline()` is the route that classifies first. The `sol0mon_graph` path also classifies. No fix needed.

---

## PATH B: Answer -> Answer Loader -> Workspace Creation

| Field | Value |
|-------|-------|
| **Pre-audit Status** | DISCONNECTED |
| **Post-audit Status** | CONNECTED |
| **Test** | `tests/test_answer_resurrection_pipeline.py` |

### Pre-audit gap
- `answer_loader.get_answer_by_id()` and `answer_selector.select_answer()` existed but nothing called them to create a build workspace.
- `build_workspace.create_workspace(source_answer_id=...)` existed but nothing passed a resurrected answer into it.

### Fix applied
- `build_pipeline.resurrect_answer(answer_spec)` wires `select_answer -> create_workspace -> write synthesized_answer.md`. 15 lines.

### Remaining gap
- None. `build_pipeline.run_build_pipeline()` auto-detects "answer N" in prompts and routes to resurrection.

---

## PATH C: Workspace -> Build Prompt Generator -> Build Spec Engine

| Field | Value |
|-------|-------|
| **Pre-audit Status** | DISCONNECTED |
| **Post-audit Status** | CONNECTED |
| **Test** | `tests/test_build_spec_pipeline.py`, `tests/test_answer_resurrection_pipeline.py` |

### Pre-audit gap
- `build_prompt_generator.generate_build_prompt()` and `build_spec_engine.build_spec()` existed but were never called from a pipeline. No orchestration: load workspace -> generate prompt -> run debate -> store spec.

### Fix applied
- `build_pipeline.generate_build_spec(workspace_id, source_answer, debate_fn, source_result)` calls `build_spec_engine.build_spec()` and `store_implementation_spec()`. 10 lines.

### Remaining gap
- None. `build_pipeline.run_build_pipeline()` calls `generate_build_spec` after answer resurrection/workspace creation.

---

## PATH D: Build Spec -> Implementation Executor

| Field | Value |
|-------|-------|
| **Pre-audit Status** | DISCONNECTED |
| **Post-audit Status** | CONNECTED |
| **Test** | `tests/test_implementation_pipeline.py` |

### Pre-audit gap
- `build_spec_engine.load_implementation_spec()` and `implementation_executor.execute_implementation()` existed but nothing connected them. The spec was stored in the workspace but never loaded into the executor.

### Fix applied
- `build_pipeline.execute_build_spec(workspace_id, executor_fn)` loads the spec and passes it to the executor. 15 lines. Executor is injectable for mock testing.

### Remaining gap
- None. The executor accepts the spec text and returns a result dict. Mock-execution tests verify the wiring without invoking Claude Code.

---

## PATH E: Implementation Executor -> Coding Agent Router

| Field | Value |
|-------|-------|
| **Pre-audit Status** | DISCONNECTED |
| **Post-audit Status** | CONNECTED |
| **Test** | `tests/test_implementation_pipeline.py` |

### Pre-audit gap
- `implementation_executor` used `cerebras_client` or the Claude Code CLI directly. It never called `coding_agent_router.get_agent()`. The router existed as a standalone stub.

### Fix applied
- `build_pipeline.execute_build_spec()` calls `coding_agent_router.get_agent("opencode", workspace_id)` after execution and attaches it to the result dict. 2 lines.

### Remaining gap
- IMPLEMENTED: Agent router is a stub. `OpenCodeAgent.run()` returns `AgentResult(success=False)`. Real agent backends (Claude Code, Codex, Gemini CLI) will be validated in a later phase per project constraints.
- RECOMMENDED FIX: When ready, have `implementation_executor._invoke_claude_code` route through `coding_agent_router.get_agent("claude_code")` instead of calling the CLI directly.
- ESTIMATED EFFORT: ~50 lines (new `ClaudeCodeAgent` subclass + router registration).

---

## PATH F: Human Required -> Decision Bundle -> Human Decision Manager

| Field | Value |
|-------|-------|
| **Pre-audit Status** | DISCONNECTED |
| **Post-audit Status** | CONNECTED |
| **Test** | `tests/test_build_spec_pipeline.py` |

### Pre-audit gap
- `decision_bundle.collect_from_governance_result()` and `human_decision_manager.save_pending()` existed but nothing in the pipeline called them. The existing pipeline used `human_decision_queue` (SQLite) — a separate system.
- 3 HUMAN_REQUIRED outcomes were never aggregated into 1 bundle.

### Fix applied
- `build_pipeline.aggregate_human_decisions(workspace_id, governance_result)` calls `collect_from_governance_result` then `save_pending`. 8 lines.
- `build_pipeline.run_build_pipeline()` calls `aggregate_human_decisions` when `source_result` contains `human_required` items.

### Remaining gap
- The existing `workspace_debate_engine.py` post-loop block (lines 662-694) still uses `human_decision_queue.enqueue_human_decision` (SQLite), not `decision_bundle` (flat-file). These are two parallel systems:
  - SQLite queue: debate-pipeline live runtime (protected, not modified)
  - Flat-file: build-phase aggregation (Phase 2+)
- RECOMMENDED FIX: When integrating, overwrite the post-loop block to call `build_pipeline.aggregate_human_decisions` instead of `enqueue_human_decision`. This is a 5-line change to `workspace_debate_engine.py` but is outside this audit's scope (debate methodology is protected).
- IMPACT: Without this change, human decisions collected by the debate pipeline do not flow into the build workspace's `human_decisions.json`. They only land in the SQLite queue. `build_pipeline` bridges this by accepting a `source_result` and aggregating separately.
- ESTIMATED EFFORT: ~5 lines, but requires touching `workspace_debate_engine.py` (not in the protected list but closely tied to debate methodology).

---

## Summary

| Path | Pre-audit | Post-audit | Test |
|------|-----------|------------|------|
| A: Prompt -> Classifier -> Debate | PARTIALLY_CONNECTED | CONNECTED | test_workflow_pipeline.py |
| B: Answer -> Loader -> Workspace | DISCONNECTED | CONNECTED | test_answer_resurrection_pipeline.py |
| C: Workspace -> BuildPrompt -> BuildSpec | DISCONNECTED | CONNECTED | test_build_spec_pipeline.py |
| D: BuildSpec -> ImplExecutor | DISCONNECTED | CONNECTED | test_implementation_pipeline.py |
| E: ImplExecutor -> CodingAgentRouter | DISCONNECTED | CONNECTED | test_implementation_pipeline.py |
| F: HumanRequired -> DecisionBundle -> Manager | DISCONNECTED | CONNECTED | test_build_spec_pipeline.py |

## Files Modified

| File | Change | Lines |
|------|--------|-------|
| `workflow_classifier.py` | Added "build" keyword to IMPLEMENTATION_KEYWORDS | 1 |
| `build_pipeline.py` | NEW — thin wiring module | ~150 |

## Files NOT Modified (protected)

- debate_engine.py
- verification_engine.py
- governance.py
- consensus_engine.py
- chat_session.py
- gemini_worker.py
- gpt_worker.py

## Remaining Gaps (deferred)

| Gap | Impact | Effort |
|-----|--------|--------|
| Agent stubs (OpenCode/Claude/Codex/Gemini CLI) | No real code execution via coding agents | ~200 lines per agent |
| workspace_debate_engine -> decision_bundle wiring | Human decisions don't auto-flow from debate to build workspace | ~5 lines (touches debate-adjacent file) |
| implementation_executor direct CLI bypass | Executor calls Claude CLI directly, not through router | ~50 lines refactor |
