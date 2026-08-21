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



# ============================================================
# Global
# ============================================================


start_time = time.time()


request_count = 0


success_files = []


failed_files = []



# ============================================================
# Gemini
# ============================================================


if not API_KEY:

    raise RuntimeError(
        "❌ GEMINI_API_KEY missing"
    )


print("=" * 70)

print("🚀 Translate VTT started")

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
# Hash
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

    print(
        "🤖 Gemini request",
        request_count
    )


    print(
        "Characters:",
        len(text)
    )


    prompt = f"""
Translate this English podcast transcript into Simplified Chinese.

Rules:

- Output ONLY Chinese translation.
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


            response = (
                client.models.generate_content(

                    model=MODEL,

                    contents=prompt

                )
            )


            result = (
                response.text
                .strip()
            )


            print(
                "✅ Gemini success"
            )


            print(
                "Output chars:",
                len(result)
            )


            return result



        except Exception as e:


            print()

            print(
                "❌ Gemini error"
            )

            print(
                type(e).__name__,
                e
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


    for key,value in speakers.items():

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


    print(
        "VTT blocks:",
        len(blocks)
    )


    return blocks



# ============================================================
# Translate block
# ============================================================


def translate_block(
    block,
    index
):


    lines = block.splitlines()


    if len(lines) < 3:

        return block



    if lines[0].strip() == "WEBVTT":

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

        return block



    original = "\n".join(
        text_lines
    )


    if not original.strip():

        return block



    print()

    print(
        f"Block {index}"
    )


    print(
        "Original:",
        original[:120]
    )



    protected, speakers = (
        protect_speaker(
            original
        )
    )



    translated = gemini_translate(
        protected
    )



    translated = restore_speaker(
        translated,
        speakers
    )



    print(
        "Chinese:",
        translated[:120]
    )


    return (

        timestamp
        + "\n"
        + original
        + "\n"
        + translated

    )



# ============================================================
# File translate
# ============================================================


def translate_file(
    source,
    target
):


    print()

    print("=" * 70)

    print(
        "📄 Translating",
        source.name
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



    for i, block in enumerate(blocks):


        result.append(

            translate_block(
                block,
                i
            )

        )


        if "-->" in block:

            translated_blocks += 1



        time.sleep(
            REQUEST_INTERVAL
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



    cost = (
        time.time()
        -
        file_start
    )


    print()

    print(
        "✅ File finished"
    )

    print(
        "Blocks translated:",
        translated_blocks
    )

    print(
        "Time:",
        round(cost,2),
        "seconds"
    )



# ============================================================
# Main
# ============================================================


def main():


    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )



    cache = load_cache()



    files = sorted(

        INPUT_DIR.glob(
            "*.vtt"
        ),

        key=lambda x:

        int(x.stem)

    )



    print()

    print(
        "Found VTT:",
        len(files)
    )



    translated_count = 0



    try:


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

                and

                old.get("hash")

                ==

                sha

                and

                target.exists()

            ):


                print(

                    "⏭️ Skip",
                    episode,
                    "(cached)"

                )

                continue



            if translated_count >= MAX_FILES:

                print(
                    "Reached MAX_FILES"
                )

                break



            try:


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
                    ).isoformat()

                }


                save_cache(
                    cache
                )


                translated_count += 1


                success_files.append(
                    episode
                )



            except Exception as e:


                print()

                print(
                    "❌ Failed:",
                    episode
                )

                print(
                    e
                )

                traceback.print_exc()


                failed_files.append(
                    episode
                )



    except KeyboardInterrupt:


        print(
            "Interrupted"
        )



    finally:


        print()

        print("=" * 70)

        print(
            "🏁 Finished"
        )


        print(
            "Translated:",
            len(success_files)
        )


        print(
            "Failed:",
            len(failed_files)
        )


        print(
            "Gemini requests:",
            request_count
        )


        print(
            "Total time:",
            round(
                time.time()-start_time,
                2
            ),
            "seconds"
        )


        if success_files:

            print(
                "Success:",
                success_files
            )


        if failed_files:

            print(
                "Failed:",
                failed_files
            )



if __name__ == "__main__":

    main()