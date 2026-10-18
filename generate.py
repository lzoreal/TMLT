#!/usr/bin/env python3

import argparse
import html
import json
import random
import re
import time
import os
import tempfile

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.thisamericanlife.org"
FIRST_YEAR = 1995

# 归档总集数低于历史缓存的这个比例时，中止更新。
ARCHIVE_MIN_RATIO = 0.90

# 连续失败时的重试次数。
REQUEST_RETRIES = 5

# 每次请求之间的等待时间。
MIN_DELAY = 2
MAX_DELAY = 5


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
            "text/html,application/xhtml+xml," "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/",
    }
)


# ============================================================
# HTTP Request
# ============================================================


def fetch(url, retries=REQUEST_RETRIES):
    """
    请求网页。
    对 429、网络错误及 HTTP 错误进行重试。
    """

    last_error = None

    for attempt in range(retries):
        try:
            print(f"Fetching: {url}")

            response = session.get(
                url,
                timeout=30,
            )

            if response.status_code == 429:
                wait = 30 * (attempt + 1)

                print(f"Rate limited: {url}")
                print(f"Sleep {wait}s")

                time.sleep(wait)
                continue

            response.raise_for_status()

            return response.text

        except Exception as exc:
            last_error = exc

            if attempt == retries - 1:
                break

            wait = 10 + attempt * 10

            print(f"Request failed: {exc}")
            print(f"Sleep {wait}s")

            time.sleep(wait)

    raise RuntimeError(f"Unable to fetch {url}: {last_error}")


# ============================================================
# Atomic file writing
# ============================================================


