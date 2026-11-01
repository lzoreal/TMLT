"""Audio cold-backup core: pure-logic tests.

Run: python -m unittest test_audio_backup -v
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import requests

import audio_backup as ab


SAMPLE_FEED = """<rss>
<channel>
<item>
<title>513: 129 Cars</title>
<link>https://www.thisamericanlife.org/513/129-cars</link>
<enclosure
url="https://www.thisamericanlife.org/sites/default/files/audio/upload/archive/513_0.mp3"
type="audio/mpeg"/>
</item>
<item>
<title>1: Somewhere in the Arabian Sea</title>
<enclosure
url="https://www.thisamericanlife.org/sites/default/files/audio/1/f4vcq5NjNe6sLhpHCrnrYIwy4MDsnHERXNinV2Es-cg/1.mp3"
type="audio/mpeg"/>
</item>
<item>
<title>489: Good Name</title>
<enclosure
url="https://www.thisamericanlife.org/sites/default/files/audio/upload/promos/489.mp3"
type="audio/mpeg"/>
</item>
<item>
<title>900: Still Making News</title>
</item>
</channel>
</rss>"""


def probes():
    return [
        ab.Probe(episode=513, url="https://x/513_0.mp3", status=200, size=1000, etag=None),
        ab.Probe(episode=1, url="https://x/1.mp3", status=200, size=2000, etag=None),
        ab.Probe(episode=9, url="https://x/9.mp3", status=404, size=0, etag=None),
    ]


class ParseFeedTest(unittest.TestCase):
    def test_episode_numbers_come_from_the_title_not_the_url(self):
        self.assertEqual([r.episode for r in ab.parse_feed(SAMPLE_FEED)], [513, 1, 489])

    def test_drupal_duplicate_suffix_stays_intact_in_the_url(self):
        refs = {r.episode: r.url for r in ab.parse_feed(SAMPLE_FEED)}
        self.assertTrue(refs[513].endswith("/audio/upload/archive/513_0.mp3"))

    def test_items_without_an_enclosure_are_skipped(self):
        self.assertNotIn(900, [r.episode for r in ab.parse_feed(SAMPLE_FEED)])


class EtagSizeTest(unittest.TestCase):
    def test_nginx_etag_carries_the_byte_size(self):
        self.assertEqual(ab.etag_size('"648b67b6-1ca88d2"'), 30050514)

    def test_etag_without_a_size_part_is_ignored(self):
        self.assertIsNone(ab.etag_size('"648b67b6"'))
        self.assertIsNone(ab.etag_size('W/"zz"'))
        self.assertIsNone(ab.etag_size(None))


class LocalNameTest(unittest.TestCase):
    def test_local_file_is_named_by_episode_number(self):
        self.assertEqual(ab.local_name(513), "513.mp3")


class PlanBackupTest(unittest.TestCase):
    def test_skips_files_that_already_match_the_remote_size(self):
        plan = ab.plan_backup(probes(), {513: 1000})
        self.assertEqual([p.episode for p in plan], [1])

    def test_requeues_a_local_file_with_the_wrong_size(self):
        plan = ab.plan_backup(probes(), {513: 999})
        self.assertEqual([p.episode for p in plan], [513, 1])

    def test_unreachable_episodes_are_never_queued(self):
        plan = ab.plan_backup(probes(), {})
        self.assertNotIn(9, [p.episode for p in plan])


class PlausibilityTest(unittest.TestCase):
    def test_master_sized_file_passes(self):
        self.assertTrue(ab.size_is_plausible(30_050_514, 3755.96))

    def test_low_bitrate_vintage_episode_passes(self):
        self.assertTrue(ab.size_is_plausible(27_767_641, 3487))

    def test_high_bitrate_modern_episode_passes(self):
        self.assertTrue(ab.size_is_plausible(81_244_770, 3378))

    def test_promo_clip_standing_in_for_the_master_fails(self):
        self.assertFalse(ab.size_is_plausible(273_971, 3526.14))

    def test_an_unknown_duration_is_never_a_failure(self):
        self.assertTrue(ab.size_is_plausible(50_000_000, None))

    def test_a_tiny_placeholder_is_refused_even_without_a_duration(self):
        # #454：官网把撤下的集换成 242 字节的 HTML 占位，HEAD 200、ETag 尾也与字节数自洽，
        # 没有 timings 就没有时长可比，只有"它根本不像一份音频"这条能拦住冷备。
        self.assertFalse(ab.size_is_plausible(242, None))

    def test_the_floor_leaves_the_smallest_real_master_alone(self):
        # 现清单里最小的一集（#86，19 MB）必须照常通过：门槛只挡占位，不重判健康条目。
        self.assertTrue(ab.size_is_plausible(19_070_255, None))


class ImplausibleTest(unittest.TestCase):
    def test_only_entries_whose_size_contradicts_the_timing_are_selected(self):
        entries = [
            {"episode": 489, "size": 273_971, "audio_seconds": 3526.14},
            {"episode": 1, "size": 30_050_514, "audio_seconds": 3755.96},
            {"episode": 7, "size": 12_000_000, "audio_seconds": None},
        ]
        self.assertEqual([e["episode"] for e in ab.implausible(entries)], [489])


class MergePreviousTest(unittest.TestCase):
    def test_a_changed_url_drops_the_stale_fingerprint(self):
        entry = {"url": "https://x/489/master.mp3", "sha256": None}
        merged = ab.merge_previous(entry, {"url": "https://x/489/promo.mp3", "sha256": "deadbeef"})
        self.assertIsNone(merged["sha256"])

    def test_an_unchanged_url_keeps_the_fingerprint(self):
        entry = {"url": "https://x/1.mp3", "sha256": None}
        merged = ab.merge_previous(entry, {"url": "https://x/1.mp3", "sha256": "abc"})
        self.assertEqual(merged["sha256"], "abc")

    def test_a_changed_url_drops_the_stale_provenance(self):
        # 没有它，下一轮 manifest 会把"保险库自证"的指纹当成"我们手上有这份字节"。
        entry = {"url": "https://x/489/master.mp3", "sha256": None}
        merged = ab.merge_previous(
            entry, {"url": "https://x/489/promo.mp3", "sha256": "deadbeef", "sha256_from": "vault"}
        )
        self.assertIsNone(merged["sha256_from"])

    def test_an_unchanged_url_keeps_the_provenance(self):
        entry = {"url": "https://x/1.mp3", "sha256": None}
        merged = ab.merge_previous(
            entry, {"url": "https://x/1.mp3", "sha256": "abc", "sha256_from": "vault"}
        )
        self.assertEqual(merged["sha256_from"], "vault")


class LoadManifestTest(unittest.TestCase):
    def test_a_missing_manifest_reads_as_no_previous_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ab.load_manifest(Path(tmp) / "absent.json"))

    def test_a_truncated_manifest_is_refused_instead_of_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text('{"version": 1, "epi', encoding="utf-8")
            with self.assertRaises(ab.ManifestCorrupt):
                ab.load_manifest(path)

    def test_a_whole_manifest_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            ab.write_manifest([{"episode": 1, "sha256": "abc"}], path=path)
            self.assertEqual(ab.load_manifest(path)["episodes"][0]["sha256"], "abc")


class Sha256Test(unittest.TestCase):
    def test_hashes_the_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"hello")
            self.assertEqual(ab.sha256_of(path), hashlib.sha256(b"hello").hexdigest())

    def test_a_one_byte_change_moves_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"hello")
            before = ab.sha256_of(path)
            path.write_bytes(b"hellp")
            self.assertNotEqual(ab.sha256_of(path), before)


class ParseAssetsTest(unittest.TestCase):
    def test_digest_prefix_is_stripped_to_plain_hex(self):
        pages = [
            [{"id": 1, "name": "1.mp3", "size": 100, "digest": "sha256:" + "ab" * 32}],
        ]
        assets = ab.parse_assets(pages)
        self.assertEqual(assets["1.mp3"]["digest"], "ab" * 32)

    def test_pages_merge_into_one_lookup(self):
        assets = ab.parse_assets(
            [
                [{"id": 1, "name": "1.mp3", "size": 100, "digest": "sha256:aa"}],
                [{"id": 2, "name": "2.mp3", "size": 200, "digest": None}],
            ]
        )
        self.assertEqual(sorted(assets), ["1.mp3", "2.mp3"])
        self.assertIsNone(assets["2.mp3"]["digest"])


class SplitTagsTest(unittest.TestCase):
    def test_blank_and_repeated_tags_collapse_to_one_ledger_entry(self):
        self.assertEqual(
            ab.split_tags("audio-vault, audio-vault-2 ,,audio-vault"),
            ["audio-vault", "audio-vault-2"],
        )

    def test_a_lone_tag_is_still_a_list(self):
        self.assertEqual(ab.split_tags("audio-vault"), ["audio-vault"])

    def test_nothing_but_commas_is_an_empty_plan(self):
        self.assertEqual(ab.split_tags(" , "), [])


class MergeLedgersTest(unittest.TestCase):
    def test_every_volume_contributes_its_own_assets(self):
        assets, conflicts = ab.merge_ledgers(
            [
                ("audio-vault", {"1.mp3": {"id": 1, "size": 100, "digest": "aa"}}),
                ("audio-vault-2", {"2.mp3": {"id": 2, "size": 200, "digest": "bb"}}),
            ]
        )
        self.assertEqual(sorted(assets), ["1.mp3", "2.mp3"])
        self.assertEqual(conflicts, [])

    def test_each_asset_remembers_which_volume_holds_it(self):
        # 下载链接得指向真正存着字节的那一卷。
        assets, _ = ab.merge_ledgers(
            [("audio-vault-2", {"2.mp3": {"id": 2, "size": 200, "digest": "bb"}})]
        )
        self.assertEqual(assets["2.mp3"]["tag"], "audio-vault-2")

    def test_one_name_with_two_digests_is_a_conflict_not_a_silent_pick(self):
        assets, conflicts = ab.merge_ledgers(
            [
                ("audio-vault", {"1.mp3": {"id": 1, "size": 100, "digest": "aa"}}),
                ("audio-vault-2", {"1.mp3": {"id": 2, "size": 100, "digest": "bb"}}),
            ]
        )
        self.assertEqual(conflicts, [("1.mp3", "audio-vault", "audio-vault-2")])
        self.assertEqual(assets["1.mp3"]["digest"], "aa")


class CapacityCheckTest(unittest.TestCase):
    def test_passing_the_slot_line_stops_before_a_single_upload(self):
        stop, note = ab.capacity_check(["audio-vault"], present=999, pending=2)
        self.assertTrue(stop)
        self.assertIn("--tag", note)

    def test_the_last_tenth_of_a_volume_is_a_warning_not_a_stop(self):
        stop, note = ab.capacity_check(["audio-vault"], present=950, pending=0)
        self.assertFalse(stop)
        self.assertIn("950", note)

    def test_room_to_grow_says_nothing(self):
        self.assertEqual(ab.capacity_check(["audio-vault"], present=877, pending=20), (False, None))

    def test_a_second_volume_doubles_the_room(self):
        self.assertEqual(
            ab.capacity_check(["a", "b"], present=1500, pending=0), (False, None)
        )


class NeedsUploadTest(unittest.TestCase):
    def setUp(self):
        self.assets = {"1.mp3": {"id": 1, "size": 1000, "digest": "good"}}

    def entry(self, size=1000, sha256="good"):
        return {"episode": 1, "size": size, "sha256": sha256}

    def test_an_asset_with_matching_size_and_digest_is_not_reuploaded(self):
        self.assertFalse(ab.needs_upload(self.entry(), self.assets))

    def test_a_size_change_forces_a_reupload(self):
        self.assertTrue(ab.needs_upload(self.entry(size=999), self.assets))

    def test_a_digest_mismatch_forces_a_reupload(self):
        self.assertTrue(ab.needs_upload(self.entry(sha256="other"), self.assets))

    def test_a_missing_asset_is_queued(self):
        self.assertTrue(ab.needs_upload({"episode": 7, "size": 5, "sha256": None}, self.assets))

    def test_an_unknown_local_fingerprint_still_trusts_the_remote_asset(self):
        self.assertFalse(ab.needs_upload(self.entry(sha256=None), self.assets))


class BalanceTest(unittest.TestCase):
    def test_heavy_items_land_on_lanes_of_equal_total_weight(self):
        sizes = {1: 100, 2: 100, 3: 100, 4: 100}
        lanes = ab.balance(list(sizes.items()), 2)
        self.assertEqual([sum(sizes[e] for e in lane) for lane in lanes], [200, 200])

    def test_the_indivisible_remainder_stays_on_the_lighter_side(self):
        # 100/90/10/10 摊两档，最好也只能是 110 与 100：贪心不该更差。
        sizes = {1: 100, 2: 90, 3: 10, 4: 10}
        lanes = ab.balance(list(sizes.items()), 2)
        totals = sorted(sum(sizes[e] for e in lane) for lane in lanes)
        self.assertEqual(totals, [100, 110])

    def test_fewer_items_than_lanes_leaves_empty_lanes(self):
        self.assertEqual(ab.balance([(1, 5), (2, 5)], 4), [[1], [2], [], []])

    def test_lanes_are_clamped_to_at_least_one(self):
        self.assertEqual(ab.balance([(1, 5)], 0), [[1]])

    def test_every_item_appears_exactly_once(self):
        items = [(n, n * 7) for n in range(1, 31)]
        lanes = ab.balance(items, 6)
        self.assertEqual(sorted(e for lane in lanes for e in lane), [n for n, _ in items])


class DownloadRetryTest(unittest.TestCase):
    """全量波里第 192 集：清单说有 55,607,339 字节，runner 拿到 404，
    单独重派立刻 200。自相矛盾的 404 必须重试，不能一抖就判死整档。"""

    class Response:
        def __init__(self, body, status_code=200):
            self.body, self.status_code = body, status_code

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            if self.status_code != 200:
                raise requests.HTTPError(
                    f"{self.status_code} error", response=self
                )

        def iter_content(self, size):
            for start in range(0, len(self.body), size):
                yield self.body[start:start + size]

    class Session:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = 0

        def get(self, url, timeout=None, stream=True):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    def setUp(self):
        self.delays = []
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.target = Path(self.workdir.name) / "192.mp3"

    def probe(self, size):
        return ab.Probe(192, "https://example/192.mp3", 200, size, None, None)

    def test_a_lone_404_retries_and_lands_the_bytes(self):
        body = b"x" * 5000
        session = self.Session([
            self.Response(body, status_code=404),
            self.Response(body),
        ])
        written, digest = ab.download(session, self.probe(len(body)), self.target,
                                      wait=self.delays.append)
        self.assertEqual(written, len(body))
        self.assertEqual(digest, hashlib.sha256(body).hexdigest())
        self.assertEqual(session.calls, 2)
        self.assertEqual(len(self.delays), 1)

    def test_a_connection_drop_also_retries(self):
        body = b"y" * 4096
        session = self.Session([requests.ConnectionError("boom"), self.Response(body)])
        written, _ = ab.download(session, self.probe(len(body)), self.target,
                                 wait=self.delays.append)
        self.assertEqual(written, len(body))
        self.assertEqual(session.calls, 2)

    def test_a_persistent_404_gives_up_after_the_budget(self):
        session = self.Session([self.Response(b"", status_code=404)] * ab.DOWNLOAD_RETRIES)
        with self.assertRaises(requests.HTTPError):
            ab.download(session, self.probe(1000), self.target, wait=self.delays.append)
        self.assertEqual(session.calls, ab.DOWNLOAD_RETRIES)

    def test_giving_up_leaves_no_fake_finished_file(self):
        session = self.Session([self.Response(b"", status_code=404)] * ab.DOWNLOAD_RETRIES)
        with self.assertRaises(requests.HTTPError):
            ab.download(session, self.probe(1000), self.target, wait=self.delays.append)
        self.assertFalse(self.target.exists())

    def test_the_backoff_grows_instead_of_hammering(self):
        session = self.Session([self.Response(b"", status_code=503)] * ab.DOWNLOAD_RETRIES)
        with self.assertRaises(requests.HTTPError):
            ab.download(session, self.probe(1000), self.target, wait=self.delays.append)
        self.assertEqual(self.delays, sorted(self.delays))
        self.assertGreater(self.delays[-1], self.delays[0])


class PadLanesTest(unittest.TestCase):
    def test_short_runs_are_padded_up_to_the_matrix_slots(self):
        lanes = ab.pad_lanes([[1, 2], [3]])
        self.assertEqual(lanes, ["1,2", "3"] + [""] * (ab.MATRIX_SLOTS - 2))

    def test_more_lanes_than_the_matrix_has_slots_is_refused(self):
        # 静默截断等于让多出来的档永不镜像，必须炸在计划阶段。
        with self.assertRaises(ValueError):
            ab.pad_lanes([[n] for n in range(ab.MATRIX_SLOTS + 1)])


class RegressionsTest(unittest.TestCase):
    """定时刷新要自动提交清单，这道闸门判断"这次探测是不是把活着说成了死了"。"""

    def entry(self, episode, status=200, size=1_000_000):
        return {
            "episode": episode,
            "status": status,
            "size": size,
            "url": f"https://example/{episode}.mp3",
        }

    def rows(self, old, new):
        return ab.regressions(old, new)

    def test_an_unchanged_ledger_is_quiet(self):
        old = [self.entry(1), self.entry(2)]
        self.assertEqual(self.rows(old, list(old)), [])

    def test_bytes_got_bigger_is_a_reencode_not_a_death(self):
        rows = self.rows([self.entry(1)], [self.entry(1, size=2_000_000)])
        self.assertEqual(rows, [])

    def test_a_live_link_answering_404_is_a_regression(self):
        rows = self.rows([self.entry(1)], [self.entry(1, status=404, size=0)])
        self.assertEqual([r["episode"] for r in rows], [1])
        self.assertIn("HTTP 200", rows[0]["was"])
        self.assertIn("HTTP 404", rows[0]["now"])

    def test_throttling_and_server_errors_count_too(self):
        old = [self.entry(1), self.entry(2), self.entry(3)]
        new = [
            self.entry(1, status=403, size=0),
            self.entry(2, status=429, size=1_000_000),
            self.entry(3, status=500, size=0),
        ]
        self.assertEqual([r["episode"] for r in self.rows(old, new)], [1, 2, 3])

    def test_a_200_with_zero_bytes_is_a_regression(self):
        rows = self.rows([self.entry(1)], [self.entry(1, size=0)])
        self.assertEqual([r["episode"] for r in rows], [1])

    def test_an_episode_that_vanished_from_the_feed_is_a_regression(self):
        rows = self.rows([self.entry(1), self.entry(2)], [self.entry(2)])
        self.assertEqual([r["episode"] for r in rows], [1])

    def test_new_episodes_are_progress_not_regression(self):
        rows = self.rows([self.entry(1)], [self.entry(1), self.entry(2)])
        self.assertEqual(rows, [])

    def test_a_link_already_recorded_as_dead_matters_no_more(self):
        # 老清单就非 200，这次换了个死法不算新增失效——闸门只盯"从活到死"。
        rows = self.rows(
            [self.entry(1, status=404, size=0)],
            [self.entry(1, status=503, size=0)],
        )
        self.assertEqual(rows, [])

    def test_the_gate_reaches_the_process_exit_code(self):
        # CI 只看得见退出码：闸门不落到 exit code 上就等于没有。
        original = ab.manifest_command
        ab.manifest_command = lambda args: 1
        self.addCleanup(setattr, ab, "manifest_command", original)
        with self.assertRaises(SystemExit) as ctx:
            ab.main(["manifest"])
        self.assertEqual(ctx.exception.code, 1)


class SyncDigestsTest(unittest.TestCase):
    """把 release 的 digest 认作清单基线：纯函数，不发网络请求。"""

    def entry(self, episode, size=50_000_000, sha256=None, status=200, plausible=True):
        return {"episode": episode, "size": size, "sha256": sha256, "status": status,
                "plausible_master": plausible, "url": f"https://x/{episode}.mp3"}

    def assets(self, episode, size=50_000_000, digest="a" * 64):
        return {f"{episode}.mp3": {"size": size, "digest": digest, "tag": "audio-vault", "id": 1}}

    def test_an_unbaselined_episode_takes_the_vault_digest(self):
        # 清单里 699 集没有本地基线，而 GitHub 早已为每个资产算好 sha256。
        entries = [self.entry(885)]
        result = ab.sync_digests(entries, self.assets(885, digest="be" * 32))
        self.assertEqual(entries[0]["sha256"], "be" * 32)
        self.assertEqual(result.synced, [885])

    def test_a_vault_digest_is_recorded_as_the_vault_saying_it(self):
        # 它是 GitHub 的单方陈述，不是"我们下过这份字节"。丢了这层，
        # release-index 的 checked 会把 699 集集体冒充本地核对。
        entries = [self.entry(885)]
        ab.sync_digests(entries, self.assets(885, digest="be" * 32))
        self.assertEqual(entries[0]["sha256_from"], "vault")

    def test_an_existing_baseline_is_never_overwritten(self):
        # 已有基线是本地算的，与远端不同就是真冲突，留给 release 的对账报警，不在这里抹平。
        entries = [self.entry(192, sha256="ff" * 32)]
        result = ab.sync_digests(entries, self.assets(192, digest="00" * 32))
        self.assertEqual(entries[0]["sha256"], "ff" * 32)
        self.assertEqual(result.synced, [])

    def test_a_different_byte_count_is_not_a_baseline(self):
        # 同名资产字节数不符 = 另一份文件，写进去会把漂移固化成基线。
        entries = [self.entry(489)]
        result = ab.sync_digests(entries, self.assets(489, size=273_971))
        self.assertIsNone(entries[0]["sha256"])
        self.assertEqual(result.mismatch, [489])

    def test_an_asset_without_a_digest_is_left_alone(self):
        entries = [self.entry(7)]
        result = ab.sync_digests(entries, self.assets(7, digest=None))
        self.assertEqual((result.synced, result.mismatch, result.absent), ([], [], []))

    def test_an_episode_the_vault_does_not_hold_yet_is_reported_not_written(self):
        entries = [self.entry(901)]
        result = ab.sync_digests(entries, {})
        self.assertIsNone(entries[0]["sha256"])
        self.assertEqual(result.absent, [901])

    def test_a_placeholder_master_is_not_eligible_for_a_baseline(self):
        # #454 那类 242 字节占位本来就不该进 release，也不该因为账本里有同名资产就拿到基线。
        entries = [self.entry(454, size=242, plausible=False)]
        result = ab.sync_digests(entries, self.assets(454, size=242))
        self.assertEqual((result.synced, result.absent), ([], []))
        self.assertIsNone(entries[0]["sha256"])

    def test_a_dead_link_is_not_eligible_for_a_baseline(self):
        entries = [self.entry(490, status=404)]
        result = ab.sync_digests(entries, self.assets(490))
        self.assertEqual(result.absent, [])
        self.assertIsNone(entries[0]["sha256"])


class LocallyVerifiedTest(unittest.TestCase):
    """release-index 的 checked 只能说"我们手上有这份字节"。"""

    def asset(self, digest="a" * 64):
        return {"digest": digest, "size": 1, "tag": "audio-vault", "id": 1}

    def test_a_locally_computed_baseline_that_agrees_is_verified(self):
        entry = {"sha256": "a" * 64, "sha256_from": "local"}
        self.assertTrue(ab.locally_verified(entry, self.asset()))

    def test_a_vault_inherited_baseline_agreeing_is_not_local_proof(self):
        entry = {"sha256": "a" * 64, "sha256_from": "vault"}
        self.assertFalse(ab.locally_verified(entry, self.asset()))

    def test_a_baseline_from_before_provenance_is_still_local_proof(self):
        # 早期本地真下过的 178 集没有 sha256_from 字段，不该被降级成"没核对"。
        self.assertTrue(ab.locally_verified({"sha256": "a" * 64}, self.asset()))

    def test_a_disagreeing_digest_is_never_verified(self):
        entry = {"sha256": "f" * 64, "sha256_from": "local"}
        self.assertFalse(ab.locally_verified(entry, self.asset()))

    def test_no_baseline_is_no_proof(self):
        # digest 也缺失时 None == None 会假阳性。
        self.assertFalse(ab.locally_verified({"sha256": None}, {"digest": None}))


if __name__ == "__main__":
    unittest.main()
