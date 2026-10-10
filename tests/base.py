import shutil
import tempfile
import time
import unittest

from app import create_app


def recipients(*names):
    return [{"name": n, "email": f"{n.split()[0].lower()}@example.com"} for n in names]


def payload(*names, **extra):
    body = {"event_name": "Python Workshop 2026", "recipients": recipients(*names)}
    body.update(extra)
    return body


class AppTestCase(unittest.TestCase):
    mode = "sync"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="certgen-test-")
        self.config = {
            "DATABASE": f"{self.tmp}/test.db",
            "STORAGE_DIR": f"{self.tmp}/certs",
            "PROCESSING_MODE": self.mode,
            "MAX_RECIPIENTS": 50,
        }
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, **overrides):
        return create_app({**self.config, **overrides})

    def tearDown(self):
        self.app.extensions["executor"].shutdown(wait=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def submit(self, body, expected=None):
        resp = self.client.post("/api/jobs", json=body)
        if expected is not None:
            self.assertEqual(resp.status_code, expected, resp.get_data(as_text=True))
        return resp

    def wait_finished(self, job_id, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.client.get(f"/api/jobs/{job_id}").get_json()
            if job["status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish in time")
