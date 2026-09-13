#!/usr/bin/env python3
"""LectureCut: fast lecture cleanup with Python orchestration and FFmpeg."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from statistics import median
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


@dataclass
class Reporter:
    """Where progress goes. The CLI prints it; the web UI queues it.

    Text lines stay human-readable for the terminal, while `event` carries the
    same information structured, so a UI never has to parse prose.
    """

    on_line: "object" = None
    on_event: "object" = None

    def line(self, text: str, *, error: bool = False) -> None:
        if self.on_line is not None:
            self.on_line(text, error)
        else:
            print(text, file=sys.stderr if error else sys.stdout, flush=True)

    def event(self, kind: str, **fields: object) -> None:
        if self.on_event is not None:
            self.on_event(kind, fields)


ACTIVE_REPORTER: ContextVar[Reporter] = ContextVar("ACTIVE_REPORTER")
ACTIVE_CANCEL: ContextVar[threading.Event] = ContextVar("ACTIVE_CANCEL")

# How much more expensive one second of video render is than one second of an
# audio-only analysis pass. Measured: 46.5s to render 180s (0.26 s/s) against
# 157s for a full audio pass over 5140s (0.03 s/s).
RENDER_COST_RATIO = 8.5
CANCEL_POLL_SECONDS = 0.2
CANCEL_GRACE_SECONDS = 5.0


class PipelineCancelled(Exception):
    """The user asked to stop. Not an error, so it is reported separately."""


def cancel_event() -> threading.Event:
    try:
        return ACTIVE_CANCEL.get()
    except LookupError:
        default = threading.Event()
        ACTIVE_CANCEL.set(default)
        return default


def raise_if_cancelled() -> None:
    if cancel_event().is_set():
        raise PipelineCancelled("Cancelled")


PHASE_MEASURE = "measure"
PHASE_CALIBRATE = "calibrate"
PHASE_SILENCE = "silence"
PHASE_RENDER = "render"

PHASE_LABELS = {
    PHASE_MEASURE: "Measuring audio",
    PHASE_CALIBRATE: "Calibrating settings",
    PHASE_SILENCE: "Detecting silence",
    PHASE_RENDER: "Rendering",
}


@dataclass
class ProgressTracker:
    """Maps per-phase progress onto one overall fraction.

    The phases differ in cost by two orders of magnitude - measurement is a fixed
    number of short windows, while the render scales with the whole lecture - so a
    bar that treated them equally would sit at 75% for most of the run.
    """

    weights: dict[str, float] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    current: str | None = None

    def normalized(self) -> dict[str, float]:
        total = sum(self.weights.values())
        if total <= 0:
            return {name: 0.0 for name in self.weights}
        return {name: weight / total for name, weight in self.weights.items()}

    def completed_before(self, phase: str) -> float:
        shares = self.normalized()
        done = 0.0
        for name in self.order:
            if name == phase:
                break
            done += shares.get(name, 0.0)
        return done

    def start(self, phase: str) -> None:
        self.current = phase
        report_event(
            "phase",
            phase=phase,
            label=PHASE_LABELS.get(phase, phase),
            overall=self.completed_before(phase),
        )

    def advance(self, fraction: float) -> None:
        if self.current is None:
            return
        share = self.normalized().get(self.current, 0.0)
        overall = self.completed_before(self.current) + share * clamp(fraction, 0.0, 1.0)
        report_event(
            "progress",
            phase=self.current,
            fraction=clamp(fraction, 0.0, 1.0),
            overall=clamp(overall, 0.0, 1.0),
        )


ACTIVE_PROGRESS: ContextVar[ProgressTracker] = ContextVar("ACTIVE_PROGRESS")


def progress_tracker() -> ProgressTracker:
    try:
        return ACTIVE_PROGRESS.get()
    except LookupError:
        default = ProgressTracker()
        ACTIVE_PROGRESS.set(default)
        return default


def report_progress(fraction: float) -> None:
    progress_tracker().advance(fraction)


def start_phase(phase: str) -> None:
    progress_tracker().start(phase)


def plan_progress_weights(
    *,
    duration: float,
    args: argparse.Namespace,
) -> ProgressTracker:
    """Weight phases by the audio-seconds each one has to push through ffmpeg."""

    window = min(args.analysis_window, duration) if duration > 0 else args.analysis_window
    measure = 0.0 if args.no_analyze else args.analysis_samples * window
    calibrate = 0.0
    if not args.no_analyze:
        silence_rounds = 3 * window
        gain_rounds = 2 * GAIN_CALIBRATION_WINDOWS * min(window, GAIN_CALIBRATION_WINDOW)
        calibrate = silence_rounds + gain_rounds
    detect = 0.0 if args.no_cut_silence else duration
    render = 0.0 if args.dry_run else duration * RENDER_COST_RATIO
    return ProgressTracker(
        weights={
            PHASE_MEASURE: measure,
            PHASE_CALIBRATE: calibrate,
            PHASE_SILENCE: detect,
            PHASE_RENDER: render,
        },
        order=[PHASE_MEASURE, PHASE_CALIBRATE, PHASE_SILENCE, PHASE_RENDER],
    )


def reporter() -> Reporter:
    try:
        return ACTIVE_REPORTER.get()
    except LookupError:
        default = Reporter()
        ACTIVE_REPORTER.set(default)
        return default


def report(text: str = "", *, error: bool = False) -> None:
    reporter().line(text, error=error)


def report_event(kind: str, **fields: object) -> None:
    reporter().event(kind, **fields)


class PipelineError(Exception):
    """A failure the user can act on: bad input, missing tool, unusable range.

    Raised instead of SystemExit so callers other than the CLI - the web UI's
    worker, tests - can catch it and report it in their own way.
    """


SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<start>[0-9.]+)")
SILENCE_END_RE = re.compile(
    r"silence_end:\s*(?P<end>[0-9.]+)\s*\|\s*silence_duration:\s*(?P<duration>[0-9.]+)"
)


SUPPRESS_DEST = "source"

AUDIO_DENOISE_MODES = ("auto", "afftdn", "anlmdn", "arnndn", "none")
AUDIO_LOUDNESS_MODES = ("dynaudnorm", "speechnorm", "loudnorm", "none")

# Used when measurement is skipped or fails. The silence threshold is well below
# the -35dB that suits a headset recording: phone-recorded lectures routinely sit
# at a speech RMS of -40dB, where -35dB classifies speech itself as silence.
FALLBACK_SILENCE_THRESHOLD_DB = -45.0
FALLBACK_NOISE_FLOOR_DB = -55.0
FALLBACK_SPEECH_LUFS = -35.0

SILENCE_THRESHOLD_LIMITS = (-70.0, -20.0)
SILENCE_FRACTION_TARGET = 0.18
SILENCE_FRACTION_MIN = 0.05
SILENCE_FRACTION_MAX = 0.40
SILENCE_CALIBRATION_STEP_DB = 3.0
SILENCE_CALIBRATION_ROUNDS = 4

# A static boost after peak normalization would only drive the limiter, so the
# fine trim stays small and any real lift comes from crest reduction instead.
GAIN_BIAS_LIMITS = (-6.0, 2.0)
GAIN_MAKEUP_MAX_DB = 7.0
GAIN_CALIBRATION_WINDOWS = 6
GAIN_CALIBRATION_WINDOW = 20.0

# alimiter caps sample peaks, but ebur128 and AAC report true (inter-sample)
# peaks, which run about a decibel higher. Keeping that margin makes the
# --true-peak flag mean what its name says.
TRUE_PEAK_MARGIN_DB = 1.0

EBUR128_I_RE = re.compile(r"^\s+I:\s*(-?[\d.]+|-?inf)\s*LUFS", re.M)
EBUR128_LRA_RE = re.compile(r"^\s+LRA:\s*(-?[\d.]+|-?inf)\s*LU", re.M)
EBUR128_PEAK_RE = re.compile(r"^\s+Peak:\s*(-?[\d.]+|-?inf)\s*dBFS", re.M)


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool
    has_video: bool
    video_fps: float


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class WindowStats:
    """EBU R128 and astats readings for one sampled window."""

    start: float
    lufs: float | None
    lra: float | None
    true_peak_db: float | None
    rms_db: float | None
    noise_floor_db: float | None
    channel_peaks_db: tuple[float, ...]


@dataclass(frozen=True)
class AudioAnalysis:
    windows: tuple[WindowStats, ...]
    starts: tuple[float, ...]
    speech_lufs: float
    speech_lufs_median: float
    noise_floor_db: float
    true_peak_db: float
    lra: float
    channel_imbalance_db: float

    @property
    def snr_db(self) -> float:
        return self.speech_lufs_median - self.noise_floor_db

    @property
    def headroom_db(self) -> float:
        return -self.true_peak_db


def build_parser() -> argparse.ArgumentParser:
    """Single source of truth for every knob: the CLI and the web UI share it."""

    parser = argparse.ArgumentParser(
        description=(
            "Cut silence, denoise/normalize audio, speed up, and render a lecture "
            "with FFmpeg. Input can be a local file or a URL supported by yt-dlp."
        )
    )
    parser.add_argument("source", help="Local media file or video URL")
    parser.add_argument("-o", "--output", type=Path, help="Output MP4 path")
    parser.add_argument("--force", action="store_true", help="Overwrite output if it exists")
    parser.add_argument(
        "--filtergraph-mode",
        choices=("select", "concat"),
        default="select",
        help="select is faster for long lectures; concat is useful for debugging",
    )

    input_group = parser.add_argument_group("input")
    input_group.add_argument(
        "--download-format",
        default="bv*+ba/best",
        help="yt-dlp format selector for URL inputs",
    )
    input_group.add_argument(
        "--start",
        type=non_negative_float,
        default=0.0,
        help="Start processing at this input timestamp, seconds",
    )
    input_group.add_argument(
        "--limit",
        type=positive_float,
        help="Process only the first N seconds; useful for previews/tests",
    )
    input_group.add_argument(
        "--workdir",
        type=Path,
        help="Directory for downloaded inputs and generated FFmpeg scripts",
    )
    input_group.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Do not delete temporary workdir after the run",
    )

    silence_group = parser.add_argument_group("silence cutting")
    silence_group.add_argument(
        "--no-cut-silence",
        action="store_true",
        help="Keep all timeline content; still applies audio filters and speed",
    )
    silence_group.add_argument(
        "--silence-threshold",
        default="auto",
        help=(
            "silencedetect noise threshold, e.g. -45dB. 'auto' derives it from the "
            "measured noise floor and calibrates against the real silence fraction"
        ),
    )
    silence_group.add_argument(
        "--min-silence",
        type=positive_float,
        default=0.35,
        help="Minimum silence duration to cut, seconds",
    )
    silence_group.add_argument(
        "--padding",
        type=non_negative_float,
        default=0.12,
        help="Speech padding retained around removed silences, seconds",
    )
    silence_group.add_argument(
        "--min-cut",
        type=non_negative_float,
        default=0.08,
        help="Ignore cuts shorter than this after padding, seconds",
    )
    silence_group.add_argument(
        "--min-segment",
        type=non_negative_float,
        default=0.04,
        help="Drop kept segments shorter than this, seconds",
    )

    audio_group = parser.add_argument_group("audio")
    audio_group.add_argument(
        "--denoise",
        choices=AUDIO_DENOISE_MODES,
        default="auto",
        help="Denoiser; auto picks arnndn when a model is given, otherwise afftdn",
    )
    audio_group.add_argument(
        "--arnndn-model",
        help="Path to an .rnnn model for the arnndn speech denoiser",
    )
    audio_group.add_argument(
        "--anlmdn-strength",
        type=positive_float,
        default=0.0008,
        help="anlmdn denoising strength; higher is stronger but slower",
    )
    audio_group.add_argument(
        "--loudness",
        choices=AUDIO_LOUDNESS_MODES,
        default="dynaudnorm",
        help=(
            "Loudness stage; dynaudnorm lifts quiet speech per frame without the "
            "pumping one-pass loudnorm causes on low-level sources"
        ),
    )
    audio_group.add_argument(
        "--target-lufs",
        type=float,
        default=-17.0,
        help="Integrated loudness target used for automatic gain calibration",
    )
    audio_group.add_argument(
        "--true-peak",
        type=float,
        default=-1.0,
        help="Output true peak ceiling in dBFS, enforced by alimiter",
    )
    audio_group.add_argument(
        "--highpass",
        type=non_negative_float,
        default=85.0,
        help="High-pass cutoff in Hz that removes rumble before gain; 0 disables",
    )
    audio_group.add_argument(
        "--no-gate",
        action="store_true",
        help="Do not attenuate residual room tone between phrases",
    )
    audio_group.add_argument(
        "--mono",
        action="store_true",
        help="Downmix to mono; useful when one channel of a phone mic is hotter",
    )
    audio_group.add_argument(
        "--declick",
        action="store_true",
        help="Run adeclick before gain to tame knocks and mic bumps",
    )
    audio_group.add_argument(
        "--gain-bias",
        type=float,
        help="Static trim in dB after the loudness stage; default is calibrated",
    )
    audio_group.add_argument(
        "--loudnorm-i",
        type=float,
        default=-16.0,
        help="Integrated loudness target for loudnorm",
    )
    audio_group.add_argument(
        "--loudnorm-tp",
        type=float,
        default=-1.5,
        help="True peak target for loudnorm",
    )
    audio_group.add_argument(
        "--loudnorm-lra",
        type=float,
        default=11.0,
        help="Loudness range target for loudnorm",
    )
    audio_group.add_argument(
        "--audio-bitrate",
        default="128k",
        help="AAC output audio bitrate",
    )
    audio_group.add_argument(
        "--audio-sample-rate",
        type=positive_int,
        default=48000,
        help="Output audio sample rate",
    )

    speed_group = parser.add_argument_group("speed")
    speed_group.add_argument(
        "--speed",
        type=positive_float,
        default=1.25,
        help="Playback speed multiplier after silence cuts",
    )

    video_group = parser.add_argument_group("video")
    video_group.add_argument(
        "--encoder",
        choices=("auto", "h264_nvenc", "hevc_nvenc", "libx264"),
        default="auto",
        help="Video encoder; auto prefers h264_nvenc when available",
    )
    video_group.add_argument(
        "--nvenc-preset",
        default="p4",
        help="NVENC preset, e.g. p1 fastest through p7 highest quality",
    )
    video_group.add_argument(
        "--x264-preset",
        default="veryfast",
        help="libx264 preset used for CPU fallback",
    )
    video_group.add_argument(
        "--cq",
        type=int,
        default=23,
        help="NVENC constant quality value",
    )
    video_group.add_argument(
        "--crf",
        type=int,
        default=23,
        help="libx264 CRF value",
    )

    analysis_group = parser.add_argument_group("analysis")
    analysis_group.add_argument(
        "--analysis-samples",
        type=positive_int,
        default=8,
        help="Number of windows sampled across the input to measure audio",
    )
    analysis_group.add_argument(
        "--analysis-window",
        type=positive_float,
        default=20.0,
        help="Length of each analysis window, seconds",
    )
    analysis_group.add_argument(
        "--no-analyze",
        action="store_true",
        help="Skip measurement and use static fallbacks for every derived setting",
    )

    preview_group = parser.add_argument_group("preview sweep")
    preview_group.add_argument(
        "--preview-dir",
        type=Path,
        help="Render short comparison clips into this directory and exit",
    )
    preview_group.add_argument(
        "--preview-start",
        type=non_negative_float,
        help="Preview sample start timestamp; defaults to --start",
    )
    preview_group.add_argument(
        "--preview-duration",
        type=positive_float,
        default=10.0,
        help="Preview sample duration per combination, seconds",
    )
    preview_group.add_argument(
        "--preview-thresholds",
        default="-45dB",
        help="Comma-separated silencedetect thresholds to compare",
    )
    preview_group.add_argument(
        "--preview-denoise",
        default="auto",
        help=f"Comma-separated denoise modes to compare: {','.join(AUDIO_DENOISE_MODES)}",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned commands and filtergraph without rendering",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def option_strings_by_dest(parser: argparse.ArgumentParser) -> dict[str, str]:
    """Map each dest to its longest option string, e.g. target_lufs -> --target-lufs."""

    mapping: dict[str, str] = {}
    for action in parser._actions:
        if not action.option_strings or action.dest == "help":
            continue
        mapping[action.dest] = max(action.option_strings, key=len)
    return mapping


def field_kind(action: argparse.Action) -> str:
    if isinstance(action, argparse._StoreTrueAction):
        return "flag"
    if action.choices:
        return "choice"
    if action.type in (positive_int,):
        return "integer"
    if action.type in (positive_float, non_negative_float, float):
        return "number"
    if action.type is Path:
        return "path"
    return "text"


def parser_schema() -> list[dict[str, object]]:
    """Describe the knobs so a UI can render them without restating defaults.

    Help strings become the UI's own tooltips, which keeps the interface and the
    CLI documentation from drifting apart.
    """

    parser = build_parser()
    groups: list[dict[str, object]] = []
    seen: set[str] = set()
    for group in parser._action_groups:
        fields: list[dict[str, object]] = []
        for action in group._group_actions:
            if not action.option_strings or action.dest in {"help", SUPPRESS_DEST}:
                continue
            if action.dest in seen:
                continue
            seen.add(action.dest)
            fields.append(
                {
                    "dest": action.dest,
                    "option": max(action.option_strings, key=len),
                    "kind": field_kind(action),
                    "choices": list(action.choices) if action.choices else None,
                    "default": action.default,
                    "help": action.help or "",
                }
            )
        if fields:
            groups.append({"title": group.title or "options", "fields": fields})
    return groups


def settings_to_argv(source: str, settings: dict[str, object]) -> list[str]:
    """Turn a UI payload into argv so argparse stays the only validator."""

    parser = build_parser()
    options = option_strings_by_dest(parser)
    flags = {
        action.dest
        for action in parser._actions
        if isinstance(action, argparse._StoreTrueAction)
    }
    unknown = sorted(set(settings) - set(options))
    if unknown:
        raise PipelineError(f"Unknown setting(s): {', '.join(unknown)}")

    argv = [source]
    for dest, value in settings.items():
        if value is None:
            continue
        option = options[dest]
        if dest in flags:
            if value:
                argv.append(option)
            continue
        # `--option=value`, never two tokens: a value such as -45dB looks like an
        # option to argparse and would be rejected when passed separately.
        argv.append(f"{option}={value}")
    return argv


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise PipelineError(f"Required command not found on PATH: {name}")


def parse_progress_seconds(line: str) -> float | None:
    """Read one `-progress pipe:1` key=value line, returning output seconds."""

    key, _, value = line.strip().partition("=")
    if key == "out_time_us" or key == "out_time_ms":
        try:
            micros = float(value)
        except ValueError:
            return None
        # ffmpeg mislabels out_time_ms: both keys carry microseconds.
        return micros / 1_000_000.0
    return None


def with_progress_output(command: list[str]) -> list[str]:
    """Ask ffmpeg for machine-readable progress on stdout."""

    if not command or not Path(command[0]).name.startswith("ffmpeg"):
        return command
    return [command[0], "-progress", "pipe:1", "-nostats", *command[1:]]


def run_command(
    command: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    progress_total: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child process, cancellable, optionally reporting ffmpeg progress.

    Popen rather than subprocess.run so a cancel can reach the child: a blocking
    run() would leave ffmpeg working for the rest of a long render.
    """

    cancel = cancel_event()
    raise_if_cancelled()

    track_progress = progress_total is not None and progress_total > 0
    if track_progress:
        command = with_progress_output(command)

    want_stdout = capture or track_progress
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE if want_stdout else None,
        stderr=subprocess.PIPE if capture else None,
    )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def drain_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if track_progress:
                seconds = parse_progress_seconds(line)
                if seconds is not None:
                    report_progress(min(1.0, seconds / progress_total))
                continue
            stdout_parts.append(line)

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_parts.append(line)

    # Both pipes are drained in threads: silencedetect over a long lecture emits
    # far more than a pipe buffer holds, and a full pipe would stall ffmpeg.
    #
    # Each thread runs inside its OWN copy of this context. A bare thread starts
    # with an empty context, which would make the caller's reporter and progress
    # tracker invisible; sharing one copy is equally wrong, because a Context can
    # only be entered once at a time and the second thread would die on entry -
    # taking its pipe drain with it, until ffmpeg blocks on a full pipe.
    def in_context(target: "object") -> "object":
        context = copy_context()
        return lambda: context.run(target)

    readers: list[threading.Thread] = []
    if want_stdout:
        readers.append(threading.Thread(target=in_context(drain_stdout), daemon=True))
    if capture:
        readers.append(threading.Thread(target=in_context(drain_stderr), daemon=True))
    for reader in readers:
        reader.start()

    cancelled = False
    while process.poll() is None:
        if cancel.is_set():
            cancelled = True
            process.terminate()
            try:
                process.wait(timeout=CANCEL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
            break
        cancel.wait(CANCEL_POLL_SECONDS)

    process.wait()
    for reader in readers:
        reader.join(timeout=CANCEL_GRACE_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()

    if cancelled:
        raise PipelineCancelled("Cancelled")

    result = subprocess.CompletedProcess(
        command,
        process.returncode,
        "".join(stdout_parts),
        "".join(stderr_parts),
    )
    if check and result.returncode != 0:
        if result.stdout:
            report(result.stdout, error=True)
        if result.stderr:
            report(result.stderr, error=True)
        raise subprocess.CalledProcessError(result.returncode, command)
    return result


def ffmpeg_input_options(start: float = 0.0, limit: float | None = None) -> list[str]:
    options: list[str] = []
    if start > 0:
        options.extend(["-ss", format_seconds(start)])
    if limit is None:
        return options
    options.extend(["-t", format_seconds(limit)])
    return options


def default_output_path(source: str) -> Path:
    if is_url(source):
        return Path("lecturecut-output.mp4")
    input_path = Path(source)
    return input_path.with_name(f"{input_path.stem}.lecturecut.mp4")


def prepare_input(source: str, workdir: Path, download_format: str) -> Path:
    if not is_url(source):
        input_path = Path(source).expanduser()
        if not input_path.exists():
            raise PipelineError(f"Input file does not exist: {input_path}")
        return input_path

    require_command("yt-dlp")
    command = [
        "yt-dlp",
        "--no-playlist",
        "-f",
        download_format,
        "--merge-output-format",
        "mp4",
        "-P",
        str(workdir),
        "-o",
        "source.%(ext)s",
        "--print",
        "after_move:filepath",
        source,
    ]
    report("Downloading input with yt-dlp...")
    result = run_command(command, capture=True)
    candidates = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    for candidate in reversed(candidates):
        if candidate.exists():
            return candidate
    files = sorted(workdir.glob("source.*"), key=lambda path: path.stat().st_mtime)
    if files:
        return files[-1]
    raise PipelineError("yt-dlp finished, but no downloaded file was found")


def probe_media(
    path: Path,
    *,
    start: float = 0.0,
    limit: float | None = None,
) -> MediaInfo:
    require_command("ffprobe")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    result = run_command(command, capture=True)
    data = json.loads(result.stdout)
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"Could not read media duration for {path}") from exc
    duration = max(0.0, duration - start)
    if limit is not None:
        duration = min(duration, limit)
    streams = data.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        {},
    )
    return MediaInfo(
        duration=duration,
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
        has_video=any(stream.get("codec_type") == "video" for stream in streams),
        video_fps=stream_fps(video_stream),
    )


