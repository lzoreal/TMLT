#!/usr/bin/env python3

import argparse
import json
import html
import random
import re
import time

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


BASE = "https://www.thisamericanlife.org"


# ============================================================
# Session
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.thisamericanlife.org/",
    }
)


# ============================================================
# HTTP Request
# ============================================================

def fetch(url, retries=5):

    for attempt in range(retries):

        try:

            r = session.get(
                url,
                timeout=30
            )

            if r.status_code == 429:

                wait = 30 * (attempt + 1)

                print(
                    f"Rate limited: {url}"
                )

                print(
                    f"Sleep {wait}s"
                )

                time.sleep(wait)

                continue

            r.raise_for_status()

            return r.text

        except Exception as e:

            if attempt == retries - 1:
                raise e

            wait = 10 + attempt * 10

            print(
                f"Request failed: {e}"
            )

            print(
                f"Sleep {wait}s"
            )

            time.sleep(wait)

    raise Exception(
        "Unable to fetch " + url
    )


# ============================================================
# JSON Cache
# ============================================================

def load_json(path, default):

    if not path.exists():

        return default

    try:

        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception as e:

        print(
            f"Failed to load JSON: {path}"
        )

        print(e)

        return default


def save_json(path, data):

    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# Archive crawler
# ============================================================

def get_archive():

    episodes = {}

    archive_page = "/archive"

    while True:

        print(
            "Fetching archive:",
            archive_page
        )

        content = fetch(
            BASE + archive_page
        )

        soup = BeautifulSoup(
            content,
            "html.parser"
        )

        found = 0

        for a in soup.select(
            "a.goto-episode"
        ):

            href = a.get("href")

            if not href:
                continue

            m = re.search(
                r"^/(\d+)/",
                href
            )

            if not m:
                continue

            episode = m.group(1)

            episodes[
                episode
            ] = BASE + href

            found += 1

        print(
            "Found:",
            found
        )

        pager = soup.select_one(
            "a.pager"
        )

        if not pager:
            break

        next_page = pager.get(
            "href"
        )

        if (
            not next_page
            or next_page == archive_page
        ):
            break

        archive_page = next_page

        time.sleep(
            random.uniform(5, 10)
        )

    return episodes


# ============================================================
# Episode parser
# ============================================================

def get_episode_info(url):

    print(
        "Fetching episode:",
        url
    )

    content = fetch(url)

    soup = BeautifulSoup(
        content,
        "html.parser"
    )

    title = ""

    h1 = soup.find("h1")

    if h1:

        title = h1.get_text(
            " ",
            strip=True
        )

    audio = ""

    playlist = soup.select_one(
        "script#playlist-data"
    )

    if playlist and playlist.string:

        try:

            data = json.loads(
                playlist.string
            )

            audio = data.get(
                "audio",
                ""
            )

            title = data.get(
                "title",
                title
            )

        except Exception as e:

            print(
                "Playlist JSON parse failed:",
                e
            )

    pub_date = ""

    meta = soup.select_one(
        "meta[property='article:published_time']"
    )

    if meta:

        pub_date = meta.get(
            "content",
            ""
        )

    description = ""

    desc = soup.select_one(
        "meta[name='description']"
    )

    if desc:

        description = desc.get(
            "content",
            ""
        )

    # ========================================================
    # Episode image
    # ========================================================

    image = ""

    episode_img = soup.select_one(
        "figure.tal-episode-image img"
    )

    if episode_img:

        image = episode_img.get(
            "src",
            ""
        )

    if not image:

        og_image = soup.select_one(
            "meta[property='og:image']"
        )

        if og_image:

            image = og_image.get(
                "content",
                ""
            )

    return {
        "title": title,
        "audio": audio,
        "url": url,
        "pubDate": pub_date,
        "description": description,
        "image": image,
    }


# ============================================================
# Audio URL resolver
# ============================================================

AUDIO_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".flac",
)


def looks_like_audio_path(path):

    if not path:
        return False

    clean_path = (
        path.lower()
        .split("?")[0]
    )

    return clean_path.endswith(
        AUDIO_EXTENSIONS
    )


