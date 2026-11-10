import re
import json

MIN_SENTENCE_LENGTH = 20
MAX_EVIDENCE_SENTENCES = 10

SYNONYM_MAP = {
    "created": "creator",
    "creator": "creator",
    "invented": "inventor",
    "inventor": "inventor",
    "founded": "founder",
    "founder": "founder",
    "developed": "developer",
    "developer": "developer",
    "released": "release",
    "release": "release",
    "database": "db",
    "db": "db",
    "asynchronous": "async",
    "async": "async"
}


def normalize(text):
    if not text:
        return ""

    text = text.lower()
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def split_sentences(text):
    """
    Technical-safe sentence splitter.

    Protects common version numbers such as:

        v1.0
        v2.5
        v3.11

    before sentence segmentation.
    """

    text = normalize(text)

    text = re.sub(
        r'v(\d+)\.(\d+)',
        r'v\1_\2',
        text
    )

    raw_sentences = re.split(
        r'(?<!\beg)(?<!\bie)(?<=[.!?])\s+',
        text
    )

    cleaned = []

    for sentence in raw_sentences:

        sentence = sentence.replace(
            "_",
            "."
        ).strip()

        if len(sentence) >= MIN_SENTENCE_LENGTH:
            cleaned.append(sentence)

    return cleaned


def tokenize(text):
    words = re.findall(
        r"\b[a-z0-9]+\b",
        text.lower()
    )

    normalized = []

    for word in words:
        normalized.append(
            SYNONYM_MAP.get(
                word,
                word
            )
        )

    return set(normalized)


def sentence_score(
    claim,
    sentence,
    source_score=0
):
    claim_tokens = tokenize(claim)
    sentence_tokens = tokenize(sentence)

    overlap = len(
        claim_tokens.intersection(
            sentence_tokens
        )
    )

    if overlap == 0:
        return 0

    score = overlap * 10

    #
    # Fix:
    # Reduce domain score influence.
    #
    score += (source_score / 20)

    #
    # Fix:
    # Exact phrase match bonus.
    #
    if claim.lower() in sentence.lower():
        score += 50

    #
    # Noise floor.
    #
    if len(claim_tokens) > 0:
        overlap_ratio = overlap / len(claim_tokens)

        if overlap_ratio < 0.10:
            return 0

    return score


def extract_evidence(
    claim,
    content,
    source_score=0
):
    #
    # Fix:
    # Protect against extremely large pages.
    #
    content = content[:50000]

    sentences = split_sentences(content)

    ranked = []

    for sentence in sentences:

        score = sentence_score(
            claim,
            sentence,
            source_score
        )

        if score > 0:
            ranked.append(
                {
                    "sentence": sentence,
                    "score": round(score, 2)
                }
            )

    ranked.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return ranked[:MAX_EVIDENCE_SENTENCES]


def extract_from_sources(
    claim,
    sources
):
    results = []

    for source in sources:

        evidence = extract_evidence(
            claim,
            source.get("content", ""),
            source.get("score", 0)
        )

        if evidence:

            results.append(
                {
                    "url": source.get("url", ""),
                    "title": source.get("title", ""),
                    "source_score": source.get("score", 0),
                    "evidence": evidence
                }
            )

    results.sort(
        key=lambda x: x["source_score"],
        reverse=True
    )

    return results


if __name__ == "__main__":

    claim = "Python was created by Guido van Rossum"

    sample_sources = [
        {
            "url": "https://example.com",
            "title": "Example",
            "score": 90,
            "content": """
            Guido van Rossum is a Dutch programmer.
            He is the creator of the Python programming language v3.11.
            He worked at Google.
            Python was first released in 1991.
            """
        }
    ]

    result = extract_from_sources(
        claim,
        sample_sources
    )

    print(
        json.dumps(
            result,
            indent=4
        )
    )
