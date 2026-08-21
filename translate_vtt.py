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


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))


MAX_FILES = int(os.environ.get("MAX_FILES", "10"))


MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "100"))


REQUEST_INTERVAL = int(os.environ.get("REQUEST_INTERVAL", "15"))


MAX_RETRY = 8


API_KEY = os.environ.get("GEMINI_API_KEY")


if not API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY")


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

print("Request interval:", REQUEST_INTERVAL)


# ============================================================
# Gemini init
# ============================================================


print("Initializing Gemini...")


client = genai.Client(api_key=API_KEY)


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

    print("💾 Cache saved:", CACHE_FILE)


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
# VTT parser
# ============================================================


def split_blocks(content):

    blocks = re.split(r"\n\s*\n", content.strip())

    return [x.strip() for x in blocks if x.strip()]


def parse_block(block):

    lines = block.splitlines()

    if len(lines) < 2:
        return None

    if lines[0].strip() == "WEBVTT":

        return None

    timestamp = None

    text = []

    for line in lines:

        if "-->" in line:

            timestamp = line

        elif timestamp:

            text.append(line)

    if not timestamp:

        return None

    return {"timestamp": timestamp, "text": "\n".join(text).strip()}


# ============================================================
# Gemini Translation
# ============================================================


def build_prompt(items):

    payload = []

    for item in items:

        payload.append({"id": item["id"], "text": item["text"]})

    return f"""
You are a professional podcast subtitle translator.

Translate the following English podcast subtitles into Simplified Chinese.

Return ONLY valid JSON.

Required JSON format:

{{
  "translations": [
    {{
      "id": 1,
      "text": "Chinese translation"
    }}
  ]
}}


Rules:

1. Do not summarize.
2. Do not explain.
3. Translate every item.
4. Keep speaker tags like <v Name>.
5. Keep the same id.
6. Output JSON only.

Input subtitles:

{json.dumps(
    payload,
    ensure_ascii=False,
    indent=2
)}
"""


def extract_json(text):
    """
    Gemini sometimes returns markdown.
    Remove ```json blocks.
    """

    text = text.strip()

    if text.startswith("```"):

        text = re.sub(r"^```(?:json)?", "", text)

        text = re.sub(r"```$", "", text)

    text = text.strip()

    start = text.find("{")

    end = text.rfind("}")

    if start >= 0 and end > start:

        text = text[start : end + 1]

    return json.loads(text)


def gemini_batch_translate(items):

    global request_count

    if not items:

        return {}

    if request_count >= MAX_REQUESTS:

        raise RuntimeError("MAX_REQUESTS reached")

    request_count += 1

    print()
    print("=" * 60)

    print(f"🤖 Gemini request #{request_count}")

    print("Blocks:", len(items))

    prompt = build_prompt(items)

    for retry in range(MAX_RETRY):

        try:

            start = time.time()

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )

            elapsed = time.time() - start

            print("Gemini response time:", f"{elapsed:.2f}s")

            raw = response.text or ""

            print("Response length:", len(raw))

            data = extract_json(raw)

            translations = {}

            for item in data.get("translations", []):

                try:

                    idx = int(item["id"])

                    translations[idx] = item.get("text", "")

                except Exception:

                    continue

            print("Parsed translations:", len(translations))

            return translations

        except Exception as e:

            message = str(e)

            print()

            print("❌ Gemini error:", message)

            # --------------------------------------------
            # Rate limit
            # --------------------------------------------

            if "429" in message or "RESOURCE_EXHAUSTED" in message:

                wait = min(120, 20 * (retry + 1))

                print("⏳ Rate limited.")

                print("Sleep:", wait, "seconds")

                time.sleep(wait)

                continue

            # --------------------------------------------
            # Server unavailable
            # --------------------------------------------

            if "503" in message or "UNAVAILABLE" in message:

                wait = min(120, 15 * (retry + 1))

                print("⏳ Server busy.")

                print("Sleep:", wait, "seconds")

                time.sleep(wait)

                continue

            # Other errors

            print("Retry:", retry + 1, "/", MAX_RETRY)

            time.sleep(10)

    raise RuntimeError("Gemini translation failed")


# ============================================================
# Batch helper
# ============================================================


def translate_blocks(blocks):
    """
    blocks:

    [
      {
        id,
        timestamp,
        text
      }
    ]

    return:

    {
      id: translated_text
    }

    """

    result = {}

    total = len(blocks)

    print()

    print("Total blocks:", total)

    for start in range(0, total, BATCH_SIZE):

        batch = blocks[start : start + BATCH_SIZE]

        print()

        print("-" * 60)

        print("Batch:", start, "-", start + len(batch) - 1)

        translated = gemini_batch_translate(batch)

        result.update(translated)

        missing = [x["id"] for x in batch if x["id"] not in translated]

        if missing:

            print("⚠️ Missing:", missing)

        else:

            print("✅ Batch complete")

        time.sleep(REQUEST_INTERVAL)

    return result


# ============================================================
# VTT Generator
# ============================================================


