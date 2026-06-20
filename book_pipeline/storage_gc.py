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
  uv run python -m book_pipeline.storage_gc restore           # 列冷藏可還原清單
  uv run python -m book_pipeline.storage_gc restore <slug>    # 從冷藏拉回某書 raw 並解壓
  uv run python -m book_pipeline.storage_gc restore <name> --kind raw_pdfs  # 拉回源 PDF
  uv run python -m book_pipeline.storage_gc reassemble <slug> # 從 raw 重生 unified（自包含）

預設一律 dry-run；加 --apply 才真的動檔案。冷藏為 copy（原件留作 backup）。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from book_pipeline import jsonio
from book_pipeline import mineru_budget as mb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'book_pipeline', 'mineru_data')
RAW_PDFS = os.path.join(ROOT, 'raw_pdfs')
QUARANTINE = os.path.join(ROOT, 'book_pipeline', '_quarantine')
BAKED = os.path.join(ROOT, 'data')
SIDECAR = os.path.join(ROOT, 'book_pipeline', '.storage_gc.json')
TICK_LOCK = os.path.join(ROOT, 'book_pipeline', '.tick.lock')


# ── daemon 互斥：所有 --apply 路徑必須與 24hr daemon tick 序列化 ─────────────────
# 鏡像 pipeline_tick.main() 的 flock（同一把 .tick.lock，NB 互斥）。手動破壞性操作
# （prune/archive/restore --apply）與 tick 絕不同時動檔：tick 跑時手跑會被拒、間隙才放行。
@contextlib.contextmanager
def _tick_lock():
    lf = open(TICK_LOCK, 'w')
    try:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('  ⛔ daemon tick 執行中（.tick.lock 被持有）；待其結束再試。')
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def _in_flight() -> set:
    """daemon 正 ingest/in-flight 的 slug 集（含 _sol）。MinerU async batch 可能跨 tick
    存活（提交的 tick 已退、無 tick 在跑、_tick_lock 也擋不住）→ 必須獨立查 _pending_batches。

    刻意用 in_flight()（_pending 全集）而非 occupied()（濾掉 stale 上傳）：stale-uploading
    的書 daemon 會「重新提交」，而 resubmit 讀的正是 chunks/p{s}-{e}.pdf（🟡 prune 目標）
    → 連 stale 的書也要保護其 chunks/，故取最廣集最保守。
    註：mb.in_flight() 底層 _pending_entries 對讀檔例外是 fail-open（損毀→空集），即 in-flight
    集可能漏判；但有 .tick.lock 序列化 + _deployed 閘兜底，最壞只多刪一本「已上站且 batch 仍掛」
    書的可重生解壓檔（非未上站書、非源 PDF），可接受。"""
    return mb.in_flight()


def _resolve_archive_root() -> str:
    """冷藏目的地解析序：env > sidecar(.storage_gc.json) > 預設。
    HDD 到貨只需改 sidecar 一行（archive_root），免改碼、免記得 source env。"""
    env = os.environ.get('BOOK_STORAGE_ARCHIVE_ROOT', '').strip()
    if env:
        return os.path.expanduser(env)
    root = (jsonio.read_json(SIDECAR, {}) or {}).get('archive_root')
    if root:
        return os.path.expanduser(root)
    return os.path.expanduser('~/cold-archive/textbook-reader')


ARCHIVE_ROOT = _resolve_archive_root()


# ── 安全閘：已上站 = 全鏈完成（鏡像 status._deployed，單一真相在彼）────────────
def _deployed(slug: str) -> bool:
    """book.json 不只「存在」還要「完整」才算上站。bake_json.dump 一旦非原子寫，
    build 被殺可留半截 book.json；只 exists 會把沒真完成的書判可刪。故讀進來驗
    非 null 且含 chapters 鍵——壞檔/半截 → 視為未上站（保守保留 raw，零誤刪）。
    （bake_json.dump 已於本 phase 原子化，此為縱深防禦：舊殘檔／外力截斷仍守得住。）"""
    bj = os.path.join(BAKED, slug, 'book.json')
    if not os.path.exists(bj):
        return False
    data = jsonio.read_json(bj, None)
    return isinstance(data, dict) and 'chapters' in data


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


