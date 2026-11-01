#!/usr/bin/env python3
"""Archive 音频容灾：指纹清单 + 原样字节冷备。

母带 URL 里的 43 字符 token 是内容指纹，一旦 TAL 换 CMS 或重新编码母带，
整批链接会同时失效，所以冷备必须是原样字节 —— 转码副本无法证明与
docs/timings/ 对齐时用的是同一份文件。

音频绝不进仓库（docs/ 是 Pages 根，站点上限 1 GB），只落仓库外的冷备目录。
"""

import argparse
import concurrent.futures
import hashlib
import heapq
import json
import os
import re
import subprocess
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import requests

from align_timings import resolve_archive

FEED_FILE = Path("docs/podcast.xml")
MANIFEST_FILE = Path("docs/audio-manifest.json")
TIMINGS_DIR = Path("docs/timings")
MANIFEST_VERSION = 1

# 矩阵档位固定 20：填小的档拿到空串自己退出，和 align-timings 的跑批惯例一致。
MATRIX_SLOTS = 20

# GitHub 每个 release 只有 1000 个资产位（单文件 <2 GiB，总大小与带宽都不限）。
# 877 集装得下，所以默认一卷；装满才需要往 --tag 末尾追加新卷。
ASSET_SLOTS_PER_RELEASE = 1000

# TAL 母带最窄见过 64kbps（≈8 KB/s），最宽 192kbps（≈24 KB/s）。
# 落在带外说明 enclosure 指向的不是母带，比如 489 的 enclosure 是个 274 KB 的 promo。
MIN_BYTES_PER_SECOND = 4000
MAX_BYTES_PER_SECOND = 40000

# 没有时长可比时也得有个下限：官网会把撤下的集的母带换成几百字节的 HTML 占位
# （#454 是 242 字节，HEAD 200 且 ETag 自洽）。真母带最小的是 #86 的 19 MB。
MIN_MASTER_BYTES = 1_000_000

# 与 generate.py 的礼貌抓取保持一致：重试退避，避开 4xx 的无意义等待。
REQUEST_RETRIES = 5
HEAD_TIMEOUT = 30
READ_TIMEOUT = 120
CHUNK = 1 << 16

# 下载时的瞬时判定比 HEAD 时宽：清单已经说过这个 URL 有 N 字节，取的时候突然
# 403/404 是自相矛盾的抖动，不是"这集没了"。全量波里第 192 集就是这样 404，
# 单独重派立刻 200 且字节与本机一致。
DOWNLOAD_RETRIES = 4
DOWNLOAD_BACKOFF_SECONDS = 10
TRANSIENT_DOWNLOAD_STATUSES = frozenset({403, 404, 408, 429, 500, 502, 503, 504})

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
# 集数只认 <title>：archive 形态带 Drupal 的重复上传后缀（513_0.mp3），
# 从 URL 尾数反推会把这类集数读错。
TITLE_RE = re.compile(r"<title>\s*(\d+)\s*:")
ENCLOSURE_RE = re.compile(r'<enclosure\s+url="([^"]+\.mp3)"')
ETAG_RE = re.compile(r'^"([0-9a-f]+)-([0-9a-f]+)"$')

AudioRef = namedtuple("AudioRef", "episode url")
Probe = namedtuple("Probe", "episode url status size etag last_modified", defaults=(None, None))


class ManifestCorrupt(Exception):
    """清单读不出来就拒绝运行，而不是当作没有上一轮。"""


def parse_feed(xml):
    """从 feed 取出每集的母带 URL，没有 enclosure 的集直接跳过。"""
    refs = []
    for item in ITEM_RE.findall(xml):
        title = TITLE_RE.search(item)
        enclosure = ENCLOSURE_RE.search(item)
        if not title or not enclosure:
            continue
        refs.append(AudioRef(int(title.group(1)), enclosure.group(1)))
    return refs


