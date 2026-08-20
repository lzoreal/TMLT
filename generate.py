#!/usr/bin/env python3

import argparse
import html
import json
import random
import re
import time

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

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
# HTTP
# ============================================================

def fetch(url, retries=5):

    for attempt in range(retries):

        try:

            r = session.get(
                url,
                timeout=30,
                allow_redirects=True,
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
                raise

            wait = 10 + attempt * 10

            print(f"Request failed: {e}")
            print(f"Sleep {wait}s")

            time.sleep(wait)

    raise Exception(
        "Unable to fetch " + url
    )


# ============================================================
# JSON
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
            f"Warning: failed to load "
            f"{path}: {e}"
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
# URL / Audio helpers
# ============================================================

AUDIO_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".flac",
    ".mp4",
)


def clean_url(url):

    if not url:
        return ""

    url = html.unescape(url)

    url = url.replace(
        "\\/",
        "/",
    )

    url = url.strip()

    # 去掉 JSON / HTML / XML 周围可能存在的字符
    url = url.strip(
        "\"'<>[](){}"
    )

    return url


def path_looks_like_audio(url):

    """
    不检查域名，只看 URL path。

    例如：

    https://abc.example.com/foo/bar.mp3
    """

    if not url:
        return False

    try:

        parsed = urlparse(url)

        path = unquote(
            parsed.path
        ).lower()

        return path.endswith(
            AUDIO_EXTENSIONS
        )

    except Exception:

        return False


