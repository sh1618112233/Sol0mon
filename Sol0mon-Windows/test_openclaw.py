if __name__ == "__main__":
    import time
    from orchestrator import submit_job, get_result, get_status
    job_id = submit_job(
        "What is the best architecture for a planet-scale AI consensus platform?"
    )
    print(f"JOB ID: {job_id}")
    while True:
        status = get_status(job_id)
        print(f"STATUS: {status}")
        if status == "COMPLETED":
            print("\nFINAL RESULT\n", get_result(job_id)["result"])
            break
        if status == "FAILED":
            print(f"FAILED: {get_result(job_id)['error']}")
            break
        time.sleep(10)
