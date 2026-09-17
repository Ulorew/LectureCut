import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
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
class SourceFolderTests(unittest.TestCase):
    """Choosing the input folder from the page, and remembering the choice."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.project = base / "project"
        self.project.mkdir()
        (self.project / "old.mp4").write_bytes(b"x")
        self.downloads = base / "Downloads"
        (self.downloads / "course" / "week1").mkdir(parents=True)
        (self.downloads / ".cache").mkdir()
        (self.downloads / "lecture03.MOV").write_bytes(b"x")
        (self.downloads / "course" / "week1" / "lecture04.mp4").write_bytes(b"x")
        (self.downloads / "notes.pdf").write_bytes(b"x")
        self.state = base / "config" / "webui.json"
        self.config = webui.Config(
            roots=[self.project.resolve()],
            upload_dir=(base / "uploads").resolve(),
            state_path=self.state,
        )
        self.client = TestClient(webui.create_app(self.config))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def names(self):
        return sorted(f["name"] for f in self.client.get("/api/files").json()["files"])

    def test_browse_lists_folders_and_counts_media(self):
        body = self.client.get("/api/browse", params={"path": str(self.downloads)}).json()

        self.assertEqual([d["name"] for d in body["dirs"]], ["course"])
        self.assertEqual(body["media_here"], 1)
        self.assertEqual(body["parent"], str(self.downloads.resolve().parent))

    def test_browse_hides_hidden_folders(self):
        body = self.client.get("/api/browse", params={"path": str(self.downloads)}).json()

        self.assertNotIn(".cache", [d["name"] for d in body["dirs"]])

    def test_browse_rejects_missing_paths_and_files(self):
        missing = self.client.get("/api/browse", params={"path": str(self.downloads / "nope")})
        a_file = self.client.get(
            "/api/browse", params={"path": str(self.downloads / "lecture03.MOV")}
        )

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(a_file.status_code, 400)

    def test_choosing_a_folder_switches_the_listing(self):
        self.assertEqual(self.names(), ["old.mp4"])

        response = self.client.post("/api/source-dir", json={"path": str(self.downloads)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.names(), ["lecture03.MOV", "lecture04.mp4"])

    def test_a_chosen_folder_becomes_readable(self):
        target = self.downloads / "lecture03.MOV"
        before = self.client.get("/api/probe", params={"path": str(target)})
        self.client.post("/api/source-dir", json={"path": str(self.downloads)})
        after = self.client.get("/api/file", params={"path": str(target)})

        self.assertEqual(before.status_code, 403)
        self.assertEqual(after.status_code, 200)

    def test_the_choice_survives_a_restart(self):
        self.client.post("/api/source-dir", json={"path": str(self.downloads)})

        reborn = webui.Config(
            roots=[self.project.resolve()],
            upload_dir=self.config.upload_dir,
            state_path=self.state,
        )
        reborn.load_state()

        self.assertEqual(reborn.current_source_dir(), self.downloads.resolve())
        self.assertIn(self.downloads.resolve(), reborn.roots)

    def test_recent_folders_are_deduplicated_newest_first(self):
        for folder in (self.downloads, self.project, self.downloads):
            self.client.post("/api/source-dir", json={"path": str(folder)})

        recent = self.client.get("/api/schema").json()["recent_source_dirs"]

        self.assertEqual(recent, [str(self.downloads.resolve()), str(self.project.resolve())])

    def test_a_vanished_folder_is_forgotten_on_restart(self):
        self.client.post("/api/source-dir", json={"path": str(self.downloads)})
        shutil.rmtree(self.downloads)

        reborn = webui.Config(
            roots=[self.project.resolve()],
            upload_dir=self.config.upload_dir,
            state_path=self.state,
        )
        reborn.load_state()

        self.assertEqual(reborn.current_source_dir(), self.project.resolve())
        self.assertEqual(reborn.recent_source_dirs, [])

    def test_browsing_can_be_disabled(self):
        self.config.allow_browse = False

        browse = self.client.get("/api/browse", params={"path": str(self.downloads)})
        choose = self.client.post("/api/source-dir", json={"path": str(self.downloads)})

        self.assertEqual(browse.status_code, 403)
        self.assertEqual(choose.status_code, 403)
        self.assertEqual(self.names(), ["old.mp4"])

    def test_listing_is_bounded_in_depth(self):
        deep = self.downloads / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "too_deep.mp4").write_bytes(b"x")
        self.client.post("/api/source-dir", json={"path": str(self.downloads)})

        self.assertNotIn("too_deep.mp4", self.names())

    def test_walk_limited_stops_at_its_limit(self):
        for index in range(10):
            (self.project / f"clip{index}.mp4").write_bytes(b"x")

        found = list(
            webui.walk_limited(self.project, max_depth=1, limit=4, want_dirs=False)
        )

        self.assertEqual(len(found), 4)


@unittest.skipIf(TestClient is None, "install the web extra to run these tests")
class DroppedFileTests(unittest.TestCase):
    """A dropped file arrives without its path; find it instead of copying it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.here = base / "here"
        self.elsewhere = base / "elsewhere"
        for folder in (self.here, self.elsewhere):
            folder.mkdir()
        (self.here / "near.mp4").write_bytes(b"a" * 10)
        (self.elsewhere / "far.mp4").write_bytes(b"b" * 25)
        (self.elsewhere / "same_name.mp4").write_bytes(b"c" * 30)
        self.config = webui.Config(
            roots=[self.here.resolve(), self.elsewhere.resolve()],
            upload_dir=(base / "uploads").resolve(),
            source_dir=self.here.resolve(),
        )
        self.client = TestClient(webui.create_app(self.config))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def locate(self, name, size):
        return self.client.get("/api/locate", params={"name": name, "size": size}).json()

    def test_a_file_in_the_current_folder_is_found(self):
        found = self.locate("near.mp4", 10)

        self.assertTrue(found["found"])
        self.assertEqual(found["path"], str(self.here / "near.mp4"))

    def test_a_file_in_another_known_folder_is_found_with_its_folder(self):
        found = self.locate("far.mp4", 25)

        self.assertTrue(found["found"])
        self.assertEqual(found["dir"], str(self.elsewhere))

    def test_the_size_has_to_match_too(self):
        self.assertFalse(self.locate("same_name.mp4", 31)["found"])
        self.assertTrue(self.locate("same_name.mp4", 30)["found"])

    def test_an_unknown_file_is_simply_not_found(self):
        self.assertFalse(self.locate("nowhere.mp4", 7)["found"])

    def test_a_path_in_the_name_cannot_escape(self):
        self.assertFalse(self.locate("../../etc/passwd", 10)["found"])

    def test_upload_says_where_it_put_the_file(self):
        body = self.client.post(
            "/api/upload", params={"name": "dropped.mp4"}, content=b"payload"
        ).json()

        # The page switches to that folder, or the upload would stay invisible.
        self.assertEqual(body["dir"], str(self.config.upload_dir))
        self.assertEqual(body["path"], str(self.config.upload_dir / "dropped.mp4"))


