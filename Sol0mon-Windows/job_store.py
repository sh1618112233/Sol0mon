import os
import sqlite3
import uuid
from datetime import datetime, timezone

DB_FILE = "database/jobs.db"


def get_connection():
    # Retained tight timeout threshold parameters as a defensive secondary boundary
    return sqlite3.connect(DB_FILE, timeout=30)


def initialize_database():
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = get_connection()
    
    # Structural Thread Optimization: Force Write-Ahead Logging state persistence
    print("[DATABASE INITIALIZATION] Enforcing WAL journal mode configuration pass...")
    conn.execute("PRAGMA journal_mode=WAL;")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        prompt TEXT NOT NULL,
        status TEXT NOT NULL,
        consensus_score REAL,
        result TEXT,
        error TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def create_job(prompt):
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    conn.execute("""
    INSERT INTO jobs (
        job_id,
        prompt,
        status,
        created_at,
        updated_at
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        job_id,
        prompt,
        "PENDING",
        now,
        now
    ))
    conn.commit()
    conn.close()

    return job_id


def get_job(job_id):
    conn = get_connection()
    cursor = conn.execute(
        "SELECT * FROM jobs WHERE job_id=?",
        (job_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row


def update_status(job_id, status):
    conn = get_connection()
    conn.execute("""
    UPDATE jobs
    SET status=?,
        updated_at=?
    WHERE job_id=?
    """, (
        status,
        datetime.now(timezone.utc).isoformat(),
        job_id
    ))
    conn.commit()
    conn.close()


def update_result(job_id, result, consensus_score=None):
    conn = get_connection()
    conn.execute("""
    UPDATE jobs
    SET result=?,
        consensus_score=?,
        status='COMPLETED',
        updated_at=?
    WHERE job_id=?
    """, (
        result,
        consensus_score,
        datetime.now(timezone.utc).isoformat(),
        job_id
    ))
    conn.commit()
    conn.close()


def update_error(job_id, error):
    conn = get_connection()
    conn.execute("""
    UPDATE jobs
    SET error=?,
        status='FAILED',
        updated_at=?
    WHERE job_id=?
    """, (
        error,
        datetime.now(timezone.utc).isoformat(),
        job_id
    ))
    conn.commit()
    conn.close()


def update_job(job_id, status, consensus_score=None, result=None, error=None):
    """
    Unified telemetry sync interface. Satisfies integration tracking hooks 
    called by the safe thread orchestrator layout upon out-of-band crash resets.
    """
    conn = get_connection()
    conn.execute("""
    UPDATE jobs
    SET status=?,
        consensus_score=?,
        result=?,
        error=?,
        updated_at=?
    WHERE job_id=?
    """, (
        status,
        consensus_score,
        result,
        error,
        datetime.now(timezone.utc).isoformat(),
        job_id
    ))
    conn.commit()
    conn.close()


def list_jobs():
    conn = get_connection()
    cursor = conn.execute("""
    SELECT
        job_id,
        prompt,
        status,
        consensus_score,
        created_at,
        updated_at
    FROM jobs
    ORDER BY created_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


if __name__ == "__main__":
    initialize_database()

    test_job = create_job("Test Prompt")
    print("Created Job:")
    print(test_job)
    print()

    print("Job Record:")
    print(get_job(test_job))
    print()

    print("All Jobs:")
    print(list_jobs())
