#!/usr/bin/env python3
"""把本地各書的 unified/images/<hash>.jpg + cover.jpg 轉成 WebP，輸出到 ../img/<slug>/。

用法：
    uv run python -m build.convert_images [slug ...]

q80（實測省 ~41%）。冪等：dst 已存在且 mtime 不舊於 src 則跳過，支援增量重跑。
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
OUT = ROOT / 'img'
QUALITY = '80'


def _convert(job: tuple[str, str]) -> bool:
    src, dst = job
    sp, dp = Path(src), Path(dst)
    if dp.is_file() and dp.stat().st_mtime >= sp.stat().st_mtime:
        return False  # 已是最新，跳過
    dp.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cwebp', '-q', QUALITY, '-quiet', src, '-o', dst], check=True)
    return True


def _jobs_for(slug: str) -> list[tuple[str, str]]:
    book_dir = DATA_DIR / slug
    out_dir = OUT / slug
    jobs: list[tuple[str, str]] = []
    img_dir = book_dir / 'unified' / 'images'
    if img_dir.is_dir():
        for jpg in img_dir.glob('*.jpg'):
            jobs.append((str(jpg), str(out_dir / f'{jpg.stem}.webp')))
    cover = book_dir / 'cover.jpg'
    if cover.is_file():
        jobs.append((str(cover), str(out_dir / 'cover.webp')))
    return jobs


def main(argv: list[str]) -> None:
    slugs = argv or sorted(p.parent.parent.name
                           for p in DATA_DIR.glob('*/parsed/book.json'))
    all_jobs: list[tuple[str, str]] = []
    for slug in slugs:
        all_jobs.extend(_jobs_for(slug))
    print(f'{len(slugs)} book(s), {len(all_jobs)} image(s) to check')

    converted = 0
    with ProcessPoolExecutor() as pool:
        for i, did in enumerate(pool.map(_convert, all_jobs, chunksize=64)):
            converted += int(did)
            if (i + 1) % 5000 == 0:
                print(f'  {i + 1}/{len(all_jobs)} processed, {converted} converted')
    print(f'done: {converted} converted, {len(all_jobs) - converted} skipped → {OUT}')


if __name__ == '__main__':
    main(sys.argv[1:])