def is_http_url(url):

    if not url:
        return False

    try:

        parsed = urlparse(url)

        return (
            parsed.scheme.lower()
            in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:

        return False


def extract_nested_urls(text):

    """
    从一个包装 URL 中提取所有嵌套的
    http:// / https:// URL。

    不限制域名。

    例如：

    https://wrapper.com/a/
    https://foo.example.com/file.mp3

    会找到：

    https://wrapper.com/a/...
    https://foo.example.com/file.mp3
    """

    if not text:
        return []

    text = html.unescape(
        text
    )

    text = text.replace(
        "\\/",
        "/",
    )

    pattern = re.compile(
        r"https?://[^\s\"'<>]+",
        re.IGNORECASE,
    )

    matches = pattern.findall(
        text
    )

    result = []

    for item in matches:

        item = clean_url(item)

        if item and item not in result:

            result.append(item)

    return result


def extract_host_path_candidates(url):

    """
    不依赖域名白名单。

    从：

    /foo/bar/audio.example.com/path/file.mp3

    这种 URL 中寻找：

    audio.example.com/path/file.mp3

    并转换成：

    https://audio.example.com/path/file.mp3

    """

    if not url:
        return []

    text = html.unescape(
        url
    )

    text = text.replace(
        "\\/",
        "/",
    )

    candidates = []

    # --------------------------------------------------------
    # 找类似：
    #
    # xxx.example.com/path/file.mp3
    #
    # 这里不规定具体域名。
    # --------------------------------------------------------

    pattern = re.compile(
        r"(?<![/A-Za-z0-9._-])"
        r"([A-Za-z0-9][A-Za-z0-9.-]*"
        r"\.[A-Za-z]{2,}"
        r"/[^?\s\"'<>]+"
        r"(?:\?[^?\s\"'<>]*)?)",
        re.IGNORECASE,
    )

    for match in pattern.findall(
        text
    ):

        candidate = (
            "https://"
            + match
        )

        candidate = clean_url(
            candidate
        )

        if candidate not in candidates:

            candidates.append(
                candidate
            )

    return candidates


def score_audio_candidate(url):

    """
    给候选 URL 打分。

    不使用任何域名白名单。

    只是判断这个 URL 看起来是不是
    更像最终音频地址。
    """

    if not url:
        return -999999

    score = 0

    try:

        parsed = urlparse(url)

        if parsed.scheme in (
            "http",
            "https",
        ):
            score += 10

        if parsed.netloc:
            score += 10

        path = unquote(
            parsed.path
        ).lower()

        # 真正的音频扩展名
        for ext in AUDIO_EXTENSIONS:

            if path.endswith(ext):

                score += 100
                break

        # URL 中出现 audio
        if "/audio/" in path:
            score += 20

        if "episode" in path:
            score += 5

        if "redirect" in path:
            score -= 20

        if "tracking" in path:
            score -= 20

        if "track" in path:
            score -= 10

        if "redirect.mp" in path:
            score -= 30

        if "pdst.fm" in parsed.netloc.lower():
            score -= 30

        # 包装 URL 往往 path 很长
        # 但最终音频也可能很长，所以这里只做轻微扣分
        if len(path) > 300:
            score -= 5

    except Exception:

        return -999999

    return score


def resolve_audio_url(audio_url):

    """
    通用解析器。

    不指定任何域名。

    思路：

    1. 如果已经是普通音频 URL，直接返回。
    2. URL decode / HTML decode。
    3. 从包装 URL 中寻找嵌套 http(s) URL。
    4. 从路径中寻找 xxx.example.com/path/file.mp3。
    5. 从所有候选中选择最像最终音频地址的一个。
    """

    original = clean_url(
        audio_url
    )

    if not original:
        return ""

    print(
        "   🔎 解析原始 audio:"
    )

    print(
        f"      {original}"
    )

    # ========================================================
    # 第一层：URL decode
    # ========================================================

    decoded = original

    for _ in range(3):

        new_value = unquote(
            decoded
        )

        if new_value == decoded:
            break

        decoded = new_value

    decoded = html.unescape(
        decoded
    )

    decoded = decoded.replace(
        "\\/",
        "/",
    )

    # ========================================================
    # 如果本身已经明显是最终音频
    # ========================================================

    if (
        is_http_url(decoded)
        and path_looks_like_audio(decoded)
    ):

        print(
            "   ✅ 已经是直接音频地址"
        )

        print(
            f"      {decoded}"
        )

        return decoded

    # ========================================================
    # 候选集合
    # ========================================================

    candidates = []

    # --------------------------------------------------------
    # 1. 原始 URL
    # --------------------------------------------------------

    candidates.append(
        original
    )

    if decoded not in candidates:

        candidates.append(
            decoded
        )

    # --------------------------------------------------------
    # 2. 嵌套 http(s)
    # --------------------------------------------------------

    nested_urls = extract_nested_urls(
        decoded
    )

    for candidate in nested_urls:

        if candidate not in candidates:

            candidates.append(
                candidate
            )

    # --------------------------------------------------------
    # 3. host/path
    # --------------------------------------------------------

    host_candidates = (
        extract_host_path_candidates(
            decoded
        )
    )

    for candidate in host_candidates:

        if candidate not in candidates:

            candidates.append(
                candidate
            )

    # ========================================================
    # 4. 对候选进行多轮解包
    # ========================================================

    expanded = []

    for candidate in candidates:

        candidate = clean_url(
            candidate
        )

        if not candidate:
            continue

        if candidate not in expanded:

            expanded.append(
                candidate
            )

        # 候选本身里面还可能嵌套 URL
        nested = extract_nested_urls(
            candidate
        )

        for nested_url in nested:

            nested_url = clean_url(
                nested_url
            )

            if (
                nested_url
                and nested_url not in expanded
            ):

                expanded.append(
                    nested_url
                )

    # ========================================================
    # 5. 再从 expanded 候选里找 host/path
    # ========================================================

    more_candidates = []

    for candidate in expanded:

        for item in extract_host_path_candidates(
            candidate
        ):

            if item not in more_candidates:

                more_candidates.append(
                    item
                )

    expanded.extend(
        more_candidates
    )

    # ========================================================
    # 6. 清理
    # ========================================================

    final_candidates = []

    for candidate in expanded:

        candidate = clean_url(
            candidate
        )

        if not is_http_url(
            candidate
        ):
            continue

        if candidate not in final_candidates:

            final_candidates.append(
                candidate
            )

    # ========================================================
    # 7. 打印候选
    # ========================================================

    if final_candidates:

        print(
            "   🔍 找到候选地址:"
        )

        for candidate in final_candidates:

            print(
                "      "
                f"[{score_audio_candidate(candidate):4d}] "
                f"{candidate}"
            )

    # ========================================================
    # 8. 选择得分最高
    # ========================================================

    audio_candidates = [
        item
        for item in final_candidates
        if path_looks_like_audio(item)
    ]

    if audio_candidates:

        best = max(
            audio_candidates,
            key=score_audio_candidate,
        )

        print(
            "   🎯 选择最终音频地址:"
        )

        print(
            f"      {best}"
        )

        return best

    # ========================================================
    # 9. 没有 .mp3 等扩展名
    #
    # 仍然选择最可能的候选。
    # ========================================================

    if final_candidates:

        best = max(
            final_candidates,
            key=score_audio_candidate,
        )

        # 如果 best 不是原始包装 URL，
        # 说明至少剥掉了一层包装。
        if best != original:

            print(
                "   🎯 选择解析后的 URL:"
            )

            print(
                f"      {best}"
            )

            return best

    # ========================================================
    # 10. 无法解析
    # ========================================================

    print(
        "   ⚠️ 未能从 URL 中提取更深层地址"
    )

    print(
        "   ↩️ 保留原始 audio"
    )

    return original


# ============================================================
# Resolve cache
# ============================================================

def resolve_episode_audio(
    episode,
    info,
    force=False,
):

    original_audio = (
        info.get(
            "audio",
            "",
        )
        or ""
    ).strip()

    cached_resolved = (
        info.get(
            "resolved_audio",
            "",
        )
        or ""
    ).strip()

    if not original_audio:

        print(
            f"[{episode}] ❌ 没有 audio"
        )

        return info

    # ========================================================
    # 已经成功解析
    # ========================================================

    if (
        cached_resolved
        and cached_resolved != original_audio
        and not force
    ):

        print(
            f"[{episode}] ♻️ "
            "使用缓存 resolved_audio"
        )

        print(
            f"   {cached_resolved}"
        )

        return info

    # ========================================================
    # 旧数据：
    #
    # resolved_audio == audio
    #
    # 不能认为成功。
    # ========================================================

    if (
        cached_resolved
        and cached_resolved == original_audio
        and not force
    ):

        print(
            f"[{episode}] ⚠️ "
            "resolved_audio == audio"
        )

        print(
            "   旧版本没有成功解析"
        )

        print(
            "   🔄 重新解析"
        )

    # ========================================================
    # force
    # ========================================================

    if force:

        print(
            f"[{episode}] 🔄 "
            "强制重新解析"
        )

    # ========================================================
    # 实际解析
    # ========================================================

    resolved = resolve_audio_url(
        original_audio
    )

    info[
        "resolved_audio"
    ] = resolved

    # ========================================================
    # 状态
    # ========================================================

    if (
        resolved
        and resolved != original_audio
    ):

        info[
            "audio_resolved"
        ] = True

        info[
            "audio_resolved_at"
        ] = (
            datetime.utcnow()
            .isoformat()
            + "Z"
        )

        print(
            f"[{episode}] ✅ "
            "解析成功"
        )

    else:

        info[
            "audio_resolved"
        ] = False

        print(
            f"[{episode}] ⚠️ "
            "没有得到新的地址"
        )

    return info


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
        "html.parser",
    )

    title = ""

    h1 = soup.find("h1")

    if h1:

        title = h1.get_text(
            " ",
            strip=True,
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
                "",
            )

            title = data.get(
                "title",
                title,
            )

        except Exception as e:

            print(
                "Playlist JSON error:",
                e,
            )

    pub_date = ""

    meta = soup.select_one(
        "meta[property='article:published_time']"
    )

    if meta:

        pub_date = meta.get(
            "content",
            "",
        )

    description = ""

    desc = soup.select_one(
        "meta[name='description']"
    )

    if desc:

        description = desc.get(
            "content",
            "",
        )

    image = ""

    episode_img = soup.select_one(
        "figure.tal-episode-image img"
    )

    if episode_img:

        image = episode_img.get(
            "src",
            "",
        )

    if not image:

        og_image = soup.select_one(
            "meta[property='og:image']"
        )

        if og_image:

            image = og_image.get(
                "content",
                "",
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
            "html.parser",
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
                href,
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
            found,
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
            random.uniform(
                5,
                10,
            )
        )

    return episodes


