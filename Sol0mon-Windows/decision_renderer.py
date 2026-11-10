"""
Project Sol0mon — Deterministic Synthesis Repair (decision_renderer).

Phase 1: decision_model is the SOLE OWNER of decision interpretation. This
module re-exports the interpretation primitives (winner_tokens, rejected_options,
winner_text, *_tokens, *_winner_phrase*, ...) from decision_model for
backwards-compatibility with existing external imports. The local
definitions have been deleted; do NOT add parsing back here. New consumers
of decision interpretation MUST import from decision_model directly.

This module keeps its rendering role - rendering markdown from the canonical
decision model into Audit Synthesis sections:

    1. Verified Decisions   <- from verification_result.resolved_claims (RESOLVED)
    2. Verified Tradeoffs   <- from governance_context.verified_tradeoffs (VERIFIED_TRADEOFF)
    3. Human Decisions Required <- from governance_context.human_required (HUMAN_REQUIRED)
    4. Architecture / Implementation Plan / Migration Path / Risks <- from the
       canonical decision model (decision_model.winner_text + decision_model.
       rejected_options), so the deterministic narrative can NEVER emit a
       rejected option as the architecture (the answer-156 contradiction).

`validate_synthesis_consistency()` is the FIX 6 + Phase 1 guard: it flags a
narrative that
    - mentions a rejected option with no rejection framing in its immediate
      vicinity (the strengthened rule - caught the 156-style bare-bullet case
      "## Persistence Layer\n- PostgreSQL primary datastore" that FIX 6 missed
      because the bullet has neither a recommend context nor a negate context),
    - negates an option the verifier declared the winner.

Pure string heuristics; no model calls. It is a warning, not a hard block.

Data shapes (see verification_engine.py / governance.py):

    verification_result = {
        "resolved_claims": [ {dispute, normalized_key,
            verification: {status:"RESOLVED", confidence, finding,
                           rejected_options: [...]} } ],
        "equivalent_claims": [...], "unresolved_claims": [...],
        "verification_score": int, "truncated_count": int,
    }

    governance_context = GovernanceResult.to_dict() = {
        "verified_decisions":   [GovernedOutcome.to_dict()],   # VERIFIED_DECISION
        "verified_tradeoffs":    [GovernedOutcome.to_dict()],   # VERIFIED_TRADEOFF
        "human_required":        [GovernedOutcome.to_dict()],   # HUMAN_REQUIRED
    }
"""

from __future__ import annotations

import re
from typing import Any

# Phase 1: interpretation primitives are OWNED by decision_model and re-exported
# here for backwards compatibility with existing external imports. Adding new
# parsing back in this module reintroduces the divergence class observed in
# answer 156; DO NOT.
from decision_model import (  # re-export - canonical home is decision_model
    _as_dict,
    _first_reject_pos,
    _is_clean_label,
    _clean_winner_name,
    _GENERIC_OPTION_WORDS,
    _LEAD_RES,
    _OPTION_STOP,
    _PROSE_MARKERS,
    _REJECT_RE,
    _REJECT_SIGNALS_RE,
    _resolved_claims,
    _resolved_keys,
    _STOPWORDS,
    _str,
    _tokens,
    _verifications,
    _WINNER_MARKERS,
    _winner_and_rejected_tokens,
    _winner_phrase,
    _winner_raw,
    decision_records,
    derive_rejected_options,
    rejected_options,
    winner_text,
    winner_tokens,
)


# ---------------------------------------------------------------------------
# Rendering helpers (presentation; stay here, not interpretation)
# ---------------------------------------------------------------------------


def _is_dispute_echo(winner: str, dispute: str) -> bool:
    """Renderer guard (Debate #164 class):

    True when ``winner`` is not actually a winner option but the dispute
    description quoted/echoed back. This happens for findings that open by
    restating the dispute ("The dispute description claims...") and where
    decision_model.winner_text's marker-based extraction could not isolate
    a clean winner phrase — it then returns the first sentence before the
    first reject signal, which IS the dispute restatement.

    A winner that *echoes* the dispute is structurally wrong as an
    architecture component name (or a build directive): it names BOTH
    options at best, and at worst it is pure meta-prose ("Dispute
    description claims GPT leaves deployment undefined"). Emitting it would
    both leak rejected options AND produce a slug like
    `dispute_description_claims_gpt_leaves_deployment_undefined` that the
    agent dutifully turns into a source package name (the Debate #164
    incident).

    Detection — pure lead-in heuristics, deliberately conservative. A
    single-token winner like "CoinGecko" winning against
    "Data Provider: CoinGecko vs CoinPaprika" must NOT be flagged (the
    legit case: a short option name appears verbatim in the dispute). The
    distinguishing feature of a true echo is the *lead-in*: real echoes are
    full sentences that BEGIN with dispute-narrative prose ("dispute
    description claims...", "the dispute...", "claims..."). Real winners
    are bare technical names and never begin that way.

    Pure renderer-side; does NOT change decision_model.winner_text (the
    user's invariant). The decision still surfaces in the
    `# Verified Decisions` and `# Constraints` sections via the
    canonical render_verified_decisions path, so the information is not
    lost — only the architecture / implementation-guide rendering is
    sanitized.
    """
    if not winner:
        return False
    w = winner.lower().strip()
    # Lead-in heuristics — the canonical "echoed dispute" shapes. A real
    # winner never begins with these phrases. Conservative on purpose:
    # false positives (dropping a legit short winner) are worse than false
    # negatives (letting a borderline winner through), because dropping a
    # real winner silently corrupts the architecture.
    return (
        w.startswith("dispute description")
        or w.startswith("the dispute description")
        or w.startswith("dispute description claims")
        or w.startswith("the dispute")
        or w.startswith("claims ")
        or w == "claims"
    )


def _confidence(x: Any) -> str:
    """Render a confidence value as "<n>%" defensively."""
    try:
        return f"{int(x)}%"
    except (TypeError, ValueError):
        return "?" if not x else f"{x}"


