"""
Project Sol0mon — LangGraph Workflow Orchestration Layer.

LangGraph owns:
- Workflow execution
- Checkpointing
- Recovery
- Branching
- Resume support
- Implementation workflow execution

Sol0mon owns (via debate_engine, governance, consensus, workspace):
- Debate state
- Consensus state
- Memory
- Verification
- Workspace management

This module integrates the two: LangGraph nodes call Sol0mon functions,
not the other way around. Sol0mon is the reasoning system; LangGraph is
the execution runtime.

Requirements: pip install langgraph
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypedDict

# Try LangGraph import — gracefully degrade if not installed
try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    print("[SOL0MON GRAPH] LangGraph not installed. Run: pip install langgraph")
    # Provide stub classes so imports don't crash
    class StateGraph:
        def __init__(self, state_schema=None):
            pass
        def add_node(self, *args, **kwargs):
            return self
        def add_edge(self, *args, **kwargs):
            return self
        def add_conditional_edges(self, *args, **kwargs):
            return self
        def compile(self, *args, **kwargs):
            return self
        def invoke(self, *args, **kwargs):
            return {}
        def stream(self, *args, **kwargs):
            yield {}
    START = "start"
    END = "end"
    class MemorySaver:
        def __init__(self):
            pass

from cerebras_client import generate as llm_generate  # routed through provider_router


# ---------------------------------------------------------------------------
# Workflow state schema
# ---------------------------------------------------------------------------


class Sol0monState(TypedDict, total=False):
    """State carried through the Sol0mon LangGraph workflow.

    This is the authoritative execution state. LangGraph checkpoints
    this at every node boundary for crash recovery and resume.
    """
    # Input
    prompt: str
    workspace_id: str
    workflow_type: str  # DISCUSSION | RESEARCH | ARCHITECTURE | IMPLEMENTATION

    # Pipeline outputs
    gpt_position: str
    gemini_position: str
    disputed_claims: list[str]
    validated_disputes: list[dict]
    verification_result: dict
    consensus_score: int
    governance_result: dict
    synthesized_answer: str
    answer_id: int | None

    # Implementation-only
    implementation_plan: str
    user_approved: bool
    code_output: str
    test_results: dict
    repair_attempts: int

    # Metadata
    status: str  # created | debating | consensus | implementing | completed | failed
    error: str
    checkpoint_id: str
    thread_id: str  # LangGraph thread for state persistence


# ---------------------------------------------------------------------------
# Default state
# ---------------------------------------------------------------------------


def default_state(prompt: str = "", workspace_id: str = "",
                  workflow_type: str = "DISCUSSION") -> Sol0monState:
    return Sol0monState(
        prompt=prompt,
        workspace_id=workspace_id,
        workflow_type=workflow_type,
        gpt_position="",
        gemini_position="",
        disputed_claims=[],
        validated_disputes=[],
        verification_result={},
        consensus_score=0,
        governance_result={},
        synthesized_answer="",
        answer_id=None,
        implementation_plan="",
        user_approved=False,
        code_output="",
        test_results={},
        repair_attempts=0,
        status="created",
        error="",
        checkpoint_id="",
        thread_id=str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# Node implementations — each node calls a Sol0mon function
# ---------------------------------------------------------------------------


def node_classify_workflow(state: Sol0monState) -> Sol0monState:
    """Node: classify the user prompt into a workflow type."""
    from workflow_classifier import classify
    classification = classify(state["prompt"])
    state["workflow_type"] = classification.workflow_type
    print(f"[GRAPH NODE] classify_workflow → {classification.workflow_type} "
          f"(confidence={classification.confidence:.2f})")
    return state


def node_create_workspace(state: Sol0monState) -> Sol0monState:
    """Node: create a workspace if one doesn't exist."""
    if not state.get("workspace_id"):
        from workspace_manager import create_workspace
        state["workspace_id"] = create_workspace(state["prompt"])
        print(f"[GRAPH NODE] create_workspace → {state['workspace_id'][:8]}...")
    state["status"] = "debating"
    return state


def node_debate(state: Sol0monState) -> Sol0monState:
    """Node: run the debate pipeline using WorkspaceDebateEngine."""
    from workspace_debate_engine import WorkspaceDebateEngine

    engine = WorkspaceDebateEngine()
    result = engine.run_in_workspace(
        prompt=state["prompt"],
        workspace_id=state.get("workspace_id"),
    )

    if result.get("_failed"):
        state["status"] = "failed"
        state["error"] = result.get("error", "Debate failed")
        return state

    state["gpt_position"] = result.get("gpt_final", "")
    state["gemini_position"] = result.get("gemini_final", "")
    state["validated_disputes"] = result.get("validated_disputes", [])
    state["verification_result"] = result.get("verification_result", {})
    state["consensus_score"] = result.get("final_consensus_score", 0)
    state["governance_result"] = {
        "verified_decisions": result.get("verified_decisions", []),
        "verified_tradeoffs": result.get("verified_tradeoffs", []),
        "human_required": result.get("human_required", []),
    }
    state["synthesized_answer"] = result.get("synthesized_answer", "")
    state["answer_id"] = result.get("answer_id")
    state["status"] = "consensus"

    print(f"[GRAPH NODE] debate → consensus_score={state['consensus_score']}% "
          f"disputes={len(state['validated_disputes'])}")
    return state


