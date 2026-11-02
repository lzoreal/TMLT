"""Targeted repair of wordless cues: pure-logic tests.

Run: python -m unittest test_repair_wordless -v
"""

import copy
import unittest
from unittest import mock

import align_timings as at


def cue(index, start, end=None, text="hello there"):
    return {"i": index, "start": start, "end": start + 1.0 if end is None else end,
            "speaker": "", "text": text}


def seg(start, end, words, text="hello there"):
    return {"start": start, "end": end, "speaker": "", "text": text, "words": words}


def word(at_ms, dur_ms=500, text="w"):
    return [text, at_ms, dur_ms]


class RepairTargetTest(unittest.TestCase):
    def test_skips_stage_tags_and_single_tokens(self):
        self.assertFalse(at.repair_target("[MUSIC - DESTINATION MOON]"))
        self.assertFalse(at.repair_target("[LAUGHTER]."))
        self.assertFalse(at.repair_target("Yes."))
        self.assertFalse(at.repair_target("(HOST) IRA GLASS: What?"))
        self.assertTrue(at.repair_target("(HOST) IRA GLASS: What happened?"))
        self.assertTrue(at.repair_target("[LAUGHTER] You're kidding me."))

    def test_singing_is_opt_in(self):
        self.assertFalse(at.repair_target("(SINGING) No, no, no, no."))
        self.assertTrue(at.repair_target("(SINGING) No, no, no, no.", include_sung=True))

    def test_interior_stage_tag_is_not_a_target(self):
        self.assertFalse(at.repair_target("Now you don't really say [GUNFIRE] that much."))

    def test_only_wordless_segments_are_targets(self):
        cues = [cue(0, 0.0), cue(1, 10.0), cue(2, 20.0)]
        segments = [seg(0, 1000, [word(0)]), seg(10000, None, []), seg(20000, 21000, [word(20000)])]
        self.assertEqual(at.repair_targets(cues, segments), [1])


class RepairWindowTest(unittest.TestCase):
    def test_consecutive_targets_share_a_neighbour_bounded_window(self):
        cues = [cue(0, 10.0), cue(1, 20.0), cue(2, 30.0), cue(3, 31.0)]
        results = {
            0: {"words": [("a", 10.0, 12.0)]},
            3: {"words": [("b", 31.0, 32.0)]},
        }
        windows = at.repair_windows([1, 2], cues, results, audio_seconds=100.0)
        self.assertEqual(len(windows), 1)
        lo, hi, group = windows[0]
        self.assertAlmostEqual(lo, 11.5)
        self.assertAlmostEqual(hi, 31.5)
        self.assertEqual([c["i"] for c in group], [1, 2])

    def test_out_of_order_neighbours_produce_no_window(self):
        # 前后实测词互相压住时没有可信区间，宁可不动也不能切出负长度窗口。
        cues = [cue(0, 0.0), cue(1, 20.0), cue(2, 30.0)]
        results = {
            0: {"words": [("a", 0.0, 32.0)]},
            2: {"words": [("b", 31.0, 32.0)]},
        }
        self.assertEqual(at.repair_windows([1], cues, results, audio_seconds=100.0), [])

    def test_single_target_uses_its_own_span_not_the_neighbours(self):
        # 单句不能借前后邻居撑成大窗口：后文常有重复句，贴过去就是错时间。
        cues = [cue(0, 10.0), cue(1, 20.0), cue(2, 31.0)]
        results = {
            0: {"words": [("a", 10.0, 12.0)]},
            2: {"words": [("b", 31.0, 32.0)]},
        }
        windows = at.repair_windows([1], cues, results, audio_seconds=100.0)
        self.assertEqual(len(windows), 1)
        lo, hi, group = windows[0]
        self.assertGreaterEqual(lo, 20.0 - at.PAD)
        self.assertLess(hi, 31.0)
        self.assertEqual([c["i"] for c in group], [1])

    def test_music_cue_caps_the_window(self):
        cues = [cue(0, 10.0), cue(1, 20.0), cue(2, 25.0, 30.0, "[MUSIC]"),
                cue(3, 40.0)]
        cues[2] = dict(cues[2], text="[MUSIC]", end=30.0)
        results = {
            0: {"words": [("a", 10.0, 12.0)]},
            3: {"words": [("b", 40.0, 41.0)]},
        }
        windows = at.repair_windows([1], cues, results, audio_seconds=100.0)
        self.assertEqual(len(windows), 1)
        self.assertLessEqual(windows[0][1], 25.0)

    def test_without_neighbours_uses_cue_estimate(self):
        cues = [cue(0, 10.0)]
        windows = at.repair_windows([0], cues, {}, audio_seconds=100.0)
        lo, hi, group = windows[0]
        self.assertAlmostEqual(lo, 10.0 - at.PAD)
        self.assertAlmostEqual(hi, 10.0 + at.cue_span(cues[0]) + at.TAIL_PAD)
        self.assertEqual(len(group), 1)

    def test_wide_gap_falls_back_to_one_cue_per_window(self):
        cues = [cue(0, 0.0), cue(1, 200.0), cue(2, 400.0)]
        results = {
            0: {"words": [("a", 0.0, 12.0)]},
            2: {"words": [("b", 400.0, 401.0)]},
        }
        windows = at.repair_windows([1], cues, results, audio_seconds=500.0)
        self.assertEqual(len(windows), 1)
        self.assertEqual([c["i"] for c in windows[0][2]], [1])
        self.assertGreaterEqual(windows[0][0], 11.5)
        self.assertLessEqual(windows[0][1], 400.5)


