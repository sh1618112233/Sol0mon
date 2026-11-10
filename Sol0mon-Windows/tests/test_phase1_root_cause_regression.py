"""
test_phase1_root_cause_regression.py - Phase 1: pin the elimination of the
answer-156 SQLite/PostgreSQL contradiction class.

ROOT CAUSE (per the investigation report):
   answer.md says SQLite         <- verifier winner
   architecture says PostgreSQL   <- decision_renderer._architecture_components
                                      read the dispute title (which names BOTH
                                      options) and matched the 'postgresql' alias
                                      inside it. The actual winner was SQLite.
   implementation deploys PostgreSQL
   execution_success = true       <- no artefact, no tests, but "no failure"

Phase 1 FIX:
  1. decision_model is the SOLE OWNER of decision interpretation. The
     winner-phrase parser, the tokens tokenizer, and the reject-signal grammar
     live in `decision_model` and are imported (re-exported) by
     `decision_renderer`. There is no second parsing copy.
  2. `_architecture_components` is rewired to consume `decision_model`:
     platform aliases match against `winner_text(claim)` (text within the
     pre-reject portion only), and any component whose alias appears in
     `rejected_options(claim)` is suppressed. Defense-in-depth + winner-side-
     only matching makes a rejected option impossible to emit as the
     architecture - the contradiction class is ELIMINATED, not just detected.
  3. `validate_synthesis_consistency` reads rejected tokens via
     `decision_model.rejected_options(claim)` (enriched grammar), so a
     finding phrased as "argues *against* X" / "rules out X" / "discards X"
     produces the rejected tokens. A narrative that then recommends them
     ("Use X", "Adopt X", "Recommend X") is flagged.

This test file's contract:
  - With the live answer-156 verification_result, `render_architecture` MUST
    NOT contain "PostgreSQL". Eliminated at the source — no PostgreSQL can
    ever appear in the deterministic architecture for that input.
  - The 156 winner/rejected interpretation comes ONLY from decision_model:
    `winner_tokens` excludes PostgreSQL, `rejected_options` lists PostgreSQL.
  - The canonical decision_model is the SOLE OWNER: importing this module
    must not pull in decision_renderer (the back-compat shim inverts that
    dependency - decision_renderer imports from decision_model, never vice
    versa).
  - The validator, fed a narrative that recommends the rejected option
    ("Use PostgreSQL ..."), flags it - defense-in-depth catches a future
    renderer regression that re-introduces the contradiction.

These tests cap the regression that produced answer 156. No LLM, no I/O.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decision_model import (
    FIELDS,
    _strip_emphasis,
    decision_records,
    derive_rejected_options,
    rejected_options,
    winner_text,
    winner_tokens,
)
from decision_renderer import (
    render_architecture,
    render_implementation_plan,
    render_migration_path,
    render_narrative_sections,
    render_risks,
    validate_synthesis_consistency,
)


# ---------------------------------------------------------------------------
# Live answer-156 verification_result (canned, deterministic; runs anywhere).
# Findings quoted verbatim from the live workspace
# workspaces/workspace_f334026c-.../decisions.json.
# ---------------------------------------------------------------------------


def answer156_verification_result() -> dict:
    """The exact verification_result of the live answer 156 that surfaced the
    bug. The finding phrasing uses markdown emphasis ('argues *against*') that
    the legacy reject-verb-only grammar missed."""
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
                        "provided GPT evidence text argues *against* PostgreSQL "
                        "for current needs and *for* SQLite, aligning perfectly "
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
                        "handle 'relational persistence' without server overhead."
                    ),
                },
            },
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }


_GC = {"verified_decisions": [], "verified_tradeoffs": [], "human_required": []}


# ---------------------------------------------------------------------------
# 1. The architecture renderer cannot emit the rejected option.
# ---------------------------------------------------------------------------


def test_render_architecture_does_not_emit_postgresql_for_156() -> None:
    """The primary root-cause contract: `render_architecture` for the live
    answer-156 verification_result MUST NOT contain "PostgreSQL". The verifier
    chose SQLite; the renderer must not surface the rejected option as the
    architecture. Before Phase 1 the renderer did — that was the bug."""
    vr = answer156_verification_result()
    arch = render_architecture(vr, _GC)
    assert "postgresql" not in arch.lower(), (
        "Phase 1 regression: render_architecture emitted PostgreSQL for an "
        f"answer-156 verification_result that chose SQLite. Got:\n{arch}")
    # And the deterministic narrative sections derived from the same model.
    plan = render_implementation_plan(vr, _GC)
    assert "postgresql" not in plan.lower(), (
        "Phase 1 regression: render_implementation_plan emitted PostgreSQL "
        f"for the 156 result. Got:\n{plan}")
    risks = render_risks(vr, _GC)
    assert "postgresql" not in risks.lower(), (
        "Phase 1 regression: render_risks emitted PostgreSQL for the 156 "
        f"result. Got:\n{risks}")
    mig = render_migration_path(vr, _GC)
    assert "postgresql" not in mig.lower(), (
        "Phase 1 regression: render_migration_path emitted PostgreSQL for the "
        f"156 result. Got:\n{mig}")
    narrative = render_narrative_sections(vr, _GC)
    assert "postgresql" not in narrative.lower(), (
        "Phase 1 regression: render_narrative_sections emitted PostgreSQL for "
        f"the 156 result. Got:\n{narrative}")


def test_render_architecture_for_156_emits_no_components() -> None:
    """The 156 finding's winner portion is prose ("Both GPT and Gemini evidence
    strips explicitly conclude that SQLite is the appropriate persistence
    engine for the given constraints (low capital, zero budget, single
    instance)") - it cannot be classified as a clean platform component
    label. Phase 1 makes the renderer emit "No verified architectural
    decisions recorded." for this input rather than the false-positive
    PostgreSQL platform component the legacy `_source_text`-based matcher
    produced. Either no-component or a SQLite-derived generic component is
    acceptable; PostgreSQL is not (asserted above)."""
    vr = answer156_verification_result()
    arch = render_architecture(vr, _GC)
    assert "# Architecture" in arch
    # "No verified architectural decisions recorded." OR a clean SQLite-bullet
    # are the only acceptable renderings.
    assert ("No verified architectural decisions recorded." in arch
            or "sqlite" in arch.lower()), arch


# ---------------------------------------------------------------------------
# 2. The canonical decision_model is the SOLE OWNER of interpretation.
# ---------------------------------------------------------------------------


def test_decision_model_does_not_import_decision_renderer() -> None:
    """The canonical model MUST not depend on decision_renderer. Before Phase 1
    decision_model imported the parsing primitives FROM decision_renderer; the
    two copies could then drift (which is the root cause of the divergence
    class). Phase 1 inverts the dependency: decision_renderer imports from
    decision_model, never the reverse. Imported 'decision_renderer' would be
    a hard regression.

    This test runs in a fresh subprocess so the assertions in this file (which
    import decision_renderer themselves for the renderer regressions) don't
    pollute the module set under inspection."""
    import subprocess
    code = (
        "import sys, decision_model as dm\n"
        "assert 'decision_renderer' not in sys.modules, (\n"
        "    'decision_model pulled decision_renderer into sys.modules - Phase 1 '\n"
        "    'inversion regressed; decision_model must be the sole owner')\n"
        "assert callable(dm.winner_text) and callable(dm.rejected_options)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"decision_model imports decision_renderer indirectly. stderr:\n"
        f"{result.stderr}")
    assert "OK" in result.stdout, result.stdout


def test_decision_records_expose_canonical_contract() -> None:
    """`decision_records(vr)` returns one record per RESOLVED claim, each
    exposing the FIELDS contract. This is the single entry point downstream
    consumers must use."""
    vr = answer156_verification_result()
    recs = decision_records(vr)
    assert len(recs) == 2, recs
    for r in recs:
        assert set(r.keys()) == set(FIELDS), r.keys()
        assert r["normalized_key"]
        assert r["dispute"]
        assert r["status"] == "RESOLVED"
        assert isinstance(r["confidence"], int)
        assert isinstance(r["winner_text"], str)
        assert isinstance(r["winner_tokens"], list)
        assert isinstance(r["rejected_options"], list)


def test_winner_text_uses_winner_portion_only_never_dispute_title() -> None:
    """For 156's database_technology_selection claim, `winner_text` must not
    contain "PostgreSQL" — that name appears in the dispute title (which names
    BOTH options) and the rejected portion of the finding ("argues
    *against* PostgreSQL"). Phase 1's winner-side-only slice (`pre_s[start:]`,
    never `s[start:]`) keeps the winner portion within the pre-reject text,
    so a rejected option can never leak into `winner_text`."""
    vr = answer156_verification_result()
    claim = vr["resolved_claims"][0]
    wtext = winner_text(claim)
    assert "postgresql" not in wtext.lower(), (
        f"Phase 1 regression: winner_text leaked the rejected option: {wtext!r}")


def test_winner_tokens_exclude_postgresql_for_156() -> None:
    """The strict contract: `winner_tokens` for the 156 database_technology
    claim MUST NOT contain 'postgresql'. Platform component alias tables
    match against this list — a leak here re-opens the architecture
    contradiction."""
    vr = answer156_verification_result()
    claim = vr["resolved_claims"][0]
    wtokens = winner_tokens(claim)
    assert "postgresql" not in wtokens, wtokens


def test_rejected_options_list_postgresql_for_156() -> None:
    """For 156's database_technology_selection claim, `rejected_options` MUST
    list "postgresql". The enriched grammar (`_REJECT_SIGNALS_RE`) plus
    Markdown emphasis stripping (`_strip_emphasis`) recognize the finding's
    "argues *against* PostgreSQL" phrasing; the legacy reject-verb-only split
    missed it."""
    vr = answer156_verification_result()
    claim = vr["resolved_claims"][0]
    ropts = rejected_options(claim)
    assert "postgresql" in ropts, ropts


# ---------------------------------------------------------------------------
# 3. The validator (defense-in-depth) catches a future renderer regression
#    that re-introduces the contradiction.
# ---------------------------------------------------------------------------


def test_validator_flags_narrative_recommending_postgresql_for_156() -> None:
    """If anything ever re-introduces the 156 architecture ("Use PostgreSQL")
    into a narrative, `validate_synthesis_consistency` must catch it. The
    enriched derive_rejected_options returns ['postgresql'] for this claim,
    so the validator's recommend-context check fires on `Use PostgreSQL`."""
    vr = answer156_verification_result()
    narrative = "# Architecture\nUse PostgreSQL as the primary database.\n"
    contras = validate_synthesis_consistency(narrative, vr, _GC)
    caught = {(c["type"], c["normalized_key"], c["option"]) for c in contras}
    assert ("REJECTED_OPTION_RECOMMENDED",
            "database_technology_selection",
            "postgresql") in caught, (
        f"Phase 1 regression: validator did not flag 'Use PostgreSQL' for the "
        f"156 claim. contradictions={contras}")


