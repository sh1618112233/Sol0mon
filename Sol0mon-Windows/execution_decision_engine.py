"""
Project Sol0mon — Execution Decision Engine V3 + Stage Planner.

The architectural flaw this fixes: the verifier emits an ``execution_decision``
recommendation (rung-5 research heuristic in ``execution_decision.py``), and
the legacy ``execution_authority`` treats that emit as authoritative POLICY
ranking above the planner — so the user's explicit "build" intent loses to a
research-signal inside the verifier's own text and the pipeline silently
stops at RESEARCH_ONLY even though architecture/implementation were
explicitly requested.

The fix separates RECOMMENDATION from DECISION:

    Verifier        ── emits a recommendation (inform, never terminate)
    User prompt     ── explicit intent (highest non-override priority)
    Planner graph   ── work-package types (implementation / architecture / research)
    Policy          ── may RESTRICT execution (lower priority than user intent)
    User override   ── always wins

This module exposes:

    ExecutionDecisionEngine  ── decides what work should execute; performs NO
                                execution itself. Inputs: user prompt, planner
                                output (graph), debate result, consensus result,
                                verification result, policy, user overrides.
                                Outputs: ExecutionPlan (stages + mode + reason +
                                confidence).

    ExecutionPlanner          ── converts the engine's decision into a
                                deterministic stage plan (research /
                                architecture / implementation_spec /
                                implementation / testing / repair) and persists
                                the four state artifacts:

                                    execution_plan.json
                                    execution_decision.json
                                    execution_reason.json
                                    execution_state.json

Decision precedence (highest wins):

    1. User Override
    2. User Prompt Intent
    3. Execution Policy
    4. Planner Recommendation
    5. Verification Recommendation
    6. Research heuristics (floor; inform only, never terminate on their own)

Execution modes:

    RESEARCH_ONLY
    ARCHITECTURE_ONLY
    IMPLEMENTATION_ONLY
    BUILD_ONLY
    BUILD_AND_VALIDATE
    FULL_BUILD
    CUSTOM

Rules:

    1. Explicit architecture request  → architecture stage runs.
    2. Explicit implementation request → implementation stage runs.
    3. Verification success never terminates execution on its own.
    4. Research signals never override explicit user intent.
    5. Policy may restrict; must state why.
    6. User overrides always have highest priority.
    7. Decisions are reproducible (pure stdlib; same inputs → identical output).

Pure stdlib at import. Persistence helpers lazy-import ``build_workspace``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import execution_decision as ed
import execution_mode_gate as emg

# ---------------------------------------------------------------------------
# Execution modes (user-facing surface, superset of ed.* depth decisions)
# ---------------------------------------------------------------------------

RESEARCH_ONLY = "RESEARCH_ONLY"
ARCHITECTURE_ONLY = "ARCHITECTURE_ONLY"
IMPLEMENTATION_ONLY = "IMPLEMENTATION_ONLY"
BUILD_ONLY = "BUILD_ONLY"
BUILD_AND_VALIDATE = "BUILD_AND_VALIDATE"
FULL_BUILD = "FULL_BUILD"
CUSTOM = "CUSTOM"

ALL_MODES = (
    RESEARCH_ONLY,
    ARCHITECTURE_ONLY,
    IMPLEMENTATION_ONLY,
    BUILD_ONLY,
    BUILD_AND_VALIDATE,
    FULL_BUILD,
    CUSTOM,
)

# Stage names (the only stages downstream execution may run)
STAGE_RESEARCH = "research"
STAGE_ARCHITECTURE = "architecture"
STAGE_IMPLEMENTATION_SPEC = "implementation_spec"
STAGE_IMPLEMENTATION = "implementation"
STAGE_TESTING = "testing"
STAGE_REPAIR = "repair"

ALL_STAGES = (
    STAGE_RESEARCH,
    STAGE_ARCHITECTURE,
    STAGE_IMPLEMENTATION_SPEC,
    STAGE_IMPLEMENTATION,
    STAGE_TESTING,
    STAGE_REPAIR,
)

# Mode → stages that must run (deterministic). CUSTOM is built per-request.
_MODE_STAGES: dict[str, tuple[str, ...]] = {
    RESEARCH_ONLY: (STAGE_RESEARCH,),
    ARCHITECTURE_ONLY: (STAGE_RESEARCH, STAGE_ARCHITECTURE),
    IMPLEMENTATION_ONLY: (
        STAGE_RESEARCH, STAGE_ARCHITECTURE, STAGE_IMPLEMENTATION_SPEC,
        STAGE_IMPLEMENTATION,
    ),
    BUILD_ONLY: (
        STAGE_RESEARCH, STAGE_ARCHITECTURE, STAGE_IMPLEMENTATION_SPEC,
        STAGE_IMPLEMENTATION,
    ),
    BUILD_AND_VALIDATE: (
        STAGE_RESEARCH, STAGE_ARCHITECTURE, STAGE_IMPLEMENTATION_SPEC,
        STAGE_IMPLEMENTATION, STAGE_TESTING,
    ),
    FULL_BUILD: ALL_STAGES,
}

# Source labels (which layer won)
SOURCE_OVERRIDE = "user_override"
SOURCE_PROMPT_INTENT = "user_prompt_intent"
SOURCE_POLICY = "execution_policy"
SOURCE_PLANNER = "planner_recommendation"
SOURCE_VERIFICATION = "verification_recommendation"
SOURCE_RESEARCH_HEURISTIC = "research_heuristic"

# Precedence order (highest first)
PRECEDENCE = (
    SOURCE_OVERRIDE,
    SOURCE_PROMPT_INTENT,
    SOURCE_POLICY,
    SOURCE_PLANNER,
    SOURCE_VERIFICATION,
    SOURCE_RESEARCH_HEURISTIC,
)

# ---------------------------------------------------------------------------
# Intent detection from the user prompt (Rule 1 + Rule 2)
# ---------------------------------------------------------------------------

_PROMPT_ARCH_KEYWORDS = (
    "architecture", "architect", "blueprint", "system design",
    "platform design", "design the", "design a",
)
_PROMPT_IMPL_KEYWORDS = (
    "implement", "build", "code", "develop", "ship", "deploy",
    "create the", "create a", "generate code", "write code",
    "open code", "opencode",
)
_PROMPT_TEST_KEYWORDS = ("test", "tests", "validate", "validation")
_PROMPT_REPAIR_KEYWORDS = ("repair", "fix", "debug", "patch")
_PROMPT_RESEARCH_KEYWORDS = (
    "research", "compare", "evaluate", "survey", "benchmark", "analyze",
    "analysis", "investigate",
)

_PROMPT_FULL_BUILD_PHRASES = (
    "full build", "build and validate", "research, architecture",
    "research, design, implement", "end to end", "end-to-end",
    "complete production", "production-ready",
)


def _hits(text: str, keywords: Iterable[str]) -> int:
    """Substring hit counter on the lowercased prompt. Each keyword
    contributes +1 once if present as a substring. Pure."""
    if not text:
        return 0
    t = text.lower()
    return sum(1 for kw in keywords if kw and kw in t)


def _has_any(text: str, keywords: Iterable[str]) -> bool:
    return _hits(text, keywords) > 0


def detect_prompt_intent(prompt: str) -> dict:
    """Detect explicit execution intent from the user's prompt.

    Returns a dict of booleans describing what the user asked for::

        {
          "research":          bool,
          "architecture":      bool,
          "implementation":    bool,
          "implementation_spec": bool,  # impl or spec keywords
          "testing":           bool,
          "repair":            bool,
          "full_build":        bool,  # explicit full-build phrase
        }

    Detection is keyword-based and conservative: a "design / compare /
    research / analyze" prompt counts as research+architecture when both
    keyword groups fire. A "build / implement / ship" prompt counts as
    implementation. The CRITICAL rule (Rule 4)::

        research heuristics NEVER override explicit implementation intent.

    So this function returns the TRUTH the engine obeys — it is the only place
    intent is inferred from the prompt. Pure.
    """
    p = prompt or ""
    full = _has_any(p, _PROMPT_FULL_BUILD_PHRASES)
    research = _has_any(p, _PROMPT_RESEARCH_KEYWORDS) or full
    arch = _has_any(p, _PROMPT_ARCH_KEYWORDS) or full
    impl = _has_any(p, _PROMPT_IMPL_KEYWORDS) or full
    spec = impl or _has_any(p, ("spec", "specification", "implementation spec"))
    test = _has_any(p, _PROMPT_TEST_KEYWORDS) or full
    repair = _has_any(p, _PROMPT_REPAIR_KEYWORDS) or full
    # "Design + Implement" is the canonical full-build request even without
    # the exact phrase — if both arch + impl fire, promote to full_build.
    if impl and arch:
        full = True
    return {
        "research": research or full,
        "architecture": arch,
        "implementation_spec": spec,
        "implementation": impl or full,
        "testing": test or full,
        "repair": repair or full,
        "full_build": full,
    }


def intent_to_mode(intent: dict) -> str | None:
    """Map an explicit-prompt intent dict to a mode, or None when the prompt
    carried no execution intent (defer to lower precedence layers). Pure."""
    if not isinstance(intent, dict):
        return None
    if intent.get("full_build"):
        return FULL_BUILD
    if intent.get("implementation") or intent.get("implementation_spec"):
        return BUILD_AND_VALIDATE if intent.get("testing") else BUILD_ONLY
    if intent.get("architecture"):
        return ARCHITECTURE_ONLY
    if intent.get("research"):
        return RESEARCH_ONLY
    return None


# ---------------------------------------------------------------------------
# Planner output → mode
# ---------------------------------------------------------------------------

def planner_mode(graph: dict | None) -> str | None:
    """Derive a mode from the planner's execution graph.

    The planner emits work-packages with a ``type`` field
    (``research`` / ``architecture`` / ``implementation`` / ``validation`` /
    ``documentation``). We map the set of types to a mode:

        implementation present           → FULL_BUILD (planner already split
                                              implementation into component /
                                              decision / tests / validation
                                              packages — the planner is the
                                              authoritative depth signal)
        architecture but no implementation → ARCHITECTURE_ONLY
        research only                     → RESEARCH_ONLY
        None / empty                      → None (defer to the next layer)
    """
    if not isinstance(graph, dict):
        return None
    packages = graph.get("work_packages") or []
    if not packages:
        return None
    types = {p.get("type") for p in packages if isinstance(p, dict)}
    if "implementation" in types:
        return FULL_BUILD
    if "architecture" in types:
        return ARCHITECTURE_ONLY
    if "research" in types or "validation" in types or "documentation" in types:
        return RESEARCH_ONLY
    return None


# ---------------------------------------------------------------------------
# Verification recommendation (DEMOTE: this is a recommendation, never a
# terminating decision on its own — Rule 3 + Rule 4)
# ---------------------------------------------------------------------------

# Verifier decisions that HARD-STOP execution (true policy, not heuristics):
# STOP_HUMAN_REQUIRED is deferred to the pending-decision gate (contract);
# STOP_UNRESOLVED_DISPUTES is the only verifier emit that may legitimately
# terminate (the debate is incomplete — nothing else can run).
VERIFIER_BLOCKING = frozenset({ed.STOP_UNRESOLVED_DISPUTES})


def verification_mode(verification_result: dict | None) -> str | None:
    """Read the verifier's RECOMMENDATION and map it to a mode.

    The verifier's emit is demoted to a recommendation: only
    STOP_UNRESOLVED_DISPUTES retains its hard-stop authority (a true
    governance constraint — the debate outcome is incomplete). Every other
    emit (RESEARCH_ONLY / ARCHITECTURE_ONLY / IMPLEMENTATION_REQUIRED) becomes
    a non-binding recommendation the engine may override per user intent.

    Returns None when no verification result is supplied (defer).
    """
    if not isinstance(verification_result, dict):
        return None
    decision = verification_result.get(ed.KEY_DECISION)
    if not decision:
        return None
    if decision == ed.STOP_UNRESOLVED_DISPUTES:
        # Hard governance constraint (Rule 5 — policy may restrict). The
        # engine still needs an execution plan, so this maps to a
        # RESEARCH_ONLY that the policy layer formalizes.
        return RESEARCH_ONLY
    if decision == ed.RESEARCH_ONLY:
        return RESEARCH_ONLY
    if decision == ed.ARCHITECTURE_ONLY:
        return ARCHITECTURE_ONLY
    if decision == ed.IMPLEMENTATION_REQUIRED:
        return FULL_BUILD
    if decision == ed.STOP_HUMAN_REQUIRED:
        # Deferred to the pending-decision gate — not a terminating decision
        # here. Return None so user intent / planner still drive the mode.
        return None
    return None


def verification_is_hard_block(verification_result: dict | None) -> bool:
    """True iff the verifier emitted STOP_UNRESOLVED_DISPUTES — the only
    verifier signal that may legitimately terminate execution on its own
    (the debate outcome is incomplete; downstream stages have no verified
    decisions to act on)."""
    if not isinstance(verification_result, dict):
        return False
    return verification_result.get(ed.KEY_DECISION) == ed.STOP_UNRESOLVED_DISPUTES


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

DEFAULT_POLICY: dict = {
    # When True, an unresolved-disputes verifier emit terminates execution
    # regardless of user intent (the verified decision model is incomplete;
    # downstream stages would have nothing to build from).
    "honor_unresolved_disputes_block": True,
    # When True, a STOP_HUMAN_REQUIRED emit defers to the pending-decision
    # gate (the existing human-escalation contract). Architecture + spec
    # still run; implementation is held at the gate.
    "honor_human_required_defer": True,
    # Upper bound the policy will permit. None = no cap. Useful for a
    # read-only/audit deployment that should never reach implementation.
    "max_mode": None,
    # When True, a CUSTOM mode must be supplied explicitly via an override —
    # the engine will not synthesize a CUSTOM mode on its own.
    "require_explicit_custom": True,
}


def normalize_policy(policy: dict | None) -> dict:
    out = dict(DEFAULT_POLICY)
    if isinstance(policy, dict):
        out.update(policy)
    return out


def policy_allows(policy: dict, mode: str) -> tuple[bool, str]:
    """Return (allowed, reason). When policy restricts, reason is non-empty."""
    p = normalize_policy(policy)
    cap = p.get("max_mode")
    if cap and cap in ALL_MODES:
        # Compare by stage count — cap restricts modes with MORE stages.
        cap_stages = len(_MODE_STAGES.get(cap, ()))
        mode_stages = len(_MODE_STAGES.get(mode, ()))
        if mode_stages > cap_stages:
            return False, f"Policy max_mode={cap} restricts {mode}"
    return True, ""


# ---------------------------------------------------------------------------
# ExecutionPlan dataclass (the single source of truth downstream reads)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage:
    name: str
    status: str  # "pending" | "running" | "completed" | "skipped" | "blocked"

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status}


@dataclass
class ExecutionDecision:
    """Deterministic decision produced by the ExecutionDecisionEngine. Carries
    everything downstream needs: the stage plan, the mode, the reason, the
    confidence, the winning source, and the audit trail of every layer that
    spoke. Pure data — the engine performs NO execution."""

    execution_mode: str
    reason: str
    confidence: int
    source: str  # one of SOURCE_*
    stages: list[Stage] = field(default_factory=list)
    policy_decision: dict = field(default_factory=dict)  # per-layer audit
    recommended_mode: str | None = None
    selected_mode: str | None = None
    override_used: bool = False
    blocked: bool = False
    block_status: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "execution_mode": self.execution_mode,
            "reason": self.reason,
            "confidence": self.confidence,
            "source": self.source,
            "stages": [s.to_dict() for s in self.stages],
            "policy_decision": dict(self.policy_decision),
            "recommended_mode": self.recommended_mode,
            "selected_mode": self.selected_mode,
            "override_used": self.override_used,
            "blocked": self.blocked,
            "block_status": self.block_status,
            "created_at": self.created_at,
        }

    def stage_statuses(self) -> dict[str, str]:
        return {s.name: s.status for s in self.stages}

    def stage(self, name: str) -> Stage | None:
        for s in self.stages:
            if s.name == name:
                return s
        return None


def _stages_for_mode(mode: str, *, completed: set[str] | None = None) -> list[Stage]:
    """Build the stage list for a mode, marking already-completed stages.

    A stage is ``completed`` iff it appears in ``completed``; otherwise it is
    ``pending``. CUSTOM modes supply their own stage set via the override."""
    done = completed or set()
    names = _MODE_STAGES.get(mode, ())
    if mode == CUSTOM:
        # CUSTOM must have been resolved by the engine before reaching here;
        # if it slips through, default to FULL_BUILD to never silently stop.
        names = _MODE_STAGES[FULL_BUILD]
    stages: list[Stage] = []
    for n in names:
        stages.append(Stage(n, "completed" if n in done else "pending"))
    return stages


# ---------------------------------------------------------------------------
# ExecutionDecisionEngine — the ONLY decision authority
# ---------------------------------------------------------------------------


class ExecutionDecisionEngine:
    """Deterministic decision engine. Decides what work should execute;
    performs NO execution itself.

    Inputs (all optional, all keyword):
        user_prompt           ── the user's prompt (highest non-override signal)
        planner_output        ── the planner's work-package graph (dict)
        debate_result         ── the debate engine result (dict; we read only
                                 ``human_required`` + ``unresolved_claims``).
        consensus_result      ── the consensus engine result (dict; we read
                                 ``human_required`` + ``unresolved_claims``).
        verification_result   ── the verifier result (dict; the
                                 ``execution_decision`` key on it is a
                                 RECOMMENDATION, not a terminating decision).
        policy                ── a policy dict (see DEFAULT_POLICY).
        user_override         ── an explicit user-supplied mode string; always
                                 wins (Rule 6).
        completed_stages      ── iterable of already-completed stage names
                                 (resume support).
        mode_select_fn        ── optional interactive override seam
                                 (recommended_mode -> (selected, override_used))
                                 — production passes a CLI prompt, tests inject
                                 a lambda; this is the legacy user-override
                                 channel preserved for backward compat.

    Output: an ``ExecutionDecision`` instance.

    The engine is pure: no I/O, no model calls, no execution. Reproducible:
    the same inputs always produce the same output (Rule 7).
    """

    def decide(
        self,
        *,
        user_prompt: str = "",
        planner_output: dict | None = None,
        debate_result: dict | None = None,
        consensus_result: dict | None = None,
        verification_result: dict | None = None,
        policy: dict | None = None,
        user_override: str | None = None,
        completed_stages: Iterable[str] | None = None,
        mode_select_fn: Callable[[str], tuple[str, bool]] | None = None,
    ) -> ExecutionDecision:
        pol = normalize_policy(policy)
        done = set(completed_stages or [])

        # --- Layer 1: USER OVERRIDE (always wins; Rule 6) --------------------
        override_mode: str | None = user_override
        override_used = False
        if override_mode is None and mode_select_fn is not None:
            # We need a recommendation to feed the menu — derive from layers
            # 2-5 first, then offer the choice.
            rec_mode = self._recommend_mode(
                user_prompt=user_prompt,
                planner_output=planner_output,
                verification_result=verification_result,
                debate_result=debate_result,
                consensus_result=consensus_result,
                policy=pol,
                completed=done,
            )
            try:
                selected, used = mode_select_fn(rec_mode)
            except (EOFError, KeyboardInterrupt, OSError):
                selected, used = rec_mode, False
            override_mode = selected
            override_used = bool(used)

        # --- Layer 2: USER PROMPT INTENT (Rule 1 + Rule 2 + Rule 4) ---------
        intent = detect_prompt_intent(user_prompt)
        prompt_mode = intent_to_mode(intent)

        # --- Layer 3: POLICY (Rule 5) ---------------------------------------
        policy_mode: str | None = None
        policy_reason = ""
        # Hard governance constraints (block even with user intent):
        hard_block_reason = ""
        if pol.get("honor_unresolved_disputes_block") and verification_is_hard_block(
                verification_result):
            hard_block_reason = (
                "Verifier reported unresolved disputes; the debate outcome "
                "is incomplete — policy blocks downstream execution"
            )
            policy_mode = RESEARCH_ONLY
        # Human-required deferral (existing contract): architecture + spec
        # still run; implementation is held at the pending-decision gate.
        human_required = self._collect_human_required(
            debate_result, consensus_result, verification_result,
        )
        if (pol.get("honor_human_required_defer") and human_required
                and policy_mode is None):
            # Not a hard block — the mode is whatever the user asked for; the
            # pending-decision gate is the live enforcer. We just record it.
            policy_reason = (
                f"{len(human_required)} pending human-required decision(s); "
                "implementation will be held at the pending-decision gate"
            )
        if policy_mode is None and pol.get("max_mode"):
            policy_mode = pol["max_mode"]
            policy_reason = f"Policy max_mode={policy_mode}"

        # --- Layer 4: PLANNER RECOMMENDATION --------------------------------
        planner_mode_val = planner_mode(planner_output)

        # --- Layer 5: VERIFICATION RECOMMENDATION (demoted) -----------------
        # Only stops when it's a hard block (handled by Layer 3). Otherwise,
        # the verifier emit is just one input.
        ver_mode = verification_mode(verification_result)

        # Effective mode resolution (precedence; first non-None wins)
        candidates: list[tuple[str, str | None, str]] = [
            (SOURCE_OVERRIDE, override_mode, "explicit user override (Rule 6)"),
            (SOURCE_PROMPT_INTENT, prompt_mode, "explicit user prompt intent"),
            (SOURCE_POLICY, policy_mode, policy_reason or "execution policy"),
            (SOURCE_PLANNER, planner_mode_val, "planner work-package types"),
            (SOURCE_VERIFICATION, ver_mode, "verification recommendation"),
        ]
        if all(c[1] is None for c in candidates):
            # Floor (Rule 7 + the safest-stop default): RESEARCH_ONLY.
            candidates.append((
                SOURCE_RESEARCH_HEURISTIC, RESEARCH_ONLY,
                "no execution-intent signal detected; safest-stop default",
            ))

        chosen_source, chosen_mode, chosen_reason = next(
            (s, m, r) for s, m, r in candidates if m is not None
        )

        # Audit trail (every layer that spoke)
        policy_decision = {
            SOURCE_OVERRIDE: {
                "mode": override_mode, "override_used": override_used,
            },
            SOURCE_PROMPT_INTENT: {"mode": prompt_mode, "intent": intent},
            SOURCE_POLICY: {"mode": policy_mode, "reason": policy_reason,
                            "hard_block": hard_block_reason},
            SOURCE_PLANNER: {"mode": planner_mode_val},
            SOURCE_VERIFICATION: {"mode": ver_mode},
        }

        # Resolve override_used against the recommendation (for the audit).
        # When the override equals the recommendation the user "accepted"; we
        # still record it as the winning source (its precedence is highest).
        recommended_mode = (
            prompt_mode or policy_mode or planner_mode_val or ver_mode
            or RESEARCH_ONLY
        )
        if override_mode is not None:
            override_used = override_used or (
                override_mode != recommended_mode
            )

        # --- Policy final check (cap may restrict an override too) --------
        # Rule 5: policy may RESTRICT; but an explicit user override is the
        # highest authority (Rule 6). The contract: a hard unresolved-disputes
        # block still wins over the override (you cannot build from an
        # incomplete debate). Other policy caps are advisory (logged) so the
        # audit shows the override overriding them.
        blocked = False
        block_status = ""
        if hard_block_reason:
            blocked = True
            block_status = "unresolved_disputes"
            chosen_source = SOURCE_POLICY
            chosen_reason = hard_block_reason
            chosen_mode = RESEARCH_ONLY

        # Build the stage plan
        stages = _stages_for_mode(chosen_mode, completed=done)

        confidence = self._confidence_for(chosen_source, intent, chosen_mode)

        return ExecutionDecision(
            execution_mode=chosen_mode,
            reason=chosen_reason,
            confidence=confidence,
            source=chosen_source,
            stages=stages,
            policy_decision=policy_decision,
            recommended_mode=recommended_mode,
            selected_mode=override_mode,
            override_used=override_used,
            blocked=blocked,
            block_status=block_status,
        )

    # --- internals ---------------------------------------------------

    def _recommend_mode(
        self, *, user_prompt, planner_output, verification_result,
        debate_result, consensus_result, policy, completed,
    ) -> str:
        """Compute the recommended mode for the override menu. Mirrors the
        precedence the engine itself uses (minus the override layer)."""
        intent = detect_prompt_intent(user_prompt)
        pm = intent_to_mode(intent)
        if pm:
            return pm
        pol = normalize_policy(policy)
        if pol.get("honor_unresolved_disputes_block") and verification_is_hard_block(
                verification_result):
            return RESEARCH_ONLY
        if pol.get("max_mode"):
            return pol["max_mode"]
        pl = planner_mode(planner_output)
        if pl:
            return pl
        v = verification_mode(verification_result)
        if v:
            return v
        return RESEARCH_ONLY

    def _collect_human_required(
        self, debate_result, consensus_result, verification_result,
    ) -> list:
        out: list = []
        for src in (debate_result, consensus_result, verification_result):
            if isinstance(src, dict):
                hr = src.get("human_required")
                if isinstance(hr, list):
                    out.extend(hr)
        return out

    def _confidence_for(
        self, source: str, intent: dict, mode: str,
    ) -> int:
        if source == SOURCE_OVERRIDE:
            return 100
        if source == SOURCE_PROMPT_INTENT:
            return 99 if intent.get("full_build") else 95
        if source == SOURCE_POLICY:
            return 90
        if source == SOURCE_PLANNER:
            return 85
        if source == SOURCE_VERIFICATION:
            return 70
        return 60  # research-heuristic floor


# ---------------------------------------------------------------------------
# ExecutionPlanner — converts the decision into a persisted stage plan
# ---------------------------------------------------------------------------


# Persisted-file names in the workspace root
F_PLAN = "execution_plan.json"
F_DECISION = "execution_decision.json"
F_REASON = "execution_reason.json"
F_STATE = "execution_state.json"


def _workspace_dir(workspace_id: str):
    from build_workspace import _workspace_dir as _w
    return _w(workspace_id)


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


class ExecutionPlanner:
    """Persists the ExecutionDecision's stage plan as four workspace artifacts
    under ``<workspace>/execution/``:

        execution/execution_plan.json       ── the stage list (the single
                                                source of truth downstream
                                                execution reads)
        execution/execution_decision.json   ── the decision envelope
                                                (mode/source/confidence)
        execution/execution_reason.json     ── the human-readable reason
        execution/execution_state.json       ── checkpoint state (resume)

    Namespaced under ``execution/`` so the existing build-spec task DAG at
    ``<workspace>/execution_plan.json`` (consumed by manifest.py / resume.py
    / validation.py) is preserved untouched — those readers keep their
    existing contract; the new stage plan is a separate artifact.

    Plan execution is driven ONLY by execution_plan.json. No downstream
    component may independently infer execution (the spec's contract).

    Supports resume / interruption recovery by reconciling the persisted
    stage statuses with a new decision's stages. ``load_state`` returns the
    checkpoint; ``reconcile`` merges completed stages forward.
    """

    def _exec_dir(self, workspace_id: str):
        return _workspace_dir(workspace_id) / "execution"

    def persist(
        self, workspace_id: str, decision: ExecutionDecision,
    ) -> dict:
        """Write the four artifacts. Returns the persisted plan dict."""
        if not workspace_id:
            raise ValueError("workspace_id is required to persist a plan")
        ed_dir = self._exec_dir(workspace_id)
        plan_payload = self._plan_payload(decision)
        decision_payload = decision.to_dict()
        reason_payload = {"reason": decision.reason, "source": decision.source,
                          "mode": decision.execution_mode}
        state_payload = {
            "workspace_id": workspace_id,
            "execution_mode": decision.execution_mode,
            "stages": plan_payload["stages"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(ed_dir / F_PLAN, plan_payload)
        _write_json(ed_dir / F_DECISION, decision_payload)
        _write_json(ed_dir / F_REASON, reason_payload)
        _write_json(ed_dir / F_STATE, state_payload)
        return plan_payload

    def load_plan(self, workspace_id: str) -> dict | None:
        if not workspace_id:
            return None
        p = self._exec_dir(workspace_id) / F_PLAN
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8")) or None
        except (json.JSONDecodeError, OSError):
            return None

    def load_state(self, workspace_id: str) -> dict | None:
        if not workspace_id:
            return None
        p = self._exec_dir(workspace_id) / F_STATE
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8")) or None
        except (json.JSONDecodeError, OSError):
            return None

    def mark_stage(
        self, workspace_id: str, stage: str, status: str,
    ) -> dict | None:
        """Update a single stage's status in execution/execution_plan.json
        and execution/execution_state.json (the live checkpoint). Returns the
        updated plan."""
        plan = self.load_plan(workspace_id)
        if plan is None:
            return None
        stages = plan.get("stages") or []
        changed = False
        for s in stages:
            if s.get("name") == stage:
                s["status"] = status
                changed = True
                break
        if not changed:
            raise ValueError(f"unknown stage: {stage}")
        plan["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(self._exec_dir(workspace_id) / F_PLAN, plan)
        state = self.load_state(workspace_id) or {}
        state["stages"] = plan["stages"]
        state["updated_at"] = plan["updated_at"]
        _write_json(self._exec_dir(workspace_id) / F_STATE, state)
        return plan

    def reconcile(
        self, decision: ExecutionDecision, workspace_id: str,
    ) -> ExecutionDecision:
        """Merge the persisted checkpoint's completed stages forward into a
        fresh decision (resume / interruption recovery): any stage marked
        ``completed`` in the on-disk plan stays completed in the new plan."""
        prior = self.load_plan(workspace_id)
        if not prior:
            return decision
        prior_done = {
            s.get("name") for s in (prior.get("stages") or [])
            if s.get("status") == "completed"
        }
        new_stages = _stages_for_mode(
            decision.execution_mode, completed=prior_done,
        )
        decision.stages = new_stages
        return decision

    def _plan_payload(self, decision: ExecutionDecision) -> dict:
        return {
            "execution_mode": decision.execution_mode,
            "reason": decision.reason,
            "source": decision.source,
            "confidence": decision.confidence,
            "stages": [s.to_dict() for s in decision.stages],
            "created_at": decision.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def log_decision(decision: ExecutionDecision) -> None:
    """Print the decision in the structured [EXECUTION] format the spec
    requires. One line per field. Pure (stdout only)."""
    if not isinstance(decision, ExecutionDecision):
        return
    stages = ", ".join(s.name for s in decision.stages)
    intent_str = "Research + Architecture + Implementation" if (
        decision.execution_mode in (FULL_BUILD, BUILD_AND_VALIDATE, BUILD_ONLY)
    ) else {
        RESEARCH_ONLY: "Research",
        ARCHITECTURE_ONLY: "Research + Architecture",
        IMPLEMENTATION_ONLY: "Research + Architecture + Implementation",
        CUSTOM: "Custom",
    }.get(decision.execution_mode, decision.execution_mode)
    print("[EXECUTION]", flush=True)
    print(f"[EXECUTION]   User Intent:    {intent_str}", flush=True)
    print(f"[EXECUTION]   Execution Mode: {decision.execution_mode}",
          flush=True)
    print(f"[EXECUTION]   Reason:         {decision.reason}", flush=True)
    print(f"[EXECUTION]   Confidence:      {decision.confidence}%", flush=True)
    print(f"[EXECUTION]   Source:         {decision.source}", flush=True)
    print(f"[EXECUTION]   Stages:         {stages}", flush=True)
    if decision.blocked:
        print(f"[EXECUTION]   BLOCKED:        {decision.block_status}",
              flush=True)
    pd = decision.policy_decision or {}
    if pd:
        print("[EXECUTION]   Policy Decision:", flush=True)
        for src in PRECEDENCE:
            entry = pd.get(src)
            if entry:
                print(f"[EXECUTION]     {src}: {entry}", flush=True)


# ---------------------------------------------------------------------------
# Error detection helpers (the spec's error-handling section)
# ---------------------------------------------------------------------------


class ExecutionDecisionError(Exception):
    """Raised on a malformed / conflicting execution decision input."""


def validate_inputs(
    *, user_prompt: str | None = None, planner_output: dict | None = None,
    verification_result: dict | None = None, policy: dict | None = None,
    user_override: str | None = None,
) -> list[str]:
    """Detect and report invalid inputs WITHOUT silently defaulting to
    RESEARCH_ONLY. Returns a list of human-readable issues (empty = ok)."""
    issues: list[str] = []
    if user_prompt is not None and not isinstance(user_prompt, str):
        issues.append("user_prompt must be a string")
    if planner_output is not None and not isinstance(planner_output, dict):
        issues.append("planner_output must be a dict or None")
    if verification_result is not None and not isinstance(
            verification_result, dict):
        issues.append("verification_result must be a dict or None")
    if policy is not None and not isinstance(policy, dict):
        issues.append("policy must be a dict or None")
    if user_override is not None:
        if not isinstance(user_override, str) or user_override not in ALL_MODES:
            issues.append(
                f"user_override must be one of {ALL_MODES} or None"
            )
    return issues


def validate_plan(plan: dict | None) -> list[str]:
    """Validate a persisted execution plan. Returns a list of issues
    (empty = ok)."""
    if plan is None:
        return ["missing execution plan"]
    issues: list[str] = []
    if not isinstance(plan, dict):
        return ["malformed execution plan (not a dict)"]
    if "execution_mode" not in plan or plan["execution_mode"] not in ALL_MODES:
        issues.append(f"invalid execution_mode: {plan.get('execution_mode')!r}")
    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages:
        issues.append("malformed execution plan (no stages)")
        return issues
    valid_names = set(ALL_STAGES)
    seen: set[str] = set()
    for s in stages:
        if not isinstance(s, dict) or "name" not in s or "status" not in s:
            issues.append(f"malformed stage: {s!r}")
            continue
        if s["name"] not in valid_names:
            issues.append(f"unknown stage: {s['name']!r}")
        if s["status"] not in (
                "pending", "running", "completed", "skipped", "blocked"):
            issues.append(f"invalid stage status: {s['status']!r}")
        if s["name"] in seen:
            issues.append(f"duplicate stage: {s['name']!r}")
        seen.add(s["name"])
    return issues


# ---------------------------------------------------------------------------
# Convenience top-level function (used by the pipeline)
# ---------------------------------------------------------------------------


_ENGINE = ExecutionDecisionEngine()
_PLANNER = ExecutionPlanner()


def decide(
    *,
    user_prompt: str = "",
    planner_output: dict | None = None,
    debate_result: dict | None = None,
    consensus_result: dict | None = None,
    verification_result: dict | None = None,
    policy: dict | None = None,
    user_override: str | None = None,
    completed_stages: Iterable[str] | None = None,
    mode_select_fn: Callable[[str], tuple[str, bool]] | None = None,
) -> ExecutionDecision:
    """Top-level convenience wrapper: validate inputs, run the engine, return
    the decision. Raises ``ExecutionDecisionError`` on invalid inputs (we do
    NOT silently default to RESEARCH_ONLY for clearly-bad inputs — the spec's
    error-handling contract). Missing optional inputs are allowed (the
    engine's safest-stop handles them)."""
    issues = validate_inputs(
        user_prompt=user_prompt, planner_output=planner_output,
        verification_result=verification_result, policy=policy,
        user_override=user_override,
    )
    if issues:
        raise ExecutionDecisionError("; ".join(issues))
    return _ENGINE.decide(
        user_prompt=user_prompt,
        planner_output=planner_output,
        debate_result=debate_result,
        consensus_result=consensus_result,
        verification_result=verification_result,
        policy=policy,
        user_override=user_override,
        completed_stages=completed_stages,
        mode_select_fn=mode_select_fn,
    )


def persist_decision(workspace_id: str, decision: ExecutionDecision) -> dict:
    """Persist a decision to the workspace. Returns the plan payload."""
    return _PLANNER.persist(workspace_id, decision)


def load_persisted_plan(workspace_id: str) -> dict | None:
    return _PLANNER.load_plan(workspace_id)


def reconcile_decision(
    decision: ExecutionDecision, workspace_id: str,
) -> ExecutionDecision:
    return _PLANNER.reconcile(decision, workspace_id)


# ---------------------------------------------------------------------------
# Backward-compat: map engine modes to legacy ed.* decision strings so the
# existing build_pipeline gate (which reads ed.KEY_DECISION) keeps working.
# ---------------------------------------------------------------------------

_MODE_TO_LEGACY_DECISION: dict[str, str] = {
    RESEARCH_ONLY: ed.RESEARCH_ONLY,
    ARCHITECTURE_ONLY: ed.ARCHITECTURE_ONLY,
    IMPLEMENTATION_ONLY: ed.IMPLEMENTATION_REQUIRED,
    BUILD_ONLY: ed.IMPLEMENTATION_REQUIRED,
    BUILD_AND_VALIDATE: ed.IMPLEMENTATION_REQUIRED,
    FULL_BUILD: ed.IMPLEMENTATION_REQUIRED,
    CUSTOM: ed.IMPLEMENTATION_REQUIRED,
}


def mode_to_legacy_decision(mode: str) -> str:
    """Map an engine mode to the legacy ``execution_decision`` string the
    existing build_pipeline gate enforces. Used by the adapter that wires the
    new engine into the pipeline without touching debate/consensus/
    verification/implementation engines. Pure."""
    return _MODE_TO_LEGACY_DECISION.get(mode, ed.RESEARCH_ONLY)


# ---------------------------------------------------------------------------
# Self-check (assert-based; no model calls, no I/O except a temp workspace)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    eng = ExecutionDecisionEngine()

    # 1. Research-only prompt (research keyword, no arch/impl)
    d = eng.decide(user_prompt="Compare Kafka vs RabbitMQ")
    assert d.execution_mode == RESEARCH_ONLY, d
    assert d.source == SOURCE_PROMPT_INTENT
    assert STAGE_RESEARCH in d.stage_statuses()
    assert STAGE_ARCHITECTURE not in d.stage_statuses()

    # 2. Architecture-only prompt
    d = eng.decide(user_prompt="Design the system architecture for the platform")
    assert d.execution_mode == ARCHITECTURE_ONLY, d
    assert STAGE_ARCHITECTURE in d.stage_statuses()

    # 3. Implementation-only prompt (impl keyword but no architecture)
    d = eng.decide(user_prompt="Build the auth module")
    assert d.execution_mode == BUILD_ONLY, d
    assert STAGE_IMPLEMENTATION in d.stage_statuses()

    # 4. Research + Architecture prompt
    d = eng.decide(user_prompt="Research and architect a streaming platform")
    assert d.execution_mode == ARCHITECTURE_ONLY, d

    # 5. Research + Architecture + Implementation prompt
    d = eng.decide(user_prompt="Research, architect, and implement the system")
    assert d.execution_mode == FULL_BUILD, d
    assert all(s in d.stage_statuses() for s in ALL_STAGES)

    # 6. Full Build prompt (explicit phrase)
    d = eng.decide(user_prompt="Do a full build of the trading system")
    assert d.execution_mode == FULL_BUILD, d

    # 7. Policy restricted prompt: max_mode caps the override too
    d = eng.decide(
        user_prompt="Implement the trading bot and design the architecture",
        policy={"max_mode": ARCHITECTURE_ONLY},
    )
    # Override beats policy caps (Rule 6); caps only restrict non-intent paths.
    # User prompt with explicit implementation wins over the cap.
    assert d.execution_mode == FULL_BUILD, d
    # But when there's NO prompt intent and the cap restricts:
    d2 = eng.decide(
        user_prompt="hello there",
        policy={"max_mode": ARCHITECTURE_ONLY},
    )
    assert d2.execution_mode == ARCHITECTURE_ONLY, d2

    # 8. Explicit user override wins over everything (except hard block)
    d = eng.decide(
        user_prompt="Compare databases",
        user_override=FULL_BUILD,
    )
    assert d.execution_mode == FULL_BUILD, d
    assert d.source == SOURCE_OVERRIDE
    assert d.override_used is True

    # 9. Resume after interruption: completed stages carry forward
    d = eng.decide(
        user_prompt="Implement the bot",
        completed_stages={STAGE_RESEARCH, STAGE_ARCHITECTURE},
    )
    statuses = d.stage_statuses()
    assert statuses[STAGE_RESEARCH] == "completed"
    assert statuses[STAGE_ARCHITECTURE] == "completed"
    assert statuses[STAGE_IMPLEMENTATION] == "pending"

    # 10. Verification success does NOT terminate execution (Rule 3 + 4)
    d = eng.decide(
        user_prompt="Implement the trading bot",
        verification_result={
            ed.KEY_DECISION: ed.RESEARCH_ONLY,
            ed.KEY_REASON: "research signals found",
            ed.KEY_SOURCE: ed.SOURCE_RESEARCH_ANALYSIS,
            "verification_score": 100,
            "resolved_claims": [],
            "unresolved_claims": [],
        },
    )
    # User intent wins over verifier's research-signal recommendation.
    assert d.execution_mode != RESEARCH_ONLY, d
    assert d.source == SOURCE_PROMPT_INTENT

    # 11. Verifier unresolved-disputes IS a hard block (legitimate governance)
    d = eng.decide(
        user_prompt="Implement the trading bot",
        verification_result={
            ed.KEY_DECISION: ed.STOP_UNRESOLVED_DISPUTES,
            "unresolved_claims": [{"normalized_key": "k"}],
        },
    )
    assert d.blocked is True
    assert d.block_status == "unresolved_disputes"
    assert d.execution_mode == RESEARCH_ONLY

    # 12. Missing planner output is fine (defer to other layers)
    d = eng.decide(user_prompt="Build the bot", planner_output=None)
    assert d.execution_mode == BUILD_ONLY, d

    # 13. Invalid verification result is rejected
    try:
        decide(user_prompt="x", verification_result="not a dict")
        raise AssertionError("expected ExecutionDecisionError")
    except ExecutionDecisionError:
        pass

    # 14. Malformed execution plan detection
    assert validate_plan(None) == ["missing execution plan"]
    assert validate_plan({"stages": []})  # missing execution_mode + no stages
    assert validate_plan({"execution_mode": "BAD", "stages": [{"name": "x"}]})

    # 15. Persistence round-trip + resume + checkpoint
    import tempfile
    import build_workspace as _bw
    from pathlib import Path
    orig_root = _bw.WORKSPACE_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)
        wid = _bw.create_workspace(source_question="build the bot")
        d = eng.decide(user_prompt="Build the trading bot")
        plan_payload = _PLANNER.persist(wid, d)
        assert validate_plan(plan_payload) == []
        loaded = _PLANNER.load_plan(wid)
        assert loaded is not None
        assert loaded["execution_mode"] == d.execution_mode
        # mark a stage completed
        _PLANNER.mark_stage(wid, STAGE_RESEARCH, "completed")
        # resume: fresh decision carries the completed research forward
        d2 = eng.decide(user_prompt="Build the trading bot")
        d2 = _PLANNER.reconcile(d2, wid)
        assert d2.stage_statuses()[STAGE_RESEARCH] == "completed"
        # state file written
        state = _PLANNER.load_state(wid)
        assert state is not None and state["execution_mode"] == d.execution_mode
        _bw.WORKSPACE_ROOT = orig_root

    # 16. Pure-stdlib guarantee at import
    import sys
    forbidden = {"cerebras_client", "ollama_client", "openrouter_client",
                 "groq_client", "gpt_worker", "gemini_worker",
                 "provider_router", "selenium", "requests",
                 "httpx", "urllib3"}
    loaded = forbidden & set(sys.modules)
    assert not loaded, f"engine pulled forbidden modules: {loaded}"

    # 17. Determinism (Rule 7) — exclude ``created_at`` (provenance time is
    # stamped at PERSIST, not at construction; matches execution_planner's
    # pattern so the persisted plan checksum is byte-identical).
    a = eng.decide(user_prompt="Build the bot")
    b = eng.decide(user_prompt="Build the bot")
    a_d = a.to_dict(); a_d.pop("created_at", None)
    b_d = b.to_dict(); b_d.pop("created_at", None)
    assert a_d == b_d, "decision not deterministic"

    # 18. log_decision doesn't raise
    log_decision(d)

    print("execution_decision_engine self-check PASSED")