class RepairGateTest(unittest.TestCase):
    def test_clean_words_pass(self):
        c = cue(0, 10.0)
        words = [("hello", 10.1, 10.6), ("there", 10.6, 11.2)]
        self.assertEqual(at.repair_reject_reason(c, words, {}, 0, 0.0, 100.0), "")

    def test_rejects_early_late_short_and_mismatched(self):
        c = cue(0, 10.0)
        self.assertEqual(
            at.repair_reject_reason(c, [("hello", 1.0, 1.5), ("there", 1.5, 2.0)], {}, 0, 0.0, 100.0),
            "before-start")
        self.assertEqual(
            at.repair_reject_reason(c, [("hello", 10.1, 10.15), ("there", 10.15, 10.2)], {}, 0, 0.0, 100.0),
            "too-short")
        self.assertEqual(
            at.repair_reject_reason(c, [("hello", 10.1, 10.6)], {}, 0, 0.0, 100.0),
            "word-mismatch")

    def test_rejects_word_starting_inside_previous_cue(self):
        c = cue(1, 10.0)
        results = {0: {"words": [("a", 9.0, 12.0)]}}
        words = [("hello", 11.5, 12.0), ("there", 12.0, 12.5)]
        self.assertEqual(at.repair_reject_reason(c, words, results, 1, 0.0, 100.0),
                         "before-prev")

    def test_rejects_words_with_a_huge_interior_gap(self):
        c = cue(0, 10.0, 50.0)
        words = [("hello", 10.1, 10.6), ("there", 40.0, 40.5)]
        self.assertEqual(
            at.repair_reject_reason(c, words, {}, 0, 0.0, 100.0), "gap")

    def test_rejects_words_outside_audio(self):
        c = cue(0, 10.0)
        words = [("hello", 10.1, 10.6), ("there", 10.6, 101.0)]
        self.assertEqual(at.repair_reject_reason(c, words, {}, 0, 0.0, 100.0),
                         "outside-audio")


class SnapSilentTokensTest(unittest.TestCase):
    def test_distant_trailing_punctuation_snaps_to_previous_word(self):
        words = [("Blue", 10.0, 10.2), ("Sky", 10.2, 10.4),
                 ("Dream", 10.4, 10.8), (".", 60.0, 60.5)]
        snapped = at.snap_silent_tokens(words)
        self.assertEqual(snapped[-1], (".", 10.8, 10.8))

    def test_adjacent_punctuation_is_untouched(self):
        words = [("Dream", 10.4, 10.8), (".", 10.8, 10.9)]
        self.assertEqual(at.snap_silent_tokens(words), words)


class BarrierTest(unittest.TestCase):
    def test_words_crossing_a_music_cue_are_rejected(self):
        cues = [cue(0, 10.0, 20.0), cue(1, 25.0, 35.0, "[MUSIC]")]
        words = [("hello", 10.5, 11.0), ("there", 26.0, 26.5)]
        self.assertTrue(at.crosses_repair_barrier(cues, 0, words))

    def test_short_laughter_is_not_a_barrier(self):
        cues = [cue(0, 10.0, 20.0), cue(1, 12.0, 13.0, "[LAUGHTER]")]
        words = [("hello", 10.5, 11.0), ("there", 13.5, 14.0)]
        self.assertFalse(at.crosses_repair_barrier(cues, 0, words))


