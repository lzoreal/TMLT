"""中文 feed：多标签输入下只保留改写后的中文 VTT。

Run: python -m unittest test_generate_zh_feed -v
"""

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import generate_zh_feed as zh

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"


def feed(tags):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="{PODCAST_NS}">
<channel>
<title>English</title>
<language>en-us</language>
<description>English feed</description>
<item>
<title>420</title>
{tags}
</item>
</channel>
</rss>"""


class BuildFeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = Path(self.tmp.name)
        (self.docs / "transcripts").mkdir()
        (self.docs / "transcripts/zh").mkdir()
        (self.docs / "transcripts/420.vtt").write_text(VTT, encoding="utf-8")
        (self.docs / "transcripts/zh/420.vtt").write_text(VTT, encoding="utf-8")
        (self.docs / "translations.json").write_text(
            json.dumps({"420": {"translated": True}}), encoding="utf-8")

    def build(self, tags):
        (self.docs / "podcast.xml").write_text(feed(tags), encoding="utf-8")
        kept, removed = zh.build_feed(self.docs, "https://example.test")
        root = ET.parse(self.docs / "podcast-zh.xml").getroot()
        return kept, removed, root

    def test_json_and_bilingual_vtt_leave_one_chinese_tag(self):
        kept, removed, root = self.build(
            '<podcast:transcript url="https://example.test/timings/420.json" '
            'type="application/json" language="en"/>'
            '<podcast:transcript url="https://example.test/transcripts/zh/420.vtt" '
            'type="text/vtt" language="zh-CN"/>')
        self.assertEqual((kept, removed), (1, 0))
        tags = root.findall(f".//{{{PODCAST_NS}}}transcript")
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].get("type"), "text/vtt")
        self.assertEqual(tags[0].get("language"), "zh-CN")
        self.assertEqual(tags[0].get("url"),
                         "https://example.test/transcripts/zh/420.vtt")

    def test_english_fallback_tag_is_rewritten_to_chinese(self):
        kept, _, root = self.build(
            '<podcast:transcript url="https://example.test/timings/420.json" '
            'type="application/json" language="en"/>'
            '<podcast:transcript url="https://example.test/transcripts/420.vtt" '
            'type="text/vtt" language="en"/>')
        self.assertEqual(kept, 1)
        (tag,) = root.findall(f".//{{{PODCAST_NS}}}transcript")
        self.assertEqual(tag.get("url"),
                         "https://example.test/transcripts/zh/420.vtt")
        self.assertEqual(tag.get("language"), "zh-CN")

    def test_untranslated_episode_is_removed(self):
        (self.docs / "translations.json").write_text("{}", encoding="utf-8")
        kept, removed, root = self.build(
            '<podcast:transcript url="https://example.test/transcripts/420.vtt" '
            'type="text/vtt" language="en"/>')
        self.assertEqual((kept, removed), (0, 1))
        self.assertEqual(root.findall(".//item"), [])


if __name__ == "__main__":
    unittest.main()
