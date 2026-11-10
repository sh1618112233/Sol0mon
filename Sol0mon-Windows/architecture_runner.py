from pipeline_runner import execute
from project_manager import (
    save_artifact,
    load_artifact
)


def build_architecture(
    project_name,
    research_file
):

    research = load_artifact(
        project_name,
        "research",
        research_file
    )

    prompt = f"""
Using the research below, design a complete system architecture.

Provide:

1. Executive Summary

2. System Architecture

3. Major Components

4. Data Flow

5. Storage Architecture

6. Security Architecture

7. Scalability Architecture

8. Deployment Architecture

9. Operational Considerations

10. Risks

Research:

{research}
"""

    result = execute(prompt)

    # C8: Use correct return schema. DebateEngine.run() returns top-level keys,
    # not a "consensus" envelope.
    architecture = result.get("synthesized_answer", "")

    if not architecture:
        print(f"[ARCHITECTURE_RUNNER] No synthesized_answer in result. "
              f"Result keys: {list(result.keys())}")
        # Fallback: try gpt_final as a partial result
        architecture = result.get("gpt_final", "")

    save_artifact(
        project_name,
        "architecture",
        "system_architecture.md",
        architecture
    )

    return architecture
