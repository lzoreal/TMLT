#!/usr/bin/env python3
"""准备 Pages 上传目录：排除已被中译取代的英文 VTT。

仓库里的英文 VTT 仍供翻译、哈希校验和缺中译集兜底；只有同时存在
`transcripts/zh/<集>.vtt` 的那些不再发布（feed 也不会引用）。

Usage:
    python stage_pages.py docs /tmp/tmlt-pages
"""

import shutil
import sys
from pathlib import Path


def superseded_english(docs):
    transcripts = Path(docs) / "transcripts"

    translated = {
        path.name for path in (transcripts / "zh").glob("*.vtt")
    }

    return [
        path for path in transcripts.glob("*.vtt") if path.name in translated
    ]


def stage(docs, out):
    docs = Path(docs)
    out = Path(out)

    if out.exists():
        shutil.rmtree(out)

    shutil.copytree(docs, out)

    removed = 0

    for source in superseded_english(docs):
        target = out / "transcripts" / source.name

        if target.is_file():
            target.unlink()
            removed += 1

    return removed


def main():
    docs = Path(sys.argv[1] if len(sys.argv) > 1 else "docs")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/tmlt-pages")

    removed = stage(docs, out)

    print(f"Pages staged in {out}; removed {removed} superseded English VTTs")

    return 0


if __name__ == "__main__":
    sys.exit(main())
