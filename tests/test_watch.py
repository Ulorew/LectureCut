import contextlib
import hashlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import watch


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves one byte string, honouring Range like the disk's downloader."""

    body = b""
    honour_range = True
    served: list[str] = []

    def do_GET(self):  # noqa: N802 - the http.server interface
        start = 0
        header = self.headers.get("Range")
        if header and self.honour_range:
            start = int(header.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        RangeHandler.served.append(header or "")
        payload = self.body[start:]
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class Server:
    def __enter__(self):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_port}/file"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class LinkTests(unittest.TestCase):
    def test_share_links_become_the_form_the_api_takes(self):
        expected = "https://disk.yandex.ru/d/w5F2CSTinmZ8dw"
        for link in (
            "https://disk.360.yandex.ru/client/aa/d_w5F2CSTinmZ8dw",  # rejected by the API as it is
            "https://disk.yandex.ru/d/w5F2CSTinmZ8dw",
            "https://disk.360.yandex.ru/d/w5F2CSTinmZ8dw",
            "https://yadi.sk/d/w5F2CSTinmZ8dw",
        ):
            with self.subTest(link=link):
                self.assertEqual(watch.public_key(link), expected)

    def test_anything_else_is_refused(self):
        with self.assertRaises(watch.WatchError):
            watch.public_key("https://example.com/lectures")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp.name) / "lecture.MOV"
        RangeHandler.body = os.urandom(300_000)
        RangeHandler.honour_range = True
        RangeHandler.served = []
        self.sha = hashlib.sha256(RangeHandler.body).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, url, sha=None):
        with contextlib.redirect_stdout(io.StringIO()):
            watch.download(url, self.dest, size=len(RangeHandler.body), sha256=sha or self.sha)

    def test_a_whole_file_arrives_verified(self):
        with Server() as url:
            self.fetch(url)
        self.assertEqual(self.dest.read_bytes(), RangeHandler.body)
        self.assertFalse(self.dest.with_name("lecture.MOV.part").exists())

    def test_a_broken_download_resumes_where_it_stopped(self):
        part = self.dest.with_name("lecture.MOV.part")
        part.write_bytes(RangeHandler.body[:120_000])
        with Server() as url:
            self.fetch(url)
        self.assertEqual(RangeHandler.served, ["bytes=120000-"])
        self.assertEqual(self.dest.read_bytes(), RangeHandler.body)

    def test_a_server_ignoring_range_restarts_the_file(self):
        RangeHandler.honour_range = False
        self.dest.with_name("lecture.MOV.part").write_bytes(b"x" * 1000)
        with Server() as url:
            self.fetch(url)
        self.assertEqual(self.dest.read_bytes(), RangeHandler.body)

    def test_a_corrupt_file_is_thrown_away(self):
        with Server() as url, self.assertRaises(watch.WatchError):
            self.fetch(url, sha="0" * 64)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name("lecture.MOV.part").exists())


