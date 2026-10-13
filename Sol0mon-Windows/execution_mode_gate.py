"""
Project Sol0mon — Execution Mode Gate V1.

Bridges the gap the Debate #145 incident exposed: the existing
``execution_decision`` engine (V1/V2 — unchanged here) decides execution
depth AFTER verification, deterministically. That recommendation can disagree
with the user's intent — a debate that synthesizes architecture + verified
decisions may still classify ``RESEARCH_ONLY`` and OpenCode is never reached.

This gate lets the user CHOOSE the execution depth BEFORE the build runs, and
OVERRIDES the recommendation. The recommendation engine stays intact; this
layer only wraps it with a user-facing surface and an override.

User-facing execution modes (Phase 1):
    RESEARCH_ONLY          Research -> Debate -> Verification -> Answer
    RESEARCH_ARCHITECTURE  Research -> Debate -> Verification -> Architecture
                           -> Stop
    FULL_BUILD             Research -> Debate -> Verification -> Architecture
                           -> Build Spec -> OpenCode -> Repair Loop

Recommendation mapping (Phase 2 — pure wrapper over execution_decision, no
behavior change):
    execution_decision.RESEARCH_ONLY          -> RESEARCH_ONLY
    execution_decision.ARCHITECTURE_ONLY       -> RESEARCH_ARCHITECTURE
    execution_decision.IMPLEMENTATION_REQUIRED -> FULL_BUILD

Enforcement (Phase 5) maps the selected user mode back to the
``execution_decision`` string the existing build-pipeline gate
(``build_pipeline.run_build_pipeline`` Step 3.5) already enforces, so the
override rides the live, single authority rather than a parallel gate:
    RESEARCH_ONLY          -> RESEARCH_ONLY          (stop after synthesis)
    RESEARCH_ARCHITECTURE  -> ARCHITECTURE_ONLY      (architecture, no specs)
    FULL_BUILD             -> IMPLEMENTATION_REQUIRED (continue to build)

Persistence (Phase 4): ``recommended_mode`` / ``selected_mode`` /
``override_used`` are written into the build workspace ``build_state.json``
via ``build_workspace.write_build_state`` (merge semantics — co-exists with
the immutable ``execution_decision`` keys). Resume honors them: a fresh
``_resolve_execution_decision`` call re-applies the persisted mode without
re-prompting.

Pure stdlib at module import (only ``execution_decision``, which itself pulls
no LLM/browser/network client). Persistence helpers lazy-import
``build_workspace``.
"""

from __future__ import annotations

from typing import Callable

import execution_decision as ed

# ---------------------------------------------------------------------------
# Phase 1 — user-facing execution modes
# ---------------------------------------------------------------------------

RESEARCH_ONLY = "RESEARCH_ONLY"
RESEARCH_ARCHITECTURE = "RESEARCH_ARCHITECTURE"
FULL_BUILD = "FULL_BUILD"

ALL_MODES = (RESEARCH_ONLY, RESEARCH_ARCHITECTURE, FULL_BUILD)

# CLI menu entry -> user-facing mode.
_MENU_CHOICE_TO_MODE = {
    "1": RESEARCH_ONLY,
    "2": RESEARCH_ARCHITECTURE,
    "3": FULL_BUILD,
}

# ---------------------------------------------------------------------------
# Phase 2 — recommendation mapping (wraps execution_decision; no behavior change)
# ---------------------------------------------------------------------------

# execution_decision depth decisions -> recommended user-facing mode.
_DECISION_TO_MODE = {
    ed.RESEARCH_ONLY: RESEARCH_ONLY,
    ed.ARCHITECTURE_ONLY: RESEARCH_ARCHITECTURE,
    ed.IMPLEMENTATION_REQUIRED: FULL_BUILD,
}

# Phase 5 — selected user mode -> the execution_decision the existing
# build-pipeline gate already enforces. Riding the existing gate means no
# parallel enforcement path and zero change to the blocked-decision logic.
_MODE_TO_DECISION = {
    RESEARCH_ONLY: ed.RESEARCH_ONLY,
    RESEARCH_ARCHITECTURE: ed.ARCHITECTURE_ONLY,
    FULL_BUILD: ed.IMPLEMENTATION_REQUIRED,
}

# Persisted key names (live on build_state.json, namespaced so they never
# collide with the execution_decision keys written by the build pipeline).
KEY_RECOMMENDED_MODE = "recommended_mode"
KEY_SELECTED_MODE = "selected_mode"
KEY_OVERRIDE_USED = "override_used"

# Execution-decision source label stamped when the mode gate overrides the
# auto recommendation (keeps execution_decision_source distinct & auditable).
SOURCE_MODE_GATE = "execution_mode_gate"
SOURCE_MODE_GATE_PERSISTED = "execution_mode_gate_persisted"


