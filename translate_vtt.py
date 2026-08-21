#!/usr/bin/env python3

import os
import re
import json
import time
import hashlib
import traceback
import functools

from pathlib import Path
from datetime import datetime, timezone

from google import genai


# ============================================================
# Force realtime logs for GitHub Actions
# ============================================================

print = functools.partial(
    print,
    flush=True
)



# ============================================================
# Config
# ============================================================


INPUT_DIR = Path(
    "docs/transcripts"
)


OUTPUT_DIR = Path(
    "docs/transcripts/zh"
)


CACHE_FILE = Path(
    "docs/translations.json"
)



MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)



MAX_FILES = int(
    os.environ.get(
        "MAX_FILES",
        "10"
    )
)



MAX_REQUESTS = int(
    os.environ.get(
        "MAX_REQUESTS",
        "300"
    )
)



REQUEST_INTERVAL = int(
    os.environ.get(
        "REQUEST_INTERVAL",
        "5"
    )
)



RETRY_COUNT = 5



API_KEY = os.environ.get(
    "GEMINI_API_KEY"
)



if not API_KEY:

    raise RuntimeError(
        "❌ GEMINI_API_KEY missing"
    )



# ============================================================
# Runtime status
# ============================================================


START_TIME = time.time()


request_count = 0


success_files = []


failed_files = []



# ============================================================
# Startup
# ============================================================


print("=" * 70)

print(
    "🚀 Translate VTT started"
)

print("=" * 70)


print(
    "Model:",
    MODEL
)


print(
    "Input:",
    INPUT_DIR
)


print(
    "Output:",
    OUTPUT_DIR
)


print(
    "Max files:",
    MAX_FILES
)


print(
    "Max requests:",
    MAX_REQUESTS
)



client = genai.Client(
    api_key=API_KEY
)



print(
    "✅ Gemini initialized"
)



# ============================================================
# Cache
# ============================================================


def load_cache():


    if not CACHE_FILE.exists():

        print(
            "ℹ️ Cache not found"
        )

        return {}



    try:

        data = json.loads(

            CACHE_FILE.read_text(
                encoding="utf-8"
            )

        )


        print(
            "Loaded cache:",
            len(data)
        )


        return data



    except Exception as e:


        print(
            "⚠️ Cache load failed:",
            e
        )


        return {}





def save_cache(data):


    CACHE_FILE.parent.mkdir(

        parents=True,

        exist_ok=True

    )


    CACHE_FILE.write_text(

        json.dumps(

            data,

            indent=2,

            ensure_ascii=False

        ),

        encoding="utf-8"

    )


    print(
        "💾 Cache saved:",
        len(data)
    )



# ============================================================
# File hash
# ============================================================


def file_hash(path):


    sha = hashlib.sha256()



    with path.open(
        "rb"
    ) as f:


        while True:


            chunk = f.read(
                8192
            )


            if not chunk:

                break



            sha.update(
                chunk
            )



    return sha.hexdigest()



# ============================================================
# Gemini translate
# ============================================================


def gemini_translate(text):


    global request_count



    if request_count >= MAX_REQUESTS:


        raise RuntimeError(
            "MAX_REQUESTS reached"
        )



    request_count += 1



    print()

    print("=" * 60)

    print(
        "🤖 Gemini request",
        request_count
    )


    print(
        "Input chars:",
        len(text)
    )


    print(
        "Input preview:"
    )


    print(
        text[:200]
    )


    print("=" * 60)



    prompt = f"""

Translate this English podcast transcript into Simplified Chinese.

Rules:

- Only output Chinese translation.
- Do not summarize.
- Do not explain.
- Keep names accurate.
- Preserve punctuation.
- Keep speaker tags unchanged.

Text:

{text}

"""



    for retry in range(RETRY_COUNT):


        try:


            print(
                "Sending Gemini request..."
            )



            response = (
                client.models.generate_content(

                    model=MODEL,

                    contents=prompt

                )
            )



            print(
                "Response received"
            )



            result = (

                response.text

                .strip()

            )



            print(
                "Output chars:",
                len(result)
            )


            print(
                "Output preview:"
            )


            print(
                result[:200]
            )



            time.sleep(
                REQUEST_INTERVAL
            )



            return result



        except Exception as e:


            print()

            print(
                "❌ Gemini error"
            )


            print(
                type(e).__name__
            )


            print(
                str(e)
            )



            wait = (
                30 *
                (retry + 1)
            )



            print(
                "Retry:",
                retry + 1,
                "/",
                RETRY_COUNT
            )


            print(
                "Sleep:",
                wait,
                "seconds"
            )



            time.sleep(
                wait
            )



    raise RuntimeError(
        "Gemini failed after retries"
    )
