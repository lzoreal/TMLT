#!/usr/bin/env python3

import argparse
from pathlib import Path
import time
import re
import sys


from translatepy import Translator



# ============================================================
# Logger
# ============================================================

def log(message):

    print(
        f"[TRANSLATE] {message}",
        flush=True
    )



# ============================================================
# VTT parser
# ============================================================

def parse_vtt(text):

    blocks = re.split(
        r"\n\s*\n",
        text.strip()
    )

    result = []

    for block in blocks:

        lines = block.splitlines()

        if len(lines) < 3:
            result.append(block)
            continue


        timestamp_index = None


        for i, line in enumerate(lines):

            if "-->" in line:

                timestamp_index = i
                break


        if timestamp_index is None:

            result.append(block)

            continue



        header = lines[:timestamp_index+1]

        content = lines[
            timestamp_index+1:
        ]


        result.append(
            (
                header,
                content
            )
        )


    return result



# ============================================================
# Translate
# ============================================================


translator = Translator()



def translate_text(
    text,
    retries=3
):

    if not text.strip():

        return text



    for attempt in range(
        1,
        retries+1
    ):


        try:

            log(
                f"    API translate attempt {attempt}"
            )


            result = translator.translate(
                text,
                "Chinese"
            )


            translated = str(
                result
            )


            if translated:

                return translated



        except Exception as e:


            log(
                f"    ERROR: {e}"
            )


            if attempt < retries:

                wait = (
                    attempt * 5
                )

                log(
                    f"    Retry after {wait}s"
                )

                time.sleep(
                    wait
                )



    log(
        "    Translation failed, keep original"
    )


    return text



# ============================================================
# Translate VTT file
# ============================================================


def translate_vtt(
    source,
    target
):


    log(
        ""
    )


    log(
        f"Processing: {source.name}"
    )


    log(
        f"Input size: {source.stat().st_size} bytes"
    )



    text = source.read_text(
        encoding="utf-8"
    )


    blocks = parse_vtt(
        text
    )


    log(
        f"VTT blocks: {len(blocks)}"
    )



    output = []


    translated_lines = 0



    for block in blocks:


        if isinstance(
            block,
            str
        ):

            output.append(
                block
            )

            continue



        header, lines = block



        new_lines = []


        for line in lines:


            if not line.strip():

                new_lines.append(
                    line
                )

                continue



            log(
                f"    EN: {line[:80]}"
            )


            zh = translate_text(
                line
            )


            log(
                f"    ZH: {zh[:80]}"
            )


            new_lines.append(
                zh
            )


            translated_lines += 1



        output.append(
            "\n".join(
                header
                +
                new_lines
            )
        )



    target.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    target.write_text(
        "\n\n".join(output),
        encoding="utf-8"
    )



    log(
        f"Output: {target}"
    )


    log(
        f"Output size: {target.stat().st_size} bytes"
    )


    log(
        f"Translated lines: {translated_lines}"
    )





# ============================================================
# Main
# ============================================================


def main():


    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--input",
        default="docs/transcripts"
    )


    parser.add_argument(
        "--output",
        default="docs/transcripts/zh"
    )


    args = parser.parse_args()



    source_dir = Path(
        args.input
    )


    target_dir = Path(
        args.output
    )



    log(
        "================================"
    )


    log(
        "VTT Translation Start"
    )


    log(
        f"Input: {source_dir}"
    )


    log(
        f"Output: {target_dir}"
    )



    if not source_dir.exists():

        log(
            "ERROR: input directory missing"
        )

        sys.exit(1)



    files = sorted(
        source_dir.glob(
            "*.vtt"
        )
    )



    log(
        f"Found VTT files: {len(files)}"
    )



    translated = 0
    skipped = 0
    failed = 0



    for index, file in enumerate(
        files,
        1
    ):


        target = (
            target_dir
            /
            file.name
        )


        log(
            ""
        )


        log(
            f"[{index}/{len(files)}]"
        )



        if target.exists():

            log(
                "Already exists, skip"
            )

            skipped += 1

            continue



        try:


            translate_vtt(
                file,
                target
            )


            translated += 1



        except Exception as e:


            log(
                f"FAILED: {e}"
            )


            failed += 1




    log(
        ""
    )


    log(
        "================================"
    )


    log(
        "SUMMARY"
    )


    log(
        f"Translated: {translated}"
    )


    log(
        f"Skipped: {skipped}"
    )


    log(
        f"Failed: {failed}"
    )


    log(
        "Finished"
    )



if __name__ == "__main__":

    main()