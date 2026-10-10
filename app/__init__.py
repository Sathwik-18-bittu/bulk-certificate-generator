"""Bulk Certificate Generator - application factory."""
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from .certificate import render_certificate
from .db import init_db
from .routes import bp
from .service import process_job, recover_incomplete_jobs

DEFAULTS = {
    "DATABASE": os.environ.get("CERTGEN_DB", "instance/certgen.db"),
    "STORAGE_DIR": os.environ.get("CERTGEN_STORAGE", "instance/certificates"),
    # "background": respond 202 immediately, generate in a worker thread
    # "sync":       generate before responding (used by tests / tiny jobs)
    "PROCESSING_MODE": os.environ.get("CERTGEN_MODE", "background"),
    "WORKERS": int(os.environ.get("CERTGEN_WORKERS", "2")),
    "MAX_RECIPIENTS": int(os.environ.get("CERTGEN_MAX_RECIPIENTS", "5000")),
    "MAX_CONTENT_LENGTH": 5 * 1024 * 1024,
    "RESUME_ON_START": True,
    "CERT_RENDERER": render_certificate,
}


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DEFAULTS)
    app.config.update(config or {})
    logging.basicConfig(level=logging.INFO)

    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)
    os.makedirs(app.config["STORAGE_DIR"], exist_ok=True)
    init_db(app.config["DATABASE"])

    app.extensions["executor"] = ThreadPoolExecutor(
        max_workers=app.config["WORKERS"], thread_name_prefix="certgen"
    )
    app.register_blueprint(bp)
    _register_error_handlers(app)

    if app.config["PROCESSING_MODE"] == "background" and app.config["RESUME_ON_START"]:
        for job_id in recover_incomplete_jobs(app.config["DATABASE"]):
            app.logger.info("resuming interrupted job %s", job_id)
            submit_job(app, job_id)
    return app


def submit_job(app, job_id):
    """Run a job now (sync) or hand it to the worker pool (background)."""
    def run():
        # look the renderer up at call time so it can be swapped (tests)
        process_job(app.config["DATABASE"], app.config["STORAGE_DIR"], job_id, app.config["CERT_RENDERER"])

    if app.config["PROCESSING_MODE"] == "sync":
        run()
    else:
        app.extensions["executor"].submit(run)


def _register_error_handlers(app):
    def err(status, code, message, details=None):
        body = {"error": {"code": code, "message": message}}
        if details:
            body["error"]["details"] = details
        return jsonify(body), status

    @app.errorhandler(HTTPException)
    def http_error(e):
        return err(e.code, e.name.upper().replace(" ", "_"), e.description)

    @app.errorhandler(Exception)
    def unexpected(e):
        app.logger.exception("unhandled error")
        return err(500, "INTERNAL_ERROR", "Unexpected server error.")
