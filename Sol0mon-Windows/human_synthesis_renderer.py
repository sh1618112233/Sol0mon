"""
Project Sol0mon — Human-Readable Synthesis V3 (human_synthesis_renderer).

A SECOND synthesis layer that turns the verified decision model into a
human-facing engineering report. The existing audit synthesis
(synthesis_engine -> synthesized_answer) is UNCHANGED and remains the
persistent audit artifact; this renderer produces a parallel human_answer
for the default user experience.

Pipeline:

    Verification -> Audit Synthesis (synthesized_answer)
                -> Human Synthesis (human_answer)   <-- this module
                -> Persistence (both stored)

Contract:
  - Deterministic. No LLM. No model calls. No I/O.
  - Only renders VERIFIED decisions (verification_result.resolved_claims).
  - Never invents a decision, component, task, or risk. Every line derives
    from verification_result, the execution_decision, or the selected_mode.
  - Reuses decision_renderer's deterministic renderers for Architecture /
    Risks / Verified Decisions / Tradeoffs / Human Decisions so the human
    answer cannot drift from the audit artifact (single source of truth).

Output sections (canonical order, Phase 2):
  # Executive Summary          — 2–5 paragraphs: what / why / confidence / recommendation
  # Verified Architecture       — grouped verified components (Infrastructure /
                                  Application / Data / Integration / ...)
  # Implementation Roadmap      — Phase 1 / Phase 2 / Phase 3, each with tasks
                                  derived ONLY from verified implementation steps
  # Risks                       — verified risks only
  # Recommended Next Action     — depends on selected_mode (Research Only -> Stop;
                                  Research+Architecture -> Produce architecture
                                  package; Full Build -> Continue to Build Spec)
  # Appendix                    — Verified Decisions / Tradeoffs / Human Decisions
                                  / Confidence (preserves audit visibility)

`render_human_answer(question, verification_result, synthesized_answer,
execution_decision=None, selected_mode=None)` — the audit `synthesized_answer`
is accepted to match the required signature but the renderer never depends on
its prose (verified decisions are the single source of truth, so the human
answer can never invent or contradict the audit artifact).
"""

from __future__ import annotations

import re
from typing import Any

from decision_renderer import (
    _architecture_components,
    render_architecture,
    render_human_required,
    render_risks,
    render_verified_decisions,
    render_verified_tradeoffs,
)

# Execution decision labels (kept in sync with execution_decision.py by name;
# this module takes execution_decision by value so it never imports the
# classifier and stays free of any non-stdlib dependency).
_HUMAN_REQUIRED = "STOP_HUMAN_REQUIRED"
_UNRESOLVED = "STOP_UNRESOLVED_DISPUTES"
_RESEARCH_ONLY = "RESEARCH_ONLY"
_ARCHITECTURE_ONLY = "ARCHITECTURE_ONLY"
_IMPLEMENTATION_REQUIRED = "IMPLEMENTATION_REQUIRED"

# execution_mode_gate user-facing modes (inlined to keep the renderer pure —
# importing execution_mode_gate is allowed, but inlining 3 string constants
# keeps the import-isolation guarantee airtight: this module pulls in only
# stdlib + decision_renderer).
_MODE_RESEARCH_ONLY = "RESEARCH_ONLY"
_MODE_RESEARCH_ARCHITECTURE = "RESEARCH_ARCHITECTURE"
_MODE_FULL_BUILD = "FULL_BUILD"

# execution_decision -> user-facing mode fallback (mirrors
# execution_mode_gate.decision_to_mode). Used when selected_mode is None
# (debate_engine, which cannot be modified to pass selected_mode, calls this
# renderer with 4 args). Safest-stop default is RESEARCH_ONLY.
_DECISION_TO_MODE = {
    _RESEARCH_ONLY: _MODE_RESEARCH_ONLY,
    _ARCHITECTURE_ONLY: _MODE_RESEARCH_ARCHITECTURE,
    _IMPLEMENTATION_REQUIRED: _MODE_FULL_BUILD,
    _HUMAN_REQUIRED: _MODE_RESEARCH_ONLY,
    _UNRESOLVED: _MODE_RESEARCH_ONLY,
}

