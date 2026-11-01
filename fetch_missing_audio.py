#!/usr/bin/env python3
"""官网母带已下线的集：从存档取回原始音频，落到 docs/audio/ 随 Pages 部署。

目前只有 #454（Mr. Daisey and the Apple Factory）：TAL 撤稿后删了母带，archive
地址返回 242 字节的 404 页面，RSS 里只剩撤稿声明 promo，音频保险库也缺这一集。
feed 里这集的 enclosure 会指向 Pages 上的副本，所以副本必须和原母带同一份录音。

来源按可信度排：
1. Wayback Machine 存的原 TAL 母带地址（原字节，最可信）；
2. Internet Archive 上的整集录音（按标题检索，用解码时长筛掉 promo/片段）。

不转码：存进去的就是下载到的字节，时间轴直接对着它算。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import align_timings as at

BASE = Path(__file__).resolve().parent
ROSTER_FILE = BASE / "docs/episodes.json"
MANIFEST_FILE = BASE / "docs/audio-manifest.json"
AUDIO_DIR = BASE / "docs/audio"

# 整集录音的下限/上限：TAL 单集 25–70 分钟，短于 15 分钟基本是 promo 或片段。
MIN_SECONDS = 900.0
MAX_SECONDS = 7200.0
# CDX 里 Content-Length 的过滤门槛：正式单集母带远大于 5 MB。
MIN_BYTES = 5 << 20

USER_AGENT = at.USER_AGENT


def log(message=""):
    at.log(message)


# ============================================================
# 候选筛选（纯逻辑，便于单测）
# ============================================================


def pick_cdx_entries(rows):
    """CDX 行 -> [(timestamp, url)]，新的在前。

    只留 200、audio/mpeg、够大的条目；同一个 URL 的多次快照取最新一份
    （母带地址带内容指纹，同一地址的各份快照就是同一份文件）。
    """

    best = {}
    for row in rows:
        if len(row) < 7 or row[0] == "urlkey":
            continue
        _, timestamp, original, mimetype, status, _digest, length = row[:7]
        if status != "200":
            continue
        # 老快照常把音频记成 application/octet-stream，只看 mimetype 会漏掉。
        looks_audio = ("audio" in mimetype
                       or (original.lower().endswith(".mp3")
                           and "html" not in mimetype))
        if not looks_audio:
            continue
        try:
            if int(length or 0) < MIN_BYTES:
                continue
        except ValueError:
            continue
        if original not in best or timestamp > best[original]:
            best[original] = timestamp
    return sorted(((timestamp, url) for url, timestamp in best.items()),
                  reverse=True)


def pick_archive_files(files):
    """IA metadata 的 files 列表 -> [(name, url_path)]，按时长筛选整集 mp3。"""

    picked = []
    for item in files or []:
        name = item.get("name") or ""
        if not name.lower().endswith(".mp3"):
            continue
        try:
            seconds = float(item.get("length") or 0)
        except (TypeError, ValueError):
            continue
        if not MIN_SECONDS <= seconds <= MAX_SECONDS:
            continue
        picked.append((name, item.get("url") or ""))
    return picked


def archive_item_candidates(items, title_words, episode=None):
    """按相关度排序，返回 [(identifier, score)]，最像的在前。

    IA 上同名条目可能是 Daisey 的舞台独角戏录音，不是 TAL 这期节目，所以
    至少要命中两个标题词，提到 "this american life" 的加分，标识/标题里带
    集号的再加 3 分（`ThisAmericanLife454…` 比 `thisamericanlife-misterdaisey`
    更确定就是这一期）。
    """

    number = str(episode) if episode else None
    scored = []
    for identifier, title, description in items:
        lowered = f"{identifier or ''} {title or ''} {description or ''}".lower()
        hits = sum(1 for word in title_words if word in lowered)
        if hits < 2 and "this american life" not in lowered:
            continue
        score = hits
        if "this american life" in lowered.replace("-", " "):
            score += 2
        if number and number in lowered:
            score += 3
        scored.append((score, identifier))
    scored.sort(reverse=True)
    return [(identifier, score) for score, identifier in scored]


# ============================================================
# 取回
# ============================================================


def get_json(session, url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=90)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt == retries:
                raise
            log(f"    {url} 失败（{type(exc).__name__}），{5 * attempt}s 后重试")
            time.sleep(5 * attempt)


def wayback_candidates(session, episode):
    prefix = f"thisamericanlife.org/sites/default/files/audio/{episode}/*"
    url = ("https://web.archive.org/cdx/search/cdx?url="
           + urllib.parse.quote(prefix, safe="")
           + "&output=json&collapse=digest&limit=200")
    rows = get_json(session, url)
    return [(f"wayback {timestamp}",
             f"https://web.archive.org/web/{timestamp}id_/{original}")
            for timestamp, original in pick_cdx_entries(rows)]


def archive_org_candidates(session, episode, title):
    words = [w.strip(".,'\"") for w in title.lower().replace(":", " ").split()]
    words = [w for w in words if len(w) > 3 and w not in ("this", "american", "life")]
    query = urllib.parse.quote(f'"{title}" OR (daisey AND apple AND factory)')
    search = ("https://archive.org/advancedsearch.php?q=" + query
              + "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=description"
              + "&rows=100&output=json")
    document = get_json(session, search)
    items = [(doc.get("identifier"), doc.get("title"), doc.get("description"))
             for doc in document.get("response", {}).get("docs", [])]
    candidates = []
    for identifier, score in archive_item_candidates(items, words, episode):
        log(f"    IA 候选 {identifier}（相关度 {score}）")
        try:
            metadata = get_json(session, f"https://archive.org/metadata/{identifier}")
        except Exception as exc:
            log(f"    metadata {identifier} 失败：{type(exc).__name__}")
            continue
        for name, _path in pick_archive_files(metadata.get("files")):
            # metadata 的 url 字段是没带协议的节点路径（dn601707.us.archive.org/…），
            # 直接用会 MissingSchema；archive.org/download 会自己重定向到数据节点。
            url = (f"https://archive.org/download/{identifier}/"
                   + urllib.parse.quote(name))
            candidates.append((f"archive.org {identifier}/{name}", url))
    return candidates


def probe_duration(path):
    import av

    with av.open(str(path), metadata_errors="ignore") as container:
        stream = container.streams.audio[0]
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
    return 0.0


def try_download(session, label, url, target):
    started = time.time()
    try:
        size = at.download(session, url, target)
        seconds = probe_duration(target)
    except Exception as exc:
        log(f"    {label} 失败：{type(exc).__name__}: {exc}")
        target.unlink(missing_ok=True)
        return None
    log(f"    {label}：{size / 1e6:.1f} MB / {seconds / 60:.1f} 分钟，"
        f"用时 {time.time() - started:.0f}s")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        log("    时长不像整集，丢弃")
        target.unlink(missing_ok=True)
        return None
    return seconds


def commit(paths, episode):
    if os.environ.get("COMMIT_RESULTS") != "true":
        return
    rels = [p.resolve().relative_to(BASE.resolve()).as_posix() for p in paths]

    def git(*args):
        return subprocess.run(("git", *args), cwd=BASE, capture_output=True, text=True)

    if git("add", "--", *rels).returncode != 0:
        return
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    if git("commit", "-m", f"Add archived master audio for #{episode}").returncode != 0:
        return
    for attempt in range(1, 4):
        if (git("pull", "--rebase", "--autostash").returncode == 0
                and git("push").returncode == 0):
            log("    已提交并推送")
            return
        git("rebase", "--abort")
        time.sleep(5 * attempt)
    log("    push 失败，结果留在本地提交里")


def fetch(session, episode, title, args):
    target = args.out / f"{episode}.mp3"
    if target.is_file():
        seconds = probe_duration(target)
        if MIN_SECONDS <= seconds <= MAX_SECONDS:
            log(f"#{episode} 已有音频 {seconds / 60:.1f} 分钟，跳过")
            return True
        log(f"#{episode} 已有音频时长异常（{seconds:.0f}s），重新取")
        target.unlink()

    staged = target.with_suffix(".mp3.part")
    sources = []
    for builder in (lambda: wayback_candidates(session, episode),
                    lambda: archive_org_candidates(session, episode, title)):
        try:
            sources.extend(builder())
        except Exception as exc:
            log(f"    候选来源失败：{type(exc).__name__}: {exc}")
    if not sources:
        log(f"#{episode} 没找到候选音频")
        return False

    for label, url in sources[:args.tries]:
        log(f"  尝试 {label}")
        seconds = try_download(session, label, url, staged)
        if seconds:
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
            log(f"#{episode} 采用 {label}（{seconds / 60:.1f} 分钟）")
            manifest_path = args.manifest
            entry = {
                "episode": episode, "file": target.name, "url": url,
                "status": 200, "size": target.stat().st_size,
                "audio_seconds": round(seconds, 2),
                "plausible_master": True,
                "source": label,
            }
            append_manifest(manifest_path, entry)
            commit([target, manifest_path], episode)
            return True
    log(f"#{episode} 候选全部不可用")
    return False


def append_manifest(path, entry):
    """把取回的副本登记进母带清单，之后的听写与审计都以它为准。"""

    document = json.loads(path.read_text(encoding="utf-8"))
    episodes = [e for e in document.get("episodes", [])
                if int(e.get("episode", -1)) != entry["episode"]]
    episodes.append(entry)
    episodes.sort(key=lambda e: int(e["episode"]))
    document["episodes"] = episodes
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(
        description="从存档取回已下线母带，落到 docs/audio/")
    parser.add_argument("--episodes", required=True, help="逗号分隔的集号")
    parser.add_argument("--out", type=Path, default=AUDIO_DIR)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--tries", type=int, default=6, help="最多试几个候选")
    args = parser.parse_args()

    roster = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
    session = at.make_session(os.environ.get("TAL_PROXY_URL") or None)
    args.out.mkdir(parents=True, exist_ok=True)

    failed = 0
    wanted = [e.strip() for e in args.episodes.split(",") if e.strip()]
    for episode in wanted:
        info = roster.get(episode) or {}
        title = str(info.get("title") or "").split(":", 1)[-1].strip()
        if not title:
            log(f"#{episode} 在 episodes.json 里没有标题")
            failed += 1
            continue
        try:
            ok = fetch(session, int(episode), title, args)
        except Exception as exc:
            log(f"#{episode} ERROR {type(exc).__name__}: {exc}")
            ok = False
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
