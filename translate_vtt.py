#!/usr/bin/env python3

import os
import re
import json
import sys
import time
import hashlib
import traceback
import logging
import random
import tempfile

from pathlib import Path
from datetime import datetime, timezone

from google import genai

# ============================================================
# Config
# ============================================================

INPUT_DIR = Path("docs/transcripts")
OUTPUT_DIR = Path("docs/transcripts/zh")
CACHE_FILE = Path("docs/translations.json")

MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.1-flash-lite",
)

MAX_FILES = int(os.environ.get("MAX_FILES", "10"))

MAX_BATCH_CHARS = int(os.environ.get("MAX_BATCH_CHARS", "120000"))

RETRY_COUNT = 5
RETRY_BASE = 20

RPM_LIMIT = int(os.environ.get("RPM_LIMIT", "14"))

if RPM_LIMIT < 1:
    raise ValueError("RPM_LIMIT must be >= 1")

MIN_REQUEST_INTERVAL = 60.0 / RPM_LIMIT

DAILY_REQUEST_LIMIT = int(os.environ.get("DAILY_REQUEST_LIMIT", "500"))

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


# ============================================================
# Logging
# ============================================================

if IN_ACTIONS:
    fmt = "[%(levelname)s] %(message)s"
else:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=fmt,
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("vtt_translator")


def log(msg=""):
    logger.info(msg)


def separator():
    log("=" * 70)


def group(title):
    if IN_ACTIONS:
        print(f"::group::{title}", flush=True)
    else:
        log(f"\n>>> {title}")


def endgroup():
    if IN_ACTIONS:
        print("::endgroup::", flush=True)


# ============================================================
# Gemini
# ============================================================

group("Initializing Gemini")

try:
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is missing")

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": 120000},
    )

    log("Gemini initialized")

except Exception:
    log("Gemini initialization failed")
    endgroup()
    raise

endgroup()

_last_request_time = 0.0


# ============================================================
# Exceptions
# ============================================================


class DailyLimitReached(Exception):
    """每日 API 请求上限已达到。"""

    pass


# ============================================================
# Atomic file writing
# ============================================================


def atomic_write_text(path: Path, content: str):
    """
    写入临时文件，成功后再替换目标文件。
    """

    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )

    temp_path = Path(temp_name)

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="",
        ) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        temp_path.replace(path)

    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ============================================================
# Cache
# ============================================================


def load_cache():
    if not CACHE_FILE.exists():
        log("Cache not found")
        return {}

    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Cache must be a JSON object")

        meta = data.setdefault(
            "__meta__",
            {},
        )

        if not isinstance(meta, dict):
            raise ValueError("Cache metadata is invalid")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if meta.get("date") != today:
            meta["date"] = today
            meta["daily_requests"] = 0

            log("New day detected, resetting daily request counter")

        episode_count = len(data) - 1

        log(f"Cache loaded: {episode_count} episodes")

        return data

    except Exception as exc:
        # 不要在缓存损坏时直接返回空缓存并覆盖原文件。
        raise RuntimeError(f"Cache load failed: {exc}") from exc


def save_cache(cache):
    CACHE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    atomic_write_text(
        CACHE_FILE,
        json.dumps(
            cache,
            indent=2,
            ensure_ascii=False,
        ),
    )

    log("Cache saved")


# ============================================================
# Hash
# ============================================================


def file_hash(path: Path):
    sha = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(8192)

            if not chunk:
                break

            sha.update(chunk)

    return sha.hexdigest()


# ============================================================
# VTT validation
# ============================================================


def is_valid_vtt(path: Path) -> bool:
    """
    检查本地 VTT 文件：
    1. 文件存在。
    2. 文件非空。
    3. 包含 WEBVTT 标记。
    4. 至少包含一条时间轴。
    5. 至少有一条非空字幕内容。
    """

    path = Path(path)

    if not path.is_file():
        return False

    try:
        content = path.read_text(encoding="utf-8-sig")

        if not content.strip():
            return False

        if not content.lstrip().startswith("WEBVTT"):
            return False

        timestamp_pattern = (
            r"(?m)^\d{2,}:\d{2}:\d{2}\.\d{3}" r"\s+-->\s+" r"\d{2,}:\d{2}:\d{2}\.\d{3}"
        )

        if not re.search(
            timestamp_pattern,
            content,
        ):
            return False

        cues = parse_vtt_cues(content)

        return bool(cues)

    except (OSError, UnicodeError, ValueError):
        return False


