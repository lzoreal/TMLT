#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Translate English VTT transcript into bilingual English + Chinese VTT

Features:
- Gemini batch translation
- Episode level cache only
- No translation memory
- No speaker translation
- GitHub Actions friendly logs
"""

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
# Configuration
# ============================================================


INPUT_DIR = Path("docs/transcripts")


OUTPUT_DIR = Path("docs/transcripts/zh")


CACHE_FILE = Path("docs/translations.json")


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


MAX_FILES = int(os.environ.get("MAX_FILES", "10"))


MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "300"))


# Gemini batch size

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))


# request retry

MAX_RETRY = 5


# statistics

request_count = 0

success_blocks = 0

failed_blocks = 0


start_time = time.time()


# ============================================================
# Logging
# ============================================================


def log(msg=""):

    print(msg, flush=True)


def section(title):

    log()

    log("=" * 70)

    log(title)

    log("=" * 70)


# ============================================================
# Gemini Initialize
# ============================================================


def init_gemini():

    section("Initializing Gemini...")

    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:

        raise RuntimeError("GEMINI_API_KEY missing")

    client = genai.Client(api_key=api_key)

    log("✅ Gemini initialized")

    log(f"Model: {MODEL}")

    return client


# ============================================================
# Cache
# ============================================================


def load_cache():

    if not CACHE_FILE.exists():

        log("ℹ️ Cache not found")

        return {}

    try:

        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

        log(f"💾 Cache loaded: {len(data)} episodes")

        return data

    except Exception as e:

        log(f"⚠️ Cache read failed: {e}")

        return {}


def save_cache(cache):

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log(f"💾 Cache saved: {CACHE_FILE}")


# ============================================================
# Hash
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
# Text helpers
# ============================================================


def clean_text(text):

    if not text:

        return ""

    return text.replace("\r\n", "\n").strip()


def extract_speaker(text):
    """
    Keep speaker English.

    Example:

    <v Ira Glass>Hello

    return:

    speaker:
        Ira Glass

    content:
        Hello
    """

    match = re.match(r"<v\s+([^>]+)>(.*)", text, re.S)

    if not match:

        return None, text

    speaker = match.group(1).strip()

    content = match.group(2).strip()

    return speaker, content


# ============================================================
# Prompt builder
# ============================================================


def build_prompt(blocks):

    items = []

    for index, block in blocks:

        speaker, text = extract_speaker(block["text"])

        items.append({"id": index, "speaker": speaker, "text": text})

    payload = json.dumps(items, ensure_ascii=False, indent=2)

    prompt = f"""

You are translating an English podcast transcript.

Translate ONLY the "text" field into Simplified Chinese.

Rules:

1. Keep speaker names unchanged.
2. Do not translate names.
3. Do not summarize.
4. Keep punctuation.
5. Return ONLY valid JSON.
6. Keep the same id.

Output format:

{{
 "translations":[
   {{
    "id":1,
    "text":"Chinese translation"
   }}
 ]
}}

Input:

{payload}

