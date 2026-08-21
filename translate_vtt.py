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


MODEL = "gemini-2.5-flash"


# 每次 Gemini 翻译多少个字幕
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))


MAX_FILES = int(os.environ.get("MAX_FILES", "10"))


MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "300"))


REQUEST_INTERVAL = int(os.environ.get("REQUEST_INTERVAL", "3"))


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


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
# Gemini
# ============================================================


def gemini_translate_batch(cues, batch_no):

    global request_count

    if request_count >= MAX_REQUESTS:

        raise RuntimeError("MAX_REQUESTS reached")

    request_count += 1

    print()

    print("🤖 Gemini request", request_count)

    print("Batch:", batch_no)

    print("Cues:", len(cues))

    content = []

    for i, cue in enumerate(cues, start=1):

        content.append(f"""
[{i}]
{cue['text']}
""")

    prompt = f"""
Translate the following English podcast subtitles into Simplified Chinese.

Rules:

- Return ONLY Chinese translation.
- Keep the same numbering.
- Do not summarize.
- Do not explain.
- Keep names accurate.
- Keep punctuation natural.

Format:

[1]
Chinese translation

[2]
Chinese translation


Text:

{"".join(content)}
"""

    for retry in range(5):

        try:

            start = time.time()

            print("Sending request...")

            response = client.models.generate_content(model=MODEL, contents=prompt)

            elapsed = time.time() - start

            print(f"✅ Gemini OK " f"{elapsed:.1f}s")

            text = response.text.strip()

            return parse_batch_result(text, len(cues))

        except Exception as e:

            print("❌ Gemini error:", e)

            wait = 20 * (retry + 1)

            print("Retry after", wait, "seconds")

            time.sleep(wait)

    raise RuntimeError("Gemini failed")


# ============================================================
# Speaker protection
# ============================================================


def protect_speaker(text):

    speakers = {}

    def replace(match):

        key = f"__SPEAKER_{len(speakers)}__"

        speakers[key] = match.group(0)

        return key

    protected = re.sub(r"<v [^>]+>", replace, text)

    return protected, speakers


def restore_speaker(text, speakers):

    for key, value in speakers.items():

        text = text.replace(key, value)

    return text


# ============================================================
# VTT Parser
# ============================================================


def split_blocks(content):

    return re.split(r"\n\s*\n+", content.strip())


def parse_vtt_blocks(content):
    """
    返回:

    [
        {
            "timestamp":
            "00:00:01 --> 00:00:02",

            "text":
            "<v Ira Glass>Hello",

            "speaker":
            {...}
        }
    ]

    """

    blocks = split_blocks(content)

    result = []

    for index, block in enumerate(blocks):

        lines = block.splitlines()

        if not lines:

            continue

        # WEBVTT header

        if lines[0].strip() == "WEBVTT":

            continue

        timestamp = None

        text_lines = []

        for line in lines:

            if "-->" in line:

                timestamp = line.strip()

            elif timestamp:

                text_lines.append(line)

        if not timestamp:

            continue

        text = "\n".join(text_lines).strip()

        if not text:

            continue

        result.append({"timestamp": timestamp, "text": text})

    return result


# ============================================================
# Parse Gemini batch result
# ============================================================


def parse_batch_result(text, count):
    """
    Gemini返回:

    [1]
    中文

    [2]
    中文


    转换:

    [
      中文,
      中文
    ]

    """

    results = []

    pattern = re.compile(r"\[(\d+)\]\s*(.*?)(?=\n\[\d+\]|$)", re.S)

    matches = pattern.findall(text)

    for _, value in matches:

        results.append(value.strip())

    if len(results) != count:

        print("⚠️ Gemini result count mismatch")

        print("Expected:", count)

        print("Got:", len(results))

        print(text[:500])

        # fallback

        while len(results) < count:

            results.append("")

    return results[:count]


# ============================================================
# Translate batch
# ============================================================


