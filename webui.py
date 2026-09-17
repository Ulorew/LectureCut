#!/usr/bin/env python3
"""Local web UI for LectureCut: pick a file, set the knobs, watch it render.

The browser never sees a filesystem path of its own, so sources are chosen on
this side: the page lists media under the configured roots. Settings are turned
back into argv and handed to the same `parse_args` the CLI uses, which keeps one
set of defaults and one validator for both entry points.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextvars
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import main as core

MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".m4v",
    ".webm",
    ".avi",
    ".mts",
    ".m2ts",
    ".wav",
    ".m4a",
    ".mp3",
    ".aac",
    ".flac",
}
UPLOAD_CHUNK = 1024 * 1024
PROBE_WORKERS = 8
# A listing should stay responsive even when pointed at a large archive.
PROBE_LIMIT = 400
# A chosen folder can be a home directory; walking all of it would stall the page.
LIST_DEPTH = 3
LIST_FILE_LIMIT = 2000
DIR_LIST_LIMIT = 500
RECENT_SOURCE_DIRS = 8
# Mutating requests must carry this header. A cross-site page cannot add a custom
# header without a CORS preflight, which this server never approves.
CSRF_HEADER = "X-LectureCut"
# Segments are kept for a while after a job ends: a viewer may still be watching
# the playlist when the final MP4 appears, and the page needs a moment to swap.
PREVIEW_GRACE_SECONDS = 15 * 60
LIVE_SERVE_RE = re.compile(r"^(index\.m3u8|init\.mp4|seg_\d{5}\.m4s)$")
# Browsers will not play HEVC; a preview of one would be a black box.
LIVE_ENCODER_BLOCKLIST = {"hevc_nvenc"}
EVENT_HISTORY_LIMIT = 2000

# Time estimates. A job passes through three buckets whose cost scales
# differently: analysis is a fixed number of short windows, silence detection is
# one audio pass over the input, and the render decodes all of it. The defaults
# are what this project measured; each finished job replaces them with what this
# machine actually took.
ANALYSIS_BUCKET = "analysis"
SILENCE_BUCKET = "silence"
RENDER_BUCKET = "render"
BUCKET_ORDER = (ANALYSIS_BUCKET, SILENCE_BUCKET, RENDER_BUCKET)
PHASE_BUCKETS = {
    core.PHASE_MEASURE: ANALYSIS_BUCKET,
    core.PHASE_CALIBRATE: ANALYSIS_BUCKET,
    core.PHASE_SILENCE: SILENCE_BUCKET,
    core.PHASE_RENDER: RENDER_BUCKET,
}
DEFAULT_ANALYSIS_SECONDS = 40.0
DEFAULT_SILENCE_RATE = 0.03  # wall seconds per input second
DEFAULT_RENDER_RATE = 0.30
# Before this much of a phase has passed, its own pace says too little.
EXTRAPOLATE_MIN_FRACTION = 0.03
EXTRAPOLATE_MIN_SECONDS = 3.0
LEARNING_FLOOR = 0.25
SSE_KEEPALIVE_SECONDS = 15.0

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

STATIC_DIR = Path(__file__).resolve().parent / "static"


def model_hint() -> str:
    """Built per request: the cache location follows the environment."""

    return (
        f"Модели по ~300 КБ, скачиваются по кнопке в кэш {core.model_cache_dir()}. "
        "Хеши зашиты, так что загрузка проверяется"
    )


def live_root() -> Path:
    return core.model_cache_dir().parent / "live"


def state_file_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "lecturecut" / "webui.json"


@dataclass
class ThroughputModel:
    """How long each bucket takes on this machine, learned from finished jobs."""

    analysis_seconds: float = DEFAULT_ANALYSIS_SECONDS
    silence_rate: float = DEFAULT_SILENCE_RATE
    render_rate: float = DEFAULT_RENDER_RATE
    samples: int = 0
    path: Path | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path | None) -> "ThroughputModel":
        model = cls(path=path)
        if path is None or not path.exists():
            return model
        try:
            data = json.loads(path.read_text())
            model.analysis_seconds = float(data["analysis_seconds"])
            model.silence_rate = float(data["silence_rate"])
            model.render_rate = float(data["render_rate"])
            model.samples = int(data["samples"])
        except (OSError, ValueError, KeyError, TypeError):
            return cls(path=path)
        return model

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "analysis_seconds": self.analysis_seconds,
            "silence_rate": self.silence_rate,
            "render_rate": self.render_rate,
            "samples": self.samples,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.part")
            temporary.write_text(json.dumps(payload, indent=2))
            os.replace(temporary, self.path)
        except OSError:
            pass

    def estimate(
        self, duration: float | None, settings: dict[str, Any]
    ) -> dict[str, float] | None:
        """Seconds per bucket for a job of this length, or None if unknown."""

        if duration is None:
            return None
        if settings.get("dry_run"):
            render = 0.0
        else:
            render = self.render_rate * duration
        return {
            ANALYSIS_BUCKET: 0.0 if settings.get("no_analyze") else self.analysis_seconds,
            SILENCE_BUCKET: 0.0 if settings.get("no_cut_silence") else self.silence_rate * duration,
            RENDER_BUCKET: render,
        }

    def learn(self, bucket_seconds: dict[str, float], duration: float) -> None:
        """Fold one finished job in.

        The first job replaces the defaults outright, later ones are averaged in,
        and the weight never drops below a quarter so a new kind of source - an
        iPhone's HEVC after 720p H.264, say - shows up within a few jobs.
        """

        if duration <= 0:
            return
        weight = max(LEARNING_FLOOR, 1.0 / (self.samples + 1))

        def blend(old: float, new: float) -> float:
            return old + weight * (new - old)

        if bucket_seconds.get(ANALYSIS_BUCKET):
            self.analysis_seconds = blend(self.analysis_seconds, bucket_seconds[ANALYSIS_BUCKET])
        if bucket_seconds.get(SILENCE_BUCKET):
            self.silence_rate = blend(self.silence_rate, bucket_seconds[SILENCE_BUCKET] / duration)
        if bucket_seconds.get(RENDER_BUCKET):
            self.render_rate = blend(self.render_rate, bucket_seconds[RENDER_BUCKET] / duration)
        self.samples += 1
        self.save()


@dataclass
class Job:
    """One unit of work. `kind` exists so previews can join later as a sibling."""

    id: str
    kind: str
    source: str
    settings: dict[str, Any]
    live_dir: Path | None = None
    input_duration: float | None = None
    bucket: str | None = None
    bucket_started: float | None = None
    bucket_seconds: dict[str, float] = field(default_factory=dict)
    phase_fraction: float = 0.0
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    phase: str | None = None
    overall: float = 0.0
    result: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    subscribers: list["queue.Queue[Any]"] = field(default_factory=list)

    def live_playlist(self) -> Path | None:
        """The playlist, once the render has actually written one."""

        if self.live_dir is None:
            return None
        playlist = self.live_dir / core.LIVE_PLAYLIST
        return playlist if playlist.exists() else None

    def enter_phase(self, phase: str, now: float) -> None:
        bucket = PHASE_BUCKETS.get(phase)
        self.phase_fraction = 0.0
        if bucket == self.bucket:
            return  # measure and calibrate share one bucket
        self.close_bucket(now)
        self.bucket = bucket
        self.bucket_started = now

    def close_bucket(self, now: float) -> None:
        if self.bucket is not None and self.bucket_started is not None:
            spent = now - self.bucket_started
            self.bucket_seconds[self.bucket] = self.bucket_seconds.get(self.bucket, 0.0) + spent
        self.bucket_started = None

    def remaining_seconds(self, model: ThroughputModel, now: float) -> float | None:
        """Time left for this job, or None when there is nothing to base it on.

        Inside silence detection and the render the job's own pace is the best
        guide, so it is extrapolated from the fraction done. Analysis reports no
        useful fraction, so there the learned figure is used instead, less what
        has already gone by.
        """

        if self.status in {STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED}:
            return 0.0
        estimate = model.estimate(self.input_duration, self.settings)
        if self.status == STATUS_QUEUED or self.bucket is None:
            if estimate is None:
                return None
            elapsed = now - self.started_at if self.started_at is not None else 0.0
            return max(sum(estimate.values()) - elapsed, 0.0)

        index = BUCKET_ORDER.index(self.bucket)
        elapsed = now - self.bucket_started if self.bucket_started is not None else 0.0
        fraction = self.phase_fraction
        if (
            self.bucket != ANALYSIS_BUCKET
            and fraction >= EXTRAPOLATE_MIN_FRACTION
            and elapsed >= EXTRAPOLATE_MIN_SECONDS
        ):
            current = elapsed * (1.0 - fraction) / fraction
        elif estimate is not None:
            current = max(estimate[self.bucket] - elapsed, 0.0)
        else:
            return None
        if index == len(BUCKET_ORDER) - 1:
            return current
        if estimate is None:
            return None
        return current + sum(estimate[b] for b in BUCKET_ORDER[index + 1 :])

    def summary(
        self,
        model: ThroughputModel | None = None,
        finishes_in: float | None = None,
    ) -> dict[str, Any]:
        remaining = (
            self.remaining_seconds(model, time.time()) if model is not None else None
        )
        return {
            "id": self.id,
            "live": self.live_playlist() is not None,
            "input_duration": self.input_duration,
            # This job's own work, and how long until it is done - which for a
            # queued job includes everything ahead of it.
            "remaining_seconds": remaining,
            "finishes_in_seconds": finishes_in,
            "kind": self.kind,
            "source": self.source,
            "settings": self.settings,
            "status": self.status,
            "phase": self.phase,
            "overall": self.overall,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "analysis": self.analysis,
            "error": self.error,
        }


class JobManager:
    """A single worker draining a queue, with per-job event fan-out.

    One worker rather than a pool: a render already saturates the encoder, and
    running two would make both slower while making progress harder to read.
    """

    def __init__(self, model: ThroughputModel | None = None) -> None:
        self.model = model or ThroughputModel()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_forever, daemon=True)
        self._worker.start()

    # -- public API -----------------------------------------------------------

    def submit(
        self,
        *,
        kind: str,
        source: str,
        settings: dict[str, Any],
        live_dir: Path | None = None,
        input_duration: float | None = None,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            source=source,
            settings=settings,
            live_dir=live_dir,
            input_duration=input_duration,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._publish(job, {"type": "status", "status": job.status})
        self._pending.put(job.id)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
        return job

    def finish_times(self, now: float) -> dict[str, float | None]:
        """Seconds until each pending job is done, in the order the worker runs them.

        One worker drains the queue first in, first out, so a job finishes after
        everything submitted before it. Once any job ahead has no estimate, none
        of the later ones can have one either.
        """

        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._order]
        finishes: dict[str, float | None] = {}
        ahead: float | None = 0.0
        for job in jobs:
            if job.status not in {STATUS_QUEUED, STATUS_RUNNING}:
                continue
            own = job.remaining_seconds(self.model, now)
            if ahead is None or own is None:
                ahead = None
                finishes[job.id] = None
            else:
                ahead += own
                finishes[job.id] = ahead
        return finishes

    def listing(self) -> list[dict[str, Any]]:
        finishes = self.finish_times(time.time())
        with self._lock:
            return [
                self._jobs[job_id].summary(self.model, finishes.get(job_id))
                for job_id in reversed(self._order)
            ]

    def queue_remaining(self) -> dict[str, Any]:
        """Time until the whole queue is through, and whether that is complete."""

        total = 0.0
        unknown = 0
        pending = 0
        now = time.time()
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._order]
        for job in jobs:
            if job.status not in {STATUS_QUEUED, STATUS_RUNNING}:
                continue
            pending += 1
            remaining = job.remaining_seconds(self.model, now)
            if remaining is None:
                unknown += 1
            else:
                total += remaining
        return {
            "pending": pending,
            "remaining_seconds": total,
            "unknown": unknown,
            "learned_from": self.model.samples,
        }

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in {STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED}:
            return job
        job.cancel.set()
        self._publish(job, {"type": "line", "text": "Cancelling...", "error": False})
        return job

    def subscribe(self, job_id: str) -> tuple[Job, "queue.Queue[Any]", list[dict[str, Any]]]:
        job = self.get(job_id)
        channel: queue.Queue[Any] = queue.Queue()
        with self._lock:
            history = list(job.events)
            job.subscribers.append(channel)
        return job, channel, history

    def unsubscribe(self, job: Job, channel: "queue.Queue[Any]") -> None:
        with self._lock:
            if channel in job.subscribers:
                job.subscribers.remove(channel)

    # -- internals ------------------------------------------------------------

    def _publish(self, job: Job, event: dict[str, Any]) -> None:
        with self._lock:
            job.events.append(event)
            if len(job.events) > EVENT_HISTORY_LIMIT:
                del job.events[: len(job.events) - EVENT_HISTORY_LIMIT]
            channels = list(job.subscribers)
        for channel in channels:
            channel.put(event)

    def _schedule_preview_cleanup(self, job: Job) -> None:
        """Drop the segments later, not the moment the job ends.

        Deleting them at once would pull the playlist out from under a viewer who
        is still watching it while the page switches over to the finished file.
        """

        folder = job.live_dir
        if folder is None:
            return

        def remove() -> None:
            shutil.rmtree(folder, ignore_errors=True)

        timer = threading.Timer(PREVIEW_GRACE_SECONDS, remove)
        timer.daemon = True
        timer.start()

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.finished_at = time.time()
        job.close_bucket(job.finished_at)
        if status == STATUS_DONE and job.input_duration and not job.settings.get("dry_run"):
            self.model.learn(job.bucket_seconds, job.input_duration)
        self._schedule_preview_cleanup(job)
        if status == STATUS_DONE:
            job.overall = 1.0
        self._publish(job, {"type": "status", "status": status})
        with self._lock:
            channels = list(job.subscribers)
        for channel in channels:
            channel.put(None)

    def _run_forever(self) -> None:
        while True:
            job_id = self._pending.get()
            try:
                self._run_job(self.get(job_id))
            except Exception as error:  # a worker crash must not kill the queue
                job = self._jobs.get(job_id)
                if job is not None:
                    job.error = f"{type(error).__name__}: {error}"
                    self._finish(job, STATUS_ERROR)

    def _run_job(self, job: Job) -> None:
        if job.cancel.is_set():
            self._finish(job, STATUS_CANCELLED)
            return

        job.status = STATUS_RUNNING
        job.started_at = time.time()
        self._publish(job, {"type": "status", "status": job.status})

        def on_line(text: str, error: bool) -> None:
            for line in str(text).splitlines() or [""]:
                self._publish(job, {"type": "line", "text": line, "error": error})

        def on_event(kind: str, fields: dict[str, Any]) -> None:
            if kind == "phase":
                job.phase = str(fields.get("phase"))
                job.overall = float(fields.get("overall") or 0.0)
                job.enter_phase(job.phase, time.time())
            elif kind == "progress":
                job.overall = float(fields.get("overall") or 0.0)
                job.phase_fraction = float(fields.get("fraction") or 0.0)
            elif kind == "result":
                job.result = dict(fields)
            elif kind == "analysis":
                job.analysis = dict(fields)
            self._publish(job, {"type": kind, **fields})

        def work() -> None:
            core.ACTIVE_REPORTER.set(core.Reporter(on_line=on_line, on_event=on_event))
            core.ACTIVE_CANCEL.set(job.cancel)
            try:
                argv = core.settings_to_argv(job.source, job.settings)
                args = core.parse_args(argv)
                code = core.run_pipeline(args)
            except core.PipelineCancelled:
                self._finish(job, STATUS_CANCELLED)
                return
            except SystemExit as error:  # argparse rejected a value
                job.error = f"Invalid settings: {error}"
                on_line(job.error, True)
                self._finish(job, STATUS_ERROR)
                return
            except core.PipelineError as error:
                job.error = str(error)
                on_line(job.error, True)
                self._finish(job, STATUS_ERROR)
                return
            except Exception as error:
                job.error = f"{type(error).__name__}: {error}"
                on_line(job.error, True)
                self._finish(job, STATUS_ERROR)
                return
            if code == 0:
                self._finish(job, STATUS_DONE)
            else:
                job.error = job.error or f"Pipeline exited with code {code}"
                self._finish(job, STATUS_ERROR)

        # A fresh context so this job's reporter, cancel flag and progress
        # tracker cannot leak into the next one.
        context = contextvars.Context()
        context.run(work)


@dataclass
class Config:
    """Server settings.

    The two request guards default to off so a Config built by hand - in a test,
    say - stays simple; build_config() turns both on for the real server.
    """

    roots: list[Path]
    upload_dir: Path
    allow_upload: bool = True
    allow_open: bool = True
    # Whether the page may point the server at folders outside the given roots.
    allow_browse: bool = True
    # Render through a playlist so a job can be watched while it runs.
    live_preview: bool = True
    source_dir: Path | None = None
    recent_source_dirs: list[Path] = field(default_factory=list)
    state_path: Path | None = None
    throughput_path: Path | None = None
    allowed_hosts: set[str] | None = None
    require_csrf_header: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def current_source_dir(self) -> Path:
        if self.source_dir is not None and self.source_dir.is_dir():
            return self.source_dir
        for root in self.roots:
            if root.is_dir() and root != self.upload_dir:
                return root
        return self.roots[0]

    def choose_source_dir(self, folder: Path) -> None:
        """Make a folder the input folder, and let the server read from it."""

        with self.lock:
            self.source_dir = folder
            if folder not in self.roots:
                self.roots.append(folder)
            recent = [folder, *(d for d in self.recent_source_dirs if d != folder)]
            self.recent_source_dirs = recent[:RECENT_SOURCE_DIRS]
        self.save_state()

    def save_state(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "source_dir": str(self.source_dir) if self.source_dir else None,
            "recent_source_dirs": [str(d) for d in self.recent_source_dirs],
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(f".{self.state_path.name}.part")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(temporary, self.state_path)
        except OSError:
            pass  # losing the remembered folder is not worth failing a request

    def load_state(self) -> None:
        """Restore the chosen folders; a folder that has since vanished is dropped."""

        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        if not self.allow_browse:
            return
        recent = [Path(p) for p in payload.get("recent_source_dirs") or []]
        self.recent_source_dirs = [d for d in recent if d.is_dir()][:RECENT_SOURCE_DIRS]
        for folder in self.recent_source_dirs:
            if folder not in self.roots:
                self.roots.append(folder)
        chosen = payload.get("source_dir")
        if chosen and Path(chosen).is_dir():
            self.source_dir = Path(chosen)
            if self.source_dir not in self.roots:
                self.roots.append(self.source_dir)


def walk_limited(
    root: Path, *, max_depth: int, limit: int, want_dirs: bool
) -> Iterator[Path]:
    """Yield files (or directories) under root, bounded in depth and count.

    A chosen input folder can be as broad as a home directory. rglob would walk
    every cache and dependency tree underneath it before the page could answer.
    Hidden directories are skipped for the same reason.
    """

    produced = 0
    for current, dirnames, filenames in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        names = dirnames if want_dirs else sorted(filenames)
        for name in names:
            if produced >= limit:
                return
            produced += 1
            yield Path(current) / name


def browse_directory(raw: str | None, config: Config) -> dict[str, Any]:
    """List the subfolders of one directory, for choosing an input folder.

    Only folder names and media counts are exposed, never file contents, and
    only when browsing is allowed at all.
    """

    if not config.allow_browse:
        raise HTTPException(status_code=403, detail="Choosing folders is disabled")
    base = Path(raw).expanduser() if raw else config.current_source_dir()
    try:
        folder = base.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise HTTPException(status_code=404, detail=f"No such folder: {raw}") from None
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {folder}")

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(folder.iterdir(), key=lambda child: child.name.lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"No permission to read {folder}") from None
    media_here = 0
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                if len(entries) < DIR_LIST_LIMIT:
                    entries.append({"name": child.name, "path": str(child)})
            elif child.suffix.lower() in MEDIA_SUFFIXES:
                media_here += 1
        except OSError:
            continue
    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder.parent != folder else None,
        "home": str(Path.home()),
        "dirs": entries,
        "media_here": media_here,
    }


def desktop_open_command(target: Path) -> list[str]:
    """The platform's 'open this with whatever handles it' command."""

    if sys.platform == "darwin":
        return ["open", str(target)]
    if sys.platform.startswith("win"):
        return ["explorer", str(target)]
    return ["xdg-open", str(target)]


