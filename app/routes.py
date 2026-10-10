"""HTTP API."""
import io
import os
import re
import zipfile

from flask import Blueprint, current_app, jsonify, request, send_file, url_for

from .db import connect
from .service import (C_FAILED, C_GENERATED, C_PENDING, J_FINISHED, create_job, status_counts)
from .validation import RequestValidationError

bp = Blueprint("api", __name__, url_prefix="/api")

MAX_FAILURES_IN_JOB = 100


def error(status, code, message, details=None):
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return jsonify(body), status


def _db():
    return connect(current_app.config["DATABASE"])


def _get_job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _job_not_found(job_id):
    return error(404, "JOB_NOT_FOUND", f"No job with id '{job_id}'.")


def _cert_json(row, job_id):
    out = {
        "id": row["id"],
        "seq": row["seq"],
        "certificate_number": row["certificate_number"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "error": row["error"],
        "generated_at": row["generated_at"],
    }
    if row["status"] == C_GENERATED:
        out["download_url"] = url_for("api.download_certificate", job_id=job_id, cert_id=row["id"])
    return out


def _job_json(conn, job, with_failures=True):
    counts = status_counts(conn, job["id"])
    done = counts[C_GENERATED] + counts[C_FAILED]
    out = {
        "id": job["id"],
        "event_name": job["event_name"],
        "title": job["title"],
        "issuer": job["issuer"],
        "issue_date": job["issue_date"],
        "status": job["status"],
        "progress": {
            "total": job["total"],
            "succeeded": counts[C_GENERATED],
            "failed": counts[C_FAILED],
            "pending": counts[C_PENDING],
            "percent": round(100 * done / job["total"], 1) if job["total"] else 100.0,
        },
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "links": {
            "self": url_for("api.get_job", job_id=job["id"]),
            "certificates": url_for("api.list_certificates", job_id=job["id"]),
            "download_all": url_for("api.download_all", job_id=job["id"]),
        },
    }
    if with_failures:
        rows = conn.execute(
            "SELECT seq, name, email, error FROM certificates WHERE job_id = ? AND status = ?"
            " ORDER BY seq LIMIT ?",
            (job["id"], C_FAILED, MAX_FAILURES_IN_JOB),
        ).fetchall()
        out["failures"] = [dict(r) for r in rows]
        out["failures_truncated"] = counts[C_FAILED] > MAX_FAILURES_IN_JOB
    return out


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@bp.post("/jobs")
def submit_job_request():
    from . import submit_job  # late import: avoids a circular import at load time

    payload = request.get_json(silent=True)
    if payload is None:
        return error(400, "INVALID_JSON", "Body must be valid JSON with Content-Type: application/json.")
    try:
        job_id = create_job(current_app.config["DATABASE"], payload, current_app.config["MAX_RECIPIENTS"])
    except RequestValidationError as exc:
        return error(400, "VALIDATION_ERROR", exc.message, exc.details)

    submit_job(current_app._get_current_object(), job_id)

    conn = _db()
    try:
        body = _job_json(conn, _get_job(conn, job_id))
    finally:
        conn.close()
    status = 201 if body["status"] in J_FINISHED else 202
    resp = jsonify(body)
    resp.status_code = status
    resp.headers["Location"] = body["links"]["self"]
    return resp


@bp.get("/jobs")
def list_jobs():
    page, per_page = _pagination()
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page),
        ).fetchall()
        items = [_job_json(conn, r, with_failures=False) for r in rows]
    finally:
        conn.close()
    return jsonify({"page": page, "per_page": per_page, "total": total, "items": items})


@bp.get("/jobs/<job_id>")
def get_job(job_id):
    conn = _db()
    try:
        job = _get_job(conn, job_id)
        if job is None:
            return _job_not_found(job_id)
        return jsonify(_job_json(conn, job))
    finally:
        conn.close()


@bp.get("/jobs/<job_id>/certificates")
def list_certificates(job_id):
    status = request.args.get("status")
    if status is not None:
        status = status.upper()
        if status not in (C_PENDING, C_GENERATED, C_FAILED):
            return error(400, "VALIDATION_ERROR", "status must be one of PENDING, GENERATED, FAILED.")
    page, per_page = _pagination()

    conn = _db()
    try:
        if _get_job(conn, job_id) is None:
            return _job_not_found(job_id)
        where, args = "job_id = ?", [job_id]
        if status:
            where += " AND status = ?"
            args.append(status)
        total = conn.execute(f"SELECT COUNT(*) FROM certificates WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM certificates WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
            args + [per_page, (page - 1) * per_page],
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "page": page, "per_page": per_page, "total": total,
        "items": [_cert_json(r, job_id) for r in rows],
    })


def _pagination():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except ValueError:
        page, per_page = 1, 50
    return page, per_page


def _safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "certificate"


@bp.get("/jobs/<job_id>/certificates/<cert_id>")
def download_certificate(job_id, cert_id):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM certificates WHERE id = ? AND job_id = ?", (cert_id, job_id)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return error(404, "CERTIFICATE_NOT_FOUND", "Certificate not found for this job.")
    if row["status"] == C_PENDING:
        return error(409, "CERTIFICATE_NOT_READY", "Certificate has not been generated yet.")
    if row["status"] == C_FAILED:
        return error(409, "CERTIFICATE_FAILED", "Certificate generation failed.", [{"message": row["error"]}])

    path = os.path.join(current_app.config["STORAGE_DIR"], row["file_path"])
    if not os.path.isfile(path):
        return error(410, "FILE_MISSING", "The certificate file is no longer available.")
    return send_file(
        path, mimetype="application/pdf", as_attachment=True,
        download_name=f"{_safe_filename(row['name'])}_{row['certificate_number']}.pdf",
    )


@bp.get("/jobs/<job_id>/download")
def download_all(job_id):
    """All generated certificates of a finished job as one ZIP."""
    conn = _db()
    try:
        job = _get_job(conn, job_id)
        if job is None:
            return _job_not_found(job_id)
        if job["status"] not in J_FINISHED:
            return error(409, "JOB_NOT_FINISHED", "Job is still running; try again when it has finished.")
        rows = conn.execute(
            "SELECT * FROM certificates WHERE job_id = ? AND status = ? ORDER BY seq", (job_id, C_GENERATED)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return error(404, "NO_CERTIFICATES", "This job has no generated certificates.")

    buf = io.BytesIO()
    storage = current_app.config["STORAGE_DIR"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for row in rows:
            path = os.path.join(storage, row["file_path"])
            if os.path.isfile(path):
                zf.write(path, f"{row['certificate_number']}_{_safe_filename(row['name'])}.pdf")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"certificates_{job_id[:8]}.zip")