# ---------------------------------------------------------------------------
# Phase 2 — recommendation helpers
# ---------------------------------------------------------------------------


def decision_to_mode(decision: str | dict | None) -> str:
    """Map an execution_decision (dict, str, or None) to a recommended
    user-facing mode. Unknown / safety-stop decisions default to
    RESEARCH_ONLY (the safest-stop — never silently auto-implement). Pure."""
    if decision is None:
        return RESEARCH_ONLY
    d = decision if isinstance(decision, str) else decision.get(ed.KEY_DECISION)
    return _DECISION_TO_MODE.get(d, RESEARCH_ONLY)


def recommend_mode_from_prompt(prompt: str) -> str:
    """Pre-debate recommendation: classify the prompt with the deterministic
    execution_decision engine (prompt-fallback rung) and map the result to a
    recommended user-facing mode. Reuses the existing engine verbatim."""
    decision = ed.classify_execution_decision(question=prompt)
    return decision_to_mode(decision)


# ---------------------------------------------------------------------------
# Phase 3 — user override (selection)
# ---------------------------------------------------------------------------


def resolve_mode(
    recommended_mode: str,
    choice: str | int | None,
) -> tuple[str, bool]:
    """Resolve a user menu choice into (selected_mode, override_used).

    choice values:
        None, "", or whitespace  -> accept the recommendation
        "1" / "2" / "3"          -> the matching mode
        any other value          -> invalid, accept the recommendation

    override_used is True iff the selected mode differs from the recommended
    one. Pure.
    """
    sel = _MENU_CHOICE_TO_MODE.get(str(choice).strip())
    if sel is None:
        # Blank / Enter / invalid -> accept the recommendation verbatim.
        return recommended_mode, False
    return sel, sel != recommended_mode


def prompt_execution_mode(
    recommended_mode: str,
    input_fn: Callable[[str], str] | None = None,
) -> tuple[str, bool]:
    """Interactive CLI selection (Phase 3). Displays the recommendation and
    the 3-option menu, reads the user's choice, and returns
    (selected_mode, override_used). Blank input accepts the recommendation.

    ``input_fn`` is injectable for tests (defaults to the builtin ``input``);
    EOFError / KeyboardInterrupt gracefully accept the recommendation so a
    non-interactive shell never deadlocks.
    """
    print(f"\nRecommended Mode: {recommended_mode}\n")
    print("Select:")
    print("  1. Research Only")
    print("  2. Research + Architecture")
    print("  3. Research + Architecture + Implementation")
    print("Press Enter to accept recommendation.")
    if input_fn is None:
        input_fn = input
    try:
        choice = input_fn("> ")
    except (EOFError, KeyboardInterrupt, OSError):
        # Non-interactive stdin (piped shell, pytest capsys, container with no
        # tty): accept the recommendation rather than deadlocking or crashing.
        return recommended_mode, False
    return resolve_mode(recommended_mode, choice)


def fixed_selection_fn(
    selected_mode: str, override_used: bool
) -> Callable[[str], tuple[str, bool]]:
    """Build a mode_select_fn that returns a pre-resolved selection without
    re-prompting. Used when the selection was already made (e.g. the debate
    command prompts once, then chains to the build pipeline) and by tests that
    want to force a specific mode deterministically."""
    def _fn(_recommended_mode: str) -> tuple[str, bool]:
        return selected_mode, override_used
    return _fn


# ---------------------------------------------------------------------------
# Phase 5 — pipeline enforcement helpers
# ---------------------------------------------------------------------------


def mode_to_decision(selected_mode: str | None) -> str | None:
    """Map a selected user mode back to the execution_decision string the
    existing build-pipeline gate enforces. Unknown / None -> None (the caller
    lets the auto recommendation stand). Pure."""
    if not selected_mode:
        return None
    return _MODE_TO_DECISION.get(selected_mode)


def mode_allows_build(selected_mode: str | None) -> bool:
    """True iff this mode continues to the build pipeline (spec + OpenCode)."""
    return selected_mode == FULL_BUILD


def mode_allows_architecture(selected_mode: str | None) -> bool:
    """True iff this mode produces architecture (stops before build specs)."""
    return selected_mode in (RESEARCH_ARCHITECTURE, FULL_BUILD)


