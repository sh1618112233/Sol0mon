import threading

from job_store import (
    initialize_database,
    create_job,
    get_job,
    update_job  # Moved to top-level imports for cleaner execution tracking
)
from worker_runner import run_job

# M5C: Lazy init — avoid import-time side effects
_initialized = False


def _ensure_db():
    global _initialized
    if not _initialized:
        initialize_database()
        _initialized = True


def safe_run_job(job_id, memory_ids=None):
    """
    Defensive runtime execution wrapper. Captures unexpected thread crashes
    to prevent jobs from getting permanently orphaned in an unhandled pending state.
    """
    try:
        run_job(job_id, memory_ids=memory_ids)
    except Exception as e:
        print(f"\n[ORCHESTRATOR CRITICAL WORKER CRASH] Job #{job_id} failed catastrophically: {e}")
        try:
            # Cleans state tracking across the telemetry table without inner import overhead
            update_job(
                job_id=job_id,
                status="FAILED",
                consensus_score=0,
                result="[CRITICAL OUT-OF-BAND WORKER CRASH]",
                error=str(e)
            )
            print(f"[ORCHESTRATOR TELEMETRY] Job #{job_id} successfully marked as FAILED in state engine.")
        except Exception as db_err:
            print(f"[ORCHESTRATOR CORRUPTION] Failed to update job status following thread crash: {db_err}")


def submit_job(prompt, memory_ids=None):
    _ensure_db()
    job_id = create_job(prompt)

    worker = threading.Thread(
        target=safe_run_job,
        args=(job_id, memory_ids),
        daemon=False
    )

    worker.start()
    return job_id


def get_status(job_id):
    job = get_job(job_id)
    if not job:
        return None
    return job[2]


def get_result(job_id):
    job = get_job(job_id)
    if not job:
        return None

    return {
        "status": job[2],
        "consensus_score": job[3],
        "result": job[4],
        "error": job[5]
    }


if __name__ == "__main__":
    job_id = submit_job("Test OpenClaw Job")

    print("Job Created:")
    print(job_id)

    print(get_status(job_id))
