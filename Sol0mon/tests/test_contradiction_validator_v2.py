"""
test_contradiction_validator_v2.py — Bug: validate_synthesis_consistency()
false positives.

Debate #120 produced 3 false positives because ordinary English words
appearing in verifier rationale ("architectural", "fundamental",
"implementation") were extracted as if they were architecture options. They
are NOT options — they are adjectives/nouns in the finding prose — so a
consistent narrative that merely uses these words in plain English was
flagged as contradicting the verifier.

FIX GOAL
--------
The validator must only compare ACTUAL disputed choices. Candidate rejected
options are extracted ONLY from:
  1. the dispute title  (e.g. "SQLite vs PostgreSQL")
  2. the explicit verifier finding clauses ("resolved in favor of X",
     "rejects Y").
Generic rationale words are never treated as options.

REGRESSION
----------
Recommending the ACTUAL rejected option (e.g. "SQLite") and negating the
ACTUAL winner (e.g. "PostgreSQL") are still flagged.

No model calls: validate_synthesis_consistency is a pure heuristic, and the
synthesis path exercised here is the deterministic (no-narrative_fn) one.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import synthesis_engine
from decision_renderer import (
    validate_synthesis_consistency,
    contradiction_warning,
    render_narrative_sections,
)


# --------------------------------------------------------------------------- #
# Debate #120 fixture — realistic findings whose prose contains the words    #
# that used to trigger false positives ("architectural", "fundamental",      #
# "implementation"). The actual disputed choices are still named explicitly   #
# in the dispute title and the finding's "in favor of"/"rejects" clauses.     #
# --------------------------------------------------------------------------- #

def debate_120_fixture():
    """Verification result whose finding prose contains ordinary English words
    ("architectural", "fundamental", "implementation") that must NOT be treated
    as architecture options. The real options are named in the dispute title and
    in the explicit finding clauses."""
    verification_result = {
        "resolved_claims": [
            {
                "dispute": "Primary Database: PostgreSQL vs SQLite",
                "normalized_key": "primary_database",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 95,
                    "finding": (
                        "Resolved in favor of PostgreSQL primary datastore; "
                        "rejects SQLite for shared production persistence."
                    ),
                },
            },
            {
                "dispute": "Execution Isolation: Docker vs WebContainers vs Firecracker",
                "normalized_key": "execution_isolation",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 93,
                    "finding": (
                        "Resolved in favor of an architectural split: Docker "
                        "workers for low-risk jobs and Firecracker microVMs for "
                        "high-risk jobs. Rejects WebContainers for authoritative "
                        "execution."
                    ),
                },
            },
            {
                "dispute": "Cost Model: BYOK vs platform-only keys",
                "normalized_key": "cost_model",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 90,
                    "finding": (
                        "Resolved in favor of BYOK as a fundamental design "
                        "decision for cost containment; rejects platform-only "
                        "keys for all tenants."
                    ),
                },
            },
            {
                "dispute": "Storage Strategy: Cloudflare R2 vs local-only storage",
                "normalized_key": "storage_strategy",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 94,
                    "finding": (
                        "Resolved in favor of a pragmatic implementation "
                        "strategy: Cloudflare R2 immutable storage plus a local "
                        "scratch filesystem; rejects local-only artifact storage."
                    ),
                },
            },
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
    }
    governance_context = {
        "verified_decisions": [],
        "verified_tradeoffs": [],
        "human_required": [],
    }
    return verification_result, governance_context


def _debate_120_consistent_narrative() -> str:
    """A narrative CONSISTENT with every Debate #120 verdict: honours each
    winner, rejects each rejected option, and uses the generic rationale
    words ("architectural", "fundamental", "implementation") in ordinary
    English right next to a negate phrase — the exact shape that used to trip
    a false WINNER_REJECTED."""
    return (
        "# Architecture\n"
        "We avoid architectural drift across the execution layers; Docker and "
        "Firecracker are our runtimes, WebContainers is off-limits. PostgreSQL "
        "is our primary datastore. We avoid fundamental cost shocks by using "
        "BYOK; platform-only keys are off-limits. We avoid implementation "
        "complexity by using Cloudflare R2 plus a local scratch filesystem; "
        "local-only artifact storage is off-limits.\n"
        "# Implementation Plan\n"
        "Deploy PostgreSQL, then Temporal-free orchestration on Docker, then "
        "Cloudflare R2 and local scratch, then BYOK routing.\n"
    )


# --------------------------------------------------------------------------- #
# NEW / FIX — Debate #120 returns no issues                                    #
# --------------------------------------------------------------------------- #

def test_debate_120_returns_no_issues() -> None:
    """The headline fix: Debate #120 must produce issues == []. The narrative
    honours every winner and rejects every rejected option; it merely uses
    'architectural', 'fundamental', and 'implementation' as ordinary English,
    which must NOT be mistaken for architecture options."""
    vr, gc = debate_120_fixture()
    narrative = _debate_120_consistent_narrative()
    issues = validate_synthesis_consistency(narrative, vr, gc)
    assert issues == [], (
        "expected no contradictions for a consistent Debate #120 narrative; "
        f"got: {[(i['type'], i['option']) for i in issues]}"
    )
    assert contradiction_warning(issues) == ""


def test_debate_120_consistent_synthesized_answer_has_no_warning() -> None:
    """End-to-end: the deterministic Debate #120 answer carries no
    contradiction warning, and its narrative Sections are internally consistent
    (they are rendered straight from the verified model)."""
    vr, gc = debate_120_fixture()
    deterministic_narrative = render_narrative_sections(vr, gc)
    assert validate_synthesis_consistency(deterministic_narrative, vr, gc) == []

    answer = synthesis_engine.synthesize_answer(
        question="Debate #120",
        gpt_final="",
        gemini_final="",
        validated_disputes=[],
        consensus_score=100,
        verification_result=vr,
        governance_context=gc,
    )
    assert "# Contradiction Warning" not in answer


# --------------------------------------------------------------------------- #
# REGRESSION — actual disputed choices are still compared                     #
# --------------------------------------------------------------------------- #

def test_actual_rejected_option_recommendation_still_flagged() -> None:
    """Regression: recommending the ACTUAL rejected option (SQLite — named in
    the dispute title and in the finding's 'rejects' clause) is still caught."""
    vr, gc = debate_120_fixture()
    narrative = "# Architecture\nUse SQLite as primary datastore.\n"
    issues = validate_synthesis_consistency(narrative, vr, gc)
    assert issues, "expected a contradiction when the rejected option SQLite is recommended"
    caught = {(i["type"], i["normalized_key"], i["option"]) for i in issues}
    assert ("REJECTED_OPTION_RECOMMENDED", "primary_database", "sqlite") in caught, caught
    assert contradiction_warning(issues)


def test_winner_negation_still_flagged() -> None:
    """Regression: negating the ACTUAL winner (PostgreSQL) still produces a
    WINNER_REJECTED contradiction."""
    vr, gc = debate_120_fixture()
    narrative = "# Architecture\nAvoid PostgreSQL; it is off-limits.\n"
    issues = validate_synthesis_consistency(narrative, vr, gc)
    assert issues, "expected a contradiction when the winner PostgreSQL is negated"
    caught = {(i["type"], i["normalized_key"], i["option"]) for i in issues}
    assert ("WINNER_REJECTED", "primary_database", "postgresql") in caught, caught


def test_webcontainers_recommendation_still_flagged() -> None:
    """Regression: recommending a different actual rejected option
    (WebContainers) is still caught."""
    vr, gc = debate_120_fixture()
    narrative = "# Architecture\nUse WebContainers for authoritative execution.\n"
    issues = validate_synthesis_consistency(narrative, vr, gc)
    assert issues
    caught = {(i["type"], i["option"]) for i in issues}
    assert any("webcontainers" in opt for _t, opt in caught), caught


# --------------------------------------------------------------------------- #
# No model calls                                                               #
# --------------------------------------------------------------------------- #

def test_validator_makes_no_model_calls(monkeypatch) -> None:
    """validate_synthesis_consistency and the deterministic synthesis path must
    never call a model. Any imported 'generate' is replaced with a hard fail."""
    monkeypatch.setattr(synthesis_engine, "generate", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("generate() must not be called")
    ))
    vr, gc = debate_120_fixture()
    narrative = _debate_120_consistent_narrative()

    # Direct validator call: pure function, no generate().
    assert validate_synthesis_consistency(narrative, vr, gc) == []

    # Deterministic synthesis path: no narrative_fn -> no generate().
    answer = synthesis_engine.synthesize_answer(
        question="Debate #120",
        gpt_final="",
        gemini_final="",
        validated_disputes=[],
        consensus_score=100,
        verification_result=vr,
        governance_context=gc,
    )
    assert "# Contradiction Warning" not in answer


if __name__ == "__main__":
    test_debate_120_returns_no_issues()
    test_debate_120_consistent_synthesized_answer_has_no_warning()
    test_actual_rejected_option_recommendation_still_flagged()
    test_winner_negation_still_flagged()
    test_webcontainers_recommendation_still_flagged()
    print("test_contradiction_validator_v2: ALL PASSED (run pytest for the monkeypatch test)")