def node_synthesize(state: Sol0monState) -> Sol0monState:
    """Node: synthesize the final answer (lightweight, for DISCUSSION/RESEARCH)."""
    if state.get("synthesized_answer"):
        # Already synthesized during debate
        return state

    from synthesis_engine import synthesize_answer
    state["synthesized_answer"] = synthesize_answer(
        question=state["prompt"],
        gpt_final=state.get("gpt_position", ""),
        gemini_final=state.get("gemini_position", ""),
        validated_disputes=state.get("validated_disputes", []),
        consensus_score=state.get("consensus_score", 0),
        verification_result=state.get("verification_result", {}),
    )
    state["status"] = "completed"
    print(f"[GRAPH NODE] synthesize → {len(state['synthesized_answer'])} chars")
    return state


def node_generate_implementation_plan(state: Sol0monState) -> Sol0monState:
    """Node: generate an implementation plan from consensus output."""
    if state["workflow_type"] != "IMPLEMENTATION":
        return state

    prompt = (
        "Based on the following architectural consensus, generate a detailed "
        "implementation plan with specific files, functions, and acceptance criteria.\n\n"
        f"CONSENSUS OUTPUT:\n{state['synthesized_answer']}\n\n"
        f"VERIFIED DECISIONS:\n{json.dumps(state['governance_result'].get('verified_decisions', []), indent=2)}\n\n"
        "Produce a step-by-step implementation plan. Include:\n"
        "- Files to create/modify\n"
        "- Key functions and classes\n"
        "- Dependencies\n"
        "- Testing approach\n"
        "- Estimated effort"
    )

    try:
        state["implementation_plan"] = llm_generate(prompt, module="implementation_plan")
        print(f"[GRAPH NODE] generate_implementation_plan → {len(state['implementation_plan'])} chars")
    except Exception as e:
        state["implementation_plan"] = f"Implementation plan generation failed: {e}"
        state["status"] = "failed"
        state["error"] = str(e)

    return state


def node_user_approval(state: Sol0monState) -> Sol0monState:
    """Node: check for user approval (gate before execution).

    In production, this would post to Telegram or a webhook and await a response.
    For MVP, we store the plan and mark status as awaiting_approval.
    """
    if state["workflow_type"] != "IMPLEMENTATION":
        state["user_approved"] = True  # Non-implementation workflows auto-approve
        return state

    state["user_approved"] = False  # Requires explicit user approval
    state["status"] = "awaiting_approval"
    print("[GRAPH NODE] user_approval → awaiting user confirmation")
    return state


def node_execute_implementation(state: Sol0monState) -> Sol0monState:
    """Node: invoke Claude Code with the implementation plan.

    Only runs if user_approved and workflow_type == IMPLEMENTATION.
    """
    if not state.get("user_approved"):
        return state

    if state["workflow_type"] != "IMPLEMENTATION":
        return state

    from implementation_executor import execute_implementation
    state["status"] = "implementing"

    try:
        result = execute_implementation(
            plan=state["implementation_plan"],
            workspace_id=state.get("workspace_id", ""),
        )
        state["code_output"] = result.get("output", "")
        state["test_results"] = result.get("test_results", {})
        state["repair_attempts"] = result.get("repair_attempts", 0)
        state["status"] = "completed" if result.get("success") else "failed"
        if not result.get("success"):
            state["error"] = result.get("error", "Implementation failed")
        print(f"[GRAPH NODE] execute_implementation → success={result.get('success')} "
              f"repairs={result.get('repair_attempts', 0)}")
    except Exception as e:
        state["status"] = "failed"
        state["error"] = str(e)

    return state


# ---------------------------------------------------------------------------
# Routing functions (conditional edges)
# ---------------------------------------------------------------------------


def route_after_classify(state: Sol0monState) -> str:
    """After classification, all workflows go to debate."""
    return "debate"


def route_after_debate(state: Sol0monState) -> str:
    """After debate, route based on workflow type."""
    if state.get("status") == "failed":
        return END
    wf = state.get("workflow_type", "DISCUSSION")
    if wf in ("DISCUSSION", "RESEARCH"):
        return "synthesize"
    if wf == "ARCHITECTURE":
        return "synthesize"  # Architecture workflows end after synthesis
    if wf == "IMPLEMENTATION":
        return "generate_implementation_plan"
    return "synthesize"


def route_after_plan(state: Sol0monState) -> str:
    """After implementation plan, check user approval."""
    if state.get("status") == "failed":
        return END
    return "user_approval"