def resolve_audio_url(audio_url):

    """
    将多层 tracking / redirect / prefix URL
    解析成真正的音频地址。

    例如：

    https://pfx.vpixl.com/6qj4J/
    dts.podtrac.com/redirect.mp/
    pdst.fm/e/
    prefix.up.audio/s/
    npr.simplecastaudio.com/
    xxx/default.mp3?...

    →

    https://npr.simplecastaudio.com/
    xxx/default.mp3?...

    同样支持：

    pdst.fm/.../serve.castfire.com/audio/xxx.mp3

    pdst.fm/.../traffic.megaphone.fm/xxx.mp3
    """

    if not audio_url:

        return ""

    audio_url = audio_url.strip()

    if not audio_url:

        return ""

    # ========================================================
    # 已经是直接音频 URL
    # ========================================================

    parsed = urlparse(
        audio_url
    )

    if (
        parsed.scheme in (
            "http",
            "https"
        )
        and parsed.netloc
        and looks_like_audio_path(
            parsed.path
        )
    ):

        print(
            "   🎧 Already direct audio URL"
        )

        return audio_url

    # ========================================================
    # 在完整 URL 中寻找所有 hostname
    # ========================================================

    hostname_pattern = re.compile(
        r"(?<![A-Za-z0-9.-])"
        r"("
        r"(?:[A-Za-z0-9-]+\.)+"
        r"[A-Za-z]{2,}"
        r")"
        r"(?=/)"
    )

    matches = list(
        hostname_pattern.finditer(
            audio_url
        )
    )

    if not matches:

        print(
            "   ⚠️ No nested hostname found"
        )

        print(
            "   保留原地址"
        )

        return audio_url

    # ========================================================
    # 从最后一个 hostname 开始向前寻找
    # ========================================================

    for match in reversed(matches):

        host = match.group(1)

        start = match.start()

        candidate = (
            "https://"
            + audio_url[start:]
        )

        candidate_parsed = urlparse(
            candidate
        )

        if not candidate_parsed.netloc:
            continue

        if looks_like_audio_path(
            candidate_parsed.path
        ):

            print(
                "   🔎 Real audio host:",
                host
            )

            print(
                "   📎 Original:"
            )

            print(
                "      ",
                audio_url
            )

            print(
                "   🎧 Resolved:"
            )

            print(
                "      ",
                candidate
            )

            return candidate

    # ========================================================
    # 如果没有识别出来
    # ========================================================

    print(
        "   ⚠️ Could not resolve audio URL"
    )

    print(
        "   保留原地址"
    )

    return audio_url


# ============================================================
# Transcript parser
# ============================================================

def parse_time(value):

    if not value:
        return None

    try:

        parts = value.split(":")

        if len(parts) == 3:

            h = float(parts[0])
            m = float(parts[1])
            s = float(parts[2])

            return (
                h * 3600
                + m * 60
                + s
            )

        elif len(parts) == 2:

            m = float(parts[0])
            s = float(parts[1])

            return (
                m * 60
                + s
            )

    except Exception:

        return None

    return None


def get_transcript(episode):

    url = (
        f"{BASE}/{episode}/transcript"
    )

    print(
        "Fetching transcript:",
        episode
    )

    content = fetch(url)

    soup = BeautifulSoup(
        content,
        "html.parser"
    )

    lines = []

    for p in soup.select(
        "p[begin]"
    ):

        begin = p.get(
            "begin"
        )

        start = parse_time(
            begin
        )

        if start is None:
            continue

        speaker = ""

        parent = p

        while parent:

            h4 = parent.find("h4")

            if h4:

                speaker = h4.get_text(
                    " ",
                    strip=True
                )

                break

            parent = parent.parent

        text = p.get_text(
            " ",
            strip=True
        )

        if text:

            lines.append(
                {
                    "start": start,
                    "speaker": speaker,
                    "text": text,
                }
            )

    return lines


# ============================================================
# VTT generator
# ============================================================

def format_vtt_time(seconds):

    milliseconds = int(
        round(seconds * 1000)
    )

    hour = (
        milliseconds
        // 3600000
    )

    milliseconds %= 3600000

    minute = (
        milliseconds
        // 60000
    )

    milliseconds %= 60000

    second = (
        milliseconds
        // 1000
    )

    ms = milliseconds % 1000

    return (
        f"{hour:02}:"
        f"{minute:02}:"
        f"{second:02}."
        f"{ms:03}"
    )


def create_vtt(lines):

    output = [
        "WEBVTT",
        ""
    ]

    for index, item in enumerate(
        lines
    ):

        start = item["start"]

        text = item["text"]

        speaker = item.get(
            "speaker",
            ""
        )

        if (
            index + 1
            < len(lines)
        ):

            end = lines[
                index + 1
            ]["start"]

        else:

            end = start + 5

        output.append(
            f"{format_vtt_time(start)} --> "
            f"{format_vtt_time(end)}"
        )

        if speaker:

            output.append(
                f"<v {speaker}>{text}"
            )

        else:

            output.append(text)

        output.append("")

    return "\n".join(output)