def stream_fps(stream: dict[str, object]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if isinstance(value, str):
            fps = parse_fraction(value)
            if fps > 0:
                return fps
    return 30.0


def parse_fraction(value: str) -> float:
    if "/" not in value:
        return float(value)
    numerator, denominator = value.split("/", 1)
    denominator_float = float(denominator)
    if denominator_float == 0:
        return 0.0
    return float(numerator) / denominator_float


def detect_silences(
    path: Path,
    *,
    threshold: str,
    min_silence: float,
    duration: float,
    start: float,
    limit: float | None,
) -> list[Silence]:
    require_command("ffmpeg")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        *ffmpeg_input_options(start, limit),
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={threshold}:d={format_seconds(min_silence)}",
        "-f",
        "null",
        "-",
    ]
    result = run_command(command, capture=True, progress_total=duration)
    log = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return parse_silencedetect_log(log, duration)


def parse_silencedetect_log(log: str, media_duration: float) -> list[Silence]:
    silences: list[Silence] = []
    pending_start: float | None = None
    for line in log.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            pending_start = float(start_match.group("start"))
            continue
        end_match = SILENCE_END_RE.search(line)
        if end_match and pending_start is not None:
            end = float(end_match.group("end"))
            if end > pending_start:
                silences.append(Silence(pending_start, min(end, media_duration)))
            pending_start = None
    if pending_start is not None and pending_start < media_duration:
        silences.append(Silence(pending_start, media_duration))
    return silences