"""

    return prompt


# End of Part 1
# ============================================================
# Gemini Batch Translation
# ============================================================


def parse_retry_seconds(error_text):
    """
    Extract retry seconds from Gemini error.

    Example:

    Please retry in 13.255561766s

    return:

    14
    """

    match = re.search(r"retry in\s+([0-9.]+)s", error_text, re.I)

    if match:

        return int(float(match.group(1))) + 1

    return None


def call_gemini(client, prompt):
    """
    Gemini request wrapper.

    Handles:

    429 RESOURCE_EXHAUSTED
    503 UNAVAILABLE
    timeout
    """

    global request_count

    if request_count >= MAX_REQUESTS:

        raise RuntimeError("MAX_REQUESTS reached")

    for retry in range(MAX_RETRY):

        request_count += 1

        log()

        log(f"🤖 Gemini request #{request_count}")

        try:

            request_start = time.time()

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2, response_mime_type="application/json"
                ),
            )

            elapsed = time.time() - request_start

            log(f"✅ Gemini success " f"{elapsed:.2f}s")

            if not response.text:

                raise RuntimeError("Empty Gemini response")

            return response.text

        except Exception as e:

            error = str(e)

            log()

            log("❌ Gemini error:")

            log(error)

            retry_seconds = parse_retry_seconds(error)

            if retry_seconds:

                wait = min(retry_seconds, 120)

            else:

                wait = min(15 * (retry + 1), 120)

            log(f"⏳ Retry after {wait}s")

            time.sleep(wait)

    raise RuntimeError("Gemini failed after retries")


def parse_translation_json(raw, expected_ids):
    """
    Parse Gemini JSON response.

    Expected:

    {
      "translations":[
        {
          "id":1,
          "text":"xxx"
        }
      ]
    }

    Return:

    {
      1:"xxx"
    }

    """

    result = {}

    try:

        data = json.loads(raw)

        translations = data.get("translations", [])

        for item in translations:

            item_id = item.get("id")

            text = item.get("text")

            if item_id is not None and text:

                result[int(item_id)] = text.strip()

    except Exception as e:

        log()

        log("⚠️ JSON parse failed")

        log(raw[:500])

        log(str(e))

    log(f"Parsed translations: {len(result)}")

    missing = set(expected_ids) - set(result.keys())

    for mid in sorted(missing):

        log(f"⚠️ Missing translation id: {mid}")

    return result


def translate_batch(client, blocks):
    """
    Translate one batch.

    blocks:

    [
       (
        id,
        {
          timestamp:"",
          text:""
        }
       )
    ]

    """

    global success_blocks

    global failed_blocks

    batch_items = []

    for index, block in blocks:

        batch_items.append((index, block))

    prompt = build_prompt(batch_items)

    expected_ids = [x[0] for x in batch_items]

    raw = call_gemini(client, prompt)

    translations = parse_translation_json(raw, expected_ids)

    output = {}

    for block_id in expected_ids:

        if block_id in translations:

            output[block_id] = translations[block_id]

            success_blocks += 1

        else:

            failed_blocks += 1

    return output


# ============================================================
# Batch Runner
# ============================================================


def translate_all_batches(client, blocks):
    """
    Split VTT blocks into batches.

    """

    translated = {}

    total = len(blocks)

    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    for start in range(0, total, BATCH_SIZE):

        batch_no = (start // BATCH_SIZE) + 1

        end = min(start + BATCH_SIZE, total)

        section(f"Batch {batch_no}/{total_batches}")

        log(f"Blocks: {start} - {end-1}")

        batch = []

        for i in range(start, end):

            batch.append((i, blocks[i]))

        result = translate_batch(client, batch)

        translated.update(result)

        log(f"Batch translated: {len(result)}/{len(batch)}")

    return translated


# End of Part 2
# ============================================================
# VTT Parser
# ============================================================


def split_vtt_blocks(content):
    """
    Split VTT into blocks.

    Keep:

    WEBVTT

    timestamps

    cues

    """

    return re.split(r"\n\s*\n", content.strip())


def parse_vtt_blocks(content):

    raw_blocks = split_vtt_blocks(content)

    blocks = []

    for raw in raw_blocks:

        lines = raw.splitlines()

        if not lines:

            continue

        # header

        if lines[0].strip() == "WEBVTT":

            blocks.append({"type": "header", "raw": raw})

            continue

        timestamp = None

        text_lines = []

        for line in lines:

            if "-->" in line:

                timestamp = line.strip()

            elif timestamp:

                text_lines.append(line)

        if not timestamp:

            blocks.append({"type": "raw", "raw": raw})

            continue

        text = "\n".join(text_lines).strip()

        blocks.append({"type": "cue", "timestamp": timestamp, "text": text})

    return blocks


# ============================================================
# Build bilingual VTT
# ============================================================


def build_bilingual_vtt(blocks, translations):

    output = []

    for index, block in enumerate(blocks):

        if block["type"] == "header":

            output.append(block["raw"])

            continue

        if block["type"] == "raw":

            output.append(block["raw"])

            continue

        timestamp = block["timestamp"]

        original = block["text"]

        translated = translations.get(index)

        if not translated:

            raise RuntimeError(f"Missing translation block {index}")

        speaker, _ = extract_speaker(original)

        #
        # Keep speaker name
        #
        # Original:
        #
        # <v Ira Glass>Hello
        #
        #
        # Chinese:
        #
        # Ira Glass：你好
        #

        if speaker:

            chinese = speaker + "：" + translated

        else:

            chinese = translated

        output.append(timestamp + "\n" + original + "\n" + chinese)

    return "\n\n".join(output)


# ============================================================
# Translate one VTT file
# ============================================================


def translate_file(client, source, target):

    section(f"📄 Translating {source.name}")

    content = source.read_text(encoding="utf-8")

    blocks = parse_vtt_blocks(content)

    log(f"Total blocks: {len(blocks)}")

    cue_blocks = []

    for index, block in enumerate(blocks):

        if block["type"] == "cue":

            cue_blocks.append((index, block))

    log(f"Cue blocks: {len(cue_blocks)}")

    if not cue_blocks:

        raise RuntimeError("No subtitle blocks found")

    translations = translate_all_batches(client, cue_blocks)

    log()

    log(f"Translated blocks: " f"{len(translations)}/{len(cue_blocks)}")

    #
    # Safety check
    #

    if len(translations) != len(cue_blocks):

        raise RuntimeError("Translation incomplete, abort saving")

    result = build_bilingual_vtt(blocks, translations)

    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_suffix(".tmp")

    tmp.write_text(result, encoding="utf-8")

    tmp.replace(target)

    log(f"✅ Saved: {target}")


# ============================================================
# Episode cache update
# ============================================================


def update_episode_cache(cache, episode, sha, blocks):

    cache[episode] = {
        "hash": sha,
        "translated": True,
        "blocks": blocks,
        "updated": datetime.now(timezone.utc).isoformat(),
    }


# End of Part 3
# ============================================================
# GitHub Actions helpers
# ============================================================


def github_notice(message):
    """
    GitHub Actions annotation
    """

    print(f"::notice title=Translate VTT::{message}", flush=True)


# ============================================================
# Read environment config
# ============================================================


def get_env_int(name, default):

    try:

        return int(os.environ.get(name, default))

    except Exception:

        return default


# ============================================================
# Main
# ============================================================


def main():

    global start_time

    section("🚀 Translate VTT started")

    log(f"Input: {INPUT_DIR}")

    log(f"Output: {OUTPUT_DIR}")

    log(f"Max files: {MAX_FILES}")

    log(f"Max requests: {MAX_REQUESTS}")

    log(f"Batch size: {BATCH_SIZE}")

    client = init_gemini()

    cache = load_cache()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda x: int(x.stem) if x.stem.isdigit() else 999999,
    )

    log()

    log(f"Found VTT: {len(files)}")

    translated_files = 0

    skipped_files = 0

    failed_files = 0

    for source in files:

        if translated_files >= MAX_FILES:

            log("Reached MAX_FILES")

            break

        episode = source.stem

        target = OUTPUT_DIR / source.name

        sha = file_hash(source)

        old = cache.get(episode)

        if old and old.get("hash") == sha and old.get("translated") and target.exists():

            log()

            log(f"⏭ Skip {episode}: cached")

            skipped_files += 1

            continue

        log()

        log("=" * 60)

        log(f"Processing episode {episode}")

        log("=" * 60)

        try:

            translate_file(client, source, target)

            update_episode_cache(cache, episode, sha, count_blocks(source))

            save_cache(cache)

            translated_files += 1

            github_notice(f"Episode {episode} translated")

        except Exception as e:

            failed_files += 1

            log()

            log(f"❌ Episode {episode} failed")

            log(str(e))

            github_notice(f"Episode {episode} failed")

    elapsed = time.time() - start_time

    section("SUMMARY")

    log(f"Translated files: {translated_files}")

    log(f"Skipped files: {skipped_files}")

    log(f"Failed files: {failed_files}")

    log(f"Gemini requests: {request_count}")

    log(f"Translated blocks: {success_blocks}")

    log(f"Failed blocks: {failed_blocks}")

    log(f"Time: {elapsed:.2f}s")

    log()

    log("✅ Finished")


# ============================================================
# Count blocks helper
# ============================================================


def count_blocks(source):

    try:

        content = source.read_text(encoding="utf-8")

        blocks = parse_vtt_blocks(content)

        return len([b for b in blocks if b["type"] == "cue"])

    except Exception:

        return 0


# ============================================================
# Entry
# ============================================================


if __name__ == "__main__":

    main()
