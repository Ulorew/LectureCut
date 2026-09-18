# LectureCut

Fully vibecoded ultimate ADHD video utility.

Turns a raw lecture recording into something watchable: cuts the silence, cleans
and levels the audio, speeds it up a little, re-encodes it. Python measures the
recording and plans the edit, FFmpeg does the media work.

Settings that depend on the recording are measured rather than guessed. Before
planning anything it samples windows across the input, reads loudness, noise
floor, headroom and channel balance, and derives the silence threshold, the
denoiser settings and the gain from those numbers. A phone recording of a quiet
lecturer and a clean headset recording need different settings, and the point of
this is not having to find them by hand.

There is a command line tool and an optional local web UI with a queue, time
estimates, and playback of a render while it is still running.

[docs/details.md](docs/details.md) covers what is measured, why the defaults are
what they are, and what was tried and rejected.

## Requirements

- FFmpeg with `ffprobe`, 6.0 or newer
- Python 3.10 or newer
- optional: `yt-dlp` for URL inputs, an NVIDIA card for `h264_nvenc`

```bash
sudo apt install ffmpeg
pipx install yt-dlp                # only for URL inputs
ffmpeg -hide_banner -encoders | grep nvenc    # is hardware encoding available
```

The command line tool needs no Python packages. The web UI does:

```bash
pip install -e .[web]
```

## Command line

```bash
python3 main.py lecture.mp4                        # writes lecture_lecturecut.mp4
python3 main.py lecture.mp4 -o out.mp4 --speed 1.5
python3 main.py "https://youtu.be/..." -o out.mp4
python3 main.py lecture.mp4 --start 900 --limit 60 # try one minute of it first
python3 main.py lecture.mp4 --dry-run              # print the plan, render nothing
```

The options worth knowing:

| option | |
| --- | --- |
| `--speed 1.25` | playback speed, applied after the cuts |
| `--silence-threshold auto` | `auto`, or a value such as `-45dB` |
| `--silence-bias 0` | shift the automatic threshold by ear, in dB |
| `--denoise auto` | `auto`, `afftdn`, `anlmdn`, `arnndn`, `none` |
| `--target-lufs -17` | loudness to aim for |
| `--encoder auto` | `auto` (NVENC), `libx264`, `hevc_nvenc` |
| `--cq 28`, `--crf 23` | quality; higher means a smaller file |
| `--mono` | downmix, for a recorder with one useful channel |
| `--no-cut-silence` | keep the whole timeline, only clean and speed up |
| `--live-dir DIR` | render through a playlist you can watch as it grows |

`python3 main.py --help` lists the rest.

An output is never overwritten: an existing name gets `_2`, `_3` and so on.
`Ctrl+C` stops FFmpeg and removes the partial file.

## Web UI

```bash
lecturecut-web                     # http://127.0.0.1:8765
lecturecut-web --root ~/lectures   # folders it may read, repeatable
```

Pick an input folder, select one or more files, set the knobs, convert. The
queue runs one job at a time and says when each will be done; a running job can
be watched from the beginning while the rest of it renders.

The form is generated from the command line parser, so both entry points share
one set of defaults and one validator. The page is in English or Russian, chosen
with the switch in its corner and remembered; it starts in whichever the browser
asks for.

The server answers only to the loopback address it is bound to and requires its
own header on anything that changes state, which keeps other sites on the
machine from driving it.

## Tests

```bash
python3 -m unittest discover -v
```

The web tests skip themselves unless the `web` extra is installed, so check that
the summary does not end in `skipped=`.

## Notes

- `data/` and `previews/` are gitignored: recordings and sample renders stay local.
- `static/vendor/` holds hls.js (Apache-2.0), vendored so the UI works offline.
- MIT, see [LICENSE](LICENSE). The vendored hls.js keeps its own Apache-2.0.
