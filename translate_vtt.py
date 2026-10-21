#!/usr/bin/env python3

import os
import re
import sys
import json
import time
import hashlib
import logging
import random
import traceback
import tempfile

from pathlib import Path
from datetime import datetime, timedelta, timezone

from google import genai


def _quota_timezone():
    """
    Gemini 的每日配额按太平洋时间午夜重置。
    Windows 缺少 IANA 时区数据库时退化为固定 UTC-8。
    """

    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Los_Angeles")

    except Exception:
        return timezone(timedelta(hours=-8))


QUOTA_TZ = _quota_timezone()


def quota_today():
    return datetime.now(QUOTA_TZ).strftime("%Y-%m-%d")


# ============================================================
# Configuration
# ============================================================

INPUT_DIR = Path("docs/transcripts")
OUTPUT_DIR = Path("docs/transcripts/zh")
CACHE_FILE = Path("docs/translations.json")

MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite",
)

MAX_FILES = int(os.environ.get("MAX_FILES", "10"))

# 降低批次大小，减少输出过长导致解析失败的概率。
MAX_BATCH_CHARS = int(os.environ.get("MAX_BATCH_CHARS", "15000"))

# 普通 API 错误的最大重试次数。
RETRY_COUNT = 5

RETRY_BASE = 20

# Gemini 每分钟请求数限制。
RPM_LIMIT = int(os.environ.get("RPM_LIMIT", "14"))

# 每日 API 请求预算。
DAILY_REQUEST_LIMIT = int(os.environ.get("DAILY_REQUEST_LIMIT", "500"))

if RPM_LIMIT < 1:
    raise ValueError("RPM_LIMIT must be >= 1")

if MAX_BATCH_CHARS < 1000:
    raise ValueError("MAX_BATCH_CHARS must be >= 1000")

# 过大的批次容易触发输出截断或客户端超时。
if MAX_BATCH_CHARS > 60000:
    raise ValueError("MAX_BATCH_CHARS must be <= 60000")

# 个别无法翻译的 cue 允许回退为原文，但比例过高说明 API/校验异常。
MAX_FALLBACK_RATIO = 0.10

if MAX_FILES < 0:
    raise ValueError("MAX_FILES must be >= 0")

MIN_REQUEST_INTERVAL = 60.0 / RPM_LIMIT

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


# ============================================================
# Logging
# ============================================================

if IN_ACTIONS:
    log_format = "[%(levelname)s] %(message)s"
else:
    log_format = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("vtt_translator")


def log(message=""):
    logger.info(message)


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
# Exceptions
# ============================================================


class DailyLimitReached(Exception):
    """每日 API 请求上限已达到。"""

    pass


# ============================================================
# Initialize Gemini
# ============================================================

group("Initializing Gemini")

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    endgroup()
    raise RuntimeError("GEMINI_API_KEY environment variable is missing")

client = genai.Client(
    api_key=api_key,
    http_options={"timeout": 120000},
)

log(f"Gemini initialized; model={MODEL}")

endgroup()

_last_request_time = 0.0


# ============================================================
# Atomic file writing
# ============================================================


def atomic_write_text(path: Path, content: str):
    """
    先写入同目录临时文件，再替换目标文件。
    避免程序中断时留下半截缓存或字幕文件。
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
        log("Translation cache not found; starting fresh")
        return {}

    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Cache root must be a JSON object")

        meta = data.setdefault(
            "__meta__",
            {},
        )

        if not isinstance(meta, dict):
            raise ValueError("Cache metadata is invalid")

        today = quota_today()

        if meta.get("date") != today:
            meta["date"] = today
            meta["daily_requests"] = 0

            log("UTC date changed; resetting daily request counter")

        meta.setdefault(
            "daily_requests",
            0,
        )

        log(f"Loaded cache: {len(data) - 1} episodes")

        return data

    except Exception as exc:
        # 不要在缓存损坏时自动覆盖旧文件。
        raise RuntimeError(f"Failed to load translation cache: {exc}") from exc


def save_cache(cache):
    atomic_write_text(
        CACHE_FILE,
        json.dumps(
            cache,
            indent=2,
            ensure_ascii=False,
        ),
    )

    log("Translation cache saved")


# ============================================================
# File hash
# ============================================================


def file_hash(path: Path):
    """
    计算源文件 hash。

    统一 CRLF/LF，避免 Windows 与 Linux 的换行符差异
    把未修改的源文件误判为“已变化”。
    """

    data = Path(path).read_bytes().replace(b"\r\n", b"\n")

    return hashlib.sha256(data).hexdigest()


# ============================================================
# VTT parser
# ============================================================


def parse_vtt_cues(content: str) -> list:
    """
    解析 WebVTT 字幕。

    支持：
    - WEBVTT 头
    - cue 标识符
    - 多行字幕
    - NOTE / STYLE / REGION 区域
    - speaker 标签
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
# VTT validation
# ============================================================


