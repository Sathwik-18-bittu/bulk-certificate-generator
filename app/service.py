"""Business logic: creating jobs and generating certificates."""
import logging
import os
import uuid
from datetime import datetime, timezone

from .db import connect, transaction
from .validation import validate_job_payload, validate_recipient

log = logging.getLogger(__name__)

# certificate states
C_PENDING, C_GENERATED, C_FAILED = "PENDING", "GENERATED", "FAILED"
# job states
J_PENDING, J_PROCESSING = "PENDING", "PROCESSING"
J_COMPLETED, J_COMPLETED_ERRORS, J_FAILED = "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"
J_FINISHED = (J_COMPLETED, J_COMPLETED_ERRORS, J_FAILED)

MAX_ERROR_LEN = 500


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_job(db_path, payload, max_recipients):
    """Validate the request and persist the job plus one row per recipient.

    Invalid recipients are stored as FAILED rows (with the reason) instead of
    rejecting the whole request. Raises RequestValidationError for request-level
    problems. Returns the new job id.
    """
    data = validate_job_payload(payload, max_recipients)
    job_id = uuid.uuid4().hex
    created = now()
    seen_emails = set()
    rows = []
    pending = 0

    for seq, raw in enumerate(data["recipients"], start=1):
        clean, error = validate_recipient(raw, seen_emails)
        if error:
            status = C_FAILED
        else:
            status, error = C_PENDING, None
            pending += 1
        rows.append((
            uuid.uuid4().hex, job_id, seq, clean["name"], clean["email"], clean["achievement"],
            f"CERT-{job_id[:8].upper()}-{seq:05d}", status, error, created,
        ))

    job_status = J_PENDING if pending else J_FAILED
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, event_name, title, issuer, issue_date, status, total, created_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, data["event_name"], data["title"], data["issuer"], data["issue_date"],
             job_status, len(rows), created, None if pending else created),
        )
        conn.executemany(
            "INSERT INTO certificates (id, job_id, seq, name, email, achievement, certificate_number,"
            " status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return job_id


def process_job(db_path, storage_dir, job_id, renderer):
    """Generate every PENDING certificate of a job.

    Safe to call again after a crash: only PENDING rows are touched. Each
    certificate is committed on its own so progress is visible while the job
    runs, and one failure never stops the others.
    """
    try:
        conn = connect(db_path)
        try:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                log.error("process_job: unknown job %s", job_id)
                return
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
                (J_PROCESSING, now(), job_id),
            )
            conn.commit()

            pending = conn.execute(
                "SELECT * FROM certificates WHERE job_id = ? AND status = ? ORDER BY seq",
                (job_id, C_PENDING),
            ).fetchall()

            for cert in pending:
                rel_path = os.path.join(job_id, f"{cert['id']}.pdf")
                try:
                    renderer(
                        os.path.join(storage_dir, rel_path),
                        name=cert["name"], achievement=cert["achievement"],
                        event_name=job["event_name"], title=job["title"], issuer=job["issuer"],
                        issue_date=job["issue_date"], certificate_number=cert["certificate_number"],
                    )
                except Exception as exc:  # one bad certificate must not stop the job
                    log.warning("certificate %s failed: %s", cert["id"], exc)
                    conn.execute(
                        "UPDATE certificates SET status = ?, error = ? WHERE id = ?",
                        (C_FAILED, f"Generation failed: {type(exc).__name__}: {exc}"[:MAX_ERROR_LEN], cert["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE certificates SET status = ?, file_path = ?, generated_at = ?, error = NULL"
                        " WHERE id = ?",
                        (C_GENERATED, rel_path, now(), cert["id"]),
                    )
                conn.commit()

            counts = status_counts(conn, job_id)
            final = J_COMPLETED if counts[C_FAILED] == 0 else (
                J_FAILED if counts[C_GENERATED] == 0 else J_COMPLETED_ERRORS)
            conn.execute("UPDATE jobs SET status = ?, completed_at = ? WHERE id = ?", (final, now(), job_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Infrastructure failure (disk, DB). Leave remaining rows PENDING so
        # recover_incomplete_jobs() can resume them on the next start.
        log.exception("process_job crashed for job %s", job_id)


def recover_incomplete_jobs(db_path):
    """Return ids of jobs interrupted mid-run (e.g. server restart)."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN (?, ?) ORDER BY created_at", (J_PENDING, J_PROCESSING)
        ).fetchall()
    return [r["id"] for r in rows]


def status_counts(conn, job_id):
    counts = {C_PENDING: 0, C_GENERATED: 0, C_FAILED: 0}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM certificates WHERE job_id = ? GROUP BY status", (job_id,)
    ):
        counts[row["status"]] = row["n"]
    return counts