def _book_prune_targets(slug: str) -> list[str]:
    """單書 🟡 可免費重生產物（raw/chunk_*/ 解壓檔 + chunks/ 切割 PDF）。"""
    targets = _raw_extracted_dirs(slug)
    c = _chunks_dir(slug)
    if c:
        targets.append(c)
    return targets


def collect_prune(only: list[str] | None = None
                  ) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """🟡 可免費重生 → 直接刪。回 (要刪路徑, 已上站slug, 跳過(slug,原因))。
    only 給定時只作用該些書（範圍化分批/驗證），並跳過全域 __pycache__。"""
    sel = set(only) if only else None
    flying = _in_flight()
    targets, deployed, skipped = [], [], []
    for slug in _slugs():
        if sel and slug not in sel:
            continue
        if not _deployed(slug):
            # 未上站可能正在 ingest：raw 解壓檔是 assemble 真相，絕不碰
            if _raw_extracted_dirs(slug) or _chunks_dir(slug):
                skipped.append((slug, '未上站（保留中間產物）'))
            continue
        # 已上站但仍有 async MinerU batch 在飛（自身或其 _sol）：raw 解壓檔可能正被
        # harvest/assemble 回讀，跨 tick 存活、_tick_lock 擋不住 → 排除，等收割完。
        if slug in flying or f'{slug}_sol' in flying:
            skipped.append((slug, '在飛（MinerU batch 未收割）'))
            continue
        deployed.append(slug)
        targets += _book_prune_targets(slug)
    # ⚪ 垃圾：__pycache__（範圍化時不掃）
    if sel is None:
        for base in (os.path.join(ROOT, 'book_pipeline'), ROOT):
            pc = os.path.join(base, '__pycache__')
            if os.path.isdir(pc):
                targets.append(pc)
    return targets, deployed, skipped


def can_archive_raw_pdf(pdf_name: str) -> tuple[bool, str]:
    """raw_pdfs/<pdf_name> 是否已被全部消費、可冷藏。回 (可否, 原因)。

    判準（復用既有真相，不另立）：對應主書已上站；若為 _sol 解答本，需母書已上站
    且該 _sol 的 unified 已組好（sol_ingest 完成＝源 PDF 已消費）。對應不到 slug
    （legacy 名未登 slug_map）一律保留——寧可不搬，不可搬走仍需用的源 PDF。"""
    from book_pipeline import booklists as bl
    from book_pipeline import status as st
    sm = (jsonio.read_json(bl.SLUG_MAP, {}) or {}).get('map', {})
    stem = pdf_name[:-4] if pdf_name.lower().endswith('.pdf') else pdf_name
    slug = sm.get(pdf_name) or (stem if bl.SLUG_RE.match(stem) else None)
    if not slug:
        return False, 'slug_map 無對應'
    if slug.endswith('_sol'):
        parent = slug[:-4]
        if not _deployed(parent):
            return False, f'母書 {parent} 未上站'
        if st._exists(slug, 'unified', 'content_list.json'):
            return True, 'sol 已 ingest'
        return False, 'sol 未 ingest'
    if not _deployed(slug):
        return False, f'{slug} 未上站'
    return True, f'{slug} 已上站'