def render_verified_decisions(
    verification_result: dict | None,
    governance_context: dict | None = None,
) -> str:
    """Deterministic markdown for every dispute the verifier RESOLVED.

    Each resolved claim is rendered directly from verification_result:
        ### <dispute>
        Winner: <finding>
        Confidence: <n>%
        Rationale: <finding>
    No LLM. governance_context is not used here (RESOLVED claims live in the
    verifier, not governance — governance houses its own VERIFIED_DECISION
    bucket, kept here only for signature symmetry with the sibling renderers).
    """
    claims = _resolved_claims(verification_result)
    lines = ["# Verified Decisions", "", f"Decision Count: {len(claims)}", ""]
    if not claims:
        lines.append("None")
        lines.append("")
        return "\n".join(lines)

    for c in claims:
        v = _verifications(c)
        dispute = _str(c.get("dispute"), "(untitled dispute)")
        finding = _str(v.get("finding"), "(no finding recorded)")
        confidence = _confidence(v.get("confidence"))
        lines += [
            "---",
            "",
            f"### {dispute}",
            "",
            "Winner:",
            finding,
            "",
            "Confidence:",
            confidence,
            "",
            "Rationale:",
            finding,
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FIX 3 — Verified Tradeoffs (from governance_context.verified_tradeoffs ONLY)
# ---------------------------------------------------------------------------


def _tradeoffs(governance_context: dict | None) -> list[dict]:
    gc = _as_dict(governance_context)
    return [t for t in (gc.get("verified_tradeoffs") or []) if isinstance(t, dict)]


def render_verified_tradeoffs(
    verification_result: dict | None,
    governance_context: dict | None = None,
) -> str:
    """Deterministic markdown for disputes governance classified as
    VERIFIED_TRADEOFF. Resolved disputes NEVER appear here: a tradeoff whose
    dispute_key matches a resolved claim's normalized_key is dropped (the
    verifier's resolution takes precedence over any stale tradeoff bucket)."""
    resolved = _resolved_keys(verification_result)
    items = _tradeoffs(governance_context)

    kept = []
    for t in items:
        key = (t.get("dispute_key") or t.get("normalized_key") or "").strip().lower()
        if key and key in resolved:
            # Resolved disputes must never be downgraded to a tradeoff.
            continue
        kept.append(t)

    lines = ["# Verified Tradeoffs", "", f"Tradeoff Count: {len(kept)}", ""]
    if not kept:
        lines.append("None")
        lines.append("")
        return "\n".join(lines)

    for t in kept:
        lines += [
            "---",
            "",
            f"### {_str(t.get('dispute_text'), '(untitled tradeoff)')}",
            "",
            "Option A:",
            _str(t.get("option_a"), "N/A"),
            "",
            "Option B:",
            _str(t.get("option_b"), "N/A"),
            "",
            "Recommendation:",
            _str(t.get("recommendation"), "No recommendation"),
            "",
            "Confidence:",
            _confidence(t.get("confidence")),
            "",
            "Rationale:",
            _str(t.get("reasoning"), ""),
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FIX 4 — Human Decisions Required (governance_context.human_required ONLY)
# ---------------------------------------------------------------------------


def _human_required(governance_context: dict | None) -> list[dict]:
    gc = _as_dict(governance_context)
    items = gc.get("human_required") or gc.get("human_required_items") or []
    return [h for h in items if isinstance(h, dict)]


def render_human_required(
    verification_result: dict | None,
    governance_context: dict | None = None,
) -> str:
    """Deterministic markdown for governance outcomes marked HUMAN_REQUIRED.
    Only such outcomes appear here. Each item surfaces Decision ID, Dispute,
    Reason, Options, Recommendation — so a human can act on it."""
    items = _human_required(governance_context)
    lines = ["# Human Decisions Required", "", f"Decision Count: {len(items)}", ""]
    if not items:
        lines.append("None")
        lines.append("")
        return "\n".join(lines)

    for h in items:
        options = h.get("options")
        opt_a = _str(h.get("option_a") or (options[0] if options else None), "N/A")
        opt_b = _str(h.get("option_b") or (options[1] if options else None), "N/A")
        lines += [
            "---",
            "",
            f"Decision ID: {_str(h.get('decision_id'), 'Not recorded')}",
            "",
            f"Dispute: {_str(h.get('dispute_text') or h.get('question'), '(no dispute text)')}",
            "",
            f"Reason: {_str(h.get('reasoning'), '(no reason recorded)')}",
            "",
            "Options:",
            f"- {opt_a}",
            f"- {opt_b}",
            "",
            f"Recommendation: {_str(h.get('recommendation'), 'Review the queued decision and choose the option to unblock implementation.')}",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FIX 6 — validate_synthesis_consistency (deterministic, no LLM)
# ---------------------------------------------------------------------------

# Verbs that, when adjacent to a rejected option, mean the narrative is
# RECOMMENDING the thing the verifier threw out.
_RECOMMEND_CONTEXTS = (
    "use", "using", "adopt", "adopting", "choose", "choosing", "select",
    "selecting", "recommend", "recommends", "recommended", "deploy",
    "deploying", "implement", "implementing", "build", "build with",
    "building", "power", "powered by", "powered with", "built on", "built with",
    "architectural choice", "as the primary", "as the authoritative",
    "should use", "must use", "will use", "we use", "we adopt", "we choose",
    "we select", "primary choice", "authoritative choice", "preferred",
)

# Verbs that, when adjacent to a WINNER, mean the narrative is REJECTING the
# thing the verifier chose.
_NEGATE_CONTEXTS = (
    "do not use", "don't use", "avoid", "avoiding", "reject", "rejects",
    "rejecting", "rejected", "should not", "should not use", "won't use",
    "will not use", "instead of", "rather than", "not use", "cannot use",
    "off-limits", "dismiss", "abandon", "drop", "abandoned", "no longer",
)


_SENTENCE_BOUNDARY = re.compile(r"[\n.!?;]")  # ponytail: simple sentence split, no NLP lib

def _all_occurrences(text: str, token: str, window: int = 60):
    """Yield (before, after) context windows for every WHOLE-WORD occurrence of
    `token` in `text`, each bounded to the SAME sentence/line.

    Two correctness bounds:
      - Whole-word: `low.find(token)` would match 'container' inside
        'webcontainers' (a rejected option) and produce false positives, so
        occurrences are matched with token boundaries.
      - Sentence-bounded windows: unbounded char windows bleed across sentence
        boundaries (a rejected option at end of one line followed by 'Deploy
        ...' on the next) and produce false positives, so each window is
        clipped at the nearest sentence/line break.
    """
    if not text or not token:
        return
    low = (text or "").lower()
    pat = re.compile(rf"(?<![a-z0-9.+-]){re.escape(token)}(?![a-z0-9+-])")
    start = 0
    while True:
        m = pat.search(low, start)
        if not m:
            return
        i = m.start()
        # Left boundary: nearest sentence/line break before the token.
        left = i
        for b in range(i - 1, max(-1, i - window) - 1, -1):
            if _SENTENCE_BOUNDARY.match(low[b]):
                left = b + 1
                break
            left = b
        before = low[left:i]
        # Right boundary: nearest sentence/line break after the token.
        end = m.end()
        right = end
        for b in range(end, min(len(low), end + window)):
            if _SENTENCE_BOUNDARY.match(low[b]):
                right = b
                break
            right = b + 1
        after = low[i:right]
        yield before, after
        start = end



def _has_any(ctx: str, contexts: tuple[str, ...]) -> bool:
    return any(c and c in ctx for c in contexts)


# ponytail: the substring context match (`c in ctx`) is intentionally loose
# in the existing tests (Use / Adopt / Recommend a rejected option all match).
# Its one false-positive surface: a context verb that lives INSIDE a larger
# word — 'deploy' inside 'deployment strategy', 'build' inside 'building',
# 'use' inside 'reuse'. Deterministic narrative renderers that print dispute
# titles like 'Deployment Strategy: Single VM ...' would otherwise be flagged
# because 'deployment' contains 'deploy'. Whole-word matching preserves every
# match the existing tests depend on ('Use X', 'Adopt X', 'Recommend X',
# 'do not use X', 'reject X', 'off-limits', ...) and removes only the
# matched-as-substring-of-a-different-word false positives. Compiled once at
# import.
def _compile_context_res(words: tuple[str, ...]) -> tuple["re.Pattern[str]", ...]:
    return tuple(
        re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", re.IGNORECASE)
        for w in words if w
    )


_RECOMMEND_CONTEXT_RES = _compile_context_res(_RECOMMEND_CONTEXTS)
_NEGATE_CONTEXT_RES = _compile_context_res(_NEGATE_CONTEXTS)


def _has_any_word(ctx: str, patterns: tuple["re.Pattern[str]", ...]) -> bool:
    return any(p.search(ctx) for p in patterns)


_DETERMINISTIC_HEADERS = (
    "Verified Decisions",
    "Verified Tradeoffs",
    "Human Decisions Required",
)
_TOPLEVEL_HEADING = re.compile(r"(?m)^#\s+(.+?)\s*$")


def _narrative_of(answer: str) -> str:
    """Isolate the LLM narrative from a synthesized answer.

    After FIX 5 the synthesized answer is:

        # Verified Decisions ...
        # Verified Tradeoffs ...
        # Human Decisions Required ...
        <narrative>

    The deterministic sections are consistent by construction and must NOT be
    scanned (a finding's own 'reject <rejected>' clause and a neighbour
    section's dispute title would otherwise cross-bleed into false
    positives). We strip every leading deterministic section — however many
    are present — and return the remaining narrative. When the caller passed
    the narrative only (no deterministic header), the whole answer is the
    narrative. When the answer is only deterministic sections, the narrative
    is empty.
    """
    if not answer:
        return ""
    last = -1
    for h in _DETERMINISTIC_HEADERS:
        i = answer.find("# " + h)
        if i != -1 and i > last:
            last = i
    if last == -1:
        # No deterministic prefix: the whole answer is the narrative.
        return answer
    after = answer[last:]
    nl = after.find("\n")
    body = after[nl + 1:] if nl != -1 else ""
    for m in _TOPLEVEL_HEADING.finditer(body):
        title = m.group(1).strip()
        if title in _DETERMINISTIC_HEADERS:
            continue
        return body[m.start():]
    # No non-deterministic top-level heading after the deterministic prefix:
    # this is only deterministic sections -> no narrative to check.
    return ""



def validate_synthesis_consistency(
    synthesized_answer: str,
    verification_result: dict | None,
    governance_context: dict | None = None,
) -> list[dict]:
    """Deterministic FIX 6 + Phase 1 guard. Returns a list of contradiction
    records:

        [{"type": "REJECTED_OPTION_RECOMMENDED"|"WINNER_REJECTED",
          "dispute": ..., "normalized_key": ..., "option": ...,
          "evidence": <the matched substring>}]

    Empty list = the narrative is consistent with the verifier.

    Only the NARRATIVE is examined. The deterministic Verified Decisions /
    Verified Tradeoffs / Human Decisions Required sections are consistent by
    construction (they are rendered straight from verification_result), so
    they are stripped before checking. Otherwise a finding's own
    'reject <rejected>' clause (a negate context) and a neighbour section's
    dispute title (which can name a rejected option) would conspire into
    false positives. What can contradict the verifier is the LLM narrative.

    Phase 1 strengthening: rejected/winner tokens come from the CANONICAL
    decision_model accessors (`rejected_options(claim)`, `winner_tokens(claim)`).
    These use the ENRICHED negation grammar (`_REJECT_SIGNALS_RE`), which
    recognizes "argues against" / "rules out" / "discards" / "excludes" in
    addition to the legacy "reject" verb family. FIX 6 used the legacy
    reject-verb-only split, which let answer 156's "argues against PostgreSQL"
    phrasing produce zero rejected tokens. With the canonical accessor, any
    such finding produces the rejected tokens, so a narrative that then
    recommends them (with "Use X" / "Adopt X" / "Recommend X" framing, the
    contract every prior-flagged case satisfies) is flagged.

    Heuristic, no model calls. Pure substring/regex on the narrative.

    Note on the answer-156 bare-bullet case ("## Persistence Layer\\n- PostgreSQL
    primary datastore"): this is caught at its SOURCE by the
    `_architecture_components` rewire (the renderer can no longer emit a
    rejected option as the architecture — defense-in-depth plus
    winner-text-only alias matching). The validator does NOT attempt to flag
    a bare mention of a rejected option; doing so produces false positives on
    the deterministic narrative (e.g. "Provider" is both a winner token for
    infrastructure and a rejected token for the LLM gateway claim — the bullet
    "- Provider abstraction layer" is a legitimate winner mention, not a
    rejected-option endorsement). The validator's contract remains "flag a
    narrative that actively RECOMMENDS a rejected option as a choice", not
    "flag every absent-negate-context occurrence".
    """
    narrative = _narrative_of(synthesized_answer or "")
    # Section titles are labels, not recommendations.
    narrative = "\n".join(
        line for line in narrative.splitlines()
        if not line.lstrip().startswith("#")
    )
    contradictions: list[dict] = []

    for c in _resolved_claims(verification_result):
        nkey = _str(c.get("normalized_key"))
        dispute = _str(c.get("dispute"))
        # Phase 1: canonical accessors (enriched negation grammar).
        winners = winner_tokens(c)
        rejected = rejected_options(c)

        for r in rejected:
            if r not in narrative.lower():
                continue
            for before, after in _all_occurrences(narrative, r):
                # Whole-word context matching (see _RECOMMEND_CONTEXT_RES):
                # catches 'Use WebContainers' / 'Adopt IndexedDB' /
                # 'Recommend FastAPI' (the cases the tests pin) without
                # flagging 'deployment strategy' / 'building something else'.
                negate_before = _has_any_word(before, _NEGATE_CONTEXT_RES)
                recommend_before = _has_any_word(before, _RECOMMEND_CONTEXT_RES)
                recommend_after = _has_any_word(after, _RECOMMEND_CONTEXT_RES)
                recommend_before_only = recommend_before and not negate_before
                if recommend_before_only or (not negate_before and recommend_after):
                    contradictions.append({
                        "type": "REJECTED_OPTION_RECOMMENDED",
                        "dispute": dispute,
                        "normalized_key": nkey,
                        "option": r,
                        "evidence": (before + r + " …").strip(),
                    })
                    break  # one bad occurrence is enough to flag this option

        # Winner-negated check: conservative, only on distinctive winner tokens.
        for w in winners:
            if w not in narrative.lower():
                continue
            for before, _after in _all_occurrences(narrative, w, window=50):
                # Only flag if a NEGATE context is the nearest framing and there
                # is no canceling recommend context.
                if (_has_any_word(before, _NEGATE_CONTEXT_RES)
                        and not _has_any_word(before, _RECOMMEND_CONTEXT_RES)):
                    contradictions.append({
                        "type": "WINNER_REJECTED",
                        "dispute": dispute,
                        "normalized_key": nkey,
                        "option": w,
                        "evidence": (before + w + " …").strip(),
                    })
                    break

    # Deduplicate by (type, normalized_key, option) — a token can match twice.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for con in contradictions:
        key = (con["type"], con["normalized_key"], con["option"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(con)
    return unique


def contradiction_warning(contradictions: list[dict]) -> str:
    """Render a visible FIX 6 warning block appended to a synthesized answer
    when the narrative contradicts the verifier. Empty string when clean."""
    if not contradictions:
        return ""
    lines = [
        "# Contradiction Warning", "",
        (f"{len(contradictions)} inconsistency(ies) detected between the "
         "synthesis narrative and the verified verdicts. The verifier is "
         "authoritative; the deterministic Verified Decisions section above "
         "stands."),
        "",
    ]
    for con in contradictions:
        lines += [
            "---",
            "",
            f"Type: {con['type']}",
            f"Dispute: {con['dispute']}",
            f"Normalized Key: {con['normalized_key']}",
            f"Option: {con['option']}",
            f"Evidence: {con['evidence']}",
            "",
        ]
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FIX 7 — Deterministic narrative sections (Architecture, Implementation
# Plan, Migration Path, Risks) rendered straight from the decision model.
# No LLM. Reproducible byte-for-byte from the same inputs. The synthesis LLM
# no longer generates these sections; it cannot, by construction, recommend
# a verified-rejected option because the renderers only emit:
#   - winner tokens in affirmative locations, and
#   - rejected tokens inside an explicit 'reject' / 'off-limits' negate
#     context — validate_synthesis_consistency() considers that safe.
# Inputs (any subset; None-friendly): verification_result, governance_context,
# verified_decisions, verified_tradeoffs, human_required.
# ---------------------------------------------------------------------------

LOW_CONF_THRESHOLD = 80  # ponytail: fixed threshold; expose as needed.

_ARCH_LAYERS = (
    "Orchestration Layer",
    "Persistence Layer",
    "Storage Layer",
    "Execution Layer",
    "LLM Gateway Layer",
    "Governance Layer",
    "Cost Layer",
    "Infrastructure Layer",
)

_COMPONENTS = (
    {
        "name": "Temporal durable workflows",
        "risk_name": "Temporal",
        "layer": "Orchestration Layer",
        "aliases": ("temporal", "orchestration engine", "durable workflow", "durable workflows"),
        "step": "Deploy Temporal",
        "migration": "Core orchestration",
        "phase": "MVP",
        "risk": "Operational complexity",
    },
    {
        "name": "PostgreSQL primary datastore",
        "risk_name": "PostgreSQL",
        "layer": "Persistence Layer",
        "aliases": ("postgresql", "postgres", "primary database", "primary datastore"),
        "step": "Deploy PostgreSQL",
        "migration": "Core persistence",
        "phase": "MVP",
        "risk": "Migration complexity",
    },
    {
        "name": "Cloudflare R2 immutable storage",
        "risk_name": "Cloudflare R2",
        "layer": "Storage Layer",
        "aliases": ("cloudflare r2", " r2 ", "artifact storage", "immutable storage"),
        "step": "Configure Cloudflare R2",
        "migration": "Object storage",
        "phase": "Scale",
        "risk": "Vendor dependency",
    },
    {
        "name": "Local scratch filesystem",
        "layer": "Storage Layer",
        "aliases": ("local scratch", "scratch filesystem", "local filesystem"),
        "step": "Configure local scratch filesystem",
        "migration": "Local scratch workspace",
        "phase": "MVP",
    },
    {
        "name": "Docker workers",
        "layer": "Execution Layer",
        "aliases": ("docker", "container workers"),
        "step": "Deploy Docker Workers",
        "migration": "Docker execution",
        "phase": "MVP",
    },
    {
        "name": "Firecracker microVMs",
        "risk_name": "Firecracker",
        "layer": "Execution Layer",
        "aliases": ("firecracker", "microvm", "micro-vm"),
        "step": "Deploy Firecracker Workers",
        "migration": "Firecracker isolation",
        "phase": "Enterprise",
        "risk": "Operational overhead",
    },
    {
        "name": "LiteLLM",
        "risk_name": "LiteLLM",
        "layer": "LLM Gateway Layer",
        "aliases": ("litellm", "llm gateway"),
        "step": "Deploy LiteLLM",
        "migration": "Gateway abstraction",
        "phase": "Scale",
        "risk": "Gateway dependency",
    },
    {
        "name": "Sol0mon control plane",
        "layer": "Governance Layer",
        "aliases": ("sol0mon control plane", "control plane"),
        "step": "Deploy Sol0mon Control Plane",
        "migration": "Advanced governance",
        "phase": "Enterprise",
    },
    {
        "name": "Hybrid BYOK model",
        "risk_name": "BYOK",
        "layer": "Cost Layer",
        "aliases": ("byok", "bring your own key", "bring-your-own-key"),
        "step": "Configure BYOK Credential Resolver",
        "migration": "Cost controls",
        "phase": "Scale",
        "risk": "Support complexity",
    },
    {
        "name": "Provider abstraction layer",
        "layer": "Infrastructure Layer",
        "aliases": ("provider routing", "provider abstraction", "provider-agnostic", "provider agnostic", "avoid vendor lock-in"),
        "step": "Configure Provider Routing",
        "migration": "Provider routing",
        "phase": "Scale",
    },
    {
        "name": "Multi-region deployment",
        "layer": "Infrastructure Layer",
        "aliases": ("multi-region", "multiregion"),
        "step": "Configure multi-region deployment",
        "migration": "Multi-region support",
        "phase": "Enterprise",
    },
    {
        "name": "Advanced governance",
        "layer": "Infrastructure Layer",
        "aliases": ("advanced governance", "governance layer"),
        "step": "Configure advanced governance",
        "migration": "Advanced governance",
        "phase": "Enterprise",
    },
)


# ---------------------------------------------------------------------------
# Architecture Classification Fix V1 — deterministic keyword/rule-based
# classification so a VERIFIED decision naming a concrete technology is
# recognised as architecture even when it is NOT one of the platform-specific
# _COMPONENTS. Debate #145 exposed the gap: "Oracle VPS", "CoinGecko", "SMA
# Strategy", "Exchange Adapter" were verified but emitted as
# "No verified architectural decisions recorded." because no hard-coded alias
# matched. Pure substring keyword rules — no model calls. This only runs for a
# decision that did not already match a _COMPONENTS entry, so the existing
# platform debates (Temporal / PostgreSQL / R2 / Docker / ...) and their tests
# are byte-for-byte unchanged.
# ---------------------------------------------------------------------------

# Category rendering/grouping order (Phase 2). Matches the acceptance format:
# "# Architecture\n\nInfrastructure:\n- Oracle VPS".
_GENERIC_CATEGORIES = ("Infrastructure", "Data", "Integration", "Application")

# Keyword/rule table (Phase 2). Lowercased substring match on the space-padded
# decision source text. Classification order matters: Integration first
# (exchange / adapter are the most specific boundary terms), then Data ("api
# provider" wins before the bare "provider" of Infrastructure), then
# Application, then the generic Infrastructure fallback.
_GENERIC_KEYWORDS = {
    "Integration": ("exchange", "adapter", "connector", "broker"),
    "Data": ("api provider", "data source", "coingecko", "coinpaprika",
             "database", "cache", "storage"),
    "Application": ("strategy", "algorithm", "execution engine",
                    "scheduler", "monitoring"),
    "Infrastructure": ("hosting", "deployment", "provider", "vps", "oracle",
                       "cloud", "worker", "runtime"),
}
_GENERIC_CLASSIFY_ORDER = ("Integration", "Data", "Application", "Infrastructure")

# Deterministic per-category implementation action (Phase 3). Derived ONLY
# from the verified architecture — no hallucinated components.
_GENERIC_STEP = {
    "Infrastructure": "Create deployment configuration: {name}",
    "Data": "Create market data layer: {name}",
    "Integration": "Create exchange integration layer: {name}",
    "Application": "Create strategy module: {name}",
}

# Regex of leading resolution / article phrases stripped from a winner phrase.
# Repeated in _clean_winner_name so a multi-clause lead ("resolved by adopting
# a") is fully removed before the component label is taken.
# (Now owned by decision_model; re-exported above.)

# Prose leakage markers: a real component label never looks like a rationale
# sentence. Used by _is_clean_label to keep long prose findings (Debate #55
# style) out of the architecture bullets.
# (Now owned by decision_model; re-exported above.)


def _classify_decision_layer(haystack: str) -> str | None:
    """Deterministic keyword/rule classification (Phase 2). The first matching
    category in _GENERIC_CLASSIFY_ORDER wins. Returns the category name or None
    when no architecture keyword is present."""
    for category in _GENERIC_CLASSIFY_ORDER:
        for kw in _GENERIC_KEYWORDS[category]:
            if kw in haystack:
                return category
    return None


def _generic_component_for(d: dict, haystack: str) -> dict | None:
    """Build a generic architecture component from a VERIFIED decision that did
    not match any _COMPONENTS entry. Deterministic, no model calls. Returns
    None when the decision is not classifiable or its winner phrase is not a
    clean short label (guards against emitting rationale prose, a rejected
    option, or an unclassifiable finding as the architecture component).

    Phase 1: winner name and rejected tokens come from the canonical
    decision_model accessors. The safety-net check uses
    decision_model.rejected_options (enriched grammar) - not the legacy
    reject-verb-only split - so a rejected option named in the winner via a
    shared token (e.g. "argues against X" with X appearing later) cannot slip
    into the architecture."""
    layer = _classify_decision_layer(haystack)
    if not layer:
        return None
    name = winner_text(d)
    if not name or not _is_clean_label(name):
        return None
    # Renderer guard (Debate #164 class): refuse to emit a winner that is
    # the dispute description echoed back. Pure renderer-side; the decision
    # model is untouched. The finding still surfaces in # Verified Decisions
    # and # Constraints.
    dispute_text = _str(d.get("dispute") or d.get("dispute_text") or "")
    if _is_dispute_echo(name, dispute_text):
        return None
    # Safety net: never let a purely-rejected token of this decision pass as
    # the winner. Tokens that are also winner tokens (a shared generic word
    # like 'matching') are allowed, so a winner sharing a word with the loser
    # still emits.
    winset = set(winner_tokens(d))
    rejected = set(rejected_options(d))
    low = name.lower()
    for r in rejected:
        if r and r in low and r not in winset:
            return None
    return {"name": name, "layer": layer,
            "step": _GENERIC_STEP[layer].format(name=name)}


def _verified_decisions_list(
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
) -> list[dict]:
    """Schema-A VERIFIED_DECISION outcomes, merged from the explicit arg and
    governance_context.verified_decisions. Same shape whether bridged from
    verification_result (answer_loader) or produced by governance."""
    items: list[dict] = []
    if isinstance(verified_decisions, list):
        items.extend(d for d in verified_decisions if isinstance(d, dict))
    gc = _as_dict(governance_context)
    for d in (gc.get("verified_decisions") or []):
        if isinstance(d, dict):
            items.append(d)
    return items


def _tradeoffs_list(
    governance_context: dict | None = None,
    verified_tradeoffs: list[dict] | None = None,
) -> list[dict]:
    items: list[dict] = []
    if isinstance(verified_tradeoffs, list):
        items.extend(d for d in verified_tradeoffs if isinstance(d, dict))
    gc = _as_dict(governance_context)
    for d in (gc.get("verified_tradeoffs") or []):
        if isinstance(d, dict):
            items.append(d)
    return items


def _human_required_list(
    governance_context: dict | None = None,
    human_required: list[dict] | None = None,
) -> list[dict]:
    items: list[dict] = []
    if isinstance(human_required, list):
        items.extend(d for d in human_required if isinstance(d, dict))
    gc = _as_dict(governance_context)
    for d in (gc.get("human_required")
              or gc.get("human_required_items") or []):
        if isinstance(d, dict):
            items.append(d)
    return items


def _dispute_text(d: dict) -> str:
    return _str(d.get("dispute_text") or d.get("dispute"), "(untitled)")


def _decision_text(d: dict) -> str:
    """The chosen-option text for a verified decision (Schema-A)."""
    return _str(d.get("decision") or d.get("reasoning") or d.get("finding"), "")




def _source_text(d: dict) -> str:
    parts = [
        d.get("dispute"),
        d.get("dispute_text"),
        d.get("decision"),
        d.get("finding"),
        d.get("reasoning"),
    ]
    v = d.get("verification")
    if isinstance(v, dict):
        parts.append(v.get("finding"))
    return " ".join(_str(p) for p in parts if p)


def _decision_sources(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
) -> list[dict]:
    items = list(_resolved_claims(verification_result))
    items.extend(_verified_decisions_list(governance_context, verified_decisions))
    return items


def _architecture_components(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
) -> list[dict]:
    """Phase 1: build the architecture from the CANONICAL DECISION MODEL only.

    Each decision source's winner text is computed by decision_model (text
    after the winner marker, cut at the first enriched-grammar REJECT signal).
    Platform component aliases are matched against that winner text only,
    never against the dispute title (which names BOTH options and is the
    source of the answer-156 PostgreSQL leak).

    Defense-in-depth: any platform component whose alias appears in the
    canonical `rejected_options` is suppressed. Together with winner-side-only
    matching, this makes it impossible for a rejected option to surface as
    the architecture - the contradiction class observed in answer 156 is
    eliminated, not just detected.
    """
    matched: list[dict] = []
    seen: set[str] = set()           # _COMPONENTS names already emitted
    seen_generic: set[tuple[str, str]] = set()  # (layer, name.lower())
    for d in _decision_sources(
        verification_result, governance_context, verified_decisions
    ):
        wtext = winner_text(d)
        if not wtext:
            # No winner extracted from the finding. Previously this fell back
            # to the dispute title (the answer-156 bug). It no longer does.
            continue
        # Renderer guard (Debate #164 class): when winner_text returns the
        # dispute description echoed back (findings that open with "The
        # dispute description claims..."), refuse to use it as an
        # architecture component name — it names BOTH options and leaks the
        # rejected option. The decision still surfaces in # Verified
        # Decisions and # Constraints; only the architecture rendering is
        # sanitized. Pure renderer-side; the decision model is untouched.
        dispute_text = _str(d.get("dispute") or d.get("dispute_text") or "")
        if _is_dispute_echo(wtext, dispute_text):
            continue
        haystack = f" {wtext.lower()} "
        rejected = set(rejected_options(d))
        # First, the hard-coded platform components. A single verified decision
        # can name several of them (e.g. "Docker and Firecracker"); all are
        # emitted. solomon_hit records that this decision was already consumed
        # so it is NOT re-classified as a generic component.
        solomon_hit = False
        for component in _COMPONENTS:
            # Defense-in-depth: if any of this component's aliases is in the
            # canonical rejected-options set, the verifier ruled this option
            # out — never emit it as the architecture.
            if any(alias in rejected for alias in component["aliases"]):
                continue
            if any(alias in haystack for alias in component["aliases"]):
                solomon_hit = True
                if component["name"] not in seen:
                    seen.add(component["name"])
                    matched.append(component)
        if solomon_hit:
            continue
        # Architecture Classification Fix V1: a verified decision that named no
        # platform component is still architecture if it names a concrete
        # technology. Deterministic keyword classification → generic component.
        comp = _generic_component_for(d, haystack)
        if comp:
            key = (comp["layer"], comp["name"].lower())
            if key not in seen_generic:
                seen_generic.add(key)
                matched.append(comp)
    return matched


def _conf(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 100  # ponytail: missing confidence is treated as max, not 0


def _all_rejected_tokens(verification_result: dict | None) -> list[str]:
    """Distinct rejected-option tokens across every RESOLVED finding, in
    first-seen order. Used by render_risks for the OFF_LIMITS line.

    Phase 1: reads the canonical `rejected_options(claim)` accessor rather
    than the legacy reject-verb-only split, so findings phrased as 'argues
    against' / 'rules out' / 'discards' contribute their rejected tokens
    too."""
    out: list[str] = []
    seen: set[str] = set()
    for c in _resolved_claims(verification_result):
        for r in rejected_options(c):
            if r not in seen:
                seen.add(r)
                out.append(r)
    return out


def render_architecture(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
    verified_tradeoffs: list[dict] | None = None,
    human_required: list[dict] | None = None,
) -> str:
    """Deterministic Architecture Composer: verified decisions grouped into
    architecture layers. No LLM, no model calls.

    Platform-specific decisions render under their hard-coded _ARCH_LAYERS
    (`## <Layer>`). Any VERIFIED decision that named a concrete technology but
    no platform component is classified (Phase 2) and rendered under its
    generic category (`<Category>:`) — the Architecture Classification Fix V1
    that stops Debate #145-style decisions being discarded."""
    lines = ["# Architecture", ""]
    components = _architecture_components(
        verification_result, governance_context, verified_decisions
    )
    if not components:
        lines.append("No verified architectural decisions recorded.")
        lines.append("")
        return "\n".join(lines)

    for layer in _ARCH_LAYERS:
        layer_items = [c for c in components if c["layer"] == layer]
        if not layer_items:
            continue
        lines.append(f"## {layer}")
        lines.extend(f"- {c['name']}" for c in layer_items)
        lines.append("")
    # Generic classified decisions (Phase 2 categories). The heading style
    # (`Category:`) deliberately differs from the platform `## <Layer>` so a
    # user-facing reader can tell a classified component from a recognised one.
    for category in _GENERIC_CATEGORIES:
        cat_items = [c for c in components if c["layer"] == category]
        if not cat_items:
            continue
        lines.append(f"{category}:")
        lines.extend(f"- {c['name']}" for c in cat_items)
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def render_implementation_plan(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
    verified_tradeoffs: list[dict] | None = None,
    human_required: list[dict] | None = None,
) -> str:
    """Deterministic ordered implementation steps from verified components."""
    lines = ["# Implementation Plan", ""]
    components = _architecture_components(
        verification_result, governance_context, verified_decisions
    )
    if not components:
        lines.append("No implementation steps; nothing verified.")
        lines.append("")
        return "\n".join(lines)

    order = {
        "Persistence Layer": 0,
        "Orchestration Layer": 1,
        "Storage Layer": 2,
        "LLM Gateway Layer": 3,
        "Execution Layer": 4,
        "Cost Layer": 5,
        "Governance Layer": 6,
        "Infrastructure Layer": 7,
        # Generic classified components (Phase 3) sort after the platform
        # components, in the Phase 2 category order.
        "Infrastructure": 10,
        "Data": 11,
        "Integration": 12,
        "Application": 13,
    }
    components = sorted(components, key=lambda c: order.get(c["layer"], 99))
    for idx, component in enumerate(components, start=1):
        lines += [f"Step {idx}", component["step"], ""]
    lines.append("")
    return "\n".join(lines)


def render_migration_path(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
    verified_tradeoffs: list[dict] | None = None,
    human_required: list[dict] | None = None,
) -> str:
    """Deterministic three-phase migration path from verified components."""
    lines = ["# Migration Path", ""]
    components = _architecture_components(
        verification_result, governance_context, verified_decisions
    )
    phases = (
        ("Phase 1 MVP", "MVP"),
        ("Phase 2 Scale", "Scale"),
        ("Phase 3 Enterprise", "Enterprise"),
    )
    migration_order = {
        "Persistence Layer": 0,
        "Orchestration Layer": 1,
        "Execution Layer": 2,
        "Storage Layer": 3,
        "LLM Gateway Layer": 4,
        "Infrastructure Layer": 5,
        "Cost Layer": 6,
        "Governance Layer": 7,
    }
    for title, key in phases:
        items = sorted(
            (c for c in components if c.get("phase") == key),
            key=lambda c: migration_order.get(c["layer"], 99),
        )
        if not items:
            continue
        lines.append(f"## {title}")
        lines.extend(c.get("migration", c["name"]) for c in items)
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def render_risks(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
    verified_tradeoffs: list[dict] | None = None,
    human_required: list[dict] | None = None,
) -> str:
    """Deterministic risks only for verified components with known risks."""
    lines = ["# Risks", ""]
    components = _architecture_components(
        verification_result, governance_context, verified_decisions
    )
    risk_items = [c for c in components if c.get("risk")]
    if not risk_items:
        lines.append("No risks identified from the verified decision model.")
        lines.append("")
        return "\n".join(lines)

    for component in risk_items:
        lines += [f"## {component.get('risk_name', component['name'])}", component["risk"], ""]
    lines.append("")
    return "\n".join(lines)


def render_narrative_sections(
    verification_result: dict | None = None,
    governance_context: dict | None = None,
    verified_decisions: list[dict] | None = None,
    verified_tradeoffs: list[dict] | None = None,
    human_required: list[dict] | None = None,
) -> str:
    """All four deterministic narrative sections in canonical order:
    Architecture, Implementation Plan, Migration Path, Risks. Used by the
    synthesis engine when no narrative_fn seam is supplied — production
    output is fully reproducible from the decision model with no LLM."""
    return "\n\n".join([
        render_architecture(verification_result, governance_context,
                            verified_decisions, verified_tradeoffs,
                            human_required),
        render_implementation_plan(verification_result, governance_context,
                                    verified_decisions, verified_tradeoffs,
                                    human_required),
        render_migration_path(verification_result, governance_context,
                              verified_decisions, verified_tradeoffs,
                              human_required),
        render_risks(verification_result, governance_context,
                     verified_decisions, verified_tradeoffs, human_required),
    ])


if __name__ == "__main__":
    # Smoke self-check: render + consistency against a synthetic verdict.
    vr = {
        "resolved_claims": [
            {"dispute": "Execution Env: Server Docker vs WebContainers",
             "normalized_key": "execution_environment",
             "verification": {"status": "RESOLVED", "confidence": 99,
                              "finding": ("The dispute is resolved in favor of "
                                          "Server-side Docker. Both evidence "
                                          "strips explicitly reject Client-side "
                                          "WebContainers for the authoritative "
                                          "execution path.")}},
        ],
        "equivalent_claims": [], "unresolved_claims": [],
        "verification_score": 100, "truncated_count": 0,
    }
    gc = {
        "verified_tradeoffs": [
            {"outcome_type": "VERIFIED_TRADEOFF", "dispute_key": "cache",
             "dispute_text": "Redis vs Memcached", "option_a": "Redis",
             "option_b": "Memcached", "recommendation": "Redis",
             "confidence": 80, "reasoning": "persistence + types"},
        ],
        "human_required": [
            {"outcome_type": "HUMAN_REQUIRED", "decision_id": "d1",
             "dispute_text": "K8s vs Nomad", "option_a": "K8s",
             "option_b": "Nomad", "reasoning": "ops preference",
             "recommendation": "K8s"},
        ],
        "verified_decisions": [],
    }

    dec = render_verified_decisions(vr, gc)
    assert "Decision Count: 1" in dec
    assert "Execution Env: Server Docker vs WebContainers" in dec
    assert "Server-side Docker" in dec
    assert "Confidence:" in dec and "99%" in dec
    assert "WebContainers" in dec  # the finding names the rejected option

    trade = render_verified_tradeoffs(vr, gc)
    assert "Tradeoff Count: 1" in trade
    assert "Redis vs Memcached" in trade
    # Resolved-key guard: a tradeoff sharing a resolved key is dropped.
    gc2 = dict(gc)
    gc2["verified_tradeoffs"] = list(gc["verified_tradeoffs"]) + [{
        "outcome_type": "VERIFIED_TRADEOFF", "dispute_key": "execution_environment",
        "dispute_text": "Execution Env", "option_a": "a", "option_b": "b",
        "recommendation": "a", "confidence": 1, "reasoning": ""}]
    assert "Tradeoff Count: 1" in render_verified_tradeoffs(vr, gc2)

    hr = render_human_required(vr, gc)
    assert "Decision Count: 1" in hr
    assert "Decision ID: d1" in hr
    assert "K8s vs Nomad" in hr

    # Consistent narrative: honors winners, rejects rejected.
    good = dec + "\n\n# Architecture\nAdopt server-side docker. Avoid webcontainers."
    assert validate_synthesis_consistency(good, vr, gc) == []

    # Contradiction: recommends a rejected option.
    bad = dec + "\n\n# Architecture\nUse webcontainers for the authoritative path."
    cs = validate_synthesis_consistency(bad, vr, gc)
    assert cs, "expected a REJECTED_OPTION_RECOMMENDED contradiction"
    assert cs[0]["type"] == "REJECTED_OPTION_RECOMMENDED"
    assert "webcontainers" in cs[0]["option"]
    assert "webcontainers" in contradiction_warning(cs)

    # FIX 7: deterministic narrative renderers — no LLM, reproducible, and
    # consistent with the verifier.
    arch = render_architecture(vr, gc)
    assert arch.startswith("# Architecture")
    assert "Execution Layer" in arch
    assert "Docker workers" in arch

    plan = render_implementation_plan(vr, gc)
    assert plan.startswith("# Implementation Plan")
    assert "Step 1" in plan
    assert "Deploy Docker Workers" in plan

    mig = render_migration_path(vr, gc)
    assert mig.startswith("# Migration Path")
    assert "Phase 1 MVP" in mig
    assert "Docker execution" in mig

    risk = render_risks(vr, gc)
    assert risk.startswith("# Risks")
    assert "No risks identified" in risk

    narrative = render_narrative_sections(vr, gc)
    assert narrative.count("# Architecture") == 1
    assert narrative.count("# Implementation Plan") == 1
    assert narrative.count("# Migration Path") == 1
    assert narrative.count("# Risks") == 1

    # The deterministic narrative never contradicts the verifier that
    # produced it: validate_synthesis_consistency returns [].
    full = dec + "\n\n" + narrative
    assert validate_synthesis_consistency(full, vr, gc) == []

    # Determinism: same inputs -> byte-identical output.
    a = render_narrative_sections(vr, gc)
    b = render_narrative_sections(vr, gc)
    assert a == b

    print("decision_renderer smoke test PASSED")