# ============================================================
# Speaker protection
# ============================================================


def protect_speaker(text):


    speakers = {}


    def repl(match):


        key = (
            f"__SPEAKER_{len(speakers)}__"
        )


        speakers[key] = (
            match.group(0)
        )


        return key



    protected = re.sub(

        r"<v [^>]+>",

        repl,

        text

    )


    return protected, speakers




def restore_speaker(
    text,
    speakers
):


    for key, value in speakers.items():

        text = text.replace(
            key,
            value
        )


    return text



# ============================================================
# VTT parser
# ============================================================


def split_blocks(content):


    blocks = re.split(

        r"\n\s*\n+",

        content.strip()

    )



    print()

    print(
        "VTT blocks:",
        len(blocks)
    )



    for i, block in enumerate(blocks[:5]):


        print(

            "Block",

            i,

            ":",

            block[:120]
            .replace(
                "\n",
                " | "
            )

        )



    return blocks




# ============================================================
# Translate block
# ============================================================


def translate_block(
    block,
    index
):


    print()

    print(
        f"Checking block {index}"
    )


    print(
        "Preview:",
        block[:120]
        .replace(
            "\n",
            " | "
        )
    )



    lines = block.splitlines()



    print(
        "Lines:",
        len(lines)
    )



    # WEBVTT header

    if (
        len(lines)
        and
        lines[0].strip()
        ==
        "WEBVTT"
    ):


        print(
            "Skip WEBVTT header"
        )


        return block




    # Too short

    if len(lines) < 2:


        print(
            "Skip short block"
        )


        return block




    timestamp = None


    text_lines = []



    for line in lines:


        if "-->" in line:


            timestamp = line



        elif timestamp:


            text_lines.append(
                line
            )




    if not timestamp:


        print(
            "Skip no timestamp"
        )


        return block




    original = "\n".join(
        text_lines
    )



    if not original.strip():


        print(
            "Skip empty text"
        )


        return block





    print()

    print(
        "=" * 40
    )


    print(
        "Block",
        index,
        "translation"
    )


    print(
        "Original:"
    )


    print(
        original[:300]
    )



    protected, speakers = (
        protect_speaker(
            original
        )
    )



    translated = (
        gemini_translate(
            protected
        )
    )



    translated = (
        restore_speaker(
            translated,
            speakers
        )
    )



    print(
        "Translated:"
    )


    print(
        translated[:300]
    )



    print(
        "=" * 40
    )



    # 双语 VTT
    #
    # 原文
    # 中文
    #

    return (

        timestamp

        +

        "\n"

        +

        original

        +

        "\n"

        +

        translated

    )



# ============================================================
# Translate one file
# ============================================================


def translate_file(
    source,
    target
):


    print()

    print("=" * 70)


    print(
        "📄 Translating:",
        source.name
    )


    print(
        "Size:",
        source.stat().st_size,
        "bytes"
    )


    print(
        "Target:",
        target
    )



    file_start = time.time()



    content = source.read_text(

        encoding="utf-8"

    )



    blocks = split_blocks(
        content
    )



    result = []



    translated_blocks = 0



    for index, block in enumerate(blocks):


        result.append(

            translate_block(

                block,

                index

            )

        )



        if "-->" in block:


            translated_blocks += 1




    target.parent.mkdir(

        parents=True,

        exist_ok=True

    )



    tmp = target.with_suffix(
        ".tmp"
    )



    tmp.write_text(

        "\n\n".join(result),

        encoding="utf-8"

    )



    tmp.replace(
        target
    )



    cost = (
        time.time()
        -
        file_start
    )



    print()

    print(
        "✅ File completed:",
        source.name
    )


    print(
        "Translated blocks:",
        translated_blocks
    )


    print(
        "Time:",
        round(cost,2),
        "seconds"
    )


    print(
        "Output size:",
        target.stat().st_size,
        "bytes"
    )
# ============================================================
# Translate block
# ============================================================

