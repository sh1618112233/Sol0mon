"""
Project Sol0mon - Phase 3: Answer Resurrection - answer_selector.

Turns a user "build spec" into a concrete answer:

    "build answer 54"                  -> direct ID lookup
    "54"                               -> direct ID lookup
    "build latest statarb bot"          -> latest answer whose topic matches
    "build latest Sol0mon architecture"-> latest answer whose topic matches
    "latest"                           -> newest answer overall

identify_topic(): normalize text to a stopword-stripped token string.
topic_matches():  true when every topic token appears in the candidate text.
select_answer():  dispatch a user spec to answer_loader; returns the canonical
                  {answer_id, question, topic, synthesized_answer} dict or None.

ponytail: token-subset match, no synonyms/stemming. Add fuzzy match if the
topic vocabulary drifts (e.g. "statarb" <-> "statistical arbitrage").
"""

from __future__ import annotations

import re

# Command filler + common English/articles stripped so the actual topic
# tokens survive. Kept small on purpose.
STOPWORDS = {
    "build", "latest", "answer", "the", "a", "an", "of", "for", "to",
    "and", "or", "please", "me", "design", "create", "implement", "show",
    "get", "find", "use", "from", "about",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def identify_topic(text: str) -> str:
    """Lowercase, tokenize, drop stopwords. Returns a space-joined token
    string. 'build latest statarb bot' -> 'statarb bot'."""
    if not text:
        return ""
    toks = _TOKEN_RE.findall(text.lower())
    return " ".join(t for t in toks if t not in STOPWORDS)


def topic_matches(topic: str, text: str) -> bool:
    """True when every token of `topic` appears in `text` (case-insensitive).
    Empty topic means 'any' (matches everything)."""
    if not topic:
        return True
    haystack = text.lower()
    return all(tok in haystack for tok in topic.split())


def select_answer(spec: str) -> dict | None:
    """Dispatch a user spec to one canonical answer dict, or None if no match.

    Order of resolution:
    1. Explicit ID   - 'answer 54', 'build answer 54', or a bare number.
    2. Latest + topic - 'latest <topic>'; newest answer whose question
                        contains all topic tokens.
    3. Bare 'latest'  - the newest answer overall.
    """
    if not spec:
        return None
    # Lazy import avoids an import cycle: answer_loader imports the pure
    # helpers above at module load.
    import answer_loader as al

    s = spec.strip().lower()

    # 1) Explicit ID: 'answer 54', 'build answer 54', or just '54'.
    m = re.search(r"\banswer\s+(\d+)\b", s) or re.match(r"^\s*(\d+)\s*$", s)
    if m:
        return al.get_answer_by_id(int(m.group(1)))

    # 2/3) Latest [topic].
    topic = identify_topic(spec)
    return al.get_latest_answer(topic=topic or None)


if __name__ == "__main__":
    # Smoke self-check (no DB needed - just pure helpers + dispatch shape)
    assert identify_topic("build latest statarb bot") == "statarb bot"
    assert identify_topic("build latest Sol0mon architecture") == "sol0mon architecture"
    assert identify_topic("latest") == ""
    assert topic_matches("statarb bot", "Design a stat arb bot for crypto") is False
    assert topic_matches("sol0mon architecture", "Design Sol0mon architecture v2") is True
    assert topic_matches("", "anything") is True
    assert select_answer("") is None
    print("answer_selector smoke test PASSED")
