import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import unittest.mock
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
        # The model cache is a real directory on the developer's machine; point it
        # somewhere disposable so a downloaded model cannot alter these results.
        cache = unittest.mock.patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(Path(self.temp.name) / "cache")}
        )
        cache.start()
        self.addCleanup(cache.stop)
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

    def test_listing_marks_processed_files(self):
        (self.root / "already_lecturecut.mp4").write_bytes(b"x")
        files = {entry["name"]: entry for entry in self.client.get("/api/files").json()["files"]}

        self.assertTrue(files["already_lecturecut.mp4"]["processed"])
        self.assertFalse(files["lecture.mp4"]["processed"])

    def test_processed_index_caches_by_size_and_mtime(self):
        calls = []

        def counting(path):
            calls.append(path)
            return False

        index = webui.ProcessedIndex()
        with unittest.mock.patch.object(webui.core, "is_already_processed", counting):
            index.flags([self.root / "lecture.mp4"])
            index.flags([self.root / "lecture.mp4"])
            self.assertEqual(len(calls), 1)

            # Re-rendering the file changes its size, which must invalidate.
            (self.root / "lecture.mp4").write_bytes(b"a different length entirely")
            index.flags([self.root / "lecture.mp4"])

        self.assertEqual(len(calls), 2)

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

    def test_schema_explains_the_denoisers_and_lists_models(self):
        (self.root / "sh.rnnn").write_bytes(b"model")
        body = self.client.get("/api/schema").json()

        self.assertEqual(set(body["denoise_help"]), set(main.AUDIO_DENOISE_MODES))
        self.assertEqual([m["name"] for m in body["models"]], ["sh.rnnn"])
        # The hint names the cache the models actually land in, per request.
        self.assertIn(str(Path(self.temp.name) / "cache"), body["model_hint"])

    def test_schema_carries_the_downloadable_catalogue(self):
        body = self.client.get("/api/schema").json()
        keys = [entry["key"] for entry in body["catalogue"]]

        self.assertEqual(set(keys), set(main.ARNNDN_MODELS))
        self.assertEqual(body["default_model"], main.ARNNDN_DEFAULT_MODEL)
        recommended = [e for e in body["catalogue"] if e["recommended"]]
        self.assertEqual([e["key"] for e in recommended], [main.ARNNDN_DEFAULT_MODEL])

    def test_fetching_a_model_reports_the_updated_lists(self):
        fetched = []

        def fake_download(model, dest_dir=None):
            fetched.append(model.name)
            target = self.root / model.file
            target.write_bytes(b"model")
            return target

        with unittest.mock.patch.object(
            webui.core, "download_arnndn_model", fake_download
        ):
            body = self.client.post("/api/models", json={}).json()

        default_file = main.ARNNDN_MODELS[main.ARNNDN_DEFAULT_MODEL].file
        self.assertEqual(fetched, [main.ARNNDN_DEFAULT_MODEL])
        self.assertIn(default_file, [m["name"] for m in body["models"]])

    def test_fetching_all_models_asks_for_every_one(self):
        fetched = []

        with unittest.mock.patch.object(
            webui.core,
            "download_arnndn_model",
            lambda model, dest_dir=None: (fetched.append(model.name), self.root / model.file)[1],
        ):
            self.client.post("/api/models", json={"keys": "all"})

        self.assertEqual(set(fetched), set(main.ARNNDN_MODELS))

    def test_fetching_an_unknown_model_is_a_400(self):
        response = self.client.post("/api/models", json={"keys": ["bogus"]})

        self.assertEqual(response.status_code, 400)

    def test_a_failed_download_is_a_502(self):
        def fail(model, dest_dir=None):
            raise main.PipelineError("network is down")

        with unittest.mock.patch.object(webui.core, "download_arnndn_model", fail):
            response = self.client.post("/api/models", json={})

        self.assertEqual(response.status_code, 502)
        self.assertIn("network is down", response.json()["detail"])

    def test_models_on_hand_describe_what_they_are_for(self):
        default = main.ARNNDN_MODELS[main.ARNNDN_DEFAULT_MODEL]
        (self.root / default.file).write_bytes(b"model")
        models = self.client.get("/api/schema").json()["models"]
        found = next(m for m in models if m["name"] == default.file)

        self.assertEqual((found["signal"], found["noise"]), (default.signal, default.noise))
        self.assertTrue(found["recommended"])

    def test_an_unknown_model_is_a_400_not_a_failed_job(self):
        response = self.client.post(
            "/api/jobs",
            json={
                "source": str(self.root / "lecture.mp4"),
                "settings": {"denoise": "arnndn", "arnndn_model": "/nope.rnnn"},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("sh", response.json()["detail"])
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])

    def test_accepting_a_job_never_downloads_a_model(self):
        def explode(*args, **kwargs):
            raise AssertionError("a request handler must not fetch a model")

        with unittest.mock.patch.object(webui.core, "download_arnndn_model", explode):
            response = self.client.post(
                "/api/jobs",
                json={
                    "source": str(self.root / "lecture.mp4"),
                    "settings": {"denoise": "arnndn"},
                },
            )

        # Accepted: the worker fetches the default model where its log is visible.
        self.assertEqual(response.status_code, 200)

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
class OutputAndOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        (self.root / "done").mkdir(parents=True)
        (self.root / ".hidden").mkdir()
        (self.root / "done" / "out.mp4").write_bytes(b"rendered")
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "other.mp4").write_bytes(b"nope")
        self.config = webui.Config(
            roots=[self.root.resolve()],
            upload_dir=(Path(self.temp.name) / "uploads").resolve(),
        )
        self.client = TestClient(webui.create_app(self.config))
        self.opened = []

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def fake_popen(self, command, **kwargs):
        self.opened.append(command)

        class Handle:
            pass

        return Handle()

    def test_dirs_offers_roots_and_their_subfolders(self):
        dirs = self.client.get("/api/dirs").json()["dirs"]
        paths = [entry["path"] for entry in dirs]

        self.assertEqual(paths[0], str(self.root.resolve()))
        self.assertTrue(dirs[0]["is_root"])
        self.assertIn(str((self.root / "done").resolve()), paths)
        self.assertFalse(any(".hidden" in path for path in paths))

    def test_open_hands_the_file_to_the_desktop(self):
        with unittest.mock.patch.object(webui.subprocess, "Popen", self.fake_popen):
            response = self.client.post(
                "/api/open", json={"path": str(self.root / "done" / "out.mp4")}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.opened), 1)
        self.assertIn(str(self.root / "done" / "out.mp4"), self.opened[0][-1])

    def test_open_can_reveal_the_containing_folder(self):
        with unittest.mock.patch.object(webui.subprocess, "Popen", self.fake_popen):
            self.client.post(
                "/api/open",
                json={"path": str(self.root / "done" / "out.mp4"), "reveal": True},
            )

        self.assertEqual(self.opened[0][-1], str((self.root / "done").resolve()))

    def test_open_refuses_paths_outside_the_roots(self):
        with unittest.mock.patch.object(webui.subprocess, "Popen", self.fake_popen):
            response = self.client.post(
                "/api/open", json={"path": str(self.outside / "other.mp4")}
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.opened, [])

    def test_open_can_be_disabled(self):
        self.config.allow_open = False
        with unittest.mock.patch.object(webui.subprocess, "Popen", self.fake_popen):
            response = self.client.post(
                "/api/open", json={"path": str(self.root / "done" / "out.mp4")}
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.opened, [])

    def test_schema_advertises_whether_opening_is_allowed(self):
        self.assertTrue(self.client.get("/api/schema").json()["allow_open"])

    def test_download_serves_a_result(self):
        response = self.client.get(
            "/api/file", params={"path": str(self.root / "done" / "out.mp4")}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"rendered")

    def test_download_refuses_paths_outside_the_roots(self):
        response = self.client.get(
            "/api/file", params={"path": str(self.outside / "other.mp4")}
        )

        self.assertEqual(response.status_code, 403)

    def test_desktop_open_command_per_platform(self):
        with unittest.mock.patch.object(webui.sys, "platform", "linux"):
            self.assertEqual(webui.desktop_open_command(Path("/x"))[0], "xdg-open")
        with unittest.mock.patch.object(webui.sys, "platform", "darwin"):
            self.assertEqual(webui.desktop_open_command(Path("/x"))[0], "open")
        with unittest.mock.patch.object(webui.sys, "platform", "win32"):
            self.assertEqual(webui.desktop_open_command(Path("/x"))[0], "explorer")