def collect_archive(only: list[str] | None = None
                    ) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """🔵 冷 → 搬 ARCHIVE_ROOT。回 (移動組, 保留清單)。
    only 給定時僅作用該些書的 raw_zips（範圍化分批/驗證），跳過 raw_pdfs/quarantine。"""
    sel = set(only) if only else None
    flying = _in_flight()
    zips: list[str] = []
    for slug in _slugs():
        if sel and slug not in sel:
            continue
        # raw_zips 閘對齊 raw_pdfs：已上站 **且** 非在飛。在飛時 zip 是 harvest 回讀
        # 真相（resume 重收割會解壓它），搬走會讓 daemon 找不到 → 與其協調須排除。
        if _deployed(slug) and slug not in flying and f'{slug}_sol' not in flying:
            zips += _raw_zips(slug)
    pdfs: list[str] = []
    held: list[tuple[str, str]] = []
    quar: list[str] = []
    if sel is None:
        if os.path.isdir(RAW_PDFS):
            for f in sorted(os.listdir(RAW_PDFS)):
                if not f.lower().endswith('.pdf'):
                    continue
                p = os.path.join(RAW_PDFS, f)
                ok, why = can_archive_raw_pdf(f)
                if ok:
                    pdfs.append(p)
                else:
                    held.append((p, why))
        if os.path.isdir(QUARANTINE):
            quar = [os.path.join(QUARANTINE, f) for f in os.listdir(QUARANTINE)]
    return {'raw_zips': zips, 'raw_pdfs': pdfs, 'quarantine': quar}, held


# ── 冷藏索引（manifest）：HDD 易手自帶清單，restore/observability 不靠人腦記憶 ────
def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _manifest_path() -> str:
    return os.path.join(ARCHIVE_ROOT, 'manifest.json')


def _load_manifest() -> dict:
    """冷藏索引：{'raw_zips':{slug:{files,bytes,moved_at}}, 'raw_pdfs':{name:{...}},
    'quarantine':{name:{...}}}。不存在/壞 → 空骨架。"""
    m = jsonio.read_json(_manifest_path(), None)
    if not isinstance(m, dict):
        m = {}
    for k in ('raw_zips', 'raw_pdfs', 'quarantine'):
        m.setdefault(k, {})
    return m


def _save_manifest(m: dict) -> None:
    os.makedirs(ARCHIVE_ROOT, exist_ok=True)
    jsonio.atomic_write_json(_manifest_path(), m, indent=2)


def _offline_books() -> list[tuple[str, bool]]:
    """已上站書中 raw 已全外移者（unified 在、raw zip+解壓檔本地皆空）。回 (slug, 可還原?)。
    可還原 = 冷藏 manifest 有該 slug 或 ARCHIVE_ROOT/raw_zips/<slug>/ 在。把「raw 還能不能
    叫回」做成可觀測欄位，而非人腦記憶——prune+archive+HDD 離線的退化態一眼可辨。"""
    man = _load_manifest()
    cold = man.get('raw_zips', {})
    out = []
    for slug in _slugs():
        if not _deployed(slug):
            continue
        if not os.path.exists(os.path.join(DATA, slug, 'unified', 'content_list.json')):
            continue
        if _raw_zips(slug) or _raw_extracted_dirs(slug):
            continue  # raw 仍在本地，非離線
        recoverable = (slug in cold
                       or os.path.isdir(os.path.join(ARCHIVE_ROOT, 'raw_zips', slug)))
        out.append((slug, recoverable))
    return out


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

    # 🔵 冷藏現況（讀 manifest）
    man = _load_manifest()
    cz, cp, cq = (len(man.get('raw_zips', {})), len(man.get('raw_pdfs', {})),
                  len(man.get('quarantine', {})))
    if cz or cp or cq or os.path.isdir(ARCHIVE_ROOT):
        print('  ' + '─' * 64)
        print(f'  🔵 冷藏現況：raw_zips {cz} 書 · raw_pdfs {cp} · quarantine {cq}'
              + ('   （詳列 → restore）' if (cz or cp or cq) else '   （空）'))

    # 離線書（raw 已外移）：把「還能否叫回」做成可觀測欄位
    offline = _offline_books()
    if offline:
        danger = [s for s, ok in offline if not ok]
        print(f'  ⚠ 離線書（raw 已外移本地無）：{len(offline)} 本'
              f'，其中冷藏可還原 {len(offline) - len(danger)}、'
              f'不可還原(需重 OCR) {len(danger)}')
        if danger:
            print(f'     ⛔ 不可還原：{", ".join(danger[:6])}'
                  + (' …' if len(danger) > 6 else ''))
    print()


