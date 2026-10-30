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
# ASR 定位出来的平移是直接测量（不是从残差反推），可以放宽到整段错位的量级：
# 实测 #168 全片 −65 秒、#192 −57 秒——官网时间戳整体差了一分钟，
# ±11 秒的窗口永远看不见，只能靠定位。
MAX_LOCATED_SHIFT = 300.0
LOCATED_GAP = 180.0
# 定位覆盖率低于此值时退回官网时间轴。60% 会把只差几个百分点的集挡在门外
# （实测 #9 匹配 57%，70 个锚点被整批丢掉，整集只能按错误的时间轴对齐）。
MIN_LOCATE_COVERAGE = 0.5

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


def align_window(model, audio, lo, hi, group):
    """对齐 [lo, hi] 这一段音频里的一个 cue 组，返回 {cue 序号: {"words": [...]}}。"""

    clip = audio[int(lo * SR): int(hi * SR)]
    if clip.size == 0:
        return {}
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


def align_group(model, audio, group, total_seconds, shift=0.0, per_cue=None):
    """对一个 cue 组切窗、对齐，返回 {cue 序号: {"words": [...]}}。

    shift 是这一组相对官网 begin 的实测位移：母带与稿件在段界处有几秒差，
    窗口和合理带都要跟着挪，否则 DTW 会对着错误的音频硬贴。
    per_cue 是逐句位移：组内正好跨过段界时，只用组首那一句的位移会把后半组
    切到窗口外（实测 #74 27:40 那簇因此整体偏 16 秒），窗口按每句的期望位置取并集。
    """

    lo = max(0.0, group[0]["start"] + shift - PAD)
    # 不能只看最后一句：cue 时序会重叠，前一句的长台词可能延伸到后面。
    hi = max(c["start"] + cue_span(c) for c in group) + shift + TAIL_PAD
    if per_cue:
        cue_lo = min(c["start"] + per_cue.get(c["i"], shift) for c in group) - PAD
        cue_hi = max(c["start"] + cue_span(c) + per_cue.get(c["i"], shift)
                     for c in group) + TAIL_PAD
        # 个别锚点离谱时窗口会横跨两段不相干的音频，这时退回组首估计。
        if cue_hi - cue_lo <= MAX_SPAN + PAD + TAIL_PAD + 3 * MAX_EARLY:
            lo = max(0.0, cue_lo)
            hi = cue_hi
    hi = min(total_seconds, hi)
    # 夹到片尾/平移过头时会切出空片段，交给 FFmpeg 只会抛
    # RuntimeError: FFmpeg failed to read "[]"，按"这句没对上"处理。
    return align_window(model, audio, lo, hi, group)


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
                                  shifts.get(group[0]["i"], 0.0),
                                  per_cue=shifts or None)
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


def match_cues(tokens, cues, block_ratio=0.35, window=400, max_window=3000):
    """把每句校对稿按词元匹配钉到 ASR 词流里的真实位置。

    tokens 是 [(词元, 起点秒)]；只做匹配，不碰音频，便于拿缓存的词流离线迭代。
    """

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
            # 判据：最长连续匹配段 ≥ 整句的 35%，且至少 3 词。
            # 最初要求 50% 会把长句判死（一句 40 词的台词中间夹一处 ASR 差异就掉到
            # 一半以下）；改成"所有匹配段之和 ≥50%"又太松，零散的小片段能凑够分数，
            # 本地实测覆盖率反而掉到 37% 并出现 +1900 秒的荒谬锚点。
            # 35% + 保底 3 词在本地 #69 上把覆盖率从 30% 提到 68%，且没有野值。
            blocks = [b for b in matcher.get_matching_blocks()
                      if b.size >= 2 and b.a < len(wanted)]
            if not blocks:
                continue
            block = max(blocks, key=lambda b: b.size)
            if block.size < max(3, int(round(block_ratio * len(wanted)))):
                continue
            score = block.size / len(wanted)
            if score > best_score:
                start = pointer + block.b - block.a
                if start >= len(tokens):
                    continue
                # 定位平移本身有量级上限（RSS 插播也就 +25~+97 秒），
                # 超出说明匹配跑到别处去了，宁可不要这个锚点。
                if abs(tokens[start][1] - cue["start"]) > MAX_LOCATED_SHIFT:
                    continue
                best_score, best_pos = score, max(pointer, start)
            if best_score >= 0.7:
                break
        # 只留一道门：连续 ≥3 词且覆盖整句 ≥35% 就算定位成功。
        # 之前外层还有 score ≥ 0.5 的第二道门，长句连续片段 48% 会被它挡掉，
        # 三集实测把能救回的 58/50 句拦在门外（覆盖率 68/61/64% → 83/71/72%）。
        if best_pos is not None:
            anchors[cue["i"]] = tokens[best_pos][1]
            pointer = best_pos

    sought = sum(1 for c in cues if WORD_TOKEN.search(c["text"].lower()))
    coverage = len(anchors) / sought if sought else 0.0
    return anchors, coverage


