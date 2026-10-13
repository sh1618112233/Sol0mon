"""
Project Sol0mon - Canonical Decision Model (Phase 0 + Phase 1).

The SINGLE OWNER of decision interpretation. Every downstream consumer -
Architecture Renderer, Build Spec Engine, Executor, Human Synthesis,
Persistence, Telemetry, and the FIX 6 contradiction validator - MUST read a
verified decision through this module. Never parse `verification_result`
directly, and never read previously-rendered markdown. This is the invariant
that prevents the divergence class observed in answer 156:

    answer.md says SQLite         <- verifier winner
    architecture says PostgreSQL   <- renderer read the dispute title, not the winner
    implementation deploys PostgreSQL
    execution_success = true       <- no artefact, no tests, but "no failure"

Phase 1 makes decision_model the SOLE OWNER: the interpretation primitives
(winner-phrase parser, reject-signal grammar, tokens, clean-winner-name) live
HERE. decision_renderer imports them back and re-exports them at its module
boundary so existing external imports keep working - a back-compat shim only.
The parsing is no longer reducible to two divergent copies anywhere in the
codebase.

Per resolved claim the Canonical Decision Model exposes:

    winner_text       - the option the verifier chose. Computed from the
                        WINNER PORTION of the finding ONLY (text after the
                        winner marker, cut at the first REJECT signal - so the
                        rejected option's name can never leak into the winner).
                        NEVER falls back to the dispute title - the dispute
                        names both options and is the source of the 156 bug.
    winner_tokens    - the significant tokens of `winner_text` (for matching
                        against component alias tables).
    rejected_options - the options the verifier ruled out. Source order:
                        1. `verification.rejected_options` (structured field,
                           populated by the verifier's batch inference - Phase 0
                           option (a): the verifier already knows which option
                           it rejected).
                        2. Fallback: `derive_rejected_options(finding)` - a
                           deterministic derivation with an ENRICHED negation
                           grammar. FIX 6's `_REJECT_RE` matched only the
                           "reject" verb family, which let 156's "argues
                           against PostgreSQL" phrasing slip past with no
                           rejected tokens; the enriched grammar recognizes
                           "argues against", "rules out", "discards", etc.

Pure stdlib. No LLM, no I/O. The derivation helpers are shared with the
verifier (`verification_engine`) so new answers (structured field) and legacy
answers (fallback derivation) produce identical tokens.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Basic shape helpers (own copy - no longer borrowed from decision_renderer)
# ---------------------------------------------------------------------------


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _str(x: Any, default: str = "") -> str:
    s = "" if x is None else str(x)
    return s if s else default


def _verifications(claim: dict) -> dict:
    v = claim.get("verification") if isinstance(claim, dict) else None
    return v if isinstance(v, dict) else {}


def _resolved_claims(verification_result: dict | None) -> list[dict]:
    vr = _as_dict(verification_result)
    return [c for c in (vr.get("resolved_claims") or []) if isinstance(c, dict)]


def _resolved_keys(verification_result: dict | None) -> set[str]:
    """normalized_keys the verifier marked RESOLVED. Used to keep resolved
    disputes out of the tradeoff section (resolution beats tradeoff)."""
    out: set[str] = set()
    for c in _resolved_claims(verification_result):
        nk = (c.get("normalized_key") or "").strip().lower()
        if nk:
            out.add(nk)
    return out


# ---------------------------------------------------------------------------
# Token / phrase parsing primitives - the soul of decision interpretation.
# Owned here. decision_renderer re-exports for backwards compat.
# ---------------------------------------------------------------------------


# Words to strip when turning a finding sentence into option tokens. Generous
# stop list: generic architecture / rationale words would cause false matches.
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "for", "is", "are", "by",
    "with", "that", "this", "both", "evidence", "strips", "dispute", "resolved",
    "resolution", "favor", "favour", "converges", "establishing", "explicitly",
    "selection", "selecting", "adopting", "choosing", "rather", "than", "not",
    "primary", "always", "on", "always-on", "backend", "approach", "model",
    "system", "record", "runtime", "execution", "path", "layer", "plane",
    "architecture", "scale", "zero", "distributed", "hybrid", "native",
    "coordination", "front", "end", "edge", "server", "side", "client",
    "identical", "converged", "describes", "design", "static", "control",
    "leased", "workers", "containers", "authoritative", "isolated", "unifies",
    "assigning", "distinct", "layers", "reliance", "cost", "costs", "inference",
    "financial", "exposure", "monolith", "domain", "agent", "agents", "loop",
    "browser", "per", "se", "storage", "store", "database", "framework",
    "strategy", "hosting", "infrastructure", "orchestration", "persistence",
    "stack", "via", "into", "its", "it", "as", "at", "be", "been", "from",
    "has", "have", "had", "was", "were", "all", "any", "each", "every", "such",
    "their", "them", "they", "we", "you", "your", "which", "while", "where",
    "when", "what", "these", "those",
}

# Bug fix (Debate #120): ordinary English words that appear in verifier
# rationale must never be treated as architecture options.
_GENERIC_OPTION_WORDS = frozenset({
    "architectural", "fundamental", "implementation", "approach", "strategy",
    "design", "system", "platform", "conflict", "resolved", "decision",
    "winner", "rationale",
})

# ponytail: stop set for OPTION tokenization = stopwords + the generic-option
# blocklist. Only used for finding option-extraction (_tokens); the narrative
# is scanned with a different whole-word regex, so this never masks a real
# contradiction's surface text.
_OPTION_STOP: set[str] = set(_STOPWORDS) | set(_GENERIC_OPTION_WORDS)


def _tokens(text: str, stop: set[str] = _OPTION_STOP) -> list[str]:
    """Significant lowercase tokens (alnum runs, len>=3, not stop). '/' is a
    delimiter so compound option names like 'webcontainers/webassembly' split
    into individually matchable tokens; '.'/'-' are kept so dotted/hyphenated
    names like 'y.js' and 'client-side' stay intact."""
    out: list[str] = []
    for m in re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", (text or "").lower()):
        t = m.strip("./")
        if not t or t in stop or len(t) < 3:
            continue
        out.append(t)
    return out


# Legacy reject-verb grammar. Kept for `_first_reject_pos` back-compat callers.
_REJECT_RE = re.compile(r"\b(reject|rejects|rejecting|rejected)\b", re.IGNORECASE)


def _first_reject_pos(finding_lower: str) -> int:
    """Return the start index of the first reject verb in the lowercased
    finding, or -1. Legacy grammar - kept for callers that test for the
    'reject' verb family specifically (back-compat). The canonical accessor
    (`rejected_options`) uses the ENRICHED grammar `_REJECT_SIGNALS_RE`."""
    m = _REJECT_RE.search(finding_lower)
    return -1 if (m is None) else m.start()


# ponytail: enriched negation grammar - the canonical derivation's source of
# truth for what counts as a rejection. FIX 6's _REJECT_RE matched only the
# "reject" verb family, which let answer 156's "argues against PostgreSQL"
# phrasing slip past with no rejected tokens; this enriched grammar recognizes
# every unambiguous negation phrasing the verifier actually emits. False
# negatives (missing a real rejection) are the contradiction bug; false
# positives (flagging a non-rejection) only suppress a spurious component, the
# safe direction. Ambiguous phrasings ("against" alone, "rather than",
# "instead of") are intentionally excluded - they fire on non-rejections too.
_REJECT_SIGNALS_RE = re.compile(
    r"\b("
    r"reject|rejects|rejecting|rejected|"
    r"rules\s+out|ruled\s+out|"
    r"argues\s+against|argued\s+against|"
    r"discards|discarded|"
    r"excludes|excluded|"
    r"off[-\s]limits|no\s+longer|"
    r"explicitly\s+(?:rejects|rules\s+out|discards|excludes)"
    r")\b",
    re.IGNORECASE,
)

# Markers that introduce the WINNER name; the chosen option follows these.
_WINNER_MARKERS = (
    "in favor of", "in favour of", "selecting", "adopting", "choosing",
    "the selection of", "with the explicit selection of", "resolved by",
    "resolved through", "converges on", "the explicit selection of",
)

# Regex of leading resolution / article phrases stripped from a winner phrase.
# Repeated in _clean_winner_name so a multi-clause lead ("resolved by adopting
# a") is fully removed before the component label is taken.
_LEAD_RES = re.compile(
    r"^\s*(?:the\s+dispute\s+is\s+resolved\s+(?:in\s+favou?r\s+of|by|with|through|as|via)|"
    r"resolved\s+(?:in\s+favou?r\s+of|by|with|through|as|via)|"
    r"(?:the\s+)?(?:explicit\s+)?selection\s+of|"
    r"(?:the\s+)?evidence\s+(?:strips\s+)?converges\s+on|converges\s+on|"
    r"adopt(?:ing|ed)?|select(?:ing|ed)?|choos(?:ing|e)?|use(?:s|ing|d)?|"
    r"winner\s*:|reject(?:ing|s|ed)?|"
    r"the|a|an)\b\s*",
    re.IGNORECASE,
)

# Prose leakage markers: a real component label never looks like a rationale
# sentence. Used by _is_clean_label to keep long prose findings out of the
# architecture bullets.
_PROSE_MARKERS = (
    "both evidence", "evidence strips", "the dispute", "converges",
    "describes", "identifies", "establishes", "effectively", "explicitly",
    "rather than", "reject",
)

# Boundaries that close a rejected-option clause so the parser does not swallow
# the winner's name that follows.
_REJECTED_CLAUSE_BOUNDS = (
    ". ", "; ", " in favor of ", " in favour of ", " favoring ",
    " favouring ", " for ", " and ", " while ", " to ",
)

# Markdown emphasis chars stripped before reject-signal / winner-phrase
# parsing. The verifier's findings sometimes use "*against*" or "_against_"
# to emphasize the negation; without stripping, `argues\s+against` would miss
# "argues *against*" (answer 156's exact phrasing). Strip the punctuation at
# the entry to both `derive_rejected_options` and `_winner_phrase` so the
# enriched negation grammar matches markdown-emphasized as well as plain
# prose negations. Stripping from both `s` and `low` keeps their character
# positions aligned.
_EMPHASIS_CHARS = "*_`"


def _strip_emphasis(text: str) -> str:
    """Remove markdown emphasis punctuation (* _ `) so negation phrases like
    'argues *against*' are recognized as the literal 'argues against'. The
    strip preserves character offsets elsewhere (it removes single chars; the
    remaining text keeps its relative order)."""
    if not text:
        return text
    for ch in _EMPHASIS_CHARS:
        text = text.replace(ch, "")
    return text


def _winner_phrase(finding: str) -> str:
    """The substring that NAMES the chosen option. Cuts at the FIRST enriched
    reject signal (so a finding like "Both ... argues against PostgreSQL ...
    for SQLite ..." yields a winner portion that never contains 'postgresql').
    Then takes the text - WITHIN the pre-reject portion only - after the last
    winner-marker, bounded at the first sentence/clause end ('. ' / '; ').

    The slice is taken from `pre_s` (the original-case prefix of `finding`
    cut at the reject signal), NOT from `s` itself. Findings of the form
    "Resolved by rejecting X; Y..." place the rejected option immediately
    after a "rejecting" verb; with `s[start:]` the slice would begin at
    "rejecting X..." - the loser - and the winner-side check would treat the
    loser tokens as winners. `pre_s[start:]` stays inside the pre-reject text,
    so when the winner is mentioned AFTER the reject (a rejected-first finding)
    the winner portion is empty - no leak. CamelCase is preserved because the
    slice is over the original-case `pre_s`, not the lowercased `pre_low`.

    Markdown emphasis (* _ `) is stripped at entry so negations like
    "argues *against*" (answer 156's exact phrasing) match the enriched
    `argues\\s+against` pattern."""
    s = (finding or "").strip()
    if not s:
        return ""
    s = _strip_emphasis(s)
    low = s.lower()
    m = _REJECT_SIGNALS_RE.search(low)
    if m:
        pre_low = low[: m.start()]
        pre_s = s[: m.start()]
    else:
        pre_low = low
        pre_s = s
    start = 0
    for mk in _WINNER_MARKERS:
        i = pre_low.rfind(mk)
        if i != -1 and i + len(mk) > start:
            start = i + len(mk)
    sub = (pre_s[start:].strip() if start else pre_s.strip())
    bound = len(sub)
    for sep in (". ", "; "):
        j = sub[:bound].find(sep)
        if j != -1 and j < bound:
            bound = j
    return sub[:bound].strip()


def _clean_winner_name(phrase: str) -> str:
    """Strip leading resolution phrasing/articles and surrounding quotes from a
    winner phrase. Iterates so a multi-clause lead is fully removed. Preserves
    internal CamelCase (CoinGecko, SMA, VPS)."""
    t = (phrase or "").strip()
    if not t:
        return ""
    prev = None
    while t and t != prev:
        prev = t
        m = _LEAD_RES.match(t)
        if m:
            t = t[m.end():]
        t = t.strip()
    t = t.strip(".'\"'\"),")
    t = " ".join(t.split())
    if not t:
        return ""
    return t[0].upper() + t[1:]


def _is_clean_label(name: str) -> bool:
    """A component label is a short, clean phrase - not a rationale sentence.
    Excludes prose markers, commas, periods, parentheses and overly long
    names so long verifier findings are not mis-emitted as architecture
    bullets."""
    if not name:
        return False
    if len(name) > 80 or len(name.split()) > 8:
        return False
    low = name.lower()
    for bad in (".", ",", "(", ")"):
        if bad in low:
            return False
    for bad in _PROSE_MARKERS:
        if bad in low:
            return False
    return True


def _winner_raw(d: dict) -> str:
    """The canonical winner signal for a decision source. Prefer the explicit
    chosen option (Schema-A `decision`), then the verifier finding, then the
    reasoning. NEVER falls back to `dispute_text` / `dispute` - those name BOTH
    options and are the source of the answer-156 rejected-option-leak bug. Empty
    string when nothing is recorded."""
    if not isinstance(d, dict):
        return ""
    v = d.get("verification")
    if isinstance(v, dict):
        cand = v.get("finding") or v.get("reasoning")
        if cand:
            return _str(cand)
    cand = d.get("decision") or d.get("finding") or d.get("reasoning")
    return _str(cand) if cand else ""


def _winner_and_rejected_tokens(finding: str) -> tuple[list[str], list[str]]:
    """Split a finding into (winner_tokens, rejected_tokens) around the first
    REJECT signal (enriched grammar).

    - rejected_tokens: significant tokens from the clause AFTER the reject
      signal, up to the next sentence end or a connective boundary that
      introduces the winning side.
    - winner_tokens: significant tokens from the affirmative clause that NAMES
      the chosen option (after the last winner-marker in the pre-reject text;
      falls back to the whole pre-reject text).

    Pure; uses `_REJECT_SIGNALS_RE` (enriched) rather than the legacy
    `_REJECT_RE`, so findings whose negation is phrased as 'argues against'
    / 'rules out' / 'discards' produce the correct rejected tokens (FIX 6
    missed this and was bypassed by answer 156). Markdown emphasis (* _ `)
    is stripped at entry so 'argues *against*' matches 'argues against'."""
    low = (finding or "").lower()
    if not low:
        return [], []
    low = _strip_emphasis(low)
    m = _REJECT_SIGNALS_RE.search(low)
    if not m:
        # No rejection: winner is the whole finding; nothing rejected.
        return _tokens(low), []
    pos = m.start()
    pre = low[:pos]
    rejected_part = low[m.end():].lstrip(" :,;")
    if not rejected_part:
        return _tokens(_winner_phrase(finding)), []

    # Bound the rejected clause: cut at the first sentence end or a connective
    # that introduces the winning side (" in favor of ", " favoring ",
    # " establishing "). Otherwise the clause swallows the winner's name and
    # turns it into spurious rejected tokens.
    best = len(rejected_part)
    for sep in _REJECTED_CLAUSE_BOUNDS:
        i = rejected_part.find(sep)
        if i != -1 and i < best:
            best = i
    rejected_part = rejected_part[:best]

    # Winner portion: text after the last winner-marker in the pre-reject
    # clause.
    start = 0
    for mk in _WINNER_MARKERS:
        i = pre.rfind(mk)
        if i != -1 and i > start:
            start = i + len(mk)
    winner_part = pre[start:] if start else pre

    # Bound the winner clause at the first sentence/clause end. (' for '/' and '
    # are NOT bounds - they sit between a winner and its descriptor.)
    best_w = len(winner_part)
    for sep in (". ", "; "):
        i = winner_part.find(sep)
        if i != -1 and i < best_w:
            best_w = i
    winner_part = winner_part[:best_w]

    return _tokens(winner_part), _tokens(rejected_part)


# ---------------------------------------------------------------------------
# Canonical accessors - the contract every consumer reads.
# ---------------------------------------------------------------------------


# Canonical record fields (the contract every consumer reads).
FIELDS = (
    "normalized_key",
    "dispute",
    "status",
    "confidence",
    "finding_raw",
    "winner_text",
    "winner_tokens",
    "rejected_options",
)


def winner_text(claim: dict) -> str:
    """The option the verifier chose - winner portion of the finding only.
    Never the dispute title (which names both options and is the source of
    the answer-156 PostgreSQL leak). Computed via `_winner_phrase` over
    `_winner_raw(claim)` (which prefers `verification.finding`, then
    `decision`, then `reasoning` - never the dispute)."""
    return _clean_winner_name(_winner_phrase(_winner_raw(claim)))


def winner_tokens(claim: dict) -> list[str]:
    """Significant tokens of the winner portion. Component alias tables
    (decision_renderer._COMPONENTS) MUST match against this, never against
    the dispute title or the full finding."""
    return _tokens(_winner_phrase(_winner_raw(claim)))


def derive_rejected_options(finding: str) -> list[str]:
    """Deterministically derive rejected-option tokens from a verifier finding.

    Used by:
      - the verifier (`verification_engine`), to persist
        `verification.rejected_options` for new answers (Phase 0 option (a));
      - `rejected_options(claim)`, as the fallback when the structured field is
        absent (legacy answers like 156).

    The rejected option is the significant token(s) immediately AFTER the first
    REJECT signal (enriched grammar), bounded at the next clause boundary so
    the winner's name (which often follows "... for <winner>") is not swallowed.

    Markdown emphasis (* _ `) is stripped at entry so negations like
    "argues *against*" (answer 156's exact phrasing) match the enriched
    `argues\\s+against` pattern.

    Pure; no model call. Returns [] when the finding has no explicit negation
    (conservative - false negatives are safe; false positives are the bug)."""
    low = (finding or "").lower()
    if not low:
        return []
    low = _strip_emphasis(low)
    m = _REJECT_SIGNALS_RE.search(low)
    if not m:
        return []
    after = low[m.end():].lstrip(" :,;")
    if not after:
        return []
    best = len(after)
    for sep in _REJECTED_CLAUSE_BOUNDS:
        i = after.find(sep)
        if i != -1 and i < best:
            best = i
    return _tokens(after[:best])


def rejected_options(claim: dict) -> list[str]:
    """The options the verifier ruled out. Source order:
      1. `verification.rejected_options` (structured field, populated by the
         verifier, Phase 0 option (a)).
      2. Fallback: `derive_rejected_options(finding)` (legacy answers).

    Normalized to lowercase for stable matching against component alias
    tables."""
    v = _verifications(claim)
    explicit = v.get("rejected_options")
    if isinstance(explicit, list) and explicit:
        return [str(t).strip().lower() for t in explicit if t]
    return derive_rejected_options(_str(v.get("finding")))


def decision_records(verification_result: dict | None) -> list[dict]:
    """Return the canonical decision list - one record per RESOLVED claim,
    exposing the `FIELDS` contract. This is the single entry point every
    downstream consumer MUST use. Never read `resolved_claims` prose directly.
    """
    out: list[dict] = []
    for c in _resolved_claims(verification_result):
        v = _verifications(c)
        out.append({
            "normalized_key": _str(c.get("normalized_key")),
            "dispute": _str(c.get("dispute")),
            "status": _str(v.get("status")),
            "confidence": v.get("confidence"),
            "finding_raw": _str(v.get("finding")),
            "winner_text": winner_text(c),
            "winner_tokens": winner_tokens(c),
            "rejected_options": rejected_options(c),
        })
    return out


if __name__ == "__main__":
    # Self-check using the CANNED answer-156 finding (deterministic, no live
    # workspace needed). This is the smallest thing that fails if the
    # canonical accessor regresses on the SQLite/PostgreSQL contradiction.
    vr_156 = {
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
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }
    recs = decision_records(vr_156)
    assert len(recs) == 1, recs
    rec = recs[0]

    # Invariant 1: the rejected option is recognized as rejected.
    assert "postgresql" in rec["rejected_options"], rec["rejected_options"]

    # Invariant 2: the rejected option does NOT leak into the winner tokens.
    assert "postgresql" not in rec["winner_tokens"], rec["winner_tokens"]

    # Invariant 3: the actual winner is present in the winner tokens.
    assert "sqlite" in rec["winner_tokens"], rec["winner_tokens"]

    # Invariant 4: structured field is preferred over the fallback. A claim
    # carrying `verification.rejected_options` returns it verbatim.
    vr_structured = {
        "resolved_claims": [{
            "normalized_key": "k", "dispute": "X vs Y",
            "verification": {
                "status": "RESOLVED", "confidence": 99,
                "finding": "Resolved in favor of Y; rejects X.",
                "rejected_options": ["ExplicitlyRejected"],
            },
        }],
    }
    rec_s = decision_records(vr_structured)[0]
    assert rec_s["rejected_options"] == ["explicitlyrejected"], rec_s

    # Invariant 5: a unification finding (no rejection) yields no rejected
    # options - the renderer must not invent a rejection.
    vr_unify = {
        "resolved_claims": [{
            "normalized_key": "k", "dispute": "Hybrid split",
            "verification": {
                "status": "RESOLVED", "confidence": 98,
                "finding": ("Resolved through a hybrid split: Edge-Native "
                            "Coordination Plane + Python Execution Layer."),
            },
        }],
    }
    rec_u = decision_records(vr_unify)[0]
    assert rec_u["rejected_options"] == [], rec_u
    # Unification still yields a non-empty winner (the chosen approach).
    assert rec_u["winner_tokens"], rec_u

    # Invariant 6: _winner_raw never falls back to dispute_text/dispute.
    # Answer 156's dispute title contains "PostgreSQL"; if winner_raw fell back
    # to it, winner_tokens would contain "postgresql" - the bug.
    no_finding = {
        "normalized_key": "k",
        "dispute": "Database Technology: GPT selects PostgreSQL, Gemini selects SQLite",
        # no verification.finding, no decision, no reasoning
    }
    assert _winner_raw(no_finding) == "", _winner_raw(no_finding)
    assert winner_text(no_finding) == "", winner_text(no_finding)
    assert "postgresql" not in winner_tokens(no_finding)

    # Pure-stdlib guarantee: importing this module pulls in no LLM/browser and
    # no longer depends on decision_renderer (sole ownership of interpretation).
    import sys as _sys
    forbidden = {"cerebras_client", "ollama_client", "openrouter_client",
                 "groq_client", "gpt_worker", "gemini_worker",
                 "decision_renderer"}
    loaded = forbidden & set(_sys.modules)
    assert not loaded, f"decision_model pulled in forbidden modules: {loaded}"

    print("decision_model Phase 1 smoke test PASSED")
    print(f"  156 winner_tokens={rec['winner_tokens']}")
    print(f"  156 rejected_options={rec['rejected_options']}")