# ============================================================
# Transcript
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

        if len(parts) == 2:

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
        "html.parser",
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

            h4 = parent.find(
                "h4"
            )

            if h4:

                speaker = h4.get_text(
                    " ",
                    strip=True,
                )

                break

            parent = parent.parent

        text = p.get_text(
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

    return lines


# ============================================================
# VTT
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

    ms = (
        milliseconds
        % 1000
    )

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

        start = item[
            "start"
        ]

        text = item[
            "text"
        ]

        speaker = item.get(
            "speaker",
            "",
        )

        if index + 1 < len(lines):

            end = lines[
                index + 1
            ][
                "start"
            ]

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

            output.append(
                text
            )

        output.append("")

    return "\n".join(
        output
    )


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

        return format_datetime(
            dt
        )

    except Exception:

        return date_string


# ============================================================
# RSS
# ============================================================

def create_rss(
    episodes,
    base_url,
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
type="application/rss+xml"
/>

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
        reverse=True,
    ):

        item = episodes[
            episode
        ]

        title = html.escape(
            item.get(
                "title",
                "",
            )
        )

        url = html.escape(
            item.get(
                "url",
                "",
            ),
            quote=True,
        )

        description = html.escape(
            item.get(
                "description",
                "",
            )
        )

        image = html.escape(
            item.get(
                "image",
                "",
            ),
            quote=True,
        )

        pub_date = rss_date(
            item.get(
                "pubDate",
                "",
            )
        )

        # ====================================================
        # 关键：
        #
        # RSS enclosure 使用 resolved_audio
        #
        # 如果没有解析成功，就使用原始 audio。
        # ====================================================

        resolved_audio = (
            item.get(
                "resolved_audio",
                "",
            )
            or ""
        ).strip()

        original_audio = (
            item.get(
                "audio",
                "",
            )
            or ""
        ).strip()

        enclosure_audio = (
            resolved_audio
            or original_audio
        )

        enclosure_audio = html.escape(
            enclosure_audio,
            quote=True,
        )

        rss += f"""

<item>

<title>
{title}
</title>

<link>
{url}
</link>

<guid isPermaLink="true">
{url}
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
url="{enclosure_audio}"
type="audio/mpeg"
/>

<podcast:transcript
url="{html.escape(
    base_url,
    quote=True,
)}/transcripts/{episode}.vtt"
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

    parser.add_argument(
        "--force-resolve",
        action="store_true",
        help="强制重新解析所有 audio URL",
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

    episodes_cache = load_json(
        cache_file,
        {},
    )

    print(
        "Cached episodes:",
        len(episodes_cache),
    )

    # ========================================================
    # Archive
    # ========================================================

    archive = get_archive()

    print(
        "Archive episodes:",
        len(archive),
    )

    # ========================================================
    # 新 episode
    # ========================================================

    new_count = 0

    for episode, url in archive.items():

        if episode in episodes_cache:

            continue

        print()
        print(
            "=" * 70
        )

        print(
            "New episode:",
            episode,
        )

        try:

            info = get_episode_info(
                url
            )

            if not info.get(
                "audio"
            ):

                print(
                    "No audio:",
                    episode,
                )

                continue

            # ------------------------------------------------
            # 解析真实音频
            # ------------------------------------------------

            info = resolve_episode_audio(
                episode,
                info,
                force=args.force_resolve,
            )

            # ------------------------------------------------
            # Transcript
            # ------------------------------------------------

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
                    encoding="utf-8",
                )

                print(
                    "VTT saved:",
                    vtt_file,
                )

            else:

                print(
                    "⚠️ No transcript:",
                    episode,
                )

            episodes_cache[
                episode
            ] = info

            new_count += 1

        except Exception as e:

            print(
                "Failed:",
                episode,
                e,
            )

        time.sleep(
            random.uniform(
                5,
                10,
            )
        )

    print()
    print(
        "New episodes added:",
        new_count,
    )

    # ========================================================
    # 修复历史数据
    # ========================================================

    repaired_count = 0

    print()
    print(
        "=" * 70
    )

    print(
        "检查历史 episode 的 resolved_audio..."
    )

    for episode, info in episodes_cache.items():

        original_audio = (
            info.get(
                "audio",
                "",
            )
            or ""
        ).strip()

        cached_resolved = (
            info.get(
                "resolved_audio",
                "",
            )
            or ""
        ).strip()

        if not original_audio:

            print(
                f"[{episode}] ⚠️ "
                "没有原始 audio，跳过"
            )

            continue

        # ----------------------------------------------------
        # 已经成功解析过
        # ----------------------------------------------------

        if (
            cached_resolved
            and cached_resolved != original_audio
            and not args.force_resolve
        ):

            print(
                f"[{episode}] ✅ "
                "已有成功的 resolved_audio"
            )

            continue

        # ----------------------------------------------------
        # 没有 resolved_audio
        # ----------------------------------------------------

        if not cached_resolved:

            print(
                f"[{episode}] ⚠️ "
                "没有 resolved_audio"
            )

        # ----------------------------------------------------
        # resolved_audio == audio
        # ----------------------------------------------------

        elif cached_resolved == original_audio:

            print(
                f"[{episode}] ⚠️ "
                "resolved_audio == audio"
            )

            print(
                "   重新解析旧缓存"
            )

        # ----------------------------------------------------
        # force
        # ----------------------------------------------------

        if args.force_resolve:

            print(
                f"[{episode}] 🔄 "
                "force resolve"
            )

        old_resolved = (
            cached_resolved
        )

        info = resolve_episode_audio(
            episode,
            info,
            force=args.force_resolve,
        )

        new_resolved = (
            info.get(
                "resolved_audio",
                "",
            )
            or ""
        ).strip()

        episodes_cache[
            episode
        ] = info

        if new_resolved != old_resolved:

            repaired_count += 1

            print(
                f"[{episode}] 🔧 "
                "resolved_audio 已更新"
            )

    print()
    print(
        "Historical audio repaired:",
        repaired_count,
    )

    # ========================================================
    # 保存 episodes.json
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
    # 生成 RSS
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

    print()
    print(
        "RSS generated:",
        rss_file,
    )

    print(
        "Total episodes:",
        len(episodes_cache),
    )


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":

    main()