def gc_book(slug: str) -> tuple[int, list[str]]:
    """刪單本書 🟡 可免費重生產物，回 (釋放位元組, 警告清單)。純執行、**不取鎖、不驗閘**
    ——呼叫端負責安全（cmd_prune 取 _tick_lock + collect_prune 已過閘；P5.1 tick 內已持
    .tick.lock + 已排除在飛）。供手動 prune 與 daemon 自動 GC 共用同一刪除核心，杜絕漂移。"""
    freed, warns = 0, []
    for p in _book_prune_targets(slug):
        sz = _du_bytes(p)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            freed += sz
        except Exception as e:
            warns.append(f'{os.path.relpath(p, ROOT)}: {e}')
    return freed, warns


def cmd_prune(args) -> None:
    only = getattr(args, 'slug', None)
    targets, deployed, skipped = collect_prune(only)
    total = _du_bytes_multi(targets)
    mode = '執行刪除' if args.apply else 'DRY-RUN（加 --apply 才真的刪）'
    print(f'\n  🟡 prune — 可免費重生產物  [{mode}]'
          + (f'  · 範圍：{", ".join(only)}' if only else ''))
    print(f'  安全閘：只動已上站的書（{len(deployed)} 本）；跳過未上站／在飛 {len(skipped)} 本')
    print('  ' + '─' * 64)
    for p in targets[:12]:
        print(f'    {_human(_du_bytes(p)):>8}  {os.path.relpath(p, ROOT)}')
    if len(targets) > 12:
        print(f'    …… 共 {len(targets)} 項')
    print('  ' + '─' * 64)
    print(f'  合計可回收：{_human(total)}    （目標 {len(targets)} 項）')
    if skipped:
        print(f'  跳過（未上站／在飛，保留中間產物）：{", ".join(s for s, _ in skipped[:8])}'
              + (' …' if len(skipped) > 8 else ''))
    if not args.apply:
        print('  → 確認無誤後加 --apply 執行\n')
        return
    with _tick_lock():
        # 取鎖後重新列舉（TOCTOU）：鎖外算的清單可能讀到剛結束的 tick 中途狀態。
        targets, deployed, _ = collect_prune(only)
        freed, warns = 0, []
        for slug in deployed:
            f, w = gc_book(slug)
            freed += f
            warns += w
        # __pycache__（非 per-book，gc_book 不涵蓋）：targets 內 basename 為 __pycache__ 者
        for p in targets:
            if os.path.basename(p) == '__pycache__' and os.path.isdir(p):
                sz = _du_bytes(p)
                try:
                    shutil.rmtree(p)
                    freed += sz
                except Exception as e:
                    warns.append(f'{os.path.relpath(p, ROOT)}: {e}')
        for w in warns:
            print(f'    ⚠ 跳過 {w}')
    print(f'  ✅ 已回收 {_human(freed)}；工作碟剩餘 {_human(_free_bytes())}\n')


