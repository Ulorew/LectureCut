# NVIDIA video decoding

`--decoder auto` is the default in the CLI and web UI. It tries CUDA/NVDEC first,
then retries with software decoding if the render fails. Decoder fallback keeps
the same encoder; only after both decoder attempts fail does `--encoder auto`
try the next encoder. `--decoder cuda` disables software decoder fallback, and
`--decoder cpu` uses the previous software decoding path. Encoding is a separate
choice: `--encoder libx264` can still decode on NVIDIA.

With NVENC and `--filtergraph-mode select`, the CUDA graph keeps decoded frames
on the device through selection, timestamp changes and the optional FPS cap.
`scale_cuda` converts the kept frames to 8-bit YUV420 for the existing output
format. Frames reach NVENC without a trip through system memory. If a display
matrix requires rotation, only the kept frames are downloaded after conversion
and the FPS cap, then rotated by the existing software filters.

The ordinary graph remains available for CPU retries. The libx264 encoder,
`concat` mode, and display matrices that need FFmpeg's own autorotate use CUDA
decoding with downloaded frames. These paths avoid requiring CUDA versions of
the software filters, but may gain less or run slower than CPU decoding.
FFmpeg needs CUDA/NVDEC support and a working NVIDIA driver. The fast path also
needs `scale_cuda`; `auto` falls back to CPU if it is missing. Audio analysis,
calibration and silence detection remain audio-only.

## Local benchmark, 2026-11-05

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
  --start 900 --limit 180 --encoder h264_nvenc --decoder cpu -o cpu.mp4
python3 main.py /path/to/Lecture_05_21_09_2026.MOV \
  --start 900 --limit 180 --encoder h264_nvenc --decoder auto -o gpu.mp4
```

The CLI reports render time; measure the process wall time separately for the
whole pipeline. Repeat with reversed order to reduce cache/order effects.
Local test media and detailed JSON/logs are under
`previews/gpu-decode-benchmark/optimized/` and `previews/gpu-decode-benchmark/compat/`
(gitignored).
