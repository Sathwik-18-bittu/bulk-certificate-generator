"""Certificate generation, per-certificate failure handling, retrieval."""
import io
import os
import tempfile
import zipfile

from app.certificate import render_certificate
from app.service import create_job, process_job
from tests.base import AppTestCase, payload


def failing_for(*bad_names):
    """Renderer that raises for the given names and renders normally otherwise."""
    def renderer(path, **kwargs):
        if kwargs["name"] in bad_names:
            raise RuntimeError(f"boom for {kwargs['name']}")
        return render_certificate(path, **kwargs)
    return renderer


class RenderTests(AppTestCase):
    def render(self, **overrides):
        args = dict(name="Ada Lovelace", achievement=None, event_name="PyCon", title="Certificate",
                    issuer="Org", issue_date="2026-10-10", certificate_number="CERT-1")
        args.update(overrides)
        path = os.path.join(self.tmp, "out", "c.pdf")
        render_certificate(path, **args)
        with open(path, "rb") as fh:
            return path, fh.read()

    def test_generates_a_pdf_with_recipient_details(self):
        path, data = self.render(achievement="with distinction")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        # check the drawn page content, not just the metadata
        for text in ("Ada Lovelace", "PyCon", "with distinction", "Certificate No. CERT-1",
                     "Date: 10 October 2026", "Issued by: Org"):
            self.assertIn(f"({text}) Tj".encode(), data)
        self.assertFalse(os.path.exists(path + ".part"))  # no temp file left behind

    def test_very_long_name_still_renders(self):
        _, data = self.render(name="Wolfeschlegelsteinhausenbergerdorff " * 2)
        self.assertTrue(data.startswith(b"%PDF"))

    def test_markup_in_names_is_not_interpreted(self):
        _, data = self.render(name="<b>Ada</b> & <script>")
        self.assertIn(b"(<b>Ada</b> & <script>) Tj", data)


class GenerationTests(AppTestCase):
    def test_pdf_files_are_written_for_each_valid_recipient(self):
        job = self.submit(payload("Ada Lovelace", "Grace Hopper"), 201).get_json()
        files = os.listdir(os.path.join(self.config["STORAGE_DIR"], job["id"]))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.endswith(".pdf") for f in files))

    def test_failure_of_one_certificate_does_not_stop_the_others(self):
        self.app.config["CERT_RENDERER"] = failing_for("Grace Hopper")
        job = self.submit(payload("Ada Lovelace", "Grace Hopper", "Alan Turing"), 201).get_json()

        self.assertEqual(job["status"], "COMPLETED_WITH_ERRORS")
        self.assertEqual(job["progress"]["succeeded"], 2)
        self.assertEqual(job["progress"]["failed"], 1)
        self.assertEqual(len(job["failures"]), 1)
        self.assertEqual(job["failures"][0]["name"], "Grace Hopper")
        self.assertIn("RuntimeError: boom", job["failures"][0]["error"])

        items = self.client.get(job["links"]["certificates"]).get_json()["items"]
        self.assertEqual([i["status"] for i in items], ["GENERATED", "FAILED", "GENERATED"])
        self.assertIn("download_url", items[0])
        self.assertNotIn("download_url", items[1])

    def test_every_certificate_failing_marks_job_failed(self):
        self.app.config["CERT_RENDERER"] = failing_for("Ada Lovelace", "Grace Hopper")
        job = self.submit(payload("Ada Lovelace", "Grace Hopper"), 201).get_json()
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["progress"]["failed"], 2)

    def test_rerunning_a_finished_job_does_not_regenerate(self):
        calls = []

        def counting(path, **kwargs):
            calls.append(kwargs["name"])
            return render_certificate(path, **kwargs)

        self.app.config["CERT_RENDERER"] = counting
        job = self.submit(payload("Ada Lovelace"), 201).get_json()
        process_job(self.config["DATABASE"], self.config["STORAGE_DIR"], job["id"], counting)
        self.assertEqual(calls, ["Ada Lovelace"])

    def test_unexpected_crash_leaves_remaining_certificates_pending(self):
        job_id = create_job(self.config["DATABASE"], payload("Ada Lovelace"), 50)

        class Boom(BaseException):  # not an Exception: simulates being killed mid-run
            pass

        def killer(path, **kwargs):
            raise Boom()

        with self.assertRaises(Boom):
            process_job(self.config["DATABASE"], self.config["STORAGE_DIR"], job_id, killer)
        job = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(job["status"], "PROCESSING")
        self.assertEqual(job["progress"]["pending"], 1)  # resumable


class RetrievalTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["CERT_RENDERER"] = failing_for("Grace Hopper")
        self.job = self.submit(payload("Ada Lovelace", "Grace Hopper", "Alan Turing"), 201).get_json()
        self.items = self.client.get(self.job["links"]["certificates"]).get_json()["items"]

    def test_download_single_certificate(self):
        resp = self.client.get(self.items[0]["download_url"])
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/pdf")
        self.assertTrue(resp.data.startswith(b"%PDF"))
        self.assertIn(b"(Ada Lovelace) Tj", resp.data)
        self.assertIn("Ada_Lovelace_CERT-", resp.headers["Content-Disposition"])

    def test_failed_certificate_cannot_be_downloaded(self):
        failed = self.items[1]
        resp = self.client.get(f"/api/jobs/{self.job['id']}/certificates/{failed['id']}")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"]["code"], "CERTIFICATE_FAILED")

    def test_unknown_certificate_and_wrong_job(self):
        self.assertEqual(self.client.get(f"/api/jobs/{self.job['id']}/certificates/nope").status_code, 404)
        other = self.submit(payload("Zed Zebra"), 201).get_json()
        cross = self.client.get(f"/api/jobs/{other['id']}/certificates/{self.items[0]['id']}")
        self.assertEqual(cross.status_code, 404)

    def test_missing_file_gives_410(self):
        path = os.path.join(self.config["STORAGE_DIR"], self.job["id"])
        for f in os.listdir(path):
            os.remove(os.path.join(path, f))
        self.assertEqual(self.client.get(self.items[0]["download_url"]).status_code, 410)

    def test_list_filter_by_status(self):
        failed = self.client.get(self.job["links"]["certificates"] + "?status=failed").get_json()
        self.assertEqual(failed["total"], 1)
        self.assertEqual(failed["items"][0]["name"], "Grace Hopper")
        self.assertEqual(self.client.get(self.job["links"]["certificates"] + "?status=bogus").status_code, 400)

    def test_list_pagination(self):
        page = self.client.get(self.job["links"]["certificates"] + "?per_page=2&page=2").get_json()
        self.assertEqual((page["total"], len(page["items"]), page["items"][0]["seq"]), (3, 1, 3))

    def test_download_all_as_zip_contains_only_generated(self):
        resp = self.client.get(self.job["links"]["download_all"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            names = zf.namelist()
            self.assertEqual(len(names), 2)
            self.assertTrue(any("Ada_Lovelace" in n for n in names))
            self.assertFalse(any("Grace" in n for n in names))
            self.assertTrue(zf.read(names[0]).startswith(b"%PDF"))

    def test_pending_certificate_and_unfinished_job_are_not_downloadable(self):
        job_id = create_job(self.config["DATABASE"], payload("Pending Person"), 50)  # never processed
        items = self.client.get(f"/api/jobs/{job_id}/certificates").get_json()["items"]
        resp = self.client.get(f"/api/jobs/{job_id}/certificates/{items[0]['id']}")
        self.assertEqual((resp.status_code, resp.get_json()["error"]["code"]), (409, "CERTIFICATE_NOT_READY"))
        zip_resp = self.client.get(f"/api/jobs/{job_id}/download")
        self.assertEqual((zip_resp.status_code, zip_resp.get_json()["error"]["code"]), (409, "JOB_NOT_FINISHED"))

    def test_zip_for_job_with_no_generated_certificates(self):
        job = self.submit({"event_name": "X", "recipients": [{"name": "", "email": ""}]}, 201).get_json()
        self.assertEqual(self.client.get(job["links"]["download_all"]).status_code, 404)