def apply_mode_override(
    exec_decision: dict,
    selected_mode: str,
    recommended_mode: str,
    override_used: bool,
    source: str = SOURCE_MODE_GATE,
) -> dict:
    """Return a new execution-decision dict reflecting the user-selected mode,
    or the input unchanged when the mode does not change the decision (user
    accepted the recommendation, or the mode maps to the same depth).

    ``exec_decision`` is the auto/persisted decision dict produced by
    ``build_pipeline._resolve_execution_decision`` (carries execution_decision
    / execution_reason / execution_confidence / execution_decision_source /
    blocked / block_status / source). When the override changes the decision,
    the returned dict re-stamps source, reason, and the mode keys so the
    pipeline + audit trail record the override. Pure."""
    override_decision = mode_to_decision(selected_mode)
    current = (exec_decision or {}).get(ed.KEY_DECISION)
    if override_decision is None or override_decision == current:
        # No depth change — but still carry the mode keys for the audit trail.
        out = dict(exec_decision or {})
        out[KEY_RECOMMENDED_MODE] = recommended_mode
        out[KEY_SELECTED_MODE] = selected_mode
        out[KEY_OVERRIDE_USED] = override_used
        return out
    return {
        ed.KEY_DECISION: override_decision,
        ed.KEY_REASON: (
            f"Execution Mode Gate V1 override: user selected {selected_mode} "
            f"(recommended {recommended_mode}, override_used={override_used}); "
            f"the auto recommendation is overridden."
        ),
        ed.KEY_CONFIDENCE: 100,
        ed.KEY_SOURCE: source,
        "blocked": ed.is_blocked_decision(override_decision),
        "block_status": ed.block_status(override_decision),
        "source": "mode_gate",
        KEY_RECOMMENDED_MODE: recommended_mode,
        KEY_SELECTED_MODE: selected_mode,
        KEY_OVERRIDE_USED: override_used,
    }


def decision_from_mode_selection(
    mode_selection: dict,
    source: str = SOURCE_MODE_GATE_PERSISTED,
) -> dict | None:
    """Build an execution-decision dict directly from a persisted mode
    selection (the resume path). The persisted mode is authoritative on
    resume — it is mapped to its execution_decision and returned with the
    mode keys carried, without recomputing the auto recommendation or
    re-prompting. Returns None when the selection does not map to a decision
    (caller falls back to the auto resolver). Pure.

    ``mode_selection`` is the dict returned by ``load_mode_selection``:
    ``{recommended_mode, selected_mode, override_used}``."""
    if not mode_selection:
        return None
    sel = mode_selection.get(KEY_SELECTED_MODE)
    rec = mode_selection.get(KEY_RECOMMENDED_MODE) or decision_to_mode(sel)
    used = bool(mode_selection.get(KEY_OVERRIDE_USED, False))
    decision_str = mode_to_decision(sel)
    if decision_str is None:
        return None
    return {
        ed.KEY_DECISION: decision_str,
        ed.KEY_REASON: (
            f"Execution Mode Gate V1 (resume): persisted selected_mode={sel}"
            f", recommended_mode={rec}, override_used={used}."
        ),
        ed.KEY_CONFIDENCE: 100,
        ed.KEY_SOURCE: source,
        "blocked": ed.is_blocked_decision(decision_str),
        "block_status": ed.block_status(decision_str),
        "source": "mode_gate_persisted",
        KEY_RECOMMENDED_MODE: rec,
        KEY_SELECTED_MODE: sel,
        KEY_OVERRIDE_USED: used,
    }


# ---------------------------------------------------------------------------
# Phase 4 — workspace persistence
# ---------------------------------------------------------------------------


def save_mode_selection(
    workspace_id: str,
    recommended_mode: str,
    selected_mode: str,
    override_used: bool,
) -> dict:
    """Persist the mode selection into the build workspace build_state.json.
    Uses build_workspace.write_build_state (merge semantics) so the mode keys
    co-exist with the immutable execution_decision keys already on the state.

    Returns the merged build_state dict that was written."""
    from build_workspace import write_build_state
    return write_build_state(
        workspace_id, "execution_mode_selected",
        **{
            KEY_RECOMMENDED_MODE: recommended_mode,
            KEY_SELECTED_MODE: selected_mode,
            KEY_OVERRIDE_USED: bool(override_used),
        },
    )


def load_mode_selection(workspace_id: str) -> dict | None:
    """Read the persisted mode selection from build_state.json. Returns None
    when no selection was persisted (fresh / legacy workspace). The resume
    path uses this to re-apply the stored mode without re-prompting."""
    import json
    from build_workspace import _workspace_dir

    p = _workspace_dir(workspace_id) / "build_state.json"
    if not p.is_file():
        return None
    try:
        state = json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return None
    sel = state.get(KEY_SELECTED_MODE)
    if not sel:
        return None
    return {
        KEY_RECOMMENDED_MODE: state.get(KEY_RECOMMENDED_MODE),
        KEY_SELECTED_MODE: sel,
        KEY_OVERRIDE_USED: bool(state.get(KEY_OVERRIDE_USED, False)),
    }