def locate_cues(model, audio, cues, cache=None, **kwargs):
    """整集粗 ASR 一遍再匹配。只用来定位：ASR 的文字不参与输出，校对稿不改。

    cache 是词流缓存路径：命中就不转写，只在没有模型或本地迭代时用。
    """

    started = time.time()
    tokens = None
    if cache is not None and Path(cache).exists():
        raw = json.loads(Path(cache).read_text(encoding="utf-8"))
        tokens = [(row[0], row[1]) for row in raw]
        log(f"    ASR 词流缓存命中：{len(tokens)} 词")
    if tokens is None:
        result = model.transcribe(audio, language="en", word_timestamps=True)
        tokens = []
        for segment in result.segments:
            for word in segment.words or []:
                for token in WORD_TOKEN.findall(word.word.lower()):
                    tokens.append((token, float(word.start)))
        if cache is not None:
            Path(cache).parent.mkdir(parents=True, exist_ok=True)
            Path(cache).write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
    if not tokens:
        return {}, 0.0
    log(f"    ASR 定位：{len(tokens)} 词，用时 {time.time() - started:.0f}s")

    anchors, coverage = match_cues(tokens, cues, **kwargs)
    log(f"    ASR 定位：{len(anchors)} 句匹配成功（{coverage:.0%}），"
        f"总用时 {time.time() - started:.0f}s")
    return anchors, coverage


def fill_gaps(shifts, cues, max_gap=LOCATED_GAP):
    """没定位到的句沿用时间上最近的已定位句的平移。

    母带的偏差通常是整段一致的（实测 #168 全片 −65 秒），所以邻句的平移
    比"退回官网时间轴"更可信；超过 max_gap 就不硬套。
    """

    import bisect

    known = sorted((c["start"], shifts[c["i"]]) for c in cues if c["i"] in shifts)
    if not known:
        return {}
    times = [t for t, _ in known]
    filled = dict(shifts)
    for cue in cues:
        if cue["i"] in filled:
            continue
        pos = bisect.bisect_left(times, cue["start"])
        near = [known[k] for k in (pos - 1, pos) if 0 <= k < len(known)]
        if not near:
            continue
        when, value = min(near, key=lambda pair: abs(pair[0] - cue["start"]))
        if abs(when - cue["start"]) <= max_gap:
            filled[cue["i"]] = value
    return filled


def realign_shifted(model, audio, cues, total_seconds, results, shifts,
                    min_shift=2.0, max_shift=MAX_LOCAL_SHIFT):
    """按定位出的位移重切窗口，只重跑位移明显的组；返回重跑的组数。"""

    speech = [c for c in cues if not BRACKET.match(c["text"])]
    redone = 0
    for group in group_cues(speech):
        shift = shifts.get(group[0]["i"], 0.0)
        if abs(shift) < min_shift or abs(shift) > max_shift:
            continue
        try:
            aligned = align_group(model, audio, group, total_seconds, shift,
                                  per_cue=shifts)
        except Exception as exc:
            log(f"    realign failed on cue {group[0]['i']}: {type(exc).__name__}")
            continue
        for cue in group:
            if cue["i"] in aligned:
                results[cue["i"]] = aligned[cue["i"]]
        redone += 1
    return redone


