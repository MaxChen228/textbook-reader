#!/usr/bin/env python3
"""book_pipeline.booklists — 具名書單 = 全 project 的「目標正典」單一真相源（SoT）。

[architect note — 為何存在、設計取捨，務必內化]

這是整套 crawl 系統的分母。在它之前，crawl 的目標是開放式的 `crawl_wishlist.json`
主題（自由文字），每次補貨都派一隻 LLM「讀整本 wishlist + 全 inventory → 從零重推該補哪幾本」，
代價是：每次補貨燒大量 token、無進度/無終點、不同 tick 的「經典優先序」會漂移。

本模組把目標**固化成枚舉清單**，於是「選書」退化成純確定性集合運算（canon ∖ 已有 ∖ 排隊中），
**完全不需要 LLM**。LLM 唯一殘留價值是「解析」（書名→z-lib 具體 id/hash，或標記 z-lib 查無），
那是 resolver 的事、每本一次性、結果寫進 sidecar 永久 cache（見 crawl_resolution.json）。

資料布局（兩層，使用者選定的顆粒度）：
  booklists/<field_id>.json = 一個領域檔，內含 `sublists`（具名子單，按該領域標準科目分組）。
  每本主書 = {slug, title, author, edition_pref?, solution?}。**不存 owned/狀態**——狀態一律衍生，
  否則會隨「收到沒」漂移。slug 全域唯一、即 pipeline 主鍵（owned 書用既有 slug、逐字對齊 inventory）。

解答本（題本）不手列：凡主書 `solution != false`（預設 true）即**系統衍生**一個 `<slug>_sol` 目標，
緊接其主書排序。其狀態同樣由 inventory + resolution 衍生（resolver 查無正版即標 absent → 永不再排，
殺掉舊系統「每 tick 重新確認 Peskin 沒解答」的空轉）。

狀態五態（衍生，join inventory + 購物清單 + resolution sidecar）：
  owned      已在 mineru_data/ 或 raw_pdfs/（不再爬）
  queued     已在 crawl_queue.json 購物清單待抓
  ready      已解析出 zlib id/hash、未 owned/未 queued → refill 的確定性候選
  absent     resolver 查證 z-lib 無此書 → 永不再排（catalog 顯示「無法收錄」）
  unresolved 尚未解析（待 resolver 處理）

下游：refill（pipeline_tick）用 select_next() 確定性拉貨；build 用 annotate() 烤 catalog；
devctl/dev 頁用 progress() 顯示各領域收錄進度。本模組純讀，唯一寫者是 resolver（寫 sidecar）。
"""
from __future__ import annotations

import argparse
import glob
import os
import re

from book_pipeline import jsonio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BP = os.path.join(ROOT, 'book_pipeline')
BOOKLISTS_DIR = os.path.join(BP, 'booklists')
RESOLUTION = os.path.join(BP, 'crawl_resolution.json')
DATA_DIR = os.path.join(BP, 'mineru_data')
RAW = os.path.join(ROOT, 'raw_pdfs')
CRAWL_QUEUE = os.path.join(BP, 'crawl_queue.json')
SLUG_MAP = os.path.join(BP, 'slug_map.json')

SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')
SOL_SUFFIX = '_sol'

# 衍生狀態（見模組 docstring）
OWNED = 'owned'
QUEUED = 'queued'
READY = 'ready'
ABSENT = 'absent'
UNRESOLVED = 'unresolved'
STATUSES = (OWNED, QUEUED, READY, ABSENT, UNRESOLVED)


# ── 載入 SoT ──────────────────────────────────────────────────────────────
def load_files(dirpath: str | None = None) -> list[dict]:
    """讀 booklists/*.json，按 (order, field_id) 排序。容錯：壞檔/非書單檔跳過。
    每個檔附 `_path`（除錯/驗證用，非 SoT 內容）。"""
    dirpath = dirpath or BOOKLISTS_DIR
    files = []
    for p in sorted(glob.glob(os.path.join(dirpath, '*.json'))):
        d = jsonio.read_json(p, None)
        if isinstance(d, dict) and isinstance(d.get('sublists'), list):
            d = dict(d)
            d['_path'] = p
            files.append(d)
    files.sort(key=lambda f: (f.get('order', 9999), f.get('field_id', '')))
    return files


