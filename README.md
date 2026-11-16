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

- FFmpeg with `ffprobe`, 6.0 or newer; `zscale` is needed for automatic HDR-to-SDR conversion
- Python 3.10 or newer
- optional: `yt-dlp` for URL inputs; Apple VideoToolbox on macOS, or an NVIDIA card and driver for CUDA/NVDEC decoding and NVENC encoding

```bash
sudo apt install ffmpeg
pipx install yt-dlp                # only for URL inputs
ffmpeg -hide_banner -encoders | grep nvenc    # is hardware encoding available
```

The command line tool needs no Python packages. The web UI does:

```bash
pip install -e '.[web]'
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
| `--encoder auto` | NVIDIA NVENC → CPU (`libx264`); Apple encoding is manual: `h264_videotoolbox` or `hevc_videotoolbox` |
| `--decoder auto` | CUDA/NVDEC → Apple VideoToolbox → CPU, skipping backends absent from FFmpeg; explicit `videotoolbox`, `cuda`, `cpu` |
| `--cq 28`, `--crf 23` | quality; higher means a smaller file |
| `--videotoolbox-quality 65` | Apple quality, 0–100; higher means better quality and a larger file |
| `--max-fps 30` | cap the frame rate; halves a 60 fps screen recording |
| `--tonemap auto` | convert HDR phone recordings to ordinary SDR |
| `--board clean` | whiten the board and darken the marker; `ink` for a faint one |
| `--mono` | downmix, for a recorder with one useful channel |
| `--no-cut-silence` | keep the whole timeline, only clean and speed up |
| `--live-dir DIR` | render through a playlist you can watch as it grows |

`python3 main.py --help` lists the rest.

Hardware decoding is preferred by default in both the CLI and web UI. It is separate
from encoding: `--encoder libx264` can still decode on Apple or NVIDIA hardware.
Use `--decoder cpu` to compare with software decoding, or select `videotoolbox`
or `cuda` explicitly to disable decoder fallback.
The automatic mode tries each available decoder in priority order before changing
encoders; the log and result report which decoder completed the render. The
audio-only analysis and silence passes do not decode video.

For HDR conversion or board processing, auto skips CUDA to preserve the existing
CPU filtering policy; VideoToolbox remains available before CPU. Explicit
`--decoder cuda` can still force the hybrid CUDA/software path.

On macOS without CUDA, `auto` selects VideoToolbox for decoding and libx264 for
encoding. Apple H.264/HEVC encoding is available explicitly. This uses Apple's video hardware through
VideoToolbox, rather than Metal compute shaders. Cuts, rotation, and frame-rate
filters still use CPU-accessible frames. Constant-quality encoding is intended
for Apple Silicon; `--videotoolbox-quality` is independent of NVENC CQ/x264 CRF.
The web quality selector sets the appropriate value for each encoder.

```bash
python3 main.py lecture.mov --decoder videotoolbox --encoder h264_videotoolbox
python3 main.py lecture.mov --encoder hevc_videotoolbox --videotoolbox-quality 75
```

H.264 supports live preview; HEVC disables it in the web UI, as with HEVC NVENC.
HEVC files use the `hvc1` tag for Apple playback. Explicit encoders report errors
instead of switching to another encoder. Auto encoding tries NVENC and then
libx264; it never selects an Apple encoder. FFmpeg builds without VideoToolbox can still use CPU.

With NVENC and the default `select` mode, decoded frames stay on the GPU through
the cuts and FPS limiting. SDR without board processing or rotation stays there
through encoding. HDR conversion, board processing and rotation download only
the kept frames; HDR retains its source bit depth until conversion to SDR.
Unsupported CUDA paths fall back to CPU decoding in `auto` mode. The CPU encoder,
`concat` mode, and unusual display matrices use downloaded frames with the
existing software filters.

[GPU decoding details and measurements](docs/gpu-decoding.md) cover the CUDA
path and a CPU/GPU comparison on a HEVC Main 10 lecture.

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
machine from driving it. It hands out media and its own results, nothing else,
and an input folder has to be in your home folder, on a mounted drive or under a
`--root`. There is no login: it runs as you and can read the folders you point
it at, so it refuses a `--host` other than loopback unless you add
`--allow-remote`, which hands that to everyone on the network.
`--no-browse --no-open --no-upload` narrows it to the `--root` folders if you
ever need to.

## New lectures from Yandex Disk

`lecturecut-watch` follows shared Yandex Disk folders and processes what
appears in them:

```bash
lecturecut-watch add https://disk.yandex.ru/d/... --to ~/Lectures/Course -- --max-fps=30
lecturecut-watch run --dry-run     # what is new
lecturecut-watch install-timer     # run every hour, as a systemd user timer
lecturecut-watch list
```

Files already in the share when it is added count as known (`fetch NAME` or
`fetch --missing` queues them anyway); later ones are downloaded into
`~/.cache/lecturecut/incoming` (resuming a dropped download, checked against the
disk's sha256), processed with the options after `--` into the folder, and the
raw file is deleted - so a slow destination drive only ever receives results. A folder on a drive that is not mounted is skipped rather than created
on the system disk, and one that moved to another mount point is followed.
Standard library only, like the command line tool.

## Tests

```bash
python3 -m unittest discover -v
```

The web tests skip themselves unless the `web` extra is installed, so check that
the summary does not end in `skipped=`. One slower test (about 25 s) renders a
synthetic lecture and checks that sound stays with the picture across 300 cuts;
it runs only with `LECTURECUT_SLOW_TESTS=1`.

On macOS with hardware access, run
`LECTURECUT_APPLE_TESTS=1 python3 -m unittest tests.test_main.AppleVideoIntegrationTests -v`
to exercise real VideoToolbox H.264/HEVC encoding and decoding, live remux,
cuts, rotation, an FPS cap, HDR conversion, and board filters. These tests require
usable Apple video hardware and FFmpeg with `zscale`.

## Notes

- `data/` and `previews/` are gitignored: recordings and sample renders stay local.
- `static/vendor/` holds hls.js (Apache-2.0), vendored so the UI works offline.
- MIT, see [LICENSE](LICENSE). The vendored hls.js keeps its own Apache-2.0.
