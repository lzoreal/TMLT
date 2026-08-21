#!/usr/bin/env python3

import os
import re
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone


from google import genai

# ============================================================
# Config
# ============================================================


INPUT_DIR = Path("docs/transcripts")


OUTPUT_DIR = Path("docs/transcripts/zh")


CACHE_FILE = Path("docs/translations.json")


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")


# GitHub Actions input
MAX_FILES = int(os.environ.get("MAX_FILES", "3"))


MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "300"))


# 一个请求包含多少 VTT block

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))


# 免费额度安全设置

REQUEST_INTERVAL = int(os.environ.get("REQUEST_INTERVAL", "15"))


MAX_RETRY = int(os.environ.get("MAX_RETRY", "8"))


# ============================================================
# Logging
# ============================================================


START_TIME = time.time()


def log(msg):

    elapsed = time.time() - START_TIME

    print(f"[{elapsed:8.1f}s] {msg}", flush=True)


def separator():

    print("=" * 70, flush=True)


# ============================================================
# Gemini
# ============================================================


log("Initializing Gemini...")


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


log("✅ Gemini initialized")


request_count = 0


# ============================================================
# Cache
# ============================================================


def load_cache():

    if not CACHE_FILE.exists():

        log("ℹ️ Cache not found")

        return {}

    try:

        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

        log(f"Loaded cache: {len(data)} episodes")

        return data

    except Exception as e:

        log(f"⚠️ Cache load failed: {e}")

        return {}


def save_cache(data):

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    CACHE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log(f"💾 Cache saved: {CACHE_FILE}")


# ============================================================
# Hash
# ============================================================


def file_hash(path):

    sha = hashlib.sha256()

    with path.open("rb") as f:

        for chunk in iter(lambda: f.read(8192), b""):

            sha.update(chunk)

    return sha.hexdigest()


# ============================================================
# Retry helper
# ============================================================


def get_retry_seconds(error):

    text = str(error)

    m = re.search(r"retry in ([0-9.]+)s", text)

    if m:

        return float(m.group(1)) + 5

    return 30


# ============================================================
# Gemini batch translation
# ============================================================


def gemini_batch_translate(blocks):

    global request_count

    if not blocks:

        return {}

    if request_count >= MAX_REQUESTS:

        raise RuntimeError("MAX_REQUESTS reached")

    request_count += 1

    separator()

    log(f"🤖 Gemini request #{request_count}")

    log(f"Blocks: {len(blocks)}")

    # ========================================================
    # 构造编号输入
    #
    # 让 Gemini 返回:
    #
    # {
    #   "0":"翻译",
    #   "1":"翻译"
    # }
    #
    # ========================================================

    payload = []

    for index, text in blocks:

        payload.append({"id": index, "text": text})

    prompt = f"""
You are a professional subtitle translator.

Translate the following English podcast transcript
into Simplified Chinese.

Rules:

1. Output ONLY valid JSON.
2. Do not use markdown.
3. Do not add explanation.
4. Keep the same ids.
5. Preserve speaker names.
6. Translate naturally.

Format:

{{
 "0": "Chinese translation",
 "1": "Chinese translation"
}}


Input:

{json.dumps(
    payload,
    ensure_ascii=False,
    indent=2
)}

"""

    for retry in range(MAX_RETRY):

        try:

            start = time.time()

            response = client.models.generate_content(model=MODEL, contents=prompt)

            cost = time.time() - start

            log(f"✅ Gemini success " f"time={cost:.2f}s")

            text = response.text.strip()

            # ------------------------------------------------
            # 清理 Gemini markdown
            # ------------------------------------------------

            text = re.sub(r"^```json", "", text)

            text = re.sub(r"```$", "", text)

            text = text.strip()

            # ------------------------------------------------
            # JSON parse
            # ------------------------------------------------

            try:

                result = json.loads(text)

            except Exception as e:

                log("⚠️ JSON parse failed")

                log(text[:500])

                return {}

            if not isinstance(result, dict):

                log("⚠️ JSON is not object")

                return {}

            log(f"Parsed translations: {len(result)}")

            return result

        except Exception as e:

            msg = str(e)

            log(f"❌ Gemini error: {msg}")

            # =================================================
            # 429 quota
            # =================================================

            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:

                wait = get_retry_seconds(e)

                log(f"⏳ Rate limit wait {wait}s")

                time.sleep(wait)

                continue

            # =================================================
            # 503 overload
            # =================================================

            if "503" in msg or "UNAVAILABLE" in msg:

                wait = 20 * (retry + 1)

                log(f"⏳ Server busy wait {wait}s")

                time.sleep(wait)

                continue

            # =================================================
            # Other errors
            # =================================================

            wait = 10 * (retry + 1)

            log(f"Retry after {wait}s")

            time.sleep(wait)

    raise RuntimeError("Gemini failed after retries")


