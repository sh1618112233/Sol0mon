"""
test_decision_model_canonical.py - Phase 0 regression: the Canonical Decision
Model prevents the answer-156 divergence class.

The bug (see root-cause report): a verified decision confirmed SQLite as the
persistence layer, yet `decision_renderer._architecture_components` matched
the "postgresql" alias inside the dispute TITLE (which names both options) and
emitted "PostgreSQL primary datastore" as the architecture - a direct
contradiction. FIX 6's `validate_synthesis_consistency` missed it because the
finding used "argues against" rather than a "reject" verb.

Phase 0 establishes the Canonical Decision Model (`decision_model`) as the
single source of truth every downstream consumer MUST read through. These
tests pin the invariant: the rejected option is recognized as rejected and
does NOT leak into the winner tokens; the actual winner is present; a
unification is never misclassified as a rejection.

These are the canonical-accessor tests. The renderer rewire + the
contradiction-validator strengthening (user Phase 1) build on this foundation.
No LLM, no I/O, no live workspace needed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decision_model import (
    decision_records,
    derive_rejected_options,
    rejected_options,
    winner_text,
    winner_tokens,
)


# ---------------------------------------------------------------------------
# The live answer-156 verification_result (canned, so this runs anywhere).
# ---------------------------------------------------------------------------

def _answer156_verification_result() -> dict:
    """The exact verification_result shape of answer 156 in answer_memory -
    5 resolved claims, the first of which is the SQLite-over-PostgreSQL win
    that exposed the bug. Findings quoted verbatim from decisions.json."""
    return {
        "resolved_claims": [
            {
                "normalized_key": "database_technology_selection",
                "dispute": ("Database Technology: GPT selects PostgreSQL for "
                            "relational durability and audit trails, while "
                            "Gemini selects SQLite for local simplicity."),
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 95,
                    "finding": (
                        "Both GPT and Gemini evidence strips explicitly "
                        "conclude that SQLite is the appropriate persistence "
                        "engine for the given constraints (low capital, zero "
                        "budget, single instance). While the dispute "
                        "description claims GPT selected PostgreSQL, the "
                        "provided GPT evidence text argues against PostgreSQL "
                        "for current needs and for SQLite, aligning perfectly "
                        "with Gemini's position."
                    ),
                },
            },
            {
                "normalized_key": "persistence_strategy",
                "dispute": ("Persistence Strategy: GPT specifies a comprehensive "
                            "relational schema for state management, while "
                            "Gemini specifies a simple state-management file or "
                            "local SQLite database."),
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 90,
                    "finding": (
                        "Both parties agree on using SQLite. GPT specifies a "
                        "'lean, workload-driven relational schema' within "
                        "SQLite, while Gemini emphasizes SQLite's ability to "
                        "handle 'relational persistence' without server "
                        "overhead."
                    ),
                },
            },
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }


# ---------------------------------------------------------------------------
# Invariant 1: the rejected option is recognized as rejected.
# ---------------------------------------------------------------------------

def test_rejected_option_is_recognized_for_argues_against_phrasing() -> None:
    """FIX 6 missed this because "argues against" is not a "reject" verb.
    The canonical derivation's enriched grammar must catch it."""
    vr = _answer156_verification_result()
    rec = decision_records(vr)[0]
    assert "postgresql" in rec["rejected_options"], rec["rejected_options"]
    # Direct accessor matches the record.
    assert "postgresql" in rejected_options(vr["resolved_claims"][0])


def test_rejected_option_does_not_leak_into_winner_tokens() -> None:
    """THE contradiction: the rejected option must not appear in winner_tokens,
    because component alias tables (decision_renderer._COMPONENTS) match
    against winner_tokens. If "postgresql" leaked in, the renderer would emit
    PostgreSQL for a SQLite-confirmed decision - exactly the 156 bug."""
    vr = _answer156_verification_result()
    rec = decision_records(vr)[0]
    assert "postgresql" not in rec["winner_tokens"], rec["winner_tokens"]


def test_actual_winner_is_present_in_winner_tokens() -> None:
    """The positive side: the actual winner (SQLite) must be present so the
    renderer can emit it (or classify it generically) when Phase 1 rewires."""
    vr = _answer156_verification_result()
    rec = decision_records(vr)[0]
    assert "sqlite" in rec["winner_tokens"], rec["winner_tokens"]


def test_rejected_option_absent_when_unification() -> None:
    """A unification finding (no explicit rejection) must NOT invent a
    rejected option. False positives here only suppress a spurious component
    - the safe direction - but the canonical model must still be faithful
    to the verifier."""
    vr = {
        "resolved_claims": [{
            "normalized_key": "k", "dispute": "Hybrid split",
            "verification": {
                "status": "RESOLVED", "confidence": 98,
                "finding": ("Resolved through a hybrid split: Edge-Native "
                            "Coordination Plane + Python Execution Layer."),
            },
        }],
    }
    rec = decision_records(vr)[0]
    assert rec["rejected_options"] == [], rec