def _iter_books(files: list[dict]):
    """deterministic 走訪：yield (file, sublist_idx, sublist, book_idx, book)。"""
    for f in files:
        for si, sl in enumerate(f.get('sublists') or []):
            for bi, b in enumerate(sl.get('books') or []):
                yield f, si, sl, bi, b


def targets(files: list[dict] | None = None) -> list[dict]:
    """攤平 SoT → 有序 target 清單（含衍生的解答本目標）。
    每個主書 → 一個 main target；主書 solution!=False → 緊接一個 <slug>_sol solution target。
    回傳每筆：{slug,title,author,edition_pref,field,field_id,subject,kind,of,order}，已按 order 排序。
    order=(field_order, sublist_idx, book_idx, kind_rank)——主書(0) 緊鄰其解答本(1)，跨書按書單序。"""
    files = load_files() if files is None else files
    out = []
    for f, si, sl, bi, b in _iter_books(files):
        slug = b.get('slug', '')
        base = (f.get('order', 9999), si, bi)
        out.append({
            'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
            'edition_pref': b.get('edition_pref', ''), 'field': f.get('field', ''),
            'field_id': f.get('field_id', ''), 'subject': sl.get('name', ''),
            'kind': 'main', 'of': None, 'order': base + (0,),
        })
        if b.get('solution', True):
            out.append({
                'slug': f'{slug}{SOL_SUFFIX}', 'title': f"{b.get('title', '')} — Solutions",
                'author': b.get('author', ''), 'edition_pref': b.get('edition_pref', ''),
                'field': f.get('field', ''), 'field_id': f.get('field_id', ''),
                'subject': sl.get('name', ''), 'kind': 'solution', 'of': slug,
                'order': base + (1,),
            })
    out.sort(key=lambda t: t['order'])
    return out


# ── 衍生狀態：join inventory + 購物清單 + resolution sidecar ───────────────
def have_slugs() -> set:
    """已存在、不再爬的 slug：mineru_data/* 任何書（含 _sol、in-flight）∪ raw_pdfs 內**已是合法
    slug 檔名**者（crawl 寫的 <slug>.pdf）。raw_pdfs 的 legacy 原始檔名（大寫/空格/括號）不認——
    其對應的書必已 ingest 進 mineru_data（上面已收），故過濾不漏 owned，且不污染成偽 slug。"""
    have = set()
    for p in glob.glob(os.path.join(DATA_DIR, '*')):
        if os.path.isdir(p):
            have.add(os.path.basename(p))
    sm = (jsonio.read_json(SLUG_MAP, {}) or {}).get('map', {})
    for p in glob.glob(os.path.join(RAW, '*.pdf')):
        fn = os.path.basename(p)
        slug = sm.get(fn)                       # legacy 原檔名 → slug_map 翻成正規 slug
        if not slug:
            base = fn[:-4]                       # crawl 寫的在途檔本就是 <slug>.pdf
            slug = base if SLUG_RE.match(base) else None
        if slug:
            have.add(slug)
    return have


def queued_slugs() -> set:
    q = jsonio.read_json(CRAWL_QUEUE, {}) or {}
    return {b['slug'] for b in (q.get('books') or []) if b.get('slug')}


def load_resolution() -> dict:
    """resolver 寫的解析 sidecar：{slug: {id,hash,title,at} | {absent:true,at,note} | {review:true,...}}。"""
    return jsonio.read_json(RESOLUTION, {}) or {}


def save_resolution(updates: dict) -> dict:
    """把 {slug: entry} 合併進 resolution sidecar 並原子寫。**resolver 是唯一寫者**（CLI 單跑、
    不與 daemon 並發寫此檔），故 read-merge-write 無需鎖。回合併後全表。"""
    cur = load_resolution()
    cur.update(updates)
    jsonio.atomic_write_json(RESOLUTION, cur, indent=1)
    return cur


def status_of(slug: str, have: set, queued: set, resolution: dict) -> str:
    if slug in have:
        return OWNED
    if slug in queued:
        return QUEUED
    r = resolution.get(slug) or {}
    if r.get('absent'):
        return ABSENT
    if r.get('id') and r.get('hash'):
        return READY
    return UNRESOLVED