def etag_size(etag):
    """nginx 的 ETag 形如 "<mtime>-<size>"，后半段就是字节数。"""
    if not etag:
        return None
    match = ETAG_RE.match(etag.strip())
    return int(match.group(2), 16) if match else None


def size_is_plausible(size, seconds):
    """字节数与校对稿时长是否相容；不知道时长时只要求它像一份音频。"""
    if size < MIN_MASTER_BYTES:
        return False
    if not seconds:
        return True
    return MIN_BYTES_PER_SECOND <= size / seconds <= MAX_BYTES_PER_SECOND


def implausible(entries):
    return [e for e in entries if not size_is_plausible(e["size"], e.get("audio_seconds"))]


def merge_previous(entry, old):
    """上一轮的 sha256 只在 URL 未变时才可继承：换过源就是另一份文件。"""
    entry["sha256"] = old.get("sha256") if old.get("url") == entry["url"] else None
    return entry


def regressions(old_entries, new_entries):
    """清单会自动提交，所以"从活到死"必须过这道闸：一轮 HEAD 抖动被写成事实之后，
    下一轮只会照着一个已死的 URL 重探，plan 也会把那一集从待镜像里永久剔掉。
    只认活→死：变大、新增、本来就死着的换了个死法，都是真变化，不该拦。"""
    old = {e["episode"]: e for e in old_entries}
    new = {e["episode"]: e for e in new_entries}
    rows = []
    for episode in sorted(old):
        was = old[episode]
        if was["status"] != 200:
            continue
        now = new.get(episode)
        if now is not None and now["status"] == 200 and now["size"] > 0:
            continue
        rows.append(
            {
                "episode": episode,
                "was": f"HTTP {was['status']}/{was['size']}",
                "now": f"HTTP {now['status']}/{now['size']}" if now else "feed 里已无此集",
            }
        )
    return rows


def load_timed_lengths(directory=TIMINGS_DIR):
    lengths = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        episode, seconds = data.get("episode"), data.get("audio_seconds")
        if isinstance(episode, int) and seconds:
            lengths[episode] = seconds
    return lengths


def local_name(episode):
    return f"{episode}.mp3"


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_assets(pages):
    """release 资产按页返回，digest 形如 "sha256:<hex>"。"""
    assets = {}
    for page in pages:
        for asset in page:
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            assets[asset["name"]] = {
                "id": asset["id"],
                "size": asset["size"],
                "digest": digest or None,
            }
    return assets


def split_tags(raw):
    """--tag 允许逗号分隔的多个卷：一个 release 只有 1000 个资产位，装满就得另起一卷，
    而所有旧卷都必须留在账本里，否则新卷会把已备份的集当成待传、重搬 39 GB。"""
    return list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))


def merge_ledgers(pages):
    """pages 是 [(卷 tag, 该卷资产), ...]，合成一本账并记住字节在哪一卷。
    同名资产在两卷里 digest 不同是真冲突：报出来，静默挑一个会让下一轮拿错的判"已备份"。"""
    assets, conflicts = {}, []
    for tag, page in pages:
        for name, asset in page.items():
            known = assets.get(name)
            if known:
                if known["digest"] != asset["digest"]:
                    conflicts.append((name, known["tag"], tag))
                continue
            assets[name] = {**asset, "tag": tag}
    return assets, conflicts


def capacity_check(tags, present, pending):
    """撞线必须在传之前说：传到第 1000 个才被拒，剩下的集要等人工发现，比不传更难查。"""
    slots = ASSET_SLOTS_PER_RELEASE * len(tags)
    projected = present + pending
    if projected > slots:
        return True, (
            f"{projected} 个资产 > {len(tags)} 卷 × {ASSET_SLOTS_PER_RELEASE} 位："
            f"在 --tag 末尾追加新卷（如 audio-vault-2），旧卷继续留在 --tag 里。"
        )
    if projected > slots * 0.9:
        return False, (
            f"{projected}/{slots} 位，已用九成——快到另起一卷的时候了。"
        )
    return False, None