# ============================================================
# RSS date
# ============================================================

def rss_date(date_string):

    if not date_string:
        return ""

    try:

        dt = datetime.fromisoformat(
            date_string.replace(
                "Z",
                "+00:00"
            )
        )

        return format_datetime(dt)

    except Exception:

        return date_string


# ============================================================
# Resolve cached audio
# ============================================================

def update_resolved_audio(
    episodes_cache,
    force=False
):

    """
    给历史 episodes.json 做音频地址迁移。

    默认行为：

    - 已经有 resolved_audio：
        不重新解析

    - 没有 resolved_audio：
        解析一次并保存

    force=True：

    - 所有 episode 重新解析
    """

    changed = False

    resolved_count = 0
    skipped_count = 0

    total = len(
        episodes_cache
    )

    print()
    print(
        "========================================"
    )
    print(
        "Checking resolved audio URLs"
    )
    print(
        "========================================"
    )

    for index, (
        episode,
        item
    ) in enumerate(
        episodes_cache.items(),
        1
    ):

        if not isinstance(
            item,
            dict
        ):

            print(
                f"[{index}/{total}] "
                f"Invalid episode data: "
                f"{episode}"
            )

            continue

        current_resolved = (
            item.get(
                "resolved_audio",
                ""
            )
            or ""
        ).strip()

        original_audio = (
            item.get(
                "audio",
                ""
            )
            or ""
        ).strip()

        # ====================================================
        # 已经缓存
        # ====================================================

        if (
            current_resolved
            and not force
        ):

            skipped_count += 1

            continue

        # ====================================================
        # 没有原始地址
        # ====================================================

        if not original_audio:

            print(
                f"[{index}/{total}] "
                f"{episode}: "
                f"no original audio"
            )

            continue

        # ====================================================
        # 重新解析
        # ====================================================

        print()
        print(
            f"[{index}/{total}] "
            f"Resolving episode {episode}"
        )

        resolved = resolve_audio_url(
            original_audio
        )

        if not resolved:

            print(
                "   ⚠️ Resolve failed"
            )

            continue

        # ====================================================
        # 只有真正发生变化才写
        # ====================================================

        if (
            item.get(
                "resolved_audio"
            )
            != resolved
        ):

            item[
                "resolved_audio"
            ] = resolved

            changed = True

            resolved_count += 1

        else:

            skipped_count += 1

    print()
    print(
        "Audio resolve summary:"
    )

    print(
        "   Newly resolved:",
        resolved_count
    )

    print(
        "   Already cached:",
        skipped_count
    )

    return changed


# ============================================================
# RSS generator
# ============================================================

def create_rss(
    episodes,
    base_url
):

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>

<rss version="2.0"
xmlns:atom="http://www.w3.org/2005/Atom"
xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
xmlns:podcast="https://podcastindex.org/namespace/1.0">

<channel>

<title>This American Life Transcript Feed</title>

<itunes:image
href="https://www.thisamericanlife.org/sites/default/files/images/promo/tal_partners_blue_-_16x9.jpg"
/>

<podcast:image
href="https://www.thisamericanlife.org/sites/default/files/images/promo/tal_partners_blue_-_16x9.jpg"
/>

<link>
https://www.thisamericanlife.org/
</link>

<atom:link
href="{html.escape(base_url, quote=True)}/podcast.xml"
rel="self"
type="application/rss+xml"/>

<description>
Unofficial This American Life feed with VTT transcripts
</description>

<language>en-us</language>

<generator>
TMLT Transcript Generator
</generator>

"""

    for episode in sorted(
        episodes.keys(),
        key=lambda x: int(x),
        reverse=True
    ):

        item = episodes[
            episode
        ]

        # ====================================================
        # 这里不再调用 resolve_audio_url()
        #
        # RSS 直接使用已经缓存好的 resolved_audio
        # ====================================================

        resolved_audio = (
            item.get(
                "resolved_audio",
                ""
            )
            or ""
        ).strip()

        # 理论上的安全兜底：
        # 如果仍然没有 resolved_audio，
        # 使用原始 audio。
        if not resolved_audio:

            resolved_audio = (
                item.get(
                    "audio",
                    ""
                )
                or ""
            ).strip()

            print(
                f"⚠️ Episode {episode} "
                f"没有 resolved_audio，"
                f"RSS 使用原始 audio"
            )

        rss += f"""