def translate_batch(batch, batch_no):

    print()

    print("=" * 60)

    print(f"📦 Batch {batch_no}")

    print("Cues:", len(batch))

    protected = []

    speaker_maps = []

    for cue in batch:

        p, speakers = protect_speaker(cue["text"])

        protected.append({"text": p})

        speaker_maps.append(speakers)

    translated = gemini_translate_batch(protected, batch_no)

    output = []

    for i, item in enumerate(translated):

        text = restore_speaker(item, speaker_maps[i])

        output.append(text)

        print(f"{i+1}.", text[:80])

    return output


# ============================================================
# Generate bilingual VTT
# ============================================================


def build_bilingual_vtt(cues, translations):

    output = ["WEBVTT", ""]

    for index, cue in enumerate(cues):

        output.append(cue["timestamp"])

        # English

        output.append(cue["text"])

        # Chinese

        if index < len(translations):

            output.append(translations[index])

        output.append("")

    return "\n".join(output)


# ============================================================
# Translate one VTT file
# ============================================================


def translate_file(source, target):

    print()

    print("=" * 70)

    print("📄 Translating:", source.name)

    start_time = time.time()

    content = source.read_text(encoding="utf-8")

    cues = parse_vtt_blocks(content)

    print("VTT cues:", len(cues))

    if not cues:

        print("⚠️ No cues found")

        return False

    all_translations = []

    total_batches = (len(cues) + BATCH_SIZE - 1) // BATCH_SIZE

    print("Total batches:", total_batches)

    for start in range(0, len(cues), BATCH_SIZE):

        batch = cues[start : start + BATCH_SIZE]

        batch_no = start // BATCH_SIZE + 1

        try:

            translations = translate_batch(batch, batch_no)

            all_translations.extend(translations)

        except Exception as e:

            print("❌ Batch failed:", batch_no, e)

            print("Keeping original text")

            all_translations.extend([cue["text"] for cue in batch])

        if batch_no < total_batches:

            print(f"⏳ Sleep {REQUEST_INTERVAL}s")

            time.sleep(REQUEST_INTERVAL)

    if len(all_translations) != len(cues):

        print("⚠️ Translation count mismatch")

        return False

    output = build_bilingual_vtt(cues, all_translations)

    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_suffix(".tmp")

    tmp.write_text(output, encoding="utf-8")

    tmp.replace(target)

    elapsed = time.time() - start_time

    print()

    print("✅ File finished")

    print("Output:", target)

    print(f"Time: {elapsed:.1f}s")

    print("Requests:", request_count)

    return True


# ============================================================
# Main
# ============================================================


def main():

    print()

    print("=" * 70)

    print("🚀 Translate VTT started")

    print("=" * 70)

    print("Model:", MODEL)

    print("Input:", INPUT_DIR)

    print("Output:", OUTPUT_DIR)

    print("Batch size:", BATCH_SIZE)

    print("Max files:", MAX_FILES)

    print("Max requests:", MAX_REQUESTS)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cache = load_cache()

    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda x: int(x.stem) if x.stem.isdigit() else 999999,
    )

    print()

    print("Found VTT:", len(files))

    translated_files = 0

    for source in files:

        if translated_files >= MAX_FILES:

            print("🛑 MAX_FILES reached")

            break

        episode = source.stem

        target = OUTPUT_DIR / source.name

        sha = file_hash(source)

        old = cache.get(episode)

        if old and old.get("hash") == sha and target.exists():

            print("⏭ Skip:", episode)

            continue

        print()

        print("-" * 70)

        print("Episode:", episode)

        success = translate_file(source, target)

        if success:

            cache[episode] = {
                "hash": sha,
                "translated": True,
                "updated": datetime.now(timezone.utc).isoformat(),
            }

            save_cache(cache)

            translated_files += 1

    print()

    print("=" * 70)

    print("🎉 Finished")

    print("Translated files:", translated_files)

    print("Total Gemini requests:", request_count)

    print("=" * 70)


if __name__ == "__main__":

    main()
