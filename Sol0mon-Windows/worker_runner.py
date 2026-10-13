from job_store import (
    get_job,
    update_status,
    update_result,
    update_error
)

from pipeline_runner import execute

import traceback
from datetime import datetime


def log(job_id, message):
    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[{timestamp}] "
        f"[JOB {job_id}] "
        f"{message}",
        flush=True
    )


def log_status(job_id, completed, current_task, next_action):
    log(
        job_id,
        "STATUS:\n"
        f"Completed: {completed}\n"
        f"Current task: {current_task}\n"
        f"Next action: {next_action}"
    )


def run_job(job_id, memory_ids=None):

    try:

        log(
            job_id,
            "Loading job"
        )

        job = get_job(job_id)

        if not job:
            raise Exception(
                f"Job not found: {job_id}"
            )

        prompt = job[1]

        log(
            job_id,
            f"Prompt length: {len(prompt)}"
        )

        if memory_ids:
            log(
                job_id,
                f"Memory context: {len(memory_ids)} prior debate(s)"
            )

        update_status(
            job_id,
            "DEBATING"
        )

        log(
            job_id,
            "Starting pipeline execution"
        )
        log_status(job_id, "job loaded", "pipeline execution", "wait for debate result")

        result = execute(prompt, memory_ids=memory_ids)

        log(
            job_id,
            "Pipeline execution completed"
        )
        log_status(job_id, "pipeline execution", "classify result", "persist final status")

        #
        # Deadlock path — MAX_ROUNDS exhausted without consensus
        # (Legacy; governance framework now catches most deadlocks)
        #
        if result.get("status") == "deadlocked":

            consensus_score = result.get(
                "final_consensus_score",
                0
            )

            exit_reason = result.get(
                "exit_reason",
                "unknown"
            )

            error_message = (
                f"Deadlocked. "
                f"Exit reason: {exit_reason}. "
                f"Final consensus score: {consensus_score}%"
            )

            update_error(
                job_id,
                error_message
            )

            log(
                job_id,
                error_message
            )

            return

        #
        # Verification failure path
        #
        if result.get("status") == "verification_failed":

            verification_result = result.get(
                "verification_result",
                {}
            )

            verification_score = verification_result.get(
                "verification_score",
                0
            )

            error_message = (
                f"Verification failed. "
                f"Verification Score={verification_score}%"
            )

            update_error(
                job_id,
                error_message
            )

            log(
                job_id,
                error_message
            )

            return

        #
        # Success path (completed, with possible tradeoffs / human_required items)
        #
        final_answer = result.get(
            "synthesized_answer",
            ""
        )

        consensus_score = result.get(
            "final_consensus_score",
            0
        )

        # Log governance outcomes for observability
        verified_decisions = result.get("verified_decisions", [])
        verified_tradeoffs = result.get("verified_tradeoffs", [])
        human_required = result.get("human_required", [])

        if verified_tradeoffs:
            log(job_id, f"Verified Tradeoffs: {len(verified_tradeoffs)} stored (not errors)")
        if human_required:
            log(job_id, f"Human Decisions Required: {len(human_required)} deferred for review")

        log(
            job_id,
            f"Final answer length: "
            f"{len(final_answer)}"
        )

        log(
            job_id,
            f"Consensus score: "
            f"{consensus_score}"
        )

        log_status(job_id, "final answer prepared", "saving result", "mark job completed")

        update_result(
            job_id,
            final_answer,
            consensus_score
        )

        log(
            job_id,
            "Job marked COMPLETED"
        )

    except Exception as e:

        log(
            job_id,
            "EXCEPTION DETECTED"
        )

        traceback.print_exc()

        try:

            update_error(
                job_id,
                str(e)
            )

            log(
                job_id,
                "Job marked FAILED"
            )

        except Exception as db_error:

            print(
                f"\nCRITICAL DATABASE ERROR:\n"
                f"{db_error}\n",
                flush=True
            )