def cmd_archive(args) -> None:
    only = getattr(args, 'slug', None)
    groups, held = collect_archive(only)
    mode = '執行搬移' if args.apply else 'DRY-RUN（加 --apply 才真的搬）'
    same_disk = os.path.isdir(ARCHIVE_ROOT) and \
        os.stat(ROOT).st_dev == os.stat(ARCHIVE_ROOT).st_dev
    print(f'\n  🔵 archive — 搬冷藏  [{mode}]'
          + (f'  · 範圍：{", ".join(only)}（僅 raw_zips）' if only else ''))
    print(f'  目的地 ARCHIVE_ROOT = {ARCHIVE_ROOT}')
    if not os.path.isdir(ARCHIVE_ROOT):
        print('  ⚠ 目的地尚未存在（HDD 未掛）。--apply 會在同碟建暫代資料夾，'
              '\n    此時搬移「不會」釋放工作碟空間，僅供測試機制；HDD 掛上改 sidecar 再搬。')
    elif same_disk:
        print('  ⚠ 目的地與工作碟同一顆實體碟 → 搬移不釋放空間（暫代測試用）。')
    total = 0
    for key in ('raw_zips', 'raw_pdfs', 'quarantine'):
        paths = groups[key]
        sz = _du_bytes_multi(paths)
        total += sz
        sub = 'raw_zips/<slug>/' if key == 'raw_zips' else f'{key}/'
        print(f'  ── {key}: {len(paths)} 項，{_human(sz)} → {ARCHIVE_ROOT}/{sub}')
    if held:
        print(f'  ── 保留（raw_pdfs 未消費完，不搬）：{len(held)} 項')
        for p, why in held[:6]:
            print(f'       · {os.path.basename(p)} — {why}')
        if len(held) > 6:
            print(f'       …… 共 {len(held)} 項保留')
    print('  ' + '─' * 64)
    print(f'  合計可冷藏：{_human(total)}')
    if not args.apply:
        print('  → HDD 掛好、設好 sidecar(.storage_gc.json)/env 後加 --apply\n')
        return
    with _tick_lock():
        groups, _ = collect_archive(only)  # 取鎖後重新列舉（TOCTOU）
        os.makedirs(ARCHIVE_ROOT, exist_ok=True)
        man = _load_manifest()
        moved = 0
        seen_slugs: set[str] = set()
        for key in ('raw_zips', 'raw_pdfs', 'quarantine'):
            for p in groups[key]:
                if key == 'raw_zips':
                    # p = mineru_data/<slug>/raw/chunk_N.zip → 取 <slug> 需 dirname 兩層
                    slug = os.path.basename(os.path.dirname(os.path.dirname(p)))
                    dest_dir = os.path.join(ARCHIVE_ROOT, 'raw_zips', slug)
                else:
                    slug = None
                    dest_dir = os.path.join(ARCHIVE_ROOT, key)
                os.makedirs(dest_dir, exist_ok=True)
                # P2.2 冷藏自帶組裝說明書：搬某書第一個 zip 時連帶複製 unified/chunks.json
                # → _assembly.json，使「只剩冷藏」也能 reassemble（不靠本地 gitignore 檔）。
                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    ck = os.path.join(DATA, slug, 'unified', 'chunks.json')
                    if os.path.exists(ck):
                        try:
                            shutil.copy2(ck, os.path.join(dest_dir, '_assembly.json'))
                        except Exception as e:
                            print(f'    ⚠ _assembly.json 複製失敗 {slug}: {e}')
                try:
                    sz = _du_bytes(p)
                    shutil.move(p, os.path.join(dest_dir, os.path.basename(p)))
                    moved += sz
                    rec = man[key].setdefault(slug if slug else os.path.basename(p),
                                              {'files': [], 'bytes': 0})
                    if slug:
                        rec['files'].append(os.path.basename(p))
                    rec['bytes'] = rec.get('bytes', 0) + sz
                    rec['moved_at'] = _now()
                except Exception as e:
                    print(f'    ⚠ 跳過 {p}: {e}')
        _save_manifest(man)
    print(f'  ✅ 已冷藏 {_human(moved)}；工作碟剩餘 {_human(_free_bytes())}'
          f'\n  冷藏索引：{_manifest_path()}\n')


