#!/usr/bin/env python3
import argparse
import html
import json
import re
import time
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

TAL_RSS = "https://feeds.thisamericanlife.org/talpodcast"

session = requests.Session()
session.headers.update({
    "User-Agent": "TAL-Podcast20-Transcript-RSS/1.0"
})

def get(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def episode_number(entry):
    text = " ".join([
        str(entry.get("title", "")),
        str(entry.get("link", "")),
        str(entry.get("id", "")),
    ])
    m = re.search(r"\b(\d{1,4})\b", text)
    return int(m.group(1)) if m else None

def transcript_url(ep):
    return f"https://www.thisamericanlife.org/{ep}/transcript"

def clean_text(value):
    s = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def parse_clock(value):
    x = str(value).strip().replace(",", ".")
    parts = x.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None

def fmt_vtt(sec):
    total = int(sec)
    ms = int(round((sec - total) * 1000))
    if ms >= 1000:
        total += 1
        ms -= 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def extract_from_embedded_json(soup):
    out = []

    def walk(x):
        if isinstance(x, dict):
            lower = {str(k).lower(): k for k in x}
            start = next((x[k] for k in lower if k in ("start", "starttime", "start_time", "begin")), None)
            end = next((x[k] for k in lower if k in ("end", "endtime", "end_time", "stop")), None)
            text = next((x[k] for k in lower if k in ("text", "body", "transcript", "content")), None)
            if start is not None and end is not None and text:
                try:
                    st = float(start)
                    en = float(end)
                    txt = clean_text(text)
                    if en > st and txt:
                        out.append((st, en, txt))
                except Exception:
                    pass
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for tag in soup.find_all("script"):
        raw = tag.string or tag.get_text()
        if not raw or len(raw) > 5_000_000:
            continue
        raw = raw.strip()
        if not (raw.startswith("{") or raw.startswith("[")):
            continue
        try:
            walk(json.loads(raw))
        except Exception:
            continue

    return out

def extract_timestamped_html(soup):
    out = []
    for el in soup.find_all(True):
        attrs = {str(k).lower(): v for k, v in el.attrs.items()}
        start = next((attrs[k] for k in attrs if k in (
            "data-start", "data-start-time", "data-starttime", "start", "starttime"
        )), None)
        end = next((attrs[k] for k in attrs if k in (
            "data-end", "data-end-time", "data-endtime", "end", "endtime"
        )), None)
        if start is None or end is None:
            continue
        st = parse_clock(start)
        en = parse_clock(end)
        txt = clean_text(el)
        if st is not None and en is not None and en > st and txt:
            out.append((st, en, txt))
    return dedupe(out)

def dedupe(items):
    seen = set()
    result = []
    for st, en, txt in sorted(items, key=lambda x: x[0]):
        key = (round(st, 3), round(en, 3), txt)
        if key not in seen:
            seen.add(key)
            result.append((st, en, txt))
    return result

def make_vtt(segments):
    lines = ["WEBVTT", ""]
    for st, en, text in segments:
        lines += [f"{fmt_vtt(st)} --> {fmt_vtt(en)}", text, ""]
    return "\n".join(lines)

def esc(s):
    return html.escape(str(s), quote=True)

def build_rss(entries, base_url, available):
    items = []
    for e in entries:
        ep = episode_number(e)
        if not ep or ep not in available:
            continue
        enclosures = e.get("enclosures", [])
        if not enclosures:
            continue
        audio = enclosures[0]
        title = e.get("title", f"This American Life #{ep}")
        link = e.get("link", f"https://www.thisamericanlife.org/{ep}")
        desc = e.get("summary", e.get("description", ""))
        guid = e.get("id", link)
        vtt_url = f"{base_url.rstrip('/')}/transcripts/{ep}.vtt"
        items.append(f"""    <item>
      <title>{esc(title)}</title>
      <link>{esc(link)}</link>
      <guid isPermaLink="false">{esc(guid)}</guid>
      <pubDate>{esc(e.get("published", ""))}</pubDate>
      <description>{esc(desc)}</description>
      <enclosure url="{esc(audio.get("href", ""))}" length="{esc(audio.get("length", "0"))}" type="{esc(audio.get("type", "audio/mpeg"))}" />
      <podcast:transcript url="{esc(vtt_url)}" type="text/vtt" language="en" rel="captions" />
    </item>""")

    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>This American Life — Podcasting 2.0 Transcript Mirror</title>
    <link>https://www.thisamericanlife.org/</link>
    <description>Unofficial RSS mirror adding Podcasting 2.0 WebVTT transcript links.</description>
    <language>en-us</language>
""" + "\n".join(items) + """
  </channel>
</rss>
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--output", default="public")
    ap.add_argument("--sleep", type=float, default=0.8)
    args = ap.parse_args()

    out = Path(args.output)
    (out / "transcripts").mkdir(parents=True, exist_ok=True)

    feed = feedparser.parse(TAL_RSS)
    entries = feed.entries[:args.episodes]
    available = {}

    for entry in entries:
        ep = episode_number(entry)
        if not ep:
            continue
        print(f"[{ep}] fetching {transcript_url(ep)}")
        try:
            soup = BeautifulSoup(get(transcript_url(ep)), "lxml")
            segments = extract_from_embedded_json(soup)
            if not segments:
                segments = extract_timestamped_html(soup)
            segments = dedupe(segments)
            if not segments:
                print("  SKIP: no real timestamped data found")
                continue

            (out / "transcripts" / f"{ep}.vtt").write_text(
                make_vtt(segments), encoding="utf-8"
            )
            available[ep] = True
            print(f"  OK: {len(segments)} cues")
        except Exception as ex:
            print(f"  ERROR: {ex}")
        time.sleep(args.sleep)

    (out / "podcast.xml").write_text(
        build_rss(entries, args.base_url, available), encoding="utf-8"
    )
    print(f"Generated {len(available)} VTT files.")

if __name__ == "__main__":
    main()
