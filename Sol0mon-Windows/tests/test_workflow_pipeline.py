"""
test_workflow_pipeline.py - TEST 1.

Input:  "Build Project Sol0mon MVP"
Expected: Workflow Classifier -> IMPLEMENTATION

Verifies Path A: prompt -> classify -> routing decision.
Tests the rule-based classifier offline (no LLM, no browser).
"""

from __future__ import annotations

import os
import sys

# Make the project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_pipeline as bp
from workflow_classifier import (
    classify, is_implementation, can_execute_code,
    WorkflowType, WorkflowClassification,
)


def test_build_project_sol0mon_mvp_classified_as_implementation() -> None:
    """TEST 1: 'Build Project Sol0mon MVP' -> IMPLEMENTATION."""
    result = classify("Build Project Sol0mon MVP")
    assert result.workflow_type == WorkflowType.IMPLEMENTATION, \
        f"Expected IMPLEMENTATION, got {result.workflow_type}"
    assert result.requires_execution is True
    assert result.requires_implementation_plan is True
    assert result.requires_user_approval is True


def test_pipeline_classify_prompt() -> None:
    """build_pipeline.classify_prompt wraps classify()."""
    result = bp.classify_prompt("Build Project Sol0mon MVP")
    assert result.workflow_type == WorkflowType.IMPLEMENTATION


def test_is_implementation_and_can_execute() -> None:
    assert is_implementation("Build the user auth module") is True
    assert can_execute_code("Build Project Sol0mon MVP") is True
    assert can_execute_code("What is microservices architecture?") is False


def test_other_workflow_types_route_correctly() -> None:
    """Non-implementation prompts should NOT route to execution."""
    assert classify("What is the difference between SQL and NoSQL?").workflow_type \
        == WorkflowType.DISCUSSION
    assert classify("Design a planet-scale consensus platform architecture").workflow_type \
        == WorkflowType.ARCHITECTURE
    assert classify("Research the best database for 10M users").workflow_type \
        == WorkflowType.RESEARCH


def test_pipeline_rejects_non_implementation() -> None:
    """run_build_pipeline returns 'not_implementation' for non-implement prompts."""
    result = bp.run_build_pipeline("What is the difference between SQL and NoSQL?")
    assert result["status"] == "not_implementation"
    assert result["classification"]["workflow_type"] == "DISCUSSION"


def test_has_answer_ref_detection() -> None:
    """Pipeline correctly detects when a prompt references an answer."""
    assert bp.has_answer_ref("Build answer 55") is True
    assert bp.has_answer_ref("answer 42") is True
    assert bp.has_answer_ref("55") is True
    assert bp.has_answer_ref("Build Project Sol0mon MVP") is False
    assert bp.extract_answer_id("Build answer 55") == 55
    assert bp.extract_answer_id("Build Project Sol0mon MVP") is None


if __name__ == "__main__":
    test_build_project_sol0mon_mvp_classified_as_implementation()
    test_pipeline_classify_prompt()
    test_is_implementation_and_can_execute()
    test_other_workflow_types_route_correctly()
    test_pipeline_rejects_non_implementation()
    test_has_answer_ref_detection()
    print("test_workflow_pipeline: ALL PASSED")