def translate_block(block, index, total):

    lines = block.splitlines()

    if len(lines) < 3:
        return block


    # WEBVTT header
    if lines[0].strip() == "WEBVTT":
        return block


    timestamp = None
    text_lines = []


    for line in lines:

        if "-->" in line:
            timestamp = line

        elif timestamp:
            text_lines.append(line)



    if not timestamp:

        print(
            f"   ⚠️ Block {index}/{total}: "
            "No timestamp"
        )

        return block



    original = "\n".join(
        text_lines
    ).strip()



    if not original:

        print(
            f"   ⚠️ Block {index}/{total}: "
            "Empty text"
        )

        return block



    print(
        f"\n   📝 Block {index}/{total}"
    )

    print(
        f"   ⏱ {timestamp}"
    )

    print(
        f"   EN: {original[:120]}"
    )


    protected, speakers = (
        protect_speaker(
            original
        )
    )


    if speakers:

        print(
            "   👤 Protected speakers:",
            list(
                speakers.values()
            )
        )



    translated = gemini_translate(
        protected,
        index
    )



    translated = restore_speaker(
        translated,
        speakers
    )


    print(
        f"   CN: {translated[:120]}"
    )


    return (
        timestamp
        + "\n"
        + original
        + "\n"
        + translated
    )



# ============================================================
# Translate File
# ============================================================


def translate_file(
    source,
    target
):

    print(
        "\n"
        + "=" * 70
    )

    print(
        "📄 Translating:",
        source.name
    )



    content = source.read_text(
        encoding="utf-8"
    )


    blocks = split_blocks(
        content
    )


    print(
        "VTT blocks:",
        len(blocks)
    )


    result = []


    start_time = time.time()



    for index, block in enumerate(
        blocks,
        start=1
    ):


        try:

            result.append(
                translate_block(
                    block,
                    index,
                    len(blocks)
                )
            )


        except Exception as e:

            print(
                f"❌ Block {index} failed:",
                e
            )

            print(
                "   Keep original"
            )


            result.append(
                block
            )



        # 每个字幕间隔
        if index != len(blocks):

            print(
                f"   ⏳ Sleep {REQUEST_INTERVAL}s"
            )

            time.sleep(
                REQUEST_INTERVAL
            )



        if index % 10 == 0:

            elapsed = (
                time.time()
                -
                start_time
            )

            print(
                "\n"
                "📊 Progress:"
            )

            print(
                f"   {index}/{len(blocks)}"
            )

            print(
                f"   Time: {elapsed:.1f}s"
            )

            print(
                f"   Requests: {request_count}"
            )



    target.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    tmp = target.with_suffix(
        ".tmp"
    )


    tmp.write_text(
        "\n\n".join(result),
        encoding="utf-8"
    )


    tmp.replace(
        target
    )


    elapsed = (
        time.time()
        -
        start_time
    )


    print(
        "\n"
        "✅ Translation finished"
    )


    print(
        "Output:",
        target
    )

    print(
        f"Time: {elapsed:.1f}s"
    )

    print(
        "Requests:",
        request_count
    )



# ============================================================
# Main
# ============================================================


def main():


    print(
        "\n"
        + "=" * 70
    )

    print(
        "🚀 Translate VTT started"
    )

    print(
        "=" * 70
    )



    print(
        "Model:",
        MODEL
    )

    print(
        "Input:",
        INPUT_DIR
    )

    print(
        "Output:",
        OUTPUT_DIR
    )

    print(
        "Max files:",
        MAX_FILES
    )

    print(
        "Max requests:",
        MAX_REQUESTS
    )


    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


    cache = load_cache()


    if cache:

        print(
            "✅ Cache loaded:",
            len(cache)
        )

    else:

        print(
            "ℹ️ Cache empty"
        )



    files = sorted(
        INPUT_DIR.glob("*.vtt"),
        key=lambda x:
        int(x.stem)
        if x.stem.isdigit()
        else 999999
    )



    print(
        "Found VTT:",
        len(files)
    )



    translated_count = 0



    for source in files:


        episode = source.stem


        target = (
            OUTPUT_DIR
            /
            source.name
        )


        sha = file_hash(
            source
        )


        old = cache.get(
            episode
        )



        if (

            old

            and old.get("hash")
            == sha

            and target.exists()

        ):


            print(
                "⏭ Skip:",
                episode
            )

            continue



        if translated_count >= MAX_FILES:


            print(
                "🛑 MAX_FILES reached"
            )

            break



        translate_file(
            source,
            target
        )



        cache[episode] = {

            "hash": sha,

            "translated": True,

            "updated":
            datetime.now(
                timezone.utc
            )
            .isoformat()

        }


        save_cache(
            cache
        )


        translated_count += 1



    print(
        "\n"
        + "=" * 70
    )

    print(
        "🎉 Finished"
    )

    print(
        "Translated:",
        translated_count
    )

    print(
        "Total requests:",
        request_count
    )

    print(
        "=" * 70
    )



if __name__ == "__main__":

    main()