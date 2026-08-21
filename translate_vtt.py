#!/usr/bin/env python3

import os
import re
import json
import time
import hashlib

from pathlib import Path
from datetime import datetime, timezone

from google import genai
from google.genai import types

# ============================================================
# Config
# ============================================================


INPUT_DIR = Path(os.environ.get("INPUT_DIR", "docs/transcripts"))


OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs/transcripts/zh"))


CACHE_FILE = Path(os.environ.get("CACHE_FILE", "docs/translations.json"))


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


# 每次发送多少个字幕给 Gemini

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))


MAX_FILES = int(os.environ.get("MAX_FILES", "10"))


MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "300"))


REQUEST_INTERVAL = int(os.environ.get("REQUEST_INTERVAL", "3"))


# ============================================================
# Gemini Client
# ============================================================


print("Initializing Gemini...")


client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=600000)
)


print("✅ Gemini initialized")


request_count = 0


# ============================================================
# Cache
# ============================================================


def load_cache():

    if not CACHE_FILE.exists():

        print("ℹ️ Cache not found")

        return {}

    try:

        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

        print("✅ Cache loaded:", len(data))

        return data

    except Exception as e:

        print("⚠️ Cache error:", e)

        return {}


def save_cache(data):

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    CACHE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


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
# Gemini translate
# ============================================================


def gemini_translate(text):

    global request_count

    if not text.strip():
        return ""

    if request_count >= MAX_REQUESTS:

        raise RuntimeError(f"MAX_REQUESTS reached: {MAX_REQUESTS}")

    request_count += 1

    print(f"\n🤖 Gemini request #{request_count}")

    print(f"   Input chars: {len(text)}")

    prompt = f"""
Translate this English podcast transcript into Simplified Chinese.

Requirements:

1. Only output the Chinese translation.
2. Do not summarize.
3. Do not explain.
4. Keep speaker names unchanged.
5. Keep punctuation natural.
6. Preserve the original meaning.

English:

{text}
"""

    start = time.time()

    for retry in range(5):

        try:

            print(f"   Gemini attempt {retry + 1}/5")

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
            )

            if not response.text:

                raise RuntimeError("Gemini returned empty response")

            result = response.text.strip()

            elapsed = time.time() - start

            print("   ✅ Gemini success")

            print(f"   Output chars: {len(result)}")

            print(f"   Time: {elapsed:.2f}s")

            return result

        except Exception as e:

            print("   ❌ Gemini error:", e)

            wait = 10 * (retry + 1)

            print(f"   ⏳ Retry after {wait}s")

            time.sleep(wait)

    raise RuntimeError("Gemini translation failed after retries")


# ============================================================
# Protect VTT speaker tag
# ============================================================


def protect_speaker(text):

    speakers = {}

    def replace(match):

        key = f"__SPEAKER_{len(speakers)}__"

        speakers[key] = match.group(0)

        return key

    protected = re.sub(r"<v [^>]+>", replace, text)

    if speakers:

        print("   🔒 Protected speakers:", list(speakers.values()))

    return protected, speakers


def restore_speaker(text, speakers):

    for key, value in speakers.items():

        text = text.replace(key, value)

    return text


# ============================================================
# VTT split
# ============================================================


def split_blocks(content):

    blocks = re.split(r"\n\s*\n", content.strip())

    print("VTT blocks:", len(blocks))

    return blocks


# ============================================================
# Translate single block
# ============================================================


def translate_block(block, index):

    lines = block.splitlines()

    if not lines:

        return block

    if lines[0].strip() == "WEBVTT":

        print(f"Block {index}: WEBVTT")

        return block

    timestamp = None

    text_lines = []

    for line in lines:

        if "-->" in line:

            timestamp = line.strip()

        elif timestamp:

            text_lines.append(line)

    if not timestamp:

        print(f"Block {index}: skip(no timestamp)")

        return block

    original = "\n".join(text_lines).strip()

    if not original:

        print(f"Block {index}: empty")

        return block

    print(f"\nBlock {index}")

    print(" Time:", timestamp)

    print(" Text:", original[:120])

    protected, speakers = protect_speaker(original)

    translated = gemini_translate(protected)

    translated = restore_speaker(translated, speakers)

    print(" Translation:", translated[:120])

    # 双语 VTT
    return timestamp + "\n" + original + "\n" + translated


# ============================================================
# Batch translate blocks
# ============================================================


def translate_blocks_batch(blocks, batch_size=10):
    """
    将多个字幕合并请求 Gemini。

    原版：
        201 blocks
        = 201 API requests

    新版：
        201 blocks
        = 约 20 API requests

    大幅降低 Gemini 免费额度压力。
    """

    results = []

    pending = []

    pending_index = []

    for index, block in enumerate(blocks):

        lines = block.splitlines()

        if not lines:

            results.append(block)

            continue

        if lines[0].strip() == "WEBVTT":

            results.append(block)

            continue

        timestamp = None

        text_lines = []

        for line in lines:

            if "-->" in line:

                timestamp = line.strip()

            elif timestamp:

                text_lines.append(line)

        if not timestamp:

            results.append(block)

            continue

        original = "\n".join(text_lines).strip()

        if not original:

            results.append(block)

            continue

        pending.append(original)

        pending_index.append((index, timestamp, original))

        if len(pending) >= batch_size:

            translated = translate_batch(pending)

            results.extend(build_batch_result(pending_index, translated))

            pending.clear()

            pending_index.clear()

    # remaining

    if pending:

        translated = translate_batch(pending)

        results.extend(build_batch_result(pending_index, translated))

    return results