def silences_to_segments(
    silences: Iterable[Silence],
    *,
    duration: float,
    padding: float,
    min_cut: float,
    min_segment: float,
) -> list[Segment]:
    cuts: list[Segment] = []
    for silence in sorted(silences, key=lambda item: item.start):
        cut_start = max(0.0, silence.start + padding)
        cut_end = min(duration, silence.end - padding)
        if cut_end - cut_start >= min_cut:
            cuts.append(Segment(cut_start, cut_end))

    merged_cuts = merge_segments(cuts, max_gap=0.0)
    kept: list[Segment] = []
    cursor = 0.0
    for cut in merged_cuts:
        if cut.start > cursor and cut.start - cursor >= min_segment:
            kept.append(Segment(cursor, cut.start))
        cursor = max(cursor, cut.end)
    if duration > cursor and duration - cursor >= min_segment:
        kept.append(Segment(cursor, duration))
    return kept or [Segment(0.0, duration)]


def merge_segments(segments: Iterable[Segment], *, max_gap: float) -> list[Segment]:
    ordered = sorted(segments, key=lambda item: item.start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for segment in ordered[1:]:
        previous = merged[-1]
        if segment.start <= previous.end + max_gap:
            merged[-1] = Segment(previous.start, max(previous.end, segment.end))
        else:
            merged.append(segment)
    return merged


def atempo_filters(speed: float) -> list[str]:
    remaining = speed
    filters: list[str] = []
    while remaining > 2.0:
        filters.append("atempo=2.000000")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.500000")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6:
        filters.append(f"atempo={remaining:.6f}")
    return filters


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def db_to_linear(value: float) -> float:
    return 10.0 ** (value / 20.0)


def combine_loudness(values: list[float]) -> float:
    """Combine per-window LUFS readings the way EBU R128 integrates them.

    Integrated loudness is an energy mean, so it tracks the loud passages rather
    than the typical window. Averaging in dB (or taking a median) understates the
    result: on a sampled lecture the median of four windows read 2.6 dB below the
    loudness of the same material measured end to end, while the energy mean
    landed within 1 dB.
    """

    if not values:
        raise ValueError("no loudness readings to combine")
    energy = sum(10.0 ** (value / 10.0) for value in values) / len(values)
    return 10.0 * math.log10(energy) if energy > 0 else float("-inf")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty sequence")
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def parse_measurement(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def first_match(pattern: re.Pattern[str], log: str) -> float | None:
    match = pattern.search(log)
    return parse_measurement(match.group(1)) if match else None


def parse_astats_blocks(log: str) -> tuple[list[dict[str, float | None]], dict[str, float | None]]:
    """Split an astats dump into per-channel blocks plus the Overall block."""

    channels: list[dict[str, float | None]] = []
    overall: dict[str, float | None] = {}
    current: dict[str, float | None] | None = None
    for raw in log.splitlines():
        line = raw.split("] ", 1)[-1].strip() if "] " in raw else raw.strip()
        if line.startswith("Channel:"):
            current = {}
            channels.append(current)
            continue
        if line == "Overall":
            current = overall
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = parse_measurement(value.strip())
    return channels, overall


def run_audio_probe(
    path: Path,
    *,
    start: float,
    duration: float,
    audio_filter: str,
) -> str:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        *ffmpeg_input_options(start, duration),
        "-i",
        str(path),
        "-vn",
        "-map",
        "0:a:0",
        "-af",
        audio_filter,
        "-f",
        "null",
        "-",
    ]
    result = run_command(command, capture=True)
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def sample_windows(duration: float, *, count: int, window: float) -> list[float]:
    """Evenly spaced window starts, trimmed away from the very edges."""

    usable = duration - window
    if usable <= 0:
        return [0.0]
    margin = min(5.0, usable * 0.05)
    span = max(0.0, usable - 2 * margin)
    if count <= 1 or span <= 0:
        return [margin + span / 2.0]
    step = span / (count - 1)
    return [margin + index * step for index in range(count)]


def measure_window(path: Path, *, start: float, duration: float) -> WindowStats:
    log = run_audio_probe(
        path,
        start=start,
        duration=duration,
        audio_filter="ebur128=peak=true,astats=metadata=1:reset=0",
    )
    channels, overall = parse_astats_blocks(log)
    channel_peaks = tuple(
        peak
        for peak in (channel.get("Peak level dB") for channel in channels)
        if peak is not None
    )
    return WindowStats(
        start=start,
        lufs=first_match(EBUR128_I_RE, log),
        lra=first_match(EBUR128_LRA_RE, log),
        true_peak_db=first_match(EBUR128_PEAK_RE, log),
        rms_db=overall.get("RMS level dB"),
        noise_floor_db=overall.get("Noise floor dB"),
        channel_peaks_db=channel_peaks,
    )


def analyze_audio(
    *,
    input_path: Path,
    media: MediaInfo,
    args: argparse.Namespace,
) -> AudioAnalysis | None:
    """Measure a handful of windows so every derived setting fits this recording."""

    starts = sample_windows(
        media.duration,
        count=args.analysis_samples,
        window=args.analysis_window,
    )
    window = min(args.analysis_window, media.duration)
    report(
        f"Analyzing audio: {len(starts)} window(s) of {window:.0f}s...",
    )
    start_phase(PHASE_MEASURE)
    windows: list[WindowStats] = []
    for index, start in enumerate(starts):
        windows.append(
            measure_window(input_path, start=args.start + start, duration=window)
        )
        report_progress((index + 1) / len(starts))

    loudness = [item.lufs for item in windows if item.lufs is not None]
    floors = [item.noise_floor_db for item in windows if item.noise_floor_db is not None]
    peaks = [item.true_peak_db for item in windows if item.true_peak_db is not None]
    ranges = [item.lra for item in windows if item.lra is not None]
    if not loudness or not floors:
        report("Audio analysis produced no usable readings; using fallbacks")
        return None

    imbalance = 0.0
    for item in windows:
        if len(item.channel_peaks_db) >= 2:
            spread = max(item.channel_peaks_db) - min(item.channel_peaks_db)
            imbalance = max(imbalance, spread)

    return AudioAnalysis(
        windows=tuple(windows),
        starts=tuple(starts),
        speech_lufs=percentile(loudness, 0.1),
        speech_lufs_median=median(loudness),
        noise_floor_db=median(floors),
        true_peak_db=max(peaks) if peaks else 0.0,
        lra=max(ranges) if ranges else 0.0,
        channel_imbalance_db=imbalance,
    )


def format_audio_analysis(analysis: AudioAnalysis) -> str:
    return (
        f"Speech loudness: {analysis.speech_lufs_median:.1f} LUFS median, "
        f"{analysis.speech_lufs:.1f} LUFS in quiet passages\n"
        f"Noise floor: {analysis.noise_floor_db:.1f} dB (SNR {analysis.snr_db:.1f} dB)\n"
        f"True peak: {analysis.true_peak_db:.1f} dBFS "
        f"(headroom {analysis.headroom_db:.1f} dB)\n"
        f"Loudness range: up to {analysis.lra:.1f} LU, "
        f"channel imbalance {analysis.channel_imbalance_db:.1f} dB"
    )


CHANNEL_IMBALANCE_HINT_DB = 2.0


def audio_analysis_hints(analysis: AudioAnalysis) -> list[str]:
    hints: list[str] = []
    if analysis.channel_imbalance_db >= CHANNEL_IMBALANCE_HINT_DB:
        hints.append(
            f"Channels differ by {analysis.channel_imbalance_db:.1f} dB; --mono "
            "trades stereo for a cleaner single voice"
        )
    if analysis.headroom_db < 3.0:
        hints.append(
            f"Only {analysis.headroom_db:.1f} dB of headroom: a transient already "
            "sits near full scale, so --declick may help before gain"
        )
    if analysis.snr_db < 12.0:
        hints.append(
            f"SNR is {analysis.snr_db:.1f} dB; consider --denoise arnndn with a "
            "model for speech-aware denoising"
        )
    return hints


def derive_silence_threshold(analysis: AudioAnalysis | None) -> float:
    """Sit above the noise floor but safely below gated speech level."""

    if analysis is None:
        return FALLBACK_SILENCE_THRESHOLD_DB
    from_floor = analysis.noise_floor_db + 10.0
    from_speech = analysis.speech_lufs - 6.0
    return clamp(min(from_floor, from_speech), *SILENCE_THRESHOLD_LIMITS)


def measure_silence_fraction(
    input_path: Path,
    *,
    starts: Iterable[float],
    window: float,
    threshold_db: float,
    min_silence: float,
    offset: float,
) -> float:
    total = 0.0
    silent = 0.0
    for start in starts:
        log = run_audio_probe(
            input_path,
            start=offset + start,
            duration=window,
            audio_filter=(
                f"silencedetect=noise={threshold_db:.1f}dB:d={format_seconds(min_silence)}"
            ),
        )
        silences = parse_silencedetect_log(log, window)
        silent += sum(silence.duration for silence in silences)
        total += window
    return silent / total if total else 0.0


def calibrate_silence_threshold(
    input_path: Path,
    *,
    analysis: AudioAnalysis | None,
    args: argparse.Namespace,
) -> float:
    """Nudge the derived threshold until the measured silence share is sane.

    The noise floor alone can mislead: a room with HVAC hum and a speaker who
    pauses often need different thresholds even at the same floor. So the derived
    value is checked against how much of the sampled audio it would actually cut.
    """

    threshold = derive_silence_threshold(analysis)
    if analysis is None:
        return threshold

    window = args.analysis_window
    starts = list(analysis.starts[:3]) or [0.0]
    measured: dict[float, float] = {}
    # Brackets of the usable range: too_low cuts nothing, too_high eats speech.
    too_low: float | None = None
    too_high: float | None = None
    best = threshold
    best_distance: float | None = None

    for _ in range(SILENCE_CALIBRATION_ROUNDS):
        threshold = round(clamp(threshold, *SILENCE_THRESHOLD_LIMITS), 1)
        if threshold in measured:
            break
        fraction = measure_silence_fraction(
            input_path,
            starts=starts,
            window=window,
            threshold_db=threshold,
            min_silence=args.min_silence,
            offset=args.start,
        )
        measured[threshold] = fraction
        report(
            f"  threshold {threshold:.1f}dB cuts {fraction * 100:.1f}% of sampled audio",
        )
        distance = abs(fraction - SILENCE_FRACTION_TARGET)
        if best_distance is None or distance < best_distance:
            best, best_distance = threshold, distance
        if SILENCE_FRACTION_MIN <= fraction <= SILENCE_FRACTION_MAX:
            return threshold

        if fraction > SILENCE_FRACTION_MAX:
            too_high = threshold if too_high is None else max(too_high, threshold)
        else:
            too_low = threshold if too_low is None else min(too_low, threshold)

        if too_low is not None and too_high is not None:
            # Both ends known: halve the remaining interval instead of stepping
            # past the answer, which a fixed step does when the band is narrow.
            if abs(too_high - too_low) <= 0.2:
                break
            threshold = (too_low + too_high) / 2.0
        elif too_high is not None:
            threshold = too_high - SILENCE_CALIBRATION_STEP_DB
        else:
            threshold = too_low + SILENCE_CALIBRATION_STEP_DB
    return best


def denoise_filters(
    args: argparse.Namespace,
    *,
    noise_floor_db: float,
    snr_db: float,
) -> list[str]:
    mode = args.denoise
    if mode == "auto":
        mode = "arnndn" if args.arnndn_model else "afftdn"
    if mode == "none":
        return []
    if mode == "arnndn":
        if not args.arnndn_model:
            raise PipelineError("--denoise arnndn requires --arnndn-model")
        model = Path(args.arnndn_model).expanduser()
        if not model.exists():
            raise PipelineError(f"arnndn model not found: {model}")
        return [f"arnndn=m={filter_escape(str(model))}"]
    if mode == "anlmdn":
        return [f"anlmdn=s={args.anlmdn_strength:g}:p=0.002:r=0.006"]
    # afftdn: aim the noise profile slightly above the measured floor, and reduce
    # harder when the recording has little headroom between speech and noise.
    # Measured on a phone-recorded lecture (SNR 20 dB): nr=25:nf=-51 left 3 dB
    # less residual noise than nr=20:nf=-54, while staying clear of nr=30 where
    # afftdn starts adding watery artifacts of its own.
    noise_profile = clamp(round(noise_floor_db + 6.0), -80.0, -20.0)
    reduction = clamp(round(45.0 - snr_db), 10.0, 28.0)
    return [f"afftdn=nr={reduction:g}:nf={noise_profile:g}:tn=1"]


def gate_filter(*, noise_floor_db: float) -> str:
    threshold = clamp(db_to_linear(noise_floor_db + 8.0), 0.0005, 0.05)
    return f"agate=threshold={threshold:.5f}:range=0.06:ratio=2:attack=20:release=300"


def loudness_filters(
    args: argparse.Namespace,
    *,
    speech_lufs: float,
    noise_floor_db: float,
) -> list[str]:
    mode = args.loudness
    if mode == "none":
        return []
    if mode == "loudnorm":
        return [
            "loudnorm="
            f"I={args.loudnorm_i:g}:"
            f"TP={args.loudnorm_tp:g}:"
            f"LRA={args.loudnorm_lra:g}"
        ]
    deficit = max(0.0, args.target_lufs - speech_lufs)
    peak = clamp(db_to_linear(args.true_peak - 0.5), 0.5, 0.99)
    if mode == "speechnorm":
        expansion = clamp(db_to_linear(deficit), 2.0, 50.0)
        threshold = clamp(db_to_linear(noise_floor_db + 8.0), 0.0005, 0.05)
        return [
            f"speechnorm=p={peak:.3f}:e={expansion:.2f}:t={threshold:.5f}:r=0.0004:f=0.0002"
        ]
    # dynaudnorm gain is a ceiling, not a target, so allow more than the deficit.
    max_gain = clamp(db_to_linear(deficit + 6.0), 4.0, 60.0)
    return [f"dynaudnorm=f=400:g=15:p={peak:.3f}:m={max_gain:.1f}:s=12"]


def compressor_filter(*, makeup_db: float, true_peak_db: float) -> str:
    """Trade a little crest for loudness, gently enough to stay inaudible.

    Peak normalization alone leaves speech with a ~23 dB crest, which lands well
    below a -17 LUFS target. The missing loudness has to come from crest
    reduction; doing it here with a slow compressor is what keeps the limiter
    idle, instead of letting it clamp transients the way one-pass loudnorm does.
    """

    threshold = clamp(db_to_linear(true_peak_db - 12.5), 0.001, 1.0)
    makeup = clamp(db_to_linear(makeup_db), 1.0, 64.0)
    return (
        f"acompressor=threshold={threshold:.5f}:ratio=2.5:attack=20:release=250:"
        f"makeup={makeup:.3f}"
    )


@dataclass(frozen=True)
class GainPlan:
    """How the measured loudness gap is closed: compression first, trim second."""

    makeup_db: float = 0.0
    bias_db: float = 0.0
    measured_lufs: float | None = None
    shortfall_db: float = 0.0


def audio_chain_for(
    args: argparse.Namespace,
    *,
    analysis: AudioAnalysis | None,
    gain_plan: GainPlan | None = None,
    include_speed: bool = True,
) -> list[str]:
    """Clean first, lift second: gain applied after denoise cannot amplify hiss."""

    noise_floor_db = analysis.noise_floor_db if analysis else FALLBACK_NOISE_FLOOR_DB
    speech_lufs = analysis.speech_lufs_median if analysis else FALLBACK_SPEECH_LUFS

    filters: list[str] = []
    if args.mono:
        filters.append("aformat=channel_layouts=mono")
    if args.highpass > 0:
        filters.append(f"highpass=f={args.highpass:g}")
    if args.declick:
        filters.append("adeclick")
    filters.extend(
        denoise_filters(
            args,
            noise_floor_db=noise_floor_db,
            snr_db=speech_lufs - noise_floor_db,
        )
    )
    if not args.no_gate:
        filters.append(gate_filter(noise_floor_db=noise_floor_db))
    filters.extend(
        loudness_filters(args, speech_lufs=speech_lufs, noise_floor_db=noise_floor_db)
    )
    plan = gain_plan or GainPlan()
    if plan.makeup_db >= 0.5:
        filters.append(
            compressor_filter(makeup_db=plan.makeup_db, true_peak_db=args.true_peak)
        )
    if abs(plan.bias_db) >= 0.1:
        filters.append(f"volume={plan.bias_db:.2f}dB")
    if include_speed:
        filters.extend(atempo_filters(args.speed))
    # atempo overshoots on transients, so the ceiling is enforced after it.
    ceiling = db_to_linear(args.true_peak - TRUE_PEAK_MARGIN_DB)
    filters.append(f"alimiter=limit={ceiling:.4f}:level=disabled")
    filters.append(f"aresample={args.audio_sample_rate}")
    return filters or ["anull"]


def measure_processed_loudness(
    input_path: Path,
    *,
    args: argparse.Namespace,
    analysis: AudioAnalysis,
    gain_plan: GainPlan,
) -> float | None:
    chain = audio_chain_for(
        args, analysis=analysis, gain_plan=gain_plan, include_speed=False
    )
    window = min(args.analysis_window, GAIN_CALIBRATION_WINDOW)
    measured: list[float] = []
    for start in analysis.starts[:GAIN_CALIBRATION_WINDOWS]:
        log = run_audio_probe(
            input_path,
            start=args.start + start,
            duration=window,
            audio_filter=",".join([*chain, "ebur128=peak=true"]),
        )
        value = first_match(EBUR128_I_RE, log)
        if value is not None:
            measured.append(value)
    return combine_loudness(measured) if measured else None


def calibrate_gain(
    input_path: Path,
    *,
    analysis: AudioAnalysis | None,
    args: argparse.Namespace,
) -> GainPlan:
    """Close the loudness gap by measuring the real chain, not by predicting it.

    dynaudnorm and speechnorm normalize toward a peak, so where they land in LUFS
    depends on this recording's crest factor. Round one measures that landing
    point, round two verifies the compression chosen to close the gap.
    """

    if analysis is None or args.loudness == "none":
        return GainPlan()

    report("Calibrating output gain...")
    measured = measure_processed_loudness(
        input_path, args=args, analysis=analysis, gain_plan=GainPlan()
    )
    if measured is None:
        return GainPlan()

    deficit = args.target_lufs - measured
    report(f"  peak-normalized loudness: {measured:.1f} LUFS")
    if deficit <= GAIN_BIAS_LIMITS[1]:
        bias = clamp(deficit, *GAIN_BIAS_LIMITS)
        return GainPlan(bias_db=bias, measured_lufs=measured)

    makeup = clamp(deficit, 0.0, GAIN_MAKEUP_MAX_DB)
    plan = GainPlan(makeup_db=makeup, measured_lufs=measured)
    verified = measure_processed_loudness(
        input_path, args=args, analysis=analysis, gain_plan=plan
    )
    if verified is None:
        report(f"  with {makeup:.1f} dB of crest reduction: not measurable")
        return plan
    bias = clamp(args.target_lufs - verified, *GAIN_BIAS_LIMITS)
    shortfall = max(0.0, args.target_lufs - verified - bias)
    report(
        f"  with {makeup:.1f} dB of crest reduction: {verified:.1f} LUFS, "
        f"trim {bias:+.1f} dB"
    )
    if shortfall > 1.0:
        report(
            f"  {shortfall:.1f} dB short of {args.target_lufs:g} LUFS; raise "
            "--target-lufs or accept the quieter result rather than clipping",
        )
    return GainPlan(
        makeup_db=makeup,
        bias_db=bias,
        measured_lufs=verified,
        shortfall_db=shortfall,
    )


def resolve_audio_settings(
    *,
    input_path: Path,
    args: argparse.Namespace,
    analysis: AudioAnalysis | None,
) -> None:
    """Freeze the derived silence threshold and audio chain onto args."""

    start_phase(PHASE_CALIBRATE)
    if str(args.silence_threshold).strip().lower() == "auto":
        threshold = calibrate_silence_threshold(
            input_path, analysis=analysis, args=args
        )
        args.silence_threshold = f"{threshold:.1f}dB"
        report(f"Silence threshold: {args.silence_threshold} (auto)")
    if args.gain_bias is None:
        plan = calibrate_gain(input_path, analysis=analysis, args=args)
    else:
        plan = GainPlan(bias_db=args.gain_bias)
    args.audio_chain = audio_chain_for(args, analysis=analysis, gain_plan=plan)
    report(f"Audio chain: {','.join(args.audio_chain)}")


def filter_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_audio_filters(args: argparse.Namespace) -> list[str]:
    chain = getattr(args, "audio_chain", None)
    if chain:
        return list(chain)
    return audio_chain_for(args, analysis=None)


def build_filtergraph(
    segments: list[Segment],
    args: argparse.Namespace,
    *,
    video_fps: float = 30.0,
) -> str:
    if args.filtergraph_mode == "concat":
        return build_concat_filtergraph(segments, args)
    return build_select_filtergraph(segments, args, video_fps=video_fps)


def build_select_filtergraph(
    segments: list[Segment],
    args: argparse.Namespace,
    *,
    video_fps: float,
) -> str:
    select_expression = "+".join(
        f"between(t\\,{format_seconds(segment.start)}\\,{format_seconds(segment.end)})"
        for segment in segments
    )
    pts_expression = segment_pts_expression(segments)
    video_filters = [
        f"select='{select_expression}'",
        f"setpts='({pts_expression})/{args.speed:.8f}/TB'",
    ]
    audio_filters = [
        f"aselect='{select_expression}'",
        f"asetpts='({pts_expression})/TB'",
        *build_audio_filters(args),
    ]
    return (
        f"[0:v]{','.join(video_filters)}[vout];\n"
        f"[0:a]{','.join(audio_filters)}[aout]\n"
    )


def segment_pts_expression(segments: list[Segment]) -> str:
    terms: list[str] = []
    output_offset = 0.0
    for segment in segments:
        start = format_seconds(segment.start)
        end = format_seconds(segment.end)
        offset = format_seconds(output_offset)
        terms.append(f"between(T\\,{start}\\,{end})*(T-{start}+{offset})")
        output_offset += segment.duration
    return "+".join(terms) or "0"


def build_concat_filtergraph(segments: list[Segment], args: argparse.Namespace) -> str:
    lines: list[str] = []
    concat_inputs: list[str] = []
    for index, segment in enumerate(segments):
        start = format_seconds(segment.start)
        end = format_seconds(segment.end)
        lines.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]"
        )
        lines.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")

    joined_inputs = "".join(concat_inputs)
    lines.append(f"{joined_inputs}concat=n={len(segments)}:v=1:a=1[vcat][acat]")
    lines.append(f"[vcat]setpts=PTS/{args.speed:.8f}[vout]")
    lines.append(f"[acat]{','.join(build_audio_filters(args))}[aout]")
    return ";\n".join(lines) + "\n"


