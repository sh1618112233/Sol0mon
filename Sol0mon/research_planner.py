"""
Research Planner — deterministic pre-debate research planning (V1).

Restores debat-evidence ordering: research is PLANNED and COLLECTED BEFORE the
debate begins, then injected into GPT/Gemini prompts so the debate is grounded
in evidence instead of debating blind and only researching at verification.

Public API:
  plan_research(question) -> {
    "research_required": bool,
    "research_topics":   [topic, ...]          # ordered by appearance
    "research_priority": {topic: HIGH|MEDIUM|LOW}
  }
  collect_research(plan, research_fn=None, prior_evidence=None) -> {
    "evidence":          [{topic, sources, summary, timestamp}, ...],
    "reused_topics":     [topic, ...],
    "researched_topics": [topic, ...],
  }
  format_research_for_prompt(evidence) -> str        # injection block, "" if none
  audit_record(plan, collected) -> dict              # Phase 4 audit
  default_research_fn(topic) -> {"claim", "sources"} # lazy browser, graceful offline

Deterministic: no LLM, no model calls. The only I/O is the (injectable)
research_fn seam; production lazy-imports the existing browser researcher,
tests inject a fake. topic-keyed reuse (prior_evidence) prevents duplicate
research calls on follow-ups.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

MAX_TOPICS = 5
MIN_QUESTION_LEN = 20
MAX_SOURCES_PER_TOPIC = 3
MAX_EXCERPT_CHARS = 500

STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "can", "shall", "must", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "as", "into", "through", "and", "or", "not", "but", "if", "then",
    "else", "when", "up", "down", "out", "about", "this", "that", "these",
    "those", "it", "its", "we", "you", "they", "our", "their", "have", "has",
    "had", "get", "got", "make", "made", "use", "used", "using", "than", "so",
    "what", "which", "who", "how", "why", "where", "there", "here", "now",
})

# Verbs / prompt words that TRIGGER research but are NOT themselves topics.
TRIGGER_WORDS = frozenset({
    "design", "build", "architect", "compare", "versus", "choose", "select",
    "evaluate", "best", "tradeoff", "tradeoffs", "scale", "scaling", "deploy",
    "recommend", "recommendation", "should", "could", "would", "might", "need",
    "needs", "plan", "planning", "implement", "implementation", "stack",
})

# Subject-generic tokens excluded from topics (carry no tech semantics).
GENERIC_TOKENS = frozenset({
    "system", "application", "approach", "options", "option", "kind", "type",
    "layer", "tier", "component", "module", "engine", "manager", "service",
    "services", "model", "models", "strategy", "framework", "library",
    "setup", "config", "configuration", "implementation", "design", "pattern",
    "plan", "solution", "platform", "project", "question", "problem", "issue",
    "scenario", "case", "context", "requirement", "requirements", "feature",
    "features", "thing", "things", "stuff", "something", "everything",
})

# Premise / negation words — not research topics (e.g. "Assume PostgreSQL
# cannot be used" should research "postgresql", not "assume"/"cannot").
PREMISE_NEGATION = frozenset({
    "assume", "assuming", "suppose", "supposing", "given", "lets", "let",
    "cannot", "cant", "wont", "dont", "doesnt", "isnt", "arent", "wasnt",
    "werent", "not", "never", "without", "no", "nor", "neither", "either",
    "already", "now", "still", "must", "have", "having",
})

# Tech/domains that map a topic to a priority band (substring match).
TECH_DOMAINS = {
    "HIGH": ["database", "storage", "consistency", "security", "deployment",
             "orchestration", "scaling", "failover", "disaster", "data",
             "infrastructure", "networking", "kafka", "postgres", "redis",
             "kubernetes", "replication", "sharding"],
    "MEDIUM": ["cache", "queue", "messaging", "api", "workflow", "pipeline",
               "observability", "monitoring", "rabbitmq", "memcached"],
    "LOW": ["library", "tooling", "naming", "formatting", "language", "sdk",
            "linting"],
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _is_topic_token(t: str) -> bool:
    return (len(t) >= 4
            and t not in STOP_WORDS
            and t not in TRIGGER_WORDS
            and t not in GENERIC_TOKENS
            and t not in PREMISE_NEGATION)


def _ordered_unique(seq) -> list[str]:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _topic_priority(topic: str) -> str:
    t = topic.lower()
    for band, domains in TECH_DOMAINS.items():
        for d in domains:
            if d in t or t in d:
                return band
    return "LOW"


def _question_subjects(question: str) -> list[str]:
    """Distinctive topic tokens in order of first appearance."""
    tokens = _tokenize(question)
    # Join alnum runs so "X vs Y" surfaces both X and Y as separate tokens
    # (the splitter already splits on non-alnum, so "redis vs memcached" ->
    # ["redis","vs","memcached"]; "vs" is filtered as a trigger).
    candidates = [t for t in tokens if _is_topic_token(t)]
    return _ordered_unique(candidates)[:MAX_TOPICS]


def plan_research(question: str) -> dict:
    """Deterministically decide whether and what to research for a question.

    research_required is True when the question is long enough AND mentions a
    research trigger (design / compare / vs / choose / ...) or a known tech
    domain token. Trivially short or non-technical questions need no research.
    """
    q = (question or "").strip()
    q_lower = q.lower()
    tokens = set(_tokenize(q_lower))

    if len(q) < MIN_QUESTION_LEN:
        return {"research_required": False,
                "research_topics": [], "research_priority": {}}

    triggered = bool(tokens & TRIGGER_WORDS)
    domain_hit = any(d in q_lower for band in TECH_DOMAINS.values()
                     for d in band)
    if not (triggered or domain_hit):
        return {"research_required": False,
                "research_topics": [], "research_priority": {}}

    topics = _question_subjects(q)
    if not topics:
        return {"research_required": False,
                "research_topics": [], "research_priority": {}}

    priority = {t: _topic_priority(t) for t in topics}
    return {"research_required": True,
            "research_topics": topics, "research_priority": priority}


def default_research_fn(topic: str) -> dict:
    """Default research seam: lazy-imports the existing browser-based
    verification_research_v2.research_claim so the engine never imports the
    browser stack at module load. Gracefully returns empty sources when the
    brows-er is unavailable (e.g. offline test env) instead of crashing."""
    try:
        from verification_research_v2 import research_claim
        return research_claim(topic)
    except Exception as e:
        print(f"[PRE-DEBATE RESEARCH] default research unavailable for "
              f"'{topic}': {e}")
        return {"claim": topic, "sources": []}


def _normalize_research_output(output, topic: str):
    """Accept the existing research shapes (`{"claim","sources":[...]}` from
    verification_research_v2, or a markdown string) and return (summary,
    sources). sources is a list of dicts (url/title/score/content)."""
    if output is None:
        return f"Research on {topic}", []
    if isinstance(output, dict):
        sources = output.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        summary = (output.get("summary") or output.get("claim")
                   or f"Research on {topic}")
        return str(summary), sources
    return str(output), []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_research(plan: dict, research_fn=None,
                     prior_evidence: list | None = None) -> dict:
    """Collect pre-debate evidence for a plan, reusing prior topic-keyed
    evidence. Returns evidence + which topics were reused vs researched.

    No duplicate calls: a topic present in prior_evidence is returned from
    the cache and NEVER passed to research_fn.
    """
    if not plan or not plan.get("research_required"):
        return {"evidence": [], "reused_topics": [], "researched_topics": []}

    prior_by_topic: dict[str, dict] = {}
    for ev in (prior_evidence or []):
        if not isinstance(ev, dict):
            continue
        k = (ev.get("topic") or "").lower()
        if k:
            prior_by_topic[k] = ev

    resolver = research_fn if research_fn is not None else default_research_fn

    evidence: list[dict] = []
    reused: list[str] = []
    researched: list[str] = []

    for topic in plan.get("research_topics", []):
        key = (topic or "").lower()
        if not key:
            continue
        if key in prior_by_topic:
            rec = dict(prior_by_topic[key])
            rec.setdefault("topic", topic)
            evidence.append(rec)
            reused.append(topic)
            print(f"[PRE-DEBATE RESEARCH] REUSED '{topic}' (prior evidence)")
            continue
        try:
            output = resolver(topic)
        except Exception as e:
            output = {"claim": topic, "sources": []}
            print(f"[PRE-DEBATE RESEARCH] '{topic}' failed: {e}")
        summary, sources = _normalize_research_output(output, topic)
        evidence.append({
            "topic": topic,
            "sources": sources or [],
            "summary": summary,
            "timestamp": _now_iso(),
        })
        researched.append(topic)
        print(f"[PRE-DEBATE RESEARCH] RESEARCHED '{topic}' "
              f"({len(sources or [])} sources)")

    return {"evidence": evidence,
            "reused_topics": reused,
            "researched_topics": researched}


def format_research_for_prompt(evidence: list | None) -> str:
    """Build the <external_research> injection block for debate prompts.

    Bounded: MAX_SOURCES_PER_TOPIC per topic, MAX_EXCERPT_CHARS per excerpt.
    Returns "" when there is no usable evidence — callers skip injection.
    """
    if not evidence:
        return ""
    lines = ["<external_research>"]
    any_content = False
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        topic = ev.get("topic", "")
        sources = ev.get("sources") or []
        summary = ev.get("summary", "") or ""
        if not sources and not summary:
            continue
        any_content = True
        lines.append(f"Topic: {topic}")
        if summary and summary != f"Research on {topic}":
            lines.append(f"  Summary: {summary[:MAX_EXCERPT_CHARS]}")
        for s in sources[:MAX_SOURCES_PER_TOPIC]:
            if not isinstance(s, dict):
                continue
            url = s.get("url", "")
            title = s.get("title", "")
            content = (s.get("content") or "")[:MAX_EXCERPT_CHARS]
            line = f"  Source: {url}"
            if title:
                line += f"  ({title})"
            if content:
                line += f"\n  Excerpt: {content}"
            lines.append(line)
    if not any_content:
        return ""
    lines.append("</external_research>")
    lines.append("Ground your reasoning in the evidence above where relevant. "
                 "Cite URLs when you rely on a source.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Research V2 — research context object + per-dispute relevance (Phase 1-4)
# Pure deterministic transforms over the existing evidence shape
# ({topic, sources, summary, timestamp}). No model calls, no I/O.
# ---------------------------------------------------------------------------

def build_research_context(evidence: list | None) -> dict:
    """Phase 1: workspace research evidence -> reusable research context object.

    Pure deterministic transform. No model calls. Returns::

        {
          "evidence_by_topic": {topic: ev_dict, ...},
          "source_counts":     {topic: int, ...},
          "citations":         {topic: [{url, title}, ...], ...},
        }

    Topic keys are case-preserved from the evidence records; lookups are
    case-insensitive at the selector. Empty/none evidence yields empty maps.
    """
    evidence_by_topic: dict[str, dict] = {}
    source_counts: dict[str, int] = {}
    citations: dict[str, list] = {}
    for ev in (evidence or []):
        if not isinstance(ev, dict):
            continue
        topic = (ev.get("topic") or "").strip()
        if not topic:
            continue
        sources = ev.get("sources") or []
        if not isinstance(sources, list):
            sources = []
        evidence_by_topic[topic] = ev
        source_counts[topic] = len(sources)
        citations[topic] = [
            {"url": s.get("url", ""), "title": s.get("title", "")}
            for s in sources if isinstance(s, dict)
        ]
    return {
        "evidence_by_topic": evidence_by_topic,
        "source_counts": source_counts,
        "citations": citations,
    }


def select_relevant_evidence(evidence: list | None, dispute_text: str,
                             normalized_key: str = "") -> list:
    """Return only the evidence topics relevant to ONE dispute.

    Relevance (deterministic, keyword-based, no model):
      1. the topic string (case-insensitive) is a substring of the dispute
         text / normalized_key, OR
      2. any non-stopword token (len>=4) of the topic appears in the haystack.
    A database dispute therefore gets database evidence; an execution dispute
    gets execution evidence. The full evidence set is NOT dumped into every
    dispute. Pure; no I/O; no model calls.
    """
    haystack = f"{dispute_text or ''} {normalized_key or ''}".lower()
    if not haystack.strip():
        return []
    relevant: list = []
    for ev in (evidence or []):
        if not isinstance(ev, dict):
            continue
        topic = (ev.get("topic") or "").strip()
        if not topic:
            continue
        tlow = topic.lower()
        if tlow and tlow in haystack:
            relevant.append(ev)
            continue
        toks = [t for t in _tokenize(tlow)
                if t not in STOP_WORDS and len(t) >= 4]
        if any(t in haystack for t in toks):
            relevant.append(ev)
    return relevant


def format_research_for_dispute(evidence: list | None, dispute_text: str,
                                normalized_key: str = "") -> str:
    """Phase 2/3: build a relevant-evidence injection block for ONE dispute.

    Filters ``evidence`` to only topics relevant to ``dispute_text`` /
    ``normalized_key`` and renders them with the existing
    ``format_research_for_prompt`` renderer. Returns "" when nothing relevant
    is present — callers then skip injection, leaving prompts unchanged.
    Pure deterministic; no model calls, no browser calls.
    """
    return format_research_for_prompt(
        select_relevant_evidence(evidence, dispute_text, normalized_key)
    )


def build_consistency_audit_context(research_evidence: list | None) -> str:
    """Phase 4: build a research-evidence block to append to a cross-dispute
    consistency-audit prompt so the auditor sees the evidence used during
    debate. Reuses ``format_research_for_prompt`` over the full evidence set
    (the audit compares resolved decisions across topics, so topic filtering
    is not applied here). Pure deterministic; no model calls.
    """
    return format_research_for_prompt(research_evidence)


def merge_evidence(prior: list | None, new: list | None) -> list:
    """Dedupe evidence by topic (case-insensitive); new overrides prior for the
    same topic. Different topics accumulate across follow-ups."""
    by_topic: dict[str, dict] = {}
    for ev in (prior or []):
        if isinstance(ev, dict):
            k = (ev.get("topic") or "").lower()
            if k:
                by_topic[k] = ev
    for ev in (new or []):
        if isinstance(ev, dict):
            k = (ev.get("topic") or "").lower()
            if k:
                by_topic[k] = ev
    return list(by_topic.values())


def audit_record(plan: dict, collected: dict) -> dict:
    """Audit record persisted to ``ws.research_audit_trail`` (Phase 4 + V2).

    V2 adds the three research-usage fields demanded by the audit-trail spec:
    ``research_used`` (was any evidence collected this run), ``research_topics_used``
    (topics with evidence collected this run — eligible for injection into
    dispute / retry / consistency-audit prompts), and ``research_reuse_count``
    (how many topics were served from prior evidence instead of re-researched).
    All deterministic, derived from ``collected``.
    """
    evidence = collected.get("evidence", []) if collected else []
    reused_topics = (collected or {}).get("reused_topics", [])
    research_topics_used = [
        ev.get("topic", "") for ev in evidence
        if isinstance(ev, dict) and ev.get("topic")
    ]
    research_sources = {}
    for ev in evidence:
        if isinstance(ev, dict):
            research_sources[ev.get("topic", "")] = len(
                ev.get("sources") or [])
    return {
        "research_plan": plan or {},
        "research_topics": (plan or {}).get("research_topics", []),
        "research_sources": research_sources,
        "research_reuse": {
            "reused_topics": reused_topics,
            "researched_topics": (collected or {}).get("researched_topics", []),
        },
        "research_used": bool(research_topics_used),
        "research_topics_used": research_topics_used,
        "research_reuse_count": len(reused_topics),
        "timestamp": _now_iso(),
    }


if __name__ == "__main__":
    plan = plan_research(
        "Design a planet-scale consensus platform using PostgreSQL and Redis")
    assert plan["research_required"] is True, plan
    assert "postgresql" in plan["research_topics"], plan
    assert "redis" in plan["research_topics"], plan
    assert set(plan["research_priority"].keys()) == set(plan["research_topics"])

    short = plan_research("hi")
    assert short["research_required"] is False, short

    # collect with a fake fn + prior reuse
    calls = []
    def fake(topic):
        calls.append(topic)
        return {"claim": topic, "sources": [{"url": f"http://x/{topic}",
                "title": topic, "content": "evidence"}]}
    c = collect_research(plan, fake, prior_evidence=None)
    assert set(c["researched_topics"]) == set(plan["research_topics"])
    assert c["reused_topics"] == []
    # second run reuses all -> no new calls
    c2 = collect_research(plan, fake, prior_evidence=c["evidence"])
    assert c2["researched_topics"] == []
    assert c2["reused_topics"] == plan["research_topics"]
    assert len(calls) == len(plan["research_topics"]), "duplicate research!"
    block = format_research_for_prompt(c["evidence"])
    assert "<external_research>" in block and "postgresql" in block.lower()
    print("research_planner self-check PASSED")
    print(f"  topics={plan['research_topics']} priority={plan['research_priority']}")
