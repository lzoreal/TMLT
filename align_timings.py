#!/usr/bin/env python3
"""以官网 begin 为锚点、窗口化对齐校对过的 transcript，产出逐词时间轴 sidecar。

文本一个字节都不改：cue 的切分和文字保持原样，只补真实 end 和逐词时间。

整集单次 align 会崩：align() 内部先跑一遍 ASR 再 DTW，遇到几分钟的音乐/演唱段
时间戳不可信，会把后面所有 cue 一起拽走（实测漂移 -140 秒）。这里按官网 begin
把 cue 分组，每组只切自己那一小段音频（±3 秒余量）单独对齐，音乐段永远进不了
任何窗口。
"""

import argparse
import json
import os
import re
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import requests

BASE = Path(__file__).resolve().parent
TRANSCRIPT_DIR = BASE / "docs/transcripts"
TIMING_DIR = BASE / "docs/timings"
RSS_FILE = BASE / "docs/podcast.xml"
MODEL_DIR = BASE / "models/ct2-turbo"

SR = 16000

# 每组最多 900 字符 / 100 秒，保证窗口里只有干净语音。
MAX_CHARS = 900
MAX_SPAN = 100.0
PAD = 3.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)

BRACKET = re.compile(r"^\s*\[.*\]\s*$", re.DOTALL)
TIMESTAMP = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2,}):(\d{2}):(\d{2})\.(\d{3})"
)
VOICE = re.compile(r"^<v\s+([^>]+)>\s*(.*)$", re.DOTALL)
EPISODE_VTT = re.compile(r"/(\d+)\.vtt$")
TRANSCRIPT_PAGE = "https://www.thisamericanlife.org/{episode}/transcript"
PLAYLIST_DATA = re.compile(
    r'<script id="playlist-data"[^>]*>(.*?)</script>', re.DOTALL
)


def log(message=""):
    print(message, flush=True)


def human(seconds):
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


# ============================================================
# VTT
# ============================================================


def vtt_seconds(hours, minutes, seconds, millis):
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_vtt(path):
    """按出现顺序读出 cue；text 保持原样，只去掉 <v Speaker> 前缀。"""

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    cues = []
    i = 0
    while i < len(lines):
        match = TIMESTAMP.match(lines[i].strip())
        if not match:
            i += 1
            continue
        start = vtt_seconds(*match.groups()[:4])
        end = vtt_seconds(*match.groups()[4:])
        i += 1
        payload = []
        while i < len(lines) and lines[i].strip():
            payload.append(lines[i])
            i += 1
        raw = "\n".join(payload)
        voice = VOICE.match(raw)
        speaker = voice.group(1).strip() if voice else ""
        text = (voice.group(2) if voice else raw).strip()
        if not text:
            continue
        cues.append(
            {
                "i": len(cues),
                "start": start,
                "end": end,
                "speaker": speaker,
                "text": text,
            }
        )
    return cues


# ============================================================
# RSS: 集号 -> 音频地址
# ============================================================


def load_sources(rss_path):
    content = rss_path.read_text(encoding="utf-8")
    sources = {}
    for block in re.findall(r"<item>.*?</item>", content, re.S):
        transcript = re.search(r'<podcast:transcript[^>]*url="([^"]+)"', block, re.S)
        enclosure = re.search(r"<enclosure[^>]*?/?>", block, re.S)
        if not transcript or not enclosure:
            continue
        episode = EPISODE_VTT.search(transcript.group(1))
        audio = re.search(r'url="([^"]+)"', enclosure.group(0))
        if not episode or not audio:
            continue
        published = re.search(r"<pubDate>([^<]+)</pubDate>", block)
        key = int(episode.group(1))
        sources[key] = {
            "id": key,
            "audio": audio.group(1).replace("&amp;", "&"),
            "published": published.group(1) if published else "",
        }
    return sources


def select_episodes(sources, args):
    episodes = sorted(sources)
    if args.episodes:
        wanted = [int(x) for x in args.episodes.split(",") if x.strip()]
        episodes = [e for e in wanted if e in sources]
    elif args.latest:
        def when(episode):
            try:
                return parsedate_to_datetime(sources[episode]["published"])
            except Exception:
                return None

        dated = [e for e in episodes if when(e) is not None]
        episodes = sorted(dated, key=when, reverse=True)[: args.latest]
        episodes.sort()
    if args.shards > 1:
        episodes = [e for pos, e in enumerate(episodes) if pos % args.shards == args.shard]
    return episodes


# ============================================================
# 官网静态母带：archive（地区无关，无广告插入）
# ============================================================