def atomic_write_text(path, content, encoding="utf-8"):
    """
    先写入临时文件，再原子替换目标文件。
    尽量避免进程中断造成文件写入一半。
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

    temp_path = Path(temp_name)

    try:
        with os.fdopen(
            fd,
            "w",
            encoding=encoding,
            newline="",
        ) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        temp_path.replace(path)

    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

        raise


# ============================================================
# JSON Cache
# ============================================================


def load_json(path, default):
    """
    读取 JSON。
    如果缓存损坏，不自动覆盖损坏文件。
    """

    if not path.exists():
        return default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Cache root must be a JSON object")

        return data

    except Exception as exc:
        raise RuntimeError(f"Cache cannot be read safely: {path}: {exc}") from exc


def save_json(path, data):
    content = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    )

    atomic_write_text(path, content)


# ============================================================
# Resolve audio URL
# ============================================================


def resolve_audio_url(url):
    """
    解析包装后的音频 URL。

    如果 URL 中存在 /s/，取其后面的地址。
    不限制音频服务商域名。

    注意：
    这里保留原脚本的 /s/ 解析规则。
    """

    if not url:
        return url

    marker = "/s/"

    if marker not in url:
        print("ℹ️ 未找到 /s/，保留原 audio")
        return url

    _, rest = url.split(marker, 1)

    if not rest:
        print("⚠️ /s/ 后面没有内容，保留原 audio")
        return url

    # 避免意外拼接出重复的协议头。
    if rest.startswith("https://") or rest.startswith("http://"):
        resolved = rest
    else:
        resolved = "https://" + rest

    print("🔎 解析原始 audio:")
    print(f"   {url}")

    print("✅ 解析后的 audio:")
    print(f"   {resolved}")

    return resolved


# ============================================================
# Archive crawler: 1995 -> current year
# ============================================================


def get_archive():
    """
    按年份遍历官网归档：

        /archive?year=1995
        /archive?year=1996
        ...
        /archive?year=当前年份

    不依赖 a.pager。

    如果任何年份请求失败或返回空结果，
    抛出异常，阻止本次 RSS 更新。
    """

    episodes = {}
    failed_years = []

    current_year = datetime.now().year

    for year in range(FIRST_YEAR, current_year + 1):
        archive_url = f"{BASE}/archive?year={year}"

        print("\n" + "=" * 70)
        print(f"Fetching archive year: {year}")
        print(f"URL: {archive_url}")

        try:
            content = fetch(archive_url)

            soup = BeautifulSoup(
                content,
                "html.parser",
            )

            year_episodes = {}

            for anchor in soup.select("a.goto-episode"):
                href = anchor.get("href", "").strip()

                if not href:
                    continue

                # 只接受节目页面路径，例如 /123/episode-title
                match = re.search(
                    r"^/(\d+)/",
                    href,
                )

                if not match:
                    continue

                episode = match.group(1)

                episode_url = urljoin(
                    BASE + "/",
                    href,
                )

                year_episodes[episode] = episode_url

            print(f"Year {year}: " f"found {len(year_episodes)} episodes")

            if not year_episodes:
                print(f"⚠️ Year {year} returned no episodes")

                failed_years.append(year)

            else:
                episodes.update(year_episodes)

        except Exception as exc:
            print(f"❌ Failed to fetch year {year}: {exc}")

            failed_years.append(year)

        time.sleep(
            random.uniform(
                MIN_DELAY,
                MAX_DELAY,
            )
        )

    print("\n" + "=" * 70)
    print(f"Total unique archive episodes: {len(episodes)}")

    if failed_years:
        raise RuntimeError(
            "Archive crawl incomplete. "
            f"Failed or empty years: {failed_years}. "
            "Aborting before modifying RSS or cache."
        )

    if not episodes:
        raise RuntimeError(
            "Archive returned zero episodes. " "Aborting to protect existing data."
        )

    return episodes


# ============================================================
# Episode parser
# ============================================================


def get_episode_info(url):
    print("Fetching episode:", url)

    content = fetch(url)

    soup = BeautifulSoup(
        content,
        "html.parser",
    )

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

    title = ""

    h1 = soup.find("h1")

    if h1:
        title = h1.get_text(
            " ",
            strip=True,
        )

    # --------------------------------------------------------
    # Audio
    # --------------------------------------------------------

    audio = ""

    playlist = soup.select_one("script#playlist-data")

    if playlist:
        playlist_text = playlist.string or playlist.get_text()

        if playlist_text.strip():
            try:
                data = json.loads(playlist_text)

                audio = data.get(
                    "audio",
                    "",
                )

                title = data.get(
                    "title",
                    title,
                )

            except Exception as exc:
                print(
                    "⚠️ playlist-data JSON parse failed:",
                    exc,
                )

    # --------------------------------------------------------
    # Publication date
    # --------------------------------------------------------

    pub_date = ""

    meta = soup.select_one("meta[property='article:published_time']")

    if meta:
        pub_date = meta.get(
            "content",
            "",
        )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    description = ""

    desc = soup.select_one("meta[name='description']")

    if desc:
        description = desc.get(
            "content",
            "",
        )

    # --------------------------------------------------------
    # Episode image
    # --------------------------------------------------------

    image = ""

    episode_img = soup.select_one("figure.tal-episode-image img")

    if episode_img:
        image = episode_img.get(
            "src",
            "",
        )

    if not image:
        og_image = soup.select_one("meta[property='og:image']")

        if og_image:
            image = og_image.get(
                "content",
                "",
            )

    if image:
        image = urljoin(
            BASE + "/",
            image,
        )

    if audio:
        audio = urljoin(
            BASE + "/",
            audio,
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
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])

            return hours * 3600 + minutes * 60 + seconds

        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])

            return minutes * 60 + seconds

    except (TypeError, ValueError):
        return None

    return None


def get_transcript(episode):
    """
    从官网抓取某集的英文 Transcript。
    """

    url = f"{BASE}/{episode}/transcript"

    print("Fetching transcript:", episode)

    content = fetch(url)

    soup = BeautifulSoup(
        content,
        "html.parser",
    )

    lines = []

    for paragraph in soup.select("p[begin]"):
        begin = paragraph.get("begin")

        start = parse_time(begin)

        if start is None:
            continue

        # ----------------------------------------------------
        # Speaker
        # ----------------------------------------------------

        speaker = ""

        parent = paragraph

        while parent and getattr(parent, "name", None):
            heading = parent.find("h4")

            if heading:
                speaker = heading.get_text(
                    " ",
                    strip=True,
                )
                break

            parent = parent.parent

        # ----------------------------------------------------
        # Text
        # ----------------------------------------------------

        text = paragraph.get_text(
            " ",
            strip=True,
        )

        if text:
            lines.append(
                {
                    "start": start,
                    "speaker": speaker,
                    "text": text,
                }
            )

    # 排序，避免网站内容顺序异常导致时间轴倒退。
    lines.sort(key=lambda item: item["start"])

    # 同一时间戳的字幕可能存在，但不在这里擅自合并文字。
    return lines


# ============================================================
# VTT generator
# ============================================================


def format_vtt_time(seconds):
    milliseconds = int(round(seconds * 1000))

    milliseconds = max(
        0,
        milliseconds,
    )

    hour = milliseconds // 3600000
    milliseconds %= 3600000

    minute = milliseconds // 60000
    milliseconds %= 60000

    second = milliseconds // 1000
    ms = milliseconds % 1000

    return f"{hour:02}:" f"{minute:02}:" f"{second:02}." f"{ms:03}"


def create_vtt(lines):
    """
    根据字幕开始时间生成 VTT。
    对重复或倒退时间戳进行基本保护。
    """

    output = [
        "WEBVTT",
        "",
    ]

    for index, item in enumerate(lines):
        start = max(
            0.0,
            float(item["start"]),
        )

        text = item["text"]
        speaker = item.get(
            "speaker",
            "",
        )

        if index + 1 < len(lines):
            next_start = float(lines[index + 1]["start"])

            # 时间戳重复或倒退时，至少显示 0.5 秒。
            end = max(
                next_start,
                start + 0.5,
            )

        else:
            end = start + 5

        start_text = format_vtt_time(start)
        end_text = format_vtt_time(end)

        output.append(f"{start_text} --> {end_text}")

        # 转义说话人标记中的特殊字符，避免破坏 VTT 标签。
        if speaker:
            safe_speaker = re.sub(
                r"[\r\n<>]",
                "",
                speaker,
            )

            if safe_speaker:
                output.append(f"<v {safe_speaker}>{text}")
            else:
                output.append(text)
        else:
            output.append(text)

        output.append("")

    return "\n".join(output)


# ============================================================
# VTT validation
# ============================================================


def is_valid_vtt(path):
    """
    判断本地 VTT 是否有效。

    检查：
    1. 文件存在。
    2. 文件非空。
    3. 文件以 WEBVTT 开头。
    4. 至少有一条合法的字幕时间轴。

    有效文件会直接复用，不重新请求官网 Transcript。
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

        if not re.search(
            timestamp_pattern,
            content,
        ):
            return False

        return True

    except (OSError, UnicodeError):
        return False


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
                "+00:00",
            )
        )

        return format_datetime(dt)

    except Exception:
        return date_string