# ============================================================
# Rate limit
# ============================================================


def _rate_limit_wait():
    global _last_request_time

    now = time.time()
    elapsed = now - _last_request_time

    if elapsed < MIN_REQUEST_INTERVAL:
        sleep_time = MIN_REQUEST_INTERVAL - elapsed

        log(f"Rate limit: sleeping {sleep_time:.2f}s")

        time.sleep(sleep_time)

    _last_request_time = time.time()


# ============================================================
# Gemini Translation
# ============================================================


def build_prompt(blocks):
    content = []

    for idx, text in blocks:
        content.append(f"{idx}|||{text}")

    joined = "\n\n".join(content)

    return f"""You are translating podcast subtitles.

Translate English subtitles into Simplified Chinese.

Rules:
1. WebVTT speaker names are metadata, not subtitle text.
2. Never output speaker names.
3. Only translate subtitle content.
4. Preserve the original meaning.
5. Do NOT summarize.
6. Keep every subtitle block.
7. Translate only the subtitle content.

Output format:
0|||Chinese translation
1|||Chinese translation

Important:
- Output exactly one line per block.
- Do not add markdown.
- Do not add explanations.
- Do not add introductions.
- Do not use code blocks.

Subtitle blocks:
{joined}
"""


def parse_translation_response(text):
    result = {}

    if not text:
        return result

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        line = re.sub(
            r"^[\-\*\s]+",
            "",
            line,
        )

        try:
            if "|||" in line:
                idx, value = line.split(
                    "|||",
                    1,
                )
            elif "|" in line:
                idx, value = line.split(
                    "|",
                    1,
                )
            else:
                continue

            idx = idx.strip()

            if not re.fullmatch(r"\d+", idx):
                continue

            value = value.strip()

            if value:
                result[int(idx)] = value

        except Exception:
            continue

    return result


def is_retryable_error(error) -> bool:
    err_str = str(error).lower()

    retryable = [
        "429",
        "too many requests",
        "rate limit",
        "quota",
        "503",
        "service unavailable",
        "500",
        "internal server error",
        "502",
        "504",
        "gateway",
        "timeout",
        "timed out",
        "deadline",
        "connection",
        "network",
        "unreachable",
    ]

    return any(keyword in err_str for keyword in retryable)


def validate_translation(
    original: str,
    translated: str,
) -> bool:
    if not translated or len(translated) < 2:
        return False

    if translated.lower().strip(".,!?") == original.lower().strip(".,!?"):
        return False

    if not re.search(
        r"[\u4e00-\u9fff]",
        translated,
    ):
        return False

    return True


def gemini_batch_translate(
    blocks,
    cache_meta,
):
    """
    带 RPM 限流、RPD 检查和重试的翻译。
    """

    prompt = build_prompt(blocks)

    for attempt in range(
        1,
        RETRY_COUNT + 1,
    ):
        daily_used = int(
            cache_meta.get(
                "daily_requests",
                0,
            )
        )

        if daily_used >= DAILY_REQUEST_LIMIT:
            raise DailyLimitReached(
                "Daily request limit reached: " f"{daily_used}/{DAILY_REQUEST_LIMIT}"
            )

        _rate_limit_wait()

        start = time.time()

        # 每次实际尝试都计入本地请求预算，
        # 包括失败请求，避免重试导致预算失真。
        cache_meta["daily_requests"] = daily_used + 1

        log(
            f"Request #{cache_meta['daily_requests']}/"
            f"{DAILY_REQUEST_LIMIT} "
            f"(attempt {attempt}/{RETRY_COUNT}), "
            f"blocks: {len(blocks)}"
        )

        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
            )

            elapsed = time.time() - start
            raw = response.text or ""

            parsed = parse_translation_response(raw)

            log(f"Success {elapsed:.2f}s, " f"parsed: {len(parsed)}")

            if not parsed:
                log("Empty parse result")
                log(raw[:500])

                if attempt < RETRY_COUNT:
                    raise RuntimeError("Empty parse result")

            return parsed

        except DailyLimitReached:
            raise

        except Exception as exc:
            log(f"Gemini error: {exc}")

            if (
                not is_retryable_error(exc)
                and "empty parse result" not in str(exc).lower()
            ):
                raise

            if attempt >= RETRY_COUNT:
                raise

            wait = RETRY_BASE * (2 ** (attempt - 1)) + random.uniform(0, 5)

            log(f"Retryable error, waiting {wait:.1f}s")

            time.sleep(wait)

    return {}


