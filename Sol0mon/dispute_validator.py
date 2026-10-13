import json

from cerebras_client import generate


def extract_json(text):
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start_idx = text.find("{")

    if start_idx != -1:
        bracket_count = 0

        for i in range(start_idx, len(text)):
            if text[i] == "{":
                bracket_count += 1

            elif text[i] == "}":
                bracket_count -= 1

                if bracket_count == 0:
                    try:
                        return json.loads(
                            text[start_idx:i + 1]
                        )
                    except Exception:
                        break

    return None


def validate_disputes(
    question,
    gpt_position,
    gemini_position,
    disputes
):
    """
    Determines whether each extracted dispute
    is a real architectural disagreement
    or merely wording / abstraction differences.
    """

    if not disputes:
        return []

    validated_disputes = []

    for dispute in disputes:

        prompt = f"""
You are a dispute validation engine.

Your task:

Determine whether the dispute below
represents a REAL disagreement that changes
architecture, implementation, operations,
security, scalability, infrastructure,
or final recommendations.

IMPORTANT

Ignore:

- wording differences
- terminology differences
- abstraction differences
- naming differences
- formatting differences

Only return VALID if the disagreement
would cause a different system to be built.

Return ONLY valid JSON:

{{
    "classification": "VALID|EQUIVALENT"
}}

QUESTION

{question}

DISPUTE

{dispute}

GPT POSITION

{gpt_position}

GEMINI POSITION

{gemini_position}
"""

        try:

            raw = generate(
                prompt,
                module="dispute_validator"
            )

            result = extract_json(raw)

            if (
                result
                and result.get("classification")
                == "VALID"
            ):
                validated_disputes.append(dispute)

        except Exception as e:

            print(
                f"[DISPUTE VALIDATOR ERROR] {e}"
            )

            validated_disputes.append(dispute)

    print(
        f"\n[DISPUTE_VALIDATOR] "
        f"Input={len(disputes)} "
        f"Validated={len(validated_disputes)} "
        f"Ignored={len(disputes) - len(validated_disputes)}"
    )

    return validated_disputes


if __name__ == "__main__":

    disputes = [
        "Temporal vs Airflow workflow engine",
        "Byzantine-inspired filtering terminology"
    ]

    validated = validate_disputes(
        question="Design an AI platform",
        gpt_position="Use Temporal",
        gemini_position="Use Airflow",
        disputes=disputes
    )

    print(
        json.dumps(
            validated,
            indent=4
        )
    )
