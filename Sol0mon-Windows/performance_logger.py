import json
import os
from datetime import datetime


LOG_DIR = "logs/performance"


def ensure_log_dir():
    os.makedirs(
        LOG_DIR,
        exist_ok=True
    )


def save_metrics(
    job_id,
    metrics
):
    ensure_log_dir()

    filename = (
        f"{job_id}.json"
    )

    filepath = os.path.join(
        LOG_DIR,
        filename
    )

    metrics["timestamp"] = (
        datetime.now().isoformat()
    )

    with open(
        filepath,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            metrics,
            f,
            indent=4
        )

    return filepath


def load_metrics(job_id):

    filepath = os.path.join(
        LOG_DIR,
        f"{job_id}.json"
    )

    if not os.path.exists(
        filepath
    ):
        return None

    with open(
        filepath,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)