# ============================================================
# Batch helper
# ============================================================


def translate_blocks_batch(blocks):
    """
    blocks:

    [
      {
        id:0,
        text:"hello"
      }
    ]

    """

    translated = {}

    total = len(blocks)

    for start in range(0, total, BATCH_SIZE):

        batch = blocks[start : start + BATCH_SIZE]

        log(f"Batch " f"{start+1}-" f"{min(start+BATCH_SIZE,total)} " f"/ {total}")

        request_data = []

        for item in batch:

            request_data.append((item["id"], item["text"]))

        result = gemini_batch_translate(request_data)

        for key, value in result.items():

            try:

                translated[int(key)] = value

            except Exception:

                translated[key] = value

        # ====================================================
        # 防止免费额度过快
        # ====================================================

        if start + BATCH_SIZE < total:

            log(f"⏳ Rate sleep " f"{REQUEST_INTERVAL}s")

            time.sleep(REQUEST_INTERVAL)

    log(f"Total translated blocks: " f"{len(translated)}/{total}")

    return translated


# ============================================================
# VTT parser
# ============================================================


def split_vtt_blocks(content):
    """
    分割 VTT cue

    保留:

    WEBVTT

    timestamp

    subtitle text

    """

    blocks = re.split(r"\n\s*\n", content.strip())

    return [b.strip() for b in blocks if b.strip()]


def parse_vtt_block(block):

    lines = block.splitlines()

    if not lines:

        return None

    # WEBVTT header

    if lines[0].strip() == "WEBVTT":

        return {"header": True, "raw": block}

    timestamp = None

    text_lines = []

    for line in lines:

        if "-->" in line:

            timestamp = line.strip()

        elif timestamp:

            text_lines.append(line)

    if not timestamp:

        return None

    text = "\n".join(text_lines).strip()

    if not text:

        return None

    return {"header": False, "timestamp": timestamp, "text": text, "raw": block}


# ============================================================
# Speaker protection
# ============================================================


def protect_speaker(text):

    speakers = {}

    def replace(match):

        key = f"__SPEAKER_{len(speakers)}__"

        speakers[key] = match.group(0)

        return key

    protected = re.sub(r"<v\s+[^>]+>", replace, text)

    return protected, speakers


def restore_speaker(text, speakers):

    for key, value in speakers.items():

        text = text.replace(key, value)

    return text


# ============================================================
# Prepare translation blocks
# ============================================================


def prepare_blocks(parsed_blocks):
    """
    转换成 Gemini 输入格式


    返回:

    [
       {
          id:0,
          text:"..."
       }
    ]

    """

    result = []

    for index, item in enumerate(parsed_blocks):

        if item.get("header"):

            continue

        text = item["text"]

        protected, _ = protect_speaker(text)

        result.append({"id": index, "text": protected})

    return result


# ============================================================
# Generate bilingual VTT
# ============================================================


def create_bilingual_vtt(parsed_blocks, translations):

    output = []

    output.append("WEBVTT")

    output.append("")

    translated_count = 0

    for index, item in enumerate(parsed_blocks):

        if item.get("header"):

            continue

        timestamp = item["timestamp"]

        original = item["text"]

        chinese = translations.get(index, "")

        if chinese:

            _, speakers = protect_speaker(original)

            chinese = restore_speaker(chinese, speakers)

            translated_count += 1

        else:

            chinese = "【翻译失败】"

        output.append(timestamp)

        # -------------------------------
        # English
        # -------------------------------

        output.append(original)

        # -------------------------------
        # Chinese
        # -------------------------------

        output.append(chinese)

        output.append("")

    log(f"Translated blocks written: " f"{translated_count}/" f"{len(parsed_blocks)}")

    return "\n".join(output)


