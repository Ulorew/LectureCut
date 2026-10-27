"""Fetch new lectures from shared Yandex Disk folders and process them.

    lecturecut-watch add LINK --to DIR [--path /Lectures ...] [-- lecturecut options]
    lecturecut-watch list
    lecturecut-watch fetch [--missing] [NAME ...]
    lecturecut-watch run [--dry-run]
    lecturecut-watch install-timer

A watch is a public folder link and a local folder. What the link already holds
when the watch is added counts as known; files that appear later are downloaded
into DIR/.incoming, verified against the sha256 the disk reports, processed with
the watch's own options into DIR, and the raw download is then deleted. A failed
step keeps the raw file and is retried on the next run, up to MAX_ATTEMPTS.

Standard library only, like the command line tool: the timer runs this without
the web extra.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import main as core

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
INCOMING = ".incoming"
MAX_ATTEMPTS = 3
CHUNK = 8 * 1024 * 1024
# Room to leave on the drive beyond the download itself: the render writes
# its result next to it before the raw file can go.
SPACE_MARGIN = 2 * 1024**3
VIDEO_SUFFIXES = {".mov", ".mp4", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".m2ts"}
TIMER_NAME = "lecturecut-watch"

# Share links come in several forms; the public API takes only the /d/ one.
LINK_PATTERNS = (
    re.compile(r"/client/[^/]+/d_([A-Za-z0-9_-]+)"),  # disk.360.yandex.ru/client/aa/d_<id>
    re.compile(r"/d/([A-Za-z0-9_-]+)"),  # disk.yandex.ru/d/<id>, disk.360..., yadi.sk/d/<id>
)


class WatchError(Exception):
    pass


def public_key(link: str) -> str:
    """The canonical public link the API accepts.

    Yandex 360 hands out https://disk.360.yandex.ru/client/aa/d_<id>, which the
    API rejects as "Invalid public key format"; the same share answers as
    https://disk.yandex.ru/d/<id>.
    """

    for pattern in LINK_PATTERNS:
        found = pattern.search(link)
        if found:
            return f"https://disk.yandex.ru/d/{found.group(1)}"
    raise WatchError(f"Not a Yandex Disk share link: {link}")


# --- the disk ---------------------------------------------------------------


def api_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("description", "")
        except ValueError:
            detail = ""
        raise WatchError(f"Yandex Disk answered {error.code}: {detail or error.reason}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise WatchError(f"Yandex Disk is unreachable: {error}") from None


def remote_files(key: str, paths: list[str]) -> Iterator[dict[str, Any]]:
    """Every video under the given folders of the share, recursively."""

    pending = list(paths) or ["/"]
    while pending:
        folder = pending.pop(0)
        offset = 0
        while True:
            page = api_get(API, {"public_key": key, "path": folder, "limit": 200, "offset": offset})
            embedded = page.get("_embedded") or {}
            items = embedded.get("items") or []
            for item in items:
                if item.get("type") == "dir":
                    pending.append(item["path"])
                elif item.get("media_type") == "video" or Path(item.get("name", "")).suffix.lower() in VIDEO_SUFFIXES:
                    yield item
            offset += len(items)
            if not items or offset >= int(embedded.get("total") or 0):
                break


def download_link(key: str, path: str) -> str:
    """A fresh direct link; they expire, so one is asked for per download."""

    href = api_get(f"{API}/download", {"public_key": key, "path": path}).get("href")
    if not href:
        raise WatchError(f"No download link for {path}")
    return href


def download(href: str, dest: Path, *, size: int, sha256: str | None) -> None:
    """Fetch href into dest, resuming a .part file, and verify it.

    A lecture is 5-6 GB; a dropped connection near the end must not cost the
    whole file again, so a .part file is continued with a Range request when the
    server honours it.
    """

    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have < size:
        request = urllib.request.Request(href, headers={"Range": f"bytes={have}-"} if have else {})
        with urllib.request.urlopen(request, timeout=120) as response:
            if have and response.status != 206:
                have = 0  # the server sent the whole file again
            with open(part, "ab" if have else "wb") as out:
                done, step = have, max(size // 10, 1)
                next_note = (done // step + 1) * step
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if done >= next_note:
                        say(f"  {dest.name}: {100 * done // size}%")
                        next_note += step
    got = part.stat().st_size
    if got != size:
        raise WatchError(f"{dest.name}: got {got} bytes of {size}; will resume next run")
    if sha256:
        digest = hashlib.sha256()
        with open(part, "rb") as source:
            for block in iter(lambda: source.read(CHUNK), b""):
                digest.update(block)
        if digest.hexdigest() != sha256:
            part.unlink()
            raise WatchError(f"{dest.name}: sha256 mismatch, download discarded")
    os.replace(part, dest)


# --- config and state ---------------------------------------------------------


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "lecturecut"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError as error:
        raise WatchError(f"{path} is not valid JSON: {error}") from None


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def say(text: str) -> None:
    print(text, flush=True)


def destination(watch: dict[str, Any]) -> Path | None:
    """The watch's folder, followed to a new mount point; None when absent.

    Never created here: a drive that is not mounted would otherwise have its
    lectures written onto the system disk under the empty mount point.
    """

    folder = Path(watch["to"]).expanduser()
    if folder.is_dir():
        return folder
    return core.relocated_folder(folder)


# --- commands ---------------------------------------------------------------


def command_add(args: argparse.Namespace, config: dict, state: dict) -> int:
    key = public_key(args.link)
    name = args.name or key.rsplit("/", 1)[-1]
    if any(watch["name"] == name for watch in config["watches"]):
        raise WatchError(f"A watch named {name} exists; pick another with --name")
    folder = Path(args.to).expanduser().resolve()
    if not folder.is_dir():
        raise WatchError(f"{folder} does not exist")
    options = args.options
    # The pipeline's own parser checks the options now rather than at 3 a.m.
    try:
        core.parse_args(["probe.mp4", *options])
    except SystemExit:
        raise WatchError("lecturecut rejected those options") from None
    files = {
        item["path"]: {"status": "known", "size": item.get("size"), "sha256": item.get("sha256"), "since": now()}
        for item in remote_files(key, args.path)
    }
    config["watches"].append(
        {"name": name, "link": key, "paths": args.path, "to": str(folder), "options": options,
         "keep_raw": args.keep_raw}
    )
    state[name] = {"files": files, "added": now()}
    say(f"Watching {name}: {len(files)} file(s) there now count as known; new ones go to {folder}")
    return 0


def command_list(args: argparse.Namespace, config: dict, state: dict) -> int:
    if not config["watches"]:
        say("No watches. Add one with: lecturecut-watch add LINK --to DIR")
    for watch in config["watches"]:
        files = state.get(watch["name"], {}).get("files", {})
        counts: dict[str, int] = {}
        for entry in files.values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        where = destination(watch)
        say(f"{watch['name']}: {watch['link']} {' '.join(watch['paths']) or '/'}")
        say(f"  to {watch['to']}" + ("" if where else "  (not available now)"))
        say(f"  options: {' '.join(watch['options']) or '(defaults)'}")
        say("  files: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"))
        for path, entry in sorted(files.items()):
            if entry["status"] in ("failed", "downloaded"):
                say(f"    {entry['status']}: {path} - {entry.get('error', '')}")
        last = state.get(watch["name"], {}).get("last_run")
        if last:
            say(f"  last run: {last}")
    return 0


def command_run(args: argparse.Namespace, config: dict, state: dict, save) -> int:
    failures = 0
    for watch in config["watches"]:
        name = watch["name"]
        record = state.setdefault(name, {"files": {}})
        folder = destination(watch)
        if folder is None:
            say(f"{name}: {watch['to']} is not available (drive not mounted?); skipped")
            continue
        try:
            items = list(remote_files(watch["link"], watch["paths"]))
        except WatchError as error:
            say(f"{name}: {error}")
            failures += 1
            continue
        record["last_run"] = now()
        todo = []
        for item in items:
            entry = record["files"].get(item["path"])
            if entry is None:
                entry = record["files"][item["path"]] = {
                    "status": "new", "size": item.get("size"), "sha256": item.get("sha256"),
                    "since": now(), "attempts": 0,
                }
            elif entry["status"] == "done" and item.get("sha256") and entry.get("sha256") != item.get("sha256"):
                # Re-uploaded under the same name: a new recording, process it again.
                entry.update(status="new", sha256=item.get("sha256"), size=item.get("size"), attempts=0)
            if entry["status"] in ("new", "downloaded", "failed") and entry.get("attempts", 0) < MAX_ATTEMPTS:
                todo.append((item, entry))
        save()
        if not todo:
            say(f"{name}: nothing new")
            continue
        for item, entry in todo:
            say(f"{name}: new {item['path']} ({(item.get('size') or 0) / 1e9:.2f} GB)")
            if args.dry_run:
                continue
            if not process_one(watch, folder, item, entry, save):
                failures += 1
    return 1 if failures else 0


def process_one(watch: dict, folder: Path, item: dict, entry: dict, save) -> bool:
    incoming = folder / INCOMING
    raw = incoming / item["name"]
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["updated"] = now()
    try:
        if entry["status"] != "downloaded" or not raw.exists():
            size = int(item.get("size") or 0)
            incoming.mkdir(exist_ok=True)
            part = raw.with_name(raw.name + ".part")
            already = part.stat().st_size if part.exists() else 0
            free = shutil.disk_usage(folder).free
            if free < size - already + SPACE_MARGIN:
                raise WatchError(
                    f"needs {(size - already) / 1e9:.1f} GB, {free / 1e9:.1f} GB free on {folder}"
                )
            download(download_link(watch["link"], item["path"]), raw, size=size, sha256=item.get("sha256"))
            entry.update(status="downloaded", error="")
            save()
        output = folder / f"{raw.stem}_lecturecut.mp4"
        say(f"  processing {raw.name} -> {output.name}")
        code = core.main([str(raw), "-o", str(output), "--if-exists=suffix", *watch["options"]])
        if code != 0:
            raise WatchError(f"lecturecut exited with {code}")
        entry.update(status="done", error="", output=str(output), finished=now())
        if not watch.get("keep_raw"):
            raw.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                incoming.rmdir()  # only if empty
        save()
        say(f"  done: {output}")
        return True
    except (WatchError, OSError, urllib.error.URLError) as error:
        if entry["status"] not in ("downloaded",):
            entry["status"] = "failed"
        entry["error"] = str(error)
        save()
        tries = entry["attempts"]
        say(f"  failed ({tries}/{MAX_ATTEMPTS}): {error}")
        return False


def present_locally(folder: Path, name: str) -> bool:
    """A recording that is already here, raw or processed."""

    stem = Path(name).stem.lower()
    for path in (*folder.iterdir(), *((folder / INCOMING).iterdir() if (folder / INCOMING).is_dir() else ())):
        lower = path.name.lower()
        if lower == name.lower() or lower.startswith(stem + "_lecturecut"):
            return True
    return False


def command_fetch(args: argparse.Namespace, config: dict, state: dict) -> int:
    """Queue files the watch counts as known: by name, or all missing here.

    A watch starts by treating what is already shared as known, so recordings
    uploaded before it was added are never fetched on their own.
    """

    if not args.missing and not args.patterns:
        raise WatchError("name the files to fetch (parts of their names), or use --missing")
    queued = 0
    for watch in config["watches"]:
        if args.watch and watch["name"] != args.watch:
            continue
        folder = destination(watch)
        if folder is None and args.missing:
            raise WatchError(f"{watch['name']}: {watch['to']} is not available, cannot tell what is missing")
        for path, entry in sorted(state.get(watch["name"], {}).get("files", {}).items()):
            if entry["status"] not in ("known", "failed", "done"):
                continue
            name = Path(path).name
            wanted = any(pattern.lower() in name.lower() for pattern in args.patterns) or (
                args.missing and not present_locally(folder, name)
            )
            if wanted:
                entry.update(status="new", attempts=0, error="")
                queued += 1
                say(f"{watch['name']}: queued {path}")
    say(f"{queued} file(s) queued; the next run fetches them" if queued else "Nothing matched")
    return 0


def command_install_timer(args: argparse.Namespace, config: dict, state: dict) -> int:
    units = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    (units / f"{TIMER_NAME}.service").write_text(
        "[Unit]\n"
        "Description=LectureCut: fetch and process new lectures\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={script.parent}\n"
        f"ExecStart={sys.executable} {script} run\n"
        # A render is long and heavy; it should yield to whatever you are doing.
        "Nice=10\n"
        "IOSchedulingClass=idle\n",
        encoding="utf-8",
    )
    (units / f"{TIMER_NAME}.timer").write_text(
        "[Unit]\n"
        "Description=Look for new lectures every hour\n\n"
        "[Timer]\n"
        "OnCalendar=hourly\n"
        "RandomizedDelaySec=5min\n"
        # Missed runs (the machine was off) happen at the next boot.
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n",
        encoding="utf-8",
    )
    say(f"Wrote {units / (TIMER_NAME + '.service')} and .timer")
    if args.no_enable:
        say(f"Enable with: systemctl --user enable --now {TIMER_NAME}.timer")
        return 0
    for command in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", f"{TIMER_NAME}.timer"]):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise WatchError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    say(f"Timer enabled. Follow it with: journalctl --user -u {TIMER_NAME} -f")
    return 0


# --- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lecturecut-watch", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser(
        "add",
        help="watch a shared Yandex Disk folder",
        epilog="Options for lecturecut itself go after --, e.g. -- --max-fps=30 --board=ink",
    )
    add.add_argument("link", help="share link, e.g. https://disk.yandex.ru/d/... or a Yandex 360 link")
    add.add_argument("--to", required=True, help="local folder for the processed lectures")
    add.add_argument("--path", action="append", default=[], help="folder inside the share; repeatable; default all")
    add.add_argument("--name", help="a short name for the watch")
    add.add_argument("--keep-raw", action="store_true", help="keep downloads in .incoming after processing")

    sub.add_parser("list", help="show watches and the state of their files")
    fetch = sub.add_parser("fetch", help="queue files the watch counts as known")
    fetch.add_argument("patterns", nargs="*", help="parts of file names, e.g. Lecture_05")
    fetch.add_argument("--missing", action="store_true", help="everything not here, raw or processed")
    fetch.add_argument("--watch", help="only this watch")
    run = sub.add_parser("run", help="one pass: fetch and process what is new")
    run.add_argument("--dry-run", action="store_true", help="only say what is new")
    timer = sub.add_parser("install-timer", help="run every hour with a systemd user timer")
    timer.add_argument("--no-enable", action="store_true", help="write the units but do not enable them")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after "--" belongs to lecturecut. argparse's REMAINDER would
    # also swallow --to and --path when they follow the link.
    options: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, options = argv[:cut], argv[cut + 1 :]
    args = build_parser().parse_args(argv)
    args.options = options
    folder = config_dir()
    config_path, state_path = folder / "watch.json", folder / "watch-state.json"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        # One run at a time: a render can outlast the hour between timer ticks.
        with open(folder / "watch.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                say("Another lecturecut-watch is running; leaving it to finish")
                return 0
            config = load_json(config_path, {"watches": []})
            state = load_json(state_path, {})

            def save() -> None:
                save_json(state_path, state)

            if args.command == "add":
                code = command_add(args, config, state)
                save_json(config_path, config)
                save()
                return code
            if args.command == "list":
                return command_list(args, config, state)
            if args.command == "fetch":
                code = command_fetch(args, config, state)
                save()
                return code
            if args.command == "run":
                return command_run(args, config, state, save)
            return command_install_timer(args, config, state)
    except WatchError as error:
        say(f"lecturecut-watch: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
