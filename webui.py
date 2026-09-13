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
import queue
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
def model_hint() -> str:
    """Built per request: the cache location follows the environment."""

    return (
        f"Модели по ~300 КБ, скачиваются по кнопке в кэш {core.model_cache_dir()}. "
        "Хеши зашиты, так что загрузка проверяется"
    )
# A listing should stay responsive even when pointed at a large archive.
PROBE_LIMIT = 400
EVENT_HISTORY_LIMIT = 2000
SSE_KEEPALIVE_SECONDS = 15.0

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class Job:
    """One unit of work. `kind` exists so previews can join later as a sibling."""

    id: str
    kind: str
    source: str
    settings: dict[str, Any]
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

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
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

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_forever, daemon=True)
        self._worker.start()

    # -- public API -----------------------------------------------------------

    def submit(self, *, kind: str, source: str, settings: dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, source=source, settings=settings)
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

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._jobs[job_id].summary() for job_id in reversed(self._order)]

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

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.finished_at = time.time()
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
            elif kind == "progress":
                job.overall = float(fields.get("overall") or 0.0)
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
    roots: list[Path]
    upload_dir: Path
    allow_upload: bool = True
    allow_open: bool = True


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
    for root in config.roots:
        if not root.exists():
            continue
        candidates = [root, *(path for path in root.rglob("*") if path.is_dir())]
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
            else sorted(directory.rglob(f"*{core.ARNNDN_MODEL_SUFFIX}"))
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
    entries: list[dict[str, Any]] = []
    for root in config.roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            stat = path.stat()
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


def create_app(config: Config, jobs: JobManager | None = None) -> FastAPI:
    app = FastAPI(title="LectureCut", docs_url=None, redoc_url=None)
    manager = jobs or JobManager()
    processed = ProcessedIndex()
    app.state.config = config
    app.state.jobs = manager
    app.state.processed = processed

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        return {
            "groups": core.parser_schema(),
            "roots": [str(root) for root in config.roots],
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
        return {"files": list_media(config, processed)}

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
        return {"path": str(target), "name": safe_name, "size": written}

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
        return {"jobs": manager.listing()}

    @app.post("/api/jobs")
    def create_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw_source = payload.get("source")
        if not raw_source:
            raise HTTPException(status_code=400, detail="A source file is required")
        source = resolve_within_roots(str(raw_source), config)

        settings = dict(payload.get("settings") or {})
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

        job = manager.submit(kind="convert", source=str(source), settings=settings)
        return job.summary()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        return {**job.summary(), "events": job.events}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        return manager.cancel(job_id).summary()

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
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    roots = [path.expanduser().resolve() for path in (args.root or [])]
    if not roots:
        cwd = Path.cwd().resolve()
        roots = [cwd]
        data_dir = cwd / "data"
        if data_dir.exists() and data_dir not in roots:
            roots.append(data_dir.resolve())
    upload_dir = (
        args.upload_dir.expanduser().resolve()
        if args.upload_dir
        else Path(tempfile.gettempdir()) / "lecturecut-uploads"
    )
    # Uploads must be selectable afterwards, so their directory is a root too.
    if upload_dir not in roots:
        roots.append(upload_dir)
    return Config(
        roots=roots,
        upload_dir=upload_dir,
        allow_upload=not args.no_upload,
        allow_open=not args.no_open,
    )


def serve(argv: list[str] | None = None) -> int:
    import uvicorn

    args = parse_server_args(argv)
    if shutil.which("ffmpeg") is None:
        print("Warning: ffmpeg was not found on PATH; jobs will fail.")
    config = build_config(args)
    app = create_app(config)
    print(f"LectureCut UI on http://{args.host}:{args.port}")
    for root in config.roots:
        print(f"  root: {root}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