# ============================================================
# Block cache
# ============================================================


def get_block_hash(text):

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_episode_cache(cache, episode):

    return cache.get(episode, {"blocks": {}})


def save_episode_cache(cache, episode, data):

    cache[episode] = data


# ============================================================
# Translate one VTT file
# ============================================================


def translate_vtt_file(source, target, cache):

    separator()

    log(f"📄 Translating {source.name}")

    content = source.read_text(encoding="utf-8")

    blocks = split_vtt_blocks(content)

    log(f"VTT blocks: {len(blocks)}")

    parsed = []

    for block in blocks:

        item = parse_vtt_block(block)

        if item:

            parsed.append(item)

    log(f"Parsed blocks: {len(parsed)}")

    episode = source.stem

    episode_cache = load_episode_cache(cache, episode)

    old_blocks = episode_cache.get("blocks", {})

    need_translate = []

    translations = {}

    for index, item in enumerate(parsed):

        if item.get("header"):

            continue

        block_hash = get_block_hash(item["text"])

        old = old_blocks.get(str(index))

        if old and old.get("hash") == block_hash:

            translations[index] = old["zh"]

        else:

            need_translate.append({"id": index, "text": item["text"]})

    log(f"Cached blocks: " f"{len(translations)}")

    log(f"Need Gemini: " f"{len(need_translate)}")

    if need_translate:

        new_translations = translate_blocks_batch(need_translate)

        translations.update(new_translations)

    # 更新 block cache

    new_cache = {}

    for index, item in enumerate(parsed):

        if item.get("header"):

            continue

        zh = translations.get(index, "")

        new_cache[str(index)] = {"hash": get_block_hash(item["text"]), "zh": zh}

    episode_cache["blocks"] = new_cache

    save_episode_cache(cache, episode, episode_cache)

    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(create_bilingual_vtt(parsed, translations), encoding="utf-8")

    log(f"✅ Saved: {target}")

    return True


# ============================================================
# Main
# ============================================================


def main():

    separator()

    log("🚀 Translate VTT started")

    separator()

    log(f"Model: {MODEL}")

    log(f"Input: {INPUT_DIR}")

    log(f"Output: {OUTPUT_DIR}")

    log(f"Max files: {MAX_FILES}")

    log(f"Max requests: {MAX_REQUESTS}")

    log(f"Batch size: {BATCH_SIZE}")

    log(f"Request interval: {REQUEST_INTERVAL}s")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cache = load_cache()

    # ========================================================
    # Find VTT
    # ========================================================

    files = sorted(
        INPUT_DIR.glob("*.vtt"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0
    )

    log(f"Found VTT: {len(files)}")

    if not files:

        log("No VTT files found")

        return

    translated_files = 0

    skipped_files = 0

    start_time = time.time()

    # ========================================================
    # Process
    # ========================================================

    for source in files:

        if translated_files >= MAX_FILES:

            log(f"Reached MAX_FILES={MAX_FILES}")

            break

        target = OUTPUT_DIR / source.name

        file_sha = file_hash(source)

        episode_cache = cache.get(source.stem, {})

        # ====================================================
        # File level cache
        # ====================================================

        if target.exists() and episode_cache.get("file_hash") == file_sha:

            log(f"⏭ Skip {source.name} " "(unchanged)")

            skipped_files += 1

            continue

        try:

            translate_vtt_file(source, target, cache)

            cache.setdefault(source.stem, {})

            cache[source.stem]["file_hash"] = file_sha

            cache[source.stem]["updated"] = datetime.now(timezone.utc).isoformat()

            save_cache(cache)

            translated_files += 1

        except Exception as e:

            log(f"❌ Failed {source.name}: {e}")

    # ========================================================
    # Final save
    # ========================================================

    save_cache(cache)

    elapsed = time.time() - start_time

    separator()

    log("🎉 Finished")

    log(f"Translated files: " f"{translated_files}")

    log(f"Skipped files: " f"{skipped_files}")

    log(f"Gemini requests: " f"{request_count}")

    log(f"Time: {elapsed:.1f}s")

    separator()


# ============================================================
# Entry
# ============================================================


if __name__ == "__main__":

    main()