# ---------------------------------------------------------------------------
# Self-check (no I/O, no model call)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Phase 2: recommendation mapping wraps the deterministic engine.
    assert recommend_mode_from_prompt("Survey vector databases") == RESEARCH_ONLY
    assert recommend_mode_from_prompt("Design a streaming platform") == RESEARCH_ARCHITECTURE
    assert recommend_mode_from_prompt("Implement the auth module") == FULL_BUILD

    # Phase 3: override — Enter accepts, invalid falls back, 1/2/3 select.
    assert resolve_mode(FULL_BUILD, "") == (FULL_BUILD, False)
    assert resolve_mode(FULL_BUILD, None) == (FULL_BUILD, False)
    assert resolve_mode(FULL_BUILD, "x") == (FULL_BUILD, False)
    assert resolve_mode(FULL_BUILD, "1") == (RESEARCH_ONLY, True)
    assert resolve_mode(RESEARCH_ONLY, "3") == (FULL_BUILD, True)
    assert resolve_mode(RESEARCH_ARCHITECTURE, "2") == (RESEARCH_ARCHITECTURE, False)

    # Phase 3: injected input_fn — Enter -> accept.
    assert prompt_execution_mode(FULL_BUILD, input_fn=lambda _p: "") == (FULL_BUILD, False)
    assert prompt_execution_mode(FULL_BUILD, input_fn=lambda _p: "1") == (RESEARCH_ONLY, True)

    # Phase 5: enforcement mapping maps back to the existing gate's decisions.
    assert mode_to_decision(RESEARCH_ONLY) == ed.RESEARCH_ONLY
    assert mode_to_decision(RESEARCH_ARCHITECTURE) == ed.ARCHITECTURE_ONLY
    assert mode_to_decision(FULL_BUILD) == ed.IMPLEMENTATION_REQUIRED
    assert mode_to_decision(None) is None
    assert mode_allows_build(FULL_BUILD) is True
    assert mode_allows_build(RESEARCH_ONLY) is False
    assert mode_allows_architecture(RESEARCH_ARCHITECTURE) is True
    assert mode_allows_architecture(FULL_BUILD) is True
    assert mode_allows_architecture(RESEARCH_ONLY) is False

    # Phase 5: override re-stamps the decision + preserves mode keys.
    auto = {
        ed.KEY_DECISION: ed.RESEARCH_ONLY,
        ed.KEY_REASON: "auto",
        ed.KEY_CONFIDENCE: 80,
        ed.KEY_SOURCE: ed.SOURCE_RESEARCH_ANALYSIS,
        "blocked": True, "block_status": "research_complete",
        "source": "persisted",
    }
    over = apply_mode_override(auto, FULL_BUILD, RESEARCH_ONLY, True)
    assert over[ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED
    assert over["blocked"] is False
    assert over[ed.KEY_SOURCE] == SOURCE_MODE_GATE
    assert over[KEY_SELECTED_MODE] == FULL_BUILD
    assert over[KEY_OVERRIDE_USED] is True
    # Accepting the recommendation -> depth unchanged, mode keys still carried.
    unchanged = apply_mode_override(auto, RESEARCH_ONLY, RESEARCH_ONLY, False)
    assert unchanged[ed.KEY_DECISION] == ed.RESEARCH_ONLY
    assert unchanged[ed.KEY_SOURCE] == ed.SOURCE_RESEARCH_ANALYSIS
    assert unchanged[KEY_SELECTED_MODE] == RESEARCH_ONLY

    # fixed_selection_fn returns the canned selection verbatim.
    fn = fixed_selection_fn(FULL_BUILD, True)
    assert fn("RESEARCH_ONLY") == (FULL_BUILD, True)

    # decision_from_mode_selection — resume path: persisted mode is authority.
    persisted = decision_from_mode_selection(
        {KEY_RECOMMENDED_MODE: RESEARCH_ONLY, KEY_SELECTED_MODE: FULL_BUILD,
         KEY_OVERRIDE_USED: True})
    assert persisted[ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED
    assert persisted["blocked"] is False
    assert persisted[KEY_SELECTED_MODE] == FULL_BUILD
    assert persisted[ed.KEY_SOURCE] == SOURCE_MODE_GATE_PERSISTED
    persisted_research = decision_from_mode_selection(
        {KEY_RECOMMENDED_MODE: FULL_BUILD, KEY_SELECTED_MODE: RESEARCH_ONLY,
         KEY_OVERRIDE_USED: True})
    assert persisted_research[ed.KEY_DECISION] == ed.RESEARCH_ONLY
    assert persisted_research["blocked"] is True
    assert persisted_research["block_status"] == "research_complete"
    assert decision_from_mode_selection(None) is None

    print("execution_mode_gate V1 smoke test PASSED")