# ============================================================
# Migrate / resolve old cache
# ============================================================


def update_resolved_audio(episodes_cache):
    """
    检查历史 episodes.json 中的 resolved_audio。

    没有 resolved_audio 时，重新解析。
    resolved_audio 与 audio 相同时，重新解析。
    已经不同则认为已经处理过。
    """

    print("\n" + "=" * 70)
    print("检查历史 episode 的 resolved_audio...")

    changed = 0

    for episode in sorted(
        episodes_cache.keys(),
        key=lambda value: (int(value) if str(value).isdigit() else 0),
        reverse=True,
    ):
        item = episodes_cache[episode]

        if not isinstance(item, dict):
            print(f"[{episode}] ⚠️ 缓存格式错误，跳过")
            continue

        audio = item.get(
            "audio",
            "",
        )

        if not audio:
            print(f"[{episode}] ⚠️ 没有 audio")
            continue

        resolved_audio = item.get("resolved_audio")

        if not resolved_audio or resolved_audio == audio:
            print(f"[{episode}] 🔄 解析 audio")

            new_resolved = resolve_audio_url(audio)

            if new_resolved != resolved_audio:
                item["resolved_audio"] = new_resolved
                changed += 1

        else:
            print(f"[{episode}] ✅ resolved_audio 已存在")

    print(f"历史缓存更新: {changed} 条")

    return changed


# ============================================================
# RSS generator
# ============================================================


def create_rss(episodes, base_url):
    """
    生成 RSS 2.0。
    最新 episode 在前。
    """

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
xmlns:atom="http://www.w3.org/2005/Atom"
xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
xmlns:podcast="https://podcastindex.org/namespace/1.0">

