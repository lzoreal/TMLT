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

            print(f"Fetching: {url}")

            r = session.get(
                url,
                timeout=30,
            )

            if r.status_code == 429:

                wait = 30 * (attempt + 1)

                print(f"Rate limited: {url}")
                print(f"Sleep {wait}s")

                time.sleep(wait)

                continue

            r.raise_for_status()

            return r.text

        except Exception as e:

            if attempt == retries - 1:
                raise e

            wait = 10 + attempt * 10

            print(f"Request failed: {e}")
            print(f"Sleep {wait}s")

            time.sleep(wait)

    raise Exception("Unable to fetch " + url)


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
            f"⚠️ JSON 读取失败: {path}: {e}"
        )

        return default


def save_json(path, data):

    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ============================================================
# Resolve audio URL
# ============================================================

def resolve_audio_url(url):
    """
    从包装后的 audio URL 中解析 /s/ 后面的真实音频地址。

    例如：

    https://pfx.vpixl.com/xxx/pdst.fm/e/prefix.up.audio/s/
    npr.simplecastaudio.com/xxx/episodes/xxx/audio/128/default.mp3?xxx

    解析为：

    https://npr.simplecastaudio.com/xxx/episodes/xxx/audio/128/default.mp3?xxx

    不限制任何域名。

    不判断：

    - castfire
    - megaphone
    - simplecast
    - npr
    - spotify
    - podtrac

    只按照 /s/ 规则解析。
    """

    if not url:
        return url

    marker = "/s/"

    if marker not in url:

        print(
            "   ℹ️ 未找到 /s/，保留原 audio"
        )

        return url

    _, rest = url.split(
        marker,
        1
    )

    if not rest:

        print(
            "   ⚠️ /s/ 后面没有内容，"
            "保留原 audio"
        )

        return url

    resolved = "https://" + rest

    print(
        "   🔎 解析原始 audio:"
    )

    print(
        f"      {url}"
    )

    print(
        "   ✅ 解析后的 audio:"
    )

    print(
        f"      {resolved}"
    )

    return resolved


# ============================================================
# Archive crawler
# ============================================================

def get_archive():
    """
    按年份抓取 This American Life 全部归档节目。
    从 1995 年开始，自动抓取到当前年份。
    使用 ?year=YYYY，不再依赖分页链接。
    """

    episodes = {}

    current_year = datetime.now().year
    failed_years = []

    for year in range(1995, current_year + 1):
        archive_page = f"/archive?year={year}"

        print("\n" + "=" * 70)
        print(f"Fetching archive year: {year}")
        print(f"URL: {BASE + archive_page}")

        try:
            content = fetch(BASE + archive_page)
            soup = BeautifulSoup(content, "html.parser")

            year_episodes = {}

            for a in soup.select("a.goto-episode"):
                href = a.get("href", "").strip()

                if not href:
                    continue

                match = re.search(r"^/(\d+)/", href)

                if not match:
                    continue

                episode = match.group(1)
                year_episodes[episode] = BASE + href

            print(f"Year {year}: found {len(year_episodes)} episodes")

            if year_episodes:
                episodes.update(year_episodes)
            else:
                # 空结果不一定代表该年份没有节目，
                # 也可能是页面结构变化或请求异常。
                print(f"WARNING: No episodes found for {year}")
                failed_years.append(year)

        except Exception as e:
            print(f"ERROR: Failed to fetch year {year}: {e}")
            failed_years.append(year)

        time.sleep(random.uniform(2, 5))

    print("\n" + "=" * 70)
    print(f"Total unique episodes found: {len(episodes)}")

    if failed_years:
        print(f"WARNING: Failed or empty years: {failed_years}")

        # 不允许某些年份抓取失败后继续生成可能不完整的 RSS。
        raise RuntimeError(
            "Archive crawl incomplete. "
            f"Failed or empty years: {failed_years}. "
            "Existing RSS should not be replaced."
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

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

    title = ""

    h1 = soup.find("h1")

    if h1:

        title = h1.get_text(
            " ",
            strip=True
        )

    # --------------------------------------------------------
    # Audio
    # --------------------------------------------------------

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
                "⚠️ playlist-data JSON "
                "解析失败:",
                e
            )

    # --------------------------------------------------------
    # Publication date
    # --------------------------------------------------------

    pub_date = ""

    meta = soup.select_one(
        "meta[property='article:published_time']"
    )

    if meta:

        pub_date = meta.get(
            "content",
            ""
        )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    description = ""

    desc = soup.select_one(
        "meta[name='description']"
    )

    if desc:

        description = desc.get(
            "content",
            ""
        )

    # --------------------------------------------------------
    # Episode image
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # 向上寻找 speaker
        # ----------------------------------------------------

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
        "",
    ]

    for index, item in enumerate(lines):

        start = item["start"]

        text = item["text"]

        speaker = item.get(
            "speaker",
            ""
        )

        if index + 1 < len(lines):

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
# Migrate / resolve old cache
# ============================================================

