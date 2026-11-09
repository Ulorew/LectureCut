# NVIDIA video decoding

`--decoder auto` is the default in the CLI and web UI. For SDR without board
processing it tries CUDA/NVDEC first, then retries with software decoding if the
render fails. HDR conversion and board processing default to CPU decoding: the
transfer back to software filters did not give a consistent gain in the integration
measurements below. Explicit `--decoder cuda` enables the hybrid path for comparisons
on other hardware. Decoder fallback keeps
the same encoder; only after its decoder attempts fail does `--encoder auto`
try the next encoder. `--decoder cuda` disables software decoder fallback, and
`--decoder cpu` uses the previous software decoding path. Encoding is a separate
choice: `--encoder libx264` can still decode on NVIDIA.

With NVENC and `--filtergraph-mode select`, the CUDA graph keeps decoded frames
on the device through selection, timestamp changes and the optional FPS cap.
For SDR, `scale_cuda` converts the kept frames to 8-bit YUV420 for the existing
output format. Frames reach NVENC without a trip through system memory unless
rotation or board processing needs the software filters. In that case only
the kept frames are downloaded, after the FPS cap.

HDR conversion downloads the kept frames at the source depth (P010 for the
usual 10-bit phone recordings), then runs the measured `zscale` conversion,
rotation and optional board filters on the CPU. Reducing HDR to 8 bits before
that conversion would lose precision in faint writing. Unknown HDR pixel
layouts use FFmpeg's ordinary automatic download path instead of guessing a
hardware layout. `--tonemap off` explicitly disables HDR conversion.

The ordinary graph remains available for CPU retries. The libx264 encoder,
`concat` mode, and display matrices that need FFmpeg's own autorotate use CUDA
decoding with downloaded frames. These paths avoid requiring CUDA versions of
the software filters, but may gain less or run slower than CPU decoding.
FFmpeg needs CUDA/NVDEC support and a working NVIDIA driver. The fast path also
needs `scale_cuda` for its SDR path; `auto` falls back to CPU if it is missing. Audio analysis,
calibration and silence detection remain audio-only.

## Local benchmark, 2026-11-05

These are the PR author's measurements on the pre-HDR-conversion branch.
They do not include the later automatic HDR-to-SDR conversion or board filters.

Input: `Lecture_05_21_09_2026.MOV`, HEVC Main 10, 1920x1080, about 30 fps,
79 min 57 s. Machine: Intel i5-12400F, RTX 3080 10 GB, NVIDIA driver 595.99.02,
FFmpeg 8.0.1. The measurements below cover seconds 900–1080 of that recording,
not the full lecture. NVENC p4, CQ 28, speed 1.25 and all audio processing use
the normal defaults.

Four sequential pipeline runs used decoder order CPU, auto, auto, CPU. Both
auto runs completed with CUDA; the table gives means of the two runs per mode.
The decode-only comparison is a separate single run per mode and keeps CUDA
frames on the device, as the fast render path does.

| Work on 180 s of input | CPU decode | GPU decode | Speedup |
| --- | ---: | ---: | ---: |
| Decode only, no encoding | 9.94 s | 4.39 s | 2.26x |
| Render, including audio filters and encoding | 14.01 s | 9.42 s | 1.49x |
| Entire pipeline, including analysis and silence detection | 17.88 s | 13.35 s | 1.34x |

The entire pipeline took 25% less wall time. GPU decoding does not remove the
cost of the remaining audio processing, filters and encoding. Adding only
`-hwaccel cuda`, with every 10-bit frame downloaded immediately, was slower
than CPU decoding on this machine; retaining frames on the GPU is necessary
for this result.

Validation:

- Every rendered output decodes completely without errors. All four have
  3,771 video frames, video duration 125.8257 s and audio duration 125.783 s.
- CPU and CUDA decoded 10-bit frame checksums match over a 3 s excerpt starting
  at 917.123 s, after normalizing the pixel layout to planar YUV420P10.
- Every output video timestamp and the SHA-256 of the decoded audio match
  between the CPU and GPU renders of the 180 s excerpt.
- Encoded pictures differ slightly because 10-to-8-bit conversion moves to
  CUDA. A 1 s comparison at output second 30 measured average PSNR 49.09 dB
  between the CPU and GPU outputs; GPU output size was about 1% larger.