# ============================================================
# Gemini batch
# ============================================================


def translate_batch(texts):

    global request_count

    if request_count >= MAX_REQUESTS:

        raise RuntimeError("MAX_REQUESTS reached")

    request_count += 1

    print("\n" + "=" * 60)

    print(f"🤖 Gemini batch request #{request_count}")

    print("Blocks:", len(texts))

    joined = ""

    for i, text in enumerate(texts):

        joined += f"\n\n" f"<<<BLOCK {i}>>>\n" f"{text}" f"\n<<<END {i}>>>"

    prompt = f"""
Translate the following English podcast subtitles
into Simplified Chinese.

Rules:

- Return ONLY translated Chinese.
- Keep every BLOCK marker unchanged.
- Do not summarize.
- Do not remove information.

{text}
"""

    for retry in range(5):

        try:

            start = time.time()

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
            )

            result = response.text.strip()

            elapsed = time.time() - start

            print("✅ Gemini batch success")

            print(f"Time: {elapsed:.2f}s")

            return parse_batch_result(result)

        except Exception as e:

            print("❌ Gemini batch error:", e)

            wait = 20 * (retry + 1)

            print(f"⏳ Retry after {wait}s")

            time.sleep(wait)

    raise RuntimeError("Gemini batch failed")


# ============================================================
# Parse Gemini batch output
# ============================================================


def parse_batch_result(text):

    results = []

    pattern = r"<<<BLOCK\s+\d+>>>" r"(.*?)" r"<<<END\s+\d+>>>"

    matches = re.findall(pattern, text, flags=re.S)

    for item in matches:

        results.append(item.strip())

    print("Parsed translations:", len(results))

    return results


# ============================================================
# Build VTT result
# ============================================================


def build_batch_result(metadata, translations):

    output = []

    for i, item in enumerate(metadata):

        index, timestamp, original = item

        if i < len(translations):

            zh = translations[i]

        else:

            print(f"⚠️ Missing translation block {index}")

            zh = ""

        output.append(timestamp + "\n" + original + "\n" + zh)

    return output


# ============================================================
# File hash
# ============================================================


def file_hash(path):

    sha = hashlib.sha256()

    with path.open("rb") as f:

        while True:

            chunk = f.read(8192)

            if not chunk:

                break

            sha.update(chunk)

    return sha.hexdigest()


# ============================================================
# Cache
# ============================================================


def load_cache():

    if not CACHE_FILE.exists():

        print("ℹ️ Cache not found")

        return {}

    try:

        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

        print("✅ Cache loaded:", len(data))

        return data

    except Exception as e:

        print("⚠️ Cache load failed:", e)

        return {}


def save_cache(data):

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    tmp = CACHE_FILE.with_suffix(".tmp")

    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    tmp.replace(CACHE_FILE)

    print("💾 Cache saved:", CACHE_FILE)


# ============================================================
# Translate single VTT file
# ============================================================


def translate_file(source, target):

    print("\n" + "=" * 70)

    print("📄 Translating:", source.name)

    start_time = time.time()

    content = source.read_text(encoding="utf-8")

    blocks = split_blocks(content)

    print("Original blocks:", len(blocks))

    print("🚀 Start Gemini batch translation...")

    translated_blocks = translate_blocks_batch(blocks, batch_size=10)

    print("Translated blocks:", len(translated_blocks))

    result = "\n\n".join(translated_blocks)

    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_suffix(".tmp")

    tmp.write_text(result, encoding="utf-8")

    tmp.replace(target)

    elapsed = time.time() - start_time

    print("✅ Saved:", target)

    print(f"⏱ Time: {elapsed:.2f}s")


# ============================================================
# Main
# ============================================================


def main():

    print("\n" + "=" * 70)

    print("🚀 Translate VTT started")

    print("=" * 70)

    print("Model:", MODEL)

    print("Input:", INPUT_DIR)

    print("Output:", OUTPUT_DIR)

    print("Max files:", MAX_FILES)

    print("Max requests:", MAX_REQUESTS)

    print("\nInitializing Gemini...")

    try:

        client.models.list()

        print("✅ Gemini initialized")

    except Exception as e:

        print("❌ Gemini init failed:", e)

        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cache = load_cache()

    files = sorted(
        INPUT_DIR.glob("*.vtt"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0
    )

    print("\nFound VTT:", len(files))

    translated_count = 0

    skipped_count = 0

    failed_count = 0

    for source in files:

        episode = source.stem

        target = OUTPUT_DIR / source.name

        sha = file_hash(source)

        old = cache.get(episode)

        if old and old.get("hash") == sha and target.exists():

            print("⏭ Skip:", episode)

            skipped_count += 1

            continue

        if translated_count >= MAX_FILES:

            print("MAX_FILES reached")

            break

        try:

            translate_file(source, target)

            cache[episode] = {
                "hash": sha,
                "translated": True,
                "updated": datetime.now(timezone.utc).isoformat(),
            }

            save_cache(cache)

            translated_count += 1

        except Exception as e:

            failed_count += 1

            print("❌ Failed:", episode, e)

    print("\n" + "=" * 70)

    print("🎉 Finished")

    print("Translated:", translated_count)

    print("Skipped:", skipped_count)

    print("Failed:", failed_count)

    print("Gemini requests:", request_count)

    print("=" * 70)


if __name__ == "__main__":

    main()
