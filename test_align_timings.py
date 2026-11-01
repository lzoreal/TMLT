"""Cross-segment ordering: pure-logic tests.

Run: python -m unittest test_align_timings -v
"""

import json
import unittest
from pathlib import Path

import align_timings as at


def cue(index, start, text="text"):
    return {"i": index, "start": start, "end": start + 1.0,
            "speaker": "", "text": text}


def seg(start, end, words, text="text"):
    return {"start": start, "end": end, "speaker": "", "text": text, "words": words}


def word(at_ms, dur_ms=500):
    return [("w", at_ms, dur_ms)]


class ClampSilentSegmentsTest(unittest.TestCase):
    def test_silent_segments_are_clamped_between_nearest_words(self):
        # #192 形态：两句有词，中间两句无词被错误 shift 拉到 3140 秒。
        cues = [cue(0, 2913.0), cue(1, 2972.3), cue(2, 2973.8), cue(3, 2994.0)]
        segments = [
            seg(2913000, 2916000, word(2913100, 2900)),
            seg(3140000, None, []),
            seg(3141400, None, []),
            seg(2937000, 2937500, word(2937000)),
        ]
        bad = at.order_violations(segments, cues)
        self.assertTrue(bad, "回归前必须检出跨段越界")

        discarded = at.repair_segment_order(segments, cues)

        self.assertEqual(discarded, 0)
        self.assertEqual(at.order_violations(segments, cues), [])
        for middle in segments[1:3]:
            self.assertGreaterEqual(middle["start"], 2916000)
            self.assertLessEqual(middle["start"], 2937000)

    def test_silent_segment_before_any_word_is_clamped_to_next_word(self):
        cues = [cue(0, 0.0), cue(1, 30.0)]
        segments = [seg(400000, None, []), seg(30000, 30500, word(30000))]
        at.repair_segment_order(segments, cues)
        self.assertLessEqual(segments[0]["start"], 30000)
        self.assertGreaterEqual(segments[0]["start"], 0)


class RepairWordOverlapTest(unittest.TestCase):
    def test_discards_the_side_further_from_cue_truth(self):
        # 第三句被锚到 200 秒，前后句都在 300-340 秒；应丢第三句的词。
        cues = [cue(i, 300.0 + i * 10) for i in range(5)]
        segments = [
            seg(300000, 310000, word(300000, 10000)),
            seg(310000, 320000, word(310000, 10000)),
            seg(320000, 330000, word(200000, 10000)),  # 错误锚点
            seg(330000, 340000, word(330000, 10000)),
            seg(340000, 350000, word(340000, 10000)),
        ]
        self.assertTrue(at.order_violations(segments, cues))

        discarded = at.repair_segment_order(segments, cues)

        self.assertEqual(discarded, 1)
        self.assertEqual(segments[2]["words"], [])
        self.assertIsNone(segments[2]["end"])
        self.assertEqual(at.order_violations(segments, cues), [])

    def test_clean_timeline_is_untouched(self):
        cues = [cue(i, 10.0 * i) for i in range(3)]
        segments = [
            seg(0, 9000, word(100, 8900)),
            seg(10000, 19000, word(10000, 9000)),
            seg(20000, 29000, word(20000, 9000)),
        ]
        before = json.dumps(segments, sort_keys=True)
        discarded = at.repair_segment_order(segments, cues)
        self.assertEqual(discarded, 0)
        self.assertEqual(json.dumps(segments, sort_keys=True), before)
        self.assertEqual(at.order_violations(segments, cues), [])


class RealEpisodeRegressionTest(unittest.TestCase):
    """#192 的 sidecar 是 2026-11 审计里 ASR 证实的错位样本。"""

    def test_192_silent_cues_move_between_neighbours(self):
        path = Path(__file__).parent / "docs" / "timings" / "192.json"
        if not path.exists():
            self.skipTest("sidecar 不在仓库")
        document = json.loads(path.read_text(encoding="utf-8"))
        segments = json.loads(json.dumps(document["segments"]))
        cues = at.parse_vtt(Path(__file__).parent / "docs" / "transcripts" / "192.vtt")

        if at.order_violations(segments, cues):
            at.repair_segment_order(segments, cues)

        self.assertEqual(at.order_violations(segments, cues), [])
        self.assertLessEqual(segments[241]["start"], 2938000)
        self.assertLessEqual(segments[242]["start"], 2938000)


if __name__ == "__main__":
    unittest.main()