- A separate 12 s excerpt with a 90-degree display matrix passed CUDA renders
  with a 15 fps cap and live HLS/remux, libx264 encoding, HEVC NVENC encoding,
  and `concat` mode. Outputs have the expected portrait dimensions and tags.
- All 272 unit/integration tests passed, including the 300-join audio/video
  sync test (`LECTURECUT_SLOW_TESTS=1`).

To compare on another machine, keep settings and source range identical:

```bash
python3 main.py /path/to/Lecture_05_21_09_2026.MOV \
  --start 900 --limit 180 --tonemap off --encoder h264_nvenc --decoder cpu -o cpu.mp4
python3 main.py /path/to/Lecture_05_21_09_2026.MOV \
  --start 900 --limit 180 --tonemap off --encoder h264_nvenc --decoder cuda -o gpu.mp4
```

`--tonemap off` reproduces that branch's processing; omit it when testing the
normal HDR-to-SDR workflow instead. The CLI reports render time; measure the process wall time separately for the
whole pipeline. Repeat with reversed order to reduce cache/order effects.
Local test media and detailed JSON/logs are under
`previews/gpu-decode-benchmark/optimized/` and `previews/gpu-decode-benchmark/compat/`
(gitignored).

## Integration measurements, 2026-11-09

Rechecked PR #1 (`ace9ee3`) against the HDR/board changes on an i5-12600K,
RTX 3090, NVIDIA driver 580.196.07 and FFmpeg 6.1.1. Inference was paused and
the GPU reserved for these runs. The source was Lecture 9, HEVC Main 10 HLG,
1920x1080 at approximately 30 fps. Seconds 900–1080 were copied to the local
SSD without re-encoding. NVENC p4/CQ28, speed 1.25 and default audio processing
were unchanged between decoder choices.

Each row is the mean of two runs per decoder, in CPU/CUDA/CUDA/CPU order.
The three-minute runs followed a one-minute warm-up comparison. The initial
one-minute CUDA render took 7.21 s versus 3.77 s on its repeat: cold-start costs
matter on short jobs, so the longer results should not be read as startup latency.

| Processing | Input | CPU wall / render | CUDA wall / render | Change in wall time |
| --- | ---: | ---: | ---: | ---: |
| Original PR, without HDR conversion | 180 s | 19.04 / 15.08 s | 13.51 / 9.47 s | 29% less |
| Measured HDR-to-SDR conversion | 180 s | 30.09 / 24.98 s | 32.81 / 27.60 s | 9% more |
| HDR + `--board clean` | 60 s | 35.41 / 30.30 s | 33.03 / 27.84 s | 7% less, inconclusive |

The board CPU runs varied from 37.39 to 33.42 s while CUDA took 33.00 and
33.06 s; that spread is larger than the mean improvement. There is no reliable
board-mode gain to promise from this small sample. The original PR's benefit
is real for the path without software colour processing, but does not carry
over to HDR automatically. This is why `auto` uses CPU decoding for HDR
conversion and board modes, while `--decoder cuda` keeps the combined path
available. NVENC encoding remains independent of that choice.

Validation of the CPU/CUDA pairs:

- Video timestamps and decoded PCM SHA-256 matched in all five pairs (the
  three one-minute profiles and the two three-minute profiles).
- HDR and board outputs had identical decoded pictures (PSNR infinity),
  including the faint writing; HDR retained its 10-bit precision before zscale.
- The original PR's GPU-side 10-to-8-bit conversion produced small differences:
  average PSNR 48.56 dB over the three-minute output. Both paths produced 3,775
  frames; neither gained speed by dropping additional frames.
- Colour tags matched between decoder choices. HDR output was BT.709.
- A separate synthetic 10-bit HLG test verified CPU/CUDA timing, audio, SDR
  transfer/primary tags and dimensions with a 15 fps cap, 90-degree rotation
  and `--board ink`.

The hardware test is opt-in so routine tests do not contend with inference:

```bash
LECTURECUT_GPU_TESTS=1 python3 -m unittest tests.test_gpu -v
```

It needs a free NVIDIA GPU and FFmpeg with NVDEC, NVENC and libx265. Select a
device with `CUDA_VISIBLE_DEVICES` when needed. Ordinary integration tests use
`--decoder cpu` explicitly; the slow audio-sync test remains independently
enabled with `LECTURECUT_SLOW_TESTS=1`.
