#!/usr/bin/env python3
"""把本地各書的 unified/images/<hash>.jpg + cover.jpg 轉成 WebP，輸出到 ../img/<slug>/。

用法：
    uv run python -m build.convert_images [slug ...]

q80（實測省 ~41%）。冪等：dst 已存在且 mtime 不舊於 src 則跳過，支援增量重跑。
"""
from __future__ import annotations

import subprocess
import sys
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
OUT = ROOT / 'img'
QUALITY = '80'
SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')


def _valid_slug(slug: str) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


def _convert(job: tuple[str, str]) -> tuple[int, str]:
    """回 (狀態, 訊息)：1=轉了 / 0=跳過(已最新) / -1=失敗。單張失敗【絕不拋】——否則
    ProcessPool.map 迭代會炸掉整個 build、留下 data/ 已烤但 img/ 半成品的撕裂狀態。"""
    src, dst = job
    sp, dp = Path(src), Path(dst)
    try:
        if dp.is_file() and dp.stat().st_mtime >= sp.stat().st_mtime:
            return 0, ''  # 已是最新，跳過
        dp.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['cwebp', '-q', QUALITY, '-quiet', src, '-o', dst],
                       check=True, capture_output=True)
        return 1, ''
    except FileNotFoundError:
        return -1, f'{src}: cwebp 未安裝（brew install webp）'
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b'').decode('utf-8', 'replace').strip()[:160]
        return -1, f'{src}: cwebp rc={e.returncode} {err}'
    except OSError as e:
        return -1, f'{src}: {e}'


def _jobs_for(slug: str) -> list[tuple[str, str]]:
    if not _valid_slug(slug):
        return []
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
    if argv:
        slugs = [slug for slug in argv if _valid_slug(slug)]
        for slug in argv:
            if not _valid_slug(slug):
                print(f'  ✗ {slug}: invalid slug（跳過）')   # 與 build_all._ensure_covers 一致：壞 token 不連坐合法書
        if not slugs:
            sys.exit(f'✗ no valid slug(s): {", ".join(argv)}')
    else:
        slugs = sorted(p.parent.parent.name
                       for p in DATA_DIR.glob('*/parsed/book.json')
                       if _valid_slug(p.parent.parent.name))
    all_jobs: list[tuple[str, str]] = []
    for slug in slugs:
        all_jobs.extend(_jobs_for(slug))
    print(f'{len(slugs)} book(s), {len(all_jobs)} image(s) to check')

    converted = skipped = failed = 0
    failures: list[str] = []
    with ProcessPoolExecutor() as pool:
        for i, (st, msg) in enumerate(pool.map(_convert, all_jobs, chunksize=64)):
            if st == 1:
                converted += 1
            elif st == 0:
                skipped += 1
            else:
                failed += 1
                if len(failures) < 20:
                    failures.append(msg)
            if (i + 1) % 5000 == 0:
                print(f'  {i + 1}/{len(all_jobs)} processed, {converted} converted')
    print(f'done: {converted} converted, {skipped} skipped, {failed} failed → {OUT}')
    if failures:
        print(f'⚠ {failed} 張轉檔失敗（前 {len(failures)} 筆）：')
        for m in failures:
            print('   ', m)
        # 全失敗（多半 cwebp 未安裝）= build 實質壞掉 → 非零退出讓 build_all/daemon 察覺；
        # 零星壞圖則容忍（其餘照轉、站照上，只缺幾張圖）。
        if failed == len(all_jobs) and all_jobs:
            sys.exit('✗ 全部轉檔失敗 — 多半 cwebp 未安裝（brew install webp）')


if __name__ == '__main__':
    main(sys.argv[1:])