<item>

<title>
{html.escape(item.get("title", ""))}
</title>


<link>
{html.escape(item.get("url", ""), quote=True)}
</link>


<guid isPermaLink="true">
{html.escape(item.get("url", ""), quote=True)}
</guid>


<description>
{html.escape(item.get("description", ""))}
</description>


<itunes:image
href="{html.escape(item.get("image", ""), quote=True)}"
/>


<podcast:image
href="{html.escape(item.get("image", ""), quote=True)}"
/>


<pubDate>
{rss_date(item.get("pubDate", ""))}
</pubDate>


<enclosure
url="{html.escape(resolved_audio, quote=True)}"
type="audio/mpeg"
/>


<podcast:transcript
url="{html.escape(base_url, quote=True)}/transcripts/{episode}.vtt"
type="text/vtt"
language="en"
/>


</item>

"""

    rss += """

</channel>

</rss>
"""

    return rss


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--base-url",
        required=True
    )

    parser.add_argument(
        "--output",
        default="public"
    )

    parser.add_argument(
        "--reparse-audio",
        action="store_true",
        help=(
            "强制重新解析所有历史 episode "
            "的 audio 地址"
        )
    )

    args = parser.parse_args()

    output = Path(
        args.output
    )

    output.mkdir(
        parents=True,
        exist_ok=True
    )

    transcript_dir = (
        output / "transcripts"
    )

    transcript_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    cache_file = (
        output / "episodes.json"
    )

    # ========================================================
    # Load cache
    # ========================================================

    episodes_cache = load_json(
        cache_file,
        {}
    )

    print(
        "Cached episodes:",
        len(episodes_cache)
    )

    # ========================================================
    # Crawl archive
    # ========================================================

    archive = get_archive()

    print(
        "Archive episodes:",
        len(archive)
    )

    # ========================================================
    # Download new episode information
    # ========================================================

    new_count = 0

    for episode, url in archive.items():

        if episode in episodes_cache:

            continue

        print()
        print(
            "========================================"
        )

        print(
            "New episode:",
            episode
        )

        print(
            "========================================"
        )

        try:

            info = get_episode_info(
                url
            )

            if not info["audio"]:

                print(
                    "No audio:",
                    episode
                )

                continue

            # =================================================
            # 新 episode：
            # 这里立即解析一次
            # =================================================

            print(
                "Resolving audio URL..."
            )

            resolved_audio = (
                resolve_audio_url(
                    info["audio"]
                )
            )

            info[
                "resolved_audio"
            ] = resolved_audio

            # =================================================
            # Transcript
            # =================================================

            vtt_file = (
                transcript_dir
                / f"{episode}.vtt"
            )

            transcript = get_transcript(
                episode
            )

            if transcript:

                vtt_file.write_text(
                    create_vtt(
                        transcript
                    ),
                    encoding="utf-8"
                )

            episodes_cache[
                episode
            ] = info

            new_count += 1

        except Exception as e:

            print(
                "Failed:",
                episode,
                e
            )

        time.sleep(
            random.uniform(5, 10)
        )

    print()
    print(
        "New episodes added:",
        new_count
    )

    # ========================================================
    # Migration / cache repair
    #
    # 旧 episodes.json 没有 resolved_audio：
    # 在这里补一次。
    #
    # 新 episode 已经有 resolved_audio，
    # 因此不会再次解析。
    # ========================================================

    changed = update_resolved_audio(
        episodes_cache,
        force=args.reparse_audio
    )

    # ========================================================
    # Save cache
    # ========================================================

    if changed or new_count > 0:

        save_json(
            cache_file,
            episodes_cache
        )

        print(
            "episodes.json updated"
        )

    else:

        print(
            "episodes.json unchanged"
        )

    # ========================================================
    # Generate RSS
    # ========================================================

    print()
    print(
        "Generating RSS..."
    )

    rss = create_rss(
        episodes_cache,
        args.base_url
    )

    rss_file = (
        output / "podcast.xml"
    )

    rss_file.write_text(
        rss,
        encoding="utf-8"
    )

    print(
        "RSS generated:",
        rss_file
    )

    print()
    print(
        "========================================"
    )

    print(
        "Done"
    )

    print(
        "Episodes:",
        len(episodes_cache)
    )

    print(
        "RSS:",
        rss_file
    )

    print(
        "========================================"
    )


if __name__ == "__main__":

    main()
