#!/usr/bin/env python3
"""package_release.py — 打包本地发布 zip（不推送、不发 release）。

把 dist/ 下的 TuxunHelper-*.exe 与 .env.example 打成
release/TuxunHelper-win64.zip，供用户本地测试/预览。
"""

from __future__ import annotations

import glob
import os
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
OUT_DIR = os.path.join(BASE, "release")
OUT = os.path.join(OUT_DIR, "TuxunHelper-win64.zip")


def main() -> int:
    exes = sorted(glob.glob(os.path.join(DIST, "TuxunHelper-*.exe")))
    if not exes:
        print("错误：dist/ 下没有 TuxunHelper-*.exe，请先运行 build_release.bat")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for path in exes:
            z.write(path, os.path.basename(path))
            print(f"  + {os.path.basename(path)} ({os.path.getsize(path) / 1e6:.1f} MB)")
        env_example = os.path.join(BASE, ".env.example")
        if os.path.isfile(env_example):
            z.write(env_example, ".env.example")
            print("  + .env.example")
    print(f"完成: {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