def supported_encoders() -> set[str]:
    result = run_command(["ffmpeg", "-hide_banner", "-encoders"], capture=True)
    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            encoders.add(parts[1])
    return encoders


def encoder_candidates(args: argparse.Namespace) -> list[str]:
    if args.encoder != "auto":
        return [args.encoder]
    encoders = supported_encoders()
    if "h264_nvenc" in encoders:
        return ["h264_nvenc", "libx264"]
    return ["libx264"]


def video_encoder_args(encoder: str, args: argparse.Namespace) -> list[str]:
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            args.nvenc_preset,
            "-cq",
            str(args.cq),
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "hevc_nvenc":
        return [
            "-c:v",
            "hevc_nvenc",
            "-preset",
            args.nvenc_preset,
            "-cq",
            str(args.cq),
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "libx264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            args.x264_preset,
            "-crf",
            str(args.crf),
            "-pix_fmt",
            "yuv420p",
        ]
    raise ValueError(f"Unsupported encoder: {encoder}")


def render_command(
    *,
    input_path: Path,
    output_path: Path,
    filtergraph_path: Path,
    encoder: str,
    args: argparse.Namespace,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-nostdin",
        *ffmpeg_input_options(args.start, args.limit),
        "-i",
        str(input_path),
        "-filter_complex_script",
        str(filtergraph_path),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        *video_encoder_args(encoder, args),
        "-c:a",
        "aac",
        "-b:a",
        args.audio_bitrate,
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def render_with_fallback(
    *,
    input_path: Path,
    output_path: Path,
    filtergraph_path: Path,
    args: argparse.Namespace,
    expected_duration: float | None = None,
) -> tuple[str, float]:
    candidates = encoder_candidates(args)
    last_error: subprocess.CalledProcessError | None = None
    temp_output = output_path.with_name(f".{output_path.name}.tmp.mp4")
    if temp_output.exists():
        temp_output.unlink()

    for index, encoder in enumerate(candidates):
        command = render_command(
            input_path=input_path,
            output_path=temp_output,
            filtergraph_path=filtergraph_path,
            encoder=encoder,
            args=args,
        )
        if args.dry_run:
            report("\nRender command:")
            report(shell_join(command))
            return encoder, 0.0

        start_phase(PHASE_RENDER)
        report(f"Rendering with {encoder}...")
        start = time.monotonic()
        try:
            result = run_command(
                command, check=False, progress_total=expected_duration
            )
        except PipelineCancelled:
            # A cancelled render must not look like a failed encoder: the loop
            # below would otherwise start the whole job again on libx264.
            if temp_output.exists():
                temp_output.unlink()
            raise
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            os.replace(temp_output, output_path)
            return encoder, elapsed

        last_error = subprocess.CalledProcessError(result.returncode, command)
        if temp_output.exists():
            temp_output.unlink()
        if index + 1 < len(candidates):
            report(
                f"{encoder} failed; falling back to {candidates[index + 1]}...",
                error=True,
            )

    assert last_error is not None
    raise last_error


def format_seconds(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def shell_join(command: list[str]) -> str:
    return " ".join(sh_quote(part) for part in command)


def sh_quote(value: str) -> str:
    if not value:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def validate_preview_denoise_modes(modes: Iterable[str]) -> list[str]:
    allowed = set(AUDIO_DENOISE_MODES)
    parsed = list(modes)
    invalid = sorted(set(parsed) - allowed)
    if invalid:
        raise PipelineError(f"Unsupported --preview-denoise mode(s): {', '.join(invalid)}")
    return parsed


def safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "value"


def describe_plan(
    *,
    media: MediaInfo,
    silences: list[Silence],
    segments: list[Segment],
    speed: float,
) -> str:
    kept = sum(segment.duration for segment in segments)
    final_estimate = kept / speed
    cut = max(0.0, media.duration - kept)
    cut_percent = cut / media.duration * 100 if media.duration else 0.0
    return (
        f"Input duration: {media.duration:.2f}s\n"
        f"Detected silences: {len(silences)}\n"
        f"Kept segments: {len(segments)}\n"
        f"Kept before speedup: {kept:.2f}s\n"
        f"Removed silence: {cut:.2f}s ({cut_percent:.1f}%)\n"
        f"Estimated output duration: {final_estimate:.2f}s at {speed:g}x"
    )


def print_dry_run_filtergraph(path: Path, filtergraph: str) -> None:
    report(f"\nFiltergraph script: {path}")
    report(filtergraph)


def plan_segments(
    *,
    input_path: Path,
    media: MediaInfo,
    args: argparse.Namespace,
) -> tuple[list[Silence], list[Segment]]:
    if args.no_cut_silence:
        return [], [Segment(0.0, media.duration)]

    start_phase(PHASE_SILENCE)
    report("Detecting silence...")
    silences = detect_silences(
        input_path,
        threshold=args.silence_threshold,
        min_silence=args.min_silence,
        duration=media.duration,
        start=args.start,
        limit=args.limit,
    )
    segments = silences_to_segments(
        silences,
        duration=media.duration,
        padding=args.padding,
        min_cut=args.min_cut,
        min_segment=args.min_segment,
    )
    return silences, segments


def render_segments(
    *,
    input_path: Path,
    output_path: Path,
    workdir: Path,
    media: MediaInfo,
    silences: list[Silence],
    segments: list[Segment],
    args: argparse.Namespace,
    filtergraph_name: str = "filtergraph.ffmpeg",
) -> tuple[str, float]:
    filtergraph = build_filtergraph(segments, args, video_fps=media.video_fps)
    filtergraph_path = workdir / filtergraph_name
    filtergraph_path.write_text(filtergraph, encoding="utf-8")

    report(describe_plan(media=media, silences=silences, segments=segments, speed=args.speed))
    if args.dry_run:
        print_dry_run_filtergraph(filtergraph_path, filtergraph)

    kept = sum(segment.duration for segment in segments)
    return render_with_fallback(
        input_path=input_path,
        output_path=output_path,
        filtergraph_path=filtergraph_path,
        args=args,
        expected_duration=kept / args.speed if args.speed > 0 else None,
    )


def run_preview_sweep(
    *,
    input_path: Path,
    workdir: Path,
    args: argparse.Namespace,
) -> int:
    preview_dir = args.preview_dir.expanduser()
    preview_dir.mkdir(parents=True, exist_ok=True)

    thresholds = csv_values(args.preview_thresholds)
    denoise_modes = validate_preview_denoise_modes(csv_values(args.preview_denoise))
    if not thresholds:
        raise PipelineError("--preview-thresholds produced no values")
    if not denoise_modes:
        raise PipelineError("--preview-denoise produced no values")

    preview_args = copy.copy(args)
    preview_args.start = args.preview_start if args.preview_start is not None else args.start
    preview_args.limit = args.preview_duration
    preview_args.force = True
    preview_args.dry_run = False

    media = probe_media(input_path, start=preview_args.start, limit=preview_args.limit)
    if media.duration <= 0:
        raise PipelineError("Preview sample is outside the input duration")
    if not media.has_video:
        raise PipelineError("Input has no video stream")
    if not media.has_audio:
        raise PipelineError("Input has no audio stream")

    manifest: list[dict[str, object]] = []
    total = len(thresholds) * len(denoise_modes)
    current = 0
    for threshold in thresholds:
        threshold_args = copy.copy(preview_args)
        threshold_args.silence_threshold = threshold
        silences, segments = plan_segments(
            input_path=input_path,
            media=media,
            args=threshold_args,
        )
        for denoise in denoise_modes:
            current += 1
            combo_args = copy.copy(threshold_args)
            combo_args.denoise = denoise
            combo_args.audio_chain = None
            output_name = (
                f"sample_{current:02d}_thr-{safe_label(threshold)}_"
                f"denoise-{safe_label(denoise)}.mp4"
            )
            output_path = preview_dir / output_name
            report(
                f"\nPreview {current}/{total}: threshold={threshold}, denoise={denoise}",
            )
            encoder, elapsed = render_segments(
                input_path=input_path,
                output_path=output_path,
                workdir=workdir,
                media=media,
                silences=silences,
                segments=segments,
                args=combo_args,
                filtergraph_name=f"filtergraph_{current:02d}.ffmpeg",
            )
            output_media = probe_media(output_path)
            manifest.append(
                {
                    "file": output_name,
                    "threshold": threshold,
                    "denoise": denoise,
                    "start": preview_args.start,
                    "input_duration": media.duration,
                    "output_duration": output_media.duration,
                    "detected_silences": len(silences),
                    "kept_segments": len(segments),
                    "encoder": encoder,
                    "render_seconds": round(elapsed, 3),
                }
            )

    manifest_path = preview_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report(f"\nPreview sweep complete: {preview_dir}")
    report(f"Manifest: {manifest_path}")
    return 0


def run_pipeline(args: argparse.Namespace) -> int:
    require_command("ffmpeg")
    workdir_manager: tempfile.TemporaryDirectory[str] | None = None
    if args.workdir is None:
        if args.keep_workdir:
            workdir = Path(tempfile.mkdtemp(prefix="lecturecut-"))
            report(f"Temporary workdir: {workdir}")
        else:
            workdir_manager = tempfile.TemporaryDirectory(prefix="lecturecut-")
            workdir = Path(workdir_manager.name)
    else:
        workdir = args.workdir
        workdir.mkdir(parents=True, exist_ok=True)

    try:
        input_path = prepare_input(args.source, workdir, args.download_format)
        if args.preview_dir is not None:
            return run_preview_sweep(input_path=input_path, workdir=workdir, args=args)

        output_path = args.output or default_output_path(args.source)
        output_path = output_path.expanduser()
        if output_path.exists() and not args.force and not args.dry_run:
            raise PipelineError(f"Output already exists, pass --force to overwrite: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        media = probe_media(input_path, start=args.start, limit=args.limit)
        if media.duration <= 0:
            raise PipelineError("Selected input range is outside the input duration")
        if not media.has_video:
            raise PipelineError("Input has no video stream")
        if not media.has_audio:
            raise PipelineError("Input has no audio stream")

        ACTIVE_PROGRESS.set(plan_progress_weights(duration=media.duration, args=args))

        analysis = None
        if not args.no_analyze:
            analysis = analyze_audio(input_path=input_path, media=media, args=args)
            if analysis is not None:
                report(format_audio_analysis(analysis))
                hints = audio_analysis_hints(analysis)
                for hint in hints:
                    report(f"  hint: {hint}")
                report_event(
                    "analysis",
                    speech_lufs=analysis.speech_lufs,
                    speech_lufs_median=analysis.speech_lufs_median,
                    noise_floor_db=analysis.noise_floor_db,
                    snr_db=analysis.snr_db,
                    true_peak_db=analysis.true_peak_db,
                    headroom_db=analysis.headroom_db,
                    lra=analysis.lra,
                    channel_imbalance_db=analysis.channel_imbalance_db,
                    hints=hints,
                )
        resolve_audio_settings(input_path=input_path, args=args, analysis=analysis)

        silences, segments = plan_segments(
            input_path=input_path,
            media=media,
            args=args,
        )
        encoder, elapsed = render_segments(
            input_path=input_path,
            output_path=output_path,
            workdir=workdir,
            media=media,
            silences=silences,
            segments=segments,
            args=args,
        )
        if args.dry_run:
            return 0

        output_media = probe_media(output_path)
        realtime = media.duration / elapsed if elapsed > 0 else 0.0
        report(f"Output: {output_path}")
        report(f"Encoder: {encoder}")
        report(f"Output duration: {output_media.duration:.2f}s")
        report(f"Render time: {elapsed:.2f}s ({realtime:.2f}x input realtime)")
        report_event(
            "result",
            output=str(output_path),
            encoder=encoder,
            input_duration=media.duration,
            output_duration=output_media.duration,
            render_seconds=elapsed,
            realtime=realtime,
            silence_threshold=str(args.silence_threshold),
            audio_chain=list(args.audio_chain),
        )
        return 0
    finally:
        if workdir_manager is not None and not args.keep_workdir:
            workdir_manager.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cancel = threading.Event()
    ACTIVE_CANCEL.set(cancel)

    def request_stop(signum: int, frame: object) -> None:
        cancel.set()

    try:
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
    except ValueError:
        # Not on the main thread; the caller owns signal handling.
        pass

    try:
        return run_pipeline(args)
    except PipelineCancelled:
        print("Cancelled", file=sys.stderr, flush=True)
        return 130
    except PipelineError as error:
        print(error, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