<channel>

<title>This American Life Transcript Feed</title>

<itunes:image
href="https://www.thisamericanlife.org/sites/all/themes/thislife/img/tal-logo-3000x3000.png"
/>

<podcast:image
href="https://www.thisamericanlife.org/sites/all/themes/thislife/img/tal-logo-3000x3000.png"
/>

<link>https://www.thisamericanlife.org/</link>

<atom:link
href="{html.escape(base_url.rstrip('/') + '/podcast.xml', quote=True)}"
rel="self"
type="application/rss+xml"/>

<description>Unofficial This American Life feed with VTT transcripts</description>

<language>en-us</language>

<generator>TMLT Transcript Generator</generator>

"""

    for episode in sorted(
        episodes.keys(),
        key=lambda value: (int(value) if str(value).isdigit() else 0),
        reverse=True,
    ):
        item = episodes[episode]

        if not isinstance(item, dict):
            continue

        title = html.escape(
            str(item.get("title", "")),
        )

        episode_url = html.escape(
            str(item.get("url", "")),
            quote=True,
        )

        description = html.escape(
            str(item.get("description", "")),
        )

        image = html.escape(
            str(item.get("image", "")),
            quote=True,
        )

        pub_date = rss_date(
            item.get("pubDate", ""),
        )

        resolved_audio = item.get("resolved_audio") or item.get("audio", "")

        resolved_audio = html.escape(
            str(resolved_audio),
            quote=True,
        )

        transcript_url = html.escape(
            base_url.rstrip("/") + f"/transcripts/{episode}.vtt",
            quote=True,
        )

        rss += f"""
<item>

<title>{title}</title>

<link>{episode_url}</link>

<guid isPermaLink="true">{episode_url}</guid>

<description>{description}</description>

<itunes:image href="{image}"/>

<podcast:image href="{image}"/>

<pubDate>{html.escape(pub_date)}</pubDate>

<enclosure
url="{resolved_audio}"
type="audio/mpeg"/>

<podcast:transcript
url="{transcript_url}"
type="text/vtt"
language="en"/>

</item>

"""

    rss += """
</channel>

