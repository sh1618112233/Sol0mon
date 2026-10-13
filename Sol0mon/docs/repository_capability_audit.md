# Repository Capability Audit

## PHASE 1 — Capability Audit

All 9 major components assessed. Status as of 2026-07-11.

---

### 1. Workflow Classifier

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `workflow_classifier.py` |
| **Entrypoint** | `classify(prompt) -> WorkflowClassification` |
| **Public Functions** | `classify()`, `is_implementation()`, `can_execute_code()` |
| **Dependencies** | `cerebras_client.generate` (LLM fallback), `debate_engine._extract_json` |
| **Callers** | `sol0mon_graph.node_classify_workflow`, `build_pipeline.classify_prompt` |
| **Notes** | Rule-based pre-filter + LLM fallback. 4 workflow types: DISCUSSION, RESEARCH, ARCHITECTURE, IMPLEMENTATION. Only IMPLEMENTATION allows code execution. Modified in this pass: added "build" keyword for offline classification of build prompts. |

---

### 2. Workspace System

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `workspace_orchestrator.py`, `workspace_manager.py`, `workspace_debate_engine.py` |
| **Entrypoint** | `workspace_orchestrator.submit_workspace(prompt)` |
| **Public Functions** | `submit_workspace()`, `follow_up_workspace()`, `reopen_workspace_dispute()`, `resolve_human_requirement()`, `get_workspace_status()`, `list_workspaces()` |
| **Dependencies** | `job_store`, `workspace_manager`, `workspace_debate_engine`, `human_decision_queue`, `answer_memory.memory` |
| **Callers** | `test_workspace_flow.py`, `sol0mon_graph.node_create_workspace` |
| **Notes** | SQLite-backed workspace persistence with WAL mode. Manages verified decisions, tradeoffs, human-required items, answer chain. Threaded job execution. Fully functional. |

#### Build Workspace (Phase 1 addition)

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `build_workspace.py` |
| **Entrypoint** | `create_workspace()`, `load_workspace()`, `save_workspace()` |
| **Public Functions** | `create_workspace()`, `load_workspace()`, `save_workspace()`, `list_build_workspaces()` |
| **Dependencies** | stdlib only (json, pathlib, uuid) |
| **Callers** | `build_pipeline`, `human_decision_manager`, `build_spec_engine`, `answer_resurrection_pipeline` tests |
| **Notes** | File-based workspace layout: `workspaces/workspace_<id>/` with workspace.json, synthesized_answer.md, implementation_plan.md, human_decisions.json, build_state.json. Separate from the SQLite-backed debate workspace. |

---

### 3. Answer Resurrection

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `answer_loader.py`, `answer_selector.py` |
| **Entrypoint** | `answer_selector.select_answer(spec) -> dict | None` |
| **Public Functions** | `get_answer_by_id()`, `get_latest_answer()`, `search_answers()`, `select_answer()`, `identify_topic()`, `topic_matches()` |
| **Dependencies** | `answer_memory.memory` (read-only, no schema changes) |
| **Callers** | `build_pipeline.resurrect_answer` |
| **Notes** | Selects previously synthesized answers by ID ("build answer 55"), topic ("build latest statarb bot"), or latest overall ("latest"). Returns `{"answer_id", "question", "topic", "synthesized_answer"}`. No new DB schema. |

---

### 4. Build Prompt Generation

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `build_prompt_generator.py` |
| **Entrypoint** | `generate_build_prompt(source_answer, decisions, constraints, tradeoffs) -> str` |
| **Public Functions** | `extract_decisions()`, `extract_constraints()`, `extract_tradeoffs()`, `generate_build_prompt()`, `validate_spec_sections()` |
| **Dependencies** | stdlib only |
| **Callers** | `build_spec_engine.build_spec`, `build_pipeline.generate_build_spec` |
| **Notes** | Pure functions. Extracts decisions/constraints/tradeoffs from governance result buckets. Generates a markdown build prompt with 7 required spec sections: architecture, components, file structure, dependencies, constraints, testing requirements, acceptance criteria. |

---

### 5. Build Spec Generation

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `build_spec_engine.py` |
| **Entrypoint** | `build_spec(source_answer, debate_fn, source_result) -> dict` |
| **Public Functions** | `build_spec()`, `store_implementation_spec()`, `load_implementation_spec()` |
| **Dependencies** | `build_prompt_generator`, `build_workspace._workspace_dir`, `pipeline_runner.execute` (lazy, injectable) |
| **Callers** | `build_pipeline.generate_build_spec` |
| **Notes** | Reuses existing debate methodology (pipeline_runner.execute) via injectable debate_fn. The Build Prompt Debate runs GPT->Gemini->Critique->Revision->Consensus and the consensus synthesized_answer becomes implementation_spec.md. No new debate engine created. |