def route_after_approval(state: Sol0monState) -> str:
    """After user approval check, either execute or wait."""
    if state.get("user_approved"):
        return "execute_implementation"
    return END  # Awaiting user approval — graph exits, can be resumed


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_sol0mon_graph() -> StateGraph:
    """Build the Sol0mon LangGraph workflow.

    Graph structure:

        START
          ↓
        classify_workflow
          ↓
        create_workspace
          ↓
        debate
          ↓
        [DISCUSSION/RESEARCH/ARCH] → synthesize → END
        [IMPLEMENTATION] → generate_plan → user_approval
                           → execute_implementation → END
    """
    graph = StateGraph(Sol0monState)

    # Add all nodes
    graph.add_node("classify_workflow", node_classify_workflow)
    graph.add_node("create_workspace", node_create_workspace)
    graph.add_node("debate", node_debate)
    graph.add_node("synthesize", node_synthesize)
    graph.add_node("generate_implementation_plan", node_generate_implementation_plan)
    graph.add_node("user_approval", node_user_approval)
    graph.add_node("execute_implementation", node_execute_implementation)

    # Edges
    graph.add_edge(START, "classify_workflow")
    graph.add_edge("classify_workflow", "create_workspace")
    graph.add_edge("create_workspace", "debate")

    # Conditional routing after debate
    graph.add_conditional_edges(
        "debate",
        route_after_debate,
        {
            "synthesize": "synthesize",
            "generate_implementation_plan": "generate_implementation_plan",
            END: END,
        },
    )

    graph.add_edge("synthesize", END)

    # Implementation branch
    graph.add_edge("generate_implementation_plan", "user_approval")
    graph.add_conditional_edges(
        "user_approval",
        route_after_approval,
        {
            "execute_implementation": "execute_implementation",
            END: END,
        },
    )
    graph.add_edge("execute_implementation", END)

    return graph


# ---------------------------------------------------------------------------
# Compiled graph with checkpointing
# ---------------------------------------------------------------------------


def get_compiled_graph():
    """Compile and return the graph with checkpoint support.

    The MemorySaver provides checkpointing for crash recovery and resume.
    """
    graph = build_sol0mon_graph()
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_workflow(prompt: str, workspace_id: str = None,
                  thread_id: str = None,
                  approved: bool = False) -> dict:
    """Run a complete Sol0mon workflow via LangGraph.

    Args:
        prompt: The user's request.
        workspace_id: Existing workspace ID (for resume/follow-up).
        thread_id: LangGraph thread ID for state persistence.
        approved: If True, an awaiting_approval workflow will proceed.

    Returns:
        Dict with completed state or "awaiting_approval" status.
    """
    if not LANGGRAPH_AVAILABLE:
        return {
            "status": "langgraph_not_installed",
            "error": "Install langgraph: pip install langgraph",
        }

    state = default_state(
        prompt=prompt,
        workspace_id=workspace_id or "",
        thread_id=thread_id or str(uuid.uuid4()),
    )

    # If resuming with approval, inject it
    if approved:
        state["user_approved"] = True
        state["status"] = "implementing"

    try:
        compiled = get_compiled_graph()
        result = compiled.invoke(
            state,
            config={"configurable": {"thread_id": state["thread_id"]}},
        )

        # Extract relevant fields for the caller
        return {
            "status": result.get("status", "unknown"),
            "workspace_id": result.get("workspace_id", ""),
            "thread_id": result.get("thread_id", ""),
            "workflow_type": result.get("workflow_type", ""),
            "synthesized_answer": result.get("synthesized_answer", ""),
            "consensus_score": result.get("consensus_score", 0),
            "governance_result": result.get("governance_result", {}),
            "implementation_plan": result.get("implementation_plan", ""),
            "user_approved": result.get("user_approved", False),
            "answer_id": result.get("answer_id"),
            "error": result.get("error", ""),
        }
    except Exception as e:
        return {
            "status": "failed",
            "error": str(e),
        }


def resume_workflow(thread_id: str, approved: bool = True) -> dict:
    """Resume a workflow that was awaiting user approval.

    Args:
        thread_id: The LangGraph thread ID from the original run_workflow call.
        approved: Whether the user approved the implementation plan.
    """
    if not LANGGRAPH_AVAILABLE:
        return {"status": "langgraph_not_installed"}

    try:
        compiled = get_compiled_graph()

        # Get the checkpointed state
        checkpointer = compiled.checkpointer
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint = checkpointer.get(config)

        if checkpoint is None:
            return {"status": "thread_not_found", "error": f"No checkpoint for thread {thread_id}"}

        state = checkpoint.get("channel_values", {})
        state["user_approved"] = approved

        result = compiled.invoke(state, config=config)
        return {
            "status": result.get("status", "unknown"),
            "synthesized_answer": result.get("synthesized_answer", ""),
            "implementation_plan": result.get("implementation_plan", ""),
            "code_output": result.get("code_output", ""),
            "test_results": result.get("test_results", {}),
            "repair_attempts": result.get("repair_attempts", 0),
            "error": result.get("error", ""),
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


if __name__ == "__main__":
    print("Project Sol0mon — LangGraph Workflow Integration")
    print(f"LangGraph available: {LANGGRAPH_AVAILABLE}")
    if LANGGRAPH_AVAILABLE:
        graph = build_sol0mon_graph()
        print(f"Graph built with {len(graph.nodes) if hasattr(graph, 'nodes') else '?'} nodes")
    else:
        print("Install langgraph: pip install langgraph")