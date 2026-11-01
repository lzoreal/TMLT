"""Pages 上传范围：已被中译取代的英文 VTT 不发布，兜底稿保留。

Run: python -m unittest test_stage_pages -v
"""

import tempfile
import unittest
from pathlib import Path

import stage_pages

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"


class StagePagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = Path(self.tmp.name) / "docs"
        self.out = Path(self.tmp.name) / "pages"
        (self.docs / "transcripts/zh").mkdir(parents=True)
        (self.docs / "timings").mkdir()
        (self.docs / "transcripts/420.vtt").write_text(VTT, encoding="utf-8")
        (self.docs / "transcripts/900.vtt").write_text(VTT, encoding="utf-8")
        (self.docs / "transcripts/zh/420.vtt").write_text(VTT, encoding="utf-8")
        (self.docs / "timings/420.json").write_text("{}", encoding="utf-8")
        (self.docs / "podcast.xml").write_text("<rss/>", encoding="utf-8")

    def test_superseded_english_vtt_is_not_staged(self):
        removed = stage_pages.stage(self.docs, self.out)
        self.assertEqual(removed, 1)
        self.assertFalse((self.out / "transcripts/420.vtt").exists())
        self.assertTrue((self.out / "transcripts/zh/420.vtt").exists())
        self.assertTrue((self.out / "transcripts/900.vtt").exists())
        self.assertTrue((self.out / "timings/420.json").exists())
        self.assertTrue((self.out / "podcast.xml").exists())

    def test_staging_twice_replaces_the_old_output(self):
        stage_pages.stage(self.docs, self.out)
        (self.docs / "transcripts/zh/900.vtt").write_text(VTT, encoding="utf-8")
        removed = stage_pages.stage(self.docs, self.out)
        self.assertEqual(removed, 2)
        self.assertFalse((self.out / "transcripts/900.vtt").exists())


if __name__ == "__main__":
    unittest.main()