@unittest.skipIf(TestClient is None, "install the web extra to run these tests")
class CachingTests(unittest.TestCase):
    """The page and its script must never be served from a browser's cache."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "a.mp4").write_bytes(b"x")
        self.config = webui.Config(roots=[root.resolve()], upload_dir=(root / "up").resolve())
        self.client = TestClient(webui.create_app(self.config))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_the_page_and_its_assets_are_not_cached(self):
        for path in ("/", "/app.js", "/app.css"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response.headers.get("cache-control", ""))

    def test_api_responses_keep_their_own_headers(self):
        # /api/file serves finished videos, where ranges and caching matter.
        response = self.client.get("/api/schema")

        self.assertNotIn("no-store", response.headers.get("cache-control", ""))


@unittest.skipIf(TestClient is None, "install the web extra to run these tests")
class RequestGuardTests(unittest.TestCase):
    """The server must not be drivable from another site."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "a.mp4").write_bytes(b"x")
        self.config = webui.Config(
            roots=[root.resolve()],
            upload_dir=(root / "up").resolve(),
            allowed_hosts=webui.allowed_hosts_for("127.0.0.1", 8765),
            require_csrf_header=True,
        )
        self.app = webui.create_app(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def client(self, host="127.0.0.1:8765"):
        return TestClient(self.app, base_url=f"http://{host}")

    def test_a_rebound_hostname_is_refused(self):
        with self.client(host="evil.example:8765") as client:
            response = client.get("/api/files")

        self.assertEqual(response.status_code, 403)

    def test_loopback_names_are_accepted(self):
        for host in ("127.0.0.1:8765", "localhost:8765"):
            with self.client(host=host) as client:
                self.assertEqual(client.get("/api/files").status_code, 200)

    def test_a_post_without_the_header_is_refused(self):
        with self.client() as client:
            response = client.post("/api/models", json={"keys": ["bogus"]})

        self.assertEqual(response.status_code, 403)

    def test_a_post_with_the_header_goes_through(self):
        with self.client() as client:
            response = client.post(
                "/api/models", json={"keys": ["bogus"]}, headers={webui.CSRF_HEADER: "1"}
            )

        # 400 comes from the handler itself, so the guard let it through.
        self.assertEqual(response.status_code, 400)

    def test_reads_do_not_need_the_header(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/schema").status_code, 200)

    def test_schema_tells_the_page_which_header_to_send(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/schema").json()["csrf_header"], webui.CSRF_HEADER)

    def test_a_wildcard_bind_turns_the_host_check_off(self):
        self.assertIsNone(webui.allowed_hosts_for("0.0.0.0", 8765))
        self.assertIn("localhost:8765", webui.allowed_hosts_for("127.0.0.1", 8765))


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
class LivePreviewTests(unittest.TestCase):
    """Watching a job while it renders."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        (self.root / "a.mp4").write_bytes(b"x")
        cache = unittest.mock.patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(Path(self.temp.name) / "cache")}
        )
        cache.start()
        self.addCleanup(cache.stop)
        self.config = webui.Config(
            roots=[self.root.resolve()], upload_dir=(self.root / "up").resolve()
        )
        self.manager = webui.JobManager()
        self.client = TestClient(webui.create_app(self.config, self.manager))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def create(self, settings=None):
        return self.client.post(
            "/api/jobs",
            json={"source": str(self.root / "a.mp4"), "settings": settings or {}},
        ).json()

    def test_a_job_gets_a_playlist_folder_of_its_own(self):
        job = self.client.get(f"/api/jobs/{self.create()['id']}").json()

        self.assertTrue(job["settings"]["live_dir"].startswith(str(webui.live_root())))
        # The pipeline must not delete segments a viewer may still be reading.
        self.assertTrue(job["settings"]["keep_live_dir"])

    def test_the_page_cannot_choose_where_segments_are_written(self):
        job = self.client.get(
            f"/api/jobs/{self.create({'live_dir': '/etc', 'keep_live_dir': True})['id']}"
        ).json()

        self.assertNotEqual(job["settings"]["live_dir"], "/etc")
        self.assertTrue(job["settings"]["live_dir"].startswith(str(webui.live_root())))

    def test_no_preview_for_a_codec_browsers_cannot_play(self):
        job = self.client.get(f"/api/jobs/{self.create({'encoder': 'hevc_nvenc'})['id']}").json()

        self.assertNotIn("live_dir", job["settings"])
        self.assertFalse(job["live"])

    def test_previews_can_be_turned_off(self):
        self.config.live_preview = False
        job = self.client.get(f"/api/jobs/{self.create()['id']}").json()

        self.assertNotIn("live_dir", job["settings"])

    def staged_job(self):
        folder = Path(self.temp.name) / "live"
        folder.mkdir()
        job = self.manager.submit(
            kind="convert", source="/missing.mp4", settings={}, live_dir=folder
        )
        return job, folder

    def test_a_job_is_watchable_only_once_the_playlist_exists(self):
        job, folder = self.staged_job()

        self.assertFalse(job.summary()["live"])
        (folder / "index.m3u8").write_text("#EXTM3U")
        self.assertTrue(job.summary()["live"])

    def test_the_playlist_is_served_without_caching(self):
        job, folder = self.staged_job()
        (folder / "index.m3u8").write_text("#EXTM3U")

        response = self.client.get(f"/api/jobs/{job.id}/live/index.m3u8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "#EXTM3U")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_segments_are_served(self):
        job, folder = self.staged_job()
        (folder / "seg_00007.m4s").write_bytes(b"segment")

        response = self.client.get(f"/api/jobs/{job.id}/live/seg_00007.m4s")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"segment")

    def test_only_playlist_names_are_served(self):
        job, folder = self.staged_job()
        (folder / "secret.txt").write_text("nope")
        (folder / "seg_00001.m4s.tmp").write_text("half written")

        for name in ("secret.txt", "seg_00001.m4s.tmp", "..%2f..%2fetc%2fpasswd"):
            with self.subTest(name=name):
                response = self.client.get(f"/api/jobs/{job.id}/live/{name}")
                self.assertEqual(response.status_code, 404)

    def test_a_missing_file_is_a_404_not_a_crash(self):
        job, _ = self.staged_job()

        self.assertEqual(
            self.client.get(f"/api/jobs/{job.id}/live/index.m3u8").status_code, 404
        )

    def test_segments_are_cleared_after_a_grace_period(self):
        job, folder = self.staged_job()
        (folder / "index.m3u8").write_text("#EXTM3U")
        scheduled = []

        class FakeTimer:
            def __init__(self, delay, action):
                scheduled.append((delay, action))
                self.daemon = True

            def start(self):
                pass

        with unittest.mock.patch.object(webui.threading, "Timer", FakeTimer):
            self.manager._finish(job, webui.STATUS_DONE)

        self.assertEqual(scheduled[0][0], webui.PREVIEW_GRACE_SECONDS)
        self.assertTrue(folder.exists())  # not yet: a viewer may still be watching
        scheduled[0][1]()
        self.assertFalse(folder.exists())

    def test_stale_folders_from_a_crash_are_purged_on_start(self):
        root = webui.live_root()
        old, fresh = root / "old", root / "fresh"
        for folder in (old, fresh):
            folder.mkdir(parents=True)
            (folder / "index.m3u8").write_text("x")
        os.utime(old, (0, 0))

        webui.purge_stale_previews()

        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


class TimeEstimateTests(unittest.TestCase):
    """How long a job, and the whole queue, still has to go."""

    def model(self, **values):
        return webui.ThroughputModel(**values)

    def job(self, **values):
        base = {"id": "j", "kind": "convert", "source": "/in.mp4", "settings": {}}
        base.update(values)
        return webui.Job(**base)

    def test_a_queued_job_is_estimated_from_its_length(self):
        model = self.model(analysis_seconds=40, silence_rate=0.03, render_rate=0.3)
        job = self.job(input_duration=1000.0)

        # 40 s of analysis + 30 s of silence detection + 300 s of rendering
        self.assertAlmostEqual(job.remaining_seconds(model, now=0.0), 370.0)

    def test_skipped_stages_cost_nothing(self):
        model = self.model(analysis_seconds=40, silence_rate=0.03, render_rate=0.3)
        job = self.job(
            input_duration=1000.0, settings={"no_analyze": True, "no_cut_silence": True}
        )

        self.assertAlmostEqual(job.remaining_seconds(model, now=0.0), 300.0)

    def test_an_unknown_length_gives_no_estimate(self):
        self.assertIsNone(self.job().remaining_seconds(self.model(), now=0.0))

    def test_a_running_render_is_extrapolated_from_its_own_pace(self):
        # The model thinks rendering takes 300 s, but this one is going twice as
        # fast: 25% done after 37.5 s. Its own pace is what should count.
        model = self.model(render_rate=0.3)
        job = self.job(input_duration=1000.0, status=webui.STATUS_RUNNING)
        job.enter_phase(main.PHASE_RENDER, now=100.0)
        job.phase_fraction = 0.25

        self.assertAlmostEqual(job.remaining_seconds(model, now=137.5), 112.5)

    def test_too_early_in_a_phase_the_model_is_used_instead(self):
        model = self.model(render_rate=0.3)
        job = self.job(input_duration=1000.0, status=webui.STATUS_RUNNING)
        job.enter_phase(main.PHASE_RENDER, now=100.0)
        job.phase_fraction = 0.01

        self.assertAlmostEqual(job.remaining_seconds(model, now=101.0), 299.0)

    def test_during_analysis_the_later_stages_are_added(self):
        model = self.model(analysis_seconds=40, silence_rate=0.03, render_rate=0.3)
        job = self.job(input_duration=1000.0, status=webui.STATUS_RUNNING)
        job.enter_phase(main.PHASE_MEASURE, now=0.0)
        job.enter_phase(main.PHASE_CALIBRATE, now=15.0)  # same bucket, same clock

        self.assertAlmostEqual(job.remaining_seconds(model, now=25.0), 15.0 + 30.0 + 300.0)

    def test_a_finished_job_has_nothing_left(self):
        job = self.job(input_duration=1000.0, status=webui.STATUS_DONE)

        self.assertEqual(job.remaining_seconds(self.model(), now=0.0), 0.0)

    def test_bucket_time_is_accounted_across_phases(self):
        job = self.job(input_duration=1000.0, status=webui.STATUS_RUNNING)
        job.enter_phase(main.PHASE_MEASURE, now=0.0)
        job.enter_phase(main.PHASE_CALIBRATE, now=10.0)
        job.enter_phase(main.PHASE_SILENCE, now=35.0)
        job.enter_phase(main.PHASE_RENDER, now=65.0)
        job.close_bucket(now=365.0)

        self.assertEqual(
            job.bucket_seconds,
            {webui.ANALYSIS_BUCKET: 35.0, webui.SILENCE_BUCKET: 30.0, webui.RENDER_BUCKET: 300.0},
        )

    def test_the_first_finished_job_replaces_the_defaults(self):
        model = self.model()
        model.learn(
            {webui.ANALYSIS_BUCKET: 50.0, webui.SILENCE_BUCKET: 100.0, webui.RENDER_BUCKET: 2000.0},
            duration=5000.0,
        )

        self.assertAlmostEqual(model.analysis_seconds, 50.0)
        self.assertAlmostEqual(model.silence_rate, 0.02)
        self.assertAlmostEqual(model.render_rate, 0.4)

    def test_later_jobs_are_averaged_in_but_never_ignored(self):
        model = self.model(render_rate=0.4, samples=20)
        model.learn({webui.RENDER_BUCKET: 1000.0}, duration=1000.0)  # this one took 1.0 s/s

        # With many samples the weight bottoms out at a quarter, so a new kind of
        # source still moves the estimate noticeably.
        self.assertAlmostEqual(model.render_rate, 0.4 + 0.25 * (1.0 - 0.4))

    def test_the_model_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "throughput.json"
            model = webui.ThroughputModel.load(path)
            model.learn({webui.RENDER_BUCKET: 900.0}, duration=3000.0)

            reloaded = webui.ThroughputModel.load(path)

            self.assertAlmostEqual(reloaded.render_rate, 0.3)
            self.assertEqual(reloaded.samples, 1)

    def test_a_damaged_model_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "throughput.json"
            path.write_text("{not json")

            self.assertEqual(webui.ThroughputModel.load(path).render_rate, webui.DEFAULT_RENDER_RATE)

    def manager_with(self, jobs, **model):
        manager = webui.JobManager.__new__(webui.JobManager)
        manager.model = self.model(**model)
        manager._lock = threading.Lock()
        manager._jobs = {job.id: job for job in jobs}
        manager._order = [job.id for job in jobs]
        return manager

    def test_a_queued_job_finishes_after_everything_ahead_of_it(self):
        manager = self.manager_with(
            [
                self.job(id="done", input_duration=500.0, status=webui.STATUS_DONE),
                self.job(id="first", input_duration=100.0),
                self.job(id="second", input_duration=300.0),
                self.job(id="third", input_duration=200.0),
            ],
            analysis_seconds=0, silence_rate=0, render_rate=0.5,
        )

        finishes = manager.finish_times(now=0.0)

        self.assertNotIn("done", finishes)
        self.assertEqual(finishes, {"first": 50.0, "second": 200.0, "third": 300.0})

    def test_an_unknown_job_hides_the_finish_of_those_behind_it(self):
        manager = self.manager_with(
            [
                self.job(id="first", input_duration=100.0),
                self.job(id="mystery"),
                self.job(id="last", input_duration=100.0),
            ],
            analysis_seconds=0, silence_rate=0, render_rate=0.5,
        )

        finishes = manager.finish_times(now=0.0)

        self.assertEqual(finishes["first"], 50.0)
        self.assertIsNone(finishes["mystery"])
        self.assertIsNone(finishes["last"])

    def test_listing_carries_both_figures(self):
        manager = self.manager_with(
            [self.job(id="first", input_duration=100.0), self.job(id="second", input_duration=100.0)],
            analysis_seconds=0, silence_rate=0, render_rate=0.5,
        )

        rows = {row["id"]: row for row in manager.listing()}

        self.assertEqual(rows["second"]["remaining_seconds"], 50.0)
        self.assertEqual(rows["second"]["finishes_in_seconds"], 100.0)

    def test_the_queue_total_adds_up_and_counts_unknowns(self):
        manager = webui.JobManager.__new__(webui.JobManager)
        manager.model = self.model(analysis_seconds=0, silence_rate=0, render_rate=0.5)
        manager._lock = threading.Lock()
        manager._jobs = {
            "a": self.job(id="a", input_duration=100.0),
            "b": self.job(id="b", input_duration=300.0),
            "c": self.job(id="c"),  # length unknown
            "d": self.job(id="d", input_duration=900.0, status=webui.STATUS_DONE),
        }
        manager._order = ["a", "b", "c", "d"]

        queue = manager.queue_remaining()

        self.assertEqual(queue["pending"], 3)
        self.assertAlmostEqual(queue["remaining_seconds"], 200.0)
        self.assertEqual(queue["unknown"], 1)


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
