import json
import re
from cerebras_client import generate


def clean_final_answer(text):
    if not text:
        return ""

    text = re.sub(
        r'^\s*(Gemini said|GPT said|ChatGPT said|Claude said|Gemini:|GPT:|ChatGPT:|Claude:)\s*',
        '',
        text,
        flags=re.IGNORECASE
    )

    return text.strip()


def calculate_confidence(conflict_package, verifier_result):
    severity = str(conflict_package.get("severity", "LOW")).upper()

    if severity == "LOW":
        return 100

    verified_count = len(verifier_result.get("verified_facts", []))
    rejected_count = len(verifier_result.get("rejected_claims", []))
    uncertain_count = len(verifier_result.get("uncertain_claims", []))

    total_claims = max(1, verified_count + rejected_count + uncertain_count)

    rejected_ratio = rejected_count / total_claims
    uncertain_ratio = uncertain_count / total_claims

    confidence = 100 - (rejected_ratio * 100) - (uncertain_ratio * 50)

    return int(max(0, min(100, confidence)))


def build_synthesis(conflict_package, verifier_result, gpt_final_position, gemini_final_position):
    result = None

    severity = str(conflict_package.get("severity", "LOW")).upper()

    if severity == "LOW":
        print("\nLOW severity detected. Synthesis skipped.")

        if not gpt_final_position and not gemini_final_position:
            return "No consensus answer could be produced."

        if len(gpt_final_position) >= len(gemini_final_position):
            print("Source selected: GPT")
            return clean_final_answer(gpt_final_position)
        else:
            print("Source selected: Gemini")
            return clean_final_answer(gemini_final_position)

    verified_facts = verifier_result.get("verified_facts", [])
    rejected_claims = verifier_result.get("rejected_claims", [])
    uncertain_claims = verifier_result.get("uncertain_claims", [])
    resolved_disputes = conflict_package.get("resolved_disputes", [])

    synthesis_prompt = (
        "\nYou are a highly rigid Consensus Integration Engine.\n\n"
        "Your task is to merge the source documents into a SINGLE, COMPREHENSIVE final master document.\n\n"
        "RULES\n\n"
        "1. Use GPT FINAL POSITION and GEMINI FINAL POSITION as source documents.\n"
        "2. Merge compatible information seamlessly.\n"
        "3. Use VERIFIED FACTS to resolve conflicts.\n"
        "4. Remove REJECTED CLAIMS.\n"
        "5. Treat UNCERTAIN CLAIMS cautiously.\n"
        "6. Do not invent facts or speculate.\n"
        "7. Return natural language only (No JSON, No Markdown blocks, No code).\n\n"
        "CRITICAL ANTI-COMPRESSION INSTRUCTIONS\n\n"
        "8. DO NOT SUMMARIZE. Summarization is strictly forbidden.\n"
        "9. Do not collapse sections into summaries or brief overviews.\n"
        "10. The final answer MUST be at least 90% as detailed as the most detailed source.\n"
        "11. You MUST explicitly preserve all major architecture decisions.\n"
        "12. You MUST explicitly preserve all implementation decisions.\n"
        "13. You MUST explicitly preserve all database decisions.\n"
        "14. You MUST explicitly preserve all scaling decisions.\n"
        "15. You MUST explicitly preserve all security decisions.\n"
        "16. You MUST explicitly preserve all recovery decisions.\n"
        "17. You MUST explicitly preserve all operational decisions.\n\n"
        "GPT FINAL POSITION\n\n"
        "==================================================\n\n"
        f"{gpt_final_position}"
        "\n\n==================================================\n\n"
        "GEMINI FINAL POSITION\n\n"
        "==================================================\n\n"
        f"{gemini_final_position}"
        "\n\n==================================================\n\n"
        "VERIFIED FACTS\n\n"
        f"{json.dumps(verified_facts, indent=4)}"
        "\n\n==================================================\n\n"
        "REJECTED CLAIMS\n\n"
        f"{json.dumps(rejected_claims, indent=4)}"
        "\n\n==================================================\n\n"
        "UNCERTAIN CLAIMS\n\n"
        f"{json.dumps(uncertain_claims, indent=4)}"
        "\n\n==================================================\n\n"
        "RESOLVED DISPUTES\n\n"
        f"{json.dumps(resolved_disputes, indent=4)}"
        "\n"
    )

    try:
        result = generate(
            synthesis_prompt,
            module="consensus_synthesis"
        ).strip()
    except Exception as e:
        print(f"\nConsensus synthesis error: {e}")

    if result:
        largest_source = max(len(gpt_final_position), len(gemini_final_position))

        print(f"\nSYNTHESIS LENGTH: {len(result)} SOURCE LENGTH: {largest_source}")

        trigger_reason = None
        bad_phrases = ["insufficient information", "cannot determine", "unable to determine"]

        # Length-based rejection triggers
        if largest_source > 4000 and len(result) < largest_source * 0.5:
            trigger_reason = "length_ratio"
        elif largest_source > 1500 and len(result) < largest_source * 0.8:
            trigger_reason = "excessive_compression"
        elif result.endswith(":"):
            trigger_reason = "ends_with_colon"
        elif any(phrase in result.lower() for phrase in bad_phrases):
            trigger_reason = "refusal_phrase"

        if trigger_reason:
            print(f"\nSynthesis rejected. Reason={trigger_reason}"
                  f" ResultLength={len(result)}"
                  f" SourceLength={largest_source}")
        else:
            return clean_final_answer(result)

    # Fallback
    print("\nConsensus synthesis fallback activated.")

    if not gpt_final_position and not gemini_final_position:
        return "No consensus answer could be produced."

    if len(gpt_final_position) >= len(gemini_final_position):
        print("Fallback source selected: GPT")
        return clean_final_answer(gpt_final_position)
    else:
        print("Fallback source selected: Gemini")
        return clean_final_answer(gemini_final_position)


def build_consensus(conflict_package, verifier_result, gpt_final_position="", gemini_final_position=""):
    if verifier_result is None:
        verifier_result = {
            "verified_facts": [],
            "rejected_claims": [],
            "uncertain_claims": []
        }

    final_answer = build_synthesis(
        conflict_package,
        verifier_result,
        gpt_final_position,
        gemini_final_position
    )

    confidence = calculate_confidence(
        conflict_package,
        verifier_result
    )

    result = {
        "final_answer": final_answer,
        "severity": str(conflict_package.get("severity", "LOW")).upper(),
        "confidence_score": confidence,
        "verification_report": {
            "verified_facts": verifier_result.get("verified_facts", []),
            "rejected_claims": verifier_result.get("rejected_claims", []),
            "uncertain_claims": verifier_result.get("uncertain_claims", [])
        }
    }

    return result


if __name__ == "__main__":
    sample_conflicts = {
        "severity": "LOW",
        "agreed_claims": ["The capital of France is Paris."],
        "disputed_claims": [],
        "resolved_disputes": []
    }

    sample_verifier = {
        "verified_facts": [],
        "rejected_claims": [],
        "uncertain_claims": []
    }

    consensus = build_consensus(
        sample_conflicts,
        sample_verifier,
        "Gemini: Paris is the capital and largest city of France.",
        "France's capital city is Paris."
    )

    print(json.dumps(consensus, indent=4))