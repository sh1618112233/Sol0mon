"""
Project Sol0mon — Debate-Governed Execution Decisions V2.

Deterministic classifier that decides, after a debate completes, whether the
result should:

    STOP_HUMAN_REQUIRED        → STOP (humans must resolve a pending decision;
                                  the actual build block is deferred to
                                  execute_build_spec's pending-decision gate so
                                  spec generation can still run and the
                                  resolve-then-proceed flow survives)
    STOP_UNRESOLVED_DISPUTES    → STOP (verifier left claims unresolved and
                                  governance did not escalate them to a human;
                                  the debate outcome is incomplete — block)
    RESEARCH_ONLY              → STOP after research
    ARCHITECTURE_ONLY           → STOP after architecture
    IMPLEMENTATION_REQUIRED     → CONTINUE to the build pipeline (build spec,
                                  OpenCode agent, tests, repair loop)

Only IMPLEMENTATION_REQUIRED (and STOP_HUMAN_REQUIRED, which defers to the
pending gate) may reach the build pipeline's spec step; every other decision
is a hard stop at the execution-decision gate.

This module is pure stdlib only. No model calls, no ``generate()``, no browser,
no API. ``classify_execution_decision`` is deterministic and reproducible: the
same inputs always produce the same output, so the decision is safe to cache
and persist immutably once stored.

V2 DECISION HIERARCHY (never reverse — the first rung that holds wins):

    1. HUMAN_REQUIRED          → STOP_HUMAN_REQUIRED
       Trigger: ``human_required`` list is non-empty (governance escalated).
       Source:  ``human_required``.

    2. UNRESOLVED DISPUTES     → STOP_UNRESOLVED_DISPUTES
       Trigger: ``verification_result.unresolved_claims`` is non-empty.
       Source:  ``unresolved_disputes``.

    3. IMPLEMENTATION ARTIFACTS → IMPLEMENTATION_REQUIRED
       Trigger: implementation signals (deploy / build / implement / execute /
                create service|api|worker|database / migration step) appear in
                verified decisions, verified tradeoffs, or the synthesized
                answer. Verifier output OVERRIDES prompt wording — a
                "Design …" prompt with verified decisions that say "Deploy X"
                classifies IMPLEMENTATION_REQUIRED (the V1 keyword classifier
                failed this, V2 fixes it).
       Source:  ``implementation_artifacts``.

    4. ARCHITECTURE DECISIONS  → ARCHITECTURE_ONLY
       Trigger: architecture signals (orchestration / database selection /
                deployment topology / provider routing / storage design /
                security model) in verified decisions (no implementation
                signal on record).
       Source:  ``architecture_decisions``.

    5. RESEARCH ONLY           → RESEARCH_ONLY
       Trigger: research signals (comparison / evaluate / benchmark /
                analysis / survey) in the verification result (resolved-claim
                findings + research evidence), with no implementation or
                architecture artifacts.
       Source:  ``research_analysis``.

    6. Prompt fallback         → V1 keyword classification of the question
       Trigger: none of the decision-model rungs fired (no human_required, no
                unresolved claims, no verified signals, no research signals).
                Falls back to counting implementation / architecture / research
                keyword hits in the question text exactly as V1 did.
       Source:  ``prompt_fallback``.

Signal matching for rungs 3-5 uses WORD BOUNDARIES so bare "deploy" matches
"Deploy PostgreSQL" but NOT "deployment topology" (an architecture signal).
The prompt-fallback rung 6 keeps V1's substring matching so existing V1 prompt
classification is unchanged.

Inputs: question, verified_decisions, verified_tradeoffs, human_required,
synthesized_answer, verification_result (all optional).

Output: ``{"execution_decision": ..., "execution_reason": str,
"execution_confidence": int, "execution_decision_source": str}``.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------

RESEARCH_ONLY = "RESEARCH_ONLY"
ARCHITECTURE_ONLY = "ARCHITECTURE_ONLY"
IMPLEMENTATION_REQUIRED = "IMPLEMENTATION_REQUIRED"
STOP_HUMAN_REQUIRED = "STOP_HUMAN_REQUIRED"
STOP_UNRESOLVED_DISPUTES = "STOP_UNRESOLVED_DISPUTES"

ALL_DECISIONS = (
    RESEARCH_ONLY,
    ARCHITECTURE_ONLY,
    IMPLEMENTATION_REQUIRED,
    STOP_HUMAN_REQUIRED,
    STOP_UNRESOLVED_DISPUTES,
)

# Decisions that HARD-STOP the build pipeline at the execution-decision gate
# (run_build_pipeline Step 3.5 + execute_build_spec defense-in-depth gate).
# STOP_HUMAN_REQUIRED is deliberately excluded: it records that a human
# decision is pending but defers the actual block to execute_build_spec's
# pending-decision gate (the existing human-escalation contract). Blocking here
# would prevent spec generation and break the resolve-then-proceed flow.
# ponytail: re-evaluate STOP_HUMAN_REQUIRED immutability if a resume-after-
# human-resolution flow through the gate is needed; today the pending gate is
# the live authority for human holds.
BLOCKED_DECISIONS = frozenset({
    RESEARCH_ONLY,
    ARCHITECTURE_ONLY,
    STOP_UNRESOLVED_DISPUTES,
})

# Persisted-key names. These live inside ``verification_result`` (which flows
# through answer_memory's JSON-TEXT column) and on the top-level debate result
# dict. Namespaced with ``execution_`` so they never collide with verifier
# keys.
KEY_DECISION = "execution_decision"
KEY_REASON = "execution_reason"
KEY_CONFIDENCE = "execution_confidence"
KEY_SOURCE = "execution_decision_source"

# Classification-source labels (the ``execution_decision_source`` values).
SOURCE_HUMAN_REQUIRED = "human_required"
SOURCE_UNRESOLVED_DISPUTES = "unresolved_disputes"
SOURCE_IMPLEMENTATION_ARTIFACTS = "implementation_artifacts"
SOURCE_ARCHITECTURE_DECISIONS = "architecture_decisions"
SOURCE_RESEARCH_ANALYSIS = "research_analysis"
SOURCE_PROMPT_FALLBACK = "prompt_fallback"

# ---------------------------------------------------------------------------
# V2 decision-model signal sets (rungs 3-5). Word-boundary matched.
# ---------------------------------------------------------------------------

IMPLEMENTATION_SIGNALS = (
    "deploy", "build", "implement", "execute",
    "create service", "create api", "create worker", "create database",
    "migration step", "migrate",
    # V1-compat synonyms kept on the implementation rung so a verified
    # decision that says "develop" / "generate code" still upgrades.
    "develop", "create code", "generate code",
)

ARCHITECTURE_SIGNALS = (
    "orchestration", "database selection", "deployment topology",
    "provider routing", "storage design", "security model",
)

RESEARCH_SIGNALS = (
    "comparison", "compare", "evaluate", "evaluation",
    "benchmark", "analysis", "analyze", "survey", "research",
)

# ---------------------------------------------------------------------------
# V1 prompt-fallback keyword sets (rung 6). Substring matched (V1 behavior).
# ---------------------------------------------------------------------------

RESEARCH_KEYWORDS = (
    "compare", "evaluate", "research", "analyze", "survey", "benchmark",
)

ARCHITECTURE_KEYWORDS = (
    "design", "architecture", "blueprint", "system design", "platform design",
)

IMPLEMENTATION_KEYWORDS = (
    "implement", "build", "create code", "generate code", "develop",
    "execute",
)

# block_status string per decision (used when a decision hard-stops the gate).
_BLOCK_STATUS = {
    RESEARCH_ONLY: "research_complete",
    ARCHITECTURE_ONLY: "architecture_complete",
    STOP_UNRESOLVED_DISPUTES: "unresolved_disputes",
    STOP_HUMAN_REQUIRED: "human_required",
    IMPLEMENTATION_REQUIRED: "",
}

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _count_hits(text_lower: str, keywords) -> int:
    """V1 substring hit counter (prompt-fallback rung). Each keyword
    contributes +1 once if present as a substring. Pure."""
    if not text_lower:
        return 0
    return sum(1 for kw in keywords if kw in text_lower)


def _count_word_hits(text_lower: str, phrases) -> int:
    """V2 word-boundary hit counter (decision-model rungs 3-5). Each phrase
    contributes +1 once if it appears as a whole word/phrase (regex
    ``\\b<phrase>\\b``). This is what lets ``deploy`` match "Deploy PostgreSQL"
    but NOT "deployment topology" (an architecture signal). Pure."""
    if not text_lower:
        return 0
    count = 0
    for phrase in phrases:
        p = phrase.strip().lower()
        if not p:
            continue
        if re.search(r"\b" + re.escape(p) + r"\b", text_lower):
            count += 1
    return count


def _intent_from_scores(impl: int, arch: int, res: int) -> str:
    """Pick the intent category from raw keyword hit counts (prompt fallback).
    Implementation wins ties. When all three are zero, default to the
    safest-stop (RESEARCH_ONLY) so ambiguous prompts never silently slip into
    implementation. Pure."""
    if impl > 0 and impl >= arch and impl >= res:
        return IMPLEMENTATION_REQUIRED
    if arch > 0 and arch >= res:
        return ARCHITECTURE_ONLY
    if res > 0:
        return RESEARCH_ONLY
    return RESEARCH_ONLY  # safest-stop default — never auto-implement.


def _decision_text(decision: Any) -> str:
    """Pull the most informative text field out of a verified-decision dict.
    The decision model carries the winner in ``decision`` or ``reasoning``;
    the original dispute lives in ``dispute_text``/``dispute``. We cast a wide
    net because callers pass either schema-A (GovernedOutcome) or schema-B
    (loaded from verification_result.resolved_claims)."""
    if not isinstance(decision, dict):
        return ""
    for k in ("decision", "reasoning", "dispute_text", "dispute",
              "finding", "winner"):
        v = decision.get(k)
        if v:
            return str(v)
    return ""


def _verification_text(verification_result: Any) -> str:
    """Flatten the verification_result's research-bearing fields into one
    text blob for the research-detection rung (5). Reads resolved-claim
    disputes + findings + per-claim research summaries, plus the top-level
    ``research_evidence`` blob. Unresolved claims are handled by rung 2, so
    they are intentionally not scanned here."""
    if not isinstance(verification_result, dict):
        return ""
    parts: list[str] = []
    for claim in verification_result.get("resolved_claims", []) or []:
        if not isinstance(claim, dict):
            continue
        parts.append(str(claim.get("dispute", "") or ""))
        v = claim.get("verification")
        if isinstance(v, dict):
            parts.append(str(v.get("finding", "") or ""))
        parts.append(str(claim.get("research_summary", "") or ""))
    re_ev = verification_result.get("research_evidence")
    if isinstance(re_ev, list):
        parts.extend(str(e) for e in re_ev)
    elif re_ev:
        parts.append(str(re_ev))
    return "\n".join(p for p in parts if p)


def _impl_model_text(
    verified_decisions: list | None,
    verified_tradeoffs: list | None,
    synthesized_answer: str | None,
) -> str:
    """Concatenate the decision-model fields that carry implementation intent
    (rung 3): verified decisions + verified tradeoffs + synthesized answer."""
    parts: list[str] = []
    for d in (verified_decisions or []):
        parts.append(_decision_text(d))
    for t in (verified_tradeoffs or []):
        parts.append(_decision_text(t))
    if synthesized_answer:
        parts.append(str(synthesized_answer))
    return "\n".join(p for p in parts if p)


def _arch_model_text(verified_decisions: list | None) -> str:
    """Concatenate the decision-model fields that carry architecture intent
    (rung 4): verified decisions only (tradeoffs/synthesis already cleared
    of implementation signals by rung 3)."""
    return "\n".join(
        _decision_text(d) for d in (verified_decisions or [])
        if _decision_text(d)
    )


# ---------------------------------------------------------------------------
# V2 classifier
# ---------------------------------------------------------------------------


def classify_execution_decision(
    question: str | None = None,
    verified_decisions: list | None = None,
    verified_tradeoffs: list | None = None,
    human_required: list | None = None,
    synthesized_answer: str | None = None,
    verification_result: dict | None = None,
) -> dict:
    """Deterministic Execution Decision classification (V2 hierarchy).

    Walks the 6-rung ladder in order; the first rung that holds wins and
    lower rungs are never reached (the hierarchy never reverses). See the
    module docstring for the full ladder.

    Returns ``{execution_decision, execution_reason, execution_confidence,
    execution_decision_source}``. Pure: no I/O, no model call.
    """
    # --- Rung 1: HUMAN_REQUIRED -------------------------------------------
    if human_required:
        return {
            KEY_DECISION: STOP_HUMAN_REQUIRED,
            KEY_REASON: (
                f"{len(human_required)} pending human-required decision(s) "
                "on record; build deferred to the pending-decision gate "
                "(execute_build_spec)."
            ),
            KEY_CONFIDENCE: 95,
            KEY_SOURCE: SOURCE_HUMAN_REQUIRED,
        }

    # --- Rung 2: UNRESOLVED DISPUTES --------------------------------------
    unresolved = (
        verification_result.get("unresolved_claims")
        if isinstance(verification_result, dict) else None
    ) or []
    if unresolved:
        return {
            KEY_DECISION: STOP_UNRESOLVED_DISPUTES,
            KEY_REASON: (
                f"{len(unresolved)} unresolved dispute(s) in the verifier "
                "output and no human escalation; the debate outcome is "
                "incomplete — build blocked."
            ),
            KEY_CONFIDENCE: 95,
            KEY_SOURCE: SOURCE_UNRESOLVED_DISPUTES,
        }

    # --- Rung 3: IMPLEMENTATION ARTIFACTS ---------------------------------
    impl_lower = _impl_model_text(
        verified_decisions, verified_tradeoffs, synthesized_answer,
    ).lower()
    impl_hits = _count_word_hits(impl_lower, IMPLEMENTATION_SIGNALS)
    if impl_hits > 0:
        return {
            KEY_DECISION: IMPLEMENTATION_REQUIRED,
            KEY_REASON: (
                f"Verified decision model contains {impl_hits} implementation "
                "signal(s) (deploy/build/implement/...); proceeding to build "
                "spec + coding agent + tests."
            ),
            KEY_CONFIDENCE: 90,
            KEY_SOURCE: SOURCE_IMPLEMENTATION_ARTIFACTS,
        }

    # --- Rung 4: ARCHITECTURE DECISIONS -----------------------------------
    arch_lower = _arch_model_text(verified_decisions).lower()
    arch_hits = _count_word_hits(arch_lower, ARCHITECTURE_SIGNALS)
    if arch_hits > 0:
        return {
            KEY_DECISION: ARCHITECTURE_ONLY,
            KEY_REASON: (
                f"Verified decision model contains {arch_hits} architecture "
                "signal(s) (orchestration/deployment topology/...); stopping "
                "after architecture — no implementation."
            ),
            KEY_CONFIDENCE: 85,
            KEY_SOURCE: SOURCE_ARCHITECTURE_DECISIONS,
        }

    # --- Rung 5: RESEARCH ONLY -------------------------------------------
    research_lower = _verification_text(verification_result).lower()
    research_hits = _count_word_hits(research_lower, RESEARCH_SIGNALS)
    if research_hits > 0:
        return {
            KEY_DECISION: RESEARCH_ONLY,
            KEY_REASON: (
                f"Verification result contains {research_hits} research "
                "signal(s) (comparison/evaluation/benchmark/...); stopping "
                "after research — no implementation."
            ),
            KEY_CONFIDENCE: 80,
            KEY_SOURCE: SOURCE_RESEARCH_ANALYSIS,
        }

    # --- Rung 6: Prompt fallback (V1 keyword classification) --------------
    q_lower = (question or "").lower()
    q_impl = _count_hits(q_lower, IMPLEMENTATION_KEYWORDS)
    q_arch = _count_hits(q_lower, ARCHITECTURE_KEYWORDS)
    q_res = _count_hits(q_lower, RESEARCH_KEYWORDS)
    decision = _intent_from_scores(q_impl, q_arch, q_res)

    if decision == IMPLEMENTATION_REQUIRED:
        reason = (
            f"Prompt fallback contains implementation signal "
            f"(impl={q_impl}, arch={q_arch}, research={q_res}). "
            "Proceeding to build spec + coding agent + tests."
        )
        confidence = 95
    elif decision == ARCHITECTURE_ONLY:
        reason = (
            f"Prompt fallback contains architecture signal "
            f"(arch={q_arch}, impl={q_impl}, research={q_res}). "
            "Stopping after architecture — no implementation."
        )
        confidence = 95
    else:  # RESEARCH_ONLY
        if q_res > 0 or q_arch > 0 or q_impl > 0:
            reason = (
                f"Prompt fallback contains research signal "
                f"(research={q_res}, arch={q_arch}, impl={q_impl}). "
                "Stopping after research — no implementation."
            )
            confidence = 95 if q_res > 0 else 70
        else:
            reason = (
                "No execution-intent keyword detected in the prompt or the "
                "decision model. Defaulting to RESEARCH_ONLY (safest-stop) — "
                "implementation requires an explicit implementation signal."
            )
            confidence = 60

    return {
        KEY_DECISION: decision,
        KEY_REASON: reason,
        KEY_CONFIDENCE: confidence,
        KEY_SOURCE: SOURCE_PROMPT_FALLBACK,
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def attach_to_verification_result(
    verification_result: dict | None, decision: dict
) -> dict:
    """Write the execution-decision keys into ``verification_result`` in place
    so they persist with it (answer_memory stores verification_result as a
    JSON-TEXT column; the keys survive the round-trip and workspace resume).
    Returns the (possibly newly created) verification_result dict. Pure: no
    I/O, no model call. """
    if verification_result is None or not isinstance(verification_result, dict):
        verification_result = {} if verification_result is None else {}
    verification_result[KEY_DECISION] = decision.get(KEY_DECISION)
    verification_result[KEY_REASON] = decision.get(KEY_REASON, "")
    verification_result[KEY_CONFIDENCE] = decision.get(KEY_CONFIDENCE, 0)
    verification_result[KEY_SOURCE] = decision.get(KEY_SOURCE, "")
    return verification_result


def from_verification_result(verification_result: dict | None) -> dict | None:
    """Read the persisted execution decision out of a verification_result
    dict. Returns ``None`` when the decision key is absent (no decision was
    ever stored). Used on resurrection/resume to honor an immutable prior
    decision rather than reclassify."""
    if not isinstance(verification_result, dict):
        return None
    d = verification_result.get(KEY_DECISION)
    if not d:
        return None
    return {
        KEY_DECISION: d,
        KEY_REASON: verification_result.get(KEY_REASON, ""),
        KEY_CONFIDENCE: verification_result.get(KEY_CONFIDENCE, 0),
        KEY_SOURCE: verification_result.get(KEY_SOURCE, ""),
    }


def is_implementation_required(decision: dict | str | None) -> bool:
    """Convenience: True iff the decision mandates implementation."""
    if decision is None:
        return False
    if isinstance(decision, str):
        return decision == IMPLEMENTATION_REQUIRED
    return decision.get(KEY_DECISION) == IMPLEMENTATION_REQUIRED


def is_blocked_decision(decision: dict | str | None) -> bool:
    """True iff the decision HARD-STOPS the build pipeline at the
    execution-decision gate (RESEARCH_ONLY / ARCHITECTURE_ONLY /
    STOP_UNRESOLVED_DISPUTES).

    ``None`` (no decision stored) is non-blocking — preserves behavior for
    any caller that loads a legacy answer without an execution_decision.

    STOP_HUMAN_REQUIRED is NOT a hard stop here: it records a pending human
    decision but defers the actual block to execute_build_spec's pending gate
    (the existing human-escalation contract), so spec generation still runs.
    """
    if decision is None:
        return False
    if isinstance(decision, str):
        return decision in BLOCKED_DECISIONS
    return decision.get(KEY_DECISION) in BLOCKED_DECISIONS


def block_status(decision: dict | str | None) -> str:
    """Map a decision to its build-pipeline block status string:
        RESEARCH_ONLY             → "research_complete"
        ARCHITECTURE_ONLY         → "architecture_complete"
        STOP_UNRESOLVED_DISPUTES  → "unresolved_disputes"
        STOP_HUMAN_REQUIRED       → "human_required"
        IMPLEMENTATION_REQUIRED   → ""
        None                      → ""
    Unknown decisions map to ``"blocked"``."""
    if decision is None:
        return ""
    d = decision if isinstance(decision, str) else decision.get(KEY_DECISION)
    return _BLOCK_STATUS.get(d, "blocked" if d else "")


if __name__ == "__main__":
    # Deterministic smoke self-check — no model call, no I/O.
    cases = [
        ("Research the best ML frameworks for 2025", RESEARCH_ONLY),
        ("Compare PostgreSQL vs MongoDB for a multi-tenant SaaS", RESEARCH_ONLY),
        ("Benchmark in-memory queues for low-latency trading", RESEARCH_ONLY),
        ("Design a planet-scale consensus platform architecture",
         ARCHITECTURE_ONLY),
        ("Blueprint the data layer for the analytics platform",
         ARCHITECTURE_ONLY),
        ("Build the user authentication module for the consensus platform",
         IMPLEMENTATION_REQUIRED),
        ("Implement the session token refresh flow", IMPLEMENTATION_REQUIRED),
        ("Develop the rate limiter service", IMPLEMENTATION_REQUIRED),
        # Ambiguous — safest-stop default.
        ("Tell me about the 2024 market", RESEARCH_ONLY),
    ]
    for prompt, expected in cases:
        out = classify_execution_decision(question=prompt)
        assert out[KEY_DECISION] == expected, (
            f"{prompt!r} expected {expected}, got {out[KEY_DECISION]}"
        )
        assert out[KEY_SOURCE] == SOURCE_PROMPT_FALLBACK, (
            f"prompt-only classify must source from prompt_fallback, "
            f"got {out[KEY_SOURCE]}"
        )

    # V2 rung 1: human_required → STOP_HUMAN_REQUIRED (overrides everything).
    hr = classify_execution_decision(
        question="Implement the auth module",
        human_required=[{"outcome_type": "HUMAN_REQUIRED", "dispute_key": "k"}],
    )
    assert hr[KEY_DECISION] == STOP_HUMAN_REQUIRED, hr
    assert hr[KEY_SOURCE] == SOURCE_HUMAN_REQUIRED

    # V2 rung 2: unresolved claims → STOP_UNRESOLVED_DISPUTES.
    unres = classify_execution_decision(
        question="Implement the auth module",
        verification_result={"unresolved_claims": [{"normalized_key": "k"}]},
    )
    assert unres[KEY_DECISION] == STOP_UNRESOLVED_DISPUTES, unres
    assert unres[KEY_SOURCE] == SOURCE_UNRESOLVED_DISPUTES

    # V2 rung 3: IMPLEMENTATION ARTIFACTS OVERRIDE PROMPT INTENT.
    # Critical case — a "Design …" prompt with verified decisions that say
    # "Deploy X" classifies IMPLEMENTATION_REQUIRED (V1 failed this).
    overridden = classify_execution_decision(
        question="Design a planet scale consensus platform",
        verified_decisions=[
            {"decision": "Deploy PostgreSQL"},
            {"decision": "Deploy Temporal"},
            {"decision": "Deploy Worker Pool"},
        ],
    )
    assert overridden[KEY_DECISION] == IMPLEMENTATION_REQUIRED, overridden
    assert overridden[KEY_SOURCE] == SOURCE_IMPLEMENTATION_ARTIFACTS

    # "deployment topology" is architecture, not implementation: word
    # boundary stops "deploy" from matching "deployment".
    arch = classify_execution_decision(
        question="Research the trade-offs",
        verified_decisions=[{"decision": "Deployment topology: use K8s"}],
    )
    assert arch[KEY_DECISION] == ARCHITECTURE_ONLY, arch
    assert arch[KEY_SOURCE] == SOURCE_ARCHITECTURE_DECISIONS

    # V2 rung 5: research signals in verification_result → RESEARCH_ONLY.
    res = classify_execution_decision(
        question="Tell me about the platform",
        verification_result={
            "resolved_claims": [
                {"dispute": "X vs Y", "verification": {
                    "finding": "Benchmark shows X faster"}},
            ],
        },
    )
    assert res[KEY_DECISION] == RESEARCH_ONLY, res
    assert res[KEY_SOURCE] == SOURCE_RESEARCH_ANALYSIS

    # Pure-stdlib guarantee: importing this module must NOT import any LLM
    # provider, browser, or network client.
    import sys
    forbidden = {"cerebras_client", "ollama_client", "openrouter_client",
                 "groq_client", "gpt_worker", "gemini_worker"}
    loaded = forbidden & set(sys.modules)
    assert not loaded, f"execution_decision pulled in forbidden modules: {loaded}"

    # Round-trip persistence through verification_result (now 4 keys).
    vr = {"resolved_claims": []}
    attach_to_verification_result(vr, overridden)
    again = from_verification_result(vr)
    assert again == {
        KEY_DECISION: IMPLEMENTATION_REQUIRED,
        KEY_REASON: overridden[KEY_REASON],
        KEY_CONFIDENCE: overridden[KEY_CONFIDENCE],
        KEY_SOURCE: overridden[KEY_SOURCE],
    }
    assert from_verification_result({"resolved_claims": []}) is None

    # Predicate coverage for the new V2 decision types.
    assert not is_blocked_decision(hr)  # STOP_HUMAN_REQUIRED defers to gate
    assert is_blocked_decision(unres)   # STOP_UNRESOLVED_DISPUTES hard-stops
    assert block_status(STOP_UNRESOLVED_DISPUTES) == "unresolved_disputes"
    assert block_status(STOP_HUMAN_REQUIRED) == "human_required"

    print("execution_decision V2 smoke test PASSED")