def list_directories(config: Config) -> list[dict[str, Any]]:
    """Candidate output folders: every root plus the directories inside them."""

    entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in list(config.roots):
        if not root.exists():
            continue
        candidates = [
            root,
            *walk_limited(root, max_depth=2, limit=DIR_LIST_LIMIT, want_dirs=True),
        ]
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen or any(part.startswith(".") for part in resolved.parts):
                continue
            seen.add(resolved)
            entries.append(
                {
                    "path": str(resolved),
                    "label": str(resolved),
                    "is_root": resolved == root,
                }
            )
    entries.sort(key=lambda entry: (not entry["is_root"], entry["path"]))
    return entries


def resolve_within_roots(raw: str, config: Config) -> Path:
    """Accept a path only if it really sits inside a configured root."""

    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No such file: {raw}") from None
    for root in config.roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    raise HTTPException(status_code=403, detail=f"Path is outside the allowed roots: {raw}")


def list_arnndn_models(config: Config) -> list[dict[str, Any]]:
    """Every model on hand: the download cache first, then the roots."""

    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    catalogue = {model.file: model for model in core.ARNNDN_MODELS.values()}

    directories = [core.model_cache_dir(), *config.roots]
    for directory in directories:
        if not directory.exists():
            continue
        paths = (
            sorted(directory.glob(f"*{core.ARNNDN_MODEL_SUFFIX}"))
            if directory == core.model_cache_dir()
            else sorted(
                path
                for path in walk_limited(
                    directory, max_depth=LIST_DEPTH, limit=LIST_FILE_LIMIT, want_dirs=False
                )
                if path.suffix == core.ARNNDN_MODEL_SUFFIX
            )
        )
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            known = catalogue.get(resolved.name)
            found.append(
                {
                    "path": str(resolved),
                    "name": resolved.name,
                    "key": known.name if known else None,
                    "signal": known.signal if known else None,
                    "noise": known.noise if known else None,
                    "recommended": bool(known)
                    and known.name == core.ARNNDN_DEFAULT_MODEL,
                }
            )
    found.sort(key=lambda entry: (not entry["recommended"], entry["name"]))
    return found