def _group_hits(group, aligned, shift):
    """这一组在给定平移下能对上几句（按组内共同平移判断合理带）。"""

    hits = 0
    for pos, cue in enumerate(group):
        words = (aligned.get(cue["i"]) or {}).get("words") or []
        next_start = (group[pos + 1]["start"] + shift) if pos + 1 < len(group) else None
        if words and plausible_words(cue, next_start, words, cue["start"] + shift):
            hits += 1
    return hits


def rescue_groups(model, audio, cues, total_seconds, results, shifts, tries=3):
    """对没对上的组，用邻句实测到的平移做多候选重试，取对上最多句的一版。

    定位覆盖率 56-75% 时，没定位到的句沿用最近邻组的平移；如果那股偏差在段界
    换过一次，这些句就会整组对不上（实测 #69/#74/#192 都卡在丢弃 11.6-11.7%，
    只比 10% 线高一点点）。这里只对失败的组重试，候选值来自附近已经对上的句，
    所以不会把窗口挪到没有依据的位置。
    """

    speech = [c for c in cues if not BRACKET.match(c["text"])]
    good = []
    for index, cue in enumerate(cues):
        aligned = results.get(cue["i"])
        if not aligned:
            continue
        next_cue = cues[index + 1] if index + 1 < len(cues) else None
        next_start = (next_cue["start"] + shifts.get(next_cue["i"], 0.0)
                      if next_cue is not None else None)
        if plausible_words(cue, next_start, aligned["words"],
                           cue["start"] + shifts.get(cue["i"], 0.0)):
            good.append((cue["start"], shifts.get(cue["i"], 0.0)))
    if not good:
        return 0

    redone = 0
    for group in group_cues(speech):
        base = shifts.get(group[0]["i"], 0.0)
        current = _group_hits(group, results, base)
        if current == len(group):
            continue
        candidates = [base]
        for _, value in sorted(good, key=lambda item: abs(item[0] - group[0]["start"])):
            if all(abs(value - seen) > 1.0 for seen in candidates):
                candidates.append(value)
            if len(candidates) > tries:
                break
        best = (current, None, base)
        for shift in candidates[1:]:
            try:
                aligned = align_group(model, audio, group, total_seconds, shift)
            except Exception:
                continue
            hits = _group_hits(group, aligned, shift)
            if hits > best[0]:
                best = (hits, aligned, shift)
        if best[1] is not None:
            results.update(best[1])
            for cue in group:
                shifts[cue["i"]] = best[2]
            redone += 1
    return redone


