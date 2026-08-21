#!/usr/bin/env python3

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET
import json
import os
import sys

PODCAST_NS = "https://podcastindex.org/namespace/1.0"


ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")

ET.register_namespace("podcast", PODCAST_NS)


# ============================================================
# Log
# ============================================================


def log(msg):
    print(f"[ZH-FEED] {msg}", flush=True)


# ============================================================
# Load translation status
# ============================================================


def load_status():

    path = Path("docs/translations.json")

    if not path.exists():

        log("WARNING: translations.json missing")

        return {}

    try:

        data = json.loads(path.read_text(encoding="utf-8"))

        log(f"Loaded translation status: {len(data)} episodes")

        return data

    except Exception as e:

        log(f"Failed loading status: {e}")

        return {}


# ============================================================
# Main
# ============================================================


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--base-url", required=True)

    args = parser.parse_args()

    log("====================================")

    log("Generate Chinese Podcast Feed")

    log("====================================")

    log(f"Base URL: {args.base_url}")

    status = load_status()

    docs = Path("docs")

    source = docs / "podcast.xml"

    output = docs / "podcast-zh.xml"

    if not source.exists():

        log("ERROR: podcast.xml missing")

        sys.exit(1)

    log(f"Input RSS: {source}")

    tree = ET.parse(source)

    root = tree.getroot()

    channel = root.find("channel")

    if channel is None:

        log("ERROR: channel missing")

        sys.exit(1)

    # ========================================================
    # Channel metadata
    # ========================================================

    title = channel.find("title")

    if title is not None:

        title.text = "This American Life " "中文双语 Transcript Feed"

    language = channel.find("language")

    if language is not None:

        language.text = "zh-CN"

    description = channel.find("description")

    if description is not None:

        description.text = "English Chinese " "bilingual transcript feed"

    # ========================================================
    # Items
    # ========================================================

    items = channel.findall("item")

    log(f"Original episodes: {len(items)}")

    kept = 0

    removed = 0

    for item in list(items):

        title_node = item.find("title")

        title_text = title_node.text if title_node is not None else "UNKNOWN"

        transcript = item.find(f"{{{PODCAST_NS}}}transcript")

        if transcript is None:

            log(f"REMOVE {title_text}: no transcript")

            channel.remove(item)

            removed += 1

            continue

        old_url = transcript.get("url", "")

        #
        # 从原 VTT:
        #
        # .../transcripts/123.vtt
        #
        # 得到 episode id
        #

        if "/transcripts/" not in old_url:

            log(f"REMOVE {title_text}: bad url")

            channel.remove(item)

            removed += 1

            continue

        episode = old_url.split("/transcripts/")[-1].replace(".vtt", "")

        info = status.get(episode)

        if not info:

            log(f"REMOVE {episode}: not translated")

            channel.remove(item)

            removed += 1

            continue

        if not info.get("translated", False):

            log(f"REMOVE {episode}: translation incomplete")

            channel.remove(item)

            removed += 1

            continue

        new_url = args.base_url + "/transcripts/zh/" + episode + ".vtt"

        transcript.set("url", new_url)

        transcript.set("language", "zh-CN")

        log(f"KEEP {episode}: {new_url}")

        kept += 1

    # ========================================================
    # Atom self link
    # ========================================================

    atom_link = channel.find("{http://www.w3.org/2005/Atom}link")

    if atom_link is not None:

        atom_link.set("href", args.base_url + "/podcast-zh.xml")

    # ========================================================
    # Write
    # ========================================================

    tree.write(output, encoding="utf-8", xml_declaration=True)

    log("====================================")

    log("SUMMARY")

    log(f"Kept translated episodes: {kept}")

    log(f"Removed episodes: {removed}")

    log(f"Output: {output}")

    log("Done")


if __name__ == "__main__":

    main()