def needs_upload(entry, assets):
    """远端同名资产的字节数与指纹都对得上就不重传。"""
    asset = assets.get(local_name(entry["episode"]))
    if not asset or asset["size"] != entry["size"]:
        return True
    if entry.get("sha256") and asset["digest"] != entry["sha256"]:
        return True
    return False


def balance(items, lanes):
    """按字节把集号摊到 lanes 档（重的先放），否则个别 job 会拖住整轮。"""
    lanes = max(1, int(lanes))
    buckets = [[] for _ in range(lanes)]
    loads = [(0, lane) for lane in range(lanes)]
    heapq.heapify(loads)
    for episode, size in sorted(items, key=lambda kv: -kv[1]):
        load, lane = heapq.heappop(loads)
        buckets[lane].append(episode)
        heapq.heappush(loads, (load + size, lane))
    for bucket in buckets:
        bucket.sort()
    return buckets


def pad_lanes(lanes):
    """每档拼成逗号串并补齐到矩阵档数。档数超过矩阵容量是计划错误：静默截断会让
    多出来的那些集永不镜像，而矩阵断言反而通得过。"""
    if len(lanes) > MATRIX_SLOTS:
        raise ValueError(
            f"--shards 不能超过 {MATRIX_SLOTS}（当前 {len(lanes)} 档）："
            f"矩阵放不下多出的档，那些集会被静默丢掉。"
        )
    joined = [",".join(str(episode) for episode in lane) for lane in lanes]
    return joined + [""] * (MATRIX_SLOTS - len(joined))


def plan_backup(probes, present):
    """列出还缺的集：本地字节数与远端不符（或不存在）才进队。"""
    todo = []
    for probe in probes:
        if probe.status != 200 or probe.size <= 0:
            continue
        if present.get(probe.episode) == probe.size:
            continue
        todo.append(probe)
    return todo


def build_session():
    session = requests.Session()
    session.headers["User-Agent"] = "TMLT-backup/1.0"
    return session


