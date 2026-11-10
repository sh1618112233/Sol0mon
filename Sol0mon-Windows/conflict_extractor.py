import json
import hashlib

from cerebras_client import generate


def extract_json(text):
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    obj_start = text.find("{")
    arr_start = text.find("[")

    if obj_start == -1 and arr_start == -1:
        return None

    if (obj_start != -1 and obj_start < arr_start) or arr_start == -1:
        start_idx = obj_start
        open_char = "{"
        close_char = "}"
    else:
        start_idx = arr_start
        open_char = "["
        close_char = "]"

    depth = 0

    for i in range(start_idx, len(text)):
        char = text[i]

        if char == open_char:
            depth += 1

        elif char == close_char:
            depth -= 1

            if depth == 0:
                try:
                    return json.loads(text[start_idx:i + 1])
                except Exception:
                    break

    return None


def normalize_string_list(values):
    if not isinstance(values, list):
        return []

    cleaned = []

    for item in values:
        if item is None:
            continue

        value = str(item).strip()

        if not value:
            continue

        cleaned.append(value)

    return list(dict.fromkeys(cleaned))


def extract_conflicts(
    gpt_final_position,
    gemini_final_position
):
    if not gpt_final_position or not str(gpt_final_position).strip():
        gpt_final_position = "No GPT position provided."

    if not gemini_final_position or not str(gemini_final_position).strip():
        gemini_final_position = "No Gemini position provided."

    # H11: Removed position truncation. Full positions are now sent to Cerebras.
    # If cost/latency control is needed, the caller should handle it with
    # sectioned extraction (system overview, storage, security, deployment separately).
    gpt_hash = hashlib.sha256(
        gpt_final_position.encode("utf-8")
    ).hexdigest()[:8]

    gemini_hash = hashlib.sha256(
        gemini_final_position.encode("utf-8")
    ).hexdigest()[:8]

    print(
        f"\n[CONFLICT_EXTRACTOR] "
        f"GPT={gpt_hash} "
        f"GEMINI={gemini_hash} "
        f"GPT_len={len(gpt_final_position)} "
        f"GEMINI_len={len(gemini_final_position)}"
    )

    extractor_prompt = f"""
You are a highly rigid Architectural Conflict Extraction Engine.

Your task is to identify all areas of agreement,
unresolved disputes,
and resolved items between the two positions.

CRITICAL DIRECTIVES

1. Focus heavily on architectural, infrastructure,
database, security, workflow, orchestration,
deployment, scaling, observability,
storage, networking, and implementation selections.

2. Treat concrete technology choices as conflicts
when different technologies are selected.

Examples:

- Temporal vs Airflow
- Kafka vs NATS
- PostgreSQL vs DynamoDB
- Kubernetes vs Nomad

3. Extract concrete implementation decisions,
framework choices,
execution protocols,
deployment strategies,
and scaling decisions.

4. Ignore:
- wording
- formatting
- style
- document structure

Return VALID JSON ONLY.

Schema:

{{
    "severity": "LOW|MEDIUM|HIGH",
    "agreed_claims": [],
    "disputed_claims": [],
    "resolved_disputes": []
}}

GPT FINAL POSITION

==================================================
{gpt_final_position}
==================================================

GEMINI FINAL POSITION

==================================================
{gemini_final_position}
==================================================

Return JSON only.
"""

    try:
        raw_output = generate(
            extractor_prompt,
            module="conflict_extractor"
        )

        result = extract_json(raw_output)

    except Exception as e:
        print(
            f"\n[CONFLICT_EXTRACTOR ERROR] "
            f"{str(e)}"
        )

        result = None

    if not isinstance(result, dict):
        return {
            "severity": "HIGH",
            "error": "conflict_extraction_failed",
            "agreed_claims": [],
            "disputed_claims": [
                "Conflict extraction system processing failed."
            ],
            "resolved_disputes": []
        }

    agreed_claims = normalize_string_list(
        result.get("agreed_claims", [])
    )

    disputed_claims = normalize_string_list(
        result.get("disputed_claims", [])
    )

    resolved_disputes = normalize_string_list(
        result.get("resolved_disputes", [])
    )

    agreed_claims = agreed_claims[:50]
    disputed_claims = disputed_claims[:30]
    resolved_disputes = resolved_disputes[:30]

    model_severity = str(
        result.get("severity", "LOW")
    ).upper()

    if model_severity not in (
        "LOW",
        "MEDIUM",
        "HIGH"
    ):
        model_severity = "LOW"

    dispute_count = len(disputed_claims)

    if model_severity == "HIGH":
        severity = "HIGH"

    elif dispute_count >= 8:
        severity = "HIGH"

    elif model_severity == "MEDIUM":
        severity = "MEDIUM"

    elif dispute_count >= 3:
        severity = "MEDIUM"

    else:
        severity = "LOW"

    result = {
        "severity": severity,
        "agreed_claims": agreed_claims,
        "disputed_claims": disputed_claims,
        "resolved_disputes": resolved_disputes
    }

    print(
        f"\n[CONFLICT_EXTRACTOR] "
        f"Severity={severity} "
        f"(Model Assessed={model_severity}) "
        f"Agreed={len(agreed_claims)} "
        f"Disputed={len(disputed_claims)} "
        f"Resolved={len(resolved_disputes)}"
    )

    print("\n=== DISPUTED CLAIMS ===")

    for idx, dispute in enumerate(
        disputed_claims,
        start=1
    ):
        print(f"{idx}. {dispute}")

    print("========================\n")

    return result


if __name__ == "__main__":
    sample_result = extract_conflicts(
        gpt_final_position=(
            "The system architecture enforces "
            "zero-trust data sovereignty using "
            "hardware-isolated TEE infrastructure."
        ),
        gemini_final_position=(
            "The system architecture utilizes "
            "standard container workloads running "
            "on public virtualized infrastructure "
            "without confidential computing."
        )
    )

    print(
        json.dumps(
            sample_result,
            indent=4
        )
    )