# ── 下游 API：annotate / select_next / progress ───────────────────────────
def annotate(files: list[dict] | None = None, have: set | None = None,
             queued: set | None = None, resolution: dict | None = None) -> list[dict]:
    """每個 target 附 status（build catalog / devctl 用）。參數可注入（測試）。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    queued = queued_slugs() if queued is None else queued
    resolution = load_resolution() if resolution is None else resolution
    rows = []
    for t in targets(files):
        r = dict(t)
        r['status'] = status_of(t['slug'], have, queued, resolution)
        rows.append(r)
    return rows


def select_next(n: int, files: list[dict] | None = None, have: set | None = None,
                queued: set | None = None, resolution: dict | None = None) -> list[dict]:
    """**確定性 refill**：status==READY 的 target，按書單序取前 n →
    [{slug,id,hash,title}]（可直接 merge 進 crawl_queue）。**零 LLM**。"""
    n = max(0, int(n))
    if n == 0:
        return []
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    queued = queued_slugs() if queued is None else queued
    resolution = load_resolution() if resolution is None else resolution
    # 註：解答本 target 獨立解析，可能在其主書尚未 owned/queued 時就 READY 而先被選——刻意允許
    # （每本書獨立可得性，題本與主書解耦；catalog 仍各自顯示三態）。
    picks = []
    for t in targets(files):
        if len(picks) >= n:
            break
        if status_of(t['slug'], have, queued, resolution) != READY:
            continue
        r = resolution[t['slug']]
        picks.append({'slug': t['slug'], 'id': str(r['id']), 'hash': str(r['hash']),
                      'title': r.get('title') or t['title']})
    return picks


def has_unresolved(files: list[dict] | None = None, have: set | None = None,
                   queued: set | None = None, resolution: dict | None = None) -> bool:
    """是否還有 unresolved target（resolver 有事可做）。refill 用：ready 不足時要不要跑 resolver。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    queued = queued_slugs() if queued is None else queued
    resolution = load_resolution() if resolution is None else resolution
    return any(status_of(t['slug'], have, queued, resolution) == UNRESOLVED
               for t in targets(files))


def progress(files: list[dict] | None = None, have: set | None = None,
             queued: set | None = None, resolution: dict | None = None) -> dict:
    """各領域 + 整體的狀態統計（dev 頁收錄進度）。"""
    rows = annotate(files, have, queued, resolution)

    def tally(rs: list[dict]) -> dict:
        c = {s: 0 for s in STATUSES}
        for r in rs:
            c[r['status']] += 1
        c['total'] = len(rs)
        c['main'] = sum(1 for r in rs if r['kind'] == 'main')
        return c

    by_field: dict[str, list] = {}
    for r in rows:
        by_field.setdefault(r['field'], []).append(r)
    return {'overall': tally(rows),
            'by_field': {f: tally(rs) for f, rs in by_field.items()}}


