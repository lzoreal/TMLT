"""听写结果 -> VTT / sidecar 的纯逻辑测试。

Run: python -m unittest test_transcribe_episodes -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import align_timings as at
import transcribe_episodes as tx


def raw_segment(start, end, text, words):
    return SimpleNamespace(
        start=start, end=end, text=text,
        words=[SimpleNamespace(word=w, start=s, end=e) for w, s, e in words],
    )


class BuildSegmentsTest(unittest.TestCase):
    def test_words_become_milliseconds_with_duration(self):
        raw = [raw_segment(1.5, 3.0, "Hello  there.",
                           [("Hello", 1.5, 2.0), ("there.", 2.0, 2.9)])]
        (segment,) = tx.build_segments(raw, 100.0)
        self.assertEqual(segment["start"], 1500)
        self.assertEqual(segment["end"], 3000)
        self.assertEqual(segment["text"], "Hello there.")
        self.assertEqual(segment["words"],
                         [["Hello", 1500, 500], ["there.", 2000, 900]])

    def test_timestamps_are_clamped_to_audio_length(self):
        raw = [raw_segment(-0.4, 70.0, "tail", [("tail", 69.9, 70.4)])]
        (segment,) = tx.build_segments(raw, 60.0)
        self.assertEqual(segment["start"], 0)
        self.assertEqual(segment["end"], 60000)
        self.assertEqual(segment["words"], [["tail", 60000, 0]])

    def test_empty_text_and_blank_words_are_dropped(self):
        raw = [raw_segment(0.0, 1.0, "   ", [("x", 0.0, 0.5)]),
               raw_segment(1.0, 2.0, "ok", [("  ", 1.0, 1.2), ("ok", 1.2, 2.0)])]
        segments = tx.build_segments(raw, 10.0)
        self.assertEqual([s["text"] for s in segments], ["ok"])
        self.assertEqual(segments[0]["words"], [["ok", 1200, 800]])

    def test_never_negative_duration(self):
        raw = [raw_segment(2.0, 1.0, "odd", [("odd", 2.0, 1.5)])]
        (segment,) = tx.build_segments(raw, 10.0)
        self.assertEqual(segment["end"], 2000)
        self.assertEqual(segment["words"], [["odd", 2000, 0]])


class MergeFragmentsTest(unittest.TestCase):
    def segment(self, start, end, text, words=None):
        return {"start": start, "end": end, "speaker": "", "text": text,
                "words": words or []}

    def test_comma_split_is_joined_onto_the_sentence(self):
        segments = [
            self.segment(0, 1000, "the kind of differences", [["a", 0, 100]]),
            self.segment(1000, 2000, "strangers point out.", [["b", 1000, 100]]),
        ]
        (merged,) = tx.merge_fragments(segments)
        self.assertEqual(merged["text"], "the kind of differences strangers point out.")
        self.assertEqual(merged["start"], 0)
        self.assertEqual(merged["end"], 2000)
        self.assertEqual(merged["words"], [["a", 0, 100], ["b", 1000, 100]])

    def test_segment_ending_a_sentence_stays_separate(self):
        segments = [self.segment(0, 1000, "First one."),
                    self.segment(1000, 2000, "Second one.")]
        self.assertEqual(len(tx.merge_fragments(segments)), 2)

    def test_merging_stops_at_the_group_limits(self):
        segments = [
            self.segment(0, 8000, "a" * 500),
            self.segment(8000, 16000, "b" * 500),
        ]
        self.assertEqual(len(tx.merge_fragments(segments, max_chars=900)), 2)
        self.assertEqual(len(tx.merge_fragments(segments, max_span=5.0)), 2)

    def test_input_segments_are_not_mutated(self):
        segments = [self.segment(0, 1000, "one", [["a", 0, 1]]),
                    self.segment(1000, 2000, "two", [["b", 1, 1]])]
        tx.merge_fragments(segments)
        self.assertEqual(segments[0]["text"], "one")
        self.assertEqual(segments[0]["end"], 1000)
        self.assertEqual(len(segments[0]["words"]), 1)


class AudioPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name)

    def test_repo_copy_wins(self):
        (self.local / "454.mp3").write_bytes(b"0" * (2 << 20))
        plan = tx.audio_plan(454, {}, {}, local_dir=self.local)
        self.assertEqual(plan[0], "local")

    def test_manifest_entry_without_sha_still_used(self):
        manifest = {495: {"status": 200, "url": "http://x/495.mp3", "size": 123,
                          "plausible_master": True}}
        plan = tx.audio_plan(495, {}, manifest, local_dir=self.local)
        self.assertEqual(plan, ("download", "http://x/495.mp3", 123, ""))

    def test_dead_manifest_entry_falls_back_to_roster(self):
        manifest = {495: {"status": 200, "url": "http://x/495.mp3", "size": 242,
                          "plausible_master": False}}
        roster = {"495": {"resolved_audio": "http://y/495.mp3"}}
        plan = tx.audio_plan(495, roster, manifest, local_dir=self.local)
        self.assertEqual(plan, ("download", "http://y/495.mp3", 0, ""))

    def test_nothing_available_means_none(self):
        self.assertIsNone(tx.audio_plan(495, {}, {}, local_dir=self.local))


class RenderVttTest(unittest.TestCase):
    def test_roundtrips_through_the_align_parser(self):
        segments = [
            {"start": 0, "end": 5220, "speaker": "",
             "text": "Hi, everybody.", "words": []},
            {"start": 5480, "end": 7870, "speaker": "",
             "text": "It's dark & quiet.", "words": []},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "497.vtt"
            path.write_text(tx.render_vtt(segments), encoding="utf-8")
            cues = at.parse_vtt(path)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 0.0)
        self.assertAlmostEqual(cues[0]["end"], 5.22)
        self.assertAlmostEqual(cues[1]["start"], 5.48)
        self.assertAlmostEqual(cues[1]["end"], 7.87)
        self.assertEqual(cues[1]["text"], "It's dark &amp; quiet.")

    def test_markup_characters_cannot_break_the_cue(self):
        segments = [{"start": 0, "end": 1000, "speaker": "",
                     "text": "a <v Bob> --> b", "words": []}]
        vtt = tx.render_vtt(segments)
        payload = vtt.splitlines()[3]
        self.assertNotIn("-->", payload)
        self.assertIn("&lt;", payload)

    def test_zero_length_segment_gets_a_visible_cue(self):
        segments = [{"start": 1000, "end": 1000, "speaker": "",
                     "text": "huh", "words": []}]
        (cue,) = at.parse_vtt(self._write(tx.render_vtt(segments)))
        self.assertEqual(cue["end"], cue["start"] + 0.5)

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False,
                                             encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)


class WriteEpisodeTest(unittest.TestCase):
    def test_sidecar_is_complete_and_points_at_the_vtt(self):
        segments = [{"start": 0, "end": 1000, "speaker": "",
                     "text": "hello", "words": [["hello", 0, 900]]}]
        with tempfile.TemporaryDirectory() as tmp:
            vtt_path = Path(tmp) / "transcripts" / "497.vtt"
            out_path = Path(tmp) / "timings" / "497.json"
            tx.write_episode(497, segments, 3600.0, vtt_path, out_path)
            document = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(document["vtt"], "transcripts/497.vtt")
            self.assertEqual(document["audio_seconds"], 3600.0)
            self.assertEqual(document["segments"], segments)
            self.assertTrue(at.is_done(out_path))

    def test_written_vtt_matches_the_sidecar_order(self):
        # 听写结果不走 align_timings 的修复流程，VTT 与 sidecar 必须自己保持一致。
        raw = [
            raw_segment(5.0, 9.0, "Hello there.",
                        [("Hello", 5.0, 5.6), ("there.", 5.6, 6.1)]),
            raw_segment(12.0, 15.0, "Second line.",
                        [("Second", 12.0, 12.6), ("line.", 12.6, 13.0)]),
        ]
        segments = tx.merge_fragments(tx.build_segments(raw, 60.0))
        with tempfile.TemporaryDirectory() as tmp:
            vtt_path = Path(tmp) / "transcripts" / "420.vtt"
            out_path = Path(tmp) / "timings" / "420.json"
            tx.write_episode(420, segments, 60.0, vtt_path, out_path)
            cues = at.parse_vtt(vtt_path)
            document = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cues), len(document["segments"]))
        self.assertEqual(at.order_violations(document["segments"], cues, 60000), [])


if __name__ == "__main__":
    unittest.main()