def head(session, url):
    """请求头部，429/5xx 退避重试，其他 4xx 视为永久结果直接记录。"""
    for attempt in range(REQUEST_RETRIES):
        try:
            response = session.head(url, timeout=HEAD_TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            response = None

        if response is not None and response.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue

        if response is not None and 500 <= response.status_code < 600:
            time.sleep(10 + attempt * 10)
            continue

        if response is None:
            return {"status": 0, "size": 0, "etag": None, "last_modified": None}

        return {
            "status": response.status_code,
            "size": int(response.headers.get("Content-Length") or 0),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
        }

    return {"status": 429, "size": 0, "etag": None, "last_modified": None}


def probe_all(refs, workers=8):
    """并发 HEAD 全部母带，按 feed 原序返回。"""
    probes = []
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        urls = [ref.url for ref in refs]
        # requests.Session 不保证线程安全，每个工作线程自带一个。
        for index, result in enumerate(pool.map(lambda u: head(build_session(), u), urls), 1):
            ref = refs[index - 1]
            probes.append(Probe(episode=ref.episode, url=ref.url, **result))
            if index % 100 == 0 or index == len(refs):
                print(f"HEAD {index}/{len(refs)}", flush=True)
    return probes


def load_manifest(path=MANIFEST_FILE):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestCorrupt(
            f"{path} 存在但读不出 JSON（{exc}）。清单里的 sha256 是冷备的唯一凭据，"
            f"不能静默重建；确认后可以删掉该文件再跑 manifest。"
        ) from exc


def write_manifest(entries, path=MANIFEST_FILE):
    payload = {
        "version": MANIFEST_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "episodes": entries,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def manifest_command(args):
    refs = parse_feed(FEED_FILE.read_text(encoding="utf-8"))
    print(f"Feed audio refs: {len(refs)}")
    lengths = load_timed_lengths()
    session = build_session()

    probes = {p.episode: p for p in probe_all(refs, workers=args.workers)}

    # enclosure 只是 feed 的便捷入口，母带权威在 transcript 页的 playlist-data。
    # 两者对不上（字节数与时长矛盾）的集才去回源校正，避免 877 次页面抓取。
    for episode, probe in list(probes.items()):
        seconds = lengths.get(episode)
        if size_is_plausible(probe.size, seconds):
            continue
        correct = resolve_archive(session, episode)
        if not correct or correct == probe.url:
            continue
        print(
            f"#{episode} enclosure {probe.size} B / {seconds:.0f}s 不是母带，"
            f"改用 playlist-data: {correct}"
        )
        probes[episode] = Probe(episode=episode, url=correct, **head(session, correct))

    previous = load_manifest()
    known = {}
    if previous:
        known = {e["episode"]: e for e in previous["episodes"]}

    entries = []
    for episode in sorted(probes):
        probe = probes[episode]
        old = known.get(episode, {})
        seconds = lengths.get(episode)
        entries.append(
            merge_previous(
                {
                    "episode": episode,
                    "file": local_name(episode),
                    "url": probe.url,
                    "status": probe.status,
                    "size": probe.size,
                    "etag": probe.etag,
                    "last_modified": probe.last_modified,
                    "etag_size_matches": probe.size > 0 and etag_size(probe.etag) == probe.size,
                    "audio_seconds": seconds,
                    "plausible_master": size_is_plausible(probe.size, seconds),
                    "sha256": None,
                },
                old,
            )
        )

    write_manifest(entries)
    reachable = [e for e in entries if e["status"] == 200]
    total = sum(e["size"] for e in reachable)
    print(f"Manifest: {MANIFEST_FILE} | 可达 {len(reachable)} | 合计 {total / 1e9:.2f} GB")
    for e in implausible(entries):
        print(f"  ⚠️ {e['episode']} 字节数与时长不符: {e['size']} B / {e['audio_seconds']}s")
    for e in entries:
        if e["status"] != 200:
            print(f"  ⚠️ {e['episode']} HTTP {e['status']} {e['url']}")
    lost = regressions(previous["episodes"] if previous else [], entries)
    if lost:
        print(f"  ⛔ 新增失效 {len(lost)}（清单已写，但自动提交该停在这一步）")
        for row in lost[:20]:
            print(f"  #{row['episode']} {row['was']} -> {row['now']}")
        print("  先分清真失效还是探测抖动：再跑一次 manifest，两次都死才是真没了。")
        return 1
    return 0


def scan_local(directory):
    present = {}
    for path in directory.glob("*.mp3"):
        stem = path.stem
        if stem.isdigit():
            present[int(stem)] = path.stat().st_size
    return present


def download(session, probe, target, wait=time.sleep):
    """先落 .part 再改名：中途断了不会留下半个伪成品；瞬时错误退避重试。
    失败抛出时 .part 还没被打开，所以盘上不会多出看起来完整的文件。"""
    part = target.with_suffix(".mp3.part")
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            with session.get(probe.url, timeout=READ_TIMEOUT, stream=True) as response:
                response.raise_for_status()
                digest = hashlib.sha256()
                written = 0
                with part.open("wb") as handle:
                    for chunk in response.iter_content(CHUNK):
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
            part.rename(target)
            return written, digest.hexdigest()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            last_try = attempt == DOWNLOAD_RETRIES - 1
            if last_try or status not in TRANSIENT_DOWNLOAD_STATUSES:
                raise
            wait(DOWNLOAD_BACKOFF_SECONDS * (attempt + 1))
        except requests.RequestException:
            if attempt == DOWNLOAD_RETRIES - 1:
                raise
            wait(DOWNLOAD_BACKOFF_SECONDS * (attempt + 1))


def backup_command(args):
    payload = load_manifest()
    if payload is None:
        sys.exit("先跑 manifest 生成指纹清单。")

    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)

    wanted = {e["episode"]: e for e in payload["episodes"] if e["status"] == 200}
    if args.episodes:
        wanted = {n: wanted[n] for n in args.episodes if n in wanted}

    present = scan_local(directory)
    probes = [
        Probe(episode=e["episode"], url=e["url"], status=e["status"],
              size=e["size"], etag=e["etag"], last_modified=e["last_modified"])
        for e in wanted.values()
    ]
    todo = plan_backup(probes, present)
    done = len(probes) - len(todo)
    total_bytes = sum(p.size for p in todo)
    print(f"已在盘 {done} | 待下 {len(todo)} | {total_bytes / 1e9:.2f} GB")

    session = build_session()
    failures = []
    for index, probe in enumerate(todo, 1):
        started = time.time()
        try:
            written, sha256 = download(session, probe, directory / local_name(probe.episode))
        except (requests.RequestException, OSError) as exc:
            failures.append((probe.episode, str(exc)[:120]))
            print(f"[{index}/{len(todo)}] {probe.episode} 失败 {exc}")
            continue

        if written != probe.size:
            failures.append((probe.episode, f"字节数 {written} != 远端 {probe.size}"))
            print(f"[{index}/{len(todo)}] {probe.episode} 长度不符")
            continue

        record = wanted[probe.episode]
        record["sha256"] = sha256
        record["size"] = written
        # 两小时的作业不能把进度攒到最后一次写盘：断了就白算。
        if index % 25 == 0 or index == len(todo):
            write_manifest(payload["episodes"])
        print(
            f"[{index}/{len(todo)}] {probe.episode} {written / 1e6:.1f} MB "
            f"{time.time() - started:.0f}s sha256={sha256[:12]}"
        )

    # wanted 里的 dict 就是 payload["episodes"] 的同一批对象，改完整体写回，
    # 子集模式也不会把其余集的记录丢掉。
    write_manifest(payload["episodes"])
    if failures:
        print(f"失败 {len(failures)} 集，重跑同一命令即可续：")
        for episode, reason in failures:
            print(f"  {episode}: {reason}")
        sys.exit(1)


def gh(*args):
    """调用 runner 自带的 gh 上传资产；认证走 GH_TOKEN，代码里不碰令牌。"""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError as exc:
        # 不能退化成"release 是空的"：那会让下一轮把 39 GB 全量重传。
        raise SystemExit("需要 gh（GitHub CLI），它只在 Actions runner 上可用。") from exc


def api_root():
    return os.environ.get("GITHUB_API_URL", "https://api.github.com")


def fetch_assets(repo, tag):
    """读回 release 资产：这是远端事实。用 HTTP 而不是 gh --jq，因为 jq 的输出
    是按页 pretty-print 的多行文本，按行解析会炸。"""
    http = build_session()
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    base = f"{api_root()}/repos/{repo}"

    # 资产列表端点只认数字 release id；/releases/tags/<tag>/assets 是 404。
    response = http.get(f"{base}/releases/tags/{tag}", headers=headers, timeout=30)
    if response.status_code == 404:
        if not token:
            raise OSError(
                f"读不到 {repo} 的 release {tag}，而且没有 token："
                f"私有仓库会被 GitHub 报成 404，当成空 release 就会把 39 GB 全量重传。"
            )
        return {}
    response.raise_for_status()
    release_id = response.json()["id"]

    assets = {}
    page = 1
    while True:
        response = http.get(
            f"{base}/releases/{release_id}/assets",
            params={"per_page": 100, "page": page},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        assets.update(parse_assets([batch]))
        if len(batch) < 100:
            return assets
        page += 1


def fetch_ledger(repo, tags):
    """逐卷读资产再合成一本账：增量判断看的是"字节在不在 GitHub 上"，不是"在不在这个 tag 上"。"""
    return merge_ledgers([(tag, fetch_assets(repo, tag)) for tag in tags])


def release_exists(repo, tag):
    return subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo], capture_output=True, text=True
    ).returncode == 0


