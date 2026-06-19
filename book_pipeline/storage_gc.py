#!/usr/bin/env python3
"""book_pipeline.storage_gc — 儲存空間分層治理（可重複、可逆、daemon 安全）。

把每本書的產物按「重生成本」分四層，依層處置：

  🟢 熱（serving 命脈，永留工作碟）  parsed/ · unified/ · data/ · img/
  🔵 冷（貴，搬冷藏）                raw/*.zip（MinerU OCR 回包）· raw_pdfs/*.pdf · _quarantine
  🟡 可免費重生（直接刪）            raw/chunk_*/（zip 解壓檔）· chunks/（切割 PDF）
  ⚪ 垃圾（直接刪）                  __pycache__

安全閘（單一真相，鏡像 status._deployed）：**只動「已上站」的書**——
data/<slug>/book.json 已烤出 ⇒ 該書全鏈完成、daemon 在 post-deploy busy-loop
（sol/catalog/math/translate）只回讀 parsed/，永不再碰 raw 解壓檔或 chunks。
故 prune 對「已上站書的中間產物」與 daemon 並行零衝突。未上站的書一律不碰
（可能正在 ingest，raw 解壓檔是 assemble 的真相來源）。

冷藏目的地 = ARCHIVE_ROOT，預設本地暫代資料夾，HDD 插上後改一個 env 即切換：
  export BOOK_STORAGE_ARCHIVE_ROOT=/Volumes/<你的HDD>/textbook-reader-cold

用法：
  uv run python -m book_pipeline.storage_gc report            # 分層盤點（隨時可重跑）
  uv run python -m book_pipeline.storage_gc prune             # 列可免費刪的（dry-run）
  uv run python -m book_pipeline.storage_gc prune --apply     # 真的刪
  uv run python -m book_pipeline.storage_gc archive           # 列要搬冷藏的（dry-run）
  uv run python -m book_pipeline.storage_gc archive --apply   # 真的搬 → ARCHIVE_ROOT
  uv run python -m book_pipeline.storage_gc restore <slug>    # 從冷藏拉回某書 raw（要重組裝時）

預設一律 dry-run；加 --apply 才真的動檔案。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'book_pipeline', 'mineru_data')
RAW_PDFS = os.path.join(ROOT, 'raw_pdfs')
QUARANTINE = os.path.join(ROOT, 'book_pipeline', '_quarantine')
BAKED = os.path.join(ROOT, 'data')

ARCHIVE_ROOT = os.environ.get(
    'BOOK_STORAGE_ARCHIVE_ROOT',
    os.path.expanduser('~/cold-archive/textbook-reader'),
)


# ── 安全閘：已上站 = 全鏈完成（鏡像 status._deployed，單一真相在彼）────────────
def _deployed(slug: str) -> bool:
    return os.path.exists(os.path.join(BAKED, slug, 'book.json'))


# ── 大小工具 ─────────────────────────────────────────────────────────────────
def _du_bytes(path: str) -> int:
    """單一路徑佔用位元組（du -sk，macOS 相容）。不存在回 0。"""
    if not os.path.exists(path):
        return 0
    try:
        out = subprocess.run(['du', '-sk', path], capture_output=True, text=True)
        return int(out.stdout.split('\t', 1)[0]) * 1024
    except Exception:
        return 0


def _du_bytes_multi(paths: list[str]) -> int:
    """多路徑佔用合計（分批避開 ARG_MAX）。"""
    total = 0
    for i in range(0, len(paths), 150):
        batch = [p for p in paths[i:i + 150] if os.path.exists(p)]
        if not batch:
            continue
        try:
            out = subprocess.run(['du', '-sk', *batch], capture_output=True, text=True)
            for line in out.stdout.splitlines():
                total += int(line.split('\t', 1)[0]) * 1024
        except Exception:
            pass
    return total


def _human(n: int) -> str:
    f = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if f < 1024 or unit == 'TB':
            return f'{f:.1f}{unit}' if unit not in ('B', 'KB') else f'{int(f)}{unit}'
        f /= 1024
    return f'{f:.1f}TB'


def _slugs() -> list[str]:
    if not os.path.isdir(DATA):
        return []
    return sorted(d for d in os.listdir(DATA) if os.path.isdir(os.path.join(DATA, d)))


def _free_bytes() -> int:
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize


# ── 分層列舉：回傳 (路徑, 大小) 清單，純列舉不動檔 ───────────────────────────
def _raw_extracted_dirs(slug: str) -> list[str]:
    """某書 raw/ 下的 zip 解壓資料夾（chunk_N/，非 chunk_N.zip）。"""
    raw = os.path.join(DATA, slug, 'raw')
    if not os.path.isdir(raw):
        return []
    out = []
    for name in os.listdir(raw):
        p = os.path.join(raw, name)
        if name.startswith('chunk_') and os.path.isdir(p):
            out.append(p)
    return out


def _raw_zips(slug: str) -> list[str]:
    raw = os.path.join(DATA, slug, 'raw')
    if not os.path.isdir(raw):
        return []
    return [os.path.join(raw, n) for n in os.listdir(raw)
            if n.startswith('chunk_') and n.endswith('.zip')]


def _chunks_dir(slug: str) -> str | None:
    p = os.path.join(DATA, slug, 'chunks')
    return p if os.path.isdir(p) else None


def collect_prune() -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """🟡 可免費重生 → 直接刪。回 (要刪路徑, 已上站slug, 跳過(slug,原因))。"""
    targets, deployed, skipped = [], [], []
    for slug in _slugs():
        if not _deployed(slug):
            # 未上站可能正在 ingest：raw 解壓檔是 assemble 真相，絕不碰
            if _raw_extracted_dirs(slug) or _chunks_dir(slug):
                skipped.append((slug, '未上站（保留中間產物）'))
            continue
        deployed.append(slug)
        targets += _raw_extracted_dirs(slug)
        c = _chunks_dir(slug)
        if c:
            targets.append(c)
    # ⚪ 垃圾：__pycache__
    for base in (os.path.join(ROOT, 'book_pipeline'), ROOT):
        pc = os.path.join(base, '__pycache__')
        if os.path.isdir(pc):
            targets.append(pc)
    return targets, deployed, skipped


def collect_archive() -> dict[str, list[str]]:
    """🔵 冷 → 搬 ARCHIVE_ROOT。分三組：raw_zips（已上站書）/ raw_pdfs / quarantine。"""
    zips: list[str] = []
    for slug in _slugs():
        if _deployed(slug):
            zips += _raw_zips(slug)
    pdfs: list[str] = []
    if os.path.isdir(RAW_PDFS):
        pdfs = [os.path.join(RAW_PDFS, f) for f in os.listdir(RAW_PDFS)
                if f.lower().endswith('.pdf')]
    quar: list[str] = []
    if os.path.isdir(QUARANTINE):
        quar = [os.path.join(QUARANTINE, f) for f in os.listdir(QUARANTINE)]
    return {'raw_zips': zips, 'raw_pdfs': pdfs, 'quarantine': quar}


# ── 指令 ─────────────────────────────────────────────────────────────────────
def cmd_report(_args) -> None:
    slugs = _slugs()
    raw_ext = _du_bytes_multi([p for s in slugs for p in _raw_extracted_dirs(s)])
    raw_zip = _du_bytes_multi([p for s in slugs for p in _raw_zips(s)])
    chunks = _du_bytes_multi([c for s in slugs if (c := _chunks_dir(s))])
    unified = _du_bytes_multi([os.path.join(DATA, s, 'unified') for s in slugs])
    parsed = _du_bytes_multi([os.path.join(DATA, s, 'parsed') for s in slugs])
    rpdfs = _du_bytes(RAW_PDFS)
    quar = _du_bytes(QUARANTINE)
    img = _du_bytes(os.path.join(ROOT, 'img'))
    baked = _du_bytes(BAKED)
    deployed_n = sum(1 for s in slugs if _deployed(s))

    print(f'\n  textbook-reader 儲存分層盤點   （書 {len(slugs)} 本，已上站 {deployed_n}）')
    print(f'  工作碟剩餘空間：{_human(_free_bytes())}')
    print('  ' + '─' * 64)

    def row(tag, name, n, note=''):
        print(f'  {tag} {name:<26}{_human(n):>9}   {note}')

    print('  🟢 熱 — serving 命脈，永留')
    row('  ', 'parsed/（成品資料）', parsed)
    row('  ', 'unified/（半成品＋圖）', unified)
    row('  ', 'img/（網頁 WebP）', img)
    row('  ', 'data/（網頁 JSON）', baked)
    print('  🔵 冷 — 貴（重生要花 OCR／額度），搬冷藏')
    row('  ', 'raw/*.zip（OCR 回包）', raw_zip, '已上站書')
    row('  ', 'raw_pdfs/（來源 PDF）', rpdfs)
    row('  ', '_quarantine（退貨書）', quar)
    print('  🟡 可免費重生 — 直接刪（要時一行重生）')
    row('  ', 'raw/chunk_*/（解壓檔）', raw_ext, '＝ zip 解壓，重複')
    row('  ', 'chunks/（切割 PDF）', chunks)
    print('  ' + '─' * 64)
    print(f'  ▸ 立刻可免費回收（🟡，已上站書）：約 {_human(raw_ext + chunks)}')
    print(f'  ▸ HDD 到再搬冷藏（🔵）：約 {_human(raw_zip + rpdfs + quar)}')
    print(f'  冷藏目的地 ARCHIVE_ROOT = {ARCHIVE_ROOT}'
          f'{"  ⚠尚未存在（HDD 未掛則為同碟暫代）" if not os.path.isdir(ARCHIVE_ROOT) else ""}')
    print()


def cmd_prune(args) -> None:
    targets, deployed, skipped = collect_prune()
    total = _du_bytes_multi(targets)
    mode = '執行刪除' if args.apply else 'DRY-RUN（加 --apply 才真的刪）'
    print(f'\n  🟡 prune — 可免費重生產物  [{mode}]')
    print(f'  安全閘：只動已上站的書（{len(deployed)} 本）；跳過未上站 {len(skipped)} 本')
    print('  ' + '─' * 64)
    for p in targets[:12]:
        print(f'    {_human(_du_bytes(p)):>8}  {os.path.relpath(p, ROOT)}')
    if len(targets) > 12:
        print(f'    …… 共 {len(targets)} 項')
    print('  ' + '─' * 64)
    print(f'  合計可回收：{_human(total)}    （目標 {len(targets)} 項）')
    if skipped:
        print(f'  跳過（未上站、保留中間產物）：{", ".join(s for s, _ in skipped[:8])}'
              + (' …' if len(skipped) > 8 else ''))
    if not args.apply:
        print('  → 確認無誤後加 --apply 執行\n')
        return
    freed = 0
    for p in targets:
        sz = _du_bytes(p)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            freed += sz
        except Exception as e:
            print(f'    ⚠ 跳過 {p}: {e}')
    print(f'  ✅ 已回收 {_human(freed)}；工作碟剩餘 {_human(_free_bytes())}\n')


def cmd_archive(args) -> None:
    groups = collect_archive()
    mode = '執行搬移' if args.apply else 'DRY-RUN（加 --apply 才真的搬）'
    same_disk = os.statvfs(ROOT).f_fsid == (
        os.statvfs(ARCHIVE_ROOT).f_fsid if os.path.isdir(ARCHIVE_ROOT) else -1)
    print(f'\n  🔵 archive — 搬冷藏  [{mode}]')
    print(f'  目的地 ARCHIVE_ROOT = {ARCHIVE_ROOT}')
    if not os.path.isdir(ARCHIVE_ROOT):
        print('  ⚠ 目的地尚未存在（HDD 未掛）。--apply 會在同碟建暫代資料夾，'
              '\n    此時搬移「不會」真的釋放工作碟空間，僅供測試機制；HDD 掛上改 env 再搬。')
    elif same_disk:
        print('  ⚠ 目的地與工作碟同一顆實體碟 → 搬移不釋放空間（暫代測試用）。')
    total = 0
    layout = {'raw_zips': 'raw_zips/<slug>/', 'raw_pdfs': 'raw_pdfs/', 'quarantine': '_quarantine/'}
    for key, paths in groups.items():
        sz = _du_bytes_multi(paths)
        total += sz
        print(f'  ── {key}: {len(paths)} 項，{_human(sz)} → {ARCHIVE_ROOT}/{layout[key]}')
    print('  ' + '─' * 64)
    print(f'  合計可冷藏：{_human(total)}')
    if not args.apply:
        print('  → HDD 掛好、設好 BOOK_STORAGE_ARCHIVE_ROOT 後加 --apply\n')
        return
    moved = 0
    for key, paths in groups.items():
        for p in paths:
            slug = os.path.basename(os.path.dirname(p)) if key == 'raw_zips' else ''
            dest_dir = os.path.join(ARCHIVE_ROOT, key.replace('raw_zips', 'raw_zips'), slug) \
                if key == 'raw_zips' else os.path.join(ARCHIVE_ROOT, key)
            os.makedirs(dest_dir, exist_ok=True)
            try:
                sz = _du_bytes(p)
                shutil.move(p, os.path.join(dest_dir, os.path.basename(p)))
                moved += sz
            except Exception as e:
                print(f'    ⚠ 跳過 {p}: {e}')
    print(f'  ✅ 已冷藏 {_human(moved)}；工作碟剩餘 {_human(_free_bytes())}\n')


def cmd_restore(args) -> None:
    """從冷藏拉回某書的 raw zip（要重組裝 --skip-upload 時）。"""
    slug = args.slug
    src = os.path.join(ARCHIVE_ROOT, 'raw_zips', slug)
    dst = os.path.join(DATA, slug, 'raw')
    if not os.path.isdir(src):
        sys.exit(f'冷藏無此書 raw：{src}')
    zips = [f for f in os.listdir(src) if f.endswith('.zip')]
    print(f'\n  restore {slug}：{len(zips)} 個 zip {src} → {dst}')
    if not args.apply:
        print('  DRY-RUN（加 --apply 執行）。拉回後可 mineru_ingest --skip-upload 重組裝\n')
        return
    os.makedirs(dst, exist_ok=True)
    for z in zips:
        shutil.copy2(os.path.join(src, z), os.path.join(dst, z))
    print(f'  ✅ 已拉回 {len(zips)} zip → {dst}\n')


def main() -> None:
    ap = argparse.ArgumentParser(prog='storage_gc', description='儲存分層治理')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('report', help='分層盤點').set_defaults(func=cmd_report)
    p = sub.add_parser('prune', help='刪可免費重生產物（🟡）')
    p.add_argument('--apply', action='store_true', help='真的刪（預設 dry-run）')
    p.set_defaults(func=cmd_prune)
    a = sub.add_parser('archive', help='搬冷藏（🔵）')
    a.add_argument('--apply', action='store_true', help='真的搬（預設 dry-run）')
    a.set_defaults(func=cmd_archive)
    r = sub.add_parser('restore', help='從冷藏拉回某書 raw')
    r.add_argument('slug')
    r.add_argument('--apply', action='store_true')
    r.set_defaults(func=cmd_restore)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