def is_valid_vtt(path: Path) -> bool:
    """
    验证本地 VTT 是否存在且具有基本有效结构。
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

        return bool(parse_vtt_cues(content))

    except (OSError, UnicodeError, ValueError):
        return False


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
    """
    发送给 Gemini 的内容不包含 speaker 元数据。
    """

    _, content = extract_speaker(text)

    return content


def restore_speaker_translation(
    original,
    translated,
):
    """
    双语 VTT 的中文行保留原来的 speaker 标签。
    """

    speaker, _ = extract_speaker(original)

    if speaker:
        return f"<v {speaker}>{translated}"

    return translated


# ============================================================
# Prompt
# ============================================================


def build_prompt(blocks):
    content = []

    for idx, text in blocks:
        content.append(f"{idx}|||{text}")

    joined = "\n\n".join(content)

    return f"""You are translating English podcast subtitles into Simplified Chinese.

Translate every subtitle block faithfully and naturally.

Rules:
1. Translate the complete meaning of each English subtitle.
2. Do not summarize, omit, or merge subtitle blocks.
3. Speaker names are metadata and must not be translated.
4. Translate only the subtitle text.
5. Keep the original subtitle order.
6. Return every block using the exact original numeric ID.
7. Do not change IDs.
8. Do not add introductions or explanations.
9. Do not use Markdown or code fences.
10. Do not add any extra lines.

Required output format:
0|||Chinese translation
1|||Chinese translation
2|||Chinese translation

Each output line must start with its exact numeric ID,
followed by three pipe characters: |||, then the translation.