</rss>
"""

    # 验证生成结果必须是可解析的 XML。
    import xml.etree.ElementTree as ET

    ET.fromstring(rss)

    return rss


# ============================================================
# Main
# ============================================================


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--base-url",
        required=True,
        help="网站公开基础 URL，例如 https://example.github.io/MyTranscript",
    )

    parser.add_argument(
        "--output",
        default="public",
    )

    args = parser.parse_args()

    output = Path(args.output)

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    transcript_dir = output / "transcripts"

    transcript_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file = output / "episodes.json"
    rss_file = output / "podcast.xml"

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
    # Crawl archive by year
    # ========================================================

    # get_archive() 失败时会抛出异常。
    # 此时不会覆盖缓存和 RSS。
    archive = get_archive()

    print(
        "Archive episodes:",
        len(archive),
    )

    # --------------------------------------------------------
    # Protection 1: sudden archive reduction
    # --------------------------------------------------------

    if episodes_cache:
        old_count = len(episodes_cache)
        new_count = len(archive)

        if new_count < old_count * ARCHIVE_MIN_RATIO:
            raise RuntimeError(
                "Archive episode count dropped unexpectedly: "
                f"archive={new_count}, "
                f"cache={old_count}. "
                "Aborting to protect existing RSS and cache."
            )

    # --------------------------------------------------------
    # Protection 2: retain cached episodes
    # --------------------------------------------------------

    all_episode_urls = {}

    for episode, item in episodes_cache.items():
        if not isinstance(item, dict):
            continue

        cached_url = item.get("url")

        if cached_url:
            all_episode_urls[episode] = cached_url

    # 新归档中的 URL 优先。
    all_episode_urls.update(archive)

    print(
        "Episodes to retain/process:",
        len(all_episode_urls),
    )

    # ========================================================
    # Process episodes and local VTT
    # ========================================================

    new_count = 0
    transcript_created = 0
    transcript_reused = 0
    transcript_failed = 0

    for episode, url in sorted(
        all_episode_urls.items(),
        key=lambda pair: (int(pair[0]) if str(pair[0]).isdigit() else 0),
        reverse=True,
    ):
        print("\n" + "=" * 70)
        print("Processing episode:", episode)

        vtt_file = transcript_dir / f"{episode}.vtt"

        cached = episodes_cache.get(episode)

        if not isinstance(cached, dict):
            cached = None

        # ----------------------------------------------------
        # 1. Existing valid VTT
        # ----------------------------------------------------

        vtt_exists = is_valid_vtt(vtt_file)

        if vtt_exists:
            print("✅ Local VTT exists; skip transcript download")

            transcript_reused += 1

            # 有缓存时，节目元数据也无需重新抓取。
            if cached:
                continue

        # ----------------------------------------------------
        # 2. Get episode metadata
        # ----------------------------------------------------

        try:
            if cached:
                info = cached

                if not info.get("url"):
                    info["url"] = url

            else:
                info = get_episode_info(url)

                if not info.get("audio"):
                    print(
                        "❌ No audio:",
                        episode,
                    )
                    continue

                info["resolved_audio"] = resolve_audio_url(info["audio"])

                episodes_cache[episode] = info
                new_count += 1

            # ------------------------------------------------
            # 3. Fetch transcript only when VTT is missing
            #    or invalid
            # ------------------------------------------------

            if not is_valid_vtt(vtt_file):
                print("🔎 Local VTT missing or invalid; " "fetching transcript")

                try:
                    transcript = get_transcript(episode)

                except Exception as exc:
                    print(f"❌ Transcript request failed " f"for {episode}: {exc}")

                    transcript_failed += 1
                    continue

                if not transcript:
                    print(
                        "⚠️ No transcript:",
                        episode,
                    )

                    transcript_failed += 1
                    continue

                content = create_vtt(transcript)

                # 先生成临时文件，验证通过后再替换。
                temp_file = vtt_file.with_name(vtt_file.name + ".tmp")

                try:
                    temp_file.write_text(
                        content,
                        encoding="utf-8",
                    )

                    if not is_valid_vtt(temp_file):
                        raise ValueError("Generated VTT failed validation")

                    temp_file.replace(vtt_file)

                    print(
                        "✅ VTT generated:",
                        vtt_file,
                    )

                    transcript_created += 1

                except Exception:
                    temp_file.unlink(missing_ok=True)
                    raise

            # ------------------------------------------------
            # 4. Resolve missing audio URL
            # ------------------------------------------------

            if info.get("audio") and not info.get("resolved_audio"):
                info["resolved_audio"] = resolve_audio_url(info["audio"])

            episodes_cache[episode] = info

        except Exception as exc:
            print(f"❌ Failed episode {episode}: {exc}")

            # 保留已有缓存，继续处理其他集。
            continue

        time.sleep(
            random.uniform(
                MIN_DELAY,
                MAX_DELAY,
            )
        )

    # ========================================================
    # Summary
    # ========================================================

    print("\n" + "=" * 70)
    print("Episode processing completed")

    print(
        "New episodes added:",
        new_count,
    )

    print(
        "Local VTT reused:",
        transcript_reused,
    )

    print(
        "VTT created:",
        transcript_created,
    )

    print(
        "Transcript failures:",
        transcript_failed,
    )

    # ========================================================
    # Update old resolved_audio cache
    # ========================================================

    update_resolved_audio(episodes_cache)

    # ========================================================
    # Save cache atomically
    # ========================================================

    save_json(
        cache_file,
        episodes_cache,
    )

    print(
        "Episodes cache saved:",
        cache_file,
    )

    # ========================================================
    # Generate and validate RSS
    # ========================================================

    rss = create_rss(
        episodes_cache,
        args.base_url,
    )

    # 先在内存中验证 XML，再原子替换旧 RSS。
    rss_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    atomic_write_text(
        rss_file,
        rss,
    )

    print(
        "RSS generated:",
        rss_file,
    )

    # ========================================================
    # Final summary
    # ========================================================

    print("\n" + "=" * 70)
    print("完成")

    print(f"Episodes: {len(episodes_cache)}")

    print(f"RSS: {rss_file}")


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":
    main()