def ensure_release(repo, tag, target):
    """一个 release 由所有分片共享，首跑时 20 档会同时抢建：抢输的一方不能当成失败，
    否则整档 job 红掉、那一档的集永不入库。"""
    if release_exists(repo, tag):
        return
    try:
        gh(
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--target",
            target,
            "--title",
            f"This American Life 母带镜像 {tag}",
            "--notes",
            "原样字节。逐集 sha256 见 release-index.json；音源账本在 TMLT/docs/audio-manifest.json。",
        )
    except subprocess.CalledProcessError:
        if not release_exists(repo, tag):
            raise


def ensure_local(session, entry, path):
    """保证盘上有这一集的母带并返回 sha256；与清单基线不符就拒绝，绝不上传错字节。"""
    if path.exists() and path.stat().st_size == entry["size"]:
        digest = sha256_of(path)
    else:
        probe = Probe(entry["episode"], entry["url"], 200, entry["size"], None, None)
        written, digest = download(session, probe, path)
        if written != entry["size"]:
            raise OSError(f"下载 {written} 字节 != 清单 {entry['size']}")
    if entry.get("sha256") and digest != entry["sha256"]:
        raise OSError(f"本地 {digest[:12]} 与清单基线 {entry['sha256'][:12]} 不符")
    entry["sha256"] = digest
    return digest


