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

INPUT_DIR = Path(
    "docs/transcripts"
)

OUTPUT_DIR = Path(
    "docs/transcripts/zh"
)

CACHE_FILE = Path(
    "docs/translations.json"
)


MODEL = "gemini-2.5-flash"


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


REQUEST_INTERVAL = 5



client = genai.Client(
    api_key=os.environ[
        "GEMINI_API_KEY"
    ]
)



request_count = 0



# ============================================================
# Cache
# ============================================================


def load_cache():

    if not CACHE_FILE.exists():

        return {}


    try:

        return json.loads(
            CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return {}



def save_cache(data):

    CACHE_FILE.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )



# ============================================================
# Hash
# ============================================================


def file_hash(path):

    sha = hashlib.sha256()

    with path.open(
        "rb"
    ) as f:

        for chunk in iter(
            lambda:
            f.read(8192),
            b"",
        ):

            sha.update(
                chunk
            )


    return sha.hexdigest()



# ============================================================
# Gemini
# ============================================================


def gemini_translate(text):

    global request_count


    if request_count >= MAX_REQUESTS:

        raise RuntimeError(
            "MAX_REQUESTS reached"
        )


    request_count += 1



    prompt = f"""
Translate this English podcast transcript into Simplified Chinese.

Rules:

- Only output Chinese translation.
- Do not summarize.
- Do not explain.
- Keep names accurate.
- Preserve punctuation.

Text:

{text}
"""


    for retry in range(5):

        try:

            response = (
                client.models.generate_content(
                    model=MODEL,
                    contents=prompt,
                )
            )


            return (
                response.text
                .strip()
            )


        except Exception as e:


            print(
                "Gemini error:",
                e
            )


            wait = (
                30 *
                (retry + 1)
            )


            print(
                "Retry after",
                wait,
                "seconds"
            )


            time.sleep(
                wait
            )


    raise RuntimeError(
        "Gemini failed"
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

    return re.split(
        r"\n\n+",
        content.strip()
    )



def translate_block(block):


    lines = block.splitlines()



    if len(lines) < 3:

        return block



    # WEBVTT

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
# Translate file
# ============================================================


def translate_file(
    source,
    target
):


    print(
        "Translating:",
        source
    )


    content = source.read_text(
        encoding="utf-8"
    )


    blocks = split_blocks(
        content
    )


    result = []


    for block in blocks:


        result.append(
            translate_block(
                block
            )
        )


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



# ============================================================
# Main
# ============================================================


def main():


    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


    cache = load_cache()


    translated = 0



    for source in sorted(

        INPUT_DIR.glob(
            "*.vtt"
        ),

        key=lambda x:
        int(x.stem)

    ):


        episode = source.stem


        target = (
            OUTPUT_DIR /
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

            and old.get(
                "hash"
            ) == sha

            and target.exists()

        ):

            print(
                "Skip:",
                episode
            )

            continue



        if translated >= MAX_FILES:

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


        translated += 1



    print(
        "Finished:",
        translated
    )


if __name__ == "__main__":

    main()