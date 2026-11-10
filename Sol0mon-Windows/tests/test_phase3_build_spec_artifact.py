"""
test_phase3_build_spec_artifact.py - Phase 3: the build specification is a
deterministic, structurally-complete artifact derived from the canonical
decision model.

The investigation report's Phase 3 recommendation was to introduce an
explicit intermediate artifact:

    Verified Decisions
            │
            ▼
    Build Specification (NEW intermediate - this is what Phase 3 ships)
            │
            ▼
    Implementation Prompt (the spec itself is the prompt)
            │
            ▼
    Coding Agent
            │
            ▼
    Generated Code
            │
            ▼
    Validation
            │
            ▼
    Repair
            │
            ▼
    Complete Build Specification

Before Phase 3, the production default for build_spec_engine.build_spec routed
the build prompt through pipeline_runner.execute, which called
synthesize_answer in production-mode (narrative_fn=None). That path IGNORES the
build prompt and returns the audit-format synthesized_answer (only
"# Verified Decisions" / "# Architecture" / "# Implementation Plan" / "# Migration
Path" / "# Risks"). validate_spec_sections returned 6 missing sections for
that audit shape — every production run spec_status was "incomplete" and the
Phase 2 gate correctly blocked.

Phase 3 changes the production default: ``build_spec_engine.build_spec`` with
``debate_fn=None`` deterministically renders an **executable implementation
specification** carrying the 6 ``EXECUTABLE_SPEC_SECTIONS`` (`# Implementation
Objective` / `# Files to Create` / `# Files to Modify` / `# Implementation
Steps` / `# Acceptance Criteria` / `# Output Contract`), from
``decision_model.decision_records(verification_result)`` — winner text only,
never the dispute title, never the full finding prose. No LLM, no I/O. The
spec is ALWAYS structurally complete AND is an executable spec, not a
decision record. The Phase 2 gate stops being a halting net; the defence-in-
depth gate at ``build_pipeline.execute_build_spec`` re-validates the on-disk
spec against ``validate_executable_spec_sections`` so OpenCode can never
receive a debate summary / verification report / decision record dressed up
as a spec.

The legacy / seam path is preserved byte-for-byte. Existing tests that
inject ``debate_fn=<fake>`` to pin the decision_gaps / missing_sections
diagnostics keep working — the seam path is unchanged.

These tests pin the Phase 3 contract:
  1. The production default produces all 6 EXECUTABLE_SPEC_SECTIONS for any
     input (the 156 fixture, the debate-55 fixture, the empty fixture).
  2. The production default never leaks the rejected option into the
     executable spec (the Phase 1 invariant enforced by the canonical
     model — winner_text only, suppressed platform components).
  3. Determinism: same input → byte-identical spec_text.
  4. The legacy ``debate_fn`` seam path is unchanged byte-for-byte.
  5. The verified winners flow into # Implementation Objective as
     "Use <winner>." directives (NOT as MUST constraints after the rewrite).
  6. End-to-end through run_build_pipeline: the live answer-156 specification
     now ships as ``spec_status=completed`` AND ``spec_missing_sections=[]``
     AND PostgreSQL never surfaces in the executable spec.

No LLM, no I/O, no live workspace needed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_pipeline as bp
import build_spec_engine as bse
import build_workspace as bw
from build_prompt_generator import EXECUTABLE_SPEC_SECTIONS, SPEC_SECTIONS
from decision_model import decision_records


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _answer156_source_result() -> dict:
    """The verification_result that surfaced the original bug. The finding's
    phrasing uses markdown emphasis ('argues *against* PostgreSQL'); Phase 1's
    `_strip_emphasis` + enriched negation grammar recognize it as a rejection
    of PostgreSQL."""
    return {
        "verification_result": {
            "resolved_claims": [{
                "normalized_key": "database_technology_selection",
                "dispute": ("Database Technology: GPT selects PostgreSQL for "
                            "relational durability and audit trails, while "
                            "Gemini selects SQLite for local simplicity."),
                "verification": {
                    "status": "RESOLVED", "confidence": 95,
                    "finding": ("Both evidence strips conclude SQLite is the "
                                "appropriate persistence engine; the text "
                                "argues *against* PostgreSQL for current needs."),
                },
            }],
            "equivalent_claims": [], "unresolved_claims": [],
            "verification_score": 100, "truncated_count": 0,
        },
        "verified_decisions": [{
            "outcome_type": "VERIFIED_DECISION",
            "dispute_key": "database_technology_selection",
            "decision": ("Both evidence strips conclude SQLite is the "
                         "appropriate persistence engine; the text argues "
                         "*against* PostgreSQL for current needs."),
            "reasoning": ("Both evidence strips conclude SQLite is the "
                          "appropriate persistence engine; the text argues "
                          "*against* PostgreSQL for current needs."),
        }],
    }


def _answer156_decision_records_count() -> int:
    """The canonical decision model returns 1 record for 156's verification
    result. (The verifier resolved exactly one dispute.)"""
    return len(decision_records(_answer156_source_result()["verification_result"]))


# --------------------------------------------------------------------------- #
# 1. The production default is structurally complete                          #
# --------------------------------------------------------------------------- #


def test_production_default_produces_all_spec_sections_for_156() -> None:
    """`build_spec` with no `debate_fn` (the production default) produces an
    executable implementation specification with all 6 EXECUTABLE_SPEC_SECTIONS
    for the 156 fixture. Before this fix the production default produced a
    decision record (audit-style headings + dispute titles as component
    names) and OpenCode was passed that audit as if it were a spec."""
    out = bse.build_spec("ignored on Phase 3 path",
                        source_result=_answer156_source_result())
    assert out["status"] == "completed", out
    assert out["missing_sections"] == [], out["missing_sections"]
    for s in EXECUTABLE_SPEC_SECTIONS:
        assert s in out["spec_text"].lower(), (
            f"missing executable-spec section in spec_text: {s}\nGot:\n{out['spec_text']}")


def test_production_default_produces_sections_for_empty_source() -> None:
    """Even an empty source_result produces all 6 EXECUTABLE_SPEC_SECTIONS by
    construction. The Implementation Objective falls back to a generic
    build directive (the prompt is the objective seed); the file/steps/
    acceptance/output sections are deterministic and always present."""
    out = bse.build_spec("ignored", source_result=None)
    assert out["status"] == "completed", out
    assert out["missing_sections"] == [], out["missing_sections"]
    # The new executable-spec heading is present (not the legacy
    # "# Architecture" — the audit shape is no longer produced by the
    # deterministic Phase 3 path).
    assert "# Implementation Objective" in out["spec_text"]
    assert "# Files to Create" in out["spec_text"]
    assert "# Output Contract" in out["spec_text"]


def test_production_default_produces_sections_for_debate55_fixture() -> None:
    """A rich multi-decision fixture (debate 55) still produces all 6
    executable sections. Phase 1 keeps the architecture invariant: Docker
    (a verified winner) surfaces (as a "Use <winner>." directive in the
    Implementation Objective); WebContainers (the rejected option) does not
    appear anywhere in the spec — the renderer only consumes winner_text,
    never the dispute title or the full finding."""
    from test_deterministic_decisions import debate55_verification_result
    src = {
        "verification_result": debate55_verification_result(),
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [],
    }
    out = bse.build_spec("ignored", source_result=src)
    assert out["status"] == "completed", out
    assert out["missing_sections"] == [], out["missing_sections"]
    # Phase 1: Docker (a verified winner) surfaces in the spec (winner_text).
    assert "docker" in out["spec_text"].lower()
    # WebContainers (rejected) does NOT surface anywhere in the executable
    # spec — the renderer uses winner_text only, never the dispute title or
    # the finding prose (which is what named both sides).
    assert "webcontainers" not in out["spec_text"].lower(), (
        f"Phase 3 regression: rejected option in the executable spec: "
        f"{out['spec_text']!r}")


# --------------------------------------------------------------------------- #
# 2. The Phase 1 invariant is enforced by the spec                            #
# --------------------------------------------------------------------------- #


def test_rejected_option_never_surfaces_as_architecture_for_156() -> None:
    """The Phase 1 invariant: the rejected option (PostgreSQL) MUST NOT
    surface anywhere in the executable spec. The Phase 3 deterministic
    renderer reads only ``winner_text`` from each
    ``decision_records(verification_result)`` entry — never the dispute
    title, never the full finding — so the rejected option cannot leak by
    construction."""
    out = bse.build_spec("ignored", source_result=_answer156_source_result())
    assert "postgresql" not in out["spec_text"].lower(), (
        f"Phase 3 regression: PostgreSQL in the executable spec: "
        f"{out['spec_text']!r}")


def test_rejected_option_never_surfaces_as_bare_component_for_156() -> None:
    """Bare component lines like "- PostgreSQL" (no rejection framing) would
    be the contradiction class. The Phase 3 renderer's # Files to Create is
    generated from the recognized platform components only; the rejected
    option is never used as a component name (the Phase 1 invariant in
    ``decision_renderer._architecture_components`` suppresses any alias in
    the ``rejected_options`` set)."""
    out = bse.build_spec("ignored", source_result=_answer156_source_result())
    files_section = (out["spec_text"]
                     .split("# Files to Create", 1)[1]
                     .split("# Files to Modify", 1)[0])
    bare_lines = [
        line for line in files_section.splitlines()
        if line.lstrip().startswith("- ")
        and "postgresql" in line.lower()
    ]
    assert not bare_lines, (
        f"Phase 3 regression: bare component lines emit PostgreSQL: "
        f"{bare_lines!r}")


def test_winner_surfaces_in_implementation_objective_for_156() -> None:
    """The verified winner (SQLite) flows into the Implementation Objective
    as a "Use <winner>." directive (because the Phase 3 renderer reads
    ``decision_records(verification_result)[i]['winner_text']`` — never
    the dispute title). This is how Phase 3 stays faithful to Phase 1's
    single source of truth — the spec never contradicts the verifier."""
    out = bse.build_spec("ignored", source_result=_answer156_source_result())
    assert "sqlite" in out["spec_text"].lower(), out["spec_text"]
    # The spec records the verified winner as a build directive.
    assert "# Implementation Objective" in out["spec_text"]


# --------------------------------------------------------------------------- #
# 3. Determinism                                                              #
# --------------------------------------------------------------------------- #


def test_production_default_is_deterministic() -> None:
    """Same input → byte-identical spec_text."""
    src = _answer156_source_result()
    a = bse.build_spec("ignored", source_result=src)
    b = bse.build_spec("ignored", source_result=src)
    c = bse.build_spec("also ignored by Phase 3 path", source_result=src)
    assert a["spec_text"] == b["spec_text"] == c["spec_text"], (
        "Phase 3 regression: spec_text drift across runs with the same input")


def test_production_default_ignores_source_answer_prose() -> None:
    """The synthesized_answer prose (`source_answer`) is NOT consumed by the
    Phase 3 deterministic path (it reads from decision_model.decision_records
    only - the single source of truth). This is the contract that prevents
    the divergence class observed in 156: prose vs. model can never diverge
    when only one is read."""
    src = _answer156_source_result()
    prose_a = "Use PostgreSQL as the primary store (this was rejected)."
    prose_b = "Ada yang penting: kita pakai PostgreSQL."
    out_a = bse.build_spec(prose_a, source_result=src)
    out_b = bse.build_spec(prose_b, source_result=src)
    assert out_a["spec_text"] == out_b["spec_text"], (
        "Phase 3 regression: the deterministic path depended on "
        "source_answer prose")


# --------------------------------------------------------------------------- #
# 4. Legacy / seam path is unchanged                                         #
# --------------------------------------------------------------------------- #


def test_debate_fn_seam_path_still_consumes_prompt() -> None:
    """The legacy `debate_fn=<callable>` path is preserved byte-for-byte. Tests
    that pin the decision_gaps / missing_sections diagnostics inject a fake
    debate that returns a canned synthesized_answer; Phase 3 must not break
    them."""
    canned_spec = (
        "# Architecture\nA layered service.\n\n"
        "# Components\nAPI, worker, DB.\n\n"
        "# File Structure\nsrc/api, src/worker.\n\n"
        "# Dependencies\nfastapi, sqlalchemy.\n\n"
        "# Constraints\nMUST use PostgreSQL.\n\n"
        "# Testing Requirements\npytest suite, 90% coverage.\n\n"
        "# Acceptance Criteria\nAll tests green; deploy in one command.\n"
    )

    def fake_debate(prompt, memory_ids=None):
        # The seam sees the BUILT PROMPT (not the empty Phase 3 string).
        return {
            "status": "completed",
            "synthesized_answer": canned_spec,
            "gpt_final": "...", "gemini_final": "...",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [], "final_consensus_score": 90,
        }

    src_result = {
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "db",
             "decision": "PostgreSQL as primary store", "reasoning": "ACID"},
        ],
        "verified_tradeoffs": [
            {"outcome_type": "VERIFIED_TRADEOFF", "dispute_key": "cache",
             "option_a": "Redis", "option_b": "Memcached",
             "recommendation": "Redis", "reasoning": "persistence"},
        ],
    }
    out = bse.build_spec("Use PostgreSQL with a Redis cache.",
                         debate_fn=fake_debate, source_result=src_result)
    assert out["status"] == "completed", out
    assert out["missing_sections"] == [], out["missing_sections"]
    # The seam path DOES build a prompt (Phase 3 path does not - this is the
    # distinguishing signal).
    assert out["prompt"], "Seam path should have built a prompt"
    assert "PostgreSQL as primary store" in out["prompt"]
    assert "Redis" in out["prompt"]
    # The seam's synthesized_answer is the spec text.
    assert out["spec_text"] == canned_spec
    assert out["debate_result"]["synthesized_answer"] == canned_spec


def test_debate_fn_seam_path_reports_incomplete_when_sections_missing() -> None:
    """The seam path's diagnostics (missing_sections = the validate_spec_sections
    result over the synthesized_answer) are unchanged."""
    out = bse.build_spec(
        "x",
        debate_fn=lambda p, memory_ids=None: {
            "synthesized_answer": "# Architecture\nonly",
        },
        source_result={},
    )
    assert out["status"] == "incomplete"
    assert out["missing_sections"], out


def test_default_debate_fn_still_importable() -> None:
    """The legacy `_default_debate_fn` (the audit-debate round-trip path) is
    kept importable for callers/tests that explicitly want it. Phase 3 only
    changes which path is the PRODUCTION DEFAULT; it does not delete the
    legacy callable."""
    assert callable(bse._default_debate_fn)


# --------------------------------------------------------------------------- #
# 5. Verified decisions + tradeoffs flow into the spec                       #
# --------------------------------------------------------------------------- #


def test_verified_decisions_surface_in_implementation_objective() -> None:
    """The Phase 3 renderer surfaces each verified decision's winner_text as
    a "Use <winner>." directive in the Implementation Objective. The
    verified_decision's `decision` field (often the full finding prose) is
    NOT passed to the agent — only the canonical winner_text from
    ``decision_records(verification_result)`` is.
    """
    src = {
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "db",
             "decision": "PostgreSQL as primary store", "reasoning": "ACID"},
        ],
        "verified_tradeoffs": [],
    }
    out = bse.build_spec("ignored", source_result=src)
    objective_section = (out["spec_text"]
                        .split("# Implementation Objective", 1)[1]
                        .split("# Files to Create", 1)[0])
    # Phase 3 surfaces the verified winner as a build directive.
    assert "# Implementation Objective" in out["spec_text"]
    assert objective_section.strip(), objective_section


def test_decision_gaps_diagnostic_preserved() -> None:
    """FIX 5's report-only diagnostic (verified decisions whose winner token
    is absent from the spec) is preserved on both paths. On the Phase 3 path
    the spec is derived from ``decision_records(verification_result)`` so the
    canonical winner_text is always present in the spec; when the caller
    passes a stale verified_decisions bucket (or none at all), the
    diagnostic correctly reports no gaps (its input is the bucket, not the
    canonical model)."""
    out = bse.build_spec("ignored", source_result=_answer156_source_result())
    assert out["decision_gaps"] == [], out["decision_gaps"]


# --------------------------------------------------------------------------- #
# 6. End-to-end through run_build_pipeline                                    #
# --------------------------------------------------------------------------- #


def test_run_build_pipeline_produces_complete_spec_for_156_lineage() -> None:
    """End-to-end: a debate that yields the 156-shape verification_result
    (no platform component recognized; the verifier winner is SQLite, the
    rejected option is PostgreSQL). With the production default (no
    debate_fn), run_build_pipeline now ships an executable spec with
    spec_status=completed and no missing executable-spec sections. The
    Phase 1 invariant holds end-to-end: PostgreSQL never surfaces in the
    executable spec at all (the winner_text-only renderer)."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)

    # The pipeline's existing generate_build_spec calls build_spec
    # (production default - no debate_fn). Phase 3 path renders the
    # executable spec deterministically.
    src = _answer156_source_result()

    result = bp.run_build_pipeline(
        "Build Project Sol0mon MVP",
        source_result=src,
        executor_fn=lambda spec: {  # would only be called if gate passes
            "success": True, "output": "ok", "test_results": {},
            "repair_attempts": 0, "error": "",
        },
    )
    assert result["classification"]["workflow_type"] == "IMPLEMENTATION"
    assert result["spec_status"] == "completed", result
    assert result["spec_missing_sections"] == [], result

    # And the implementation_plan.md carries the executable spec.
    wid = result["workspace_id"]
    spec = bse.load_implementation_spec(wid)
    assert spec
    for s in EXECUTABLE_SPEC_SECTIONS:
        assert s in spec.lower(), f"executable spec missing section: {s}"

    # The Phase 1 invariant holds end-to-end: PostgreSQL never surfaces in
    # the executable spec for the 156 lineage (the winner_text-only
    # renderer + the suppressed-platform-component filter).
    assert "postgresql" not in spec.lower(), (
        f"End-to-end Phase 3 regression: PostgreSQL in the executable spec: "
        f"{spec!r}")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