Subtitle blocks:
{joined}
"""


# ============================================================
# Parse Gemini response
# ============================================================


def parse_translation_response(text):
    """
    解析 ID|||译文 格式。
    兼容部分模型可能返回的 ID|译文 格式。
    """

    result = {}

    if not text:
        return result

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        line = re.sub(
            r"^\s*(?:[-*]\s+)",
            "",
            line,
        )

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
        value = value.strip()

        if not re.fullmatch(
            r"\d+",
            idx,
        ):
            continue

        if value:
            result[int(idx)] = value

    return result


def validate_translation(
    original: str,
    translated: str,
) -> bool:
    """
    进行基本的翻译结果检查。
    """

    if not translated:
        return False

    translated = translated.strip()

    if len(translated) < 2:
        return False

    normalized_original = original.lower().strip(" .,!?:;")

    normalized_translation = translated.lower().strip(" .,!?:;")

    if normalized_translation == normalized_original:
        return False

    # 对于英文播客的中文翻译，要求至少包含一个汉字。
    if not re.search(
        r"[\u4e00-\u9fff]",
        translated,
    ):
        return False

    return True


# ============================================================
# Rate limiting
# ============================================================


def _rate_limit_wait():
    global _last_request_time

    now = time.time()

    elapsed = now - _last_request_time

    if elapsed < MIN_REQUEST_INTERVAL:
        sleep_time = MIN_REQUEST_INTERVAL - elapsed

        log(f"RPM throttle: sleeping {sleep_time:.1f}s")

        time.sleep(sleep_time)

    _last_request_time = time.time()


# ============================================================
# Retry helpers
# ============================================================


def is_retryable_error(error) -> bool:
    text = str(error).lower()

    retryable_keywords = [
        "429",
        "too many requests",
        "rate limit",
        "quota",
        "500",
        "internal server error",
        "502",
        "503",
        "504",
        "service unavailable",
        "gateway",
        "timeout",
        "timed out",
        "deadline",
        "connection",
        "network",
        "unreachable",
        "temporarily unavailable",
    ]

    return any(keyword in text for keyword in retryable_keywords)


def is_daily_quota_error(error) -> bool:
    """
    区分“每日配额耗尽”和普通的 RPM 限流。

    每日配额耗尽重试没有意义，应立即停止本次运行。
    """

    text = str(error).lower()

    daily_keywords = [
        "perday",
        "per day",
        "requests per day",
        "daily quota",
        "daily limit",
    ]

    return any(keyword in text for keyword in daily_keywords)


def retry_wait(attempt):
    return RETRY_BASE * (2 ** (attempt - 1)) + random.uniform(0, 5)


# ============================================================
# Gemini API call
# ============================================================


def call_gemini_once(
    blocks,
    cache_meta,
):
    """
    只发送一次 API 请求。

    此函数不负责重试，也不负责拆批。
    空响应会返回空字典，由上层函数决定如何拆批。
    """

    if not blocks:
        return {}

    daily_used = int(
        cache_meta.get(
            "daily_requests",
            0,
        )
    )

    if daily_used >= DAILY_REQUEST_LIMIT:
        raise DailyLimitReached(
            f"Daily request limit reached: " f"{daily_used}/{DAILY_REQUEST_LIMIT}"
        )

    prompt = build_prompt(blocks)

    _rate_limit_wait()

    # 每次尝试都计数，包括 HTTP 200 但无有效解析结果的请求。
    cache_meta["daily_requests"] = daily_used + 1

    log(
        f"Request #{cache_meta['daily_requests']}/"
        f"{DAILY_REQUEST_LIMIT}; "
        f"blocks={len(blocks)}; "
        f"prompt_chars={len(prompt)}"
    )

    start = time.time()

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
    )

    elapsed = time.time() - start

    raw = response.text or ""

    parsed = parse_translation_response(raw)

    candidates = (
        getattr(
            response,
            "candidates",
            None,
        )
        or []
    )

    if candidates:
        candidate = candidates[0]

        finish_reason = getattr(
            candidate,
            "finish_reason",
            None,
        )

        finish_message = getattr(
            candidate,
            "finish_message",
            None,
        )

    else:
        finish_reason = None
        finish_message = None

    prompt_feedback = getattr(
        response,
        "prompt_feedback",
        None,
    )

    usage = getattr(
        response,
        "usage_metadata",
        None,
    )

    log(
        f"Response in {elapsed:.2f}s; "
        f"parsed={len(parsed)}/{len(blocks)}; "
        f"finish_reason={finish_reason}; "
        f"finish_message={finish_message}"
    )

    if prompt_feedback:
        log(f"Prompt feedback: {prompt_feedback}")

    if usage:
        log(f"Token usage: {usage}")

    if not parsed:
        log("Empty parse result; response preview: " f"{raw[:500]!r}")

    return parsed


# ============================================================
# Smart batch translation
# ============================================================


def gemini_batch_translate(
    blocks,
    cache_meta,
    depth=0,
):
    """
    智能批量翻译：

    - 正常返回部分结果：保留结果，由上层批量补漏。
    - 多片段完全无法解析：拆成两半分别请求。
    - 单片段无法解析：有限重试。
    - 不会对同一个大批次反复发送相同请求。
    - 每次 API 调用都会计入每日请求数。
    """

    if not blocks:
        return {}

    # 防止异常数据导致无限递归。
    MAX_SPLIT_DEPTH = 12

    try:
        result = call_gemini_once(
            blocks,
            cache_meta,
        )

        # 有部分结果就保留，不必因个别缺失而丢弃整批。
        if result:
            return result

        # 整批为空时，优先拆分。
        if len(blocks) > 1:
            if depth >= MAX_SPLIT_DEPTH:
                log(
                    "Maximum split depth reached; "
                    f"returning empty result for {len(blocks)} blocks"
                )

                return {}

            midpoint = len(blocks) // 2

            left = blocks[:midpoint]
            right = blocks[midpoint:]

            log(
                f"Empty batch; splitting "
                f"{len(blocks)} -> "
                f"{len(left)} + {len(right)}"
            )

            left_result = gemini_batch_translate(
                left,
                cache_meta,
                depth=depth + 1,
            )

            right_result = gemini_batch_translate(
                right,
                cache_meta,
                depth=depth + 1,
            )

            combined = {}
            combined.update(left_result)
            combined.update(right_result)

            return combined

        # 只有剩下单个片段时才重试相同请求。
        log("Single-block empty result; " "retrying the same block")

    except DailyLimitReached:
        raise

    except Exception as exc:
        if is_daily_quota_error(exc):
            raise DailyLimitReached(str(exc)) from exc

        if not is_retryable_error(exc):
            raise

        log(f"Retryable API error: " f"{type(exc).__name__}: {exc}")

    # 单片段重试；多片段请求发生 API 临时错误时，
    # 先重试该批次的 API 错误，而不是将网络故障误判为空响应。
    if len(blocks) > 1:
        for attempt in range(1, RETRY_COUNT + 1):
            wait = retry_wait(attempt)

            log(
                f"Transient error; retrying batch "
                f"in {wait:.1f}s "
                f"(attempt {attempt}/{RETRY_COUNT})"
            )

            time.sleep(wait)

            try:
                result = call_gemini_once(
                    blocks,
                    cache_meta,
                )

                if result:
                    return result

                # HTTP 请求成功但无解析结果，停止重复请求大批次，
                # 交给拆批逻辑。
                midpoint = len(blocks) // 2

                log(f"Batch still empty; splitting " f"{len(blocks)} blocks")

                left_result = gemini_batch_translate(
                    blocks[:midpoint],
                    cache_meta,
                    depth=depth + 1,
                )

                right_result = gemini_batch_translate(
                    blocks[midpoint:],
                    cache_meta,
                    depth=depth + 1,
                )

                combined = {}
                combined.update(left_result)
                combined.update(right_result)

                return combined

            except DailyLimitReached:
                raise

            except Exception as exc:
                if is_daily_quota_error(exc):
                    raise DailyLimitReached(str(exc)) from exc

                if not is_retryable_error(exc):
                    raise

                log(f"Retry failed: {exc}")

        raise RuntimeError(
            f"Gemini request failed after retries "
            f"for a batch of {len(blocks)} blocks"
        )

    # 单片段重试。
    for attempt in range(1, RETRY_COUNT + 1):
        wait = retry_wait(attempt)

        log(
            f"Retrying single block in {wait:.1f}s "
            f"(attempt {attempt}/{RETRY_COUNT})"
        )

        time.sleep(wait)

        try:
            result = call_gemini_once(
                blocks,
                cache_meta,
            )

            if result:
                return result

        except DailyLimitReached:
            raise

        except Exception as exc:
            if is_daily_quota_error(exc):
                raise DailyLimitReached(str(exc)) from exc

            if not is_retryable_error(exc):
                raise

            log(f"Single-block retry failed: {exc}")

    log("Single block still has no valid translation")

    return {}


# ============================================================
# Batch creation
# ============================================================


def chunk_cues_by_chars(
    cues,
    max_chars=MAX_BATCH_CHARS,
    skip_indices=None,
):
    """
    按字符数拆分字幕。

    返回列表中的每个元素为：
        (原始字幕索引, 发送给 Gemini 的文本)

    保留原始索引，便于将结果放回正确的字幕位置。
    skip_indices 中的 cue 不参与请求（例如纯数字/符号）。
    """

    batches = []

    current = []
    current_len = 500

    for i, cue in enumerate(cues):
        if skip_indices and i in skip_indices:
            continue

        text = prepare_gemini_text(cue["text"])

        item_len = len(text) + 20

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
# Translate one episode
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

    if not cues:
        raise RuntimeError("Source VTT contains no subtitle cues")

    log(f"Total subtitle cues: {len(cues)}")

    translated = {}

    # 纯数字、电话号码、符号等 cue 没有可翻译语言内容。
    # 直接复制原文，既不请求 API，也不会被“必须包含汉字”的校验拒绝。
    for index, cue in enumerate(cues):
        text = prepare_gemini_text(cue["text"])

        if not re.search(r"[A-Za-z]", text):
            translated[index] = text

    if translated:
        log(f"Pre-filled non-linguistic cues: {len(translated)}")

    # --------------------------------------------------------
    # 1. Initial translation
    # --------------------------------------------------------

    batches = chunk_cues_by_chars(
        cues,
        max_chars=MAX_BATCH_CHARS,
        skip_indices=set(translated),
    )

    log(f"Initial batches: {len(batches)}; " f"max chars per batch: {MAX_BATCH_CHARS}")

    for batch_idx, batch in enumerate(
        batches,
        1,
    ):
        group(f"Initial batch {batch_idx}/{len(batches)} " f"({len(batch)} cues)")

        try:
            result = gemini_batch_translate(
                batch,
                cache_meta,
            )

            for idx, value in result.items():
                if not (0 <= idx < len(cues)):
                    continue

                original = prepare_gemini_text(cues[idx]["text"])

                if validate_translation(
                    original,
                    value,
                ):
                    translated[idx] = value

        finally:
            endgroup()

    # --------------------------------------------------------
    # 2. Batch retry for missing cues
    # --------------------------------------------------------

    MAX_MISSING_RETRY_ROUNDS = 3

    for retry_round in range(
        1,
        MAX_MISSING_RETRY_ROUNDS + 1,
    ):
        missing = [i for i in range(len(cues)) if i not in translated]

        if not missing:
            break

        log(
            f"Missing cues after pass: {len(missing)}; "
            f"batch retry round "
            f"{retry_round}/{MAX_MISSING_RETRY_ROUNDS}"
        )

        # 构造缺失片段集合。
        retry_cues = [cues[i] for i in missing]

        retry_batches = chunk_cues_by_chars(
            retry_cues,
            max_chars=MAX_BATCH_CHARS,
        )

        log(f"Missing-cue retry batches: " f"{len(retry_batches)}")

        for batch_idx, batch in enumerate(
            retry_batches,
            1,
        ):
            group(
                f"Missing retry {retry_round}, "
                f"batch {batch_idx}/{len(retry_batches)}"
            )

            try:
                result = gemini_batch_translate(
                    batch,
                    cache_meta,
                )

                for local_idx, value in result.items():
                    if not (0 <= local_idx < len(missing)):
                        continue

                    original_idx = missing[local_idx]

                    original = prepare_gemini_text(cues[original_idx]["text"])

                    if validate_translation(
                        original,
                        value,
                    ):
                        translated[original_idx] = value

            finally:
                endgroup()

    # --------------------------------------------------------
    # 3. Verify completeness
    # --------------------------------------------------------

    missing = [i for i in range(len(cues)) if i not in translated]

    if missing:
        missing_ratio = len(missing) / len(cues)

        log(
            f"Translation incomplete: {len(missing)} cues missing "
            f"({missing_ratio:.1%})"
        )

        for i in missing[:10]:
            log(f"Missing cue {i}: {cues[i]['text'][:80]}")

        # 少量专名、拟声词、无法翻成中文的短句允许回退为原文；
        # 比例过高则说明 API/校验异常，不能悄悄生成一份半英文文件。
        # 短集允许最多 3 条绝对缺失，避免 1 条专名就导致整集失败。
        if len(missing) > 3 and missing_ratio > MAX_FALLBACK_RATIO:
            raise RuntimeError(
                "Translation incomplete after batch retries: "
                f"{len(missing)} cues missing ({missing_ratio:.1%})"
            )

        for i in missing:
            translated[i] = prepare_gemini_text(cues[i]["text"])

        log(
            f"Fallback to English for {len(missing)} cues "
            f"({missing_ratio:.1%})"
        )

    fallback_count = len(missing)

    # --------------------------------------------------------
    # 4. Build bilingual VTT
    # --------------------------------------------------------

    output = [
        "WEBVTT",
        "",
    ]

    for i, cue in enumerate(cues):
        output.append(cue["timestamp"])

        # English original.
        output.append(cue["text"])

        # Chinese translation; speaker metadata is preserved.
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

    log(f"Saved bilingual VTT: {target}")

    return fallback_count


# ============================================================
# Main
# ============================================================


def main():
    separator()

    log("VTT translation started")
    log(f"Model: {MODEL}")
    log(f"Input directory: {INPUT_DIR}")
    log(f"Output directory: {OUTPUT_DIR}")
    log(f"Cache file: {CACHE_FILE}")
    log(f"MAX_FILES: {MAX_FILES}")
    log(f"MAX_BATCH_CHARS: {MAX_BATCH_CHARS}")
    log(f"RPM_LIMIT: {RPM_LIMIT}")
    log(f"DAILY_REQUEST_LIMIT: {DAILY_REQUEST_LIMIT}")

    if not INPUT_DIR.is_dir():
        log(f"Input directory does not exist: {INPUT_DIR}")

        return 1

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache = load_cache()

    meta = cache.setdefault(
        "__meta__",
        {},
    )

    today = quota_today()

    if meta.get("date") != today:
        meta["date"] = today
        meta["daily_requests"] = 0

    meta.setdefault(
        "daily_requests",
        0,
    )

    # 只读取输入目录顶层的 VTT 文件，
    # 避免把 zh 子目录里的双语文件再次作为源文件。
    # 新集（编号更大）优先处理，积压时不会一直翻译旧集。
    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda path: (
            (
                0,
                -int(path.stem),
            )
            if path.stem.isdigit()
            else (
                1,
                path.stem.lower(),
            )
        ),
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

            log(f"Processing episode: {episode}")

            # ------------------------------------------------
            # 1. Check source file
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
            # 2. Check local bilingual VTT
            # ------------------------------------------------

            if is_valid_vtt(target):
                old_hash = old.get("hash")

                if old_hash == sha and old.get("translated") is True:
                    log(
                        "Valid local bilingual VTT and "
                        "matching source hash; skipping Gemini"
                    )

                    skipped += 1
                    continue

                if old_hash is None:
                    log(
                        "Local bilingual VTT exists, "
                        "but cache has no source hash; "
                        "retranslating to verify consistency"
                    )

                else:
                    log(
                        "Source hash changed or translation "
                        "cache is incomplete; retranslating"
                    )

            # ------------------------------------------------
            # 3. Check processing limit
            # ------------------------------------------------

            if processed >= MAX_FILES:
                log("MAX_FILES reached; stopping")

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
                fallback_count = translate_episode(
                    source,
                    target,
                    meta,
                )

                # 只有翻译完成且目标文件有效后才写成功缓存。
                if not is_valid_vtt(target):
                    raise RuntimeError("Output VTT validation failed")

                cache[episode] = {
                    "hash": sha,
                    "translated": True,
                    "fallback": fallback_count,
                    "updated": datetime.now(timezone.utc).isoformat(),
                }

                processed += 1

                log(f"Episode completed: {episode}")

                # 逐集保存，进程被硬中断时不会丢掉已消耗的配额和已完成集。
                save_cache(cache)

            except DailyLimitReached as exc:
                log(f"Daily API limit reached: {exc}")

                stopped_by_limit = True
                break

            except Exception as exc:
                log(f"Episode {episode} failed: " f"{type(exc).__name__}: {exc}")

                traceback.print_exc()

                errors.append((episode, str(exc)))

                save_cache(cache)

                continue

            finally:
                endgroup()

    finally:
        # 保留 API 请求计数，即使中途触发限额或单集失败。
        save_cache(cache)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    separator()

    log("Translation run finished")
    log(f"Successfully translated: {processed}")
    log(f"Skipped: {skipped}")
    log(f"Failed: {len(errors)}")
    log(
        f"Daily requests: " f"{meta.get('daily_requests', 0)}/" f"{DAILY_REQUEST_LIMIT}"
    )

    if errors:
        log("Failed episodes:")

        for episode, error in errors:
            log(f"  - {episode}: {error}")

    if stopped_by_limit:
        log(
            "Stopped because the daily API request limit "
            "was reached. Run again later to continue."
        )

    separator()

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
