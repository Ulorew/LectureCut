import argparse
import contextlib
import io
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import main


class CutExpressionCache:
    """Translate an ffmpeg cut expression to Python once, not once per probe."""

    _cache: dict[str, object] = {}

    @classmethod
    def compiled(cls, expression: str):
        if expression not in cls._cache:
            python = re.sub(r"\b(if|lt|between)\(", r"_\1(", expression.replace("\\,", ","))
            cls._cache[expression] = compile(python, "<cut expression>", "eval")
        return cls._cache[expression]


class LectureCutTests(unittest.TestCase):
    def test_parse_silencedetect_log_handles_trailing_silence(self):
        log = """
        [silencedetect @ 0x123] silence_start: 1.25
        [silencedetect @ 0x123] silence_end: 2 | silence_duration: 0.75
        [silencedetect @ 0x123] silence_start: 9.5
        """

        silences = main.parse_silencedetect_log(log, media_duration=10.0)

        self.assertEqual(silences, [main.Silence(1.25, 2.0), main.Silence(9.5, 10.0)])

    def test_the_silence_pass_never_decodes_video(self):
        """Decoding a 60 fps HEVC track here once cost more than the render."""

        seen = []

        def fake_run(command, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with unittest.mock.patch.object(main, "run_command", fake_run), \
                unittest.mock.patch.object(main, "require_command", lambda name: None):
            main.detect_silences(
                Path("lecture.mp4"),
                threshold="-45dB",
                min_silence=0.6,
                duration=60.0,
                start=0.0,
                limit=None,
            )

        command = seen[0]
        self.assertIn("-vn", command)
        mapped = command[command.index("-map") + 1]
        self.assertEqual(mapped, "0:a:0")  # the stream the render graph reads as [0:a]
        self.assertLess(command.index("-i"), command.index("-vn"))  # an output option

    @staticmethod
    def evaluate_cut_expression(expression: str, variable: str, value: float) -> float:
        """Evaluate the subset of ffmpeg's expression language the cuts use."""

        code = CutExpressionCache.compiled(expression)
        functions = {
            "_if": lambda condition, then, otherwise: then if condition else otherwise,
            "_lt": lambda a, b: 1.0 if a < b else 0.0,
            "_between": lambda x, lo, hi: 1.0 if lo <= x <= hi else 0.0,
        }
        return eval(code, {"__builtins__": {}}, {**functions, variable: value})

    def test_the_cut_expressions_find_the_right_segment(self):
        rng = random.Random(7)
        segments, cursor = [], 0.0
        for _ in range(1612):  # the lecture that exposed the per-frame cost
            cursor += rng.uniform(0.05, 3.0)
            length = rng.uniform(0.1, 30.0)
            segments.append(main.Segment(round(cursor, 3), round(cursor + length, 3)))
            cursor += length
        select = main.segment_select_expression(segments)
        pts = main.segment_pts_expression(segments)

        offsets, total = [], 0.0
        for segment in segments:
            offsets.append(total)
            total += segment.duration
        probes = [rng.uniform(-1.0, cursor + 1.0) for _ in range(3000)]
        for segment in segments[::40]:
            probes += [segment.start, segment.end, segment.start - 1e-4, segment.end + 1e-4]

        for t in probes:
            inside = [i for i, seg in enumerate(segments) if seg.start <= t <= seg.end]
            expected_pts = t - segments[inside[0]].start + offsets[inside[0]] if inside else 0.0
            self.assertEqual(bool(self.evaluate_cut_expression(select, "t", t)), bool(inside), t)
            self.assertAlmostEqual(
                self.evaluate_cut_expression(pts, "T", t), expected_pts, places=6, msg=t
            )

    def test_the_cut_expressions_stay_shallow(self):
        """Each frame should walk one path of the tree, not every segment."""

        segments = [main.Segment(i * 2.0, i * 2.0 + 1.0) for i in range(1612)]
        for expression in (
            main.segment_select_expression(segments),
            main.segment_pts_expression(segments),
        ):
            depth = deepest = 0
            for char in expression:
                depth += {"(": 1, ")": -1}.get(char, 0)
                deepest = max(deepest, depth)
            self.assertLessEqual(deepest, 2 * 12 + 3)  # 11-12 levels of if(lt(...))
            self.assertEqual(expression.count("between("), 1612)  # each segment once

    def test_no_segments_selects_nothing(self):
        self.assertEqual(main.segment_select_expression([]), "0")
        self.assertEqual(main.segment_pts_expression([]), "0")

    def test_silences_to_segments_keeps_padding_around_speech(self):
        segments = main.silences_to_segments(
            [main.Silence(3.0, 5.0), main.Silence(7.0, 7.3)],
            duration=10.0,
            padding=0.2,
            min_cut=0.1,
            min_segment=0.04,
        )

        self.assertEqual(
            segments,
            [
                main.Segment(0.0, 3.2),
                main.Segment(4.8, 10.0),
            ],
        )

    def test_atempo_filters_split_large_speedups(self):
        self.assertEqual(
            main.atempo_filters(5.0),
            ["atempo=2.000000", "atempo=2.000000", "atempo=1.250000"],
        )

    @staticmethod
    def audio_args(**overrides):
        args = main.parse_args(["input.mp4"])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_build_filtergraph_contains_expected_labels(self):
        args = self.audio_args(speed=1.25, filtergraph_mode="concat", audio_chain=["anull"])

        filtergraph = main.build_filtergraph(
            [main.Segment(0.0, 1.0), main.Segment(2.0, 3.5)],
            args,
        )

        self.assertIn("[0:v]trim=start=0:end=1,setpts=PTS-STARTPTS[v0]", filtergraph)
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcat][acat]", filtergraph)
        self.assertIn("[vcat]setpts=PTS/1.25000000[vout]", filtergraph)
        self.assertIn("[acat]anull[aout]", filtergraph)

    def test_only_concat_mode_warns_about_memory(self):
        media = main.MediaInfo(duration=10.0, has_audio=True, has_video=True, video_fps=30.0)
        for mode, warned in (("concat", True), ("select", False)):
            args = self.audio_args(filtergraph_mode=mode, audio_chain=["anull"])
            stderr = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
                main, "render_with_fallback", return_value=("libx264", 1.0)
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                main.render_segments(
                    input_path=Path("in.mp4"),
                    output_path=Path(tmp) / "out.mp4",
                    workdir=Path(tmp),
                    media=media,
                    silences=[],
                    segments=[main.Segment(0.0, 4.0), main.Segment(6.0, 10.0)],
                    args=args,
                )
            self.assertEqual(main.CONCAT_MEMORY_WARNING in stderr.getvalue(), warned, mode)

    def test_build_select_filtergraph_uses_single_linear_pass(self):
        args = self.audio_args(
            speed=2.0,
            denoise="none",
            loudness="none",
            no_gate=True,
            highpass=0.0,
            filtergraph_mode="select",
        )

        filtergraph = main.build_filtergraph(
            [main.Segment(0.0, 1.0), main.Segment(2.0, 3.5)],
            args,
            video_fps=25.0,
        )

        select = "if(lt(t\\,2)\\,between(t\\,0\\,1)\\,between(t\\,2\\,3.5))"
        pts = "if(lt(T\\,2)\\,between(T\\,0\\,1)*(T-0+0)\\,between(T\\,2\\,3.5)*(T-2+1))"
        self.assertIn(f"select='{select}'", filtergraph)
        self.assertIn(f"setpts='({pts})/2.00000000/TB'[vout]", filtergraph)
        self.assertIn(f"aselect='{select}'", filtergraph)
        self.assertIn(f"asetpts='({pts})/TB'", filtergraph)
        self.assertIn(
            "atempo=2.000000,alimiter=limit=0.7943:level=disabled,aresample=48000[aout]",
            filtergraph,
        )

    def test_segment_pts_expression_tracks_kept_timeline(self):
        expression = main.segment_pts_expression(
            [main.Segment(1.0, 2.5), main.Segment(4.0, 5.0)]
        )

        self.assertEqual(
            expression,
            "if(lt(T\\,4)\\,between(T\\,1\\,2.5)*(T-1+0)\\,between(T\\,4\\,5)*(T-4+1.5))",
        )

    def test_parse_fraction(self):
        self.assertEqual(main.parse_fraction("25/1"), 25.0)
        self.assertEqual(main.parse_fraction("0/0"), 0.0)

    def test_ffmpeg_input_options_include_start_and_limit(self):
        self.assertEqual(main.ffmpeg_input_options(), [])
        self.assertEqual(main.ffmpeg_input_options(12.5, 10.0), ["-ss", "12.5", "-t", "10"])

    def test_preview_csv_and_labels(self):
        self.assertEqual(main.csv_values(" none, afftdn ,, "), ["none", "afftdn"])
        self.assertEqual(main.safe_label("-35dB"), "-35dB")
        self.assertEqual(main.safe_label("a/b c"), "a_b_c")

    def test_validate_preview_denoise_modes_rejects_unknown(self):
        with self.assertRaises(main.PipelineError):
            main.validate_preview_denoise_modes(["none", "rnnoise"])

    def test_default_cut_and_preview_settings_are_opinionated_baseline(self):
        args = main.parse_args(["input.mp4"])

        self.assertEqual(args.silence_threshold, "auto")
        self.assertEqual(args.denoise, "auto")
        self.assertEqual(args.loudness, "dynaudnorm")
        self.assertEqual(args.target_lufs, -17.0)
        self.assertEqual(args.preview_thresholds, "-45dB")
        self.assertEqual(args.preview_denoise, "auto")

    def test_default_output_name_uses_an_underscore_suffix(self):
        self.assertEqual(
            main.default_output_path("/x/data/lec1.mp4").name, "lec1_lecturecut.mp4"
        )
        self.assertEqual(
            main.default_output_path("https://youtu.be/abc").name,
            "lecturecut-output.mp4",
        )

    def test_render_command_stamps_the_output(self):
        args = main.parse_args(["in.mp4"])
        command = main.render_command(
            input_path=main.Path("in.mp4"),
            output_path=main.Path("out.mp4"),
            filtergraph_path=main.Path("graph.ffmpeg"),
            encoder="libx264",
            args=args,
        )
        joined = " ".join(command)

        self.assertIn(f"{main.LECTURECUT_TAG}={main.LECTURECUT_VERSION}", command)
        # Without this flag the mov muxer drops unknown keys and the tag is lost.
        self.assertIn("+faststart+use_metadata_tags", joined)

    def test_repeat_runs_get_a_suffix_instead_of_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "lecture_lecturecut.mp4"
            target.write_bytes(b"first")
            args = main.parse_args(["in.mp4"])

            with contextlib.redirect_stdout(io.StringIO()):
                second = main.resolve_output_path(target, args)
                second.write_bytes(b"second")
                third = main.resolve_output_path(target, args)

            self.assertEqual(second.name, "lecture_lecturecut_2.mp4")
            self.assertEqual(third.name, "lecture_lecturecut_3.mp4")
            self.assertEqual(target.read_bytes(), b"first")

    def test_force_and_overwrite_keep_the_name(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "out.mp4"
            target.write_bytes(b"x")

            forced = main.resolve_output_path(target, main.parse_args(["in.mp4", "--force"]))
            chosen = main.resolve_output_path(
                target, main.parse_args(["in.mp4", "--if-exists", "overwrite"])
            )

            self.assertEqual(forced, target)
            self.assertEqual(chosen, target)

    def test_if_exists_error_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "out.mp4"
            target.write_bytes(b"x")
            args = main.parse_args(["in.mp4", "--if-exists", "error"])

            with self.assertRaises(main.PipelineError):
                main.resolve_output_path(target, args)

    def test_a_free_name_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "out.mp4"

            self.assertEqual(
                main.resolve_output_path(target, main.parse_args(["in.mp4"])), target
            )

    def test_suffix_is_the_default(self):
        self.assertEqual(main.parse_args(["in.mp4"]).if_exists, "suffix")

    def test_arnndn_without_a_model_fetches_the_default(self):
        args = main.parse_args(["in.mp4", "--denoise", "arnndn"])
        asked = []

        def fake_download(model, dest_dir=None):
            asked.append(model.name)
            return Path("/cache") / model.file

        with unittest.mock.patch.object(main, "download_arnndn_model", fake_download):
            resolved = main.validate_denoise_settings(args)

        default_file = main.ARNNDN_MODELS[main.ARNNDN_DEFAULT_MODEL].file
        self.assertEqual(asked, [main.ARNNDN_DEFAULT_MODEL])
        self.assertEqual(resolved, Path("/cache") / default_file)
        # The resolved path is written back so the chain builder repeats no work.
        self.assertEqual(args.arnndn_model, str(Path("/cache") / default_file))

    def test_a_catalogue_name_is_accepted_as_a_model(self):
        args = main.parse_args(["in.mp4", "--denoise", "arnndn", "--arnndn-model", "bd"])

        with unittest.mock.patch.object(
            main, "download_arnndn_model", lambda model, dest_dir=None: Path(model.file)
        ):
            self.assertEqual(main.validate_denoise_settings(args), Path("bd.rnnn"))

    def test_an_unknown_model_spec_is_refused(self):
        args = main.parse_args(
            ["in.mp4", "--denoise", "arnndn", "--arnndn-model", "/nope.rnnn"]
        )

        with self.assertRaises(main.PipelineError) as caught:
            main.check_denoise_settings(args)
        with self.assertRaises(main.PipelineError):
            main.validate_denoise_settings(args)

        self.assertIn("sh", str(caught.exception))

    def test_the_quick_check_neither_downloads_nor_touches_args(self):
        args = main.parse_args(["in.mp4", "--denoise", "arnndn"])

        def explode(*_args, **_kwargs):
            raise AssertionError("the quick check must not download")

        with unittest.mock.patch.object(main, "download_arnndn_model", explode):
            main.check_denoise_settings(args)
            main.check_denoise_settings(
                main.parse_args(["in.mp4", "--denoise", "arnndn", "--arnndn-model", "sh"])
            )

        self.assertIsNone(args.arnndn_model)

    def test_model_catalogue_is_pinned_and_complete(self):
        for name, model in main.ARNNDN_MODELS.items():
            self.assertEqual(model.name, name)
            self.assertEqual(len(model.sha256), 64)
            self.assertTrue(model.url.startswith("https://"))
            self.assertLess(model.size, main.ARNNDN_MODEL_MAX_BYTES)
            self.assertIn(model.signal, {"speech", "voice", "general"})
            self.assertIn(model.noise, {"recording", "general"})
        # The default was picked by measuring speech loss against SNR gain on real
        # lectures, not from the upstream table, so only its presence is asserted.
        self.assertIn(main.ARNNDN_DEFAULT_MODEL, main.ARNNDN_MODELS)

    def test_a_download_that_does_not_match_its_digest_is_rejected(self):
        model = main.ARNNDN_MODELS["sh"]

        with self.assertRaises(main.PipelineError) as caught:
            main.verify_model_bytes(b"not the model", model)

        self.assertIn("digest", str(caught.exception))

    def test_install_models_rejects_unknown_names(self):
        with self.assertRaises(main.PipelineError):
            main.install_models("sh,bogus")

    def test_auto_denoise_needs_no_model(self):
        self.assertIsNone(main.validate_denoise_settings(main.parse_args(["in.mp4"])))
        self.assertEqual(main.resolved_denoise_mode(main.parse_args(["in.mp4"])), "afftdn")

    def test_auto_denoise_prefers_arnndn_when_a_model_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp) / "sh.rnnn"
            model.write_bytes(b"model")
            args = main.parse_args(["in.mp4", "--arnndn-model", str(model)])

            self.assertEqual(main.resolved_denoise_mode(args), "arnndn")
            self.assertEqual(main.validate_denoise_settings(args), model)
            self.assertEqual(
                main.denoise_filters(args, noise_floor_db=-57.0, snr_db=20.0),
                [f"arnndn=m={main.filter_escape(str(model))}"],
            )

    def test_every_denoise_mode_is_explained(self):
        self.assertEqual(
            set(main.AUDIO_DENOISE_HELP), set(main.AUDIO_DENOISE_MODES)
        )
        self.assertTrue(all(main.AUDIO_DENOISE_HELP.values()))

    def test_name_hint_recognises_outputs_old_and_new(self):
        self.assertTrue(main.name_suggests_output(main.Path("a_lecturecut.mp4")))
        self.assertTrue(main.name_suggests_output(main.Path("b.lecturecut.mp4")))
        self.assertTrue(main.name_suggests_output(main.Path("c_LectureCut.mp4")))
        self.assertFalse(main.name_suggests_output(main.Path("Lecture_01.MOV")))
        self.assertFalse(main.name_suggests_output(main.Path("notes.mp4")))

    def calibrate_on_curve(self, curve, *, floor=-43.2, speech=-42.2, windows=8):
        """Run the threshold search against a made-up share-cut curve."""

        tried, seen_starts = [], []

        def fake_fraction(input_path, *, starts, window, threshold_db, min_silence, offset):
            tried.append(threshold_db)
            seen_starts.append(list(starts))
            return curve(threshold_db)

        analysis = self.analysis(
            noise_floor_db=floor,
            speech_lufs=speech,
            starts=tuple(float(i * 20) for i in range(windows)),
        )
        with unittest.mock.patch.object(main, "measure_silence_fraction", fake_fraction):
            with contextlib.redirect_stdout(io.StringIO()):
                result = main.calibrate_silence_threshold(
                    main.Path("in.mp4"), analysis=analysis, args=self.audio_args()
                )
        return result, tried, seen_starts

    @staticmethod
    def seminar_curve(threshold):
        """Roughly the seminar's: nothing below -42 dB, half gone by -33 dB."""

        return max(0.0, min(1.0, (threshold + 42.0) / 18.0)) ** 1.5

    def test_the_search_does_not_stop_after_two_short_steps(self):
        # The bug: two thresholds that both cut nothing sent the next step back to
        # one already tried, and the search returned the first, useless, one.
        result, tried, _ = self.calibrate_on_curve(self.seminar_curve)

        self.assertGreater(len(tried), 2)
        self.assertGreater(result, -40.0)

    def test_the_search_lands_near_the_target_not_just_inside_a_band(self):
        result, _, _ = self.calibrate_on_curve(self.seminar_curve)

        self.assertAlmostEqual(
            self.seminar_curve(result),
            main.SILENCE_FRACTION_TARGET,
            delta=main.SILENCE_FRACTION_TOLERANCE,
        )

    def test_the_search_starts_above_the_noise_floor(self):
        _, tried, _ = self.calibrate_on_curve(self.seminar_curve)

        # Derived from speech it would start at -48 dB, where nothing can be found.
        self.assertGreaterEqual(tried[0], -43.2)

    def test_the_search_samples_every_analysis_window(self):
        _, _, seen = self.calibrate_on_curve(self.seminar_curve, windows=8)

        self.assertEqual(len(seen[0]), 8)

    def test_a_steep_curve_still_converges(self):
        steep = lambda t: 0.0 if t < -32.5 else (0.16 if t < -32.1 else 0.45)
        result, tried, _ = self.calibrate_on_curve(steep, floor=-38.3, speech=-36.0)

        self.assertLessEqual(len(tried), main.SILENCE_CALIBRATION_ROUNDS)
        self.assertAlmostEqual(steep(result), 0.16)

    def test_arnndn_strength_blends_in_the_input(self):
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp) / "lq.rnnn"
            model.write_bytes(b"model")
            full = main.parse_args(["in.mp4", "--denoise", "arnndn", "--arnndn-model", str(model)])
            gentle = main.parse_args(
                ["in.mp4", "--denoise", "arnndn", "--arnndn-model", str(model), "--arnndn-mix", "0.6"]
            )

            full_stage = main.denoise_filters(full, noise_floor_db=-43.0, snr_db=3.0)[0]
            gentle_stage = main.denoise_filters(gentle, noise_floor_db=-43.0, snr_db=3.0)[0]

        self.assertNotIn("mix=", full_stage)
        self.assertTrue(gentle_stage.endswith(":mix=0.6"))

    def test_arnndn_strength_must_be_a_fraction(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main.parse_args(["in.mp4", "--arnndn-mix", "1.5"])

    def test_gate_stays_clear_of_the_speech(self):
        # Good SNR: the old floor+8 rule already sat well below the speech.
        self.assertAlmostEqual(
            main.gate_threshold_db(noise_floor_db=-57.0, speech_lufs=-37.0), -49.0
        )

    def test_gate_is_refused_when_there_is_no_room_for_it(self):
        # ~8 dB SNR: floor+8 would land inside the speech and silence passages.
        self.assertIsNone(
            main.gate_threshold_db(noise_floor_db=-38.0, speech_lufs=-29.8)
        )

    def test_chain_omits_the_gate_on_a_noisy_recording(self):
        noisy = self.analysis(noise_floor_db=-38.0, speech_lufs_median=-29.8)
        quiet = self.analysis(noise_floor_db=-57.0, speech_lufs_median=-37.0)

        noisy_chain = main.audio_chain_for(self.audio_args(), analysis=noisy)
        quiet_chain = main.audio_chain_for(self.audio_args(), analysis=quiet)

        self.assertNotIn("agate", [item.split("=")[0] for item in noisy_chain])
        self.assertIn("agate", [item.split("=")[0] for item in quiet_chain])

    def checks(self, *results):
        """A fake measurement returning each check in turn, then None when off."""

        queue = list(results)

        def measure(input_path, *, args, analysis):
            if main.resolved_denoise_mode(args) == "none" or not queue:
                return None
            check = queue.pop(0)
            return main.DenoiseCheck(
                main.resolved_denoise_mode(args),
                check.speech_before,
                check.speech_after,
                check.tilt_before,
                check.tilt_after,
            )

        return measure

    def run_sanity(self, args, *results):
        with unittest.mock.patch.object(main, "measure_denoise_effect", self.checks(*results)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main.enforce_denoise_sanity(main.Path("in.mp4"), args=args, analysis=self.analysis())

    def test_a_muffling_denoiser_is_replaced_even_at_equal_loudness(self):
        # The seminar case: 0.3 dB of loudness, but the upper frequencies gone.
        args = self.audio_args(denoise="afftdn")
        self.run_sanity(args, main.DenoiseCheck("afftdn", -40.0, -40.3, 0.0, -9.3))

        self.assertEqual(args.denoise, "none")

    def test_a_thinning_denoiser_is_replaced_too(self):
        args = self.audio_args(denoise="arnndn", arnndn_model="/m.rnnn")
        self.run_sanity(
            args,
            main.DenoiseCheck("arnndn", -40.0, -40.5, 0.0, +7.1),
            main.DenoiseCheck("afftdn", -40.0, -40.2, 0.0, -2.0),
        )

        self.assertEqual(args.denoise, "afftdn")

    def test_the_fallback_is_measured_as_well(self):
        # arnndn fails, and so does the afftdn it falls back to: end with none.
        args = self.audio_args(denoise="arnndn", arnndn_model="/m.rnnn")
        self.run_sanity(
            args,
            main.DenoiseCheck("arnndn", -40.0, -46.1),
            main.DenoiseCheck("afftdn", -40.0, -40.3, 0.0, -10.4),
        )

        self.assertEqual(args.denoise, "none")

    def test_a_modest_tilt_is_accepted(self):
        args = self.audio_args(denoise="afftdn")
        self.run_sanity(args, main.DenoiseCheck("afftdn", -40.0, -40.2, 0.0, -5.5))

        self.assertEqual(args.denoise, "afftdn")

    def test_spectral_probe_graph_orders_body_before_presence(self):
        graph = main.spectral_probe_graph(["highpass=f=85"])

        self.assertIn("ebur128", graph)
        self.assertLess(
            graph.index("lowpass=f=1000,volumedetect"), graph.index("lowpass=f=8000,volumedetect")
        )

    def test_spectral_probe_log_is_read_by_instance_order(self):
        # ffmpeg may print the later instance first; the index decides.
        log = "\n".join(
            [
                "[Parsed_volumedetect_9 @ 0x2] mean_volume: -52.0 dB",
                "[Parsed_volumedetect_6 @ 0x1] mean_volume: -40.0 dB",
                "    I:         -38.5 LUFS",
            ]
        )

        loudness, tilt = main.parse_spectral_probe(log)

        self.assertEqual(loudness, -38.5)
        self.assertEqual(tilt, -12.0)  # presence (-52) minus body (-40)

    def test_a_probe_without_band_levels_gives_no_tilt(self):
        self.assertEqual(
            main.parse_spectral_probe("    I:         -38.5 LUFS"), (-38.5, None)
        )

    def test_denoise_loss_is_the_difference_in_speech_level(self):
        check = main.DenoiseCheck("arnndn", speech_before=-22.0, speech_after=-48.6)

        self.assertAlmostEqual(check.loss_db, 26.6)

    def test_a_destructive_denoiser_is_replaced(self):
        args = self.audio_args(denoise="arnndn", arnndn_model="/model.rnnn")
        check = main.DenoiseCheck("arnndn", speech_before=-22.0, speech_after=-48.6)

        with unittest.mock.patch.object(
            main, "measure_denoise_effect", self.checks(check)
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main.enforce_denoise_sanity(
                    main.Path("in.mp4"), args=args, analysis=self.analysis()
                )

        self.assertEqual(args.denoise, "afftdn")

    def test_a_harmless_denoiser_is_kept(self):
        args = self.audio_args(denoise="arnndn", arnndn_model="/model.rnnn")
        check = main.DenoiseCheck("arnndn", speech_before=-22.0, speech_after=-23.1)

        with unittest.mock.patch.object(
            main, "measure_denoise_effect", self.checks(check)
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main.enforce_denoise_sanity(
                    main.Path("in.mp4"), args=args, analysis=self.analysis()
                )

        self.assertEqual(args.denoise, "arnndn")

    def test_a_destructive_afftdn_falls_back_to_no_denoise(self):
        args = self.audio_args(denoise="afftdn")
        check = main.DenoiseCheck("afftdn", speech_before=-22.0, speech_after=-40.0)

        with unittest.mock.patch.object(
            main, "measure_denoise_effect", self.checks(check)
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main.enforce_denoise_sanity(
                    main.Path("in.mp4"), args=args, analysis=self.analysis()
                )

        self.assertEqual(args.denoise, "none")

    def test_the_loss_check_can_be_switched_off(self):
        args = self.audio_args(denoise="arnndn", denoise_loss_limit=0.0)

        def explode(*a, **k):
            raise AssertionError("the check must not run when disabled")

        with unittest.mock.patch.object(main, "measure_denoise_effect", explode):
            main.enforce_denoise_sanity(
                main.Path("in.mp4"), args=args, analysis=self.analysis()
            )

        self.assertEqual(args.denoise, "arnndn")

    def test_percentile_interpolates(self):
        self.assertEqual(main.percentile([1.0], 0.5), 1.0)
        self.assertEqual(main.percentile([0.0, 10.0], 0.5), 5.0)
        self.assertAlmostEqual(main.percentile([0.0, 5.0, 10.0, 20.0], 0.1), 1.5)

    def test_sample_windows_stay_inside_the_input(self):
        starts = main.sample_windows(600.0, count=4, window=20.0)

        self.assertEqual(len(starts), 4)
        self.assertGreater(starts[0], 0.0)
        self.assertLessEqual(starts[-1] + 20.0, 600.0)
        self.assertEqual(main.sample_windows(5.0, count=8, window=20.0), [0.0])

    def test_parse_astats_blocks_splits_channels_and_overall(self):
        log = "\n".join(
            [
                "[Parsed_astats_0 @ 0x1] Channel: 1",
                "[Parsed_astats_0 @ 0x1] Peak level dB: -11.40",
                "[Parsed_astats_0 @ 0x1] Channel: 2",
                "[Parsed_astats_0 @ 0x1] Peak level dB: -3.17",
                "[Parsed_astats_0 @ 0x1] Overall",
                "[Parsed_astats_0 @ 0x1] RMS level dB: -38.36",
                "[Parsed_astats_0 @ 0x1] Noise floor dB: -inf",
            ]
        )

        channels, overall = main.parse_astats_blocks(log)

        self.assertEqual([item["Peak level dB"] for item in channels], [-11.40, -3.17])
        self.assertEqual(overall["RMS level dB"], -38.36)
        self.assertIsNone(overall["Noise floor dB"])

    def test_ebur128_summary_parsing(self):
        log = "\n".join(
            [
                "[Parsed_ebur128_0 @ 0x1] Summary:",
                "",
                "  Integrated loudness:",
                "    I:         -41.2 LUFS",
                "    Threshold: -52.6 LUFS",
                "",
                "  Loudness range:",
                "    LRA:         8.6 LU",
                "",
                "  True peak:",
                "    Peak:      -15.2 dBFS",
            ]
        )

        self.assertEqual(main.first_match(main.EBUR128_I_RE, log), -41.2)
        self.assertEqual(main.first_match(main.EBUR128_LRA_RE, log), 8.6)
        self.assertEqual(main.first_match(main.EBUR128_PEAK_RE, log), -15.2)

    def test_derived_silence_threshold_stays_below_speech(self):
        analysis = self.analysis(speech_lufs=-41.2, noise_floor_db=-57.0)

        self.assertAlmostEqual(main.derive_silence_threshold(analysis), -47.2)
        self.assertEqual(
            main.derive_silence_threshold(None), main.FALLBACK_SILENCE_THRESHOLD_DB
        )

    def test_silence_bias_shifts_the_calibrated_threshold(self):
        # Positive bias raises the threshold, so more of the pauses get cut.
        self.assertAlmostEqual(main.apply_silence_bias(-49.1, 5.0), -44.1)
        self.assertAlmostEqual(main.apply_silence_bias(-49.1, -5.0), -54.1)
        self.assertAlmostEqual(main.apply_silence_bias(-49.1, 0.0), -49.1)

    def test_silence_bias_stays_inside_the_usable_range(self):
        low, high = main.SILENCE_THRESHOLD_LIMITS

        self.assertEqual(main.apply_silence_bias(-49.1, -40.0), low)
        self.assertEqual(main.apply_silence_bias(-49.1, 40.0), high)

    def test_silence_bias_defaults_to_no_shift(self):
        self.assertEqual(main.parse_args(["in.mp4"]).silence_bias, 0.0)

    def test_derived_silence_threshold_tracks_a_loud_noise_floor(self):
        # A noisy room must not push the threshold above gated speech level.
        analysis = self.analysis(speech_lufs=-30.0, noise_floor_db=-32.0)

        self.assertAlmostEqual(main.derive_silence_threshold(analysis), -36.0)

    @staticmethod
    def analysis(**overrides):
        values = {
            "windows": (),
            "starts": (0.0,),
            "speech_lufs": -41.2,
            "speech_lufs_median": -37.0,
            "noise_floor_db": -57.0,
            "true_peak_db": -0.7,
            "lra": 18.7,
            "channel_imbalance_db": 2.4,
        }
        values.update(overrides)
        return main.AudioAnalysis(**values)

    def test_denoise_adapts_to_the_measured_noise_floor(self):
        args = self.audio_args()

        quiet_room = main.denoise_filters(args, noise_floor_db=-70.0, snr_db=35.0)
        noisy_room = main.denoise_filters(args, noise_floor_db=-45.0, snr_db=12.0)

        self.assertEqual(quiet_room, ["afftdn=nr=10:nf=-64:tn=1"])
        # 12 dB of SNR leaves 1 dB of headroom above the floor, not the 6 dB a
        # clean recording gets.
        self.assertEqual(noisy_room, ["afftdn=nr=28:nf=-44:tn=1"])

    def test_afftdn_profile_never_sits_above_the_quiet_speech(self):
        # The seminar that came out sounding under water: floor -43.2 dB, speech
        # in its quiet passages at -42.6 LUFS, 3.3 dB of SNR. The old rule put the
        # profile at -37, over the speech; it has to stay under it.
        profile = main.afftdn_noise_profile(
            noise_floor_db=-43.2, snr_db=3.3, quiet_speech_lufs=-42.6
        )

        self.assertEqual(profile, -46)
        self.assertLess(profile, -42.6)

    def test_a_clean_recording_keeps_its_headroom(self):
        # 31.7 dB of SNR: the speech guard is far away and the full margin applies.
        self.assertEqual(
            main.afftdn_noise_profile(
                noise_floor_db=-49.1, snr_db=31.7, quiet_speech_lufs=-23.0
            ),
            -43,
        )

    def test_headroom_shrinks_with_the_snr(self):
        profile = lambda snr: main.afftdn_noise_profile(
            noise_floor_db=-50.0, snr_db=snr, quiet_speech_lufs=None
        )

        self.assertEqual(profile(3.0), -50)
        self.assertEqual(profile(10.0), -50)
        self.assertEqual(profile(16.0), -47)
        self.assertEqual(profile(22.0), -44)
        self.assertEqual(profile(40.0), -44)

    def test_denoise_modes_and_model_validation(self):
        self.assertEqual(
            main.denoise_filters(
                self.audio_args(denoise="none"), noise_floor_db=-57.0, snr_db=20.0
            ),
            [],
        )
        with self.assertRaises(main.PipelineError):
            main.denoise_filters(
                self.audio_args(denoise="auto", arnndn_model="/nope/missing.rnnn"),
                noise_floor_db=-57.0,
                snr_db=20.0,
            )

    def test_loudness_gain_ceiling_follows_the_deficit(self):
        args = self.audio_args(target_lufs=-17.0)

        quiet = main.loudness_filters(args, speech_lufs=-37.0, noise_floor_db=-57.0)
        already_loud = main.loudness_filters(args, speech_lufs=-17.0, noise_floor_db=-57.0)

        self.assertEqual(quiet, ["dynaudnorm=f=400:g=15:p=0.841:m=20.0:s=12"])
        self.assertEqual(already_loud, ["dynaudnorm=f=400:g=15:p=0.841:m=4.0:s=12"])

    def test_audio_chain_cleans_before_it_lifts(self):
        chain = main.audio_chain_for(
            self.audio_args(),
            analysis=self.analysis(),
            gain_plan=main.GainPlan(bias_db=2.0),
        )
        names = [item.split("=")[0] for item in chain]

        self.assertEqual(
            names,
            [
                "highpass",
                "afftdn",
                "agate",
                "dynaudnorm",
                "volume",
                "atempo",
                "alimiter",
                "aresample",
            ],
        )
        self.assertLess(names.index("afftdn"), names.index("dynaudnorm"))
        self.assertLess(names.index("atempo"), names.index("alimiter"))

    def test_previews_use_the_measured_audio_chain(self):
        analysis = self.analysis()
        plan = main.GainPlan(bias_db=-3.0)
        chains = []

        def fake_render(**kwargs):
            chains.append(kwargs["args"].audio_chain)
            return "libx264", 1.0

        media = main.MediaInfo(duration=60.0, has_audio=True, has_video=True, video_fps=30.0)
        with tempfile.TemporaryDirectory() as tmp:
            args = main.parse_args(
                ["in.mp4", "--preview-dir", tmp, "--preview-thresholds=-40dB,-30dB",
                 "--preview-denoise", "none"]
            )
            with contextlib.ExitStack() as stack:
                patch = lambda name, **kw: stack.enter_context(
                    unittest.mock.patch.object(main, name, **kw)
                )
                patch("probe_media", return_value=media)
                patch("analyze_audio", return_value=analysis)
                patch("measure_denoise_effect", return_value=None)
                patch("calibrate_gain", return_value=plan)
                patch("plan_segments", return_value=([], [main.Segment(0.0, 8.0)]))
                patch("render_segments", side_effect=fake_render)
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                main.run_preview_sweep(input_path=Path("in.mp4"), workdir=Path(tmp), args=args)

        expected_args = main.parse_args(["in.mp4", "--denoise", "none"])
        expected = main.audio_chain_for(expected_args, analysis=analysis, gain_plan=plan)
        self.assertEqual(chains, [expected, expected])
        self.assertNotEqual(expected, main.audio_chain_for(expected_args, analysis=None))

    def test_audio_chain_omits_a_negligible_gain_bias(self):
        chain = main.audio_chain_for(
            self.audio_args(),
            analysis=self.analysis(),
            gain_plan=main.GainPlan(bias_db=0.05),
        )

        self.assertNotIn("volume", [item.split("=")[0] for item in chain])

    def test_large_deficit_is_closed_by_compression_not_by_raw_gain(self):
        chain = main.audio_chain_for(
            self.audio_args(),
            analysis=self.analysis(),
            gain_plan=main.GainPlan(makeup_db=7.0, bias_db=0.4),
        )
        names = [item.split("=")[0] for item in chain]

        self.assertIn("acompressor", names)
        self.assertLess(names.index("acompressor"), names.index("dynaudnorm") + 2)
        self.assertLess(names.index("acompressor"), names.index("alimiter"))
        self.assertIn("volume=0.40dB", chain)

    def test_compressor_makeup_is_linear_gain(self):
        # acompressor takes makeup as a multiplier, not dB.
        stage = main.compressor_filter(makeup_db=6.0, true_peak_db=-1.0)

        self.assertIn("makeup=1.995", stage)
        self.assertIn("threshold=0.21135", stage)

    def test_combine_loudness_uses_the_energy_domain(self):
        # An energy mean sits above the arithmetic/median value, tracking the loud
        # passages the way integrated loudness does.
        combined = main.combine_loudness([-11.6, -15.3, -16.9, -16.9])

        self.assertAlmostEqual(combined, -14.57, places=2)
        self.assertGreater(combined, -16.1)
        self.assertEqual(main.combine_loudness([-20.0, -20.0]), -20.0)

    def test_limiter_keeps_a_true_peak_margin(self):
        chain = main.audio_chain_for(self.audio_args(true_peak=-1.0), analysis=None)
        limiter = next(item for item in chain if item.startswith("alimiter"))

        # -2.0 dBFS of sample peak leaves room for inter-sample peaks at -1.0.
        self.assertIn("limit=0.7943", limiter)

    def test_hints_surface_what_the_measurements_imply(self):
        found = main.audio_analysis_hints(
            self.analysis(channel_imbalance_db=3.3, true_peak_db=-0.7, speech_lufs_median=-48.0)
        )
        hints = " ".join(hint["text"] for hint in found)

        # The key and the numbers travel with the sentence, so a UI can say it
        # in its own language.
        self.assertEqual([hint["key"] for hint in found], ["mono", "declick", "arnndn"])
        self.assertEqual(found[0]["imbalance"], 3.3)

        self.assertIn("--mono", hints)
        self.assertIn("--declick", hints)
        self.assertIn("arnndn", hints)
        self.assertEqual(
            main.audio_analysis_hints(
                self.analysis(channel_imbalance_db=0.2, true_peak_db=-12.0)
            ),
            [],
        )

    def test_gain_bias_never_slams_the_limiter(self):
        self.assertEqual(main.GAIN_BIAS_LIMITS[1], 2.0)
        self.assertEqual(main.clamp(9.0, *main.GAIN_BIAS_LIMITS), 2.0)

    def test_build_audio_filters_prefers_the_resolved_chain(self):
        args = self.audio_args(audio_chain=["anull"])

        self.assertEqual(main.build_audio_filters(args), ["anull"])


class LivePreviewTests(unittest.TestCase):
    """Rendering into a playlist that can be watched while it grows."""

    def render(self, **overrides):
        values = {
            "input_path": Path("in.MOV"),
            "output_path": Path("out.tmp.mp4"),
            "filtergraph_path": Path("graph.ffmpeg"),
            "encoder": "libx264",
            "args": main.parse_args(["in.MOV"]),
        }
        values.update(overrides)
        return main.render_command(**values)

    def test_live_render_writes_an_event_playlist(self):
        command = self.render(live_dir=Path("/live"))
        joined = " ".join(command)

        self.assertIn("-f hls", joined)
        self.assertIn("-hls_playlist_type event", joined)
        self.assertIn("-hls_segment_type fmp4", joined)
        # Segments appear only once complete, so a player never reads half of one.
        self.assertIn("temp_file", joined)
        self.assertEqual(command[-1], "/live/index.m3u8")

    def test_live_render_leaves_tags_to_the_remux(self):
        joined = " ".join(self.render(live_dir=Path("/live")))

        self.assertNotIn("-metadata", joined)
        self.assertNotIn("faststart", joined)

    def test_direct_render_is_unchanged(self):
        command = self.render()

        self.assertEqual(command[-1], "out.tmp.mp4")
        self.assertIn("+faststart+use_metadata_tags", command)
        self.assertNotIn("hls", " ".join(command))

    def test_remux_copies_streams_and_keeps_the_source_metadata(self):
        command = main.remux_live_command(
            live_dir=Path("/live"), source=Path("/in.MOV"), output_path=Path("/out.mp4")
        )
        joined = " ".join(command)

        self.assertIn("-i /live/index.m3u8 -i /in.MOV", joined)
        self.assertIn("-map 0 -map_metadata 1", joined)
        self.assertIn("-c copy", joined)
        self.assertIn(f"{main.LECTURECUT_TAG}={main.LECTURECUT_VERSION}", command)
        self.assertIn("+faststart+use_metadata_tags", command)
        self.assertEqual(command[-1], "/out.mp4")

    def test_clearing_the_live_dir_removes_only_our_files(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            ours = ["index.m3u8", "init.mp4", "seg_00000.m4s", "seg_00007.m4s.tmp"]
            theirs = ["unrelated.txt", "seg_1.m4s", "index.m3u8.bak", "movie.mp4"]
            for name in ours + theirs:
                (folder / name).write_text("x")

            main.clear_live_dir(folder)

            self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(theirs))

    def test_clearing_a_missing_dir_is_harmless(self):
        main.clear_live_dir(Path("/definitely/not/here"))

    def fallback_run(self, *, returncodes, keep=False):
        """Drive render_with_fallback with a fake ffmpeg; return what happened."""

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        live = base / "live"
        output = base / "out.mp4"
        argv = ["in.MOV", "--live-dir", str(live), "--encoder", "auto"]
        if keep:
            argv.append("--keep-live-dir")
        args = main.parse_args(argv)
        codes = iter(returncodes)
        commands, events = [], []

        def fake_run(command, capture=False, check=True, progress_total=None):
            commands.append(command)
            if "hls" in command:
                (live / "index.m3u8").write_text("#EXTM3U")
                (live / "seg_00000.m4s").write_text("segment")
                code = next(codes)
            else:  # the remux writes the temporary MP4
                Path(command[-1]).write_text("final")
                code = 0
            return subprocess.CompletedProcess(command, code, "", "")

        reporter = main.Reporter(
            on_line=lambda text, error: None,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )
        token = main.ACTIVE_REPORTER.set(reporter)
        try:
            with unittest.mock.patch.object(main, "run_command", fake_run), \
                    unittest.mock.patch.object(
                        main, "encoder_candidates", lambda args: ["h264_nvenc", "libx264"]
                    ):
                encoder, _ = main.render_with_fallback(
                    input_path=Path("in.MOV"),
                    output_path=output,
                    filtergraph_path=base / "graph",
                    args=args,
                )
        finally:
            main.ACTIVE_REPORTER.reset(token)
        return encoder, commands, events, live, output

    def test_a_live_render_is_remuxed_into_the_output(self):
        encoder, commands, events, live, output = self.fallback_run(returncodes=[0])

        self.assertEqual(encoder, "h264_nvenc")
        self.assertEqual(output.read_text(), "final")
        self.assertIn("-c", commands[-1])
        self.assertEqual([k for k, _ in events if k == "live"], ["live"])
        # Segments are cleaned up once the MP4 exists...
        self.assertEqual(list(live.iterdir()), [])

    def test_the_caller_can_keep_the_playlist(self):
        _, _, _, live, _ = self.fallback_run(returncodes=[0], keep=True)

        # ...unless a viewer may still be reading them.
        self.assertTrue((live / "index.m3u8").exists())

    def test_a_fallback_attempt_starts_a_fresh_playlist(self):
        encoder, _, events, _, _ = self.fallback_run(returncodes=[1, 0])
        attempts = [fields["attempt"] for kind, fields in events if kind == "live"]

        self.assertEqual(encoder, "libx264")
        # A player must reload: the second encoder writes a new playlist.
        self.assertEqual(attempts, [1, 2])

    def test_when_every_encoder_fails_the_error_says_why(self):
        """A web job used to get only "returned non-zero exit status 234"."""

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        args = main.parse_args(["in.MOV", "--encoder", "auto"])
        reasons = iter(
            [
                "[h264_nvenc] OpenEncodeSessionEx failed: unsupported device\n",
                "x264 [error]: invalid preset 'bogus'\nConversion failed!\n",
            ]
        )
        lines = []

        def fake_run(command, capture=False, check=True, progress_total=None):
            return subprocess.CompletedProcess(command, 234, "", next(reasons))

        reporter = main.Reporter(
            on_line=lambda text, error: lines.append(text), on_event=lambda *a: None
        )
        token = main.ACTIVE_REPORTER.set(reporter)
        try:
            with unittest.mock.patch.object(main, "run_command", fake_run), \
                    unittest.mock.patch.object(
                        main, "encoder_candidates", lambda args: ["h264_nvenc", "libx264"]
                    ):
                with self.assertRaises(main.PipelineError) as caught:
                    main.render_with_fallback(
                        input_path=Path("in.MOV"),
                        output_path=base / "out.mp4",
                        filtergraph_path=base / "graph",
                        args=args,
                    )
        finally:
            main.ACTIVE_REPORTER.reset(token)

        message = str(caught.exception)
        self.assertIn("h264_nvenc, libx264", message)
        self.assertIn("exit code 234", message)
        self.assertIn("invalid preset 'bogus'", message)
        self.assertNotIn("Conversion failed!", message)
        # The fallback itself is still announced, not raised.
        self.assertTrue(any("falling back to libx264" in line for line in lines))

    def test_live_options_are_grouped_for_the_page_to_skip(self):
        titles = [group["title"] for group in main.parser_schema()]

        self.assertIn("live preview", titles)


class ParserSchemaTests(unittest.TestCase):
    def test_schema_exposes_groups_and_field_kinds(self):
        schema = main.parser_schema()
        by_dest = {
            field["dest"]: field
            for group in schema
            for field in group["fields"]
        }

        self.assertIn("audio", [group["title"] for group in schema])
        self.assertEqual(by_dest["mono"]["kind"], "flag")
        self.assertEqual(by_dest["denoise"]["kind"], "choice")
        self.assertEqual(by_dest["denoise"]["choices"], list(main.AUDIO_DENOISE_MODES))
        self.assertEqual(by_dest["target_lufs"]["kind"], "number")
        self.assertEqual(by_dest["analysis_samples"]["kind"], "integer")
        self.assertEqual(by_dest["workdir"]["kind"], "path")
        self.assertEqual(by_dest["target_lufs"]["default"], -17.0)
        self.assertTrue(by_dest["denoise"]["help"])
        # the positional source is selected separately, not rendered as a field
        self.assertNotIn("source", by_dest)

    def test_schema_lists_every_dest_once(self):
        dests = [
            field["dest"]
            for group in main.parser_schema()
            for field in group["fields"]
        ]

        self.assertEqual(len(dests), len(set(dests)))

    def test_settings_round_trip_through_argparse(self):
        settings = {
            "speed": 1.5,
            "target_lufs": -19.0,
            "denoise": "none",
            "mono": True,
            "no_gate": False,
            "silence_threshold": "-45dB",
        }

        argv = main.settings_to_argv("input.mp4", settings)
        args = main.parse_args(argv)

        self.assertEqual(args.source, "input.mp4")
        self.assertEqual(args.speed, 1.5)
        self.assertEqual(args.target_lufs, -19.0)
        self.assertEqual(args.denoise, "none")
        self.assertTrue(args.mono)
        self.assertFalse(args.no_gate)
        self.assertEqual(args.silence_threshold, "-45dB")
        # a false flag is simply absent rather than passed as a value
        self.assertNotIn("--no-gate", argv)
        # values are attached with '=' so that -45dB is not read as an option
        self.assertIn("--silence-threshold=-45dB", argv)

    def test_settings_reject_unknown_keys(self):
        with self.assertRaises(main.PipelineError):
            main.settings_to_argv("input.mp4", {"nonsense": 1})

    def test_invalid_values_are_rejected_by_argparse(self):
        argv = main.settings_to_argv("input.mp4", {"speed": -1})

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main.parse_args(argv)


class NumericOptionTests(unittest.TestCase):
    def assert_rejected(self, *argv: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main.parse_args(["input.mp4", *argv])

    def test_no_float_option_takes_inf_or_nan(self):
        float_options = [
            action.option_strings[-1]
            for action in main.build_parser()._actions
            if main.field_kind(action) == "number"
        ]
        self.assertIn("--speed", float_options)
        self.assertIn("--target-lufs", float_options)
        for option in float_options:
            for value in ("inf", "-inf", "nan", "Infinity"):
                with self.subTest(option=option, value=value):
                    self.assert_rejected(f"{option}={value}")

    def test_quality_values_stay_on_the_encoder_scale(self):
        args = main.parse_args(["input.mp4", "--cq=0", "--crf=51"])
        self.assertEqual((args.cq, args.crf), (0, 51))
        for argv in ("--cq=52", "--cq=-1", "--crf=60", "--crf=-5"):
            with self.subTest(argv=argv):
                self.assert_rejected(argv)

    def test_atempo_filters_refuses_a_speed_it_cannot_reach(self):
        for speed in (float("inf"), float("nan"), 0.0, -1.0):
            with self.subTest(speed=speed):
                with self.assertRaises(main.PipelineError):
                    main.atempo_filters(speed)
        self.assertEqual(main.atempo_filters(5.0), ["atempo=2.000000"] * 2 + ["atempo=1.250000"])


class SilenceThresholdTests(unittest.TestCase):
    def parse_rejected(self, *argv: str) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                main.parse_args(["input.mp4", *argv])
        return stderr.getvalue()

    def test_spellings_of_a_level_become_one_canonical_form(self):
        for given, expected in (
            ("-45dB", "-45dB"),
            ("-45 dB", "-45dB"),
            ("-45db", "-45dB"),
            ("-45.5dB", "-45.5dB"),
            ("-45", "-45dB"),
            (" -45dB ", "-45dB"),
            ("AUTO", "auto"),
        ):
            with self.subTest(given=given):
                args = main.parse_args(["input.mp4", f"--silence-threshold={given}"])
                self.assertEqual(args.silence_threshold, expected)

    def test_filtergraph_syntax_cannot_ride_along(self):
        for value in (
            "-45dB,ametadata=mode=print:file=/tmp/x",
            "-45dB:d=0",
            "-45dB;[a]anull",
            "nan",
            "-infdB",
        ):
            with self.subTest(value=value):
                message = self.parse_rejected(f"--silence-threshold={value}")
                self.assertIn("--silence-threshold", message)

    def test_a_forgotten_minus_is_refused_rather_than_cutting_everything(self):
        for value in ("45dB", "45", "0dB", "-5dB", "-120dB"):
            with self.subTest(value=value):
                message = self.parse_rejected(f"--silence-threshold={value}")
                self.assertIn("e.g. -45dB", message)

    def test_preview_thresholds_are_checked_one_by_one(self):
        args = main.parse_args(["input.mp4", "--preview-thresholds= -40 , -45db,"])
        self.assertEqual(args.preview_thresholds, "-40dB,-45dB")

        self.parse_rejected("--preview-thresholds=-40dB,auto")
        self.parse_rejected("--preview-thresholds=-40dB,anull")
        self.parse_rejected("--preview-thresholds=,")

    def test_detect_silences_receives_only_the_canonical_level(self):
        args = main.parse_args(["input.mp4", "--silence-threshold=-45 dB"])
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with unittest.mock.patch.object(main, "require_command"), \
                unittest.mock.patch.object(main, "run_command", return_value=result) as run:
            main.detect_silences(
                Path("input.mp4"),
                threshold=args.silence_threshold,
                min_silence=0.35,
                duration=10.0,
                start=0.0,
                limit=None,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-af") + 1], "silencedetect=noise=-45dB:d=0.35")


class ProgressTests(unittest.TestCase):
    def test_parse_progress_seconds(self):
        self.assertEqual(main.parse_progress_seconds("out_time_us=1500000"), 1.5)
        self.assertEqual(main.parse_progress_seconds("out_time_ms=2000000\n"), 2.0)
        self.assertIsNone(main.parse_progress_seconds("frame=12"))
        self.assertIsNone(main.parse_progress_seconds("out_time_us=N/A"))

    def test_with_progress_output_keeps_ffmpeg_first(self):
        command = main.with_progress_output(["ffmpeg", "-i", "in.mp4", "out.mp4"])

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-progress", command)
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")
        self.assertIn("-nostats", command)

    def test_render_dominates_the_weighting(self):
        args = main.parse_args(["in.mp4"])
        tracker = main.plan_progress_weights(duration=5140.0, args=args)
        shares = tracker.normalized()

        self.assertGreater(shares[main.PHASE_RENDER], 0.8)
        # measurement is a fixed number of windows, so it barely registers on a
        # long lecture even though it takes a minute in absolute terms
        self.assertLess(shares[main.PHASE_MEASURE], 0.02)
        self.assertAlmostEqual(sum(shares.values()), 1.0, places=6)

    def test_short_input_shifts_weight_towards_measurement(self):
        args = main.parse_args(["in.mp4"])
        long_shares = main.plan_progress_weights(duration=5140.0, args=args).normalized()
        short_shares = main.plan_progress_weights(duration=60.0, args=args).normalized()

        self.assertGreater(
            short_shares[main.PHASE_MEASURE], long_shares[main.PHASE_MEASURE]
        )

    def test_skipped_phases_carry_no_weight(self):
        args = main.parse_args(["in.mp4", "--no-analyze", "--dry-run"])
        shares = main.plan_progress_weights(duration=600.0, args=args).normalized()

        self.assertEqual(shares[main.PHASE_MEASURE], 0.0)
        self.assertEqual(shares[main.PHASE_CALIBRATE], 0.0)
        self.assertEqual(shares[main.PHASE_RENDER], 0.0)
        self.assertEqual(shares[main.PHASE_SILENCE], 1.0)

    def test_overall_progress_accumulates_across_phases(self):
        events = []
        tracker = main.ProgressTracker(
            weights={main.PHASE_MEASURE: 1.0, main.PHASE_RENDER: 9.0},
            order=[main.PHASE_MEASURE, main.PHASE_RENDER],
        )
        reporter = main.Reporter(
            on_line=lambda text, error: None,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )
        token = main.ACTIVE_REPORTER.set(reporter)
        progress_token = main.ACTIVE_PROGRESS.set(tracker)
        try:
            main.start_phase(main.PHASE_MEASURE)
            main.report_progress(1.0)
            main.start_phase(main.PHASE_RENDER)
            main.report_progress(0.5)
        finally:
            main.ACTIVE_REPORTER.reset(token)
            main.ACTIVE_PROGRESS.reset(progress_token)

        overalls = [fields["overall"] for _, fields in events]
        self.assertEqual(overalls, sorted(overalls))
        self.assertAlmostEqual(overalls[1], 0.1)
        self.assertAlmostEqual(overalls[-1], 0.55)

    def test_progress_fraction_is_clamped(self):
        events = []
        tracker = main.ProgressTracker(
            weights={main.PHASE_RENDER: 1.0}, order=[main.PHASE_RENDER]
        )
        tracker.current = main.PHASE_RENDER
        reporter = main.Reporter(
            on_line=lambda text, error: None,
            on_event=lambda kind, fields: events.append(fields),
        )
        token = main.ACTIVE_REPORTER.set(reporter)
        progress_token = main.ACTIVE_PROGRESS.set(tracker)
        try:
            main.report_progress(1.7)
        finally:
            main.ACTIVE_REPORTER.reset(token)
            main.ACTIVE_PROGRESS.reset(progress_token)

        self.assertEqual(events[0]["overall"], 1.0)


class ReporterAndCancelTests(unittest.TestCase):
    def test_reporter_captures_lines_and_marks_errors(self):
        captured = []
        reporter = main.Reporter(
            on_line=lambda text, error: captured.append((text, error)),
            on_event=lambda kind, fields: None,
        )
        token = main.ACTIVE_REPORTER.set(reporter)
        try:
            main.report("hello")
            main.report("bad", error=True)
        finally:
            main.ACTIVE_REPORTER.reset(token)

        self.assertEqual(captured, [("hello", False), ("bad", True)])

    def test_run_command_stops_a_running_child(self):
        cancel = threading.Event()
        token = main.ACTIVE_CANCEL.set(cancel)
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        try:
            start = time.monotonic()
            with self.assertRaises(main.PipelineCancelled):
                main.run_command(["sleep", "30"])
            elapsed = time.monotonic() - start
        finally:
            timer.cancel()
            main.ACTIVE_CANCEL.reset(token)

        # it returns as soon as the child dies, not after sleep 30 finishes
        self.assertLess(elapsed, 10.0)

    def test_run_command_refuses_to_start_when_already_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        token = main.ACTIVE_CANCEL.set(cancel)
        try:
            with self.assertRaises(main.PipelineCancelled):
                main.run_command(["true"])
        finally:
            main.ACTIVE_CANCEL.reset(token)

    def test_progress_from_the_reader_thread_reaches_the_reporter(self):
        # The pipe readers run in their own threads; a bare thread would start
        # with an empty context and drop every progress event on the floor.
        fractions = []
        tracker = main.ProgressTracker(
            weights={main.PHASE_RENDER: 1.0}, order=[main.PHASE_RENDER]
        )
        tracker.current = main.PHASE_RENDER
        reporter = main.Reporter(
            on_line=lambda text, error: None,
            on_event=lambda kind, fields: fractions.append(fields["fraction"])
            if kind == "progress"
            else None,
        )
        tokens = (
            main.ACTIVE_REPORTER.set(reporter),
            main.ACTIVE_PROGRESS.set(tracker),
            main.ACTIVE_CANCEL.set(threading.Event()),
        )
        try:
            main.run_command(
                ["printf", "out_time_us=1000000\nout_time_us=2000000\n"],
                progress_total=4.0,
            )
        finally:
            main.ACTIVE_REPORTER.reset(tokens[0])
            main.ACTIVE_PROGRESS.reset(tokens[1])
            main.ACTIVE_CANCEL.reset(tokens[2])

        self.assertEqual(fractions, [0.25, 0.5])

    def test_both_pipes_drain_while_progress_is_tracked(self):
        """Capture and progress together must not starve either pipe.

        silencedetect over a long lecture writes far more to stderr than a pipe
        buffer holds, so if the stderr drain dies - as it does when both threads
        share one Context - ffmpeg blocks forever on a full pipe.
        """

        noisy = (
            "import sys\n"
            "sys.stdout.write('out_time_us=2000000\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('x' * 400000)\n"
        )
        fractions = []
        tracker = main.ProgressTracker(
            weights={main.PHASE_RENDER: 1.0}, order=[main.PHASE_RENDER]
        )
        tracker.current = main.PHASE_RENDER
        reporter = main.Reporter(
            on_line=lambda text, error: None,
            on_event=lambda kind, fields: fractions.append(fields["fraction"])
            if kind == "progress"
            else None,
        )
        tokens = (
            main.ACTIVE_REPORTER.set(reporter),
            main.ACTIVE_PROGRESS.set(tracker),
            main.ACTIVE_CANCEL.set(threading.Event()),
        )
        try:
            result = main.run_command(
                [sys.executable, "-c", noisy], capture=True, progress_total=4.0
            )
        finally:
            main.ACTIVE_REPORTER.reset(tokens[0])
            main.ACTIVE_PROGRESS.reset(tokens[1])
            main.ACTIVE_CANCEL.reset(tokens[2])

        self.assertEqual(len(result.stderr), 400000)
        self.assertEqual(fractions, [0.5])

    def test_undecodable_output_does_not_kill_the_drain(self):
        """ffmpeg echoes a cp1251 title tag as raw bytes in its banner.

        A strict decoder raised inside the drain thread, so everything after the
        banner - the silencedetect lines - was lost and, past a pipe buffer's
        worth of output, the child blocked for good. The output here stays under
        a pipe buffer so a regression fails instead of hanging the suite.
        """

        noisy = (
            "import sys\n"
            "title = '\\u041b\\u0435\\u043a\\u0446\\u0438\\u044f'.encode('cp1251')\n"
            "sys.stderr.buffer.write(b'    title           : ' + title + b'\\n')\n"
            "sys.stderr.buffer.write(b'[silencedetect] silence_start: 1.5\\n' * 1000)\n"
            "sys.stdout.buffer.write(title + b'\\nout\\n')\n"
        )
        died = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda hook_args: died.append(hook_args.exc_value)
        token = main.ACTIVE_CANCEL.set(threading.Event())
        try:
            result = main.run_command([sys.executable, "-c", noisy], capture=True)
        finally:
            main.ACTIVE_CANCEL.reset(token)
            threading.excepthook = previous_hook

        self.assertEqual(died, [])
        self.assertEqual(result.stderr.count("silence_start: 1.5"), 1000)
        self.assertTrue(result.stdout.endswith("out\n"))

    def test_progress_flags_are_only_added_for_ffmpeg(self):
        self.assertEqual(
            main.with_progress_output(["printf", "hello"]), ["printf", "hello"]
        )
        self.assertIn("-progress", main.with_progress_output(["ffmpeg", "-i", "a"]))

    def test_run_command_still_captures_output(self):
        cancel = threading.Event()
        token = main.ACTIVE_CANCEL.set(cancel)
        try:
            result = main.run_command(["echo", "captured"], capture=True)
        finally:
            main.ACTIVE_CANCEL.reset(token)

        self.assertEqual(result.stdout.strip(), "captured")
        self.assertEqual(result.returncode, 0)

    def test_run_command_raises_on_failure_when_checked(self):
        cancel = threading.Event()
        token = main.ACTIVE_CANCEL.set(cancel)
        try:
            with self.assertRaises(main.PipelineError):
                main.run_command(["false"], capture=True)
        finally:
            main.ACTIVE_CANCEL.reset(token)

    def test_a_failed_command_says_why(self):
        """A bare CalledProcessError was a traceback in the CLI and a 500 in /api/probe."""

        failing = (
            "import sys\n"
            "sys.stderr.write('banner\\n' * 50)\n"
            "sys.stderr.write('bad.mp4: Invalid data found when processing input\\n')\n"
            "sys.exit(3)\n"
        )
        token = main.ACTIVE_CANCEL.set(threading.Event())
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(main.CommandFailed) as caught:
                    main.run_command([sys.executable, "-c", failing], capture=True)
        finally:
            main.ACTIVE_CANCEL.reset(token)

        self.assertIsInstance(caught.exception, main.PipelineError)
        self.assertEqual(caught.exception.returncode, 3)
        message = str(caught.exception)
        self.assertIn("exit code 3", message)
        self.assertIn("Invalid data found when processing input", message)
        # Only the tail is quoted, not the whole banner.
        self.assertLessEqual(message.count("banner"), main.ERROR_TAIL_LINES)

    def test_ffmpeg_closing_summary_is_not_quoted_as_the_reason(self):
        stderr = (
            "x264 [error]: invalid preset 'bogus'\n"
            "frame=    0 fps=0.0 q=0.0 Lsize=       0kB time=N/A\n"
            "Conversion failed!\n"
        )

        self.assertEqual(main.stderr_tail(stderr), "x264 [error]: invalid preset 'bogus'")

    def test_uncaptured_stderr_passes_through_but_only_its_tail_is_kept(self):
        """A long render writes to stderr for hours; keep only what an error needs."""

        chatty = (
            "import sys\n"
            "for i in range(5000):\n"
            "    sys.stderr.write(f'line {i}\\n')\n"
        )
        console = io.StringIO()
        token = main.ACTIVE_CANCEL.set(threading.Event())
        try:
            with contextlib.redirect_stderr(console):
                result = main.run_command([sys.executable, "-c", chatty])
        finally:
            main.ACTIVE_CANCEL.reset(token)

        self.assertEqual(console.getvalue().count("\n"), 5000)
        kept = result.stderr.splitlines()
        self.assertEqual(len(kept), main.STDERR_TAIL_LINES)
        self.assertEqual(kept[-1], "line 4999")

    def test_a_short_child_is_not_held_for_a_whole_poll_interval(self):
        token = main.ACTIVE_CANCEL.set(threading.Event())
        try:
            start = time.monotonic()
            for _ in range(5):
                main.run_command(["true"], capture=True)
            elapsed = time.monotonic() - start
        finally:
            main.ACTIVE_CANCEL.reset(token)

        # Sleeping CANCEL_POLL_SECONDS per check made this at least 1 s.
        self.assertLess(elapsed, 5 * main.CANCEL_POLL_SECONDS * 0.75)

    def test_parallel_probes_see_the_jobs_reporter_and_cancel_event(self):
        lines = []
        cancel = threading.Event()
        reporter = main.Reporter(
            on_line=lambda text, error: lines.append(text), on_event=lambda kind, fields: None
        )

        def probe(item):
            main.report(f"probe {item}")
            return main.cancel_event() is cancel

        def job():
            main.ACTIVE_REPORTER.set(reporter)
            main.ACTIVE_CANCEL.set(cancel)
            return main.map_probes(probe, range(8))

        import contextvars

        results = contextvars.Context().run(job)

        self.assertEqual(results, [True] * 8)
        self.assertEqual(sorted(lines), sorted(f"probe {i}" for i in range(8)))

    def test_parallel_probes_keep_input_order(self):
        def probe(item):
            time.sleep(0.01 * (8 - item))
            return item

        self.assertEqual(main.map_probes(probe, range(8)), list(range(8)))

    def test_cancel_stops_parallel_probes(self):
        cancel = threading.Event()

        def job():
            main.ACTIVE_CANCEL.set(cancel)
            threading.Timer(0.3, cancel.set).start()
            return main.map_probes(lambda _: main.run_command(["sleep", "30"]), range(6))

        import contextvars

        start = time.monotonic()
        with self.assertRaises(main.PipelineCancelled):
            contextvars.Context().run(job)
        self.assertLess(time.monotonic() - start, 10.0)


def make_tiny_lecture(
    path: Path, seconds: int = 10, extra: tuple[str, ...] = (), size: str = "64x48"
) -> None:
    """Speech-like 1.5 s tone bursts with 1 s pauses, over a tiny test picture."""

    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration={seconds}",
         "-f", "lavfi", "-i",
         f"sine=frequency=300:duration={seconds},volume='if(lt(mod(t,2.5),1.5),0.3,0)':eval=frame",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", *extra, "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class PipelineEndToEndTests(unittest.TestCase):
    """The render path with real ffmpeg; everything else in this file mocks it."""

    def test_a_lecture_comes_out_cut_tagged_and_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "talk.mp4", Path(directory) / "out.mp4"
            make_tiny_lecture(source)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main.main([str(source), "-o", str(output), "--encoder=libx264",
                                  "--analysis-samples=2", "--speed=1.0"])
            self.assertEqual(code, 0)
            media = main.probe_media(output)
            tag = main.read_lecturecut_tag(output)

        self.assertTrue(media.has_audio and media.has_video)
        # 4 pauses of about 1 s go, minus the padding kept around speech.
        self.assertLess(media.duration, 9.0)
        self.assertGreater(media.duration, 5.0)
        self.assertTrue(tag)


HLG_TAGS = ("-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc")


class HdrTests(unittest.TestCase):
    def test_hlg_and_pq_are_converted_and_sdr_is_left_alone(self):
        hlg = {"color_transfer": "arib-std-b67", "color_primaries": "bt2020", "color_space": "bt2020nc"}
        pq = {"color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc"}
        self.assertIn("tin=arib-std-b67", main.hdr_to_sdr_filters(hlg)[0])
        self.assertIn("tin=smpte2084", main.hdr_to_sdr_filters(pq)[0])
        for filters in (main.hdr_to_sdr_filters(hlg), main.hdr_to_sdr_filters(pq)):
            self.assertIn("t=bt709:p=bt709:m=bt709", filters[0])
        for sdr in ({}, {"color_transfer": "bt709"}, {"color_transfer": "iec61966-2-1"}):
            self.assertEqual(main.hdr_to_sdr_filters(sdr), ())

    def test_unknown_primaries_fall_back_to_what_hdr_uses(self):
        filters = main.hdr_to_sdr_filters({"color_transfer": "arib-std-b67", "color_primaries": "unknown"})
        self.assertIn("pin=bt2020:min=bt2020nc", filters[0])

    def test_conversion_follows_the_cap_and_precedes_rotation(self):
        args = main.parse_args(["in.mp4", "--max-fps=30"])
        tail = main.video_tail_filters(args, video_fps=60, upright=("hflip", "vflip"), sdr=("zscale=x", "format=yuv420p"))
        self.assertEqual(tail, ["fps=30", "zscale=x", "format=yuv420p", "hflip", "vflip"])

    def test_off_passes_hdr_through(self):
        args = main.parse_args(["in.mp4", "--tonemap=off"])
        self.assertEqual(main.video_tail_filters(args, video_fps=30, upright=(), sdr=("zscale=x",)), [])

    def measured(self, highs, hdr_input="tin=arib-std-b67:pin=bt2020:min=bt2020nc"):
        media = main.MediaInfo(duration=600.0, has_audio=True, has_video=True, video_fps=30.0,
                               hdr="HLG" if hdr_input else "", hdr_input=hdr_input)
        values = iter(highs)

        def fake_run(command, **kwargs):
            value = next(values)
            return subprocess.CompletedProcess(command, 0, f"lavfi.signalstats.YHIGH={value}\n", "")

        with unittest.mock.patch.object(main, "run_command", fake_run):
            return main.measure_hdr_white(Path("phone.mov"), media, main.parse_args(["phone.mov"]))

    def test_white_is_put_at_the_target_rather_than_at_203(self):
        """Phones exposed the board at 420-500 cd/m2; at 203 it clipped to 255."""

        npl = self.measured([167] * main.HDR_PROBE_FRAMES)  # what Lecture 9 measured
        white = main.HDR_PROBE_NITS * main.bt709_to_linear(167)
        self.assertGreater(npl, 450)
        self.assertAlmostEqual(
            white / npl, main.bt709_to_linear(16 + main.HDR_WHITE_TARGET * 219), places=6
        )

    def test_the_measured_npl_stays_in_range(self):
        self.assertEqual(self.measured([20] * main.HDR_PROBE_FRAMES), main.HDR_NPL_RANGE[0])
        self.assertEqual(self.measured([235] * main.HDR_PROBE_FRAMES), main.HDR_NPL_RANGE[1])

    def test_a_stray_frame_does_not_move_the_exposure(self):
        steady = self.measured([167] * main.HDR_PROBE_FRAMES)
        with_outliers = self.measured([167] * (main.HDR_PROBE_FRAMES - 2) + [40, 235])
        self.assertAlmostEqual(steady, with_outliers, delta=steady * 0.02)

    def test_sdr_needs_no_measurement(self):
        with unittest.mock.patch.object(main, "run_command", side_effect=AssertionError("probed")):
            self.assertEqual(self.measured([], hdr_input=""), main.HDR_REFERENCE_WHITE_NITS)

    def test_code_values_map_to_linear_light(self):
        self.assertEqual(main.bt709_to_linear(16), 0.0)
        self.assertAlmostEqual(main.bt709_to_linear(235), 1.0, places=6)
        self.assertLess(main.bt709_to_linear(126), 0.3)  # mid code is far below mid light

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_an_hlg_recording_comes_out_tagged_sdr(self):
        def tags(path: Path) -> str:
            return subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=color_transfer,color_primaries", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True,
            ).stdout.strip()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "phone.mp4"
            make_tiny_lecture(source, extra=HLG_TAGS)
            self.assertEqual(main.probe_media(source).hdr, "HLG")
            results = {}
            for mode in ("auto", "off"):
                output = Path(directory) / f"{mode}.mp4"
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main.main([str(source), "-o", str(output), "--encoder=libx264",
                                                "--analysis-samples=2", f"--tonemap={mode}"]), 0)
                results[mode] = tags(output)

        self.assertEqual(results["auto"], "bt709,bt709")
        self.assertEqual(results["off"], "arib-std-b67,bt2020")


class BoardTests(unittest.TestCase):
    SEGMENTS = [main.Segment(0.0, 1.0), main.Segment(2.0, 3.0)]

    def test_off_leaves_the_graph_alone(self):
        args = main.parse_args(["in.mp4"])
        graph = main.build_filtergraph(self.SEGMENTS, args, size=(1920, 1080))
        self.assertNotIn("blend=all_mode=divide", graph)
        self.assertIn("[vout]", graph)

    def test_both_modes_divide_by_a_background_of_the_exact_frame_size(self):
        for mode in ("clean", "ink"):
            for graph_mode in ("select", "concat"):
                args = main.parse_args(["in.mp4", f"--board={mode}", f"--filtergraph-mode={graph_mode}"])
                graph = main.build_filtergraph(self.SEGMENTS, args, size=(1920, 1342))
                with self.subTest(mode=mode, graph=graph_mode):
                    self.assertIn("[vbase];", graph)
                    self.assertIn("scale=1920:1342:flags=bicubic[bfond]", graph)
                    self.assertTrue(graph.rstrip().endswith("[vout]"))
                    self.assertEqual(graph.count("[vout]"), 1)

    def test_a_quarter_turn_swaps_the_background_size(self):
        args = main.parse_args(["in.mp4", "--board=clean"])
        graph = main.build_filtergraph(self.SEGMENTS, args, size=(1920, 1080), upright=("transpose=clock",))
        self.assertIn("scale=1080:1920:flags=bicubic", graph)

    def test_an_unknown_frame_size_is_an_error_not_a_broken_graph(self):
        args = main.parse_args(["in.mp4", "--board=ink"])
        with self.assertRaises(main.PipelineError):
            main.build_filtergraph(self.SEGMENTS, args, size=(0, 0))

    def test_the_white_threshold_moves_the_curve_knee(self):
        for mode in ("clean", "ink"):
            with self.subTest(mode=mode):
                self.assertIn("0.900/0.97", main.board_finish(mode, 0.90))
                self.assertIn("0.950/0.97", main.board_finish(mode, 0.95))

    def test_the_white_threshold_is_bounded(self):
        for value in ("0.5", "1.2", "nan"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main.parse_args(["in.mp4", f"--board-white={value}"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_ink_keeps_a_faint_stroke_of_any_colour(self):
        """A green+blue mix left a blue marker at 155 of 255 against 80 for the others."""

        colours = {"black": "0x606060", "blue": "0x5070D0", "red": "0xD05050",
                   "green": "0x50A050", "orange": "0xD09050"}
        boxes = ",".join(
            f"drawbox=x={40 + 120 * i}:y=40:w=80:h=6:c={c}:t=fill"
            for i, c in enumerate(colours.values())
        )
        graph = main.board_graph("ink", (640, 200), "0:v", "out", white=0.95)
        greys = {}
        for i, name in enumerate(colours):
            stats = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=white:s=640x200:d=0.1,format=rgb24,{boxes}",
                 "-filter_complex", f"{graph};[out]crop=10:4:{80 + 120 * i}:41,signalstats,metadata=print:file=-",
                 "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, check=True,
            ).stdout
            greys[name] = float(re.search(r"YAVG=([\d.]+)", stats).group(1))
        self.assertLess(max(greys.values()) - min(greys.values()), 20, greys)
        self.assertLess(max(greys.values()), 120, greys)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_both_modes_render_a_frame_whose_height_does_not_divide_by_four(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "talk.mp4"
            make_tiny_lecture(source, size="96x50")
            for mode in ("clean", "ink"):
                output = Path(directory) / f"{mode}.mp4"
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = main.main([str(source), "-o", str(output), "--encoder=libx264",
                                      "--analysis-samples=2", f"--board={mode}"])
                with self.subTest(mode=mode):
                    self.assertEqual(code, 0)
                    media = main.probe_media(output)
                    self.assertEqual((media.width, media.height), (96, 50))


def display_matrix(a: int, b: int, c: int, d: int, rotation: float) -> dict:
    """A video stream as ffprobe reports one carrying a display matrix."""

    text = (
        f"\n00000000: {a:12d} {b:11d} {0:11d}\n"
        f"00000001: {c:12d} {d:11d} {0:11d}\n"
        f"00000002: {0:12d} {0:11d}  1073741824\n"
    )
    return {
        "codec_type": "video",
        "side_data_list": [
            {"side_data_type": "Display Matrix", "displaymatrix": text, "rotation": rotation}
        ],
    }


class UprightTests(unittest.TestCase):
    """Taking rotation over from ffmpeg, and only where that is safe."""

    ONE = 65536

    def test_quarter_turns_map_to_the_filters_ffmpeg_would_insert(self):
        one = self.ONE
        cases = {
            "upside down, as the iPad recording": (display_matrix(-one, 0, 0, -one, -180), ("hflip", "vflip")),
            "rotation 90": (display_matrix(0, -one, one, 0, 90), ("transpose=cclock",)),
            "rotation -90": (display_matrix(0, one, -one, 0, -90), ("transpose=clock",)),
            "identity": (display_matrix(one, 0, 0, one, 0), ()),
        }
        for name, (stream, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(main.upright_filters(stream), expected)

    def test_a_mirror_is_left_to_ffmpeg(self):
        """A plain horizontal flip is reported as rotation -180."""

        one = self.ONE
        self.assertIsNone(main.upright_filters(display_matrix(-one, 0, 0, one, -180)))
        self.assertIsNone(main.upright_filters(display_matrix(0, -one, -one, 0, 90)))

    def test_odd_angles_and_unreadable_matrices_are_left_to_ffmpeg(self):
        one = self.ONE
        self.assertIsNone(main.upright_filters(display_matrix(46341, -46341, 46341, 46341, 45)))
        broken = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}]}
        self.assertIsNone(main.upright_filters(broken))

    def test_no_matrix_needs_nothing(self):
        self.assertEqual(main.upright_filters({"codec_type": "video"}), ())

    def test_autorotate_is_switched_off_only_for_the_graph_that_rotates(self):
        args = main.parse_args(["in.mp4"])
        for manual in (False, True):
            command = main.render_command(
                input_path=Path("in.mp4"),
                output_path=Path("out.mp4"),
                filtergraph_path=Path("graph.txt"),
                encoder="libx264",
                args=args,
                manual_rotation=manual,
            )
            with self.subTest(manual=manual):
                self.assertEqual("-noautorotate" in command, manual)
                if manual:
                    self.assertLess(command.index("-noautorotate"), command.index("-i"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_the_frames_match_ffmpegs_own_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            plain = folder / "plain.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc=size=160x120:rate=5:duration=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(plain)],
                check=True,
            )
            for rotation in ("90", "180", "-90"):
                rotated = folder / f"rot{rotation}.mp4"
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-display_rotation", rotation,
                     "-i", str(plain), "-c", "copy", str(rotated)],
                    check=True,
                )
                media = main.probe_media(rotated)
                self.assertTrue(media.upright, rotation)

                def frames(*command: str) -> list[str]:
                    out = subprocess.run(
                        ["ffmpeg", "-v", "error", *command, "-pix_fmt", "yuv420p",
                         "-c:v", "rawvideo", "-f", "framemd5", "-"],
                        check=True, capture_output=True, text=True,
                    ).stdout
                    return [line for line in out.splitlines() if line.startswith("0,")]

                with self.subTest(rotation=rotation):
                    self.assertEqual(
                        frames("-noautorotate", "-i", str(rotated), "-vf", ",".join(media.upright)),
                        frames("-i", str(rotated)),
                    )


class FrameRateCapTests(unittest.TestCase):
    def tail(self, *, fps: float, speed: float, cap: float, upright=()) -> list[str]:
        args = main.parse_args(["in.mp4", f"--speed={speed}", f"--max-fps={cap}"])
        return main.video_tail_filters(args, video_fps=fps, upright=upright)

    def test_a_60_fps_lecture_is_capped(self):
        self.assertEqual(self.tail(fps=59.97, speed=1.15, cap=30), ["fps=30"])

    def test_the_speed_up_counts(self):
        """25 fps played at 1.25x is 31.25 fps on the way out."""

        self.assertEqual(self.tail(fps=25, speed=1.25, cap=30), ["fps=30"])
        self.assertEqual(self.tail(fps=25, speed=1.15, cap=30), [])

    def test_a_slower_source_is_not_padded_with_repeats(self):
        self.assertEqual(self.tail(fps=24, speed=1.0, cap=30), [])

    def test_zero_keeps_the_source_rate(self):
        self.assertEqual(self.tail(fps=59.97, speed=1.15, cap=0), [])

    def test_rotation_follows_the_cap_so_dropped_frames_are_never_turned(self):
        self.assertEqual(
            self.tail(fps=60, speed=1.0, cap=30, upright=("hflip", "vflip")),
            ["fps=30", "hflip", "vflip"],
        )

    def test_both_graph_modes_end_the_video_chain_with_the_tail(self):
        segments = [main.Segment(0.0, 1.0), main.Segment(2.0, 3.0)]
        for mode in ("select", "concat"):
            args = main.parse_args(["in.mp4", "--max-fps=30", f"--filtergraph-mode={mode}"])
            graph = main.build_filtergraph(segments, args, video_fps=60.0, upright=("hflip", "vflip"))
            with self.subTest(mode=mode):
                self.assertRegex(graph, r",fps=30,hflip,vflip\[vout\]")


class DownloadCacheTests(unittest.TestCase):
    @staticmethod
    def fake_ytdlp(command, capture=False, **kwargs):
        # Mimics yt-dlp: a file already at source.mp4 counts as downloaded and
        # is printed again without fetching the new URL.
        workdir = Path(command[command.index("-P") + 1])
        target = workdir / "source.mp4"
        if not target.exists():
            target.write_text(command[-1])
        return subprocess.CompletedProcess(command, 0, stdout=f"{target}\n", stderr="")

    def fetch(self, url, workdir, fmt="best"):
        with unittest.mock.patch.object(main, "require_command"), unittest.mock.patch.object(
            main, "run_command", side_effect=self.fake_ytdlp
        ) as run, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            path = main.prepare_input(url, workdir, fmt)
        return path, run

    def test_a_reused_workdir_downloads_a_new_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            first, _ = self.fetch("https://example.com/a", workdir)
            self.assertEqual(first.read_text(), "https://example.com/a")
            second, _ = self.fetch("https://example.com/b", workdir)
            self.assertEqual(second.read_text(), "https://example.com/b")

    def test_the_same_url_keeps_its_cached_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            first, _ = self.fetch("https://example.com/a", workdir)
            first.write_text("cached")
            second, _ = self.fetch("https://example.com/a", workdir)
            self.assertEqual(second.read_text(), "cached")

    def test_an_unrecorded_leftover_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "source.webm").write_text("old")
            (workdir / "source.mp4.part").write_text("old")
            path, _ = self.fetch("https://example.com/a", workdir)
            self.assertEqual(path.read_text(), "https://example.com/a")
            self.assertEqual(sorted(p.name for p in workdir.glob("source.*")), ["source.mp4"])


if __name__ == "__main__":
    unittest.main()
