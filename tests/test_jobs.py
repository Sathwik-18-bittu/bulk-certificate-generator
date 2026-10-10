"""Creating a job, background processing, job status/progress."""
from app.service import create_job, recover_incomplete_jobs
from app import create_app
from tests.base import AppTestCase, payload


class CreateJobTests(AppTestCase):
    def test_create_job_returns_finished_job_in_sync_mode(self):
        resp = self.submit(payload("Ada Lovelace", "Grace Hopper"), expected=201)
        job = resp.get_json()
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["progress"], {"total": 2, "succeeded": 2, "failed": 0, "pending": 0, "percent": 100.0})
        self.assertEqual(job["title"], "Certificate of Completion")  # default
        self.assertEqual(resp.headers["Location"], job["links"]["self"])

    def test_custom_job_fields_are_stored(self):
        job = self.submit(payload("Ada Lovelace", title="Certificate of Excellence",
                                  issuer="ACME Academy", issue_date="2026-01-31"), 201).get_json()
        self.assertEqual((job["title"], job["issuer"], job["issue_date"]),
                         ("Certificate of Excellence", "ACME Academy", "2026-01-31"))

    def test_job_can_be_fetched_and_listed(self):
        job = self.submit(payload("Ada Lovelace"), 201).get_json()
        fetched = self.client.get(f"/api/jobs/{job['id']}").get_json()
        self.assertEqual(fetched["id"], job["id"])
        listing = self.client.get("/api/jobs").get_json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], job["id"])

    def test_unknown_job_is_404_json(self):
        resp = self.client.get("/api/jobs/doesnotexist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"]["code"], "JOB_NOT_FOUND")

    def test_unknown_route_returns_json_error(self):
        resp = self.client.get("/api/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.get_json())

    def test_certificate_numbers_are_unique(self):
        job = self.submit(payload("Ada Lovelace", "Grace Hopper", "Alan Turing"), 201).get_json()
        items = self.client.get(job["links"]["certificates"]).get_json()["items"]
        numbers = [i["certificate_number"] for i in items]
        self.assertEqual(len(set(numbers)), 3)


class ProgressTests(AppTestCase):
    def test_progress_is_visible_while_job_runs(self):
        seen = []

        def spying_renderer(path, **kwargs):
            job_id = self.app_job_id()
            seen.append(self.client.get(f"/api/jobs/{job_id}").get_json())
            from app.certificate import render_certificate
            return render_certificate(path, **kwargs)

        self.app.config["CERT_RENDERER"] = spying_renderer
        self.app_job_id = lambda: self.client.get("/api/jobs").get_json()["items"][0]["id"]
        self.submit(payload("Ada Lovelace", "Grace Hopper", "Alan Turing"), 201)

        self.assertEqual([s["status"] for s in seen], ["PROCESSING"] * 3)
        self.assertEqual([s["progress"]["succeeded"] for s in seen], [0, 1, 2])
        self.assertEqual([s["progress"]["pending"] for s in seen], [3, 2, 1])
        self.assertEqual([s["progress"]["percent"] for s in seen], [0.0, 33.3, 66.7])

    def test_final_status_completed_with_errors(self):
        body = payload("Ada Lovelace")
        body["recipients"].append({"name": "", "email": "bad"})
        job = self.submit(body, 201).get_json()
        self.assertEqual(job["status"], "COMPLETED_WITH_ERRORS")
        self.assertIsNotNone(job["completed_at"])

    def test_all_invalid_recipients_gives_failed_job(self):
        body = {"event_name": "X", "recipients": [{"name": "", "email": ""}, "nonsense"]}
        job = self.submit(body, 201).get_json()
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["progress"]["failed"], 2)


class BackgroundModeTests(AppTestCase):
    mode = "background"

    def test_returns_202_then_completes_in_background(self):
        resp = self.submit(payload("Ada Lovelace", "Grace Hopper", "Alan Turing"))
        self.assertEqual(resp.status_code, 202)
        job_id = resp.get_json()["id"]
        final = self.wait_finished(job_id)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["progress"]["succeeded"], 3)

    def test_interrupted_job_is_resumed_on_startup(self):
        # simulate a crash: a job was stored but never processed
        job_id = create_job(self.config["DATABASE"], payload("Ada Lovelace", "Grace Hopper"), 50)
        self.assertIn(job_id, recover_incomplete_jobs(self.config["DATABASE"]))
        app2 = create_app(self.config)  # "restart"
        try:
            client = app2.test_client()
            import time
            deadline = time.time() + 15
            while time.time() < deadline:
                job = client.get(f"/api/jobs/{job_id}").get_json()
                if job["status"] == "COMPLETED":
                    break
                time.sleep(0.05)
            self.assertEqual(job["status"], "COMPLETED")
            self.assertEqual(job["progress"]["succeeded"], 2)
        finally:
            app2.extensions["executor"].shutdown(wait=True)