def _cold_inventory() -> None:
    """restore 無參數時：列冷藏可還原清單（讀 manifest，無則掃 ARCHIVE_ROOT 兜底）。
    取代「要先知道 slug 才能 restore」——冷藏自帶可發現性。"""
    print(f'\n  🔵 冷藏現況 ARCHIVE_ROOT = {ARCHIVE_ROOT}')
    if not os.path.isdir(ARCHIVE_ROOT):
        print('  （目的地尚未存在——HDD 未掛或未曾 archive）\n')
        return
    man = _load_manifest()
    for key, label in (('raw_zips', '📦 raw_zips（書 OCR 回包，可 reassemble）'),
                       ('raw_pdfs', '📄 raw_pdfs（來源 PDF）'),
                       ('quarantine', '🗃 quarantine（退貨書）')):
        items = dict(man.get(key, {}))
        if not items:  # manifest 缺 → 掃實際目錄（手動搬入/前 manifest 時代資料）
            d = os.path.join(ARCHIVE_ROOT, key)
            if os.path.isdir(d):
                items = {n: None for n in sorted(os.listdir(d))}
        print(f'  {label}：{len(items)} 項')
        for name, meta in list(items.items())[:12]:
            extra = ''
            if isinstance(meta, dict):
                extra = f'  {_human(meta.get("bytes", 0))}  {meta.get("moved_at", "")}'
            print(f'      · {name}{extra}')
        if len(items) > 12:
            print(f'      …… 共 {len(items)} 項')
    print('  還原：restore <名> [--kind raw_zips|raw_pdfs|quarantine] --apply\n')


def _restore_raw_zips(slug: str, apply: bool) -> None:
    """拉回某書 raw zip 並**解壓**重建 raw/chunk_i/（reassemble 前提），連帶拉回
    _assembly.json（供 reassemble fallback）。mineru_ingest._chunk_done 認解壓資料夾
    （含 *_content_list.json）非 zip，故拷回必解壓，否則重組裝看不到 chunk。冷藏為 copy
    （非 move），原件留作 durable backup。"""
    src = os.path.join(ARCHIVE_ROOT, 'raw_zips', slug)
    dst = os.path.join(DATA, slug, 'raw')
    if not os.path.isdir(src):
        sys.exit(f'冷藏無此書 raw：{src}')
    zips = [f for f in os.listdir(src) if f.endswith('.zip')]
    asm = os.path.join(src, '_assembly.json')
    print(f'\n  restore {slug}（raw_zips）：{len(zips)} 個 zip → {dst}（拷回並解壓）'
          + ('  + _assembly.json' if os.path.exists(asm) else ''))
    if not apply:
        print('  DRY-RUN（加 --apply 執行）。完成後接：'
              f'\n    uv run python -m book_pipeline.storage_gc reassemble {slug} --apply'
              f'\n    uv run python -m build.build_all {slug}\n')
        return
    from pathlib import Path
    from book_pipeline.mineru_ingest import extract_zip
    with _tick_lock():  # 寫入 DATA/<slug>/raw，與 daemon harvest 序列化
        os.makedirs(dst, exist_ok=True)
        for z in zips:
            zp = os.path.join(dst, z)
            shutil.copy2(os.path.join(src, z), zp)
            extract_zip(Path(zp), Path(dst) / z[:-4])  # chunk_N.zip → chunk_N/
        if os.path.exists(asm):  # reassemble fallback #3 讀 DATA/<slug>/_assembly.json
            shutil.copy2(asm, os.path.join(DATA, slug, '_assembly.json'))
    print(f'  ✅ 已拉回並解壓 {len(zips)} chunk → {dst}'
          f'\n    下一步：reassemble {slug} --apply → build.build_all {slug}\n')