def retry_dropped(model, audio, cues, total_seconds, results, shifts):
    """对没对上的单句，用左右邻居的实测词边界框一个小窗口再对齐一次。

    实证（816 集、6992 个丢弃句）：36% 是 ≤3 词的对白短句，63% 左右邻居都已
    对齐，35% 落在邻居之间 0.3–10 秒的空隙里（中位 1.7 秒）。这类不是内容不
    一致，而是整组切窗时短句被邻近长句吞掉或压扁。邻居窗口只有几秒，单独
    对齐很便宜；词表、词长、与邻居的位置关系全部过关才采纳。
    """

    speech = [c for c in cues if not BRACKET.match(c["text"])]
    fixed = 0
    for position, cue in enumerate(speech):
        if (results.get(cue["i"]) or {}).get("words"):
            continue
        previous = speech[position - 1] if position else None
        following = speech[position + 1] if position + 1 < len(speech) else None
        prev_words = (results.get(previous["i"]) or {}).get("words") if previous else None
        next_words = (results.get(following["i"]) or {}).get("words") if following else None

        lo = hi = None
        if prev_words and next_words:
            gap = next_words[0][1] - prev_words[-1][2]
            if 0.3 <= gap <= 10.0:
                lo, hi = prev_words[-1][2] - 0.5, next_words[0][1] + 0.5
        if lo is None:
            shift = shifts.get(cue["i"], 0.0)
            lo = max(0.0, cue["start"] + shift - PAD)
            hi = min(total_seconds, cue["start"] + cue_span(cue) + shift + TAIL_PAD)
        if hi - lo < 0.3 or hi - lo > 30.0:
            continue

        try:
            got = align_window(model, audio, lo, hi, [cue])
        except Exception:
            continue
        words = (got.get(cue["i"]) or {}).get("words") or []
        if not words:
            continue
        if [w[0] for w in words] != cue["text"].split():
            continue
        first, last = words[0][1], words[-1][2]
        natural = len(cue["text"]) / NOMINAL_CHARS_PER_SECOND
        # 一句 "Yes?" 的正常时长只有 0.2 秒左右，下限按文本长度取，不设绝对秒数。
        if last - first <= 0 or last - first < max(0.1, 0.5 * natural):
            continue
        if prev_words and first < prev_words[-1][1]:
            continue
        if next_words and last > next_words[0][1] + MAX_LATE:
            continue
        results[cue["i"]] = {"words": words}
        shifts[cue["i"]] = first - cue["start"]
        fixed += 1
    return fixed


def adopt_measured_shifts(cues, results, shifts, min_gap=MAX_EARLY):
    """把"对齐结果与定位估计矛盾、但词长合理"的句改成实测位置。

    定位估计是切窗前的推断，段界处会落后一步：实测 #74 在 27:40 一簇句的估计比
    实际位置早 16 秒、34:43 一簇早 26 秒，对齐本身是连续单调的，被拒只是因为离
    估计值太远。矛盾超过 MAX_EARLY 且词长与文本相符时，实测位置才是证据。

    词长守卫防的是另一种错：窗口切错时 DTW 会把整句压进错误音频，词长明显短于
    文本应有的时长，这种矛盾句保持原估计、交给合理性检查丢弃。
    """

    adopted = 0
    for cue in cues:
        aligned = results.get(cue["i"])
        words = aligned["words"] if aligned else []
        if not words:
            continue
        measured = words[0][1] - cue["start"]
        if abs(measured - shifts.get(cue["i"], 0.0)) <= min_gap:
            continue
        if abs(measured) > MAX_LOCATED_SHIFT:
            continue
        natural = len(cue["text"]) / NOMINAL_CHARS_PER_SECOND
        if words[-1][2] - words[0][1] < max(0.3, 0.5 * natural):
            continue
        shifts[cue["i"]] = measured
        adopted += 1
    return adopted


def judge(cues, results, shifts):
    """按平移重算残差、写 segments；返回 (残差中位, segments, dropped)。"""

    residuals = [
        results[c["i"]]["words"][0][1] - (c["start"] + shifts.get(c["i"], 0.0))
        for c in cues if c["i"] in results
    ]
    median_abs = percentile([abs(e) for e in residuals], 0.5) if residuals else float("nan")
    segments, dropped = build_segments(cues, results, shifts)
    return median_abs, segments, dropped


def plausible_reason(cue, next_start, words, expected=None):
    """返回空串表示可信，否则返回失败原因代码（便于逐句取证）。"""

    if not words:
        return "no-words"

    cue_start = cue["start"] if expected is None else expected
    first = words[0][1]
    last = words[-1][2]

    # 整句被压成 instant 就没有可用信息；但"末词零长度"是库的常见产物
    # （本地 #74 实测 11/31 的失败句其实首词偏差 <0.4 秒，末词只是被压成零长），
    # 只影响 end 的取法，不该整句作废。
    if last - first <= 0:
        return "instant"
    # 对齐是库对给定文本做的；词表一旦和原文不一致（常见为重复末词），整句不可信。
    if [w[0] for w in words] != cue["text"].split():
        return "word-mismatch"
    if last < cue_start + 0.2:
        return "before-start"
    if first < cue_start - MAX_EARLY:
        return "too-early"
    if first > cue_start + MAX_EARLY:
        return "too-late"
    if next_start is not None:
        if first > next_start + 0.5:
            return "after-next"
        if last > next_start + MAX_LATE:
            return "over-next"
    return ""


