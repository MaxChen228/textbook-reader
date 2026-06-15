#!/usr/bin/env python3
"""drive_queue — 本機 ↔ Drive 中轉佇列，餵雲端 ingest skill。

動機：MinerU quota 綁帳號（1000 頁/帳號/日），雲端跑不增額度；但雲端 Claude Code
session 可 7×24 掛著、跨自然日重置慢慢消化超額，且不佔本機。MinerU 是中國服務，
不能直抓 Google Drive（國外 URL 超時），故 Drive 只當「本機 → 雲端」的 PDF 中轉：
雲端下載 PDF 後仍走「切片 + PUT 到 MinerU 自家 OSS」這條已驗證路徑。

佇列規則：Drive 上的 PDF 一律以 <slug>.pdf 命名（slug 是 ingest 主鍵），雲端零歧義。
本機上傳前查 slug_map.json 把雜訊檔名 rename 成 slug。

用法：
  # 把本機 raw_pdfs 裡「repo 尚無 unified」的書上傳 Drive 佇列
  python -m book_pipeline.drive_queue push [--src raw_pdfs] [--remote gdrive:qbank_ingest_queue]
        [--all]   也上傳已完成的（重跑用）
        [--dry-run]
  # 列佇列內容 vs repo 完成狀態
  python -m book_pipeline.drive_queue status [--src raw_pdfs] [--remote gdrive:qbank_ingest_queue]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'book_pipeline' / 'mineru_data'
SLUG_MAP_PATH = ROOT / 'book_pipeline' / 'slug_map.json'
DEFAULT_REMOTE = 'gdrive:qbank_ingest_queue'        # PDF 待 ingest 佇列
DEFAULT_DATA_REMOTE = 'gdrive:qbank_data_backup'    # unified/ 機器產物備份（per-book tar）
DEFAULT_SRC = ROOT / 'raw_pdfs'


def load_slug_map() -> dict[str, str]:
    if not SLUG_MAP_PATH.exists():
        return {}
    return json.loads(SLUG_MAP_PATH.read_text()).get('map', {})


def mechanical_slug(name: str) -> str:
    """檔名查無對照時的機械 fallback（去 z-library 雜訊、_solutions→_sol）。"""
    s = name.lower().removesuffix('.pdf')
    s = re.sub(r'\s*\(.*?z-?lib.*?\)', '', s)
    s = re.sub(r'_solutions$', '_sol', s)
    s = re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s


def slug_for(name: str, smap: dict[str, str]) -> tuple[str, bool]:
    """回傳 (slug, mapped)。mapped=False 表示走 fallback（建議補 slug_map）。"""
    if name in smap:
        return smap[name], True
    return mechanical_slug(name), False


def is_done(slug: str) -> bool:
    """完成的唯一真相：repo 有 unified/content_list.json。"""
    return (DATA / slug / 'unified' / 'content_list.json').exists()


def rclone(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(['rclone', *args], check=True,
                          text=True, capture_output=capture)


def queue_slugs(remote: str) -> set[str]:
    """佇列裡已有的 slug（<slug>.pdf 的 stem）。remote 不存在 → 空 set。"""
    try:
        out = rclone(['lsf', remote], capture=True).stdout
    except subprocess.CalledProcessError:
        return set()
    return {Path(line).stem for line in out.splitlines()
            if line.strip().endswith('.pdf')}


def iter_pdfs(src: Path):
    for p in sorted(src.glob('*.pdf')):
        yield p


def cmd_status(args) -> int:
    smap = load_slug_map()
    src = Path(args.src)
    inq = queue_slugs(args.remote)
    print(f'{"slug":32} {"done":5} {"queued":7} pdf')
    for p in iter_pdfs(src):
        slug, mapped = slug_for(p.name, smap)
        flag = '' if mapped else ' [fallback-slug!]'
        print(f'{slug:32} {"✓" if is_done(slug) else "·":5} '
              f'{"✓" if slug in inq else "·":7} {p.name[:40]}{flag}')
    print(f'\n佇列 {args.remote}: {len(inq)} 檔  {sorted(inq)}')
    return 0


def cmd_push(args) -> int:
    smap = load_slug_map()
    src = Path(args.src)
    inq = queue_slugs(args.remote)
    todo, skipped, warned = [], [], []
    for p in iter_pdfs(src):
        slug, mapped = slug_for(p.name, smap)
        if not mapped:
            warned.append((p.name, slug))
        if is_done(slug) and not args.all:
            skipped.append((slug, 'done'))
            continue
        if slug in inq and not args.force:
            skipped.append((slug, 'already-queued'))
            continue
        todo.append((p, slug))

    if warned:
        print('[warn] 以下檔名查無 slug_map 對照，用機械 fallback（建議補 slug_map.json）：')
        for n, s in warned:
            print(f'  {n}  →  {s}')
    for slug, why in skipped:
        print(f'[skip] {slug} ({why})')
    if not todo:
        print('沒有要上傳的（全完成或已在佇列）。'); return 0

    print(f'\n[push] {len(todo)} 本 → {args.remote}/')
    for p, slug in todo:
        dst = f'{args.remote}/{slug}.pdf'
        mb = p.stat().st_size / 1e6
        print(f'  {slug}.pdf  ({mb:.1f} MB)  ← {p.name[:40]}')
        if args.dry_run:
            continue
        rclone(['copyto', str(p), dst, '-P'])
    if args.dry_run:
        print('(dry-run，未實際上傳)')
    else:
        print(f'\n完成。下一步：雲端 Claude Code session 跑 /ingest-cloud')
    return 0


def _books_with_unified() -> list[str]:
    return sorted(p.name for p in DATA.iterdir()
                  if (p / 'unified' / 'content_list.json').exists())


def cmd_backup(args) -> int:
    """把書的 unified/（圖+OCR輸出，不可重生機器產物）打包 per-book tar 上傳 Drive。
    extract_rules/zh.json 走 git、parsed 可重生，皆不備份。"""
    slugs = args.slugs or (_books_with_unified() if args.all else [])
    if not slugs:
        print('指定 slug 或 --all'); return 1
    for slug in slugs:
        if not (DATA / slug / 'unified' / 'content_list.json').exists():
            print(f'[skip] {slug}：無 unified'); continue
        tar = f'/tmp/{slug}_unified.tar'
        subprocess.run(['tar', '-cf', tar, '-C', str(DATA / slug), 'unified'], check=True)
        mb = os.path.getsize(tar) / 1e6
        print(f'[backup] {slug}: unified {mb:.0f}MB → {args.remote_data}/{slug}.tar')
        if not args.dry_run:
            rclone(['copyto', tar, f'{args.remote_data}/{slug}.tar', '-P'])
        os.remove(tar)
    return 0


def cmd_restore(args) -> int:
    """從 Drive 拉 <slug>.tar 解出 unified/，並（預設）跑 parser 重生 parsed/。
    換機器 / 災難還原：git clone 後 restore --all 即補回所有書籍資料。"""
    if args.all:
        out = rclone(['lsf', args.remote_data], capture=True).stdout
        slugs = [Path(l).stem for l in out.splitlines() if l.strip().endswith('.tar')]
    else:
        slugs = args.slugs
    if not slugs:
        print('指定 slug 或 --all'); return 1
    for slug in slugs:
        tar = f'/tmp/{slug}.tar'
        print(f'[restore] {slug} ← {args.remote_data}/{slug}.tar')
        rclone(['copyto', f'{args.remote_data}/{slug}.tar', tar, '-P'])
        (DATA / slug).mkdir(parents=True, exist_ok=True)
        subprocess.run(['tar', '-xf', tar, '-C', str(DATA / slug)], check=True)
        os.remove(tar)
        if not args.no_parse and (DATA / slug / 'extract_rules.yaml').exists():
            print(f'  [parser] 重生 {slug} parsed/')
            subprocess.run(['uv', 'run', '--with', 'pyyaml', 'python', '-m',
                            'book_pipeline.parser', slug], check=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='Drive 中轉佇列：本機上傳，雲端 ingest')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('push', 'status'):
        sp = sub.add_parser(name)
        sp.add_argument('--src', default=str(DEFAULT_SRC))
        sp.add_argument('--remote', default=DEFAULT_REMOTE)
    push = sub.choices['push']
    push.add_argument('--all', action='store_true', help='含已完成的也上傳（重跑用）')
    push.add_argument('--force', action='store_true', help='已在佇列也覆蓋')
    push.add_argument('--dry-run', action='store_true')
    # backup / restore：unified/ 機器產物 ↔ Drive per-book tar
    for name in ('backup', 'restore'):
        sp = sub.add_parser(name)
        sp.add_argument('slugs', nargs='*', help='指定書 slug；省略則配 --all')
        sp.add_argument('--all', action='store_true', help='全部（backup=有 unified 的；restore=Drive 上所有 tar）')
        sp.add_argument('--remote-data', dest='remote_data', default=DEFAULT_DATA_REMOTE)
    sub.choices['backup'].add_argument('--dry-run', action='store_true')
    sub.choices['restore'].add_argument('--no-parse', action='store_true',
                                        help='只解 unified，不自動跑 parser 重生 parsed')
    args = ap.parse_args()
    return {'push': cmd_push, 'status': cmd_status,
            'backup': cmd_backup, 'restore': cmd_restore}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