class WatchRunTests(unittest.TestCase):
    """add, then run, against a fake share and a fake pipeline."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.config_home = base / "config"
        self.to = base / "Lectures" / "RP"
        self.to.mkdir(parents=True)
        self.share = {
            "/Lectures": [self.item("/Lectures/Lecture_01.MOV")],
            "/Seminars": [],
        }
        self.bodies = {"/Lectures/Lecture_01.MOV": b"old lecture"}
        self.processed = []
        patches = [
            unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)}),
            unittest.mock.patch.object(watch, "api_get", self.fake_api),
            unittest.mock.patch.object(watch, "download", self.fake_download),
            unittest.mock.patch.object(watch.core, "main", self.fake_render),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.render_code = 0

    def tearDown(self):
        self.temp.cleanup()

    def item(self, path, body=None):
        body = body if body is not None else path.encode()
        return {"type": "file", "path": path, "name": Path(path).name, "media_type": "video",
                "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}

    def fake_api(self, url, params):
        if url.endswith("/download"):
            return {"href": "fake://" + params["path"]}
        items = self.share.get(params["path"], [])
        return {"_embedded": {"items": items[params["offset"]:], "total": len(items)}}

    def fake_download(self, href, dest, *, size, sha256):
        dest.write_bytes(self.bodies[href.removeprefix("fake://")])

    def fake_render(self, argv):
        source, output = Path(argv[0]), Path(argv[argv.index("-o") + 1])
        self.processed.append((source.name, argv[3:]))
        if self.render_code == 0:
            output.write_bytes(b"rendered " + source.read_bytes())
        return self.render_code

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = watch.main(list(argv))
        return code, out.getvalue()

    def add(self, *options):
        return self.cli("add", "https://disk.360.yandex.ru/client/aa/d_abc", "--to", str(self.to),
                        "--path", "/Lectures", "--path", "/Seminars", "--name", "rp", "--", *options)

    def state(self):
        return json.loads((self.config_home / "lecturecut" / "watch-state.json").read_text())

    def new_lecture(self, path="/Lectures/Lecture_02.MOV", body=b"new lecture"):
        self.share["/Lectures"].append(self.item(path, body))
        self.bodies[path] = body

    def test_what_is_there_when_added_is_left_alone(self):
        code, _ = self.add()
        self.assertEqual(code, 0)
        code, out = self.cli("run")
        self.assertEqual(code, 0)
        self.assertIn("nothing new", out)
        self.assertEqual(self.processed, [])

    def test_a_new_lecture_is_fetched_processed_and_its_raw_file_dropped(self):
        self.add("--max-fps=30", "--silence-bias=5")
        self.new_lecture()
        code, _ = self.cli("run")

        self.assertEqual(code, 0)
        self.assertEqual(self.processed, [("Lecture_02.MOV", ["--if-exists=suffix", "--max-fps=30", "--silence-bias=5"])])
        self.assertEqual((self.to / "Lecture_02_lecturecut.mp4").read_bytes(), b"rendered new lecture")
        self.assertFalse((self.to / ".incoming").exists())  # raw gone, folder gone
        entry = self.state()["rp"]["files"]["/Lectures/Lecture_02.MOV"]
        self.assertEqual(entry["status"], "done")

        code, out = self.cli("run")  # and it is not done twice
        self.assertIn("nothing new", out)
        self.assertEqual(len(self.processed), 1)

    def test_a_failed_render_keeps_the_raw_file_and_retries_without_downloading(self):
        self.add()
        self.new_lecture()
        self.render_code = 1
        code, out = self.cli("run")
        self.assertEqual(code, 1)
        self.assertTrue((self.to / ".incoming" / "Lecture_02.MOV").exists())
        self.assertEqual(self.state()["rp"]["files"]["/Lectures/Lecture_02.MOV"]["status"], "downloaded")

        self.render_code = 0
        with unittest.mock.patch.object(watch, "download", side_effect=AssertionError("downloaded again")):
            code, _ = self.cli("run")
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["rp"]["files"]["/Lectures/Lecture_02.MOV"]["status"], "done")

    def test_a_file_that_keeps_failing_is_given_up_after_three_tries(self):
        self.add()
        self.new_lecture()
        self.render_code = 1
        for _ in range(5):
            self.cli("run")
        self.assertEqual(len(self.processed), watch.MAX_ATTEMPTS)

    def test_an_unmounted_drive_is_skipped_and_nothing_is_created(self):
        self.add()
        self.new_lecture()
        moved = self.to.with_name("RP-gone")
        self.to.rename(moved)
        with unittest.mock.patch.object(watch.core, "mount_points", lambda: []):
            code, out = self.cli("run")
        self.assertIn("not available", out)
        self.assertFalse(self.to.exists())  # no folder made on the empty mount point
        self.assertEqual(self.processed, [])

    def test_a_drive_at_a_new_mount_point_is_followed(self):
        self.add()
        self.new_lecture()
        mount = Path(self.temp.name) / "mnt" / "sda3"
        new_home = mount / "Lectures" / "RP"
        new_home.parent.mkdir(parents=True)
        self.to.rename(new_home)
        with unittest.mock.patch.object(watch.core, "mount_points", lambda: [mount]):
            code, _ = self.cli("run")
        self.assertEqual(code, 0)
        self.assertTrue((new_home / "Lecture_02_lecturecut.mp4").exists())

    def test_a_reuploaded_recording_is_processed_again(self):
        self.add()
        self.new_lecture()
        self.cli("run")
        self.share["/Lectures"][-1] = self.item("/Lectures/Lecture_02.MOV", b"fixed upload")
        self.bodies["/Lectures/Lecture_02.MOV"] = b"fixed upload"
        self.cli("run")
        self.assertEqual(len(self.processed), 2)
        self.assertTrue((self.to / "Lecture_02_lecturecut.mp4").exists())

    def test_fetch_queues_known_files_by_name(self):
        self.add()
        code, _ = self.cli("fetch", "Lecture_01")
        self.assertEqual(code, 0)
        self.cli("run")
        self.assertEqual([name for name, _ in self.processed], ["Lecture_01.MOV"])

    def test_fetch_missing_skips_what_is_here_raw_or_processed(self):
        self.share["/Lectures"] += [self.item("/Lectures/Lecture_02.MOV"), self.item("/Lectures/Lecture_03.MOV")]
        self.bodies.update({"/Lectures/Lecture_02.MOV": b"two", "/Lectures/Lecture_03.MOV": b"three"})
        self.add()
        (self.to / "Lecture_01_lecturecut.mp4").write_bytes(b"done before")
        (self.to / "lecture_03.mov").write_bytes(b"raw, other case")
        self.cli("fetch", "--missing")
        self.cli("run")
        self.assertEqual([name for name, _ in self.processed], ["Lecture_02.MOV"])

    def test_fetch_needs_something_to_fetch(self):
        self.add()
        code, out = self.cli("fetch")
        self.assertEqual(code, 1)

    def test_options_lecturecut_would_reject_are_refused_when_added(self):
        code, out = self.add("--speed=inf")
        self.assertEqual(code, 1)
        self.assertFalse((self.config_home / "lecturecut" / "watch.json").exists())

    def test_a_second_run_while_one_is_going_steps_aside(self):
        self.add()
        lock_path = self.config_home / "lecturecut" / "watch.lock"
        with open(lock_path, "w") as held:
            watch.fcntl.flock(held, watch.fcntl.LOCK_EX)
            code, out = self.cli("run")
        self.assertEqual(code, 0)
        self.assertIn("Another lecturecut-watch is running", out)

    def test_not_enough_space_fails_before_downloading(self):
        self.add()
        self.new_lecture()
        tiny = unittest.mock.Mock(free=10)
        with unittest.mock.patch.object(watch.shutil, "disk_usage", return_value=tiny), \
                unittest.mock.patch.object(watch, "download", side_effect=AssertionError("downloaded")):
            code, out = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("GB free", out)


if __name__ == "__main__":
    unittest.main()