def plausible_words(cue, next_start, words, expected=None):
    """词集与词位置是否可信；不可信时调用方丢弃逐词而不是写错的数据。

    expected 是本地平移后的期望起点（母带与稿件在段界处有几秒差），
    缺省退回官网 begin。
    """

    return not plausible_reason(cue, next_start, words, expected)


def drop_reasons(cues, results, shifts):
    """统计被丢弃的句按原因分类，附三个例子；拒绝时写进日志。"""

    shifts = shifts or {}
    counts = {}
    examples = []
    for index, cue in enumerate(cues):
        aligned = results.get(cue["i"])
        words = aligned["words"] if aligned else []
        if not words:
            continue
        expected = cue["start"] + shifts.get(cue["i"], 0.0)
        next_cue = cues[index + 1] if index + 1 < len(cues) else None
        next_start = (next_cue["start"] + shifts.get(next_cue["i"], 0.0)
                      if next_cue is not None else None)
        reason = plausible_reason(cue, next_start, words, expected)
        if not reason:
            continue
        counts[reason] = counts.get(reason, 0) + 1
        if len(examples) < 3:
            examples.append(f"cue{cue['i']}@{human(cue['start'])} {reason}")
    return counts, examples


def dump_cues(path, cues, results, shifts):
    """逐句落盘：官网起点、期望起点、对齐首词/末词、失败原因。"""

    shifts = shifts or {}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("i\tsite\texpected\tfirst\tlast\twords\treason\ttext\n")
        for index, cue in enumerate(cues):
            aligned = results.get(cue["i"])
            words = aligned["words"] if aligned else []
            expected = cue["start"] + shifts.get(cue["i"], 0.0)
            next_cue = cues[index + 1] if index + 1 < len(cues) else None
            next_start = (next_cue["start"] + shifts.get(next_cue["i"], 0.0)
                          if next_cue is not None else None)
            reason = plausible_reason(cue, next_start, words, expected)
            first = f"{words[0][1]:.2f}" if words else ""
            last = f"{words[-1][2]:.2f}" if words else ""
            text = cue["text"].replace("\t", " ").replace("\n", " ")
            fh.write(f"{cue['i']}\t{cue['start']:.2f}\t{expected:.2f}\t{first}\t{last}\t"
                     f"{len(words)}\t{reason}\t{text}\n")


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
        start_ms = int(round((cue["start"] + shifts.get(cue["i"], 0.0)) * 1000))
        if words:
            # 末词可能是零长度（库的产物），end 取最后一个有正时长的词。
            last_word = next((w for w in reversed(words) if w[2] > w[1]), words[-1])
            end_ms = int(round(last_word[2] * 1000))
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
    token_cache = None
    if args.token_cache:
        token_cache = Path(args.token_cache) / f"tokens_{episode}.json"
    located = {}
    if args.locate_first:
        anchors, coverage = locate_cues(model, audio, cues, cache=token_cache)
        if coverage >= MIN_LOCATE_COVERAGE:
            located = {c["i"]: anchors[c["i"]] - c["start"]
                       for c in cues if c["i"] in anchors}
            located = fill_gaps(located, cues)
            log(f"{tag}#{episode} 先定位再对齐：覆盖 {coverage:.0%}，"
                f"平移 {min(located.values()):+.1f}…{max(located.values()):+.1f}s")
        else:
            log(f"{tag}#{episode} 定位覆盖率 {coverage:.0%}，"
                f"不足 {MIN_LOCATE_COVERAGE:.0%}，退回官网时间轴")
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
        # 定位结果是切窗前的估计，不是最终事实：对齐落点与它矛盾时要重新取证据。
        shifts, untrusted = dict(located), set()
    else:
        shifts, untrusted = local_shifts(cues, results)

    def refresh():
        adopted = adopt_measured_shifts(cues, results, shifts) if located else 0
        if adopted:
            log(f"{tag}#{episode} 采纳实测位置 {adopted} 句"
                f"（与定位估计差 >{MAX_EARLY:.0f}s，词长合理）")
        return judge(cues, results, shifts)

    median_abs, segments, dropped = refresh()
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
        anchors, coverage = locate_cues(model, audio, cues, cache=token_cache)
        if coverage >= MIN_LOCATE_COVERAGE:
            located = {c["i"]: anchors[c["i"]] - c["start"]
                       for c in cues if c["i"] in anchors}
            located = fill_gaps(located, cues)
            redone = realign_shifted(model, audio, cues, audio_seconds, results, located,
                                     max_shift=MAX_LOCATED_SHIFT)
            shifts = {**shifts, **located}
            median_abs, segments, dropped = refresh()
            log(f"{tag}#{episode} 按定位重锚：重跑 {redone} 组，残差中位 {median_abs:.1f}s，"
                f"丢弃 {dropped}/{len(results)}")
        else:
            log(f"{tag}#{episode} ASR 定位覆盖率 {coverage:.0%}，"
                f"不足 {MIN_LOCATE_COVERAGE:.0%}，不重锚")

    # 第二轮恢复：定位之后仍有一小撮句对不上（实测 #69/#74/#192 卡在 11.6-11.7%），
    # 用邻句实测到的平移做多候选重试。只重跑失败的组，不扩大搜索范围。
    if (args.rescue and shifts and results
            and dropped > 0.10 * len(results)
            and median_abs <= AUDIO_MISMATCH_SECONDS):
        rescued = rescue_groups(model, audio, cues, audio_seconds, results, shifts)
        if rescued:
            median_abs, segments, dropped = refresh()
            log(f"{tag}#{episode} 多候选重试 {rescued} 组 → "
                f"残差中位 {median_abs:.1f}s，丢弃 {dropped}/{len(results)}")

    # 组级重试之后仍没对上的零散句：左右邻居已经给出实测边界，单独再对齐一次。
    if args.rescue and results:
        fixed = retry_dropped(model, audio, cues, audio_seconds, results, shifts)
        if fixed:
            median_abs, segments, dropped = refresh()
            log(f"{tag}#{episode} 单句补对齐 {fixed} 句 → "
                f"残差中位 {median_abs:.1f}s，丢弃 {dropped}/{len(results)}")

    # 取证落盘要放在拒绝判定之前：被拒的集正是要看的那批。
    if args.dump_cues:
        target = Path(args.dump_cues)
        if not target.suffix:
            target.mkdir(parents=True, exist_ok=True)
            target = target / f"{episode}.tsv"
        dump_cues(target, cues, results, shifts)

    reject = (median_abs > AUDIO_MISMATCH_SECONDS
              or (results and dropped > 0.10 * len(results)))
    if reject:
        counts, examples = drop_reasons(cues, results, shifts)
        if counts:
            summary = ", ".join(f"{k} {v}" for k, v in
                                sorted(counts.items(), key=lambda kv: -kv[1]))
            log(f"{tag}#{episode} 丢弃原因: {summary}；例 {', '.join(examples)}")

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
        "--token-cache", metavar="DIR",
        help="ASR 词流缓存目录（本地迭代用：命中就跳过转写）",
    )
    parser.add_argument(
        "--locate-only", action="store_true",
        help="诊断：只跑 ASR 定位并打印覆盖率与偏移分布，不对齐、不写 sidecar",
    )
    parser.add_argument(
        "--dump-cues", metavar="PATH",
        help="逐句落盘对齐诊断（官网起点/期望/首末词/失败原因）；给目录名则按集号分文件",
    )
    parser.add_argument(
        "--rescue", action="store_true",
        help="定位对齐后，对没对上的组用邻句平移做多候选重试（第二轮恢复用）",
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
