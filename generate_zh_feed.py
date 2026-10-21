#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

from pathlib import Path

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
# File helpers
# ============================================================


def is_valid_vtt(path):
    """
    检查中文 VTT 是否具有最基本的有效结构。
    """

    path = Path(path)

    if not path.is_file():
        return False

    try:
        content = path.read_text(encoding="utf-8-sig")

        if not content.strip():
            return False

        if not content.lstrip().startswith("WEBVTT"):
            return False

        timestamp_pattern = (
            r"(?m)^\d{2,}:\d{2}:\d{2}\.\d{3}" r"\s+-->\s+" r"\d{2,}:\d{2}:\d{2}\.\d{3}"
        )

        return bool(re.search(timestamp_pattern, content))

    except (OSError, UnicodeError):
        return False


def file_hash(path):
    """
    计算源文件 hash。

    先统一换行符，避免 Windows 的 CRLF 与 Linux 的 LF
    被误判为源文件内容变化。
    """

    data = Path(path).read_bytes().replace(b"\r\n", b"\n")

    return hashlib.sha256(data).hexdigest()


def atomic_write_xml(tree, path):
    """
    先写临时文件并校验 XML，再原子替换目标文件。
    """

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )

    # tree.write() 会自行打开该路径，先关闭 mkstemp 的 fd。
    os.close(fd)

    temp_path = Path(temp_name)

    try:
        tree.write(
            temp_path,
            encoding="utf-8",
            xml_declaration=True,
        )

        # 替换前确认生成的是可解析的 XML。
        ET.parse(temp_path)

        temp_path.replace(path)

    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

        raise


# ============================================================
# Load translation status
# ============================================================


def load_status(path):
    """
    读取 translations.json。

    读取或解析失败时必须报错，不能返回空字典。
    否则下面的过滤逻辑会把所有 item 删除，生成空 feed。
    """

    path = Path(path)

    if not path.is_file():
        raise RuntimeError(f"Translation status file is missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))

    except Exception as exc:
        raise RuntimeError(f"Failed to read translation status: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Translation status root must be a JSON object")

    return data


# ============================================================
# Build feed
# ============================================================


def build_feed(docs, base_url):
    docs = Path(docs)

    status = load_status(docs / "translations.json")

    source = docs / "podcast.xml"

    output = docs / "podcast-zh.xml"

    if not source.is_file():
        raise RuntimeError(f"Input RSS is missing: {source}")

    log(f"Input RSS: {source}")

    try:
        tree = ET.parse(source)

    except ET.ParseError as exc:
        raise RuntimeError(f"Input RSS is not valid XML: {source}: {exc}") from exc

    root = tree.getroot()

    channel = root.find("channel")

    if channel is None:
        raise RuntimeError("Input RSS has no <channel>")

    # ========================================================
    # Channel metadata
    # ========================================================

    title = channel.find("title")

    if title is not None:
        title.text = "This American Life 中文双语 Transcript Feed"

    language = channel.find("language")

    if language is not None:
        language.text = "zh-CN"

    description = channel.find("description")

    if description is not None:
        description.text = "English Chinese bilingual transcript feed"

    # ========================================================
    # Items
    # ========================================================

    items = channel.findall("item")

    log(f"Original episodes: {len(items)}")

    base_url = base_url.rstrip("/")

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

        match = re.search(
            r"/transcripts/([^/?#]+)\.vtt(?:[?#].*)?$",
            old_url,
        )

        if not match:
            log(f"REMOVE {title_text}: bad transcript URL")
            channel.remove(item)
            removed += 1
            continue

        episode = match.group(1)

        info = status.get(episode)

        if not isinstance(info, dict):
            log(f"REMOVE {episode}: no translation status")
            channel.remove(item)
            removed += 1
            continue

        if not info.get("translated", False):
            log(f"REMOVE {episode}: translation incomplete")
            channel.remove(item)
            removed += 1
            continue

        zh_file = docs / "transcripts" / "zh" / f"{episode}.vtt"

        if not is_valid_vtt(zh_file):
            log(f"REMOVE {episode}: Chinese VTT missing or invalid")
            channel.remove(item)
            removed += 1
            continue

        # 如果缓存中存了源文件 hash，确认中文译文仍对应当前英文源。
        stored_hash = info.get("hash")

        en_file = docs / "transcripts" / f"{episode}.vtt"

        if stored_hash and en_file.is_file() and file_hash(en_file) != stored_hash:
            log(f"REMOVE {episode}: source VTT changed after translation")
            channel.remove(item)
            removed += 1
            continue

        new_url = f"{base_url}/transcripts/zh/{episode}.vtt"

        transcript.set("url", new_url)
        transcript.set("language", "zh-CN")

        log(f"KEEP {episode}: {new_url}")

        kept += 1

    # ========================================================
    # Refuse to silently wipe an existing feed
    # ========================================================

    if kept == 0 and output.is_file():

        try:
            old_items = ET.parse(output).getroot().findall(".//item")

        except ET.ParseError as exc:
            raise RuntimeError(
                "Existing Chinese feed is not valid XML; "
                f"refusing to overwrite: {output}: {exc}"
            ) from exc

        if old_items:
            raise RuntimeError(
                "No translated episode left; refusing to overwrite "
                f"non-empty feed: {output}"
            )

    # ========================================================
    # Atom self link
    # ========================================================

    atom_link = channel.find("{http://www.w3.org/2005/Atom}link")

    if atom_link is not None:
        atom_link.set("href", f"{base_url}/podcast-zh.xml")

    # ========================================================
    # Write atomically
    # ========================================================

    atomic_write_xml(tree, output)

    log("====================================")
    log("SUMMARY")
    log(f"Kept translated episodes: {kept}")
    log(f"Removed episodes: {removed}")
    log(f"Output: {output}")
    log("Done")

    return kept, removed


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

    try:
        build_feed(
            Path("docs"),
            args.base_url,
        )

    except Exception as exc:
        log(f"ERROR: {exc}")
        log("Existing Chinese feed was left unchanged")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
