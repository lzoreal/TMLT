#!/usr/bin/env python3
"""整集听写：官网没有校对稿的集，用 Whisper 出稿并同时产出句级 VTT 与词级 sidecar。

align_timings.py 的前提是"已有校对稿，只补时间"。这 7 集（57/79/86/375/454/460/497）
官网从未提供 transcript（/N/transcript 返回 404/403/500），没有可对齐的文本，
只能由模型听写。因此这里产出的 VTT 就是 feed 里的 transcript，sidecar 直接标
complete，不再走 align_timings 的对齐与拒收集流程。

音频只认 archive 母带（见 AGENTS.md 铁律 2）：先用 docs/audio-manifest.json 里
带 sha256 的地址；#454 的母带已下线，改用 docs/audio/454.mp3（随 Pages 部署）。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# 导入顺序有讲究：align_timings 会在导入时关掉 tqdm 的 \r 进度条输出。
import align_timings as at

BASE = Path(__file__).resolve().parent
ROSTER_FILE = BASE / "docs/episodes.json"
MANIFEST_FILE = BASE / "docs/audio-manifest.json"
LOCAL_AUDIO_DIR = BASE / "docs/audio"
VTT_DIR = BASE / "docs/transcripts"
TIMING_DIR = BASE / "docs/timings"
DEFAULT_MODEL = BASE / "models/ct2-large-v3"

# 母带短于这个时长说明下到的是占位页或片段（#454 的 242 字节 404 页面）。
MIN_AUDIO_SECONDS = 600.0


def log(message=""):
    at.log(message)


# ============================================================
# 音频来源
# ============================================================


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_entries(path=MANIFEST_FILE):
    document = load_json(path)
    return {int(entry["episode"]): entry for entry in document.get("episodes", [])}


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_plan(episode, roster, manifest):
    """返回 ("local", 路径) 或 ("download", 地址, 字节数, sha256)。

    本地副本优先：#454 的母带只在仓库里。否则用清单里已验过字节的 archive
    地址；清单没有可用条目时退回 episodes.json 的 resolved_audio，但仍会按
    下载后的真实时长校验。
    """

    local = LOCAL_AUDIO_DIR / f"{episode}.mp3"
    if local.is_file() and local.stat().st_size > 1 << 20:
        return ("local", local)

    entry = manifest.get(episode)
    if entry and entry.get("status") == 200 and entry.get("sha256"):
        return ("download", entry["url"], int(entry.get("size") or 0),
                entry["sha256"])

    info = roster.get(str(episode)) or {}
    url = info.get("resolved_audio") or info.get("audio")
    if url:
        return ("download", url, 0, "")
    return None


def fetch_audio(session, episode, plan, workdir):
    if plan[0] == "local":
        return plan[1]

    _, url, expected_size, expected_sha = plan
    target = workdir / f"{episode}_archive.mp3"
    if target.is_file() and expected_size and target.stat().st_size == expected_size:
        log(f"    #{episode} 音频缓存命中（{expected_size / 1e6:.1f} MB）")
    else:
        started = time.time()
        size = at.download(session, url, target)
        log(f"    #{episode} 下载 {size / 1e6:.1f}MB，用时 {time.time() - started:.0f}s")
    if expected_size and target.stat().st_size != expected_size:
        raise IOError(f"size mismatch {target.stat().st_size}/{expected_size}")
    if expected_sha:
        digest = sha256_of(target)
        if digest != expected_sha:
            raise IOError(f"sha256 mismatch {digest[:12]}…/{expected_sha[:12]}…")
    return target


# ============================================================
# 结果 -> VTT / sidecar
# ============================================================


def milliseconds(seconds):
    return int(round(float(seconds) * 1000))


def build_segments(raw_segments, audio_seconds):
    """把 stable-whisper 的 segment 转成 sidecar 格式（毫秒、[[词, 起点, 时长]]）。"""

    limit = milliseconds(audio_seconds)
    segments = []
    for raw in raw_segments:
        text = " ".join(str(raw.text or "").split())
        if not text:
            continue
        start = min(max(0, milliseconds(raw.start)), limit)
        end = min(max(start, milliseconds(raw.end)), limit)
        words = []
        for word in getattr(raw, "words", None) or []:
            token = " ".join(str(word.word or "").split())
            if not token:
                continue
            word_start = min(max(0, milliseconds(word.start)), limit)
            word_end = min(max(word_start, milliseconds(word.end)), limit)
            words.append([token, word_start, word_end - word_start])
        segments.append({
            "start": start,
            "end": end,
            "speaker": "",
            "text": text,
            "words": words,
        })
    return segments


SENTENCE_END = re.compile(r"[.?!…][\"'”’)\]]*$")


def merge_fragments(segments, max_chars=at.MAX_CHARS, max_span=at.MAX_SPAN):
    """把同一句里被停顿切开的碎片并回一条 cue。

    stable-whisper 默认按停顿切，100 秒能切出 45 条（"…when you inevitably" /
    "pull up a photo…" 各占一条），读起来是碎的。这里只并"上一句没有句末标点"
    的片段，并到句号或 at 的分组上限（900 字符 / 100 秒）为止。
    """

    merged = []
    for segment in segments:
        if merged:
            last = merged[-1]
            span = (segment["end"] - last["start"]) / 1000.0
            chars = len(last["text"]) + len(segment["text"]) + 1
            if (not SENTENCE_END.search(last["text"])
                    and chars <= max_chars and span <= max_span):
                last["text"] = f'{last["text"]} {segment["text"]}'
                last["end"] = segment["end"]
                last["words"].extend(segment["words"])
                continue
        merged.append(dict(segment, words=list(segment["words"])))
    return merged


def vtt_time(seconds):
    total = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def vtt_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_vtt(segments):
    lines = ["WEBVTT", ""]
    for segment in segments:
        start = segment["start"] / 1000.0
        end = segment["end"] / 1000.0
        if end <= start:
            end = start + 0.5
        lines.append(f"{vtt_time(start)} --> {vtt_time(end)}")
        lines.append(vtt_escape(segment["text"]))
        lines.append("")
    return "\n".join(lines)


def write_episode(episode, segments, audio_seconds, vtt_path, out_path):
    vtt_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = vtt_path.with_suffix(vtt_path.suffix + ".tmp")
    tmp.write_text(render_vtt(segments), encoding="utf-8")
    tmp.replace(vtt_path)
    at.write_document(out_path, episode, audio_seconds, segments, 1, complete=True)


# ============================================================
# 提交
# ============================================================


def commit_episode(paths, episode):
    if os.environ.get("COMMIT_RESULTS") != "true":
        return
    try:
        rels = [p.resolve().relative_to(BASE.resolve()).as_posix() for p in paths]
    except ValueError:
        return

    def git(*args):
        return subprocess.run(("git", *args), cwd=BASE, capture_output=True, text=True)

    def push():
        for attempt in range(1, 3):
            if (git("pull", "--rebase", "--autostash").returncode == 0
                    and git("push").returncode == 0):
                return True
            git("rebase", "--abort")
            time.sleep(5 * attempt)
        return False

    if git("add", "--", *rels).returncode != 0:
        return
    if git("diff", "--cached", "--quiet").returncode == 0:
        ahead = git("rev-list", "--count", "@{u}..HEAD")
        if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
            push()
        return
    if git("commit", "-m",
           f"Add ASR transcript and timings (#{episode}, "
           f"shard {os.environ.get('SHARD', '?')})").returncode != 0:
        return
    if not push():
        log(f"    #{episode} 结果已本地提交，push 留给下一集重试")


# ============================================================
# 单集
# ============================================================


def transcribe(model, audio, language):
    with at.Heartbeat(f"听写中（音频 {at.human(audio.size / at.SR)}）"):
        result = model.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            beam_size=5,
            # 长音频里重复段落会让解码循环复读，关掉上文条件可以打断。
            condition_on_previous_text=False,
            check_sorted=False,
            verbose=None,
        )
    return result.segments


def run_episode(model, session, episode, roster, manifest, args):
    vtt_path = VTT_DIR / f"{episode}.vtt"
    out_path = args.out / f"{episode}.json"

    if not args.force and at.is_done(out_path) and vtt_path.exists():
        log(f"#{episode} skip (already done)")
        return "skipped"

    plan = audio_plan(episode, roster, manifest)
    if plan is None:
        log(f"#{episode} 没有可用音频地址")
        return "failed"

    audio_path = fetch_audio(session, episode, plan, args.workdir)
    audio = at.decode_audio(audio_path)
    audio_seconds = audio.size / at.SR
    if audio_seconds < MIN_AUDIO_SECONDS:
        raise RuntimeError(f"音频只有 {audio_seconds:.0f} 秒，疑似占位文件")
    log(f"#{episode} 音频 {at.human(audio_seconds)}，开始听写")

    started = time.time()
    raw = transcribe(model, audio, args.language)
    segments = merge_fragments(build_segments(raw, audio_seconds))
    if not segments:
        raise RuntimeError("听写结果为空")
    words = sum(len(s["words"]) for s in segments)
    log(f"#{episode} 听写 {len(segments)} 句 / {words} 词，"
        f"用时 {at.human(time.time() - started)}")

    write_episode(episode, segments, audio_seconds, vtt_path, out_path)
    log(f"#{episode} 写入 {vtt_path.name} + {out_path.name}")
    commit_episode([vtt_path, out_path], episode)
    return "done"


# ============================================================
# CLI
# ============================================================


def select_episodes(roster, raw):
    wanted = [int(x) for x in raw.split(",") if x.strip()]
    missing = [e for e in wanted if str(e) not in roster]
    if missing:
        raise SystemExit(f"集号不在 episodes.json 里：{missing}")
    return wanted


def main():
    parser = argparse.ArgumentParser(
        description="官网无校对稿的集：Whisper 整集听写，产出 VTT 与词级 sidecar")
    parser.add_argument("--episodes", required=True, help="逗号分隔的集号")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--out", type=Path, default=TIMING_DIR)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/tmlt-transcribe"))
    parser.add_argument("--language", default="en")
    parser.add_argument("--max-minutes", type=float, default=300.0)
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    roster = load_json(ROSTER_FILE)
    manifest = manifest_entries()
    episodes = select_episodes(roster, args.episodes)

    args.workdir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    at.silence_tqdm()
    import stable_whisper as sw

    threads = args.cpu_threads or min(16, (os.cpu_count() or 4))
    started = time.time()
    model = sw.load_faster_whisper(
        args.model, device="cpu", compute_type="int8", cpu_threads=threads)
    log(f"model ready in {time.time() - started:.1f}s (cpu_threads={threads})")

    session = at.make_session(os.environ.get("TAL_PROXY_URL") or None)
    deadline = time.time() + args.max_minutes * 60
    done = failed = 0
    for position, episode in enumerate(episodes, 1):
        if time.time() > deadline:
            log(f"时间预算用完，剩 {len(episodes) - position + 1} 集留给下一轮")
            break
        tag = f"[{position:>2}/{len(episodes)}] "
        log(f"{tag}#{episode} 开始")
        try:
            outcome = run_episode(model, session, episode, roster, manifest, args)
        except Exception as exc:
            failed += 1
            log(f"{tag}#{episode} ERROR {type(exc).__name__}: {exc}")
            continue
        if outcome != "done":
            failed += 1
        done += 1

    log(f"run_summary attempted={done} left={len(episodes) - done}")
    log(f"done {done}/{len(episodes)} episodes, {failed} failed, "
        f"elapsed {at.human(time.time() - started)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