---

### 6. Human Decision Aggregation

| Field | Value |
|-------|-------|
| **Status** | IMPLEMENTED |
| **File** | `decision_bundle.py`, `human_decision_manager.py` |
| **Entrypoint** | `decision_bundle.collect_from_governance_result(gov_result, workspace_id) -> dict` |
| **Public Functions** | `add_decision()`, `generate_bundle()`, `collect_from_governance_result()`, `save_pending()`, `load_pending()`, `resolve()`, `load_resolved()` |
| **Dependencies** | `build_workspace._workspace_dir` (file path only) |
| **Callers** | `build_pipeline.aggregate_human_decisions` |
| **Notes** | Collects N HUMAN_REQUIRED outcomes into 1 bundle. Persists to `workspaces/workspace_<id>/human_decisions.json` in `{"pending": [], "resolved": []}` format. Separate from the SQLite-backed `human_decision_queue`. 3 HUMAN_REQUIRED -> 1 bundle verified by test. |

---

### 7. Implementation Executor

| Field | Value |
|-------|-------|
| **Status** | PARTIALLY_IMPLEMENTED |
| **File** | `implementation_executor.py` |
| **Entrypoint** | `execute_implementation(plan, workspace_id, use_claude_code) -> dict` |
| **Public Functions** | `execute_implementation()` |
| **Dependencies** | `cerebras_client.generate` (direct implementation fallback), `subprocess` (Claude Code CLI) |
| **Callers** | `sol0mon_graph.node_execute_implementation`, `build_pipeline.execute_build_spec` |
| **Notes** | Repair loop (3 retries) with Claude Code CLI or cerebras_client fallback. Runs pytest after generation. NOT connected to coding_agent_router (uses cerebras_client or claude CLI directly). Connected via `build_pipeline.execute_build_spec` which routes to coding_agent_router for agent selection. |

---

### 8. Coding Agent Router

| Field | Value |
|-------|-------|
| **Status** | PARTIALLY_IMPLEMENTED (stub) |
| **File** | `coding_agent_router.py`, `coding_agents/base_agent.py`, `coding_agents/opencode_agent.py` |
| **Entrypoint** | `get_agent(name, workspace_id) -> BaseAgent | None` |
| **Public Functions** | `get_agent()`, `available_agents()` |
| **Dependencies** | `coding_agents.base_agent.BaseAgent`, `coding_agents.opencode_agent.OpenCodeAgent` |
| **Callers** | `build_pipeline.execute_build_spec` |
| **Notes** | Phase 1 stub. Registers opencode agent only. `run()` returns `AgentResult(success=False, error="not implemented")`. No Claude Code, Codex, or Gemini CLI integration. Connected to the pipeline via `build_pipeline.execute_build_spec` which calls `get_agent("opencode", workspace_id)` after execution. |

---

### 9. Graph Runtime

| Field | Value |
|-------|-------|
| **Status** | PARTIALLY_IMPLEMENTED |
| **File** | `sol0mon_graph.py` |
| **Entrypoint** | `run_workflow(prompt, workspace_id, thread_id, approved) -> dict` |
| **Public Functions** | `run_workflow()`, `resume_workflow()`, `build_sol0mon_graph()`, `get_compiled_graph()` |
| **Dependencies** | `langgraph` (optional, degrades gracefully), `workflow_classifier`, `workspace_manager`, `workspace_debate_engine`, `synthesis_engine`, `implementation_executor`, `cerebras_client` |
| **Callers** | None in production (smoke test only in `__main__`) |
| **Notes** | LangGraph workflow: classify -> create_workspace -> debate -> [synthesize|generate_plan -> user_approval -> execute]. Connects workflow_classifier to debate pipeline (Path A). However, uses `workspace_manager.create_workspace` (SQLite) not `build_workspace.create_workspace` (file-based). Not connected to build_prompt_generator or build_spec_engine. The file-based `build_pipeline.py` is the non-LangGraph alternative. |

---

## Summary Table

| # | Component | Status |
|---|-----------|--------|
| 1 | Workflow Classifier | IMPLEMENTED |
| 2 | Workspace System | IMPLEMENTED |
| 3 | Answer Resurrection | IMPLEMENTED |
| 4 | Build Prompt Generation | IMPLEMENTED |
| 5 | Build Spec Generation | IMPLEMENTED |
| 6 | Human Decision Aggregation | IMPLEMENTED |
| 7 | Implementation Executor | PARTIALLY_IMPLEMENTED |
| 8 | Coding Agent Router | PARTIALLY_IMPLEMENTED (stub) |
| 9 | Graph Runtime | PARTIALLY_IMPLEMENTED |
