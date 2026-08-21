#!/usr/bin/env python3

import os
import re
import json
import time
import hashlib
import traceback

from pathlib import Path
from datetime import datetime, timezone

from google import genai

# ============================================================
# Config
# ============================================================

INPUT_DIR = Path("docs/transcripts")

OUTPUT_DIR = Path("docs/transcripts/zh")

CACHE_FILE = Path("docs/translations.json")


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


MAX_FILES = int(os.environ.get("MAX_FILES", "10"))


MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "300"))


BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))


RETRY_COUNT = 5


RETRY_BASE = 20


# ============================================================
# Logging
# ============================================================


def log(msg=""):

    print(msg, flush=True)


def separator():

    log("=" * 70)


# ============================================================
# Gemini
# ============================================================


log("Initializing Gemini...")


try:

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    log("✅ Gemini initialized")


except Exception as e:

    log("❌ Gemini initialization failed")

    raise e


request_count = 0


# ============================================================
# Cache
# only episode status
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

        log(f"⚠️ Cache load failed: {e}")

        return {}


def save_cache(cache):

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log("💾 Cache saved")


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
# Gemini Batch Translation
# ============================================================


def build_prompt(blocks):

    content = []

    for idx, text in blocks:

        content.append(f"{idx}|||{text}")

    joined = "\n\n".join(content)

    prompt = f"""
You are translating podcast subtitles.

Translate English subtitles into Simplified Chinese.

Rules:

1. Keep speaker names unchanged.
2. Do NOT translate names.
3. Preserve meaning.
4. Do NOT summarize.
5. Keep every block.
6. Output ONLY:

0|||Chinese translation
1|||Chinese translation

No markdown.
No explanation.


Subtitle blocks:

{joined}

"""

    return prompt


def parse_translation_response(text):

    result = {}

    if not text:

        return result

    lines = text.splitlines()

    for line in lines:

        if "|||" not in line:

            continue

        try:

            idx, value = line.split("|||", 1)

            idx = int(idx.strip())

            result[idx] = value.strip()

        except Exception:

            continue

    return result


def gemini_batch_translate(blocks):

    global request_count

    if request_count >= MAX_REQUESTS:

        raise RuntimeError("MAX_REQUESTS reached")

    prompt = build_prompt(blocks)

    for attempt in range(1, RETRY_COUNT + 1):

        request_count += 1

        start = time.time()

        log("")

        log(f"🤖 Gemini request #{request_count}")

        log(f"Blocks: {len(blocks)}")

        try:

            response = client.models.generate_content(model=MODEL, contents=prompt)

            elapsed = time.time() - start

            raw = response.text or ""

            parsed = parse_translation_response(raw)

            log(f"✅ Gemini success {elapsed:.2f}s")

            log(f"Parsed: {len(parsed)}")

            if len(parsed) == 0:

                log("⚠️ Empty parse result")

                log(raw[:500])

            return parsed

        except Exception as e:

            log(f"❌ Gemini error: {e}")

            if attempt >= RETRY_COUNT:

                raise

            wait = RETRY_BASE * attempt

            log(f"⏳ Retry after {wait}s")

            time.sleep(wait)

    return {}


# ============================================================
# VTT Parser
# ============================================================


def split_vtt_blocks(content):

    return re.split(r"\n\s*\n", content.strip())


def parse_cue(block):

    lines = block.splitlines()

    if len(lines) < 2:

        return None

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

    return {"timestamp": timestamp, "text": text}


def clean_speaker(text):
    """
    Keep speaker name in English.

    <v Ira Glass>Hello
    =>
    Ira Glass：Hello
    """

    return re.sub(r"<v\s+([^>]+)>", r"\1：", text)


# ============================================================
# Translate Episode
# ============================================================


def translate_episode(source, target):

    separator()

    log(f"📄 Translating {source.name}")

    separator()

    content = source.read_text(encoding="utf-8")

    blocks = split_vtt_blocks(content)

    log(f"Total blocks: {len(blocks)}")

    cues = []

    for block in blocks:

        cue = parse_cue(block)

        if cue:

            cues.append(cue)

    log(f"Cue blocks: {len(cues)}")

    translated = {}

    # ----------------------------
    # Batch translate
    # ----------------------------

    for start in range(0, len(cues), BATCH_SIZE):

        end = min(start + BATCH_SIZE, len(cues))

        batch = []

        for i in range(start, end):

            text = clean_speaker(cues[i]["text"])

            batch.append((i, text))

        separator()

        log(f"Batch {start//BATCH_SIZE+1}")

        log(f"Blocks: {start} - {end-1}")

        result = gemini_batch_translate(batch)

        for idx, value in result.items():

            translated[idx] = value

        time.sleep(2)

    # ----------------------------
    # Check result
    # ----------------------------

    missing = []

    for i in range(len(cues)):

        if i not in translated:

            missing.append(i)

    if missing:

        log(f"⚠️ Missing blocks: {len(missing)}")

        for i in missing[:10]:

            log(f"Missing block {i}")

        raise RuntimeError("Translation incomplete")

    # ----------------------------
    # Generate bilingual VTT
    # ----------------------------

    output = []

    output.append("WEBVTT")

    for i, cue in enumerate(cues):

        output.append(cue["timestamp"])

        original = clean_speaker(cue["text"])

        chinese = translated[i]

        output.append(original)

        output.append(chinese)

        output.append("")

    target.parent.mkdir(parents=True, exist_ok=True)

    temp = target.with_suffix(".tmp")

    temp.write_text("\n".join(output), encoding="utf-8")

    temp.replace(target)

    log(f"✅ Saved: {target}")


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

    cache = load_cache()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda x: int(x.stem) if x.stem.isdigit() else 999999,
    )

    log(f"Found VTT: {len(files)}")

    processed = 0

    for source in files:

        episode = source.stem

        separator()

        log(f"Processing episode {episode}")

        sha = file_hash(source)

        old = cache.get(episode)

        target = OUTPUT_DIR / source.name

        if old and old.get("hash") == sha and old.get("translated") and target.exists():

            log("⏭ Already translated")

            continue

        if processed >= MAX_FILES:

            log("MAX_FILES reached")

            break

        try:

            translate_episode(source, target)

            cache[episode] = {
                "hash": sha,
                "translated": True,
                "updated": datetime.now(timezone.utc).isoformat(),
            }

            save_cache(cache)

            processed += 1

        except Exception as e:

            log(f"❌ Episode {episode} failed")

            log(str(e))

            traceback.print_exc()

            continue

    separator()

    log("Finished")

    log(f"Translated episodes: {processed}")

    separator()


if __name__ == "__main__":

    main()