# ============================================================
# VTT Parser
# ============================================================


def parse_vtt_cues(content: str) -> list[dict]:
    """
    解析 WebVTT cue。
    支持 cue 标识符、多行文本和 NOTE/STYLE/REGION 区域。
    """

    cues = []
    lines = content.splitlines()

    i = 0
    n = len(lines)

    while i < n:
        stripped = lines[i].strip()

        if stripped in ("WEBVTT", ""):
            i += 1
            continue

        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            i += 1

            while i < n and lines[i].strip() != "":
                i += 1

            continue

        if "-->" in stripped:
            break

        if i + 1 < n and "-->" in lines[i + 1]:
            break

        i += 1

    while i < n:
        while i < n and lines[i].strip() == "":
            i += 1

        if i >= n:
            break

        identifier = None

        if i + 1 < n and "-->" in lines[i + 1]:
            identifier = lines[i].strip()
            i += 1

        if i >= n or "-->" not in lines[i]:
            i += 1
            continue

        timestamp = lines[i].strip()
        i += 1

        text_lines = []

        while i < n and lines[i].strip() != "":
            text_lines.append(lines[i])
            i += 1

        text = "\n".join(text_lines).strip()

        if text:
            cues.append(
                {
                    "timestamp": timestamp,
                    "text": text,
                    "identifier": identifier,
                }
            )

    return cues


# ============================================================
# Speaker handling
# ============================================================


def extract_speaker(text):
    match = re.match(
        r"<v\s+([^>]+)>(.*)",
        text,
        re.DOTALL,
    )

    if match:
        return (
            match.group(1).strip(),
            match.group(2).strip(),
        )

    return None, text


def prepare_gemini_text(text):
    _, content = extract_speaker(text)
    return content


def restore_speaker_translation(
    original,
    translated,
):
    speaker, _ = extract_speaker(original)

    if speaker:
        return f"<v {speaker}>{translated}"

    return translated


# ============================================================
# Batch chunking
# ============================================================


def chunk_cues_by_chars(
    cues,
    max_chars=MAX_BATCH_CHARS,
):
    batches = []
    current = []
    current_len = 500

    for i, cue in enumerate(cues):
        text = prepare_gemini_text(cue["text"])

        item_len = len(text) + 15

        if current and current_len + item_len > max_chars:
            batches.append(current)

            current = [(i, text)]
            current_len = 500 + item_len

        else:
            current.append((i, text))
            current_len += item_len

    if current:
        batches.append(current)

    return batches


# ============================================================
# Translate Episode
# ============================================================


