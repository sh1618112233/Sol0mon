from ollama_client import generate
from project_manager import (
    load_artifact,
    save_artifact
)


def summarize_artifact(
    project_name,
    category,
    filename
):

    content = load_artifact(
        project_name,
        category,
        filename
    )

    prompt = f"""
Summarize the following artifact.

Provide:

1. Executive Summary

2. Key Findings

3. Architectural Decisions

4. Tradeoffs

5. Risks

6. Open Questions

Keep the output concise but complete.

ARTIFACT

{content}
"""

    print(
        "\n===== RUNNING FAST SUMMARY =====\n"
    )

    # C9: Removed invalid think=False argument
    summary = generate(
        prompt,
        module="summarizer"
    )

    summary_filename = (
        filename.replace(
            ".md",
            "_summary.md"
        )
    )

    save_artifact(
        project_name,
        "summaries",
        summary_filename,
        summary
    )

    return summary


if __name__ == "__main__":

    result = summarize_artifact(
        "openclaw",
        "research",
        "planet_scale_ai_consensus_systems.md"
    )

    print(
        result[:2000]
    )
