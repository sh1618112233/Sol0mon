from artifact_summarizer import (
    summarize_artifact
)

if __name__ == "__main__":
    summary = summarize_artifact(
        "openclaw", "research", "planet_scale_ai_consensus_systems.md"
    )
    print("\n===== SUMMARY =====\n")
    print(summary[:2000])
