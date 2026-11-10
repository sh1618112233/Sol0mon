import json
from datetime import datetime

from debate_engine import DebateEngine
from conflict_extractor import extract_conflicts
from qwen_verifier import verify
from consensus_engine import build_consensus


def get_prompt():
    print("\nPaste prompt below.")
    print("Finish by entering a single line containing only:")
    print("END")
    print()

    lines = []

    while True:
        line = input()

        if line.strip() == "END":
            break

        lines.append(line)

    return "\n".join(lines).strip()


def run():
    prompt = get_prompt()

    if not prompt:
        print("\nERROR: Empty prompt.\n")
        return

    print("\n===== PROMPT LOADED =====")
    print(f"Prompt Length: {len(prompt)}")
    print("=========================\n")

    print("===== PROMPT PREVIEW =====")
    print(prompt[:1000])
    print("==========================\n")

    engine = DebateEngine()

    try:
        print("\n===== STARTING DEBATE =====\n")

        debate_result = engine.run(prompt)

        gpt_response = debate_result["gpt_final"]
        gemini_response = debate_result["gemini_final"]

        print("\n===== DEBATE COMPLETE =====")
        print(
            f"Consensus Score: "
            f"{debate_result.get('final_consensus_score', 0)}%"
        )
        print("===========================\n")

        print("\n===== EXTRACTING CONFLICTS =====\n")

        conflicts = extract_conflicts(
            gpt_final_position=gpt_response,
            gemini_final_position=gemini_response
        )

        print(
            f"Conflict Severity: "
            f"{conflicts.get('severity')}"
        )

        print(
            f"Agreed Claims: "
            f"{len(conflicts.get('agreed_claims', []))}"
        )

        print(
            f"Disputed Claims: "
            f"{len(conflicts.get('disputed_claims', []))}"
        )

        print("\n===== VERIFYING CLAIMS =====\n")

        verifier_result = verify(
            conflicts
        )

        print(
            f"Verified Facts: "
            f"{len(verifier_result.get('verified_facts', []))}"
        )

        print(
            f"Rejected Claims: "
            f"{len(verifier_result.get('rejected_claims', []))}"
        )

        print(
            f"Uncertain Claims: "
            f"{len(verifier_result.get('uncertain_claims', []))}"
        )

        print(
            "\n===== SYNTHESIZING FINAL CONSENSUS =====\n"
        )

        consensus = build_consensus(
            conflicts,
            verifier_result,
            gpt_response,
            gemini_response
        )

        final_answer = consensus.get(
            "final_answer",
            ""
        )

        # Generate a unique timestamped filename to prevent regression overwrites
        filename = (
            f"final_consensus_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )

        with open(
            filename,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(final_answer)

        print("\n" + "=" * 60)
        print("FINAL SYNTHESIS GENERATED")
        print("=" * 60)

        print(
            f"Length: {len(final_answer)}"
        )

        print(
            f"Saved: {filename}"
        )

        print("\n" + "=" * 60)
        print("CONSENSUS METRICS")
        print("=" * 60)

        print(
            f"Confidence Score: "
            f"{consensus.get('confidence_score', 0)}%"
        )

        print(
            f"Severity: "
            f"{consensus.get('severity', 'UNKNOWN')}"
        )

        print("=" * 60 + "\n")

    except Exception as e:
        print(
            f"\nCRITICAL FAILURE: {str(e)}"
        )


if __name__ == "__main__":
    run()