# ---------------------------------------------------------------------------
# 4. The enriched-grammar + emphasis-stripping primitives.
# ---------------------------------------------------------------------------


def test_derive_rejected_options_recognizes_argues_against_with_emphasis() -> None:
    """The exact phrasing of answer 156's finding: 'argues *against* X'.
    Markdown emphasis must be stripped before the reject-signal regex
    searches, so the enriced 'argues\\s+against' pattern matches."""
    finding = ("Both evidence strips conclude SQLite is appropriate. The "
               "text argues *against* PostgreSQL for current needs.")
    ro = derive_rejected_options(finding)
    assert "postgresql" in ro, ro


def test_derive_rejected_options_handles_plain_argues_against() -> None:
    finding = "The evidence argues against Memcached as a cache layer."
    assert "memcached" in derive_rejected_options(finding)


def test_derive_rejected_options_handles_rules_out() -> None:
    finding = "Verifier rules out WebContainers for the authoritative path."
    assert "webcontainers" in derive_rejected_options(finding)


def test_derive_rejected_options_handles_reject_verb() -> None:
    """The legacy 'reject' verb family is a subset of the enriched grammar;
    every existing reject-verb finding still yields its rejected tokens."""
    finding = "Resolved in favor of server-side SQLite; rejects IndexedDB."
    assert "indexeddb" in derive_rejected_options(finding)