def model_catalogue() -> list[dict[str, Any]]:
    """What can be fetched, and whether it already is."""

    return [
        {
            "key": model.name,
            "file": model.file,
            "size": model.size,
            "signal": model.signal,
            "noise": model.noise,
            "recommended": model.name == core.ARNNDN_DEFAULT_MODEL,
            "installed": core.installed_model_path(model).exists(),
        }
        for model in core.ARNNDN_MODELS.values()
    ]


class ProcessedIndex:
    """Remembers which files already carry a LectureCut tag.

    Reading the tag costs an ffprobe call - around 30 ms - which is nothing once,
    and far too much on every listing of a full archive. Results are keyed by
    path, size and mtime, so a re-render invalidates its own entry.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, float], bool] = {}
        self._lock = threading.Lock()

    def flags(self, paths: list[Path]) -> dict[str, bool]:
        keys: dict[str, tuple[str, int, float]] = {}
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            keys[str(path)] = (str(path), stat.st_size, stat.st_mtime)

        with self._lock:
            unknown = [
                (path, key)
                for path, key in keys.items()
                if key not in self._cache
            ][:PROBE_LIMIT]

        if unknown:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=PROBE_WORKERS
            ) as pool:
                results = list(
                    pool.map(
                        lambda item: core.is_already_processed(Path(item[0])), unknown
                    )
                )
            with self._lock:
                for (_, key), value in zip(unknown, results):
                    self._cache[key] = value

        with self._lock:
            return {path: self._cache.get(key, False) for path, key in keys.items()}


def list_media(config: Config, index: ProcessedIndex | None = None) -> list[dict[str, Any]]:
    """Media in the current input folder - one folder, like a file picker."""

    entries: list[dict[str, Any]] = []
    root = config.current_source_dir()
    if root.exists():
        for path in walk_limited(
            root, max_depth=LIST_DEPTH, limit=LIST_FILE_LIMIT, want_dirs=False
        ):
            if path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "relative": str(path.relative_to(root)),
                    "root": str(root),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    if index is not None:
        flags = index.flags([Path(entry["path"]) for entry in entries])
        for entry in entries:
            entry["processed"] = flags.get(entry["path"], False)
    entries.sort(key=lambda entry: entry["mtime"], reverse=True)
    return entries


def purge_stale_previews(grace: float = PREVIEW_GRACE_SECONDS) -> None:
    """Clear segment folders left behind by a crash or a kill."""

    root = live_root()
    if not root.is_dir():
        return
    cutoff = time.time() - grace
    for folder in root.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def effective_duration(source: Path, settings: dict[str, Any]) -> float | None:
    """The stretch of the input a job will actually process, if it can be probed."""

    try:
        start = float(settings.get("start") or 0.0)
        limit = settings.get("limit")
        media = core.probe_media(
            source, start=start, limit=float(limit) if limit not in (None, "") else None
        )
    except (core.PipelineError, subprocess.CalledProcessError, ValueError, OSError):
        # An unreadable file still gets queued; the pipeline reports what is wrong.
        return None
    return media.duration if media.duration > 0 else None


def live_preview_allowed(config: Config, settings: dict[str, Any]) -> bool:
    if not config.live_preview:
        return False
    return str(settings.get("encoder") or "auto") not in LIVE_ENCODER_BLOCKLIST


def create_app(config: Config, jobs: JobManager | None = None) -> FastAPI:
    app = FastAPI(title="LectureCut", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard_requests(request: Request, call_next: Any) -> Any:
        # DNS rebinding: a hostile page can resolve its own name to 127.0.0.1 and
        # then read this server as same-origin. The Host header gives it away.
        if config.allowed_hosts is not None:
            host = (request.headers.get("host") or "").lower()
            if host not in config.allowed_hosts:
                return JSONResponse({"detail": "Unexpected Host header"}, status_code=403)
        if (
            config.require_csrf_header
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get(CSRF_HEADER) != "1"
        ):
            return JSONResponse(
                {"detail": f"Missing {CSRF_HEADER} header"}, status_code=403
            )
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            # The page and its script are one unit. A browser that keeps an old
            # index.html while fetching a new app.js gets a script reaching for
            # elements that are not there - which is how this rule came about.
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    manager = jobs or JobManager(ThroughputModel.load(config.throughput_path))
    processed = ProcessedIndex()
    app.state.config = config
    app.state.jobs = manager
    app.state.processed = processed

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        return {
            "groups": core.parser_schema(),
            "roots": [str(root) for root in config.roots],
            "source_dir": str(config.current_source_dir()),
            "recent_source_dirs": [str(d) for d in config.recent_source_dirs],
            "allow_browse": config.allow_browse,
            "live_preview": config.live_preview,
            "csrf_header": CSRF_HEADER,
            "allow_upload": config.allow_upload,
            "allow_open": config.allow_open,
            "denoise_help": core.AUDIO_DENOISE_HELP,
            "models": list_arnndn_models(config),
            "catalogue": model_catalogue(),
            "default_model": core.ARNNDN_DEFAULT_MODEL,
            "model_hint": model_hint(),
        }

    @app.get("/api/files")
    def files() -> dict[str, Any]:
        return {
            "source_dir": str(config.current_source_dir()),
            "files": list_media(config, processed),
        }

    @app.get("/api/locate")
    def locate(name: str = Query(...), size: int = Query(...)) -> dict[str, Any]:
        """Find a dropped file among the folders already known.

        A browser hands over a dropped file's bytes but not its path, so the
        alternative is copying gigabytes across localhost to a place the server
        can already read from.
        """

        wanted = Path(name).name
        for root in [config.current_source_dir(), *config.roots]:
            if not root.is_dir():
                continue
            for path in walk_limited(
                root, max_depth=LIST_DEPTH, limit=LIST_FILE_LIMIT, want_dirs=False
            ):
                try:
                    if path.name == wanted and path.stat().st_size == size:
                        return {"found": True, "path": str(path), "dir": str(path.parent)}
                except OSError:
                    continue
        return {"found": False}

    @app.get("/api/browse")
    def browse(path: str | None = Query(default=None)) -> dict[str, Any]:
        return browse_directory(path, config)

    @app.post("/api/source-dir")
    def set_source_dir(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not config.allow_browse:
            raise HTTPException(status_code=403, detail="Choosing folders is disabled")
        raw = str(payload.get("path") or "")
        try:
            folder = Path(raw).expanduser().resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise HTTPException(status_code=404, detail=f"No such folder: {raw}") from None
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a folder: {folder}")
        config.choose_source_dir(folder)
        return {
            "source_dir": str(folder),
            "recent_source_dirs": [str(d) for d in config.recent_source_dirs],
            "files": list_media(config, processed),
        }

    @app.get("/api/probe")
    def probe(path: str = Query(...)) -> dict[str, Any]:
        resolved = resolve_within_roots(path, config)
        try:
            media = core.probe_media(resolved)
        except core.PipelineError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        return {
            "path": str(resolved),
            "duration": media.duration,
            "has_audio": media.has_audio,
            "has_video": media.has_video,
            "video_fps": media.video_fps,
            "default_output": str(core.default_output_path(str(resolved))),
        }

    @app.post("/api/upload")
    async def upload(request: Request, name: str = Query(...)) -> dict[str, Any]:
        """Raw-body upload for sources outside the roots.

        The body is streamed rather than parsed as multipart so a multi-gigabyte
        lecture never has to be held in memory - and so the CLI's dependency
        list does not grow a form parser.
        """

        if not config.allow_upload:
            raise HTTPException(status_code=403, detail="Uploads are disabled")
        safe_name = Path(name).name
        if not safe_name:
            raise HTTPException(status_code=400, detail="A file name is required")
        config.upload_dir.mkdir(parents=True, exist_ok=True)
        target = config.upload_dir / safe_name
        written = 0
        with target.open("wb") as handle:
            async for chunk in request.stream():
                handle.write(chunk)
                written += len(chunk)
        return {
            "path": str(target),
            "dir": str(config.upload_dir),
            "name": safe_name,
            "size": written,
        }

    @app.post("/api/models")
    def fetch_models(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Fetch models into the cache. They are ~300 KB each with pinned digests."""

        requested = payload.get("keys") or [core.ARNNDN_DEFAULT_MODEL]
        if requested == "all":
            requested = list(core.ARNNDN_MODELS)
        unknown = [key for key in requested if key not in core.ARNNDN_MODELS]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"Unknown model(s): {', '.join(unknown)}"
            )
        fetched: list[str] = []
        for key in requested:
            try:
                path = core.download_arnndn_model(core.ARNNDN_MODELS[key])
            except core.PipelineError as error:
                raise HTTPException(status_code=502, detail=str(error)) from None
            fetched.append(str(path))
        return {
            "fetched": fetched,
            "models": list_arnndn_models(config),
            "catalogue": model_catalogue(),
        }

    @app.get("/api/dirs")
    def dirs() -> dict[str, Any]:
        return {"dirs": list_directories(config)}

    @app.post("/api/open")
    def open_path(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Hand a finished file to the desktop.

        The browser cannot follow a file:// link from an http page, and this
        server runs on the same machine as the person using it, so opening it
        here is the shortest honest path to "show me the result".
        """

        if not config.allow_open:
            raise HTTPException(status_code=403, detail="Opening files is disabled")
        target = resolve_within_roots(str(payload.get("path") or ""), config)
        if payload.get("reveal"):
            target = target.parent
        try:
            subprocess.Popen(
                desktop_open_command(target),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise HTTPException(status_code=500, detail=str(error)) from None
        return {"opened": str(target)}

    @app.get("/api/file")
    def download(path: str = Query(...)) -> FileResponse:
        resolved = resolve_within_roots(path, config)
        return FileResponse(str(resolved), filename=resolved.name)

    @app.get("/api/jobs")
    def job_list() -> dict[str, Any]:
        return {"jobs": manager.listing(), "queue": manager.queue_remaining()}

    @app.post("/api/jobs")
    def create_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw_source = payload.get("source")
        if not raw_source:
            raise HTTPException(status_code=400, detail="A source file is required")
        source = resolve_within_roots(str(raw_source), config)

        settings = dict(payload.get("settings") or {})
        # Where segments are written is the server's business, not the page's.
        settings.pop("live_dir", None)
        settings.pop("keep_live_dir", None)
        output = settings.get("output")
        if output:
            destination = Path(str(output)).expanduser()
            parent = resolve_within_roots(str(destination.parent), config)
            settings["output"] = str(parent / destination.name)
        elif payload.get("output_dir"):
            # Batches cannot share one file name, so the folder is chosen and the
            # name is left to the core - keeping that rule in one place.
            folder = resolve_within_roots(str(payload["output_dir"]), config)
            settings["output"] = str(folder / core.default_output_path(str(source)).name)

        try:
            argv = core.settings_to_argv(str(source), settings)
            # Refuse a job the pipeline cannot run before it reaches the queue.
            # The check does not download: fetching a model belongs in the worker,
            # where its progress is visible, not in a request handler.
            core.check_denoise_settings(core.parse_args(argv))
        except core.PipelineError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        except SystemExit:
            raise HTTPException(
                status_code=400, detail="argparse rejected these settings"
            ) from None

        live_dir: Path | None = None
        if live_preview_allowed(config, settings):
            live_dir = live_root() / uuid.uuid4().hex[:12]
            settings["live_dir"] = str(live_dir)
            # The viewer, not the pipeline, decides when the segments go.
            settings["keep_live_dir"] = True

        job = manager.submit(
            kind="convert",
            source=str(source),
            settings=settings,
            live_dir=live_dir,
            input_duration=effective_duration(source, settings),
        )
        return job.summary(manager.model, manager.finish_times(time.time()).get(job.id))

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        return {**job.summary(), "events": job.events}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        return manager.cancel(job_id).summary()

    @app.get("/api/jobs/{job_id}/live/{name}")
    def live_segment(job_id: str, name: str) -> FileResponse:
        """Serve one playlist or segment of a job being rendered."""

        job = manager.get(job_id)
        if job.live_dir is None or not LIVE_SERVE_RE.match(name):
            raise HTTPException(status_code=404, detail="No such preview file")
        path = job.live_dir / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="Not written yet")
        kind = (
            "application/vnd.apple.mpegurl"
            if name.endswith(".m3u8")
            else "video/iso.segment"
        )
        # The playlist grows; a cached copy would freeze the preview.
        return FileResponse(path, media_type=kind, headers={"Cache-Control": "no-store"})

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str) -> StreamingResponse:
        job, channel, history = manager.subscribe(job_id)
        terminal = {STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED}

        def stream() -> Iterator[str]:
            try:
                for event in history:
                    yield sse(event)
                # A finished job has nothing more to send: replay its history and
                # close, or the browser holds an open connection per job viewed.
                if job.status in terminal:
                    return
                while True:
                    try:
                        event = channel.get(timeout=SSE_KEEPALIVE_SECONDS)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    if event is None:
                        return
                    yield sse(event)
            finally:
                manager.unsubscribe(job, channel)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:

        @app.get("/")
        def missing_static() -> JSONResponse:
            return JSONResponse(
                {"detail": f"Static assets not found at {STATIC_DIR}"},
                status_code=500,
            )

    return app


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def parse_server_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the LectureCut web UI on localhost."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        help="Directory to offer for file selection; repeatable",
    )
    parser.add_argument(
        "--upload-dir",
        type=Path,
        help="Where browser uploads land; defaults to a temporary directory",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Refuse browser uploads and only serve files from the roots",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not let the page open finished files on this desktop",
    )
    parser.add_argument(
        "--no-live-preview",
        action="store_true",
        help="Do not render through a playlist; results can only be watched when done",
    )
    parser.add_argument(
        "--no-browse",
        action="store_true",
        help="Keep the page to the given roots instead of letting it choose folders",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    roots = [path.expanduser().resolve() for path in (args.root or [])]
    default_source: Path | None = None
    if not roots:
        cwd = Path.cwd().resolve()
        roots = [cwd]
        data_dir = cwd / "data"
        if data_dir.exists() and data_dir not in roots:
            roots.append(data_dir.resolve())
            # Where the lectures conventionally live, rather than the whole project.
            default_source = data_dir.resolve()
    upload_dir = (
        args.upload_dir.expanduser().resolve()
        if args.upload_dir
        else Path(tempfile.gettempdir()) / "lecturecut-uploads"
    )
    # Uploads must be selectable afterwards, so their directory is a root too.
    if upload_dir not in roots:
        roots.append(upload_dir)
    config = Config(
        roots=roots,
        upload_dir=upload_dir,
        allow_upload=not args.no_upload,
        allow_open=not args.no_open,
        allow_browse=not args.no_browse,
        live_preview=not args.no_live_preview,
        state_path=state_file_path(),
        throughput_path=state_file_path().with_name("throughput.json"),
        allowed_hosts=allowed_hosts_for(args.host, args.port),
        require_csrf_header=True,
        source_dir=default_source,
    )
    config.load_state()
    return config


def allowed_hosts_for(host: str, port: int) -> set[str] | None:
    """Host headers this server answers to.

    Bound to a wildcard address the server is reachable under names it cannot
    predict, so the check is dropped - binding it that way is a deliberate choice
    to expose it.
    """

    if host in {"0.0.0.0", "::", ""}:
        return None
    names = {host, "127.0.0.1", "localhost", "[::1]"}
    return {f"{name}:{port}".lower() for name in names}


def serve(argv: list[str] | None = None) -> int:
    import uvicorn

    args = parse_server_args(argv)
    if shutil.which("ffmpeg") is None:
        print("Warning: ffmpeg was not found on PATH; jobs will fail.")
    config = build_config(args)
    purge_stale_previews()
    app = create_app(config)
    print(f"LectureCut UI on http://{args.host}:{args.port}", flush=True)
    print(f"  input folder: {config.current_source_dir()}", flush=True)
    for root in config.roots:
        print(f"  root: {root}", flush=True)
    if config.allowed_hosts is None:
        print("  Warning: bound to all interfaces; the Host check is off", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