def create_bilingual_vtt(original_blocks, translations):

    output = []

    output.append("WEBVTT")

    output.append("")

    for block in original_blocks:

        idx = block["id"]

        timestamp = block["timestamp"]

        original = block["text"]

        translated = translations.get(idx)

        output.append(timestamp)

        # --------------------------------------------
        # English
        # --------------------------------------------

        if original:

            output.append(original)

        # --------------------------------------------
        # Chinese
        # --------------------------------------------

        if translated:

            output.append(translated)

        output.append("")

    return "\n".join(output)


# ============================================================
# Parse VTT
# ============================================================


def parse_vtt_blocks(content):

    raw_blocks = split_blocks(content)

    blocks = []

    counter = 1

    for raw in raw_blocks:

        parsed = parse_block(raw)

        if not parsed:

            continue

        blocks.append(
            {"id": counter, "timestamp": parsed["timestamp"], "text": parsed["text"]}
        )

        counter += 1

    return blocks


# ============================================================
# Cache helpers
# ============================================================


def get_episode_cache(cache, episode):

    if episode not in cache:

        cache[episode] = {"hash": "", "updated": "", "blocks": {}}

    return cache[episode]


def save_block_cache(cache, episode, sha, blocks):

    item = get_episode_cache(cache, episode)

    item["hash"] = sha

    item["updated"] = datetime.now(timezone.utc).isoformat()

    if "blocks" not in item:

        item["blocks"] = {}

    for block in blocks:

        item["blocks"][str(block["id"])] = block["translation"]


# ============================================================
# Translate one VTT file
# ============================================================


def translate_file(source, target, cache):

    print()

    print("=" * 70)

    print("📄 Translating:", source.name)

    print("=" * 70)

    sha = file_hash(source)

    episode = source.stem

    episode_cache = get_episode_cache(cache, episode)

    # --------------------------------------------
    # Already finished
    # --------------------------------------------

    if episode_cache.get("hash") == sha and target.exists():

        print("⏭ Skip cached:", episode)

        return False

    content = source.read_text(encoding="utf-8")

    blocks = parse_vtt_blocks(content)

    print("VTT blocks:", len(blocks))

    if not blocks:

        print("⚠️ Empty VTT")

        return False

    translations = {}

    # --------------------------------------------
    # Load existing block cache
    # --------------------------------------------

    old_blocks = episode_cache.get("blocks", {})

    pending = []

    for block in blocks:

        cached = old_blocks.get(str(block["id"]))

        if cached:

            translations[block["id"]] = cached

        else:

            pending.append(block)

    print("Cached blocks:", len(translations))

    print("Need translate:", len(pending))

    # --------------------------------------------
    # Gemini translate
    # --------------------------------------------

    if pending:

        new_translations = translate_blocks(pending)

        translations.update(new_translations)

        # save partial cache immediately

        for idx, text in new_translations.items():

            episode_cache.setdefault("blocks", {})[str(idx)] = text

        save_cache(cache)

    # --------------------------------------------
    # Missing fallback
    # --------------------------------------------

    missing = [b["id"] for b in blocks if b["id"] not in translations]

    if missing:

        print("⚠️ Missing blocks:", missing)

        for idx in missing:

            translations[idx] = ""

    # --------------------------------------------
    # Generate bilingual VTT
    # --------------------------------------------

    vtt = create_bilingual_vtt(blocks, translations)

    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_suffix(".tmp")

    tmp.write_text(vtt, encoding="utf-8")

    tmp.replace(target)

    print("✅ Saved:", target)

    # --------------------------------------------
    # Update cache
    # --------------------------------------------

    episode_cache["hash"] = sha

    episode_cache["updated"] = datetime.now(timezone.utc).isoformat()

    save_cache(cache)

    return True


# ============================================================
# Main
# ============================================================


def main():

    print()

    print("=" * 70)

    print("🚀 Starting translation job")

    print("=" * 70)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cache = load_cache()

    if not INPUT_DIR.exists():

        raise RuntimeError(f"Input directory not found: {INPUT_DIR}")

    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda x: int(x.stem) if x.stem.isdigit() else 999999,
    )

    print()

    print("Found VTT:", len(files))

    translated_files = 0

    skipped_files = 0

    failed_files = 0

    start_time = time.time()

    for index, source in enumerate(files):

        if translated_files >= MAX_FILES:

            print()

            print("Reached MAX_FILES:", MAX_FILES)

            break

        target = OUTPUT_DIR / source.name

        print()

        print(f"[{index+1}/{len(files)}]", source.name)

        try:

            changed = translate_file(source, target, cache)

            if changed:

                translated_files += 1

            else:

                skipped_files += 1

        except Exception as e:

            failed_files += 1

            print()

            print("❌ Failed:", source.name)

            print(repr(e))

        # protect API

        time.sleep(2)

    elapsed = time.time() - start_time

    print()

    print("=" * 70)

    print("🎉 Translation finished")

    print("=" * 70)

    print("Translated files:", translated_files)

    print("Skipped files:", skipped_files)

    print("Failed files:", failed_files)

    print("Gemini requests:", request_count)

    print("Time:", f"{elapsed:.2f}s")

    print("=" * 70)


# ============================================================
# Entry
# ============================================================


if __name__ == "__main__":

    main()