def test_derive_rejected_options_empty_for_unification() -> None:
    """A unification finding (no reject signal) yields no rejected tokens —
    the canonical model must not invent a rejection (false positives here
    only suppress a spurious component, but faithfulness matters)."""
    finding = ("Resolved through a hybrid split: Edge-Native Coordination "
               "Plane + Python Execution Layer.")
    assert derive_rejected_options(finding) == []


def test_strip_emphasis_removes_markdown_punctuation() -> None:
    """Markdown emphasis stripping must remove * _ ` so negations formatted
    as 'argues *against*' / 'argues _against_' / 'argues `against`' all match
    the plain-grammar pattern."""
    for ch in ("*", "_", "`"):
        s = f"argues {ch}against{ch} PostgreSQL"
        assert _strip_emphasis(s) == "argues against PostgreSQL", s


# ---------------------------------------------------------------------------
# 5. Regression: the existing test fixtures are not corrupted by Phase 1.
# ---------------------------------------------------------------------------


def test_debate55_deterministic_narrative_is_consistent() -> None:
    """Sanity: a debate-55 deterministic narrative is consistent (no
    contradiction). Phase 1 must preserve this contract - the canonical
    accessor + renderer rewire must not introduce false flags on the
    canonical fixture."""
    from test_deterministic_decisions import (
        debate55_verification_result,
        debate55_governance_context,
    )
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    narrative = render_narrative_sections(vr, gc)
    contras = validate_synthesis_consistency(narrative, vr, gc)
    assert contras == [], (
        f"Phase 1 regression on debate-55 deterministic narrative: {contras}")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