# ---------------------------------------------------------------------------
# Invariant 2: structured field is preferred over the derivation fallback.
# ---------------------------------------------------------------------------

def test_structured_rejected_options_field_is_preferred() -> None:
    """When the verifier persists `verification.rejected_options` (Phase 0
    option (a)), the accessor returns it verbatim - even if the finding prose
    would derive something different. The structured field is authoritative."""
    claim = {
        "normalized_key": "k", "dispute": "X vs Y",
        "verification": {
            "status": "RESOLVED", "confidence": 99,
            "finding": "Resolved in favor of Y; rejects X.",
            "rejected_options": ["ExplicitlyRejected"],
        },
    }
    assert rejected_options(claim) == ["explicitlyrejected"]


def test_fallback_used_when_structured_field_absent() -> None:
    """Legacy answers (pre-Phase-0, e.g. 156) carry no structured field. The
    accessor must still return the rejected options via the shared
    derivation - same tokens the verifier would persist for new answers."""
    claim = {
        "normalized_key": "k", "dispute": "PostgreSQL vs SQLite",
        "verification": {
            "status": "RESOLVED", "confidence": 99,
            "finding": "Resolved in favor of SQLite; rejects PostgreSQL.",
        },
    }
    assert rejected_options(claim) == ["postgresql"]


def test_fallback_used_when_structured_field_empty() -> None:
    """An empty structured field (LLM returned []) is treated as absent so
    the derivation can recover the rejected options from the finding."""
    claim = {
        "normalized_key": "k", "dispute": "PostgreSQL vs SQLite",
        "verification": {
            "status": "RESOLVED", "confidence": 99,
            "finding": "Resolved in favor of SQLite; rejects PostgreSQL.",
            "rejected_options": [],
        },
    }
    assert rejected_options(claim) == ["postgresql"]


# ---------------------------------------------------------------------------
# Invariant 3: the live answer-156 contradiction class is closed end-to-end.
# ---------------------------------------------------------------------------

def test_answer156_no_postgresql_in_any_winner_tokens() -> None:
    """The strongest form of the invariant: across EVERY verified decision in
    the 156 result, PostgreSQL never appears in winner_tokens. Before Phase 0,
    `decision_renderer._architecture_components` emitted "PostgreSQL primary
    datastore" for this exact input. After Phase 1 rewires the renderer to
    `winner_tokens`, this test guarantees the contradiction cannot recur."""
    vr = _answer156_verification_result()
    for rec in decision_records(vr):
        assert "postgresql" not in rec["winner_tokens"], rec
    # sanity: the records are populated
    assert len(decision_records(vr)) == 2


# ---------------------------------------------------------------------------
# Invariant 4: `derive_rejected_options` is the shared source the verifier
# uses to persist the field. Pin the contract so the verifier and the
# accessor cannot drift.
# ---------------------------------------------------------------------------

def test_derive_rejected_options_handles_reject_verb() -> None:
    finding = "Resolved in favor of server-side authoritative storage; rejects IndexedDB."
    assert "indexeddb" in derive_rejected_options(finding)


def test_derive_rejected_options_handles_argues_against() -> None:
    finding = ("The evidence argues against PostgreSQL for current needs and "
               "for SQLite.")
    assert "postgresql" in derive_rejected_options(finding)


def test_derive_rejected_options_handles_rules_out() -> None:
    finding = "The verifier rules out WebContainers for the authoritative path."
    assert "webcontainers" in derive_rejected_options(finding)


def test_derive_rejected_options_empty_for_unification() -> None:
    finding = "Both parties converge on a hybrid split combining X and Y."
    assert derive_rejected_options(finding) == []


def test_derive_rejected_options_empty_for_empty_finding() -> None:
    assert derive_rejected_options("") == []
    assert derive_rejected_options(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Invariant 5: the accessor never reads the dispute title for the winner.
# ---------------------------------------------------------------------------

def test_winner_tokens_exclude_dispute_only_options() -> None:
    """The dispute title names BOTH options. A token that appears ONLY in the
    dispute title (not in the finding) must not surface as a winner token - it
    is just as likely the rejected option."""
    claim = {
        "normalized_key": "k",
        "dispute": "Database Technology: GPT selects PostgreSQL for relational durability, while Gemini selects SQLite for local simplicity.",
        "verification": {
            "status": "RESOLVED", "confidence": 95,
            "finding": ("Both parties agree on using SQLite for the given "
                        "constraints."),
        },
    }
    wtokens = winner_tokens(claim)
    assert "sqlite" in wtokens
    # "postgresql" appears only in the dispute title, not in the finding's
    # winner portion. It must NOT surface as a winner token.
    assert "postgresql" not in wtokens
    assert winner_text(claim)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