def update_resolved_audio(
    episodes_cache
):
    """
    检查历史 episodes.json。

    情况：

    1. 没有 resolved_audio
       -> 解析 audio

    2. resolved_audio == audio
       -> 说明旧版本没有成功解析
       -> 重新解析 audio

    3. resolved_audio != audio
       -> 已经成功解析
       -> 不重新解析

    最终把 resolved_audio 写回缓存。
    """

    print(
        "\n"
        + "=" * 70
    )

    print(
        "检查历史 episode 的 resolved_audio..."
    )

    changed = 0

    for episode in sorted(
        episodes_cache.keys(),
        key=lambda x: int(x)
        if str(x).isdigit()
        else 0,
        reverse=True,
    ):

        item = episodes_cache[
            episode
        ]

        audio = item.get(
            "audio",
            ""
        )

        if not audio:

            print(
                f"[{episode}] ⚠️ 没有 audio"
            )

            continue

        resolved_audio = item.get(
            "resolved_audio"
        )

        # ----------------------------------------------------
        # 没有 resolved_audio
        # ----------------------------------------------------

        if not resolved_audio:

            print(
                f"[{episode}] "
                "⚠️ 没有 resolved_audio"
            )

            print(
                "   🔄 重新解析"
            )

            new_resolved = (
                resolve_audio_url(
                    audio
                )
            )

            item[
                "resolved_audio"
            ] = new_resolved

            changed += 1

            continue

        # ----------------------------------------------------
        # resolved_audio == audio
        #
        # 旧版本可能把原始地址错误地保存成
        # resolved_audio。
        # ----------------------------------------------------

        if resolved_audio == audio:

            print(
                f"[{episode}] "
                "⚠️ resolved_audio == audio"
            )

            print(
                "   旧版本没有成功解析"
            )

            print(
                "   🔄 重新解析"
            )

            new_resolved = (
                resolve_audio_url(
                    audio
                )
            )

            if new_resolved != resolved_audio:

                item[
                    "resolved_audio"
                ] = new_resolved

                changed += 1

                print(
                    "   💾 resolved_audio "
                    "已更新"
                )

            else:

                print(
                    "   ℹ️ 解析结果仍然相同"
                )

            continue

        # ----------------------------------------------------
        # 已经成功解析
        # ----------------------------------------------------

        print(
            f"[{episode}] "
            "✅ resolved_audio 已存在"
        )

    print(
        f"历史缓存更新: {changed} 条"
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

    # --------------------------------------------------------
    # 最新 episode 在前
    # --------------------------------------------------------

    for episode in sorted(
        episodes.keys(),
        key=lambda x: int(x)
        if str(x).isdigit()
        else 0,
        reverse=True,
    ):

        item = episodes[
            episode
        ]

        title = html.escape(
            item.get(
                "title",
                ""
            )
        )

        episode_url = html.escape(
            item.get(
                "url",
                ""
            ),
            quote=True,
        )

        description = html.escape(
            item.get(
                "description",
                ""
            )
        )

        image = html.escape(
            item.get(
                "image",
                ""
            ),
            quote=True,
        )

        pub_date = rss_date(
            item.get(
                "pubDate",
                ""
            )
        )

        # ----------------------------------------------------
        # 关键：
        #
        # RSS enclosure 使用 resolved_audio
        #
        # 如果没有 resolved_audio，
        # 才 fallback 到 audio。
        # ----------------------------------------------------

        resolved_audio = (
            item.get(
                "resolved_audio"
            )
            or item.get(
                "audio",
                ""
            )
        )

        resolved_audio = html.escape(
            resolved_audio,
            quote=True,
        )

        rss += f"""

<item>

<title>
{title}
</title>

<link>
{episode_url}
</link>

<guid isPermaLink="true">
{episode_url}
</guid>

<description>
{description}
</description>

<itunes:image
href="{image}"
/>

<podcast:image
href="{image}"
/>

<pubDate>
{pub_date}
</pubDate>

<enclosure
url="{resolved_audio}"
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
        required=True,
    )

    parser.add_argument(
        "--output",
        default="public",
    )

    args = parser.parse_args()

    output = Path(
        args.output
    )

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    transcript_dir = (
        output / "transcripts"
    )

    transcript_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file = (
        output / "episodes.json"
    )

    # ========================================================
    # Load cache
    # ========================================================

    episodes_cache = load_json(
        cache_file,
        {},
    )

    print(
        "Cached episodes:",
        len(episodes_cache),
    )

    # ========================================================
    # Crawl archive
    # ========================================================

    archive = get_archive()

    print(
        "Archive episodes:",
        len(archive),
    )

    # ========================================================
    # Process new episodes
    # ========================================================

    new_count = 0

    for episode, url in archive.items():

        if episode in episodes_cache:

            continue

        print(
            "\n"
            + "=" * 70
        )

        print(
            "New episode:",
            episode
        )

        try:

            # ------------------------------------------------
            # Episode information
            # ------------------------------------------------

            info = get_episode_info(
                url
            )

            if not info.get(
                "audio"
            ):

                print(
                    "❌ No audio:",
                    episode
                )

                continue

            # ------------------------------------------------
            # Resolve audio
            # ------------------------------------------------

            print(
                "🔎 解析音频地址..."
            )

            resolved_audio = (
                resolve_audio_url(
                    info["audio"]
                )
            )

            info[
                "resolved_audio"
            ] = resolved_audio

            # ------------------------------------------------
            # Transcript
            # ------------------------------------------------

            vtt_file = (
                transcript_dir
                / f"{episode}.vtt"
            )

            transcript = (
                get_transcript(
                    episode
                )
            )

            if transcript:

                vtt_file.write_text(
                    create_vtt(
                        transcript
                    ),
                    encoding="utf-8",
                )

                print(
                    "VTT generated:",
                    vtt_file
                )

            else:

                print(
                    "⚠️ No transcript:",
                    episode
                )

            # ------------------------------------------------
            # Cache
            # ------------------------------------------------

            episodes_cache[
                episode
            ] = info

            new_count += 1

        except Exception as e:

            print(
                "❌ Failed:",
                episode,
                e
            )

        time.sleep(
            random.uniform(
                5,
                10
            )
        )

    print(
        "\nNew episodes added:",
        new_count
    )

    # ========================================================
    # IMPORTANT:
    #
    # 检查历史 episode。
    #
    # 解决旧版本 episodes.json 没有
    # resolved_audio 的问题。
    # ========================================================

    update_resolved_audio(
        episodes_cache
    )

    # ========================================================
    # Save cache
    # ========================================================

    save_json(
        cache_file,
        episodes_cache
    )

    print(
        "Episodes cache saved:",
        cache_file
    )

    # ========================================================
    # Generate RSS
    # ========================================================

    rss = create_rss(
        episodes_cache,
        args.base_url,
    )

    rss_file = (
        output / "podcast.xml"
    )

    rss_file.write_text(
        rss,
        encoding="utf-8",
    )

    print(
        "RSS generated:",
        rss_file
    )

    # ========================================================
    # Show summary
    # ========================================================

    print(
        "\n"
        + "=" * 70
    )

    print(
        "完成"
    )

    print(
        f"Episodes: {len(episodes_cache)}"
    )

    print(
        f"RSS: {rss_file}"
    )


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":
    main()
