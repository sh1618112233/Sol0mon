"""
Project Sol0mon — Execution Decision Authority.

Single source of truth for the effective execution decision. Resolves the
conflicting-states failure mode the spec called out:

    Execution Decision:   RESEARCH_ONLY
    Execution Mode:        FULL_BUILD
    override_used:         True
    -> "Chaining FULL_BUILD -> build pipeline"

The bug: two parallel authorities (the persisted ``execution_decision`` and
the persisted ``execution_mode`` override) agreed to disagree, with no single
resolution. This module makes the precedence EXPLICIT and DETERMINISTIC so
only ONE effective decision exists after policy evaluation.

Precedence (highest wins; first rung that produces a non-None decision):

    1. Explicit user override   (mode_select_fn / persisted mode selection)
    2. Policy Engine             (the immutable persisted execution_decision
                                  inside verification_result — the verifier
                                  settled this; it is policy, not preference)
    3. Planner recommendation    (derived from the planner's work-package graph)
    4. Prompt fallback / default (deterministic classify_execution_decision
                                  over the user objective; safest-stop default)

Both the **Policy Decision** (what each input layer said) and the
**Effective Execution Decision** (the single winner) are logged and
returned, so the audit trail records WHY the pipeline ran the way it did.

The pipeline MUST execute only the effective decision. The build_pipeline
gate (``build_pipeline._resolve_execution_decision`` and
``execute_build_spec``) already enforces an ``execution_decision`` string —
this authority produces exactly that string plus the audit record. No
parallel gate is introduced; the existing gate continues to be the live
enforcer. This module is the resolve-then-log adapter.

Pure stdlib. Reuses ``execution_decision`` (deterministic classifier),
``execution_mode_gate`` (user mode mapping), and ``build_workspace`` (state
persistence) — no new behaviour, only one explicit resolution point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import execution_decision as ed
import execution_mode_gate as emg


# ---------------------------------------------------------------------------
# Source labels (the layer that supplied the decision)
# ---------------------------------------------------------------------------

SOURCE_OVERRIDE = "user_override"
SOURCE_POLICY = "policy_engine"
SOURCE_PLANNER = "planner_recommendation"
SOURCE_DEFAULT = "prompt_fallback"

# All four layers, in precedence order (highest first).
PRECEDENCE = (SOURCE_OVERRIDE, SOURCE_POLICY, SOURCE_PLANNER, SOURCE_DEFAULT)


# ---------------------------------------------------------------------------
# Layer evaluators — each returns a decision dict or None
# ---------------------------------------------------------------------------

def _layer_override(
    *,
    mode_select_fn: Callable[[str], tuple[str, bool]] | None,
    persisted_mode: dict | None,
    recommended_mode: str,
) -> dict | None:
    """Layer 1 — Explicit user override.

    Two ways a user override enters the system:
      (a) ``mode_select_fn`` is supplied (live CLI prompt) — call it with the
          recommended mode and apply the result.
      (b) a ``persisted_mode`` selection exists on the workspace (resume) —
          re-apply it via ``emg.decision_from_mode_selection``.

    Returns the override decision dict when the override actually CHANGES the
    depth (i.e. the selected mode differs from the recommended one, OR the
    persisted selection was an override). Returns None when no override is
    present or the user accepted the recommendation — so layers 2/3/4 get to
    speak.
    """
    selected_mode: str | None = None
    override_used = False

    if mode_select_fn is not None:
        selected_mode, override_used = mode_select_fn(recommended_mode)
    elif persisted_mode is not None:
        sel = persisted_mode.get(emg.KEY_SELECTED_MODE)
        if not sel:
            return None
        selected_mode = sel
        override_used = bool(persisted_mode.get(emg.KEY_OVERRIDE_USED, False))

    if not selected_mode:
        return None

    decision_str = emg.mode_to_decision(selected_mode)
    if decision_str is None:
        return None

    # An override that maps to the SAME depth as the recommendation is not a
    # real override — defer to lower layers and let the policy engine / planner
    # supply the reason. (This is the bug: the override "wins" but agrees with
    # the policy, so no conflict actually existed. Returning None here makes
    # the Policy Decision and Effective Decision match — single source of truth.)
    rec_decision = emg.mode_to_decision(recommended_mode)
    if not override_used and decision_str == rec_decision:
        return None

    return {
        ed.KEY_DECISION: decision_str,
        ed.KEY_REASON: (
            f"Execution Authority: explicit user override selected_mode="
            f"{selected_mode} (recommended_mode={recommended_mode}, "
            f"override_used={override_used})."
        ),
        ed.KEY_CONFIDENCE: 100,
        ed.KEY_SOURCE: SOURCE_OVERRIDE,
        emg.KEY_RECOMMENDED_MODE: recommended_mode,
        emg.KEY_SELECTED_MODE: selected_mode,
        emg.KEY_OVERRIDE_USED: override_used,
    }


def _layer_policy(
    *,
    verification_result: dict | None,
    workspace_state: dict | None,
) -> dict | None:
    """Layer 2 — Policy Engine.

    The Policy Decision is the immutable execution_decision the verifier
    recorded. Two sources (precedence within this layer):
      (a) ``verification_result[ed.KEY_DECISION]`` (the resurrected answer).
      (b) ``workspace_state[ed.KEY_DECISION]`` (build_state.json after a prior
          run).

    Returns the policy decision dict, or None when neither carries one.
    """
    from_vr = ed.from_verification_result(verification_result)
    if from_vr is not None:
        return {
            **from_vr,
            ed.KEY_SOURCE: SOURCE_POLICY,
        }
    if isinstance(workspace_state, dict):
        d = workspace_state.get(ed.KEY_DECISION)
        if d:
            return {
                ed.KEY_DECISION: d,
                ed.KEY_REASON: str(workspace_state.get(ed.KEY_REASON, "")),
                ed.KEY_CONFIDENCE: int(workspace_state.get(ed.KEY_CONFIDENCE, 0)),
                ed.KEY_SOURCE: SOURCE_POLICY,
            }
    return None


def _layer_planner(
    *,
    graph: dict | None,
    objective: str,
) -> dict | None:
    """Layer 3 — Planner recommendation.

    Derives a depth recommendation from the planner's execution graph:
      - any work package whose type is ``implementation`` → IMPLEMENTATION_REQUIRED
      - else any ``architecture`` package → ARCHITECTURE_ONLY
      - else (research / validation / documentation only) → RESEARCH_ONLY

    Returns None when no graph is supplied — the default layer then takes
    over (so a caller that bypasses the planner keeps the existing behaviour).
    """
    if not isinstance(graph, dict):
        return None
    packages = graph.get("work_packages") or []
    if not packages:
        return None
    types = {p.get("type") for p in packages if isinstance(p, dict)}
    if "implementation" in types:
        decision = ed.IMPLEMENTATION_REQUIRED
        reason = "Planner graph contains an implementation work package."
    elif "architecture" in types:
        decision = ed.ARCHITECTURE_ONLY
        reason = "Planner graph has architecture but no implementation packages."
    else:
        decision = ed.RESEARCH_ONLY
        reason = "Planner graph has only research/validation/documentation packages."
    return {
        ed.KEY_DECISION: decision,
        ed.KEY_REASON: reason,
        ed.KEY_CONFIDENCE: 85,
        ed.KEY_SOURCE: SOURCE_PLANNER,
    }


def _layer_default(objective: str) -> dict:
    """Layer 4 — Prompt fallback / default.

    Delegates to the existing deterministic classifier (prompt-fallback
    rung). Always returns a non-None decision — this is the floor of the
    ladder. The default is RESEARCH_ONLY when no implementation signal is
    detected (safest-stop — never silently auto-implement)."""
    out = ed.classify_execution_decision(question=objective or "")
    return {**out, ed.KEY_SOURCE: SOURCE_DEFAULT}


# ---------------------------------------------------------------------------
# Authority — single resolution point
# ---------------------------------------------------------------------------

def resolve(
    objective: str,
    *,
    graph: dict | None = None,
    verification_result: dict | None = None,
    workspace_state: dict | None = None,
    mode_select_fn: Callable[[str], tuple[str, bool]] | None = None,
    persisted_mode: dict | None = None,
) -> dict:
    """Resolve a single Effective Execution Decision from all input layers.

    Returns::

        {
          "policy_decision":   {layer: decision_dict | None, ...},  # the audit
          "effective_decision": {...},                              # the winner
          "effective_source":  SOURCE_OVERRIDE | SOURCE_POLICY | ...,
          "resolved_at":       iso,
        }

    ``effective_decision`` is always non-None (the default layer is the floor).
    The returned dict carries the standard ``execution_decision`` /
    ``execution_reason`` / ``execution_confidence`` /
    ``execution_decision_source`` keys the existing build-pipeline gate reads,
    so the gate remains the single live enforcer (no parallel enforcement).
    """
    # A recommended mode is needed before the override layer can act. We let
    # the policy / planner / default layers speak first to derive it, then
    # offer the user the choice.
    policy = _layer_policy(
        verification_result=verification_result,
        workspace_state=workspace_state,
    )
    planner = _layer_planner(graph=graph, objective=objective)
    default = _layer_default(objective)

    # Recommendation precedence for the menu: policy > planner > default
    # (the user is being asked what to do GIVEN what we already know).
    recommender = policy or planner or default
    recommended_mode = emg.decision_to_mode(
        recommender.get(ed.KEY_DECISION) if recommender else None,
    )

    override = _layer_override(
        mode_select_fn=mode_select_fn,
        persisted_mode=persisted_mode,
        recommended_mode=recommended_mode,
    )

    layers = {
        SOURCE_OVERRIDE: override,
        SOURCE_POLICY: policy,
        SOURCE_PLANNER: planner,
        SOURCE_DEFAULT: default,
    }

    effective = next(
        (layers[s] for s in PRECEDENCE if layers[s] is not None),
        default,  # guaranteed non-None
    )
    # Construct the final effective decision dict — carries the standard
    # contract keys the existing gate enforces, plus the audit mode keys when
    # an override was the winner.
    eff = {
        ed.KEY_DECISION: effective[ed.KEY_DECISION],
        ed.KEY_REASON: effective.get(ed.KEY_REASON, ""),
        ed.KEY_CONFIDENCE: int(effective.get(ed.KEY_CONFIDENCE, 0)),
        ed.KEY_SOURCE: effective.get(ed.KEY_SOURCE, SOURCE_DEFAULT),
        "blocked": ed.is_blocked_decision(effective[ed.KEY_DECISION]),
        "block_status": ed.block_status(effective[ed.KEY_DECISION]),
    }
    if emg.KEY_SELECTED_MODE in effective:
        eff[emg.KEY_RECOMMENDED_MODE] = effective.get(emg.KEY_RECOMMENDED_MODE,
                                                      recommended_mode)
        eff[emg.KEY_SELECTED_MODE] = effective[emg.KEY_SELECTED_MODE]
        eff[emg.KEY_OVERRIDE_USED] = effective.get(emg.KEY_OVERRIDE_USED, False)
    else:
        # Always carry the recommended mode so the audit trail records what
        # was on the menu even when the user did not override.
        eff[emg.KEY_RECOMMENDED_MODE] = recommended_mode

    return {
        "policy_decision": layers,
        "effective_decision": eff,
        "effective_source": eff[ed.KEY_SOURCE],
        "recommended_mode": recommended_mode,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def log_authority(resolution: dict) -> None:
    """Print the Policy Decision and Effective Execution Decision.

    One line per layer that spoke; the winner is logged last and labelled
    ``EFFECTIVE``. The build-pipeline gate enforces the effective decision;
    the policy decision trail is informational."""
    if not isinstance(resolution, dict):
        return
    print("[AUTHORITY] Policy Decision:", flush=True)
    for layer in PRECEDENCE:
        d = (resolution.get("policy_decision") or {}).get(layer)
        if d is None:
            continue
        print(f"[AUTHORITY]   {layer}: "
              f"{d.get(ed.KEY_DECISION)} "
              f"(source={d.get(ed.KEY_SOURCE, layer)}, "
              f"confidence={d.get(ed.KEY_CONFIDENCE, 0)})", flush=True)
    eff = resolution.get("effective_decision") or {}
    print(f"[AUTHORITY] EFFECTIVE: "
          f"{eff.get(ed.KEY_DECISION)} "
          f"(source={eff.get(ed.KEY_SOURCE)}, "
          f"recommended_mode={resolution.get('recommended_mode')}, "
          f"selected_mode={eff.get(emg.KEY_SELECTED_MODE, '-')}, "
          f"override_used={eff.get(emg.KEY_OVERRIDE_USED, False)})",
          flush=True)


if __name__ == "__main__":
    # ponytail self-check: every precedence rung + the single-effective guarantee.
    # 1. Override wins over policy and planner.
    r = resolve(
        "ship X", graph={"work_packages": [{"id": "i", "type": "implementation"}]},
        verification_result={ed.KEY_DECISION: ed.RESEARCH_ONLY, ed.KEY_REASON: "p",
                             ed.KEY_CONFIDENCE: 80, ed.KEY_SOURCE: "research"},
        mode_select_fn=lambda rec: (emg.FULL_BUILD, True),
    )
    assert r["effective_source"] == SOURCE_OVERRIDE
    assert r["effective_decision"][ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED
    # Policy said RESEARCH_ONLY but the override wins — single effective.
    assert r["policy_decision"][SOURCE_POLICY][ed.KEY_DECISION] == ed.RESEARCH_ONLY

    # 2. No override → Policy Engine wins.
    r2 = resolve(
        "ship X", graph={"work_packages": [{"id": "i", "type": "research"}]},
        verification_result={ed.KEY_DECISION: ed.ARCHITECTURE_ONLY,
                             ed.KEY_REASON: "p", ed.KEY_CONFIDENCE: 85,
                             ed.KEY_SOURCE: "arch"},
    )
    assert r2["effective_source"] == SOURCE_POLICY
    assert r2["effective_decision"][ed.KEY_DECISION] == ed.ARCHITECTURE_ONLY

    # 3. No policy → Planner wins.
    r3 = resolve(
        "ship X", graph={"work_packages": [
            {"id": "r", "type": "research"},
            {"id": "a", "type": "architecture"},
        ]},
    )
    assert r3["effective_source"] == SOURCE_PLANNER
    assert r3["effective_decision"][ed.KEY_DECISION] == ed.ARCHITECTURE_ONLY

    # 4. No planner → default (safest-stop for ambiguous prompt).
    r4 = resolve("hello world")
    assert r4["effective_source"] == SOURCE_DEFAULT
    assert r4["effective_decision"][ed.KEY_DECISION] == ed.RESEARCH_ONLY

    # 5. Override that AGREES with the recommendation is a no-op — the policy
    #    layer wins (no false conflict in the audit trail). When the override
    #    is a no-op, no override keys are carried.
    r5 = resolve(
        "ship X",
        verification_result={ed.KEY_DECISION: ed.IMPLEMENTATION_REQUIRED,
                             ed.KEY_REASON: "p", ed.KEY_CONFIDENCE: 90,
                             ed.KEY_SOURCE: "impl"},
        mode_select_fn=lambda rec: (emg.FULL_BUILD, False),  # accept rec
    )
    assert r5["effective_source"] == SOURCE_POLICY, r5
    assert emg.KEY_OVERRIDE_USED not in r5["effective_decision"] or \
           r5["effective_decision"][emg.KEY_OVERRIDE_USED] is False

    # 6. Persisted resume override wins.
    r6 = resolve(
        "ship X",
        graph={"work_packages": [{"id": "i", "type": "research"}]},
        persisted_mode={emg.KEY_RECOMMENDED_MODE: emg.RESEARCH_ONLY,
                        emg.KEY_SELECTED_MODE: emg.FULL_BUILD,
                        emg.KEY_OVERRIDE_USED: True},
    )
    assert r6["effective_source"] == SOURCE_OVERRIDE
    assert r6["effective_decision"][ed.KEY_DECISION] == ed.IMPLEMENTATION_REQUIRED

    log_authority(r)
    print("execution_authority self-check PASSED")