def resolve_archive(session, episode, retries=3):
    """从 transcript 页的 playlist-data 取 archive；取不到返回 None。"""

    page = TRANSCRIPT_PAGE.format(episode=episode)
    for attempt in range(1, retries + 1):
        try:
            response = session.get(page, timeout=60)
            response.raise_for_status()
            match = PLAYLIST_DATA.search(response.text)
            if not match:
                raise ValueError("playlist-data not found")
            archive = json.loads(match.group(1)).get("archive")
            if not archive:
                raise ValueError("archive missing")
            return archive
        except Exception as exc:
            if attempt == retries:
                log(f"    #{episode} archive unavailable: {type(exc).__name__}: {exc}")
                return None
            time.sleep(5 * attempt)


# ============================================================
# 下载与解码
# ============================================================


def make_session(proxy):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def download(session, url, dest, retries=4):
    for attempt in range(1, retries + 1):
        try:
            with session.get(url, stream=True, timeout=180) as response:
                response.raise_for_status()
                expected = int(response.headers.get("Content-Length") or 0)
                part = dest.with_suffix(dest.suffix + ".part")
                received = 0
                with open(part, "wb") as file:
                    for chunk in response.iter_content(1 << 16):
                        if chunk:
                            file.write(chunk)
                            received += len(chunk)
                if expected and received != expected:
                    raise IOError(f"incomplete {received}/{expected} bytes")
                part.replace(dest)
                return received
        except Exception as exc:
            part = dest.with_suffix(dest.suffix + ".part")
            part.unlink(missing_ok=True)
            if attempt == retries:
                raise
            wait = 10 * attempt
            log(f"    download retry in {wait}s ({attempt}/{retries}): {exc}")
            time.sleep(wait)


def decode_audio(path):
    """PyAV 自带 ffmpeg，不需要系统装 ffmpeg。"""

    import av

    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype("float32")


# ============================================================
# 分组与对齐
# ============================================================


def group_cues(cues):
    groups = []
    current = []
    for cue in cues:
        if current:
            chars = sum(len(c["text"]) for c in current)
            span = cue["start"] - current[0]["start"]
            if chars + len(cue["text"]) > MAX_CHARS or span > MAX_SPAN:
                groups.append(current)
                current = []
        current.append(cue)
    if current:
        groups.append(current)
    return groups


def align_episode(model, audio, cues, total_seconds):
    """返回 {cue 序号: {"begin": 秒, "end": 秒, "words": [...]}}；非语音 cue 不出现。"""

    speech = [c for c in cues if not BRACKET.match(c["text"])]
    skipped = len(cues) - len(speech)
    results = {}
    failures = 0

    for group in group_cues(speech):
        lo = max(0.0, group[0]["start"] - PAD)
        hi = min(total_seconds, group[-1]["end"] + PAD)
        clip = audio[int(lo * SR): int(hi * SR)]
        text_block = "\n".join(c["text"] for c in group)
        try:
            aligned = model.align(clip, text_block, language="en", original_split=True)
        except Exception as exc:
            failures += len(group)
            log(f"    align failed on cue {group[0]['i']}-{group[-1]['i']}: "
                f"{type(exc).__name__}: {exc}")
            continue

        by_local_id = {segment.id: segment for segment in aligned.segments}
        for position, cue in enumerate(group):
            segment = by_local_id.get(position)
            words = list(segment.words) if segment is not None and segment.words else []
            if not words:
                failures += 1
                continue
            results[cue["i"]] = {
                "words": [
                    (w.word.strip(), w.start + lo, w.end + lo) for w in words
                ],
            }
    return results, skipped, failures


def percentile(values, q):
    values = sorted(values)
    if not values:
        return float("nan")
    return values[min(len(values) - 1, int(len(values) * q))]


# ============================================================
# 写 sidecar
# ============================================================