HUMAN_HEADERS = (
    "# Executive Summary",
    "# Verified Architecture",
    "# Implementation Roadmap",
    "# Risks",
    "# Recommended Next Action",
    "# Appendix",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _resolved_claims(verification_result: dict | None) -> list[dict]:
    vr = _as_dict(verification_result)
    return [c for c in (vr.get("resolved_claims") or []) if isinstance(c, dict)]


def _unresolved_claims(verification_result: dict | None) -> list[dict]:
    vr = _as_dict(verification_result)
    return [c for c in (vr.get("unresolved_claims") or []) if isinstance(c, dict)]


def _verifications(claim: dict) -> dict:
    v = claim.get("verification")
    return v if isinstance(v, dict) else {}


def _exec_decision_value(execution_decision: Any) -> str:
    """Accept the dict form (classify_execution_decision result), the bare
    string label, or None. Returns the decision label or ''."""
    if execution_decision is None:
        return ""
    if isinstance(execution_decision, dict):
        return str(execution_decision.get("execution_decision") or "")
    return str(execution_decision)


def _first_sentence(text: str, limit: int = 200) -> str:
    """Faithful prefix of a finding: keep the first sentence (or first clause
    when it ends in ';'), up to `limit` chars. The closing punctuation is
    dropped so the bullet reads cleanly. A prefix never invents content — it
    only shortens. Findings commonly read "...resolved in favor of X; rejects
    Y" — the first clause names the winner, the second names the rejected
    option (which is fully preserved in the audit artifact)."""
    if not text:
        return ""
    for sep in (". ", ".\n", "\n"):
        i = text.find(sep)
        if 0 < i <= limit:
            return text[:i].rstrip(". \t")
    i = text.find("; ")
    if 0 < i <= limit:
        return text[:i].rstrip("; \t")
    if len(text) <= limit:
        return text.rstrip(". \t")
    return text[:limit].rstrip(" .\t") + "..."


def _resolve_mode(execution_decision: Any, selected_mode: str | None) -> str:
    """Resolve the authoritative user-facing mode: an explicit selected_mode
    wins; otherwise fall back to the execution_decision -> mode mapping. When
    neither is present, default to the safest-stop (RESEARCH_ONLY). Pure."""
    if selected_mode:
        return selected_mode
    dec = _exec_decision_value(execution_decision)
    return _DECISION_TO_MODE.get(dec, _MODE_RESEARCH_ONLY)


# ---------------------------------------------------------------------------
# 1. Executive Summary — 2–5 paragraphs (what / why / confidence / recommendation)
# ---------------------------------------------------------------------------


def render_executive_summary(
    question: str,
    verification_result: dict | None,
    execution_decision: Any = None,
    selected_mode: str | None = None,
) -> str:
    """Deterministic 2–5 paragraph summary. Every line derives from
    verification_result.resolved_claims, the execution_decision, or the
    selected_mode. Nothing is invented."""
    vr = _as_dict(verification_result)
    resolved = _resolved_claims(vr)
    unresolved = _unresolved_claims(vr)
    score = vr.get("verification_score")
    score_text = f"{score}%" if isinstance(score, (int, float)) else "N/A"

    q = (question or "").strip()
    lines = ["# Executive Summary", ""]

    # — Paragraph 1: what Sol0mon concluded.
    topic = f" \"{q}\"" if q else ""
    if resolved:
        lines.append(
            f"Sol0mon debated{topic} and verified {len(resolved)} "
            f"architectural decision(s) against the evidence, reaching a "
            f"verification score of {score_text}."
        )
    else:
        lines.append(
            f"Sol0mon debated{topic}; no architectural decisions were verified "
            f"against the evidence."
        )
    lines.append("")

    # — Paragraph 2: why — the verified decisions themselves.
    if resolved:
        lines.append("The verified decisions are:")
        lines.append("")
        for c in resolved:
            v = _verifications(c)
            finding = str(v.get("finding") or "")
            title = str(
                c.get("dispute") or c.get("normalized_key") or "(untitled dispute)"
            )
            winner = _first_sentence(finding) or "(no finding recorded)"
            lines.append(f"- {title} — {winner}")
        lines.append("")
    else:
        lines.append("No verified decisions were recorded.")
        lines.append("")

    # — Paragraph 3: confidence.
    confs = [
        v.get("confidence")
        for c in resolved
        for v in [_verifications(c)]
        if isinstance(v.get("confidence"), (int, float))
    ]
    if confs:
        avg = sum(confs) / len(confs)
        lines.append(
            f"Consensus confidence: {avg:.0f}% across {len(confs)} verified "
            f"decision(s) (verification score: {score_text})."
        )
    elif isinstance(score, (int, float)):
        lines.append(f"Consensus confidence: {score_text}.")
    if unresolved:
        lines.append(
            f"{len(unresolved)} dispute(s) remain unresolved and are called out "
            f"below."
        )
    lines.append("")

    # — Paragraph 4: execution recommendation.
    dec = _exec_decision_value(execution_decision)
    if dec == _HUMAN_REQUIRED:
        rec = ("A human decision is required before any implementation can "
               "proceed; see Recommended Next Action.")
    elif dec == _UNRESOLVED:
        rec = (f"Implementation is blocked: {len(unresolved)} unresolved "
               f"dispute(s) must be resolved first; see Recommended Next Action.")
    else:
        mode = _resolve_mode(execution_decision, selected_mode)
        if mode == _MODE_FULL_BUILD:
            rec = ("Execution recommendation: proceed to a full build "
                   "(architecture + build spec + implementation).")
        elif mode == _MODE_RESEARCH_ARCHITECTURE:
            rec = ("Execution recommendation: produce the architecture package "
                   "and stop short of implementation.")
        else:
            rec = ("Execution recommendation: research-only; stop after the "
                   "verified findings below.")
    lines.append(rec)
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Verified Architecture — grouped verified components
# ---------------------------------------------------------------------------


def render_verified_architecture(verification_result: dict | None) -> str:
    """Reuse decision_renderer.render_architecture (deterministic, verified-
    decisions-only) and rename the top header to the human-facing title.
    Identical content to the audit's '# Architecture' section — no duplication
    of the component/layer logic, no drift. Components are grouped into the
    categories the classifier recognises (Infrastructure / Application / Data /
    Integration, plus platform layers); only sections with verified
    components appear, so no empty group is ever invented."""
    body = render_architecture(verification_result, None)
    return body.replace("# Architecture", "# Verified Architecture", 1)


# ---------------------------------------------------------------------------
# 3. Implementation Roadmap — Phase 1 / 2 / 3, tasks from verified steps ONLY
# ---------------------------------------------------------------------------

# Platform components carry a `phase` field (MVP / Scale / Enterprise).
_PHASE_BY_PHASE_FIELD = {"MVP": 1, "Scale": 2, "Enterprise": 3}

# Generic classified components (Infrastructure / Data / Integration /
# Application) have no phase field. Deterministic category -> phase mapping so
# a Debate #145-style roadmap groups foundation (data + strategy) first,
# integration second, deployment (infrastructure) last — the acceptance layout.
# ponytail: fixed mapping, no invention; "Do not invent tasks" is honored
# because every task line is a verified component's `step`.
_PHASE_BY_GENERIC_CATEGORY = {
    "Data": 1,
    "Application": 1,
    "Integration": 2,
    "Infrastructure": 3,
}


def _component_phase(component: dict) -> int:
    pf = component.get("phase")
    if pf in _PHASE_BY_PHASE_FIELD:
        return _PHASE_BY_PHASE_FIELD[pf]
    layer = component.get("layer", "")
    if layer in _PHASE_BY_GENERIC_CATEGORY:
        return _PHASE_BY_GENERIC_CATEGORY[layer]
    return 3  # unknown -> last phase (deploy)


def render_implementation_roadmap(verification_result: dict | None) -> str:
    """Deterministic three-phase roadmap. Phase buckets are derived ONLY from
    the architecture components' verified `step` field — no task is invented,
    omitted, or reordered within a phase. Empty phases are skipped."""
    components = _architecture_components(verification_result, None)
    lines = ["# Implementation Roadmap", ""]
    if not components:
        lines.append("No implementation steps; nothing verified.")
        lines.append("")
        return "\n".join(lines)

    phases: dict[int, list[str]] = {1: [], 2: [], 3: []}
    for c in components:
        phases[_component_phase(c)].append(c["step"])

    for p in (1, 2, 3):
        steps = phases[p]
        if not steps:
            continue
        lines.append(f"## Phase {p}")
        lines.extend(f"- {s}" for s in steps)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Risks — verified risks only
# ---------------------------------------------------------------------------


def render_risks_section(verification_result: dict | None) -> str:
    """Reuse decision_renderer.render_risks (deterministic, verified-
    components-only). Only risks attached to a VERIFIED component appear; the
    spec's categories (technical / operational / deployment / financial) are
    the set the classifier may surface — no fabricated risk or category is
    emitted."""
    return render_risks(verification_result, None)


# ---------------------------------------------------------------------------
# 5. Recommended Next Action — depends on selected_mode
# ---------------------------------------------------------------------------


def render_recommended_next_action(
    verification_result: dict | None,
    execution_decision: Any = None,
    selected_mode: str | None = None,
) -> str:
    """The next action tracks the user-selected execution mode. Safety gates
    take precedence: a pending human decision (STOP_HUMAN_REQUIRED) or
    unresolved disputes (STOP_UNRESOLVED_DISPUTES) block any next action
    regardless of the mode the user picked — the human-escalation contract is
    never bypassed by a presentation layer. When selected_mode is None
    (debate_engine's call), the action falls back to the execution_decision ->
    mode mapping."""
    dec = _exec_decision_value(execution_decision)
    unresolved = _unresolved_claims(verification_result)

    if dec == _HUMAN_REQUIRED:
        action = ("A human decision is required before any further action; "
                  "resolve the pending decision(s) and re-run.")
    elif dec == _UNRESOLVED:
        action = (f"Resolve the {len(unresolved)} unresolved dispute(s) before "
                  f"proceeding; the architecture above is provisional.")
    else:
        mode = _resolve_mode(execution_decision, selected_mode)
        if mode == _MODE_FULL_BUILD:
            action = ("Continue to Build Spec — generate a build specification "
                      "from the verified architecture above and proceed to "
                      "implementation.")
        elif mode == _MODE_RESEARCH_ARCHITECTURE:
            action = ("Produce architecture package — assemble the verified "
                      "architecture above into a deliverable architecture "
                      "document; stop short of implementation.")
        else:  # RESEARCH_ONLY (safest-stop default)
            action = ("Stop — this outcome is research-only; adopt the "
                      "verified findings above and do not implement without "
                      "further verification.")

    return "\n".join(["# Recommended Next Action", "", action, ""])


# ---------------------------------------------------------------------------
# 6. Appendix — Verified Decisions / Tradeoffs / Human Decisions / Confidence
# ---------------------------------------------------------------------------


def _demote_h1(text: str) -> str:
    """Demote top-level '# ' headings to '## ' so audit sub-sections nest
    under '# Appendix'. Only h1 lines are touched; h2/h3 ('## ', '### ') and
    body text are preserved. ponytail: one regex, no re-implementation of the
    audit renderers — the appendix shows the audit sections verbatim, just
    demoted one level."""
    return re.sub(r"(?m)^# ", "## ", text or "")


def _confidence_summary(verification_result: dict | None) -> str:
    """Deterministic confidence block: per-decision confidence + the aggregate
    verification score. Pure."""
    vr = _as_dict(verification_result)
    resolved = _resolved_claims(vr)
    score = vr.get("verification_score")
    score_text = f"{score}%" if isinstance(score, (int, float)) else "N/A"

    lines = ["## Confidence", "", f"Verification score: {score_text}", ""]
    if not resolved:
        lines.append("No verified decisions to score.")
        lines.append("")
        return "\n".join(lines)
    for c in resolved:
        v = _verifications(c)
        conf = v.get("confidence")
        conf_text = f"{int(conf)}%" if isinstance(conf, (int, float)) else "?"
        title = str(
            c.get("dispute") or c.get("normalized_key") or "(untitled dispute)"
        )
        lines.append(f"- {title}: {conf_text}")
    confs = [
        v.get("confidence")
        for c in resolved
        for v in [_verifications(c)]
        if isinstance(v.get("confidence"), (int, float))
    ]
    if confs:
        lines.append("")
        lines.append(f"Average: {sum(confs) / len(confs):.0f}%")
    lines.append("")
    return "\n".join(lines)


def render_appendix(
    verification_result: dict | None,
    governance_context: dict | None = None,
) -> str:
    """Audit visibility appendix. Reuses the decision_renderer audit sections
    (Verified Decisions / Tradeoffs / Human Decisions) demoted to h2 under
    '# Appendix', plus a deterministic Confidence summary. No invention: every
    line comes from verification_result (or governance_context, when a caller
    supplies it)."""
    parts = ["# Appendix", ""]
    parts.append(_demote_h1(
        render_verified_decisions(verification_result, governance_context)))
    parts.append(_demote_h1(
        render_verified_tradeoffs(verification_result, governance_context)))
    parts.append(_demote_h1(
        render_human_required(verification_result, governance_context)))
    parts.append(_confidence_summary(verification_result))
    return "\n\n".join(p.rstrip() for p in parts)


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------


def render_human_answer(
    question: str,
    verification_result: dict | None,
    synthesized_answer: str | None = None,
    execution_decision: Any = None,
    selected_mode: str | None = None,
) -> str:
    """Render the human-facing engineering report from the verified decision
    model.

    Deterministic. No LLM. No I/O. Only renders verified decisions
    (verification_result.resolved_claims). The audit `synthesized_answer` is
    accepted to match the required signature but the renderer never depends
    on its prose — verified decisions are the single source of truth, so the
    human answer can never invent or contradict the audit artifact.

    `selected_mode` (Phase 5) drives the Recommended Next Action: RESEARCH_ONLY
    -> Stop; RESEARCH_ARCHITECTURE -> Produce architecture package; FULL_BUILD
    -> Continue to Build Spec. When None, the action falls back to the
    execution_decision -> mode mapping (so the debate_engine call site, which
    cannot be modified to pass selected_mode, still produces a sensible
    recommendation).

    Returns the six canonical sections in order:
        # Executive Summary
        # Verified Architecture
        # Implementation Roadmap
        # Risks
        # Recommended Next Action
        # Appendix
    """
    # ponytail: synthesized_answer is accepted by contract but unused by
    # design — the human answer is decision-model-driven, not prose-driven.
    sections = [
        render_executive_summary(
            question, verification_result, execution_decision, selected_mode,
        ),
        render_verified_architecture(verification_result),
        render_implementation_roadmap(verification_result),
        render_risks_section(verification_result),
        render_recommended_next_action(
            verification_result, execution_decision, selected_mode,
        ),
        render_appendix(verification_result),
    ]
    return "\n\n".join(s.rstrip() for s in sections)


if __name__ == "__main__":
    # Deterministic smoke self-check — no model call, no I/O.
    vr = {
        "resolved_claims": [
            {"normalized_key": "execution_environment",
             "dispute": "Execution: Server-side Docker vs. Client-side WebContainers",
             "verification": {"status": "RESOLVED", "confidence": 99,
                              "finding": ("The dispute is resolved in favor of "
                                          "Server-side Docker. Both evidence strips "
                                          "explicitly reject Client-side "
                                          "WebContainers for the authoritative "
                                          "execution path.")}},
        ],
        "equivalent_claims": [], "unresolved_claims": [],
        "verification_score": 100, "truncated_count": 0,
    }
    exec_dec = {"execution_decision": _ARCHITECTURE_ONLY,
                "execution_reason": "arch", "execution_confidence": 85,
                "execution_decision_source": "architecture_decisions"}

    a = render_human_answer("Design the platform", vr, "audit-prose-ignored",
                            exec_dec)
    for h in HUMAN_HEADERS:
        assert h in a, f"missing section: {h}"
    assert a.count("# Executive Summary") == 1
    assert "Execution: Server-side Docker" in a
    assert "in favor of Server-side Docker" in a
    # ARCHITECTURE_ONLY -> RESEARCH_ARCHITECTURE fallback -> "Produce architecture package"
    assert "Produce architecture package" in a

    # Determinism: same inputs -> byte-identical output.
    b = render_human_answer("Design the platform", vr, "audit-prose-ignored",
                            exec_dec)
    assert a == b

    # selected_mode overrides the next action.
    full = render_human_answer("q", vr, "a", exec_dec, _MODE_FULL_BUILD)
    assert "Continue to Build Spec" in full
    research = render_human_answer("q", vr, "a", exec_dec, _MODE_RESEARCH_ONLY)
    assert research.split("# Verified Architecture")[0].count("research-only") >= 1
    assert "Stop — this outcome is research-only" in research

    # No-injection: a fabricated option never appears as a decision.
    assert "QuantumBroom" not in a

    # Empty verification_result still renders all six sections.
    empty = render_human_answer("q", None, None, None)
    for h in HUMAN_HEADERS:
        assert h in empty
    assert "No verified decisions were recorded." in empty
    assert "No implementation steps; nothing verified." in empty or \
           "No risks identified" in empty

    # The module exposes no model symbols and imports no model client.
    import sys as _sys
    forbidden = {"generate", "llm", "complete", "chat", "prompt"}
    assert not (forbidden & set(dir(_sys.modules[__name__])))
    assert "cerebras_client" not in _sys.modules, (
        "human_synthesis_renderer pulled in cerebras_client")

    print("human_synthesis_renderer smoke test PASSED")
