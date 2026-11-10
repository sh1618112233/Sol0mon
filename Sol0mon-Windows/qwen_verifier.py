import json

from verification_engine import verify_all_disputes


def verify(conflict_package):
    """
    Legacy compatibility wrapper.

    Existing pipeline code may still call:

        verify(conflict_package)

    This wrapper converts the old conflict package format
    into the newer verification_engine format.
    """

    disputed_claims = conflict_package.get(
        "disputed_claims",
        []
    )

    validated_disputes = []

    for claim in disputed_claims:

        if isinstance(claim, dict):

            validated_disputes.append({
                "dispute": claim.get(
                    "claim",
                    claim.get(
                        "dispute",
                        ""
                    )
                ),
                "normalized_key": claim.get(
                    "normalized_key",
                    ""
                )
            })

        else:

            validated_disputes.append({
                "dispute": str(claim),
                "normalized_key": ""
            })

    result = verify_all_disputes(
        question="Legacy Verification Request",
        validated_disputes=validated_disputes,
        gpt_position="",
        gemini_position="",
        use_external_research=False
    )

    return {
        "verified_facts": [
            item.get("dispute")
            for item in result.get(
                "resolved_claims",
                []
            )
        ],

        "rejected_claims": [],

        "uncertain_claims": [
            item.get("dispute")
            for item in result.get(
                "unresolved_claims",
                []
            )
        ],

        "verification_score": result.get(
            "verification_score",
            0
        )
    }


if __name__ == "__main__":

    sample_conflict = {
        "severity": "MEDIUM",
        "disputed_claims": [
            "Kubernetes orchestration versus decentralized runtime architecture"
        ]
    }

    output = verify(
        sample_conflict
    )

    print(
        json.dumps(
            output,
            indent=4
        )
    )