def catalog(files: list[dict] | None = None, have: set | None = None,
            queued: set | None = None, resolution: dict | None = None) -> dict:
    """UI 收錄表結構：field → sublist → 主書（status + 解答本 sol_status）+ 各層統計。
    build 烤成 data/catalog.json 供 reader library 渲染收錄表；status 對應 UI 三態：
    owned/queued/ready→已收錄或排隊中、absent→無法收錄、unresolved→待解析。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    queued = queued_slugs() if queued is None else queued
    resolution = load_resolution() if resolution is None else resolution
    pr = progress(files, have, queued, resolution)
    fields = []
    for f in files:
        subs = []
        for sl in (f.get('sublists') or []):
            books = []
            for b in (sl.get('books') or []):
                slug = b.get('slug', '')
                e = {'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
                     'status': status_of(slug, have, queued, resolution)}
                if b.get('edition_pref'):
                    e['edition_pref'] = b['edition_pref']
                if b.get('solution', True):
                    e['sol_status'] = status_of(f'{slug}{SOL_SUFFIX}', have, queued, resolution)
                books.append(e)
            subs.append({'name': sl.get('name', ''), 'books': books})
        fields.append({'field': f.get('field', ''), 'field_id': f.get('field_id', ''),
                       'order': f.get('order', 9999), 'sublists': subs,
                       'stats': pr['by_field'].get(f.get('field', ''), {})})
    return {'fields': fields, 'overall': pr['overall']}


# ── 驗證 / 對賬（assembly 與 CI 用，不自動改 SoT）──────────────────────────
def validate(files: list[dict] | None = None) -> list[str]:
    """schema + slug 全域唯一/合法性。回錯誤清單（空=通過）。"""
    files = load_files() if files is None else files
    errs = []
    seen: dict[str, str] = {}
    for f in files:
        fid = f.get('field_id') or f.get('_path') or '?'
        if not f.get('field'):
            errs.append(f'{fid}: 缺 field 顯示名')
        for sl in (f.get('sublists') or []):
            if not sl.get('name'):
                errs.append(f'{fid}: 有子單缺 name')
            for b in (sl.get('books') or []):
                slug = b.get('slug', '')
                if not SLUG_RE.match(slug):
                    errs.append(f'{fid}: slug 不合法（須 [a-z0-9_]{{1,64}}）：{slug!r}')
                    continue
                if slug.endswith(SOL_SUFFIX):
                    errs.append(f'{fid}/{slug}: 主書 slug 不得以 {SOL_SUFFIX} 結尾（解答本由系統衍生）')
                if not b.get('title'):
                    errs.append(f'{fid}/{slug}: 缺 title')
                if not b.get('author'):
                    errs.append(f'{fid}/{slug}: 缺 author')
                if slug in seen:
                    errs.append(f'slug 重複：{slug}（{seen[slug]} 與 {fid}）')
                else:
                    seen[slug] = fid
    return errs


def reconcile_owned(files: list[dict] | None = None, have: set | None = None) -> dict:
    """SoT ↔ inventory 對賬：
      inventory_not_in_sot：inventory 有主書但書單漏列 → **異常**，該補進某書單。
      owned_sol_not_in_sot：inventory 有解答本但對應主書 solution=false（未衍生該目標）→ **異常**，
        該主書應改 solution:true（否則已擁有的題本不被追蹤）。
      in_sot_not_inventory：書單有、inventory 無 → 正常（待收錄缺口），僅供概覽。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    ts = targets(files)
    main_slugs = {t['slug'] for t in ts if t['kind'] == 'main'}
    sol_targets = {t['slug'] for t in ts if t['kind'] == 'solution'}
    have_main = {s for s in have if not s.endswith(SOL_SUFFIX)}
    have_sol = {s for s in have if s.endswith(SOL_SUFFIX)}
    return {'inventory_not_in_sot': sorted(have_main - main_slugs),
            'owned_sol_not_in_sot': sorted(have_sol - sol_targets),
            'in_sot_not_inventory': sorted(main_slugs - have)}


# ── CLI（ops / dry-run / CI）───────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description='具名書單 SoT 工具')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('validate')
    sub.add_parser('reconcile')
    sub.add_parser('progress')
    p_next = sub.add_parser('next')
    p_next.add_argument('n', type=int)
    args = ap.parse_args()

    if args.cmd == 'validate':
        errs = validate()
        if errs:
            print(f'✗ {len(errs)} 個問題：')
            for e in errs:
                print(f'  - {e}')
            return 1
        print(f'✓ 書單通過（{len(targets())} targets，{len([t for t in targets() if t["kind"]=="main"])} 主書）')
        return 0
    if args.cmd == 'reconcile':
        r = reconcile_owned()
        bad, badsol = r['inventory_not_in_sot'], r['owned_sol_not_in_sot']
        print(f'inventory 有但書單漏列（{len(bad)}）：{" ".join(bad) or "（無，✓）"}')
        print(f'owned 題本但主書 solution=false（{len(badsol)}）：{" ".join(badsol) or "（無，✓）"}')
        print(f'書單有但尚未收錄（{len(r["in_sot_not_inventory"])}，待收錄缺口）')
        return 1 if (bad or badsol) else 0
    if args.cmd == 'progress':
        pr = progress()
        o = pr['overall']
        print(f'整體：{o[OWNED]}/{o["total"]} 收錄 · queued {o[QUEUED]} · ready {o[READY]} · '
              f'absent {o[ABSENT]} · unresolved {o[UNRESOLVED]}（主書 {o["main"]}）')
        for fld, c in pr['by_field'].items():
            print(f'  {fld:18} {c[OWNED]:>3}/{c["total"]:<3} 收錄  '
                  f'(ready {c[READY]} · unresolved {c[UNRESOLVED]} · absent {c[ABSENT]})')
        return 0
    if args.cmd == 'next':
        for b in select_next(args.n):
            print(f'{b["slug"]:40} id={b["id"]} hash={b["hash"]}')
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
