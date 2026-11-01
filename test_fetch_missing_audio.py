"""存档音频候选筛选的纯逻辑测试。

Run: python -m unittest test_fetch_missing_audio -v
"""

import unittest

import fetch_missing_audio as fa


class PickCdxTest(unittest.TestCase):
    HEADER = ["urlkey", "timestamp", "original", "mimetype",
              "statuscode", "digest", "length"]

    def row(self, ts, original, mimetype="audio/mpeg", status="200",
            length="40000000"):
        return ["k", ts, original, mimetype, status, "d", length]

    def test_keeps_only_real_audio(self):
        rows = [
            self.HEADER,
            self.row("20120301", "http://a/454.mp3"),
            self.row("20120302", "http://b/454.mp3", mimetype="text/html"),
            self.row("20120303", "http://c/454.mp3", status="404"),
            self.row("20120304", "http://d/454.mp3", length="242"),
            self.row("20120305", "http://e/454.mp3",
                     mimetype="application/octet-stream"),
        ]
        self.assertEqual(
            fa.pick_cdx_entries(rows),
            [("20120305", "http://e/454.mp3"), ("20120301", "http://a/454.mp3")])

    def test_newest_first_and_deduplicated(self):
        rows = [
            self.row("20120101", "http://a/454.mp3"),
            self.row("20130101", "http://a/454.mp3"),
            self.row("20140101", "http://b/454.mp3"),
        ]
        self.assertEqual(
            fa.pick_cdx_entries(rows),
            [("20140101", "http://b/454.mp3"), ("20130101", "http://a/454.mp3")])

    def test_bad_length_is_skipped(self):
        rows = [self.row("20120101", "http://a/454.mp3", length="n/a")]
        self.assertEqual(fa.pick_cdx_entries(rows), [])


class PickArchiveFilesTest(unittest.TestCase):
    def test_duration_window_excludes_promos_and_fragments(self):
        files = [
            {"name": "454.mp3", "length": "2400", "url": "/download/454.mp3"},
            {"name": "promo.mp3", "length": "120", "url": "/download/promo.mp3"},
            {"name": "full.mp3", "length": "9999", "url": "/download/full.mp3"},
            {"name": "notes.txt", "length": "2400", "url": "/download/notes.txt"},
            {"name": "odd.mp3", "length": "?", "url": "/download/odd.mp3"},
        ]
        self.assertEqual(fa.pick_archive_files(files),
                         [("454.mp3", "/download/454.mp3")])


class ArchiveItemCandidatesTest(unittest.TestCase):
    def test_title_hits_rank_first(self):
        items = [
            ("item-a", "some radio show", ""),
            ("item-b", "Mr Daisey Apple Factory", ""),
            ("item-c", "apple pie", ""),
        ]
        ranked = fa.archive_item_candidates(items, ["daisey", "apple", "factory"])
        self.assertEqual([identifier for identifier, _ in ranked], ["item-b"])

    def test_this_american_life_mention_wins(self):
        items = [
            ("plain", "Daisey Apple Factory monologue", ""),
            ("tal", "Mr Daisey and the Apple Factory",
             "This American Life episode 454"),
        ]
        ranked = fa.archive_item_candidates(items, ["daisey", "apple", "factory"])
        self.assertEqual(ranked[0][0], "tal")


if __name__ == "__main__":
    unittest.main()
