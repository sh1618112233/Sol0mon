import time

from orchestrator import (
    submit_job,
    get_result,
    get_status
)

job_id = submit_job(
    "What is the best architecture for a planet-scale AI consensus platform?"
)

print(
    f"JOB ID: {job_id}"
)

while True:

    status = get_status(job_id)

    print(
        f"STATUS: {status}"
    )

    if status == "COMPLETED":

        result = get_result(job_id)

        print(
            "\nFINAL RESULT\n"
        )

        print(
            result["result"]
        )

        break

    elif status == "FAILED":

        result = get_result(job_id)

        print(
            f"FAILED: {result['error']}"
        )

        break

    time.sleep(10)
