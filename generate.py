#!/usr/bin/env python3

import argparse
import json
import html
import random
import re
import time

from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.thisamericanlife.org"


# =====================
# Session
# =====================

session = requests.Session()


session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/126.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml," "application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.thisamericanlife.org/",
    }
)


# =====================
# HTTP Request
# =====================


def fetch(url, retries=5):

    for attempt in range(retries):

        try:

            r = session.get(url, timeout=30)

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

            time.sleep(wait)

    raise Exception("Unable to fetch " + url)


# =====================
# JSON Cache
# =====================


def load_json(path, default):

    if not path.exists():

        return default

    try:

        return json.loads(path.read_text(encoding="utf-8"))

    except Exception:

        return default


def save_json(path, data):

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# =====================
# Archive crawler
# =====================


def get_archive():

    episodes = {}

    archive_page = "/archive"

    while True:

        print("Fetching archive:", archive_page)

        content = fetch(BASE + archive_page)

        soup = BeautifulSoup(content, "html.parser")

        found = 0

        for a in soup.select("a.goto-episode"):

            href = a.get("href")

            if not href:

                continue

            m = re.search(r"^/(\d+)/", href)

            if not m:

                continue

            episode = m.group(1)

            episodes[episode] = BASE + href

            found += 1

        print("Found:", found)

        break

        pager = soup.select_one("a.pager")

        if not pager:

            break

        next_page = pager.get("href")

        if not next_page or next_page == archive_page:

            break

        archive_page = next_page

        time.sleep(random.uniform(5, 10))

    return episodes


# =====================
# Episode parser
# =====================


def get_episode_info(url):

    print("Fetching episode:", url)

    content = fetch(url)

    soup = BeautifulSoup(content, "html.parser")

    title = ""

    h1 = soup.find("h1")

    if h1:

        title = h1.get_text(" ", strip=True)

    audio = ""

    playlist = soup.select_one("script#playlist-data")

    if playlist and playlist.string:

        try:

            data = json.loads(playlist.string)

            audio = data.get("audio", "")

            title = data.get("title", title)

        except Exception:

            pass

    pub_date = ""

    meta = soup.select_one("meta[property='article:published_time']")

    if meta:

        pub_date = meta.get("content", "")

    description = ""

    desc = soup.select_one("meta[name='description']")

    if desc:

        description = desc.get("content", "")

    return {
        "title": title,
        "audio": audio,
        "url": url,
        "pubDate": pub_date,
        "description": description,
    }


# =====================
# Transcript parser
# =====================


def parse_time(value):

    if not value:

        return None

    try:

        parts = value.split(":")

        if len(parts) == 3:

            h = float(parts[0])

            m = float(parts[1])

            s = float(parts[2])

            return h * 3600 + m * 60 + s

        elif len(parts) == 2:

            m = float(parts[0])

            s = float(parts[1])

            return m * 60 + s

    except Exception:

        return None

    return None


def get_transcript(episode):

    url = f"{BASE}/{episode}/transcript"

    print("Fetching transcript:", episode)

    content = fetch(url)

    soup = BeautifulSoup(content, "html.parser")

    lines = []

    # TAL transcript 结构可能变化
    # 尝试带 begin 属性的段落

    for p in soup.find_all("p"):

        begin = p.get("begin")

        if not begin:

            continue

        start = parse_time(begin)

        if start is None:

            continue

        text = p.get_text(" ", strip=True)

        if text:

            lines.append((start, text))

    return lines


# =====================
# VTT generator
# =====================


def format_vtt_time(seconds):

    seconds = int(seconds)

    hour = seconds // 3600

    minute = (seconds % 3600) // 60

    second = seconds % 60

    return f"{hour:02}:" f"{minute:02}:" f"{second:02}.000"


def create_vtt(lines):

    output = ["WEBVTT", ""]

    for index, item in enumerate(lines):

        start, text = item

        if index + 1 < len(lines):

            end = lines[index + 1][0]

        else:

            end = start + 5

        output.append(f"{format_vtt_time(start)} --> " f"{format_vtt_time(end)}")

        output.append(text)

        output.append("")

    return "\n".join(output)


# =====================
# RSS generator
# =====================


def create_rss(episodes, base_url):

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>

<rss version="2.0"
xmlns:podcast="https://podcastindex.org/namespace/1.0">

<channel>

<title>
This American Life Transcript Feed
</title>


<link>
https://www.thisamericanlife.org/
</link>


<description>
Unofficial This American Life feed with VTT transcripts
</description>

"""

    for episode in sorted(episodes.keys(), key=lambda x: int(x), reverse=True):

        item = episodes[episode]

        rss += f"""

<item>

<title>
{html.escape(item.get("title",""))}
</title>


<podcast:transcript

url="{html.escape(base_url + "/transcripts/" + episode + ".vtt", quote=True)}"

type="text/vtt"

language="en"

/>


<description>
{html.escape(item.get("description",""))}
</description>


<pubDate>
{item.get("pubDate","")}
</pubDate>


<enclosure

url="{html.escape(item.get("audio",""), quote=True)}"

type="audio/mpeg"

/>


<podcast:transcript

url="{base_url}/transcripts/{episode}.vtt"

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


# =====================
# Main
# =====================


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--base-url", required=True)

    parser.add_argument("--output", default="public")

    args = parser.parse_args()

    output = Path(args.output)

    output.mkdir(parents=True, exist_ok=True)

    transcript_dir = output / "transcripts"

    transcript_dir.mkdir(parents=True, exist_ok=True)

    cache_file = output / "episodes.json"

    episodes_cache = load_json(cache_file, {})

    print("Cached episodes:", len(episodes_cache))

    archive = get_archive()
    archive = dict(
    list(archive.items())[:5]
)

    print("Archive episodes:", len(archive))

    new_count = 0

    for episode, url in archive.items():

        if episode in episodes_cache:

            continue

        print("New episode:", episode)

        try:

            info = get_episode_info(url)

            if not info["audio"]:

                print("No audio:", episode)

                continue

            vtt_file = transcript_dir / f"{episode}.vtt"

            transcript = get_transcript(episode)

            if transcript:

                vtt_file.write_text(create_vtt(transcript), encoding="utf-8")

            episodes_cache[episode] = info

            new_count += 1

        except Exception as e:

            print("Failed:", episode, e)

        time.sleep(random.uniform(5, 10))

    print("New episodes added:", new_count)

    save_json(cache_file, episodes_cache)

    rss = create_rss(episodes_cache, args.base_url)

    rss_file = output / "podcast.xml"

    rss_file.write_text(rss, encoding="utf-8")

    print("RSS generated:", rss_file)


if __name__ == "__main__":

    main()