def translate_episode(
    source,
    target,
    cache_meta,
):
    separator()

    log(f"Translating {source.name}")

    content = source.read_text(encoding="utf-8-sig")

    cues = parse_vtt_cues(content)

    log(f"Cue blocks: {len(cues)}")

    if not cues:
        raise RuntimeError("Source VTT contains no subtitle cues")

    translated = {}
    batches = chunk_cues_by_chars(cues)

    log(f"Initial batches: {len(batches)} " f"(max ~{MAX_BATCH_CHARS} chars)")

    # --------------------------------------------------------
    # 1. 首轮分批翻译
    # --------------------------------------------------------

    for batch_idx, batch in enumerate(batches, 1):
        group(
            f"Initial batch {batch_idx}/{len(batches)} "
            f"({batch[0][0]}-{batch[-1][0]})"
        )

        try:
            result = gemini_batch_translate(
                batch,
                cache_meta,
            )

            for idx, value in result.items():
                if not (0 <= idx < len(cues)):
                    continue

                original = prepare_gemini_text(cues[idx]["text"])

                if validate_translation(original, value):
                    translated[idx] = value

        finally:
            endgroup()

    # --------------------------------------------------------
    # 2. 收集所有缺失片段，批量重试
    # --------------------------------------------------------

    missing = [i for i in range(len(cues)) if i not in translated]

    log(f"Missing after first pass: {len(missing)}")

    for retry_round in range(1, 4):
        if not missing:
            break

        log(f"Batch retry round {retry_round}/3: " f"{len(missing)} missing cues")

        # 只对缺失片段分批，不再逐条请求。
        retry_cues = [
            {
                "timestamp": cues[i]["timestamp"],
                "text": cues[i]["text"],
                "identifier": cues[i].get("identifier"),
                "_original_index": i,
            }
            for i in missing
        ]

        retry_batches = chunk_cues_by_chars(
            retry_cues,
            max_chars=MAX_BATCH_CHARS,
        )

        log(f"Retry batches: {len(retry_batches)}")

        for batch_idx, batch in enumerate(
            retry_batches,
            1,
        ):
            group(f"Retry {retry_round}, " f"batch {batch_idx}/{len(retry_batches)}")

            try:
                # chunk_cues_by_chars 返回的是
                # (retry_cues 内的索引, 文本)。
                result = gemini_batch_translate(
                    batch,
                    cache_meta,
                )

                for local_idx, value in result.items():
                    if not (0 <= local_idx < len(retry_cues)):
                        continue

                    original_idx = retry_cues[local_idx]["_original_index"]

                    original_text = prepare_gemini_text(cues[original_idx]["text"])

                    if validate_translation(
                        original_text,
                        value,
                    ):
                        translated[original_idx] = value

            finally:
                endgroup()

        missing = [i for i in range(len(cues)) if i not in translated]

        log(f"Remaining missing cues: {len(missing)}")

    # --------------------------------------------------------
    # 3. 如果仍有缺失，放弃本集输出
    # --------------------------------------------------------

    if missing:
        for i in missing[:10]:
            log(f"Missing cue {i}: " f"{cues[i]['text'][:80]}...")

        raise RuntimeError(
            "Translation incomplete after 3 batch retries: "
            f"{len(missing)} cues missing"
        )

    # --------------------------------------------------------
    # 4. 生成双语 VTT
    # --------------------------------------------------------

    output = [
        "WEBVTT",
        "",
    ]

    for i, cue in enumerate(cues):
        output.append(cue["timestamp"])
        output.append(cue["text"])

        output.append(
            restore_speaker_translation(
                cue["text"],
                translated[i],
            )
        )

        output.append("")

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    atomic_write_text(
        target,
        "\n".join(output),
    )

    if not is_valid_vtt(target):
        raise RuntimeError(f"Generated bilingual VTT is invalid: {target}")

    log(f"Saved: {target}")


# ============================================================
# Main
# ============================================================