@unittest.skipIf(TestClient is None, "install the web extra to run these tests")
class QueueTests(unittest.TestCase):
    """Several runs may be queued; one worker drains them in order."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            (self.root / name).write_bytes(b"x")
        self.config = webui.Config(
            roots=[self.root.resolve()], upload_dir=(self.root / "up").resolve()
        )
        self.client = TestClient(webui.create_app(self.config))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_output_dir_lets_the_core_name_each_file(self):
        response = self.client.post(
            "/api/jobs",
            json={
                "source": str(self.root / "a.mp4"),
                "settings": {},
                "output_dir": str(self.root),
            },
        )

        self.assertEqual(response.status_code, 200)
        job = self.client.get(f"/api/jobs/{response.json()['id']}").json()
        self.assertEqual(
            job["settings"]["output"], str(self.root / "a_lecturecut.mp4")
        )

    def test_output_dir_is_refused_outside_the_roots(self):
        response = self.client.post(
            "/api/jobs",
            json={
                "source": str(self.root / "a.mp4"),
                "settings": {},
                "output_dir": "/etc",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_an_explicit_output_wins_over_the_folder(self):
        response = self.client.post(
            "/api/jobs",
            json={
                "source": str(self.root / "a.mp4"),
                "settings": {"output": str(self.root / "chosen.mp4")},
                "output_dir": str(self.root),
            },
        )
        job = self.client.get(f"/api/jobs/{response.json()['id']}").json()

        self.assertEqual(job["settings"]["output"], str(self.root / "chosen.mp4"))

    def test_multiple_jobs_queue_up_and_are_all_listed(self):
        ids = []
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            response = self.client.post(
                "/api/jobs", json={"source": str(self.root / name), "settings": {}}
            )
            self.assertEqual(response.status_code, 200)
            ids.append(response.json()["id"])

        listed = self.client.get("/api/jobs").json()["jobs"]
        self.assertEqual(len(listed), 3)
        # newest first, so the listing is the reverse of submission order
        self.assertEqual([job["id"] for job in listed], list(reversed(ids)))
        self.assertEqual(len(set(ids)), 3)

    def test_events_stream_closes_for_a_finished_job(self):
        job_id = self.client.post(
            "/api/jobs", json={"source": str(self.root / "a.mp4"), "settings": {}}
        ).json()["id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/jobs/{job_id}").json()["status"]
            if status in {"done", "error", "cancelled"}:
                break
            time.sleep(0.05)

        # Without an explicit close this request would hang on keepalives.
        with self.client.stream("GET", f"/api/jobs/{job_id}/events") as response:
            body = "".join(response.iter_text())

        self.assertIn("status", body)


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
