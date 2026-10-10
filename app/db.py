"""SQLite access helpers.

A fresh connection is opened per unit of work. SQLite connections must not be
shared between threads, and background workers run in their own threads, so
"one short-lived connection per operation" is the simplest correct model.
"""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    event_name    TEXT NOT NULL,
    title         TEXT NOT NULL,
    issuer        TEXT NOT NULL,
    issue_date    TEXT NOT NULL,
    status        TEXT NOT NULL,
    total         INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS certificates (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    name               TEXT,
    email              TEXT,
    achievement        TEXT,
    certificate_number TEXT NOT NULL,
    status             TEXT NOT NULL,
    error              TEXT,
    file_path          TEXT,
    created_at         TEXT NOT NULL,
    generated_at       TEXT,
    UNIQUE (job_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_cert_job_status ON certificates(job_id, status);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(db_path):
    """Commit on success, roll back on error, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path):
    with transaction(db_path) as conn:
        conn.executescript(SCHEMA)
