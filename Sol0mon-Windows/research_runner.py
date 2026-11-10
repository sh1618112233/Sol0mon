from pipeline_runner import execute
from project_manager import save_artifact


def run_research(
    project_name,
    topic
):

    prompt = f"""
Conduct deep technical research.

Topic:

{topic}

Requirements:

1. Current industry approaches
2. Architecture patterns
3. Technologies used
4. Advantages
5. Disadvantages
6. Scalability considerations
7. Security considerations
8. Operational considerations

Produce a comprehensive research document.
"""

    result = execute(prompt)

    # C8: Use correct return schema. DebateEngine.run() returns top-level keys.
    # synthesized_answer is the final output. Status may be 'completed',
    # 'verification_failed', or 'deadlocked'.
    status = result.get("status", "unknown")
    final_answer = result.get("synthesized_answer", "")

    if not final_answer:
        print(f"[RESEARCH_RUNNER] No synthesized_answer in result. Status={status}. "
              f"Result keys: {list(result.keys())}")
        # Fallback: try gpt_final as a partial result
        final_answer = result.get("gpt_final", "")

    filename = (
        topic.lower()
        .replace(" ", "_")
        + ".md"
    )

    save_artifact(
        project_name,
        "research",
        filename,
        final_answer
    )

    return final_answer
