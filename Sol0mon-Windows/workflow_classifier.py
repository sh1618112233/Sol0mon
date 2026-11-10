"""
Project Sol0mon — Workflow Classifier.

Every user request is classified into one of four workflow types before
entering the pipeline. This determines which stages run and whether
implementation may proceed.

Workflow types:
    DISCUSSION    → Research → Debate → Consensus → Answer
    RESEARCH      → Research → Debate → Consensus → Research Output
    ARCHITECTURE  → Research → Debate → Verification → Governance →
                    Consensus → Architecture Output
    IMPLEMENTATION → Research → Debate → Verification → Governance →
                     Consensus → Synthesis → Implementation Plan →
                     User Approval → Execution

Only IMPLEMENTATION workflows may invoke Claude Code for autonomous execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from cerebras_client import generate as llm_generate  # routed through provider_router
from debate_engine import _extract_json

# ---------------------------------------------------------------------------
# Classification constants
# ---------------------------------------------------------------------------


class WorkflowType:
    DISCUSSION = "DISCUSSION"
    RESEARCH = "RESEARCH"
    ARCHITECTURE = "ARCHITECTURE"
    IMPLEMENTATION = "IMPLEMENTATION"


# Classification keywords (rule-based pre-filter before LLM classification)
DISCUSSION_KEYWORDS = [
    "what is", "explain", "describe", "tell me about", "how does",
    "opinion", "compare", "difference between", "pros and cons",
    "discuss", "elaborate", "thoughts on",
]

RESEARCH_KEYWORDS = [
    "research", "investigate", "study the", "survey", "literature",
    "state of the art", "best practices", "benchmark", "comparison of",
    "evaluate options", "analyze the", "feasibility", "landscape",
    "explore", "gather evidence", "findings", "what are the options",
]

ARCHITECTURE_KEYWORDS = [
    "design a", "design the", "architecture for", "system design",
    "component", "how should we", "how would you design",
    "recommend a", "tradeoffs", "which technology",
    "what stack", "database choice", "orchestration",
    "deploy", "infrastructure", "scale", "scalable",
    "pattern", "pipeline for data", "workflow for",
    "should we use", "plan for", "propose",
]

IMPLEMENTATION_KEYWORDS = [
    "implement", "code the", "build", "create the",
    "write the", "write code", "generate code", "produce the",
    "fix the", "refactor the", "deploy to", "ship the",
    "make it work", "execute the", "deliver the",
    "proceed with implementation", "start coding",
    "develop the", "construct", "set up the",
    "ci/cd", "CI/CD", "devops pipeline",
]


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


@dataclass
class WorkflowClassification:
    """Structured classification of a user request."""

    workflow_type: str  # DISCUSSION | RESEARCH | ARCHITECTURE | IMPLEMENTATION
    confidence: float  # 0.0 – 1.0
    reasoning: str = ""

    # Allowed downstream stages
    requires_research: bool = True
    requires_architecture: bool = False
    requires_verification: bool = False
    requires_governance: bool = False
    requires_synthesis: bool = False
    requires_implementation_plan: bool = False
    requires_user_approval: bool = False
    requires_execution: bool = False

    def to_dict(self) -> dict:
        return {
            "workflow_type": self.workflow_type,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "requires_research": self.requires_research,
            "requires_architecture": self.requires_architecture,
            "requires_verification": self.requires_verification,
            "requires_governance": self.requires_governance,
            "requires_synthesis": self.requires_synthesis,
            "requires_implementation_plan": self.requires_implementation_plan,
            "requires_user_approval": self.requires_user_approval,
            "requires_execution": self.requires_execution,
        }


# ---------------------------------------------------------------------------
# Default stage configurations per workflow type
# ---------------------------------------------------------------------------

WORKFLOW_STAGES: dict[str, dict[str, bool]] = {
    WorkflowType.DISCUSSION: {
        "requires_research": True,
        "requires_architecture": False,
        "requires_verification": False,
        "requires_governance": False,
        "requires_synthesis": True,
        "requires_implementation_plan": False,
        "requires_user_approval": False,
        "requires_execution": False,
    },
    WorkflowType.RESEARCH: {
        "requires_research": True,
        "requires_architecture": False,
        "requires_verification": False,
        "requires_governance": False,
        "requires_synthesis": True,
        "requires_implementation_plan": False,
        "requires_user_approval": False,
        "requires_execution": False,
    },
    WorkflowType.ARCHITECTURE: {
        "requires_research": True,
        "requires_architecture": True,
        "requires_verification": True,
        "requires_governance": True,
        "requires_synthesis": True,
        "requires_implementation_plan": False,
        "requires_user_approval": False,
        "requires_execution": False,
    },
    WorkflowType.IMPLEMENTATION: {
        "requires_research": True,
        "requires_architecture": True,
        "requires_verification": True,
        "requires_governance": True,
        "requires_synthesis": True,
        "requires_implementation_plan": True,
        "requires_user_approval": True,
        "requires_execution": True,
    },
}


# ---------------------------------------------------------------------------
# Rule-based classification (fast pre-filter)
# ---------------------------------------------------------------------------


def _rule_based_classify(prompt: str) -> str | None:
    """Quick keyword-based classification. Returns None if ambiguous."""
    prompt_lower = prompt.lower()

    # Count keyword matches per type
    scores = {
        WorkflowType.DISCUSSION: 0,
        WorkflowType.RESEARCH: 0,
        WorkflowType.ARCHITECTURE: 0,
        WorkflowType.IMPLEMENTATION: 0,
    }

    for kw in DISCUSSION_KEYWORDS:
        if kw in prompt_lower:
            scores[WorkflowType.DISCUSSION] += 1
    for kw in RESEARCH_KEYWORDS:
        if kw in prompt_lower:
            scores[WorkflowType.RESEARCH] += 1
    for kw in ARCHITECTURE_KEYWORDS:
        if kw in prompt_lower:
            scores[WorkflowType.ARCHITECTURE] += 1
    for kw in IMPLEMENTATION_KEYWORDS:
        if kw in prompt_lower:
            scores[WorkflowType.IMPLEMENTATION] += 1

    # Find the winner
    best_type = max(scores, key=lambda k: scores[k])
    best_score = scores[best_type]

    if best_score == 0:
        return None  # No keywords matched — need LLM

    # Check if the winner is clearly ahead
    runner_up = max(scores[k] for k in scores if k != best_type)
    if best_score >= 2 and best_score >= runner_up + 2:
        return best_type  # Clear winner (strong signal)
    if best_score >= 1 and runner_up == 0:
        return best_type  # Only one type matched at all

    return None  # Ambiguous — need LLM


# ---------------------------------------------------------------------------
# LLM-based classification
# ---------------------------------------------------------------------------


CLASSIFICATION_PROMPT = """Classify this user request into exactly one workflow type.

