from performance_logger import (
    save_metrics,
    load_metrics
)

save_metrics(
    "test_job",
    {
        "debate_seconds": 100,
        "consensus_score": 92
    }
)

print(
    load_metrics(
        "test_job"
    )
)
