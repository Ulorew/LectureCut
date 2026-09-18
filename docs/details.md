# How it works, and why the defaults are what they are

Everything here was measured on real lectures rather than reasoned about, and
the numbers are kept because they are the argument for the defaults. Two
recordings come up repeatedly: a phone recording of a quiet lecturer with 15-20
dB of signal-to-noise, and a seminar with 3 dB, where most of the interesting
failures happened.

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

Calibration searches for the threshold at which silencedetect would cut 18% of
the sampled audio: it widens its steps until it has a threshold on each side of
that, then halves the interval, using every analysis window. That is a starting
point rather than an answer, so `--silence-bias` shifts it by ear. Positive cuts
more pauses, negative keeps more; a few dB either way is the usual range.

Silence is detected on the input, not on the denoised audio. It seems the better
order, but measured on a noisy seminar it is not: after afftdn or anlmdn the
share cut at each threshold barely moves, and after arnndn most of the quiet
speech already sits below any usable threshold, so a detector placed there
would remove exactly the speech arnndn had pushed down.

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

`--arnndn-mix` (the «Сила arnndn» slider) sets how much of arnndn's output is
used, the rest being the untouched input. arnndn tends to push quiet speech
down along with the noise: on a seminar with 3 dB of SNR, 72% of three minutes
ended up below -50 dB at full strength, 30% at 0.8 and 4% at 0.6. The filter
delays its input by exactly its own 10 ms frame before blending - measured
sample-exact - so a lower strength adds no comb filtering.

**`arnndn` is not automatically better than `afftdn`.** It cleans harder, but it
decides what is speech, and when it decides wrong it removes the voice. On a
very quiet source it also thins out quiet passages, widening the loudness range
from ~7 LU to 20-25 LU. `afftdn` remains the default denoiser.

## When a denoiser goes wrong

Any denoiser can mistake speech for noise. Before the run, the pipeline measures
the sampled windows with and without the denoiser - on exactly the signal the
render will feed it, downmixed first if `--mono` is set - and checks two things:

- **loudness**: costing more than `--denoise-loss-limit` dB (6 by default) means
  it is removing speech rather than noise;
- **spectral tilt**: the presence band (2-8 kHz) against the body band
  (100-1000 Hz) moving by more than 6 dB means it is reshaping the voice -
  muffled, as if under water, when negative, thin when positive.

Loudness alone missed the worst case: an afftdn that stripped the upper
frequencies off a seminar changed its loudness by 0.3 dB while tilting it by
-9.3 dB. A denoiser that fails steps down to `afftdn`, then to no denoise, and
each step is measured again. `--denoise-loss-limit 0` turns the check off. The
queue shows which denoiser each job actually ran, and says so when it differs
from the one requested.

afftdn subtracts whatever it believes is at or below its noise profile, so that
profile is kept below the quietest speech. On a clean recording it sits a few
dB above the noise floor, where that leaves less hiss; as the speech gets closer
to the noise the margin shrinks to nothing, because there the same margin puts
the profile over the speech itself.

The noise gate is subject to the same reasoning. Sitting a fixed distance above
the noise floor is only safe when the speech is well clear of it: on a recording
with ~10 dB of signal-to-noise that rule put the gate *inside* the speech and
silenced whole passages. The gate now also has to sit 12 dB below the speech
level, and is dropped entirely when no such gap exists.

## Size of the result

A lecture is a static frame of handwriting, and the old default spent four times
the bitrate it needed on one. Measured through the pipeline on a whiteboard
seminar at 1080p30, and compared by cropping the same frame at 1:1 - the
handwriting looks the same in all of them:

| setting | per hour | render, 180 s of input |
| --- | --- | --- |
| `--cq 23` (the old default) | 2.8 GB | 23 s |
| `--cq 28` (now the default) | 1.4 GB | 23 s |
| `--encoder libx264 --crf 26 --x264-preset medium` | 0.72 GB | 43 s |
| `--encoder hevc_nvenc --cq 30` | ~0.7 GB | 23 s |

So the default halves the file for nothing, and the CPU encoder halves it again
for twice the render time. HEVC does the same on the GPU, but browsers will not
play it, so a job encoding with it gets no live preview.

The page has «Сжатие видео» with five steps and an encoder choice, and shows the
size to expect per hour and for the selected file. That estimate comes from this
lecture; a busier frame will need more.

The default `--filtergraph-mode select` is intended for speed on long lectures.
It preserves sync by mapping every kept segment back onto a shared output
timeline for audio and video. `--filtergraph-mode concat` keeps the older
per-segment trim/concat graph for debugging, but it can be much slower when
silence cutting creates many segments.

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

## Watching a render in progress

A render outruns playback by several times over, so there is no reason to wait
for it. Press «Смотреть» on a running job and it plays from the beginning while
the rest is still being produced, seekable up to wherever the render has got to.
When the job finishes the player swaps to the finished file at the same
position. The button is shown, greyed out, from the moment a job is queued: on a
long lecture the audio analysis and the silence pass take several minutes before
there is any video to watch.

## How long the queue will take

Every pending job shows when it will be done, counting everything ahead of it,
and the queue shows the total and the clock time it will finish by.

The estimate is learned rather than guessed. A job passes through three stages
whose cost scales differently — analysis is a fixed number of short windows,
the silence pass reads the audio once, the render decodes the whole input — and
each finished job records how long each stage took on this machine, kept in
`~/.config/lecturecut/throughput.json`. Inside the silence pass and the render
the job's own pace is used instead, extrapolated from the fraction done.

Measured on three jobs queued at once: until the first one finished, the built-in
defaults were pessimistic, by up to two minutes on a two-minute lecture. From then
on the last job in the queue was predicted within 1.4 s while it was still
waiting, and every stage of the running jobs within 1.3 s.

This works because the render is muxed as an HLS event playlist of fMP4 segments
rather than straight to MP4, then remuxed into the usual faststart MP4 with a
stream copy. An ordinary MP4 cannot be played while it is being written - its
index is only written at the end - and a growing fragmented MP4 plays but cannot
be sought. Measured in Chrome, the playlist gives a seekable range from zero to
the render's edge.

It is free: on the same two-minute slice the direct MP4 took 43.3 and 42.3 s
against 42.8 and 40.0 s for the playlist, and remuxing a three-minute result
took 0.9 s. Segments live in `~/.cache/lecturecut/live` and are removed fifteen
minutes after the job ends, so a job needs its output size free there as well.
`--no-live-preview` renders straight to MP4 instead. Browsers cannot play HEVC,
so a job encoding with `hevc_nvenc` gets no preview.

From the command line the same playlist works with any player that reads HLS:

```bash
python3 main.py data/lec1.mp4 --live-dir /tmp/lec1-live &
mpv /tmp/lec1-live/index.m3u8
```

## Interrupting a run

`Ctrl+C` stops the current ffmpeg and cleans up the partial file instead of
leaving it behind; the exit code is 130. The web UI's cancel button does the
same thing.

Note for scripting: values that start with a dash but are not numbers need the
`=` form, or argparse reads them as an option - `--silence-threshold=-45dB`,
not `--silence-threshold -45dB`. Plain negative numbers such as
`--target-lufs -19` are fine either way.

## Comparing settings on short clips

`--preview-dir` renders one short clip per combination of silence threshold and
denoiser, plus a `manifest.json` of the settings and timings, which is the
quickest way to judge a new recording by ear:

```bash
python3 main.py lecture.mp4 --preview-dir previews \
  --preview-start 900 --preview-duration 10 \
  --preview-thresholds=-45dB,-40dB,-35dB --preview-denoise none,afftdn
```