Types:
- DISCUSSION: A question, comparison, or exploration — no design or implementation
- RESEARCH: An investigation or study — requires evidence gathering
- ARCHITECTURE: A design question — requires architectural decision-making
- IMPLEMENTATION: A request to build, code, or implement something

Return JSON only:
{"workflow_type": "DISCUSSION|RESEARCH|ARCHITECTURE|IMPLEMENTATION",
 "confidence": 0.0-1.0,
 "reasoning": "brief explanation"}

REQUEST:
{prompt}"""


def _llm_classify(prompt: str) -> WorkflowClassification:
    """Use the provider router to classify the prompt."""
    try:
        raw = llm_generate(
            CLASSIFICATION_PROMPT.format(prompt=prompt[:3000]),
            module="workflow_classifier",
        )
        result = _extract_json(raw)
        if isinstance(result, dict):
            wf_type = result.get("workflow_type", WorkflowType.DISCUSSION)
            # Normalize
            if wf_type not in (
                WorkflowType.DISCUSSION,
                WorkflowType.RESEARCH,
                WorkflowType.ARCHITECTURE,
                WorkflowType.IMPLEMENTATION,
            ):
                wf_type = WorkflowType.DISCUSSION
            confidence = float(result.get("confidence", 0.5))
            reasoning = result.get("reasoning", "")
            return _build_classification(wf_type, confidence, reasoning)
    except Exception as e:
        print(f"[WORKFLOW CLASSIFIER] LLM classification failed: {e} — defaulting to DISCUSSION")

    return _build_classification(WorkflowType.DISCUSSION, 0.5,
                                  "LLM classification failed — defaulting")


def _build_classification(wf_type: str, confidence: float,
                           reasoning: str) -> WorkflowClassification:
    """Build a full classification with stage configuration."""
    stages = WORKFLOW_STAGES.get(wf_type, WORKFLOW_STAGES[WorkflowType.DISCUSSION])
    return WorkflowClassification(
        workflow_type=wf_type,
        confidence=confidence,
        reasoning=reasoning,
        **stages,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(prompt: str) -> WorkflowClassification:
    """Classify a user request into a workflow type.

    Uses rule-based matching first (fast, no API call). Falls back to
    LLM classification via the provider router only when ambiguous.

    Returns a WorkflowClassification with the type and enabled stages.
    """
    # Rule-based pre-filter
    rule_result = _rule_based_classify(prompt)
    if rule_result is not None:
        return _build_classification(rule_result, 0.85,
                                      f"Rule-based classification: {rule_result}")

    # LLM fallback
    return _llm_classify(prompt)


def is_implementation(prompt: str) -> bool:
    """Quick check: is this an implementation request?"""
    result = classify(prompt)
    return result.workflow_type == WorkflowType.IMPLEMENTATION


def can_execute_code(prompt: str) -> bool:
    """Only IMPLEMENTATION workflows may invoke Claude Code for execution."""
    classification = classify(prompt)
    return (classification.workflow_type == WorkflowType.IMPLEMENTATION
            and classification.requires_execution)


if __name__ == "__main__":
    test_prompts = [
        "What is the difference between SQL and NoSQL?",
        "Research the best database for a social media platform with 10M users",
        "Design a planet-scale consensus platform architecture",
        "Implement the user authentication module for the consensus platform",
    ]

    for p in test_prompts:
        result = classify(p)
        print(f"\nPrompt: {p[:80]}...")
        print(f"  Type: {result.workflow_type} (confidence={result.confidence:.2f})")
        print(f"  Reasoning: {result.reasoning}")
        print(f"  Execution allowed: {result.requires_execution}")