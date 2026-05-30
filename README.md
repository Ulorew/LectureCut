# LectureCut

Minimal Python + FFmpeg pipeline for lecture videos. Python plans the edits and
FFmpeg does the media work: download, silence detection, denoise, loudness
normalization, speedup, and hardware-accelerated rendering.

## Requirements

```bash
sudo apt update
sudo apt install ffmpeg python3-venv pipx
pipx install yt-dlp
```

No Python packages are required for normal use.

Check hardware encoding:

```bash
ffmpeg -hide_banner -encoders | grep -E 'h264_nvenc|hevc_nvenc'
```

## Usage

Run from the repository root:

Local file:

```bash
python3 main.py data/lec1.mp4 -o out.mp4 --force
```

YouTube or another `yt-dlp` supported URL:

```bash
python3 main.py "https://www.youtube.com/watch?v=..." -o lecture-cut.mp4
```

Fast preview on the first 60 seconds:

```bash
python3 main.py data/lec1.mp4 -o preview.mp4 --limit 60 --force
```

Sample a later timestamp:

```bash
python3 main.py data/lec1.mp4 -o preview.mp4 --start 900 --limit 60 --force
```

Dry-run the generated FFmpeg filtergraph:

```bash
python3 main.py data/lec1.mp4 --limit 30 --dry-run
```

Compare denoise and silence thresholds on short clips:

```bash
python3 main.py data/lec1.mp4 \
  --preview-dir previews \
  --preview-start 900 \
  --preview-duration 10
```

By default the preview uses `-35dB` and `afftdn`. To compare variants:

```bash
python3 main.py data/lec1.mp4 \
  --preview-dir previews \
  --preview-start 900 \
  --preview-duration 10 \
  --preview-thresholds=-45dB,-40dB,-35dB,-30dB \
  --preview-denoise none,afftdn \
  --encoder h264_nvenc \
  --nvenc-preset p1
```

This writes one MP4 per combination plus `manifest.json` with the settings and
basic timing stats.

Useful knobs:

```bash
--speed 1.25
--silence-threshold -35dB
--min-silence 0.35
--padding 0.12
--denoise afftdn
--no-normalize
--audio-sample-rate 48000
--encoder auto
--nvenc-preset p1
```

`--encoder auto` prefers `h264_nvenc` when FFmpeg exposes it and falls back to
`libx264` if the NVENC render fails.

The default `--filtergraph-mode select` is intended for speed on long lectures.
It preserves sync by mapping every kept segment back onto a shared output
timeline for audio and video. `--filtergraph-mode concat` keeps the older
per-segment trim/concat graph for debugging, but it can be much slower when
silence cutting creates many segments.

## Tests

```bash
python3 -m unittest discover -v
python3 -m py_compile main.py tests/test_main.py
```

## Repository Notes

`data/` and `previews/` are intentionally ignored. Keep large source videos,
rendered outputs, and parameter sweep samples local.
