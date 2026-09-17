import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import main


class LectureCutTests(unittest.TestCase):
    def test_parse_silencedetect_log_handles_trailing_silence(self):
        log = """
        [silencedetect @ 0x123] silence_start: 1.25
        [silencedetect @ 0x123] silence_end: 2 | silence_duration: 0.75
        [silencedetect @ 0x123] silence_start: 9.5
        """

        silences = main.parse_silencedetect_log(log, media_duration=10.0)

        self.assertEqual(silences, [main.Silence(1.25, 2.0), main.Silence(9.5, 10.0)])

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

        self.assertIn("select='between(t\\,0\\,1)+between(t\\,2\\,3.5)'", filtergraph)
        self.assertIn(
            "setpts='(between(T\\,0\\,1)*(T-0+0)+between(T\\,2\\,3.5)*(T-2+1))/2.00000000/TB'[vout]",
            filtergraph,
        )
        self.assertIn("aselect='between(t\\,0\\,1)+between(t\\,2\\,3.5)'", filtergraph)
        self.assertIn(
            "asetpts='(between(T\\,0\\,1)*(T-0+0)+between(T\\,2\\,3.5)*(T-2+1))/TB'",
            filtergraph,
        )
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
            "between(T\\,1\\,2.5)*(T-1+0)+between(T\\,4\\,5)*(T-4+1.5)",
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
        hints = " ".join(
            main.audio_analysis_hints(
                self.analysis(channel_imbalance_db=3.3, true_peak_db=-0.7, speech_lufs_median=-48.0)
            )
        )

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
            with self.assertRaises(subprocess.CalledProcessError):
                main.run_command(["false"], capture=True)
        finally:
            main.ACTIVE_CANCEL.reset(token)


if __name__ == "__main__":
    unittest.main()
