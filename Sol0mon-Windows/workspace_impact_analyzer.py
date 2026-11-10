"""
Workspace Impact Analyzer — deterministic follow-up impact analysis.

Determines which prior disputes/decisions a follow-up question AFFECTS (must
reopen) and which it preserves, plus an audit record. Deterministic: no LLM,
no model calls, no network — pure keyword + dependency-graph analysis over
normalized_keys and decision text already persisted in the workspace.

Follow-up classification:
  A. Clarification — asks for explanation; reopens nothing.
  B. Extension     — adds scope; reopens only affected (direct + dependent).
  C. Contradiction — negates a prior assumption; reopens only affected.

Public API:
  analyze_followup_impact(workspace_state, follow_up_question) -> dict with
    {affected_disputes, unaffected_disputes, reopen_required, follow_up_type, reasoning}

`workspace_state` accepts either a Workspace object (attribute access) or a
plain dict, so the analyzer is unit-testable offline without SQLite/models.

# ponytail: dependency edges come from normalized_key subject tokens matched
# against other decisions' text (word boundary). A real "depends-on" relation
# would come from the debate graph; this heuristic covers the common case.
# Upgrade path: track an explicit depends_on list on Dispute at debate time.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "can", "shall", "must", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "as", "into", "through", "and", "or", "not", "but", "if", "then",
    "else", "when", "up", "down", "out", "about", "this", "that", "these",
    "those", "it", "its", "we", "you", "they", "our", "their", "have", "has",
    "had", "get", "got", "make", "made", "use", "used", "using", "than", "so",
})

# Key parts that carry no subject semantics — never anchor a dependency edge.
GENERIC_KEY_TOKENS = frozenset({
    "choice", "model", "strategy", "routing", "approach", "option", "options",
    "pattern", "plan", "layer", "tier", "type", "kind", "selection",
    "decision", "decisions", "framework", "library", "provider", "service",
    "setup", "config", "configuration", "manager", "engine", "module",
    "system", "component", "implementation", "design", "approach",
})

CONTRADICTION_SIGNALS = (
    "cannot", "can't", "no longer", "not available", "unavailable",
    "instead of", "replace with", "switch from", "abandon", "deprecate",
    "dropped", "wrong", "incorrect", "invalidate", "contradict",
    "reconsider", "is not an option", "off the table", "not possible",
    "not feasible", "exclude", "cannot use",
)

CLARIFICATION_SIGNALS = (
    "explain", "clarify", "elaborate", "more detail", "what do you mean",
    "can you explain", "expand on", "tell me more", "walk me through",
    "why did you choose", "why did you pick", "summarize", "recap", "restate",
    "what is the reasoning", "what is the rationale",
)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _distinctive(tokens: list[str]) -> set[str]:
    """Tokens worth matching: len>=3, non-stop, non-generic."""
    return {t for t in tokens if len(t) >= 3
            and t not in STOP_WORDS and t not in GENERIC_KEY_TOKENS}


def _key_subject(normalized_key: str) -> list[str]:
    """Subject tokens of a normalized_key: distinctive non-generic parts of
    length>=4. Empty if none qualify (then the key anchors no dependency)."""
    parts = re.split(r"[_\s\-/.]+", (normalized_key or "").lower())
    return [p for p in parts if len(p) >= 4 and p not in GENERIC_KEY_TOKENS]


def _get(state, key, default=None):
    """Read attribute or dict key — accepts Workspace or plain dict."""
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _classify(follow_up_lower: str) -> str:
    for sig in CONTRADICTION_SIGNALS:
        if sig in follow_up_lower:
            return "contradiction"
    for sig in CLARIFICATION_SIGNALS:
        if sig in follow_up_lower:
            return "clarification"
    return "extension"


def _gather_disputes(workspace_state) -> list[dict]:
    """Collect a unified {normalized_key -> text} map from all dispute-bearing
    workspace fields: verified_decisions, verified_tradeoffs, resolved_disputes,
    human_required_items. Text is concatenated so nothing is lost."""
    out: dict[str, str] = {}

    def _add(key, text):
        k = (key or "").strip()
        if not k:
            return
        out[k] = (out[k] + " " + (text or "")) if k in out else (text or "")

    for d in (_get(workspace_state, "verified_decisions", []) or []):
        _add(d.get("normalized_key"),
             f"{d.get('decision','')} {d.get('agreed_position') or ''}")
    for t in (_get(workspace_state, "verified_tradeoffs", []) or []):
        _add(t.get("dispute_key") or t.get("normalized_key"),
             " ".join([t.get("option_a", ""), t.get("option_b", ""),
                       t.get("recommendation", ""), t.get("reasoning", "")]))
    for r in (_get(workspace_state, "resolved_disputes", []) or []):
        _add(r.get("normalized_key"),
             f"{r.get('dispute','')} {r.get('decision') or ''} "
             f"{r.get('agreed_position') or ''}")
    for h in (_get(workspace_state, "human_required_items", []) or []):
        affected = h.get("affected_disputes") or []
        key = affected[0] if affected else h.get("normalized_key")
        _add(key, f"{h.get('dispute_text','')} {h.get('question') or ''} "
                  f"{h.get('reasoning') or ''}")
    return [{"normalized_key": k, "text": v} for k, v in out.items()]


def _direct_hits(follow_up: str, disputes: list[dict]) -> set[str]:
    """Disputes whose signature overlaps the follow-up. >=2 distinctive
    tokens is a hit; a single token of length>=5 (e.g. a tech name like
    'postgresql') also counts — the existing >=2 rule would miss proper nouns
    that appear once."""
    fw_tokens = _distinctive(_tokenize(follow_up))
    if not fw_tokens:
        return set()
    hits = set()
    for d in disputes:
        sig = _distinctive(_tokenize(f"{d['normalized_key']} {d['text']}"))
        overlap = sig & fw_tokens
        if not overlap:
            continue
        if len(overlap) >= 2 or any(len(t) >= 5 for t in overlap):
            hits.add(d["normalized_key"])
    return hits


def _build_dependencies(disputes: list[dict]) -> dict[str, set[str]]:
    """dependents[A] = {B : B references A's subject token, i.e. B depends on
    A}. B depends on A => when A reopens, B must reopen too."""
    dependents: dict[str, set[str]] = {d["normalized_key"]: set()
                                       for d in disputes}
    for a in disputes:
        a_key = a["normalized_key"]
        a_subs = _key_subject(a_key)
        if not a_subs:
            continue
        for b in disputes:
            b_key = b["normalized_key"]
            if b_key == a_key:
                continue
            b_lower = f"{b_key} {b['text']}".lower()
            for sub in a_subs:
                if re.search(r"\b" + re.escape(sub) + r"\b", b_lower):
                    dependents[a_key].add(b_key)
                    break
    return dependents


def _propagate(direct: set[str],
               dependents: dict[str, set[str]]) -> set[str]:
    affected = set(direct)
    queue = list(direct)
    while queue:
        a = queue.pop()
        for b in dependents.get(a, ()):
            if b not in affected:
                affected.add(b)
                queue.append(b)
    return affected


def _reasoning(ftype: str, direct: set[str], affected: set[str],
               dependents: dict[str, set[str]]) -> str:
    deps = {k: sorted(dependents.get(k, ())) for k in affected
            if dependents.get(k)}
    n_dep = len(affected - direct)
    return (f"{ftype}: {len(direct)} direct, {n_dep} dependent; "
            f"affected={sorted(affected)}, dependents={deps}")


def analyze_followup_impact(workspace_state,
                            follow_up_question: str) -> dict:
    """Deterministically determine which prior disputes a follow-up affects.

    Returns:
        {
          "affected_disputes":   [normalized_key, ...],   # reopen these
          "unaffected_disputes": [normalized_key, ...],   # preserve these
          "reopen_required":     bool,
          "follow_up_type":      "clarification"|"extension"|"contradiction",
          "reasoning":           str,
        }
    """
    follow_up = follow_up_question or ""
    fu_lower = follow_up.lower()
    disputes = _gather_disputes(workspace_state)
    all_keys = [d["normalized_key"] for d in disputes]
    ftype = _classify(fu_lower)

    if ftype == "clarification":
        return {
            "affected_disputes": [],
            "unaffected_disputes": list(all_keys),
            "reopen_required": False,
            "follow_up_type": "clarification",
            "reasoning": "clarification: asks for explanation; nothing reopened",
        }

    direct = _direct_hits(follow_up, disputes)
    dependents = _build_dependencies(disputes)
    affected = _propagate(direct, dependents)
    unaffected = [k for k in all_keys if k not in affected]

    return {
        "affected_disputes": sorted(affected),
        "unaffected_disputes": unaffected,
        "reopen_required": bool(affected),
        "follow_up_type": ftype,
        "reasoning": _reasoning(ftype, direct, affected, dependents),
    }


def audit_entry(impact: dict, follow_up: str) -> dict:
    """Build a workspace-persistable audit record from an impact result."""
    return {
        "follow_up_type": impact.get("follow_up_type"),
        "affected_dispute_count": len(impact.get("affected_disputes", [])),
        "reopened_disputes": list(impact.get("affected_disputes", [])),
        "preserved_disputes": list(impact.get("unaffected_disputes", [])),
        "reasoning": impact.get("reasoning", ""),
        "follow_up": (follow_up or "")[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    # Critical self-check: 4 decisions, PostgreSQL contradiction.
    ws = {
        "verified_decisions": [
            {"normalized_key": "database_choice",
             "decision": "Use PostgreSQL as the primary database",
             "agreed_position": "PostgreSQL gives us ACID transactions"},
            {"normalized_key": "provider_routing",
             "decision": "Route requests via an API gateway to model providers"},
            {"normalized_key": "deployment_model",
             "decision": "Deploy on Kubernetes with horizontal pod autoscaling"},
            {"normalized_key": "storage_strategy",
             "decision": "Use the database native replication for the storage tier",
             "agreed_position": "Rely on the database's WAL replication"},
        ],
        "verified_tradeoffs": [],
        "resolved_disputes": [],
        "human_required_items": [],
    }
    impact = analyze_followup_impact(ws, "Assume PostgreSQL cannot be used.")
    assert impact["follow_up_type"] == "contradiction", impact["follow_up_type"]
    assert impact["affected_disputes"] == ["database_choice", "storage_strategy"], \
        impact["affected_disputes"]
    assert set(impact["unaffected_disputes"]) == {"deployment_model",
                                                   "provider_routing"}, \
        impact["unaffected_disputes"]
    assert impact["reopen_required"] is True
    assert len(impact["affected_disputes"]) == 2, "queue size must be 2"
    print("workspace_impact_analyzer: critical self-check PASSED")
    print(f"  {impact['reasoning']}")