class ApplyRepairTest(unittest.TestCase):
    def test_only_the_target_segment_changes(self):
        segments = [
            seg(0, 1000, [word(100)]),
            seg(20000, None, [], "hello there"),
            seg(30000, 31000, [word(30000)]),
        ]
        before = copy.deepcopy(segments)
        words = [("hello", 20.5, 21.0), ("there", 21.0, 21.4)]
        at.apply_repair(segments, 1, words, 100.0)
        self.assertEqual(segments[1]["words"],
                         [["hello", 20500, 500], ["there", 21000, 400]])
        self.assertEqual(segments[1]["start"], 20000)
        self.assertEqual(segments[1]["end"], 21400)
        self.assertEqual(segments[0], before[0])
        self.assertEqual(segments[2], before[2])

    def test_start_is_pulled_forward_to_the_first_word(self):
        segments = [seg(50000, None, [])]
        at.apply_repair(segments, 0, [("hello", 40.0, 40.5), ("there", 40.5, 41.0)], 100.0)
        self.assertEqual(segments[0]["start"], 40000)
        self.assertEqual(segments[0]["end"], 41000)


class RepairSegmentsTest(unittest.TestCase):
    def test_gate_controls_write_back(self):
        cues = [cue(0, 0.0, 1.0, "[MUSIC]"), cue(1, 10.0, 20.0),
                cue(2, 25.0, 30.0, "good bye now")]
        segments = [
            seg(0, 1000, [word(0)], "[MUSIC]"),
            seg(10000, None, []),
            seg(25000, 30000, [word(25000, text="good")], "good bye now"),
        ]
        segments[2]["words"] = [["good", 25000, 400], ["bye", 25400, 400], ["now", 25800, 400]]
        with mock.patch.object(at, "align_window",
                               return_value={1: {"words": [("hello", 10.5, 11.0),
                                                          ("there", 11.0, 11.5)]}}):
            repaired, attempted, reasons, _ = at.repair_segments(
                segments, cues, model=None, audio=None, audio_seconds=100.0)
        self.assertEqual((repaired, attempted), (1, 1))
        self.assertEqual(dict(reasons), {})
        self.assertEqual(segments[1]["words"],
                         [["hello", 10500, 500], ["there", 11000, 500]])
        self.assertEqual(segments[1]["end"], 11500)

    def test_leading_word_stretched_over_a_pause_is_normalized(self):
        cues = [cue(0, 2952.24, 2952.74, "[MUSIC]"), cue(1, 2952.74, 2966.0, "OK. OK.")]
        segments = [seg(2952240, 2952740, [word(2952240)], "[MUSIC]"),
                    seg(2952740, None, [], "OK. OK.")]
        with mock.patch.object(at, "align_window",
                               return_value={1: {"words": [("OK.", 2952.74, 2955.10),
                                                          ("OK.", 2964.38, 2965.16)]}}):
            repaired, _, _, _ = at.repair_segments(
                segments, cues, model=None, audio=None, audio_seconds=4000.0)
        self.assertEqual(repaired, 1)
        first, second = segments[1]["words"][0], segments[1]["words"][1]
        self.assertLessEqual(first[2], 1200)
        self.assertLessEqual(first[1] + first[2], second[1])

    def test_failed_gate_leaves_segment_wordless(self):
        cues = [cue(0, 0.0, 1.0, "[MUSIC]"), cue(1, 10.0, 20.0)]
        segments = [seg(0, 1000, [word(0)], "[MUSIC]"), seg(10000, None, [])]
        with mock.patch.object(at, "align_window",
                               return_value={1: {"words": [("hello", 1.0, 1.5),
                                                          ("there", 1.5, 2.0)]}}):
            repaired, attempted, reasons, _ = at.repair_segments(
                segments, cues, model=None, audio=None, audio_seconds=100.0)
        self.assertEqual((repaired, attempted), (0, 1))
        self.assertEqual(segments[1]["words"], [])
        self.assertEqual(segments[1]["end"], None)
        self.assertIn("before-start", reasons)


if __name__ == "__main__":
    unittest.main()
