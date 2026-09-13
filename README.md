# LectureCut

Minimal Python + FFmpeg pipeline for lecture videos. Python measures the source
and plans the edits, FFmpeg does the media work: download, silence detection,
denoise, loudness normalization, speedup, and hardware-accelerated rendering.

Settings that depend on the recording are measured rather than guessed. Before
planning, the pipeline samples windows across the input, reads loudness, noise
floor and headroom, and derives the silence threshold, denoiser strength and
gain from those numbers.

## Requirements

```bash
sudo apt update
sudo apt install ffmpeg python3-venv pipx
pipx install yt-dlp
```

No Python packages are required for the CLI. The web UI is an optional extra:

```bash
pip install -e .[web]
```

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

By default the preview uses `-45dB` and `auto`. To compare variants:

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

## Quiet recordings

Phone-recorded lectures are the hard case: speech well below -35 dBFS, a noise
floor only ~15 dB under it, and a stray transient already near full scale. Gain
alone cannot fix that, so the audio chain cleans before it lifts:

```
highpass → denoise → gate → dynaudnorm → [compressor] → trim → atempo → alimiter
```

Two properties matter. Denoise runs *before* gain, so a 20 dB lift does not
bring the room tone with it. And the lift comes from `dynaudnorm`, which
normalizes per frame, instead of one-pass `loudnorm`, whose internal limiter
pumps and flattens dynamics when the deficit exceeds the headroom.

Measured on an 85-minute iPhone recording (-35.8 LUFS integrated, -0.7 dBFS true
peak, noise floor -57 dB):

| | old one-pass `loudnorm` | measured profile |
| --- | --- | --- |
| Integrated loudness | -18.2 LUFS | -18.4 LUFS |
| Loudness range | 5.1 LU (input was 8.6) | 9.2 LU |
| Noise floor | -42.6 dB | -87.6 dB |

Same loudness, dynamics intact, and the amplified hiss gone.

The same recording also shows why `--silence-threshold` is measured: at the old
-35dB default, 35% of a speech-dense window reads as silence, so cutting would
chew through syllables. `auto` derived -49.1dB for it.

If the gap to `--target-lufs` is larger than a couple of decibels, a slow
compressor takes up the slack, because peak normalization cannot raise loudness
past the crest factor of the material. Loudness lands within about 1.5 dB of the
target, erring quiet; raise `--target-lufs` if you want more.

Denoiser options, cheapest first: `afftdn` (default, adaptive `nr`/`nf`),
`anlmdn` (slower, broadband), and `arnndn` — speech-aware and the best of the
three, but it needs a model file:

```bash
python3 main.py data/lec1.mp4 -o out.mp4 --denoise arnndn --arnndn-model ~/models/sh.rnnn
```

Models ship separately from FFmpeg; point `--arnndn-model` at any `.rnnn` file.

Useful knobs:

```bash
--speed 1.25
--silence-threshold auto        # or an explicit -45dB
--min-silence 0.35
--padding 0.12
--denoise auto                  # auto|afftdn|anlmdn|arnndn|none
--loudness dynaudnorm           # dynaudnorm|speechnorm|loudnorm|none
--target-lufs -17
--true-peak -1.0
--highpass 85
--mono                          # when one channel of a phone mic is hotter
--declick                       # tame knocks that eat headroom
--no-gate
--gain-bias 0                   # override the calibrated trim
--audio-sample-rate 48000
--encoder auto
--nvenc-preset p1
```

Measurement knobs:

```bash
--analysis-samples 8
--analysis-window 20
--no-analyze                    # skip measurement, use static fallbacks
```

`--encoder auto` prefers `h264_nvenc` when FFmpeg exposes it and falls back to
`libx264` if the NVENC render fails.

The default `--filtergraph-mode select` is intended for speed on long lectures.
It preserves sync by mapping every kept segment back onto a shared output
timeline for audio and video. `--filtergraph-mode concat` keeps the older
per-segment trim/concat graph for debugging, but it can be much slower when
silence cutting creates many segments.

## Web UI

```bash
lecturecut-web                       # or: python3 webui.py
lecturecut-web --port 8800 --root ~/lectures --root ~/recordings
```

Then open http://127.0.0.1:8765. Pick a file, set the knobs, press
«Конвертировать»; progress is streamed per phase and the run can be cancelled.

Sources are chosen **on the server**: a browser hands over a dropped file's
contents but never its path, and copying a multi-gigabyte lecture through HTTP
to the same machine would be pointless. The page lists media under the
configured roots (by default the working directory and `data/`), and a dropped
file is matched against that index by name and size. Anything outside the roots
is refused, and can be uploaded instead if that is what you want.

The form is generated from the CLI parser, so defaults, choices and help text
come from one place and cannot drift: settings are turned back into argv and
validated by the same `parse_args` the CLI uses.

## Interrupting a run

`Ctrl+C` stops the current ffmpeg and cleans up the partial file instead of
leaving it behind; the exit code is 130. The web UI's cancel button does the
same thing.

Note for scripting: values that start with a dash but are not numbers need the
`=` form, or argparse reads them as an option - `--silence-threshold=-45dB`,
not `--silence-threshold -45dB`. Plain negative numbers such as
`--target-lufs -19` are fine either way.

## Tests

```bash
python3 -m unittest discover -v
python3 -m py_compile main.py webui.py tests/test_main.py
```

The web tests skip themselves unless the `web` extra is installed.

## Repository Notes

`data/` and `previews/` are intentionally ignored. Keep large source videos,
rendered outputs, and parameter sweep samples local.
