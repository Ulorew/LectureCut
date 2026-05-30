import argparse
import unittest

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

    def test_build_filtergraph_contains_expected_labels(self):
        args = argparse.Namespace(
            speed=1.25,
            denoise="afftdn",
            no_normalize=False,
            loudnorm_i=-16.0,
            loudnorm_tp=-1.5,
            loudnorm_lra=11.0,
            audio_sample_rate=48000,
            filtergraph_mode="concat",
        )

        filtergraph = main.build_filtergraph(
            [main.Segment(0.0, 1.0), main.Segment(2.0, 3.5)],
            args,
        )

        self.assertIn("[0:v]trim=start=0:end=1,setpts=PTS-STARTPTS[v0]", filtergraph)
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcat][acat]", filtergraph)
        self.assertIn("[vcat]setpts=PTS/1.25000000[vout]", filtergraph)
        self.assertIn(
            "[acat]afftdn,atempo=1.250000,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]",
            filtergraph,
        )

    def test_build_select_filtergraph_uses_single_linear_pass(self):
        args = argparse.Namespace(
            speed=2.0,
            denoise="none",
            no_normalize=True,
            loudnorm_i=-16.0,
            loudnorm_tp=-1.5,
            loudnorm_lra=11.0,
            audio_sample_rate=48000,
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
        self.assertIn("atempo=2.000000,aresample=48000[aout]", filtergraph)

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
        with self.assertRaises(SystemExit):
            main.validate_preview_denoise_modes(["none", "rnnoise"])

    def test_default_cut_and_preview_settings_are_opinionated_baseline(self):
        args = main.parse_args(["input.mp4"])

        self.assertEqual(args.silence_threshold, "-35dB")
        self.assertEqual(args.denoise, "afftdn")
        self.assertEqual(args.preview_thresholds, "-35dB")
        self.assertEqual(args.preview_denoise, "afftdn")


if __name__ == "__main__":
    unittest.main()
