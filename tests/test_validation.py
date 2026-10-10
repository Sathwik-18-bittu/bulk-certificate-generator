"""Input validation: request-level (400) and recipient-level (stored as FAILED)."""
from tests.base import AppTestCase, payload


class RequestValidationTests(AppTestCase):
    def assert_400(self, resp, field=None):
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        err = resp.get_json()["error"]
        self.assertIn(err["code"], ("VALIDATION_ERROR", "INVALID_JSON"))
        if field:
            self.assertIn(field, [d["field"] for d in err["details"]])

    def test_body_not_json(self):
        resp = self.client.post("/api/jobs", data="hello", content_type="text/plain")
        self.assert_400(resp)
        self.assertEqual(resp.get_json()["error"]["code"], "INVALID_JSON")

    def test_body_not_an_object(self):
        self.assert_400(self.client.post("/api/jobs", json=[1, 2]))

    def test_missing_event_name(self):
        body = payload("Ada Lovelace")
        del body["event_name"]
        self.assert_400(self.submit(body), "event_name")

    def test_empty_recipients(self):
        self.assert_400(self.submit({"event_name": "X", "recipients": []}), "recipients")

    def test_recipients_wrong_type(self):
        self.assert_400(self.submit({"event_name": "X", "recipients": "ada"}), "recipients")

    def test_too_many_recipients(self):
        body = {"event_name": "X",
                "recipients": [{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(51)]}
        self.assert_400(self.submit(body), "recipients")

    def test_bad_issue_date(self):
        self.assert_400(self.submit(payload("Ada Lovelace", issue_date="31/01/2026")), "issue_date")

    def test_unrenderable_event_name(self):
        self.assert_400(self.submit(payload("Ada Lovelace", event_name="活动")), "event_name")

    def test_rejected_request_creates_no_job(self):
        self.submit({"event_name": "X", "recipients": []})
        self.assertEqual(self.client.get("/api/jobs").get_json()["total"], 0)


class RecipientValidationTests(AppTestCase):
    def failures_for(self, *recipients):
        body = {"event_name": "X", "recipients": list(recipients)}
        job = self.submit(body, 201).get_json()
        return job, {f["seq"]: f["error"] for f in job["failures"]}

    def test_invalid_recipients_are_reported_and_valid_ones_still_generated(self):
        job, failures = self.failures_for(
            {"name": "Ada Lovelace", "email": "ada@example.com"},   # 1 ok
            {"name": "", "email": "x@example.com"},                  # 2 missing name
            {"name": "Bob", "email": "not-an-email"},                # 3 bad email
            {"name": "Eve"},                                         # 4 missing email
            "just a string",                                         # 5 wrong type
            {"name": "Grace Hopper", "email": "grace@example.com"},  # 6 ok
        )
        self.assertEqual(job["status"], "COMPLETED_WITH_ERRORS")
        self.assertEqual(job["progress"]["succeeded"], 2)
        self.assertEqual(sorted(failures), [2, 3, 4, 5])
        self.assertIn("name is required", failures[2])
        self.assertIn("valid address", failures[3])
        self.assertIn("email is required", failures[4])
        self.assertIn("JSON object", failures[5])

    def test_duplicate_email_in_same_job_is_rejected_case_insensitively(self):
        _, failures = self.failures_for(
            {"name": "Ada", "email": "ada@example.com"},
            {"name": "Ada Again", "email": "ADA@example.com"},
        )
        self.assertEqual(list(failures), [2])
        self.assertIn("duplicate", failures[2])

    def test_name_too_long(self):
        _, failures = self.failures_for({"name": "A" * 101, "email": "a@example.com"})
        self.assertIn("at most 100", failures[1])

    def test_unsupported_characters_in_name(self):
        _, failures = self.failures_for({"name": "李雷", "email": "li@example.com"})
        self.assertIn("cannot render", failures[1])

    def test_latin1_accented_names_are_accepted(self):
        job, failures = self.failures_for({"name": "José Müller-Ñandú", "email": "jose@example.com"})
        self.assertEqual(failures, {})
        self.assertEqual(job["status"], "COMPLETED")

    def test_achievement_must_be_string(self):
        _, failures = self.failures_for({"name": "Ada", "email": "a@example.com", "achievement": 5})
        self.assertIn("achievement must be a string", failures[1])

    def test_whitespace_is_normalised(self):
        job = self.submit({"event_name": "X", "recipients": [
            {"name": "  Ada   Lovelace ", "email": " Ada@Example.com "}]}, 201).get_json()
        item = self.client.get(job["links"]["certificates"]).get_json()["items"][0]
        self.assertEqual((item["name"], item["email"]), ("Ada Lovelace", "ada@example.com"))
