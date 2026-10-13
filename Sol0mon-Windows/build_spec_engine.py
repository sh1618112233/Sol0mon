"""
Project Sol0mon - Phase 4: Build Spec Engine + Phase 3 Build Specification.

Orchestrates the Answer -> Implementation Specification conversion. Two paths:

  Production default (Phase 3 - ``debate_fn=None``):
    Deterministically render the 7 SPEC_SECTIONS directly from the canonical
    decision model (decision_model.decision_records + decision_renderer's
    Phase-1-corrected architecture renderer). No LLM, no browser, no I/O.
    Always structurally complete - every section present and non-empty. This
    eliminates the answer-156 contradiction class at the source: with the
    audit-synthesis renderer reused as the spec, ``validate_spec_sections``
    always found 6 missing sections, the Phase 2 gate always blocked, and
    the build pipeline never produced output. Phase 3 makes the default
    produce a build spec the agent can actually act on.

  Debate seam (``debate_fn=<callable>``):
    Run the Build Prompt through the supplied callable
    (GPT Position -> Gemini Position -> Critique -> Revision -> Consensus).
    Tests use this seam to inject canned outputs and exercise the
    ``decision_gaps`` / ``missing_sections`` diagnostics. The synthesized_answer
    returned by the seam IS the implementation_plan.md content. The
    ``_default_debate_fn`` (pipeline_runner.execute → audit-format synthesizer)
    remains importable but is no longer the default; tests that want to
    exercise the audit path explicitly pass it.

API:
    build_spec(source_answer, debate_fn=None, source_result=None) -> dict
    store_implementation_spec(workspace_id, spec_text) -> bool
    load_implementation_spec(workspace_id) -> str

build_spec() takes the source synthesized answer text plus, optionally, the
full source debate_result dict (so decisions/constraints/tradeoffs can be
extracted from its governance buckets). If the source debate_result is
omitted, extraction falls back to empty lists (the deterministic renderer
still produces all sections).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from build_prompt_generator import (
    EXECUTABLE_SPEC_SECTIONS,
    extract_constraints,
    extract_decisions,
    extract_tradeoffs,
    generate_build_prompt,
    parse_files_to_create,
    validate_executable_spec_sections,
    validate_spec_sections,
)
from build_workspace import _workspace_dir  # reuse Phase 1 layout
from decision_model import decision_records
from decision_renderer import (
    _architecture_components,
    _is_dispute_echo,
    render_architecture,
)


def _spec_path(workspace_id: str):
    return _workspace_dir(workspace_id) / "implementation_plan.md"


def _default_debate_fn(prompt: str, memory_ids: list | None = None) -> dict:
    """Production debate runner (LEGACY audit-debate path). Lazily imported
    so that importing build_spec_engine never triggers browser/network side
    effects (DebateEngine.run spins up GPT/Gemini browser sessions).

    Phase 3: this is NOT the production default for build_spec any more. The
    production default deterministically renders the SPEC_SECTIONS from the
    canonical decision model (``_render_spec_sections``). This function is
    kept for callers/tests that explicitly want the audit-debate path; pass
    ``debate_fn=_default_debate_fn`` to opt in."""
    from pipeline_runner import execute
    return execute(prompt, memory_ids=memory_ids)


# ---------------------------------------------------------------------------
# Phase 3: Deterministic SPEC_SECTIONS renderer.
#
# Inputs (any subset; None-friendly): source_result, target_components.
# source_result is the resurrected-answer debate result, carrying
# verification_result + verified_decisions + verified_tradeoffs +
# human_required. Every section derives from these (and the canonical
# decision_model), so the spec can NEVER contradict the verifier (Phase 1
# invariant) and is ALWAYS structurally complete (Phase 3 goal - Phase 2's
# gate stops being a halting net and becomes a productive path).
# ---------------------------------------------------------------------------

# Per-platform-component pip-installable dependencies. Used by the
# Dependencies section. Unknown platform components contribute nothing (the
# verifier said X is the architecture; the agent infers the package from
# the winner name). Verified decisions that imply a known tool (e.g.
# "SQLite" → the stdlib ``sqlite3`` module) contribute a zero-dependency
# row so the section is never empty on a decision-only result like answer 156.
_PLATFORM_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "Temporal durable workflows": ("temporalio",),
    "PostgreSQL primary datastore":
        ("psycopg2-binary", "sqlalchemy>=2.0", "alembic"),
    "Cloudflare R2 immutable storage": ("boto3",),
    "Local scratch filesystem": (),
    "Docker workers": ("docker",),
    "Firecracker microVMs": (),
    "LiteLLM": ("litellm",),
    "Sol0mon control plane": (),
    "Hybrid BYOK model": (),
    "Provider abstraction layer": (),
}


# Verified-decision winner tokens that imply a known language-level
# dependency. Maps a lowercase substring of the winner phrase to the
# dependency hint. Lets a decision-only result (no platform component)
# still produce a non-empty Dependencies section (answer 156's case).
_DECISION_DEPENDENCY_HINTS: tuple[tuple[str, str], ...] = (
    ("sqlite", "stdlib sqlite3 (no extra package - Python standard library)"),
    ("postgresql", "psycopg2-binary OR stdlib sqlite3 (per the verifier)"),
    ("redis", "redis-py"),
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("django", "django"),
)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """Deterministic component slug for the File Structure section."""
    s = _SLUG_RE.sub("_", (name or "").lower()).strip("_")
    return s or "component"


def _component_dependency_lines(components: list[dict],
                                 decisions: list[dict]) -> list[str]:
    """Build the Dependencies section lines from the matched platform
    components + verified decisions. Always returns at least one line so
    the section is non-empty (the spec is structurally complete by
    construction)."""
    lines: list[str] = []
    seen: set[str] = set()
    for c in components:
        for dep in _PLATFORM_DEPENDENCIES.get(c["name"], ()):
            if dep not in seen:
                seen.add(dep)
                lines.append(f"- {dep}")
    for d in decisions:
        decision_text = (d.get("decision") or "").lower()
        for trigger, hint in _DECISION_DEPENDENCY_HINTS:
            if trigger in decision_text and hint not in seen:
                seen.add(hint)
                lines.append(f"- {hint}")
                break
    if not lines:
        lines.append("- Python 3.10+ standard library")
    return lines


def _render_spec_sections(
    source_result: dict | None,
    target_components: list[str] | None = None,
) -> str:
    """Phase 3: deterministically render an **executable** implementation
    specification from the canonical decision model. Pure; no LLM, no I/O.

    The spec carries exactly the 6 ``EXECUTABLE_SPEC_SECTIONS`` (in order):

      1. ``# Implementation Objective``  — restate the verified winners as a
         build directive ("Use X. Use Y."), never as a debate summary,
         verification report, or decision record. The agent gets the
         architecture-to-build, not the audit that produced it.
      2. ``# Files to Create``           — one ``src/<slug>/__init__.py`` +
         ``src/<slug>/<slug>.py`` + ``tests/test_<slug>.py`` per recognized
         platform component, plus ``README.md`` and ``pyproject.toml``. A
         greenfield fallback (``src/app/``) is emitted when no component is
         recognized, so the section is always non-empty AND the file list is
         always actionable.
      3. ``# Files to Modify``           — greenfield by default; explicitly
         forbids modifying anything outside the paths in # Files to Create.
      4. ``# Implementation Steps``       — one numbered step per platform
         component (the deterministic ``step`` from the canonical
         ``_COMPONENTS`` table) plus a final "add one tests/test_*.py per
         source module" step.
      5. ``# Acceptance Criteria``       — the completion contract the post-
         execution gate in ``execute_build_spec`` enforces (tests actually
         executed, required files exist, no rejected option surfaced).
      6. ``# Output Contract``           — the executor-side exit contract
         (the success contract the orchestrator measures against).

    All sections are non-empty by construction — the defence-in-depth
    ``validate_executable_spec_sections`` gate at
    ``build_pipeline.execute_build_spec`` is what ultimately rejects a spec
    that is missing any of these, but the deterministic renderer ensures any
    spec produced from a ``verification_result`` passes that gate by
    construction.

    Phase-1 invariant preserved: the renderer only reads from canonical
    ``decision_records(verification_result)`` (winner text + rejected
    options) and ``_architecture_components`` (platform-component alias
    matching). The rejected option never surfaces as a component name and
    the dispute title never reaches the agent — the answer-156 leak class
    is closed at the source.
    """
    src = source_result or {}
    verification_result = src.get("verification_result", {}) or {}
    records = decision_records(verification_result)

    # === 1) Implementation Objective =====================================
    # Each verified decision's winner_text becomes a "Use <winner>." directive.
    # The dispute title and the finding prose (which names BOTH sides) DO NOT
    # reach the agent — only the chosen winner. The objective is a build
    # directive, not an audit trail. Always at least one directive line.
    winner_directives: list[str] = []
    for r in records:
        w = (r.get("winner_text") or "").strip()
        if not w:
            continue
        # Renderer guard (Debate #164 class): when winner_text is the
        # dispute description echoed back (findings that open with "The
        # dispute description claims..."), do NOT emit it as a "Use X."
        # directive — it names BOTH options and would leak the rejected
        # option to the agent. The decision still appears in # Constraints
        # below, sourced from the full finding text via extract_constraints.
        # Pure renderer-side; the decision model is untouched.
        dispute_text = (r.get("dispute") or "").strip()
        if _is_dispute_echo(w, dispute_text):
            continue
        winner_directives.append(f"- Use {w}.")
    if not winner_directives:
        # No verification_result / no extractable winners. The objective
        # remains a build directive: build the package described by the
        # prompt, with a standard src/<package>/ + tests/ layout. The agent
        # never sees "no decisions" as an audit fact — it sees an actionable
        # objective. (The gate's completion check rejects a spec that visibly
        # has no chance of producing artefacts; the renderer's job is to
        # always hand the agent something to do.)
        winner_directives.append(
            "- Implement the project described by the build prompt as a "
            "Python application under src/<package>/ with one module per "
            "responsibility and one tests/test_*.py per source module.")
    obj_text = ("# Implementation Objective\n\n"
                "Build a Python implementation that satisfies every choice "
                "below. Do not re-open the architectural decisions; implement "
                "them as written.\n\n"
                + "\n".join(winner_directives) + "\n")

    # === 2) Files to Create =============================================
    # One src/<slug>/ package per recognized platform component, with a
    # <slug>.py module + a tests/test_<slug>.py contract test. Plus the
    # always-required README.md and pyproject.toml. The slug is derived
    # from the component name (never the dispute title — Phase 1 invariant).
    components = _architecture_components(verification_result)
    files_lines: list[str] = []
    for c in components:
        slug = _slug(c["name"])
        files_lines.append(f"- src/{slug}/__init__.py")
        files_lines.append(f"- src/{slug}/{slug}.py  # {c['name']}")
        files_lines.append(f"- tests/test_{slug}.py  # {c['name']} contract")
    if target_components:
        for tc in target_components:
            slug = _slug(tc)
            files_lines.append(f"- src/{slug}/{slug}.py  # {tc}")
            files_lines.append(f"- tests/test_{slug}.py  # {tc} contract")
    # Always-required layout files (so the gate's file-existence check has
    # concrete anchors even when no platform component was recognized).
    files_lines.append("- README.md")
    files_lines.append("- pyproject.toml  # pytest config")
    if not components:
        # Greenfield fallback: a single app package so the agent always has
        # at least one source module to create. Tests/test_app.py asserts the
        # package exposes a working entry point.
        files_lines = [
            "- src/app/__init__.py",
            "- src/app/main.py  # application entry point",
            "- tests/test_app.py  # application contract",
            "- README.md",
            "- pyproject.toml  # pytest config",
        ]
    files_text = "# Files to Create\n\n" + "\n".join(files_lines) + "\n"

    # === 3) Files to Modify =============================================
    # Greenfield by default. The agent MUST NOT touch any file outside the
    # paths declared in # Files to Create (forbids editing pre-existing
    # workspace files like decisions.json / answer.json / build_state.json).
    modify_lines = [
        "- (none — greenfield build; do NOT modify any file outside the "
        "paths listed in # Files to Create)",
    ]
    modify_text = "# Files to Modify\n\n" + "\n".join(modify_lines) + "\n"

    # === 4) Implementation Steps ========================================
    # One numbered step per recognized platform component (the deterministic
    # ``step`` from the _COMPONENTS table is the canonical instruction),
    # plus a final tests step that ties back to # Files to Create.
    step_lines: list[str] = []
    if components:
        for i, c in enumerate(components, 1):
            step = c.get("step") or f"Build the {c['name']} component"
            step_lines.append(f"{i}. {step}.")
    else:
        step_lines.append(
            "1. Scaffold the application package under src/app/ with a "
            "single main module exposing the application entry point.")
    step_lines.append(
        f"{len(step_lines) + 1}. Add exactly one tests/test_*.py per source "
        "module listed in # Files to Create. Each test file asserts the "
        "real contract of the module it tests (no stub / placeholder "
        "tests).")
    steps_text = "# Implementation Steps\n\n" + "\n".join(step_lines) + "\n"

    # === 5) Acceptance Criteria ========================================
    # The completion contract the post-execution QualityGate enforces.
    accept_lines = [
        "- The pytest test suite exits 0 with at least one passing test.",
        "- Every file listed in # Files to Create exists at the declared "
        "relative path under the build workspace, with non-placeholder "
        "content.",
        "- No generated file declares a bare REJECTED option from the "
        "verified decisions; the agent honours the implementation "
        "objective verbatim.",
        "- Tests actually executed (runner != none; passed >= 1; "
        "failed == 0; exit_code == 0).",
        "- No test file in tests/ is a stub or placeholder; every test "
        "asserts a real contract of the module it tests.",
    ]
    accept_text = "# Acceptance Criteria\n\n" + "\n".join(accept_lines) + "\n"

    # === 6) Output Contract ============================================
    # The executor-side exit contract. This is the success contract the
    # orchestrator's post-execution gate measures against — see
    # ``build_pipeline.execute_build_spec``.
    output_lines = [
        "- The build is successful ONLY when ALL of: implementation "
        "completed; source artefacts generated; every file in # Files "
        "to Create exists; tests actually executed and passed; and the "
        "post-execution QualityGate passed.",
        "- Any failure of the above sets ``execution_success = False``; "
        "the result carries a structured ``quality_gate`` report naming "
        "the missing files and / or failed checks. The build is NOT "
        "successful merely because no tests failed.",
        "- Do not commit. Do not push. Stop after the tests pass; the "
        "orchestrator handles persistence and manifest assembly.",
    ]
    output_text = "# Output Contract\n\n" + "\n".join(output_lines) + "\n"

    return "\n\n".join([
        obj_text, files_text, modify_text, steps_text,
        accept_text, output_text,
    ])


def _spec_decision_gaps(spec_text: str, verified_decisions: list[dict]) -> list[dict]:
    """FIX 5 guard: return verified decisions NOT honoured by the spec text.

    A decision is considered honoured when the spec references its
    normalized_key OR at least one non-stopword token from the WINNER portion
    of its finding (the text before "reject"). Restricting to the winner
    portion is important: findings also name the rejected option, so matching
    the whole finding would false-pass on a spec that selected the rejected
    option. Pure string match; no model calls. This is a necessary (not
    sufficient) diagnostic — callers use it to flag specs that silently drop
    verified decisions.
    """
    if not verified_decisions:
        return []
    low = (spec_text or "").lower()
    if not low:
        return list(verified_decisions)
    stop = {"the", "a", "an", "of", "and", "or", "to", "in", "for",
            "is", "are", "by", "with", "that", "this", "both", "evidence",
            "strips", "dispute", "resolved", "reject", "rejects",
            "selection", "selecting", "favor", "converges", "establishes",
            "rather", "than", "not", "client", "server", "side", "winner",
            "explicitly", "primary", "system", "record", "approach"}
    gaps = []
    for d in verified_decisions:
        if not isinstance(d, dict):
            continue
        nk = (d.get("dispute_key") or d.get("normalized_key") or "").lower()
        finding = (d.get("decision") or d.get("reasoning") or "").lower()
        # Winner portion = text before the first "reject"; that is where the
        # chosen option is named. The rejected option is named after "reject".
        winner_part = finding.split("reject")[0] if "reject" in finding else finding
        tokens = [t for t in winner_part.replace("/", " ").split()
                  if len(t) > 3 and t not in stop]
        if nk and nk in low:
            continue
        if any(t and t in low for t in tokens[:8]):
            continue
        gaps.append(d)
    return gaps


def build_spec(
    source_answer: str,
    debate_fn: Callable[..., dict] | None = None,
    source_result: dict | None = None,
    memory_ids: list | None = None,
    target_components: list[str] | None = None,
) -> dict:
    """Convert a synthesized answer into an implementation specification.

    Phase 3: the production default (``debate_fn=None``) deterministically
    renders an **executable implementation specification** carrying the 6
    ``EXECUTABLE_SPEC_SECTIONS`` (``# Implementation Objective`` / ``# Files
    to Create`` / ``# Files to Modify`` / ``# Implementation Steps`` / ``#
    Acceptance Criteria`` / ``# Output Contract``). The spec is derived from
    ``decision_model.decision_records(verification_result)`` — winner text
    only; never the dispute title, never the full verification finding,
    never the synthesized-answer prose. No LLM, no browser, no I/O. The
    defence-in-depth gate in ``build_pipeline.execute_build_spec`` then
    re-validates the on-disk spec against
    ``validate_executable_spec_sections`` before dispatching to the
    implementation executor; a spec that is a debate summary, verification
    report, or decision record (anything missing the executable sections)
    is rejected with a structured ``incomplete_executable_spec`` block and
    the executor is never invoked.

    Legacy audit-debate path (``debate_fn=<callable>``): unchanged. Used by
    tests that inject a canned ``synthesized_answer`` to pin the
    legacy-shape ``decision_gaps`` / ``missing_sections`` diagnostics; the
    production default never routes through this path anymore.

    Args:
        source_answer:   the human-readable synthesized answer text (kept
                         for back-compat; the Phase 3 deterministic path
                         reads from ``source_result`` instead, never from
                         prose — single source of truth).
        debate_fn:       callable(prompt, memory_ids=None) -> debate_result.
                         When supplied, the legacy audit-debate path runs
                         and the returned synthesized_answer becomes the
                         spec text (existing tests' contract preserved).
                         When None, the deterministic Phase 3 executable-
                         spec renderer is used (no audit-debate round-trip
                         - the source of the 156 contradiction and the Phase
                         2 halting, and the root cause of the
                         decision-record-as-spec bug fixed here).
        source_result:   optional full source debate_result dict; its
                         verification_result drives the executable-spec
                         renderer (Phase 3 path) and its
                         verified_decisions / verified_tradeoffs buckets
                         are extracted into the build prompt (legacy path).
        memory_ids:      optional prior debate ids passed to the debate
                         (legacy path only).
        target_components: optional explicit component list for the spec
                         (extends # Files to Create on the Phase 3 path,
                         # Components on the legacy path).

    Returns:
        {
          "spec_text": str,              # the implementation_plan.md body
          "prompt": str,                 # the build prompt (legacy path)
                                         # or "" (Phase 3 path - no prompt)
          "decisions": [...], "constraints": [...], "tradeoffs": [...],
          "verified_decisions": [...],
          "verification_result": dict,
          "decision_gaps": [...],        # diagnostic (FIX 5 report-only)
          "debate_result": dict,         # raw result from the legacy engine,
                                         # or {"status":"completed"} (Phase 3)
          "status": str,                 # "completed" | "incomplete"
          "missing_sections": [str],     # executable-spec sections not
                                         # found in spec_text on the Phase 3
                                         # path; legacy SPEC_SECTIONS on the
                                         # audit-debate path
        }
    """
    src = source_result or {}
    decisions = extract_decisions(src)
    constraints = extract_constraints(src)
    tradeoffs = extract_tradeoffs(src)

    verification_result = src.get("verification_result", {}) or {}
    verified_decisions = src.get("verified_decisions", []) or []

    if debate_fn is not None:
        # Legacy / seam path: build a prompt and run it through the supplied
        # callable (test mock or _default_debate_fn). Existing tests pin this
        # contract byte-for-byte; Phase 3 leaves it unchanged. The legacy
        # ``validate_spec_sections`` (7 audit sections) is the validator.
        prompt = generate_build_prompt(
            source_answer, decisions, constraints, tradeoffs,
            target_components=target_components,
        )
        debate_result = debate_fn(prompt, memory_ids=memory_ids) or {}
        spec_text = (debate_result.get("synthesized_answer")
                     or debate_result.get("synthesized") or "")
        status = debate_result.get("status", "completed")
        if not spec_text:
            status = "incomplete"
        missing = validate_spec_sections(spec_text)
    else:
        # Phase 3 production path: deterministic executable-spec renderer.
        # No prompt is built (the decision model, not the prompt, drives the
        # spec). No debate round trip; no audit synthesis whose ignore-the-
        # prompt behavior produced the 156 contradiction / decision-record-
        # as-spec bug. The executable-spec validator (6 sections) is the
        # gate; the defence-in-depth gate at execute_build_spec re-runs it
        # on the loaded spec text before the executor is dispatched.
        prompt = ""
        spec_text = _render_spec_sections(
            source_result=src, target_components=target_components,
        )
        debate_result = {"status": "completed"}
        missing = validate_executable_spec_sections(spec_text)

    if missing:
        status = "incomplete"
    else:
        status = "completed"

    decision_gaps = _spec_decision_gaps(spec_text, verified_decisions)

    return {
        "spec_text": spec_text,
        "prompt": prompt,
        "decisions": decisions,
        "constraints": constraints,
        "tradeoffs": tradeoffs,
        "verified_decisions": verified_decisions,
        "verification_result": verification_result,
        "decision_gaps": decision_gaps,
        "debate_result": debate_result,
        "status": status,
        "missing_sections": missing,
    }


def store_implementation_spec(workspace_id: str, spec_text: str) -> bool:
    """Write the implementation specification into the workspace as
    implementation_plan.md. Creates the dir if missing (defensive)."""
    try:
        p = _spec_path(workspace_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(spec_text, encoding="utf-8")
        return True
    except OSError as e:
        print(f"[BUILD SPEC STORE ERROR] {e}")
        return False


def load_implementation_spec(workspace_id: str) -> str:
    """Read implementation_plan.md for a workspace ('' if absent)."""
    p = _spec_path(workspace_id)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


if __name__ == "__main__":
    # Smoke self-check: stubbed debate_fn, no browser, no network.
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
        # Reuses the existing methodology shape; we stub the consensus output.
        return {
            "status": "completed",
            "synthesized_answer": canned_spec,
            "gpt_final": "...", "gemini_final": "...",
            "validated_disputes": [],
            "verified_decisions": [],
            "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 90,
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
    out = build_spec("Use PostgreSQL with a Redis cache.",
                     debate_fn=fake_debate, source_result=src_result)
    assert out["status"] == "completed", out
    assert out["missing_sections"] == [], out["missing_sections"]
    assert "PostgreSQL as primary store" in out["prompt"]
    assert "Redis" in out["prompt"]
    assert len(out["decisions"]) == 1
    assert len(out["tradeoffs"]) == 1
    assert len(out["constraints"]) == 1
    # incomplete when a section is absent
    out2 = build_spec("x", debate_fn=lambda p, memory_ids=None:
                      {"synthesized_answer": "# Architecture\nonly"},
                      source_result={})
    assert out2["status"] == "incomplete" and out2["missing_sections"]

    # Phase 3 production default (debate_fn=None): deterministic executable
    # spec. ALL 6 EXECUTABLE_SPEC_SECTIONS are present by construction. The
    # legacy audit-debate round-trip is no longer the production path; the
    # defence-in-depth gate at execute_build_spec enforces the executable-
    # spec shape on disk.
    out3 = build_spec("ignored on Phase 3 path", source_result=src_result)
    assert out3["status"] == "completed", out3
    assert out3["missing_sections"] == [], out3["missing_sections"]
    assert out3["prompt"] == ""  # Phase 3 path does not build a prompt
    # The 6 executable-spec sections are present.
    from build_prompt_generator import EXECUTABLE_SPEC_SECTIONS
    for s in EXECUTABLE_SPEC_SECTIONS:
        assert s in out3["spec_text"].lower(), (
            f"Phase 3 regression: executable-spec section absent: {s}")
    # Determinism: same input -> identical output, byte-for-byte.
    out3_b = build_spec("ignored on Phase 3 path", source_result=src_result)
    assert out3["spec_text"] == out3_b["spec_text"]

    # The answer-156 shape: a decision-only result (no recognized platform
    # component). Phase 3 still produces all 6 executable-spec sections. The
    # rejected option (PostgreSQL) does NOT leak into the spec (Phase 1
    # invariant); the verified winner (SQLite) is recorded in the
    # Implementation Objective as a "Use <winner>." directive.
    src_156 = {
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
    out_156 = build_spec("ignored", source_result=src_156)
    assert out_156["status"] == "completed", out_156
    assert out_156["missing_sections"] == [], out_156["missing_sections"]
    spec_low = out_156["spec_text"].lower()
    # The verified winner (SQLite) surfaces in the executable spec.
    assert "sqlite" in spec_low
    # The rejected option (PostgreSQL) NEVER surfaces anywhere in the
    # executable spec — the renderer reads winner_text only.
    assert "postgresql" not in spec_low, (
        "Phase 3 regression: PostgreSQL surfaced in the executable spec: "
        f"{out_156['spec_text']!r}")
    print("build_spec_engine Phase 3 smoke test PASSED")
