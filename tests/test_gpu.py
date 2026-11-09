"""Opt-in GPU integration checks: LECTURECUT_GPU_TESTS=1 python -m unittest tests.test_gpu.

Needs NVDEC/NVENC and libx265 in FFmpeg. Normal test runs never reserve a GPU.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    result = subprocess.run(args, capture_output=True, timeout=120)
    if result.returncode:
        raise AssertionError((result.stdout + result.stderr).decode(errors="replace")[-6000:])
    return result


@unittest.skipUnless(os.environ.get("LECTURECUT_GPU_TESTS"), "opt-in: needs a free NVIDIA GPU")
class GpuHdrIntegrationTests(unittest.TestCase):
    def test_hdr_and_rotated_board_match_cpu_timing_audio_and_colour_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "hdr.mp4"
            command(
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=4",
                "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
                "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                "-x265-params", "pools=1:frame-threads=1:log-level=error",
                "-color_trc", "arib-std-b67", "-color_primaries", "bt2020",
                "-colorspace", "bt2020nc", "-c:a", "aac", "-shortest", str(source),
            )
            rotated = folder / "rotated.mp4"
            command("ffmpeg", "-v", "error", "-display_rotation", "90", "-i", str(source),
                    "-c", "copy", str(rotated))
            for mode, input_path, dims in (("off", source, (320, 240)),
                                           ("ink", rotated, (240, 320))):
                observed = []
                for decoder in ("cpu", "cuda"):
                    output = folder / f"{mode}-{decoder}.mp4"
                    result = command(
                        sys.executable, str(ROOT / "main.py"), str(input_path), "-o", str(output),
                        "--encoder=h264_nvenc", f"--decoder={decoder}", f"--board={mode}",
                        "--max-fps=15", "--analysis-samples=2",
                    )
                    self.assertIn(f"Decoder: {decoder}".encode(), result.stdout)
                    stream = json.loads(command(
                        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
                        "-of", "json", str(output),
                    ).stdout)["streams"][0]
                    self.assertEqual((stream["width"], stream["height"]), dims)
                    for tag in ("color_transfer", "color_primaries"):
                        self.assertEqual(stream[tag], "bt709")
                    if mode == "off":
                        self.assertEqual(stream["color_space"], "bt709")
                    frames = json.loads(command(
                        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
                        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(output),
                    ).stdout)["frames"]
                    audio = command("ffmpeg", "-v", "error", "-i", str(output),
                                    "-map", "0:a:0", "-f", "s16le", "-").stdout
                    observed.append(([f["best_effort_timestamp_time"] for f in frames],
                                     hashlib.sha256(audio).hexdigest()))
                self.assertEqual(observed[0], observed[1], mode)
