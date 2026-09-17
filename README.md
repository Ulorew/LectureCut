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

Calibration aims for a typical share of removed pauses, which is a starting
point rather than an answer - so `--silence-bias` shifts it by ear. Positive
cuts more pauses, negative keeps more; a few dB either way is the usual range.

If the gap to `--target-lufs` is larger than a couple of decibels, a slow
compressor takes up the slack, because peak normalization cannot raise loudness
past the crest factor of the material. Loudness lands within about 1.5 dB of the
target, erring quiet; raise `--target-lufs` if you want more.

Denoiser options:

| mode | what it is |
| --- | --- |
| `auto` | `arnndn` when a model is given, otherwise `afftdn` |
| `afftdn` | spectral gate, fast; `nr` and `nf` are set from the measured floor |
| `anlmdn` | non-local means, slower, sometimes cleaner on broadband hiss |
| `arnndn` | recurrent network trained on speech, the best of these; needs a model |
| `none` | leave the noise alone |

`arnndn` needs a `.rnnn` model, which ships separately from FFmpeg. They are
~300 KB each, so they are fetched on demand:

```bash
python3 main.py data/lec1.mp4 -o out.mp4 --denoise arnndn          # fetches sh
python3 main.py data/lec1.mp4 -o out.mp4 --denoise arnndn --arnndn-model bd
python3 main.py data/lec1.mp4 -o out.mp4 --denoise arnndn --arnndn-model ~/models/own.rnnn
python3 main.py --download-models all                              # prefetch, then exit
```

Models land in `~/.cache/lecturecut/models` and their SHA-256 digests are pinned,
so a download that does not match is discarded rather than used. The web UI
lists what is on hand and offers a button for the rest.

The catalogue comes from
[rnnoise-models](https://github.com/GregorR/rnnoise-models), whose table maps
the signal a model expects against the noise it expects:

| key | expects | noise | |
| --- | --- | --- | --- |
| `lq` | voice | general | default, chosen by measurement — see below |
| `sh` | speech | recorded | |
| `bd` | voice, including laughter | recorded | |
| `cb` | any audio | recorded | |
| `mp` | any audio | general | |

The default is **not** the one the table nominates for a lecture. Measured
across two recordings, `sh` took 3-27 dB of speech with it, while `lq` took
1-8 dB and still gave the best or near-best improvement in signal-to-noise.
A denoiser is a classifier, and this one is confidently wrong often enough that
the table is not a safe guide.

**`arnndn` is not automatically better than `afftdn`.** It cleans harder, but it
decides what is speech, and when it decides wrong it removes the voice. On a
very quiet source it also thins out quiet passages, widening the loudness range
from ~7 LU to 20-25 LU. `afftdn` remains the default denoiser.

## When a denoiser goes wrong

Any denoiser can mistake speech for noise. Before the run, the pipeline measures
the speech level with and without the denoiser on the sampled windows; if the
denoiser costs more than `--denoise-loss-limit` dB (6 by default), it is removing
speech rather than noise and the run falls back to `afftdn` — or to no denoise if
`afftdn` was the culprit. `--denoise-loss-limit 0` turns the check off.

The noise gate is subject to the same reasoning. Sitting a fixed distance above
the noise floor is only safe when the speech is well clear of it: on a recording
with ~10 dB of signal-to-noise that rule put the gate *inside* the speech and
silenced whole passages. The gate now also has to sit 12 dB below the speech
level, and is dropped entirely when no such gap exists.

## Repeating a run

An output that already exists is not overwritten: the next free name is used
instead, so `lec1_lecturecut.mp4` is followed by `lec1_lecturecut_2.mp4`.
`--if-exists overwrite` (or `--force`) replaces it, `--if-exists error` refuses.

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
to the same machine would be pointless. The page lists media in one input
folder — `data/` to begin with — and a dropped file is matched against that list
by name and size.

«Сменить…» opens a folder browser, so the input folder can be anywhere on disk,
`~/Downloads` included. The choice and a short list of recent folders are kept in
`~/.config/lecturecut/webui.json` and restored on the next start. Listing stops
three folders deep and at 2000 files, so pointing it at a home directory does not
stall the page. `--no-browse` keeps the page to the roots given with `--root`.

Because the page can point the server at any folder, the server checks that
requests are really its own. The `Host` header must name the loopback address it
is bound to, which stops a hostile site from reaching it through DNS rebinding,
and every request that changes something must carry an `X-LectureCut` header,
which a cross-site page cannot add. Binding to `0.0.0.0` turns the `Host` check
off, since the server is then deliberately reachable under other names.

The form is generated from the CLI parser, so defaults, choices and help text
come from one place and cannot drift: settings are turned back into argv and
validated by the same `parse_args` the CLI uses.

Select as many files as you like - clicking a row toggles it - and they queue
up, drained one at a time because a single render already saturates the encoder.
For a batch the file names come from the core, so only the folder is chosen.
Each row in the queue carries its own progress and can be cancelled on its own;
clicking one shows its log. Finished runs offer «Открыть» and «Папка», which
hand the file to this desktop's own handler - a browser cannot follow a
`file://` link from an http page, and the server is on the same machine
anyway - plus a plain download link. Pass `--no-open` to turn that off.

The output folder is chosen from the same roots; leaving it on «рядом с
источником» keeps the CLI's default of writing next to the input. Settings are
remembered between visits, except `--start`, `--limit` and the output file
name, which belong to one particular file - silently reusing them would quietly
process 60 seconds of the next lecture. «Сбросить» restores the defaults.

Finished lectures are marked, and hidden from the source list by default, so
they are not offered back for a second pass.

## Recognising a finished render

Every output is named `<stem>_lecturecut.mp4` and carries a `lecturecut`
metadata tag naming the version that produced it:

```bash
ffprobe -v error -show_entries format_tags=lecturecut -of default=nw=1:nk=1 out.mp4
```

The tag is the reliable signal; the name only a hint, since anyone can rename a
file. The name is still checked as a fallback, which is what recognises renders
made before tagging existed. The CLI says so when handed an input it has
processed before.

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

The web tests skip themselves unless the `web` extra is installed, so run the
suite with the interpreter that has it and check that the summary does not end
in `skipped=`.

## Repository Notes

`data/` and `previews/` are intentionally ignored. Keep large source videos,
rendered outputs, and parameter sweep samples local.
