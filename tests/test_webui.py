import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - the web extra is optional
    TestClient = None

import main

if TestClient is not None:
    import webui


@unittest.skipIf(TestClient is None, "install the web extra to run these tests")
class WebUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        (self.root / "lecture.mp4").write_bytes(b"not really video")
        (self.root / "notes.txt").write_text("ignored")
        (self.outside / "secret.mp4").write_bytes(b"nope")
        self.config = webui.Config(
            roots=[self.root.resolve()],
            upload_dir=(Path(self.temp.name) / "uploads").resolve(),
        )
        self.app = webui.create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_schema_comes_from_the_cli_parser(self):
        body = self.client.get("/api/schema").json()
        dests = {
            field["dest"]
            for group in body["groups"]
            for field in group["fields"]
        }

        self.assertIn("target_lufs", dests)
        self.assertIn("denoise", dests)
        self.assertEqual(body["roots"], [str(self.root.resolve())])

    def test_file_listing_only_offers_media_from_the_roots(self):
        files = self.client.get("/api/files").json()["files"]
        names = [entry["name"] for entry in files]

        self.assertEqual(names, ["lecture.mp4"])
        self.assertNotIn("notes.txt", names)
        self.assertNotIn("secret.mp4", names)

    def test_paths_outside_the_roots_are_refused(self):
        response = self.client.get(
            "/api/probe", params={"path": str(self.outside / "secret.mp4")}
        )

        self.assertEqual(response.status_code, 403)

    def test_traversal_out_of_a_root_is_refused(self):
        response = self.client.get(
            "/api/probe",
            params={"path": str(self.root / ".." / "outside" / "secret.mp4")},
        )

        self.assertEqual(response.status_code, 403)

    def test_missing_file_is_a_404(self):
        response = self.client.get(
            "/api/probe", params={"path": str(self.root / "absent.mp4")}
        )

        self.assertEqual(response.status_code, 404)

    def test_job_creation_rejects_a_source_outside_the_roots(self):
        response = self.client.post(
            "/api/jobs",
            json={"source": str(self.outside / "secret.mp4"), "settings": {}},
        )

        self.assertEqual(response.status_code, 403)

    def test_job_creation_rejects_unknown_settings(self):
        response = self.client.post(
            "/api/jobs",
            json={"source": str(self.root / "lecture.mp4"), "settings": {"nope": 1}},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("nope", response.json()["detail"])

    def test_job_creation_rejects_values_argparse_would_reject(self):
        with contextlib.redirect_stderr(io.StringIO()):
            response = self.client.post(
                "/api/jobs",
                json={"source": str(self.root / "lecture.mp4"), "settings": {"speed": -1}},
            )

        self.assertEqual(response.status_code, 400)

    def test_job_creation_requires_a_source(self):
        response = self.client.post("/api/jobs", json={"settings": {}})

        self.assertEqual(response.status_code, 400)

    def test_upload_streams_into_the_upload_directory(self):
        response = self.client.post(
            "/api/upload", params={"name": "dropped.mp4"}, content=b"payload"
        )

        self.assertEqual(response.status_code, 200)
        target = self.config.upload_dir / "dropped.mp4"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"payload")

    def test_upload_strips_directory_components_from_the_name(self):
        self.client.post(
            "/api/upload", params={"name": "../escape.mp4"}, content=b"x"
        )

        self.assertTrue((self.config.upload_dir / "escape.mp4").exists())
        self.assertFalse((Path(self.temp.name) / "escape.mp4").exists())

    def test_upload_can_be_disabled(self):
        self.config.allow_upload = False
        response = self.client.post(
            "/api/upload", params={"name": "x.mp4"}, content=b"x"
        )

        self.assertEqual(response.status_code, 403)


@unittest.skipIf(TestClient is None, "install the web extra to run these tests")
class JobLifecycleTests(unittest.TestCase):
    """Drive the manager directly: no ffmpeg, just the state machine."""

    def setUp(self):
        self.manager = webui.JobManager()

    def test_a_failing_job_reports_the_pipeline_error(self):
        job = self.manager.submit(
            kind="convert", source="/definitely/missing.mp4", settings={}
        )
        self.wait_for(job, {webui.STATUS_ERROR})

        self.assertEqual(job.status, webui.STATUS_ERROR)
        self.assertIn("does not exist", job.error)
        self.assertTrue(
            any(event["type"] == "line" and event["error"] for event in job.events)
        )

    def test_cancelling_before_the_worker_starts_skips_the_work(self):
        job = webui.Job(id="x", kind="convert", source="/missing.mp4", settings={})
        job.cancel.set()
        self.manager._jobs[job.id] = job
        self.manager._order.append(job.id)
        self.manager._run_job(job)

        self.assertEqual(job.status, webui.STATUS_CANCELLED)

    def test_cancel_is_a_noop_once_finished(self):
        job = self.manager.submit(
            kind="convert", source="/definitely/missing.mp4", settings={}
        )
        self.wait_for(job, {webui.STATUS_ERROR})
        self.manager.cancel(job.id)

        self.assertEqual(job.status, webui.STATUS_ERROR)

    def test_events_replay_for_a_late_subscriber(self):
        job = self.manager.submit(
            kind="convert", source="/definitely/missing.mp4", settings={}
        )
        self.wait_for(job, {webui.STATUS_ERROR})
        _, channel, history = self.manager.subscribe(job.id)

        self.assertTrue(history)
        self.assertEqual(history[0]["type"], "status")

    def test_jobs_do_not_share_a_reporter(self):
        first = self.manager.submit(
            kind="convert", source="/missing-one.mp4", settings={}
        )
        self.wait_for(first, {webui.STATUS_ERROR})
        second = self.manager.submit(
            kind="convert", source="/missing-two.mp4", settings={}
        )
        self.wait_for(second, {webui.STATUS_ERROR})

        first_lines = [e for e in first.events if e["type"] == "line"]
        second_lines = [e for e in second.events if e["type"] == "line"]
        self.assertTrue(all("missing-one" in e["text"] for e in first_lines))
        self.assertTrue(all("missing-two" in e["text"] for e in second_lines))

    def wait_for(self, job, statuses, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job.status in statuses:
                return
            time.sleep(0.02)
        raise AssertionError(f"job stayed in {job.status}")


class SseFormatTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "install the web extra to run these tests")
    def test_sse_frames_are_json_lines(self):
        frame = webui.sse({"type": "line", "text": "привет"})

        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(
            json.loads(frame[len("data: ") :].strip())["text"], "привет"
        )


if __name__ == "__main__":
    unittest.main()