def main():
    separator()

    log("Translate VTT started")
    log(f"Model: {MODEL}")
    log(f"Input: {INPUT_DIR}")
    log(f"Output: {OUTPUT_DIR}")
    log(f"Max files: {MAX_FILES}")
    log(f"Max batch chars: {MAX_BATCH_CHARS}")
    log(f"RPM limit: {RPM_LIMIT} " f"(interval: {MIN_REQUEST_INTERVAL:.2f}s)")
    log(f"Daily request limit: {DAILY_REQUEST_LIMIT}")

    if MAX_FILES < 0:
        raise ValueError("MAX_FILES must be >= 0")

    if MAX_BATCH_CHARS < 1000:
        raise ValueError("MAX_BATCH_CHARS must be >= 1000")

    if not INPUT_DIR.is_dir():
        log(f"Input directory does not exist: {INPUT_DIR}")
        return 1

    cache = load_cache()

    meta = cache.setdefault(
        "__meta__",
        {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "daily_requests": 0,
        },
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if meta.get("date") != today:
        meta["date"] = today
        meta["daily_requests"] = 0

        log("New day, daily counter reset")

    meta.setdefault(
        "daily_requests",
        0,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda path: (int(path.stem) if path.stem.isdigit() else 999999),
    )

    log(f"Found source VTT files: {len(files)}")

    processed = 0
    skipped = 0
    errors = []
    stopped_by_limit = False

    try:
        for source in files:
            episode = source.stem
            target = OUTPUT_DIR / source.name

            separator()

            log(f"Processing episode {episode}")

            # ------------------------------------------------
            # 1. Validate source VTT
            # ------------------------------------------------

            if not is_valid_vtt(source):
                log(f"Invalid source VTT; skipping: {source}")
                skipped += 1
                continue

            sha = file_hash(source)

            old = cache.get(episode)

            if not isinstance(old, dict):
                old = {}

            # ------------------------------------------------
            # 2. Check local translated VTT
            # ------------------------------------------------

            if is_valid_vtt(target):
                old_hash = old.get("hash")

                # 只有原文哈希一致时才直接跳过。
                if old_hash == sha and old.get("translated"):
                    log(
                        "Local bilingual VTT valid "
                        "and source hash unchanged; skipping"
                    )

                    skipped += 1
                    continue

                # 缓存丢失时，不足以证明本地双语文件对应
                # 当前原文，因此不能盲目认定为最新译文。
                if old_hash is None:
                    log(
                        "Local bilingual VTT exists, "
                        "but no matching source hash is available"
                    )

                else:
                    log("Source hash changed; " "retranslation required")

            # ------------------------------------------------
            # 3. Restore cache if local file matches source
            # ------------------------------------------------

            # 上面的逻辑已覆盖哈希匹配的有效双语文件。
            # 只有没有有效目标文件时才需要调用 Gemini。

            if processed >= MAX_FILES:
                log("MAX_FILES reached")
                break

            if int(meta.get("daily_requests", 0)) >= DAILY_REQUEST_LIMIT:
                log("Daily request limit reached; stopping")

                stopped_by_limit = True
                break

            # ------------------------------------------------
            # 4. Translate
            # ------------------------------------------------

            group(f"Episode {episode}")

            try:
                translate_episode(
                    source,
                    target,
                    meta,
                )

                # 翻译成功后再验证文件。
                if not is_valid_vtt(target):
                    raise RuntimeError("Output VTT failed validation")

                cache[episode] = {
                    "hash": sha,
                    "translated": True,
                    "updated": datetime.now(timezone.utc).isoformat(),
                }

                processed += 1

                log(f"Translation completed: {episode}")

            except DailyLimitReached as exc:
                log(f"Daily API limit reached: {exc}")

                stopped_by_limit = True
                break

            except Exception as exc:
                log(f"Episode {episode} failed: {exc}")

                traceback.print_exc()

                errors.append((episode, str(exc)))

                continue

            finally:
                endgroup()

    finally:
        # 即使遇到 API 限额，也保存每日请求计数。
        save_cache(cache)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    separator()

    log("Finished")
    log(f"Translated episodes: {processed}")
    log(f"Skipped episodes: {skipped}")

    if errors:
        log(f"Failed episodes: {len(errors)}")

        for episode, error in errors:
            log(f"  - {episode}: {error}")

    log("Daily requests used: " f"{meta['daily_requests']}/{DAILY_REQUEST_LIMIT}")

    separator()

    if stopped_by_limit:
        log("Stopped due to daily API limit. " "Run again later to continue.")

        return 0

    if errors:
        log("Completed with errors.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
