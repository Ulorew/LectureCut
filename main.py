#!/usr/bin/env python3
"""LectureCut: fast lecture cleanup with Python orchestration and FFmpeg."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<start>[0-9.]+)")
SILENCE_END_RE = re.compile(
    r"silence_end:\s*(?P<end>[0-9.]+)\s*\|\s*silence_duration:\s*(?P<duration>[0-9.]+)"
)


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        default="-35dB",
        help="FFmpeg silencedetect noise threshold, e.g. -35dB",
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
        choices=("afftdn", "none"),
        default="afftdn",
        help="Audio denoise filter",
    )
    audio_group.add_argument(
        "--no-normalize",
        action="store_true",
        help="Disable one-pass loudnorm",
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
        default="-35dB",
        help="Comma-separated silencedetect thresholds to compare",
    )
    preview_group.add_argument(
        "--preview-denoise",
        default="afftdn",
        help="Comma-separated denoise modes to compare: none,afftdn",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned commands and filtergraph without rendering",
    )
    return parser.parse_args(argv)


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
        raise SystemExit(f"Required command not found on PATH: {name}")


def run_command(
    command: list[str],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and process.returncode != 0:
        if capture and process.stdout:
            print(process.stdout, file=sys.stderr)
        if capture and process.stderr:
            print(process.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(process.returncode, command)
    return process


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
            raise SystemExit(f"Input file does not exist: {input_path}")
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
    print("Downloading input with yt-dlp...", flush=True)
    result = run_command(command, capture=True)
    candidates = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    for candidate in reversed(candidates):
        if candidate.exists():
            return candidate
    files = sorted(workdir.glob("source.*"), key=lambda path: path.stat().st_mtime)
    if files:
        return files[-1]
    raise SystemExit("yt-dlp finished, but no downloaded file was found")


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
        raise SystemExit(f"Could not read media duration for {path}") from exc
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
    result = run_command(command, capture=True)
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


def build_audio_filters(args: argparse.Namespace) -> list[str]:
    filters: list[str] = []
    if args.denoise == "afftdn":
        filters.append("afftdn")
    filters.extend(atempo_filters(args.speed))
    if not args.no_normalize:
        filters.append(
            "loudnorm="
            f"I={args.loudnorm_i:g}:"
            f"TP={args.loudnorm_tp:g}:"
            f"LRA={args.loudnorm_lra:g}"
        )
    filters.append(f"aresample={args.audio_sample_rate}")
    return filters or ["anull"]


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
            print("\nRender command:")
            print(shell_join(command))
            return encoder, 0.0

        print(f"Rendering with {encoder}...", flush=True)
        start = time.monotonic()
        result = run_command(command, check=False)
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            os.replace(temp_output, output_path)
            return encoder, elapsed

        last_error = subprocess.CalledProcessError(result.returncode, command)
        if temp_output.exists():
            temp_output.unlink()
        if index + 1 < len(candidates):
            print(
                f"{encoder} failed; falling back to {candidates[index + 1]}...",
                file=sys.stderr,
                flush=True,
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
    allowed = {"none", "afftdn"}
    parsed = list(modes)
    invalid = sorted(set(parsed) - allowed)
    if invalid:
        raise SystemExit(f"Unsupported --preview-denoise mode(s): {', '.join(invalid)}")
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
    print(f"\nFiltergraph script: {path}")
    print(filtergraph)


def plan_segments(
    *,
    input_path: Path,
    media: MediaInfo,
    args: argparse.Namespace,
) -> tuple[list[Silence], list[Segment]]:
    if args.no_cut_silence:
        return [], [Segment(0.0, media.duration)]

    print("Detecting silence...", flush=True)
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

    print(describe_plan(media=media, silences=silences, segments=segments, speed=args.speed))
    if args.dry_run:
        print_dry_run_filtergraph(filtergraph_path, filtergraph)

    return render_with_fallback(
        input_path=input_path,
        output_path=output_path,
        filtergraph_path=filtergraph_path,
        args=args,
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
        raise SystemExit("--preview-thresholds produced no values")
    if not denoise_modes:
        raise SystemExit("--preview-denoise produced no values")

    preview_args = copy.copy(args)
    preview_args.start = args.preview_start if args.preview_start is not None else args.start
    preview_args.limit = args.preview_duration
    preview_args.force = True
    preview_args.dry_run = False

    media = probe_media(input_path, start=preview_args.start, limit=preview_args.limit)
    if media.duration <= 0:
        raise SystemExit("Preview sample is outside the input duration")
    if not media.has_video:
        raise SystemExit("Input has no video stream")
    if not media.has_audio:
        raise SystemExit("Input has no audio stream")

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
            output_name = (
                f"sample_{current:02d}_thr-{safe_label(threshold)}_"
                f"denoise-{safe_label(denoise)}.mp4"
            )
            output_path = preview_dir / output_name
            print(
                f"\nPreview {current}/{total}: threshold={threshold}, denoise={denoise}",
                flush=True,
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
    print(f"\nPreview sweep complete: {preview_dir}")
    print(f"Manifest: {manifest_path}")
    return 0


def run_pipeline(args: argparse.Namespace) -> int:
    require_command("ffmpeg")
    workdir_manager: tempfile.TemporaryDirectory[str] | None = None
    if args.workdir is None:
        if args.keep_workdir:
            workdir = Path(tempfile.mkdtemp(prefix="lecturecut-"))
            print(f"Temporary workdir: {workdir}", flush=True)
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
            raise SystemExit(f"Output already exists, pass --force to overwrite: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        media = probe_media(input_path, start=args.start, limit=args.limit)
        if media.duration <= 0:
            raise SystemExit("Selected input range is outside the input duration")
        if not media.has_video:
            raise SystemExit("Input has no video stream")
        if not media.has_audio:
            raise SystemExit("Input has no audio stream")

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
        print(f"Output: {output_path}")
        print(f"Encoder: {encoder}")
        print(f"Output duration: {output_media.duration:.2f}s")
        print(f"Render time: {elapsed:.2f}s ({realtime:.2f}x input realtime)")
        return 0
    finally:
        if workdir_manager is not None and not args.keep_workdir:
            workdir_manager.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
