"""Audio stays with the picture across hundreds of joins.

Slow (about 25 s): set LECTURECUT_SLOW_TESTS=1 to run it. It renders a
synthetic lecture whose every phrase carries a white flash and a 3 kHz beep at
the same instant, then times the beeps the way a player hears them - decoded
samples back to back - against the flashes.

Timing the audio by its timestamps instead cannot see the failure this guards
against: those stamps were right all along, while the samples themselves ran
up to 300 ms ahead after 300 joins.
"""

import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parent.parent
SR, FPS, W, H = 48000, 30, 64, 48
BEEP_BAND = "bandpass=f=3000:width_type=h:w=150,bandpass=f=3000:width_type=h:w=150"


def tone(freq: int, seconds: float, amp: float) -> bytes:
    count = int(round(seconds * SR))
    period = SR // freq  # the frequencies used divide the rate: repeat one cycle
    cycle = b"".join(
        struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * i / period)))
        for i in range(period)
    )
    return (cycle * (count // period + 1))[: count * 2]


def make_lecture(path: Path, seconds: float, seed: int = 1) -> int:
    """Random-length phrases and pauses, as in speech; returns the marker count.

    Regular phrase lengths hide the drift: the join errors then repeat in a
    cycle and cancel out.
    """

    rng = random.Random(seed)
    audio, video = bytearray(bytes(int(0.5 * SR) * 2)), bytearray()
    black, white = bytes(W * H), b"\xff" * (W * H)
    t, frames_done, markers = 0.5, 0, 0
    while t < seconds - 4:
        burst, gap = rng.uniform(0.8, 2.0), rng.uniform(0.5, 1.3)
        mark = math.ceil((t + rng.uniform(0.2, burst - 0.35)) * FPS) / FPS
        before = mark - t
        audio += tone(300, before, 0.1) + tone(3000, 0.03, 0.5)
        audio += tone(300, burst - before - 0.03, 0.1) + bytes(int(round(gap * SR)) * 2)
        flash = int(round(mark * FPS))
        end = int(round((t + burst + gap) * FPS))
        for k in range(frames_done, end):
            video += white if flash <= k < flash + 2 else black
        frames_done, markers = end, markers + 1
        t += burst + gap
        del audio[int(round(t * SR)) * 2 :]
    with tempfile.TemporaryDirectory() as tmp:
        raw_audio, raw_video = Path(tmp) / "a.raw", Path(tmp) / "v.raw"
        raw_audio.write_bytes(audio)
        raw_video.write_bytes(video)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", str(raw_video),
             "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", str(raw_audio),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
            check=True,
        )
    return markers


def audio_minus_video(path: Path) -> list[float]:
    """Beep onset minus flash onset, per marker, in seconds."""

    def probe(entry: str, stream: str = "") -> float:
        args = ["ffprobe", "-v", "error", *(["-select_streams", stream] if stream else []),
                "-show_entries", entry, "-of", "csv=p=0", str(path)]
        return float(subprocess.run(args, capture_output=True, text=True).stdout.strip() or 0)

    end = probe("format=duration")
    video = subprocess.run(
        ["ffmpeg", "-i", str(path), "-map", "0:v", "-vf", "blackdetect=d=0.01:pix_th=0.5",
         "-f", "null", "-"], capture_output=True, text=True,
    ).stderr
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "-"], capture_output=True, check=True,
    ).stdout
    audio = subprocess.run(
        ["ffmpeg", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-af", f"{BEEP_BAND},asetnsamples=n=48,silencedetect=n=-30dB:d=0.1", "-f", "null", "-"],
        input=raw, capture_output=True,
    ).stderr.decode(errors="replace")
    start = probe("stream=start_time", "a:0")
    # Both detectors also report the end of the trailing black/silent stretch.
    flashes = [float(x) for x in re.findall(r"black_end:([\d.]+)", video) if float(x) < end - 0.5]
    beeps = [start + float(x) for x in re.findall(r"silence_end: ([\d.]+)", audio)]
    beeps = [b for b in beeps if b < end - 0.5]
    return [min(beeps, key=lambda b: abs(b - f)) - f for f in flashes]


@unittest.skipUnless(os.environ.get("LECTURECUT_SLOW_TESTS"), "slow: set LECTURECUT_SLOW_TESTS=1")
@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class SyncTests(unittest.TestCase):
    def test_audio_does_not_drift_across_300_joins(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "lecture.mp4", Path(tmp) / "out.mp4"
            markers = make_lecture(source, 720)
            subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(source), "-o", str(output),
                 "--denoise=none", "--encoder=libx264", "--decoder=cpu", "--speed=1.0"],
                check=True, capture_output=True,
            )
            offsets = audio_minus_video(output)

        self.assertGreater(len(offsets), markers * 0.95, "markers were lost, not a sync result")
        spread = max(offsets) - min(offsets)
        # Measured: about 0.1 s with the resync whatever the number of joins,
        # 0.3-0.4 s without it after 300 joins and still growing.
        self.assertLess(spread, 0.15, f"audio wandered {spread * 1000:.0f} ms against the picture")


class ResyncPlacementTests(unittest.TestCase):
    def test_audio_is_resynced_right_after_its_timestamps_are_set(self):
        args = main.parse_args(["in.mp4", "--filtergraph-mode=select"])
        graph = main.build_filtergraph([main.Segment(0.0, 1.0), main.Segment(2.0, 3.0)], args)
        audio_chain = graph.split("[0:a]", 1)[1]
        filters = re.split(r",(?=[a-z_]+=)", audio_chain)
        self.assertTrue(filters[1].startswith("asetpts="), filters[:3])
        self.assertEqual(filters[2], main.AUDIO_RESYNC_FILTER)


if __name__ == "__main__":
    unittest.main()
