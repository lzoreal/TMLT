#!/usr/bin/env python3

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET
import os
import sys


PODCAST_NS = (
    "https://podcastindex.org/namespace/1.0"
)


ET.register_namespace(
    "atom",
    "http://www.w3.org/2005/Atom"
)

ET.register_namespace(
    "itunes",
    "http://www.itunes.com/dtds/podcast-1.0.dtd"
)

ET.register_namespace(
    "podcast",
    PODCAST_NS
)



def log(msg):

    print(
        f"[ZH-FEED] {msg}",
        flush=True
    )



def main():

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--base-url",
        required=True
    )


    args = parser.parse_args()


    log(
        "===================================="
    )

    log(
        "Generate Chinese Podcast Feed"
    )

    log(
        "===================================="
    )


    log(
        f"Current directory: {os.getcwd()}"
    )


    log(
        f"Base URL: {args.base_url}"
    )


    docs = Path(
        "docs"
    )


    source = (
        docs /
        "podcast.xml"
    )


    output = (
        docs /
        "podcast-zh.xml"
    )


    # --------------------------------------------------------
    # Check files
    # --------------------------------------------------------

    log(
        f"Input RSS: {source}"
    )


    if not source.exists():

        log(
            "ERROR: podcast.xml not found"
        )

        sys.exit(1)


    log(
        f"Input size: {source.stat().st_size} bytes"
    )



    # --------------------------------------------------------
    # Parse XML
    # --------------------------------------------------------

    log(
        "Parsing XML..."
    )


    try:

        tree = ET.parse(
            source
        )

    except Exception as e:

        log(
            f"XML parse failed: {e}"
        )

        sys.exit(1)



    root = tree.getroot()


    log(
        f"Root tag: {root.tag}"
    )



    channel = root.find(
        "channel"
    )


    if channel is None:

        log(
            "ERROR: channel missing"
        )

        sys.exit(1)



    # --------------------------------------------------------
    # Channel update
    # --------------------------------------------------------

    log(
        "Updating channel metadata..."
    )


    title = channel.find(
        "title"
    )

    if title is not None:

        log(
            f"Old title: {title.text}"
        )

        title.text = (
            "This American Life "
            "中文双语 Transcript Feed"
        )


    language = channel.find(
        "language"
    )

    if language is not None:

        log(
            f"Old language: {language.text}"
        )

        language.text = (
            "zh-CN"
        )



    description = channel.find(
        "description"
    )

    if description is not None:

        description.text = (
            "This American Life "
            "English Chinese bilingual "
            "transcript feed"
        )



    atom_link = channel.find(
        "{http://www.w3.org/2005/Atom}link"
    )


    if atom_link is not None:

        old_link = atom_link.get(
            "href"
        )


        log(
            f"Old self link: {old_link}"
        )


        new_link = (
            args.base_url
            +
            "/podcast-zh.xml"
        )


        atom_link.set(
            "href",
            new_link
        )


        log(
            f"New self link: {new_link}"
        )



    # --------------------------------------------------------
    # Items
    # --------------------------------------------------------

    items = channel.findall(
        "item"
    )


    log(
        f"Total episodes: {len(items)}"
    )


    updated = 0
    skipped = 0
    missing = 0



    for index, item in enumerate(items, 1):


        title_node = item.find(
            "title"
        )


        title_text = (
            title_node.text
            if title_node is not None
            else "UNKNOWN"
        )


        log(
            ""
        )

        log(
            f"[{index}/{len(items)}] {title_text}"
        )



        transcript = item.find(
            f"{{{PODCAST_NS}}}transcript"
        )


        if transcript is None:

            log(
                "  WARNING: no transcript tag"
            )

            missing += 1

            continue



        old_url = transcript.get(
            "url",
            ""
        )


        log(
            f"  Original VTT: {old_url}"
        )



        if "/transcripts/zh/" in old_url:

            log(
                "  Already Chinese feed"
            )

            skipped += 1

            continue



        if "/transcripts/" not in old_url:

            log(
                "  WARNING: unexpected URL"
            )

            missing += 1

            continue



        episode = (
            old_url
            .split(
                "/transcripts/"
            )[-1]
        )


        new_url = (
            args.base_url
            +
            "/transcripts/zh/"
            +
            episode
        )


        transcript.set(
            "url",
            new_url
        )


        transcript.set(
            "language",
            "zh-CN"
        )


        log(
            f"  New VTT: {new_url}"
        )


        updated += 1



    # --------------------------------------------------------
    # Write
    # --------------------------------------------------------

    log(
        ""
    )

    log(
        "Writing output..."
    )


    tree.write(
        output,
        encoding="utf-8",
        xml_declaration=True
    )


    log(
        f"Output: {output}"
    )


    log(
        f"Output size: {output.stat().st_size} bytes"
    )


    log(
        "===================================="
    )

    log(
        "SUMMARY"
    )

    log(
        f"Updated: {updated}"
    )

    log(
        f"Skipped: {skipped}"
    )

    log(
        f"Missing: {missing}"
    )


    log(
        "Done"
    )



if __name__ == "__main__":

    main()