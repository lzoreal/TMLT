"""Cross-segment ordering: pure-logic tests.

Run: python -m unittest test_align_timings -v
"""

import json
import tempfile
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


class CompressedRunTest(unittest.TestCase):
    def test_punctuation_only_word_does_not_crash(self):
        # #528 的 ValueError: 8 is not in list：run 里混了没有字母/数字的词。
        cues = [{"i": 0, "start": 9.0, "text": "-- alpha beta gamma delta"}]
        results = {0: {"words": [
            ("--", 10.00, 10.02), ("alpha", 10.03, 10.05),
            ("beta", 10.06, 10.08), ("gamma", 10.09, 10.11), ("delta", 12.0, 12.2),
        ]}}
        tokens = [("alpha", 10.03), ("beta", 10.06), ("gamma", 10.09)]
        self.assertEqual(at.repair_compressed_runs(cues, results, tokens), 0)


class MarkAttemptTest(unittest.TestCase):
    def test_keeps_complete_when_rerun_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.json"
            path.write_text(json.dumps({
                "version": 1, "episode": 1, "audio_seconds": 10.0,
                "complete": True, "attempts": 1,
                "segments": [seg(0, 1000, word(100))],
            }), encoding="utf-8")
            at.mark_attempt(path, 1, 2, "error:ValueError")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(document["complete"])
            self.assertEqual(document["attempts"], 2)
            self.assertNotIn("rejected", document)
            self.assertEqual(len(document["segments"]), 1)

    def test_marks_incomplete_when_no_prior_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2.json"
            path.write_text(json.dumps({
                "version": 1, "episode": 2, "audio_seconds": 10.0,
                "complete": False, "attempts": 1, "segments": [],
            }), encoding="utf-8")
            at.mark_attempt(path, 2, 2, "error:ValueError")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(document["complete"])
            self.assertEqual(document["rejected"], "error:ValueError")


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


class LoadSourcesTest(unittest.TestCase):
    def test_prefers_the_vtt_transcript_over_the_json_sidecar(self):
        feed = """<rss><channel><item>
<title>1: test</title>
<podcast:transcript
url="https://example.test/timings/1.json"
type="application/json"/>
<podcast:transcript
url="https://example.test/transcripts/zh/1.vtt"
type="text/vtt"/>
<enclosure url="https://example.test/archive/1.mp3" type="audio/mpeg"/>
<pubDate>Fri, 16 Oct 2026 11:21:15 -0400</pubDate>
</item></channel></rss>"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "podcast.xml"
            path.write_text(feed, encoding="utf-8")
            sources = at.load_sources(path)
        self.assertEqual(list(sources), [1])
        self.assertEqual(sources[1]["audio"], "https://example.test/archive/1.mp3")
        self.assertEqual(sources[1]["published"], "Fri, 16 Oct 2026 11:21:15 -0400")


if __name__ == "__main__":
    unittest.main()