def release_command(args):
    payload = load_manifest()
    if payload is None:
        sys.exit("先跑 manifest。")
    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)

    entries = [e for e in payload["episodes"] if e["status"] == 200 and e["plausible_master"]]
    if args.episodes:
        picked = set(args.episodes)
        entries = [e for e in entries if e["episode"] in picked]

    tags = split_tags(args.tag)
    if not tags:
        sys.exit("--tag 至少要有一卷。")
    head = tags[-1]  # 新集只往最后一卷传，旧卷从此不再写

    assets, conflicts = fetch_ledger(args.repo, tags)
    disputed = {name for name, _, _ in conflicts}
    todo = [
        e for e in entries
        if local_name(e["episode"]) not in disputed and needs_upload(e, assets)
    ]
    print(
        f"本轮 {len(entries)} | release 已有 {len(assets)}（{len(tags)} 卷） | 待传 {len(todo)} "
        f"| {sum(e['size'] for e in todo) / 1e9:.2f} GB"
    )
    failures = []
    for name, earlier, later in conflicts:
        failures.append((name, f"跨卷 digest 不一致：{earlier} 里的与 {later} 里的不同"))
        print(f"冲突 {name} 同时存在于 {earlier} 和 {later}，且字节不同：本轮不碰它")
    stop, note = capacity_check(tags, len(assets), len(todo))
    if note:
        print(note)
    if stop:
        return 1
    if args.no_push:
        print("--no-push：只下载与校验，不创建也不上传 release")
    else:
        ensure_release(args.repo, head, args.target)
    session = build_session()
    verified = 0
    for index, entry in enumerate(todo, 1):
        path = directory / local_name(entry["episode"])
        try:
            digest = ensure_local(session, entry, path)
        except (requests.RequestException, OSError) as exc:
            failures.append((entry["episode"], str(exc)[:100]))
            print(f"[{index}/{len(todo)}] {entry['episode']} 拒绝上传：{exc}")
            continue
        verified += 1
        if args.no_push:
            print(
                f"[{index}/{len(todo)}] {entry['episode']} "
                f"{entry['size'] / 1e6:.1f} MB 已校验 sha256={digest[:12]}"
            )
            continue
        try:
            gh("release", "upload", head, str(path), "--repo", args.repo, "--clobber")
        except subprocess.CalledProcessError as exc:
            failures.append((entry["episode"], f"上传失败 {exc.stderr.strip()[:100]}"))
            print(f"[{index}/{len(todo)}] {entry['episode']} 上传失败")
            continue
        print(f"[{index}/{len(todo)}] {entry['episode']} {entry['size'] / 1e6:.1f} MB sha256={digest[:12]} 已传")
        if index % 25 == 0:
            write_manifest(payload["episodes"])

    write_manifest(payload["episodes"])
    if args.no_push:
        print(f"本地校验 {verified} 集，sha256 已写回清单；release 未动")
        return 1 if failures else 0

    # 读回 GitHub 自己算的 digest：这才证明远端存的字节等于我下载的那份。
    # 只有本轮真的算过 sha256 的集才有基线可比：清单是本地账本，CI 不回写，
    # 把 skipped 的集当成"损坏"会把整轮刷红。
    remote, _ = fetch_ledger(args.repo, tags)
    matched = 0
    unchecked = 0
    records = []
    for entry in entries:
        asset = remote.get(local_name(entry["episode"]))
        if not asset:
            continue
        if entry.get("sha256") and asset["digest"] == entry["sha256"]:
            matched += 1
        elif entry.get("sha256"):
            failures.append((entry["episode"], f"远端 digest {asset['digest']} != {entry['sha256']}"))
        else:
            unchecked += 1
        records.append(
            {
                "episode": entry["episode"],
                "asset_id": asset["id"],
                "name": local_name(entry["episode"]),
                "size": asset["size"],
                "sha256": asset["digest"],
                # checked=true 才是"我们下过这份字节、GitHub 存的 digest 对得上"；
                # false 只有 GitHub 的单方说法。
                "checked": entry.get("sha256") == asset["digest"],
                "source": entry["url"],
                # 下载链接必须指向真正存着这份字节的那一卷。
                "volume": asset["tag"],
                "download": (
                    f"https://github.com/{args.repo}/releases/download/"
                    f"{asset['tag']}/{local_name(entry['episode'])}"
                ),
            }
        )

    index_path = directory / "release-index.json"
    index_path.write_text(
        json.dumps(
            {
                "version": MANIFEST_VERSION,
                "repo": args.repo,
                # 账本读过哪几卷；每条记录自己带 volume，取字节时认那一条。
                "volumes": tags,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "episodes": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"远端 digest 核对一致 {matched} | 本轮没基线可比 {unchecked} "
        f"| release 现有资产 {len(records)} | 索引 {index_path}"
    )
    if failures:
        print(f"异常 {len(failures)}：")
        for row in failures[:20]:
            print(f"  {row}")
        return 1
    return 0


def plan_command(args):
    """打印固定 20 项 JSON：每档一串集号，空档拿到空串自己退出。"""
    payload = load_manifest()
    if payload is None:
        sys.exit("先跑 manifest。")
    entries = [e for e in payload["episodes"] if e["status"] == 200 and e["plausible_master"]]
    if args.episodes:
        picked = set(args.episodes)
        entries = [e for e in entries if e["episode"] in picked]
    lanes = balance([(e["episode"], e["size"]) for e in entries], args.shards)
    try:
        padded = pad_lanes(lanes)
    except ValueError as exc:
        sys.exit(str(exc))
    sizes = [sum(e["size"] for e in entries if str(e["episode"]) in lane.split(","))
             for lane in padded]
    print(f"待镜像 {len(entries)} 集 / {sum(e['size'] for e in entries) / 1e9:.2f} GB，"
          f"{args.shards} 档各 GB: {[round(s / 1e9, 2) for s in sizes]}", file=sys.stderr)
    print(json.dumps(padded))


def verify_command(args):
    payload = load_manifest()
    if payload is None:
        sys.exit("先跑 manifest。")
    directory = Path(args.out)
    present = scan_local(directory)
    missing = []
    bad = []
    hashed = 0
    recorded = 0
    for entry in sorted(payload["episodes"], key=lambda e: e["episode"]):
        if entry["status"] != 200:
            continue
        episode = entry["episode"]
        if episode not in present:
            missing.append(episode)
            continue
        if present[episode] != entry["size"]:
            bad.append((episode, "字节数不符", present[episode], entry["size"]))
            continue
        digest = sha256_of(directory / local_name(episode))
        if entry.get("sha256") is None:
            # 清单重探过就没有基线了，这里把盘上的指纹补回去。
            entry["sha256"] = digest
            recorded += 1
            hashed += 1
        elif digest != entry["sha256"]:
            bad.append((episode, "sha256 不符", digest[:12], entry["sha256"][:12]))
        else:
            hashed += 1

    if recorded:
        write_manifest(payload["episodes"])

    print(f"完好 {hashed}（新补指纹 {recorded}）| 缺失 {len(missing)} | 损坏 {len(bad)}")
    if missing:
        print(f"  待下示例: {missing[:10]}")
    for row in bad:
        print(f"  损坏: {row}")
    # 还没下全不是损坏，冷备是分批跑的；退出码只代表盘上的东西坏没坏。
    return 1 if bad else 0


def check_command(args):
    """月度体检：只重发 HEAD，对比清单里的字节数与 ETag，发现漂移就报警。"""
    payload = load_manifest()
    if payload is None:
        sys.exit("先跑 manifest。")
    refs = [AudioRef(e["episode"], e["url"]) for e in payload["episodes"]]
    fresh = {p.episode: p for p in probe_all(refs, workers=args.workers)}
    drifted = []
    for entry in payload["episodes"]:
        probe = fresh.get(entry["episode"])
        if probe is None:
            continue
        if probe.status != entry["status"] or probe.size != entry["size"]:
            drifted.append(
                (entry["episode"], f"HTTP {entry['status']}/{entry['size']} -> "
                                   f"HTTP {probe.status}/{probe.size}")
            )
    print(f"漂移 {len(drifted)}")
    for row in drifted:
        print(f"  {row}")
    return 1 if drifted else 0


def comma_ints(value):
    return [int(x) for x in value.split(",") if x.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Archive 音频指纹清单与冷备")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("manifest", "解析 feed 并 HEAD 全部母带，写 docs/audio-manifest.json（新增失效 → 退出码 1）"),
        ("check", "重发 HEAD 与清单比对，报告链接漂移"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--workers", type=int, default=8)

    p = sub.add_parser("backup", help="按清单下载原样字节到冷备目录")
    p.add_argument("--out", required=True)
    p.add_argument("--episodes", type=comma_ints)

    p = sub.add_parser("plan", help="把待镜像的集号按字节摊成固定 20 档 JSON")
    p.add_argument("--shards", type=int, default=10)
    p.add_argument("--episodes", type=comma_ints)

    p = sub.add_parser("release", help="按清单上传原样母带到 release，并读回 digest 校验")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", required=True, help="保险库仓库 owner/name")
    p.add_argument(
        "--tag",
        default="audio-vault",
        help="逗号分隔的卷列表；新集只传到最后一卷，前面的卷仍算已备份",
    )
    p.add_argument("--target", default="audio", help="tag 落在哪条分支上")
    p.add_argument("--episodes", type=comma_ints)
    p.add_argument("--no-push", action="store_true", help="下载并校验，但不碰 release")

    p = sub.add_parser("verify", help="校验冷备目录的字节数与 sha256")
    p.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            sys.exit(manifest_command(args))
        elif args.command == "check":
            sys.exit(check_command(args))
        elif args.command == "backup":
            backup_command(args)
        elif args.command == "plan":
            plan_command(args)
        elif args.command == "release":
            sys.exit(release_command(args))
        elif args.command == "verify":
            sys.exit(verify_command(args))
    except ManifestCorrupt as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