def write_sidecar(path, episode, cues, results, audio_seconds):
    segments = []
    for cue in cues:
        aligned = results.get(cue["i"])
        words = aligned["words"] if aligned else []
        start_ms = int(round(cue["start"] * 1000))
        if words:
            end_ms = int(round(words[-1][2] * 1000))
            word_payload = [
                [text, int(round(s * 1000)), int(round((e - s) * 1000))]
                for text, s, e in words
            ]
        else:
            end_ms = None
            word_payload = []
        segments.append(
            {
                "start": start_ms,
                "end": end_ms,
                "speaker": cue["speaker"],
                "text": cue["text"],
                "words": word_payload,
            }
        )

    document = {
        "version": 1,
        "episode": episode,
        "vtt": f"transcripts/{episode}.vtt",
        "audio_seconds": round(audio_seconds, 2),
        "segments": segments,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(path)


# ============================================================
# 单集
# ============================================================


def run_episode(model, session, source, args, workdir):
    episode = source["id"]
    vtt_path = TRANSCRIPT_DIR / f"{episode}.vtt"
    out_path = args.out / f"{episode}.json"

    if out_path.exists() and not args.force:
        log(f"#{episode} skip (already done)")
        return None

    cues = parse_vtt(vtt_path)
    if not cues:
        log(f"#{episode} skip (empty transcript)")
        return None

    if args.audio:
        audio_path = Path(args.audio)
    else:
        audio_path = workdir / f"{episode}_archive.mp3"
        source_url = None
        if not audio_path.exists():
            source_url = resolve_archive(session, episode)
            if source_url is None:
                audio_path = workdir / f"{episode}_rss.mp3"
                source_url = source["audio"]
                log(
                    f"#{episode} WARNING: archive unavailable, using RSS audio; "
                    "inserted breaks may shift the timeline"
                )
        if source_url is not None:
            started = time.time()
            size = download(session, source_url, audio_path)
            log(f"#{episode} downloaded {size / 1e6:.1f}MB in {time.time() - started:.0f}s")

    started = time.time()
    audio = decode_audio(audio_path)
    audio_seconds = audio.size / SR
    decode_seconds = time.time() - started

    align_started = time.time()
    results, skipped, failures = align_episode(model, audio, cues, audio_seconds)
    align_seconds = time.time() - align_started

    words = sum(len(r["words"]) for r in results.values())
    begin_errors = [
        results[c["i"]]["words"][0][1] - c["start"] for c in cues if c["i"] in results
    ]
    end_saved = sum(
        c["end"] - results[c["i"]]["words"][-1][2] for c in cues if c["i"] in results
    )

    write_sidecar(out_path, episode, cues, results, audio_seconds)

    try:
        shown = out_path.relative_to(BASE)
    except ValueError:
        shown = out_path

    log(
        f"#{episode} audio={human(audio_seconds)} ok={len(results)}/{len(cues) - skipped} "
        f"bracket={skipped} fail={failures} words={words} "
        f"decode={decode_seconds:.0f}s align={align_seconds:.0f}s "
        f"({audio_seconds / max(align_seconds, 1e-6):.2f}sec/s) "
        f"begin_err_p50={percentile(begin_errors, .5):+.2f}s "
        f"end_saved={end_saved:.0f}s -> {shown}"
    )

    if args.audio is None:
        audio_path.unlink(missing_ok=True)

    if failures:
        log(f"    {failures} cue 未对齐，保留原 end；下次运行会重试")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", help="逗号分隔的集号，优先于 --latest")
    parser.add_argument("--latest", type=int, default=0, help="按发布日期取最近 N 集")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--max-minutes", type=float, default=285.0, help="本进程时间预算")
    parser.add_argument("--model", default=str(MODEL_DIR))
    parser.add_argument("--out", default=str(TIMING_DIR))
    parser.add_argument("--audio", help="用本地音频代替下载（调试用）")
    parser.add_argument("--workdir", default="/tmp/tmlt-audio")
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="忽略已有结果重新对齐")
    args = parser.parse_args()

    args.out = Path(args.out)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    sources = load_sources(RSS_FILE)
    episodes = select_episodes(sources, args)
    pending = [e for e in episodes if args.force or not (args.out / f"{e}.json").exists()]
    log(f"shard {args.shard}/{args.shards}: {len(episodes)} 集待查, "
        f"{len(episodes) - len(pending)} 集已有结果, 本次处理 {len(pending)} 集")
    if not pending:
        log("nothing to do")
        return 0

    import stable_whisper as sw

    threads = args.cpu_threads or min(16, (os.cpu_count() or 4))
    started = time.time()
    model = sw.load_faster_whisper(
        args.model, device="cpu", compute_type="int8", cpu_threads=threads
    )
    log(f"model ready in {time.time() - started:.1f}s (cpu_threads={threads})")

    proxy = os.environ.get("TAL_PROXY_URL") or None
    if proxy:
        # 只在日志里露协议和主机，不打印凭据
        log(f"proxy configured: {re.sub(r'//[^@]*@', '//***@', proxy)}")
    else:
        log("no TAL_PROXY_URL: 直连（archive 是静态母带，不受地区广告插入影响）")
    session = make_session(proxy)

    deadline = time.time() + args.max_minutes * 60
    failures = 0
    done = 0
    for episode in pending:
        if time.time() > deadline:
            log(f"时间预算用完，剩 {len(pending) - done} 集留给下次运行")
            break
        try:
            if run_episode(model, session, sources[episode], args, workdir):
                failures += 1
        except Exception as exc:
            failures += 1
            log(f"#{episode} ERROR {type(exc).__name__}: {exc}")
        done += 1

    log(f"done {done} episodes, {failures} with failures, "
        f"elapsed {human(time.time() - started)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