def _restore_flat(name: str, kind: str, apply: bool) -> None:
    """拉回 raw_pdfs/quarantine 的單一檔/目錄（冷藏 copy 回工作碟原位）。raw_pdfs 必須
    可還原，否則重 OCR 時源 PDF 半永久流放冷藏。"""
    src = os.path.join(ARCHIVE_ROOT, kind, name)
    dst_root = RAW_PDFS if kind == 'raw_pdfs' else QUARANTINE
    dst = os.path.join(dst_root, name)
    if not os.path.exists(src):
        sys.exit(f'冷藏無此項：{src}')
    print(f'\n  restore {name}（{kind}）→ {dst}')
    if not apply:
        print('  DRY-RUN（加 --apply 執行）\n')
        return
    with _tick_lock():
        os.makedirs(dst_root, exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    print(f'  ✅ 已拉回 → {dst}\n')


def cmd_restore(args) -> None:
    """從冷藏拉回。無 slug → 列冷藏清單；有 slug → 按 --kind 還原（預設 raw_zips）。"""
    if not args.slug:
        _cold_inventory()
        return
    if args.kind == 'raw_zips':
        _restore_raw_zips(args.slug, args.apply)
    else:
        _restore_flat(args.slug, args.kind, args.apply)


def _count_chunk_dirs(raw_dir: str) -> int:
    if not os.path.isdir(raw_dir):
        return 0
    return len([d for d in os.listdir(raw_dir)
                if d.startswith('chunk_') and os.path.isdir(os.path.join(raw_dir, d))])


def _reconstruct_ranges_from_chunks(raw_dir: str, overlap: int
                                    ) -> list[tuple[int, int]] | None:
    """最深 fallback：純憑已解壓 raw/chunk_i/ 反推 ranges——讀每個 chunk 的 *_origin.pdf
    頁數，逆 plan_chunks（s0=1；s_{i+1}=e_i-overlap+1）。不需任何 sidecar/cold 檔，只要
    chunk dir 在即可。overlap 取不到時用 1（plan_chunks/submit 全域預設）。"""
    n = _count_chunk_dirs(raw_dir)
    if n == 0:
        return None
    try:
        import fitz  # pymupdf（pyproject 已宣告）
    except Exception:
        return None
    ranges: list[tuple[int, int]] = []
    s = 1
    for i in range(n):
        cdir = os.path.join(raw_dir, f'chunk_{i}')
        pdfs = [f for f in os.listdir(cdir) if f.endswith('_origin.pdf')]
        if not pdfs:
            return None  # 缺 origin.pdf 無法定頁數 → 放棄此 fallback（不靜默猜）
        try:
            doc = fitz.open(os.path.join(cdir, pdfs[0]))
            pc = doc.page_count
            doc.close()
        except Exception:
            return None
        e = s + pc - 1
        ranges.append((s, e))
        s = e - overlap + 1
    return ranges


def _resolve_ranges_overlap(book: str, raw_dir: str
                            ) -> tuple[list[tuple[int, int]], int, str]:
    """reassemble 的 ranges/overlap 來源 fallback 鏈，徹底斷除對 gitignore 單檔的單點依賴：
      1. _run.json（本地，主來源）
      2. unified/chunks.json（assemble 另存 {ranges,overlap}；驗證或 unified 尚在時）
      3. _assembly.json（archive 連帶冷藏、restore 拉回的 chunks.json 副本；只憑冷藏也能重組）
      4. 反推自 raw/chunk_i/*_origin.pdf 頁數（最深，只需解壓檔本身）
    回 (ranges, overlap, 來源說明)。全鏈皆無 → sys.exit。"""
    # 1. _run.json
    run = jsonio.read_json(os.path.join(book, '_run.json'), None)
    if run and run.get('ranges'):
        return [tuple(r) for r in run['ranges']], run.get('overlap', 1), '_run.json'
    # 2. unified/chunks.json
    ck = jsonio.read_json(os.path.join(book, 'unified', 'chunks.json'), None)
    if ck and ck.get('ranges'):
        return [tuple(r) for r in ck['ranges']], ck.get('overlap', 1), 'unified/chunks.json'
    # 3. _assembly.json（冷藏帶回）
    asm = jsonio.read_json(os.path.join(book, '_assembly.json'), None)
    if asm and asm.get('ranges'):
        return [tuple(r) for r in asm['ranges']], asm.get('overlap', 1), '_assembly.json(冷藏)'
    # 4. 反推自 origin.pdf 頁數（overlap 取不到 → 預設 1）
    rec = _reconstruct_ranges_from_chunks(raw_dir, 1)
    if rec:
        return rec, 1, 'origin.pdf 頁數反推(overlap=1)'
    sys.exit(f'缺 ranges 全部來源（_run.json/unified·chunks.json/_assembly.json/origin.pdf），'
             f'無法自包含重組裝：{os.path.basename(book)}')


def cmd_reassemble(args) -> None:
    """從已解壓 raw/chunk_i/ 重生 unified/，直呼 mineru_ingest.assemble。自包含：不碰
    MinerU、不需 _pending_batches。ranges/overlap 走 fallback 鏈（去 _run.json 單點依賴）。
    --out 可導向 temp 目錄供保真度驗證（不覆蓋線上 unified）。"""
    from pathlib import Path
    slug = args.slug
    book = os.path.join(DATA, slug)
    raw_dir = os.path.join(book, 'raw')
    ranges, overlap, src = _resolve_ranges_overlap(book, raw_dir)
    out = args.out or os.path.join(book, 'unified')
    ndir = _count_chunk_dirs(raw_dir)
    print(f'\n  reassemble {slug}：{len(ranges)} chunks（已解壓 {ndir} 個）→ {out}')
    print(f'  ranges 來源：{src}（overlap={overlap}）')
    if ndir < len(ranges):
        print(f'  ⚠ 解壓資料夾不足（需 {len(ranges)}）。先 restore {slug} --apply')
    if not args.apply:
        print('  DRY-RUN（加 --apply 執行）。產出 unified 後 build.build_all 即上站\n')
        return
    from book_pipeline.mineru_ingest import assemble
    summary = assemble(Path(raw_dir), ranges, overlap, Path(out))
    print(f'  ✅ unified 重生：blocks={summary.get("total_blocks")} '
          f'images={summary.get("images_merged", "?")} → {out}\n')


def main() -> None:
    ap = argparse.ArgumentParser(prog='storage_gc', description='儲存分層治理')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('report', help='分層盤點').set_defaults(func=cmd_report)
    p = sub.add_parser('prune', help='刪可免費重生產物（🟡）')
    p.add_argument('--slug', nargs='+', help='只作用這些書（範圍化分批/驗證）')
    p.add_argument('--apply', action='store_true', help='真的刪（預設 dry-run）')
    p.set_defaults(func=cmd_prune)
    a = sub.add_parser('archive', help='搬冷藏（🔵）')
    a.add_argument('--slug', nargs='+', help='只作用這些書的 raw_zips')
    a.add_argument('--apply', action='store_true', help='真的搬（預設 dry-run）')
    a.set_defaults(func=cmd_archive)
    r = sub.add_parser('restore', help='從冷藏拉回（無參數=列冷藏清單）')
    r.add_argument('slug', nargs='?',
                   help='書 slug（raw_zips）或檔名（raw_pdfs/quarantine）；省略=列清單')
    r.add_argument('--kind', choices=['raw_zips', 'raw_pdfs', 'quarantine'],
                   default='raw_zips', help='還原類別（預設 raw_zips）')
    r.add_argument('--apply', action='store_true')
    r.set_defaults(func=cmd_restore)
    rs = sub.add_parser('reassemble', help='從已解壓 raw 重生 unified（自包含）')
    rs.add_argument('slug')
    rs.add_argument('--out', help='輸出 unified 目錄（預設書內 unified/；驗證可指 temp）')
    rs.add_argument('--apply', action='store_true')
    rs.set_defaults(func=cmd_reassemble)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
