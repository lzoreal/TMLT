#!/usr/bin/env python3
"""以官网 begin 为锚点、窗口化对齐校对过的 transcript，产出逐词时间轴 sidecar。

文本一个字节都不改：cue 的切分和文字保持原样，只补真实 end 和逐词时间。
sidecar 记录 complete/attempts：少量缺口视为完成；对齐不上的（GIVE_UP_REASONS）
一次判定就放弃，其余未完成的最多重试 MAX_ATTEMPTS 次，避免确定性失败每轮重跑。

整集单次 align 会崩：align() 内部先跑一遍 ASR 再 DTW，遇到几分钟的音乐/演唱段
时间戳不可信，会把后面所有 cue 一起拽走（实测漂移 -140 秒）。这里按官网 begin
把 cue 分组，每组只切自己那一小段音频（±3 秒余量）单独对齐，音乐段永远进不了
任何窗口。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

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

# 音频版本守卫：RSS/播放器音频按地区插播广告，相对母版偏移可达 +25~+97 秒。
# 窗口是按官网 begin 切的，版本不对时首词会成片偏出去，中位偏差立刻暴露。
AUDIO_MISMATCH_SECONDS = 3.0

# 单句合理性：对齐偶尔会把某句贴到窗口边缘（词整体落在本句 begin 之前，
# 或越过下句 begin 太多）。这类 cue 不写逐词，end 置空由消费方回退到 VTT。
MAX_EARLY = 8.0
MAX_LATE = 8.0

# 官网 end 是合成的：标签 cue 能虚报几十秒，片尾字幕又会短于真实语音。
# 末句窗口按文本长度估算语音时长，并额外留出尾量，避免截尾或超长窗口。
NOMINAL_CHARS_PER_SECOND = 15.0
SLOWEST_CHARS_PER_SECOND = 4.0
TAIL_PAD = 8.0

# 母带与稿件在段落交界处有几秒级的内容差（剪辑、垫乐长短不同），表现为阶梯状平移：
# 实测 #477 在 23 分钟处跳 +5 秒、30 分钟到 +8 秒、35 分钟回落 +5 秒，每段内部
# 稳定在 ±0.3 秒。窗口留了 -3/+8 秒余量，第一遍对齐本来就落在正确位置，错的是
# "拿官网 begin 当期望起点"这一步。所以按邻域实测偏移重算期望值，不重跑对齐。
LOCAL_SHIFT_WINDOW = 45.0
MIN_LOCAL_SAMPLES = 3
# 超过这个量级说明第一遍的窗口根本兜不住（RSS 插播广告是 +25~+97 秒），
# 实测值也不可信，仍然整集拒绝。
MAX_LOCAL_SHIFT = 15.0

# 未完成的 sidecar 最多自动重试次数；超过即放弃，避免确定性失败每轮重跑。
MAX_ATTEMPTS = 2

# 这两类拒绝由音频母带本身决定（对齐落到错误位置、母带与 transcript 不同版），
# 同一份音频重跑必然同样失败，所以一次判定后不再占用重试机会：
# 单次尝试在 CI 上要 30-50 分钟，重试等于白烧一个机时。
GIVE_UP_REASONS = frozenset({"audio_mismatch", "high_drop_rate"})

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)

BRACKET = re.compile(r"^\s*\[.*\]\s*$", re.DOTALL)
# 定位用的词元：小写字母数字加撇号，标点不参与匹配。
WORD_TOKEN = re.compile(r"[a-z0-9']+")
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
    import requests

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
    import numpy as np

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


def cue_span(cue):
    """本句语音时长的合理上界。

    官网 end 是合成的：标签 cue 能虚报几十秒，片尾字幕又会短于真实语音。
    取声明时长与按文本长度估算的区间交集，避免单句把窗口撑爆或截断。
    """

    declared = max(0.0, cue["end"] - cue["start"])
    natural = len(cue["text"]) / NOMINAL_CHARS_PER_SECOND
    ceiling = len(cue["text"]) / SLOWEST_CHARS_PER_SECOND + 5.0
    return min(max(declared, natural), ceiling)


def group_cues(cues):
    groups = []
    current = []
    for cue in cues:
        if current:
            chars = sum(len(c["text"]) for c in current)
            cue_end = max(cue["start"], cue["end"])
            span = cue_end - current[0]["start"]
            if chars + len(cue["text"]) > MAX_CHARS or span > MAX_SPAN:
                groups.append(current)
                current = []
        current.append(cue)
    if current:
        groups.append(current)
    return groups


def align_group(model, audio, group, total_seconds, shift=0.0):
    """对一个 cue 组切窗、对齐，返回 {cue 序号: {"words": [...]}}。

    shift 是这一组相对官网 begin 的实测位移：母带与稿件在段界处有几秒差，
    窗口和合理带都要跟着挪，否则 DTW 会对着错误的音频硬贴。
    """

    lo = max(0.0, group[0]["start"] + shift - PAD)
    # 不能只看最后一句：cue 时序会重叠，前一句的长台词可能延伸到后面。
    hi = min(total_seconds, max(
        c["start"] + cue_span(c) for c in group
    ) + shift + TAIL_PAD)
    clip = audio[int(lo * SR): int(hi * SR)]
    text_block = "\n".join(c["text"] for c in group)
    aligned = model.align(clip, text_block, language="en", original_split=True)

    by_local_id = {segment.id: segment for segment in aligned.segments}
    results = {}
    for position, cue in enumerate(group):
        segment = by_local_id.get(position)
        words = list(segment.words) if segment is not None and segment.words else []
        if not words:
            continue
        results[cue["i"]] = {
            "words": [
                (w.word.strip(), w.start + lo, w.end + lo) for w in words
            ],
        }
    return results


def align_episode(model, audio, cues, total_seconds, shifts=None):
    """返回 {cue 序号: {"begin": 秒, "end": 秒, "words": [...]}}；非语音 cue 不出现。"""

    shifts = shifts or {}
    speech = [c for c in cues if not BRACKET.match(c["text"])]
    skipped = len(cues) - len(speech)
    results = {}
    failures = 0

    for group in group_cues(speech):
        try:
            aligned = align_group(model, audio, group, total_seconds,
                                  shifts.get(group[0]["i"], 0.0))
        except Exception as exc:
            failures += len(group)
            log(f"    align failed on cue {group[0]['i']}-{group[-1]['i']}: "
                f"{type(exc).__name__}: {exc}")
            continue
        results.update(aligned)
        failures += len(group) - len(aligned)
    return results, skipped, failures


def probe_offsets(model, audio, cues, total_seconds, samples):
    """对整集均匀采样 samples 段做同样窗口对齐，返回每段的偏移统计。

    用来判断被拒集的偏移形状：全段相等=官网时间戳整体平移，单调递增=线性漂移，
    中途断层=少/多一段内容。只读：不写 sidecar、不提交。
    """

    speech = [c for c in cues if not BRACKET.match(c["text"])]
    groups = group_cues(speech)
    if not groups:
        return []

    count = max(1, min(samples, len(groups)))
    if count == 1:
        picks = [groups[0]]
    else:
        span = len(groups) - 1
        picks = [groups[int(round(k * span / (count - 1)))] for k in range(count)]

    rows = []
    for k, group in enumerate(picks):
        t = group[0]["start"]
        try:
            results = align_group(model, audio, group, total_seconds)
        except Exception as exc:
            rows.append((k + 1, t, None, f"{type(exc).__name__}"))
            continue
        errors = sorted(
            results[c["i"]]["words"][0][1] - c["start"]
            for c in group if c["i"] in results
        )
        if not errors:
            rows.append((k + 1, t, None, "无对齐结果"))
            continue
        rows.append((
            k + 1, t, len(errors),
            f"median={percentile(errors, .5):+7.2f}s p10={percentile(errors, .1):+7.2f}s "
            f"p90={percentile(errors, .9):+7.2f}s max_abs={max(abs(e) for e in errors):5.1f}s",
        ))
    return rows


def percentile(values, q):
    values = sorted(values)
    if not values:
        return float("nan")
    return values[min(len(values) - 1, int(len(values) * q))]


def local_shifts(cues, results):
    """返回 (shifts, untrusted)：每个 cue 的本地平移，以及实测不可信的 cue。

    官网 end 是合成的、常虚报几十秒，group_cues 因此切出大量 1-3 句的小组
    （实测 119 个采样里 73 个只有 2-3 句），用组中位数噪声太大、跨段界还会
    借错邻组。所以按"时间邻域内其他句的实测偏移中位数"来定，邻句不足就把
    窗口逐级放宽，仍不够才退回全集中位数。

    留一法（不含本句）是关键：期望值不能由被测样本自己决定，
    否则散乱的错误对齐会把自己解释成零残差。
    """

    samples = sorted(
        (c["start"], c["i"], results[c["i"]]["words"][0][1] - c["start"])
        for c in cues if c["i"] in results
    )
    if len(samples) < MIN_LOCAL_SAMPLES + 1:
        return {}, set()

    shifts = {}
    untrusted = set()
    for cue in cues:
        if cue["i"] not in results:
            continue
        window = LOCAL_SHIFT_WINDOW
        near = []
        while True:
            near = [v for t, i, v in samples
                    if i != cue["i"] and abs(t - cue["start"]) <= window]
            if len(near) >= MIN_LOCAL_SAMPLES or window >= 8 * LOCAL_SHIFT_WINDOW:
                break
            window *= 2
        if not near:
            continue
        shift = percentile(near, .5)
        if abs(shift) > MAX_LOCAL_SHIFT:
            untrusted.add(cue["i"])
            continue
        shifts[cue["i"]] = shift
    return shifts, untrusted


# ============================================================
# 写 sidecar
# ============================================================


def locate_cues(model, audio, cues, min_ratio=0.5, window=400, max_window=3000):
    """整集粗 ASR 一遍，把每句校对稿按词元匹配钉到真实位置。

    只用来定位：ASR 自己的文字不参与输出，校对稿一个字节都不改。
    匹配用 difflib 找"最长公共片段"，比逐词前缀相等宽容——ASR 与校对稿常在
    数字、缩写、口语写法上不一致，逐词全等只能匹配到 43%（实测 #192）。
    返回 (anchors, 覆盖率)：anchors 是 {cue 序号: 该句首词的真实起点}。
    """

    started = time.time()
    result = model.transcribe(audio, language="en", word_timestamps=True)
    tokens = []
    for segment in result.segments:
        for word in segment.words or []:
            for token in WORD_TOKEN.findall(word.word.lower()):
                tokens.append((token, float(word.start)))
    if not tokens:
        return {}, 0.0
    log(f"    ASR 定位：{len(tokens)} 词，用时 {time.time() - started:.0f}s")

    import difflib

    anchors = {}
    pointer = 0
    for cue in cues:
        wanted = WORD_TOKEN.findall(cue["text"].lower())
        if not wanted:
            continue
        best_score, best_pos = 0.0, None
        for size in (window, max_window):
            end = min(len(tokens), pointer + size)
            if end <= pointer:
                break
            asr = [token for token, _ in tokens[pointer:end]]
            matcher = difflib.SequenceMatcher(None, wanted, asr, autojunk=False)
            blocks = [b for b in matcher.get_matching_blocks() if b.size >= 3]
            if not blocks:
                continue
            block = max(blocks, key=lambda b: b.size)
            score = block.size / len(wanted)
            if score > best_score:
                # 命中段之前的词按每词 0.4 秒倒推，首词位置才能对上
                start = pointer + block.b - block.a
                best_score, best_pos = score, max(pointer, start)
            if best_score >= 0.7:
                break
        if best_pos is not None and best_score >= min_ratio:
            anchors[cue["i"]] = tokens[best_pos][1]
            pointer = best_pos

    sought = sum(1 for c in cues if WORD_TOKEN.search(c["text"].lower()))
    coverage = len(anchors) / sought if sought else 0.0
    log(f"    ASR 定位：{len(anchors)}/{sought} 句匹配成功（{coverage:.0%}），"
        f"总用时 {time.time() - started:.0f}s")
    return anchors, coverage


def realign_shifted(model, audio, cues, total_seconds, results, shifts,
                    min_shift=2.0):
    """按定位出的位移重切窗口，只重跑位移明显的组；返回重跑的组数。"""

    speech = [c for c in cues if not BRACKET.match(c["text"])]
    redone = 0
    for group in group_cues(speech):
        shift = shifts.get(group[0]["i"], 0.0)
        if abs(shift) < min_shift or abs(shift) > MAX_LOCAL_SHIFT:
            continue
        try:
            aligned = align_group(model, audio, group, total_seconds, shift)
        except Exception as exc:
            log(f"    realign failed on cue {group[0]['i']}: {type(exc).__name__}")
            continue
        for cue in group:
            if cue["i"] in aligned:
                results[cue["i"]] = aligned[cue["i"]]
        redone += 1
    return redone


def judge(cues, results, shifts):
    """按平移重算残差、写 segments；返回 (残差中位, segments, dropped)。"""

    residuals = [
        results[c["i"]]["words"][0][1] - (c["start"] + shifts.get(c["i"], 0.0))
        for c in cues if c["i"] in results
    ]
    median_abs = percentile([abs(e) for e in residuals], 0.5) if residuals else float("nan")
    segments, dropped = build_segments(cues, results, shifts)
    return median_abs, segments, dropped


def plausible_words(cue, next_start, words, expected=None):
    """词集与词位置是否可信；不可信时调用方丢弃逐词而不是写错的数据。

    expected 是本地平移后的期望起点（母带与稿件在段界处有几秒差），
    缺省退回官网 begin。
    """

    if not words:
        return False

    cue_start = cue["start"] if expected is None else expected
    first = words[0][1]
    last = words[-1][2]

    # 整句被压成 instant，或末词落在窗口截断点，都没有可用的逐词信息。
    if last - first <= 0:
        return False
    if words[-1][2] - words[-1][1] <= 0:
        return False
    # 对齐是库对给定文本做的；词表一旦和原文不一致（常见为重复末词），整句不可信。
    if [w[0] for w in words] != cue["text"].split():
        return False
    if last < cue_start + 0.2:
        return False
    if first < cue_start - MAX_EARLY:
        return False
    if first > cue_start + MAX_EARLY:
        return False
    if next_start is not None:
        if first > next_start + 0.5:
            return False
        if last > next_start + MAX_LATE:
            return False
    return True


def build_segments(cues, results, shifts=None):
    """返回 (segments, dropped)；dropped 只统计有词但被判定不可信的 cue。"""

    shifts = shifts or {}
    segments = []
    dropped = 0
    for index, cue in enumerate(cues):
        aligned = results.get(cue["i"])
        words = aligned["words"] if aligned else []
        expected = cue["start"] + shifts.get(cue["i"], 0.0)
        next_cue = cues[index + 1] if index + 1 < len(cues) else None
        next_start = (next_cue["start"] + shifts.get(next_cue["i"], 0.0)
                      if next_cue is not None else None)
        if words and not plausible_words(cue, next_start, words, expected):
            words = []
            dropped += 1
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
    return segments, dropped


def write_document(path, episode, audio_seconds, segments, attempts,
                   complete=False, rejected=None):
    document = {
        "version": 1,
        "episode": episode,
        "vtt": f"transcripts/{episode}.vtt",
        "audio_seconds": round(audio_seconds, 2),
        "complete": complete,
        "attempts": attempts,
        "segments": segments,
    }
    if rejected:
        document["rejected"] = rejected

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(path)


def mark_attempt(path, episode, attempts, rejected):
    """保留已有 segments，只更新尝试次数/拒绝原因（用于无法重新对齐的集）。"""

    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    if not isinstance(old, dict):
        old = {}
    write_document(
        path,
        episode,
        float(old.get("audio_seconds") or 0.0),
        old.get("segments") or [],
        attempts,
        rejected=rejected,
    )


def read_status(path):
    """返回 (complete, attempts, rejected)；缺失或损坏按未完成处理。"""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return False, 0, ""
        attempts = int(document.get("attempts") or 0)
    except Exception:
        return False, 0, ""
    return (
        document.get("complete") is True,
        attempts,
        str(document.get("rejected") or ""),
    )


def status_is_done(complete, attempts, rejected):
    return complete or rejected in GIVE_UP_REASONS or attempts >= MAX_ATTEMPTS


def is_done(path):
    return status_is_done(*read_status(path))


def build_plan(pending, shards):
    """把当前待办按轮转摊成 shards 份集号表，供 check job 一次算完、各档取自己那份。

    静态 pos % shards 切片会让先跑完的一档永久空转、慢档被钉在同一批剩余集上；
    计划每轮重算，档位拿到的是别人的 leftovers。一份集号只出现在一个档位里，
    所以同轮内仍然互斥，不会撞 rebase。
    """
    buckets = [[] for _ in range(shards)]
    for index, episode in enumerate(pending):
        buckets[index % shards].append(str(episode))
    return [",".join(bucket) for bucket in buckets]


def persist_progress(path, episode):
    """把刚写完的 sidecar 立刻提交并推送。

    超时或 runner 被回收时，最多只丢正在处理的那一集；已完成的结果
    不会等到 40 分钟分段提交才落盘。
    """

    if os.environ.get("COMMIT_RESULTS") != "true":
        return
    try:
        rel = path.resolve().relative_to(BASE.resolve()).as_posix()
    except ValueError:
        return

    def git(*args):
        return subprocess.run(
            ("git", *args), cwd=BASE, capture_output=True, text=True
        )

    def push():
        for attempt in range(1, 3):
            if (git("pull", "--rebase", "--autostash").returncode == 0
                    and git("push").returncode == 0):
                return True
            git("rebase", "--abort")
            time.sleep(5 * attempt)
        return False

    def ahead():
        result = git("rev-list", "--count", "@{u}..HEAD")
        return result.returncode == 0 and result.stdout.strip() not in ("", "0")

    if git("add", "--", rel).returncode != 0:
        return
    if git("diff", "--cached", "--quiet").returncode == 0:
        # 没有新文件，但可能有上次 push 失败留下的本地提交
        if ahead():
            push()
        return
    shard = os.environ.get("SHARD", "?")
    if git("commit", "-m",
           f"Add word-level timings (shard {shard}, #{episode})").returncode != 0:
        return
    if not push():
        log(f"    #{episode} sidecar committed locally; push will retry")


# ============================================================
# 单集
# ============================================================


def probe_episode(model, session, source, args, workdir, tag=""):
    """诊断：采样对齐若干段并打印偏移曲线，不写 sidecar。"""

    episode = source["id"]
    cues = parse_vtt(TRANSCRIPT_DIR / f"{episode}.vtt")
    if not cues:
        log(f"{tag}#{episode} 稿件为空，跳过")
        return

    if args.audio:
        audio_path = Path(args.audio)
    else:
        audio_path = workdir / f"{episode}_archive.mp3"
        if not audio_path.exists():
            source_url = resolve_archive(session, episode)
            if source_url is None:
                log(f"{tag}#{episode} 取不到 archive，跳过")
                return
            download(session, source_url, audio_path)

    audio = decode_audio(audio_path)
    total_seconds = audio.size / SR
    log(f"{tag}#{episode} probe: 音频 {human(total_seconds)}，采样 {args.offset_probe} 段")
    for k, t, n, detail in probe_offsets(model, audio, cues, total_seconds,
                                         args.offset_probe):
        if n is None:
            log(f"{tag}  probe {k} t={human(t):>7} {detail}")
        else:
            log(f"{tag}  probe {k} t={human(t):>7} n={n:>2} {detail}")

    if args.audio is None:
        audio_path.unlink(missing_ok=True)


def locate_only(model, session, source, args, workdir, tag=""):
    """诊断：只做整集 ASR 定位并打印覆盖率与偏移分布，不对齐、不写 sidecar。"""

    episode = source["id"]
    cues = parse_vtt(TRANSCRIPT_DIR / f"{episode}.vtt")
    if not cues:
        log(f"{tag}#{episode} 稿件为空")
        return

    if args.audio:
        audio_path = Path(args.audio)
    else:
        audio_path = workdir / f"{episode}_archive.mp3"
        if not audio_path.exists():
            source_url = resolve_archive(session, episode)
            if source_url is None:
                log(f"{tag}#{episode} 取不到 archive")
                return
            download(session, source_url, audio_path)

    audio = decode_audio(audio_path)
    anchors, coverage = locate_cues(model, audio, cues)
    offsets = sorted(anchors[c["i"]] - c["start"] for c in cues if c["i"] in anchors)
    if offsets:
        far = sum(1 for v in offsets if abs(v) > MAX_LOCAL_SHIFT)
        log(f"{tag}#{episode} 覆盖 {coverage:.0%}，偏移 p10={percentile(offsets, .1):+.1f}s "
            f"p50={percentile(offsets, .5):+.1f}s p90={percentile(offsets, .9):+.1f}s "
            f"范围 {offsets[0]:+.1f}…{offsets[-1]:+.1f}s，超 {MAX_LOCAL_SHIFT:.0f}s 的 {far} 句")
    if args.audio is None:
        audio_path.unlink(missing_ok=True)


def run_episode(model, session, source, args, workdir, tag=""):
    episode = source["id"]
    vtt_path = TRANSCRIPT_DIR / f"{episode}.vtt"
    out_path = args.out / f"{episode}.json"

    prev_attempts = 0
    if out_path.exists():
        complete, prev_attempts, rejected = read_status(out_path)
        if not args.force:
            if complete:
                log(f"{tag}#{episode} skip (already done)")
                return None
            if rejected in GIVE_UP_REASONS:
                log(f"{tag}#{episode} skip (rejected={rejected}, 确定性失败，不再重试)")
                return None
            if prev_attempts >= MAX_ATTEMPTS:
                suffix = f", rejected={rejected}" if rejected else ""
                log(f"{tag}#{episode} skip (gave up after {prev_attempts} attempts{suffix})")
                return None
            log(f"{tag}#{episode} retry incomplete "
                f"(attempt {prev_attempts + 1}/{MAX_ATTEMPTS})")
    attempts = prev_attempts + 1

    cues = parse_vtt(vtt_path)
    if not cues:
        mark_attempt(out_path, episode, attempts, "empty_transcript")
        log(f"{tag}#{episode} skip (empty transcript)")
        return 1

    if args.audio:
        audio_path = Path(args.audio)
    else:
        audio_path = workdir / f"{episode}_archive.mp3"
        source_url = None
        if not audio_path.exists():
            source_url = resolve_archive(session, episode)
            if source_url is None:
                if not args.allow_rss_audio:
                    mark_attempt(out_path, episode, attempts, "no_archive")
                    log(f"{tag}#{episode} REJECT no archive audio; skip "
                        "(RSS 音频按地区插播，时间轴不可信；确需回退加 --allow-rss-audio)")
                    return 1
                audio_path = workdir / f"{episode}_rss.mp3"
                source_url = source["audio"]
                log(
                    f"{tag}#{episode} WARNING: archive unavailable, using RSS audio; "
                    "inserted breaks may shift the timeline"
                )
        if source_url is not None:
            started = time.time()
            size = download(session, source_url, audio_path)
            log(f"{tag}#{episode} downloaded {size / 1e6:.1f}MB in {time.time() - started:.0f}s")

    started = time.time()
    audio = decode_audio(audio_path)
    audio_seconds = audio.size / SR
    decode_seconds = time.time() - started

    align_started = time.time()
    located = {}
    if args.locate_first:
        anchors, coverage = locate_cues(model, audio, cues)
        if coverage >= 0.6:
            located = {c["i"]: anchors[c["i"]] - c["start"]
                       for c in cues if c["i"] in anchors}
            log(f"{tag}#{episode} 先定位再对齐：覆盖 {coverage:.0%}，"
                f"平移 {min(located.values()):+.1f}…{max(located.values()):+.1f}s")
        else:
            log(f"{tag}#{episode} 定位覆盖率 {coverage:.0%}，退回官网时间轴")
    results, skipped, failures = align_episode(model, audio, cues, audio_seconds, located)
    align_seconds = time.time() - align_started

    words = sum(len(r["words"]) for r in results.values())
    begin_errors = [
        results[c["i"]]["words"][0][1] - c["start"] for c in cues if c["i"] in results
    ]
    end_saved = sum(
        c["end"] - results[c["i"]]["words"][-1][2] for c in cues if c["i"] in results
    )

    if args.debug_errors and begin_errors:
        pairs = [
            (c["i"], c["start"], results[c["i"]]["words"][0][1] - c["start"])
            for c in cues
            if c["i"] in results
        ]
        step = max(1, len(pairs) // 4)
        for q in range(4):
            part = pairs[q * step:(q + 1) * step] if q < 3 else pairs[3 * step:]
            if not part:
                continue
            vals = [e for _, _, e in part]
            log(f"{tag}  q{q + 1} t={human(part[0][1])}-{human(part[-1][1])} n={len(vals)} "
                f"median={percentile(vals, .5):+.2f}s "
                f"p10={percentile(vals, .1):+.2f}s p90={percentile(vals, .9):+.2f}s")
        worst = sorted(pairs, key=lambda x: -abs(x[2]))[:6]
        log(f"{tag}  worst: " + ", ".join(
            f"cue{i}@{human(t)} {e:+.1f}s" for i, t, e in worst))

    # 母带与稿件在段界处有几秒差，先按邻域实测偏移重算期望起点，
    # 再看残差是"对齐不稳"还是"官网 begin 不准"。
    if located:
        # 已经按 ASR 定位对齐过，定位结果就是期望起点，不再从残差反推。
        shifts, untrusted = dict(located), set()
    else:
        shifts, untrusted = local_shifts(cues, results)
    median_abs, segments, dropped = judge(cues, results, shifts)
    raw_median = percentile([abs(e) for e in begin_errors], 0.5) if begin_errors else float("nan")

    if shifts and median_abs > AUDIO_MISMATCH_SECONDS:
        log(f"{tag}#{episode} 局部平移 {min(shifts.values()):+.1f}s…{max(shifts.values()):+.1f}s "
            f"（原始中位 {raw_median:.1f}s → 校正后 {median_abs:.1f}s）")
    if untrusted:
        log(f"{tag}#{episode} {len(untrusted)} 句所在组实测平移超 {MAX_LOCAL_SHIFT:.0f}s，"
            "按不可信丢弃")

    # 第一遍按官网时间轴 + 邻域平移仍判不过：整集粗 ASR 一遍，把每句钉到真实位置，
    # 再按定位出的位移重切窗口。ASR 的文字不参与输出，校对稿一个字节都不改。
    if (not located
            and (median_abs > AUDIO_MISMATCH_SECONDS
                 or (results and dropped > 0.10 * len(results)))):
        anchors, coverage = locate_cues(model, audio, cues)
        if coverage >= 0.6:
            located = {c["i"]: anchors[c["i"]] - c["start"]
                       for c in cues if c["i"] in anchors}
            redone = realign_shifted(model, audio, cues, audio_seconds, results, located)
            shifts = {**shifts, **located}
            median_abs, segments, dropped = judge(cues, results, shifts)
            log(f"{tag}#{episode} 按定位重锚：重跑 {redone} 组，残差中位 {median_abs:.1f}s，"
                f"丢弃 {dropped}/{len(results)}")
        else:
            log(f"{tag}#{episode} ASR 定位覆盖率 {coverage:.0%}，不足 60%，不重锚")

    if median_abs > AUDIO_MISMATCH_SECONDS:
        # 宁可这一集不做，也不要把整体错位的时间轴写进仓库。
        # 空 sidecar 标上原因即终局：同一份母带重跑必然同样拒绝。
        write_document(out_path, episode, audio_seconds, [], attempts,
                       rejected="audio_mismatch")
        if args.audio is None:
            audio_path.unlink(missing_ok=True)
        log(
            f"{tag}#{episode} REJECT audio mismatch: median|residual|={median_abs:.1f}s "
            f"> {AUDIO_MISMATCH_SECONDS:.0f}s, ok={len(results)}/{len(cues) - skipped}"
        )
        return 1

    if results and dropped > 0.10 * len(results):
        # 定位之后仍有这么多句对不上，说明内容真的不一致（重剪、垫乐段落），
        # 不交半对半错的数据；空 sidecar 标上原因即终局。
        write_document(out_path, episode, audio_seconds, [], attempts,
                       rejected="high_drop_rate")
        if args.audio is None:
            audio_path.unlink(missing_ok=True)
        log(f"{tag}#{episode} REJECT per-cue: {dropped}/{len(results)} cues implausible")
        return 1

    # 少量缺口（交叉说话、无法对齐的标签句）重试也修不好，按整集比例判定；
    # 只有大比例失败才值得再跑一次。
    speech_total = len(cues) - skipped
    complete = (
        speech_total > 0
        and dropped <= 0.10 * len(results)
        and failures <= 0.10 * speech_total
    )
    write_document(out_path, episode, audio_seconds, segments, attempts, complete)

    try:
        shown = out_path.relative_to(BASE)
    except ValueError:
        shown = out_path

    log(
        f"{tag}#{episode} audio={human(audio_seconds)} ok={len(results)}/{len(cues) - skipped} "
        f"bracket={skipped} fail={failures} words={words} "
        f"decode={decode_seconds:.0f}s align={align_seconds:.0f}s "
        f"({audio_seconds / max(align_seconds, 1e-6):.2f}sec/s) "
        f"begin_err_p50={percentile(begin_errors, .5):+.2f}s "
        f"shift={min(shifts.values(), default=0.0):+.1f}/{max(shifts.values(), default=0.0):+.1f}s "
        f"resid_p50={median_abs:.2f}s "
        f"end_saved={end_saved:.0f}s dropped={dropped} "
        f"complete={complete} attempt={attempts}/{MAX_ATTEMPTS} -> {shown}"
    )

    if args.audio is None:
        audio_path.unlink(missing_ok=True)

    if failures:
        log(f"    {failures} cue 未对齐，保留原 end；sidecar 标记未完成，"
            f"最多重试 {MAX_ATTEMPTS} 次")
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
    parser.add_argument(
        "--status", action="store_true",
        help="只统计完成/待办并输出 pending=N，不加载模型或第三方依赖",
    )
    parser.add_argument(
        "--plan", action="store_true",
        help="按 --shards 把待办摊成 JSON 数组（下标=档位，值=逗号分隔集号）后退出，"
             "stdout 只有一行 JSON，供 check job 写进 output",
    )
    parser.add_argument(
        "--debug-errors", action="store_true", help="按四分位打印首词偏移，诊断音频版本差异"
    )
    parser.add_argument(
        "--offset-probe", type=int, default=0, metavar="N",
        help="诊断：每集只采样对齐 N 段并打印偏移曲线，不写 sidecar、不提交",
    )
    parser.add_argument(
        "--locate-first", action="store_true",
        help="先整集粗 ASR 定位每句的真实位置，再按定位切窗对齐（恢复被拒集用）",
    )
    parser.add_argument(
        "--locate-only", action="store_true",
        help="诊断：只跑 ASR 定位并打印覆盖率与偏移分布，不对齐、不写 sidecar",
    )
    parser.add_argument(
        "--allow-rss-audio",
        action="store_true",
        help="archive 取不到时回退 RSS 音频（默认跳过：RSS 音频按地区插播，会写坏时间轴）",
    )
    args = parser.parse_args()

    args.out = Path(args.out)
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("需要 1 <= shards 且 0 <= shard < shards")

    sources = load_sources(RSS_FILE)
    episodes = select_episodes(sources, args)

    if args.plan:
        todo = [e for e in episodes if args.force or not is_done(args.out / f"{e}.json")]
        print(json.dumps(build_plan(todo, args.shards)))
        return 0

    if args.status:
        selected = [
            e for pos, e in enumerate(episodes) if pos % args.shards == args.shard
        ]
        done = refused = pending = 0
        for episode in selected:
            path = args.out / f"{episode}.json"
            if not path.exists():
                pending += 1
                continue
            complete, attempts, rejected = read_status(path)
            if status_is_done(complete, attempts, rejected):
                done += 1
                refused += bool(rejected)
            else:
                pending += 1
        log(f"transcript={len(selected)} done={done} refused={refused} pending={pending}")
        print(f"pending={pending}")
        return 0

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    done_paths = {e: args.out / f"{e}.json" for e in episodes}
    global_pending = sum(
        1 for e in episodes if args.force or not is_done(done_paths[e])
    )
    # 按"完整选中列表"切片而不是按待办切片：待办会随其他分片提交而变化，
    # 那样两个分片可能在不同时刻把同一集分给自己，导致 rebase 冲突。
    shard_episodes = [
        e for pos, e in enumerate(episodes) if pos % args.shards == args.shard
    ]
    pending = [
        e for e in shard_episodes if args.force or not is_done(done_paths[e])
    ]
    log(f"shard {args.shard}/{args.shards}: 全局待办 {global_pending} 集"
        f"（选中 {len(episodes)} 集，已完成 {len(episodes) - global_pending} 集）；"
        f"本分片共 {len(shard_episodes)} 集，已完成 {len(shard_episodes) - len(pending)} 集，"
        f"本段 {len(pending)} 集")
    if not pending:
        log("nothing to do")
        log("run_summary attempted=0 left=0")
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
    total = len(pending)
    shard_total = len(shard_episodes)
    shard_done_before = shard_total - total
    run_started = time.time()
    log(f"budget {args.max_minutes:.0f}min, 本分片共 {shard_total} 集，已完成 {shard_done_before} 集")
    for episode in pending:
        if time.time() > deadline:
            log(f"时间预算用完，本段剩 {total - done} 集，交给下一段继续")
            break
        elapsed = time.time() - run_started
        position = shard_done_before + done + 1
        eta = elapsed / done * (shard_total - position + 1) if done else float("nan")
        tag = f"[{position:>3}/{shard_total} {100 * position / shard_total:3.0f}% eta " \
              f"{'--' if done == 0 else human(eta)}] "
        try:
            if args.locate_only:
                locate_only(model, session, sources[episode], args, workdir, tag)
                done += 1
                continue
            if args.offset_probe:
                probe_episode(model, session, sources[episode], args, workdir, tag)
                done += 1
                continue
            if run_episode(model, session, sources[episode], args, workdir, tag):
                failures += 1
        except Exception as exc:
            failures += 1
            path = args.out / f"{episode}.json"
            prev = read_status(path)[1] if path.exists() else 0
            mark_attempt(path, episode, prev + 1, f"error:{type(exc).__name__}")
            log(f"{tag}#{episode} ERROR {type(exc).__name__}: {exc}")
        persist_progress(args.out / f"{episode}.json", episode)
        done += 1

    # 探针/定位不写 sidecar，pending 永远不会降；报 left=0 让分段循环收尾。
    left = 0 if (args.offset_probe or args.locate_only) else total - done
    log(f"run_summary attempted={done} left={left}")
    log(f"done {done}/{total} episodes, {failures} with failures, "
        f"elapsed {human(time.time() - started)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
