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

狀態五態（衍生，純 join inventory + resolution sidecar，無任何 runtime buffer）：
  owned      已在 mineru_data/ 或 raw_pdfs/（不再爬）
  ready      已確認 zlib id/hash、未 owned → 買書員直接下載的候選（= 解析池）
  absent     crawl agent 查證 z-lib 無此書 → 永不再排（catalog 顯示「無法收錄」）
  review     crawl agent 信心不足、待**架構師**人工裁決 → **不自動重試**（不回 crawl agent 工作母體）
  unresolved 尚未解析（crawl agent 的工作母體：select_next 的上游、解析池的 State 1）

review 與 unresolved 必須分立：unresolved=agent 還沒看、review=agent 看過不敢判。混為一談會讓
review 書每 cycle 被重派、燒 token、且解析池永遠補不滿（review 不算 confirmed）→ daemon 不收斂。

（2026-06 簡化：廢「queued / crawl_queue.json 購物清單 buffer」第六態——買書員改每 tick 直接
select_next 取解析池下載，buffer 唯一不可推導的下載失敗計數移到 pipeline_state.json。狀態遂收斂成
純 (inventory, resolution) 的函式，無 runtime buffer 漏進收錄表。）

下游：買書員（pipeline_tick.drain）用 select_next() 確定性取 ready 直接下載；do_crawl_resolve 用
unresolved_targets() 切批派 crawl agent；build 用 annotate() 烤 catalog；devctl/dev 頁用 progress()
顯示收錄進度。本模組純讀，sidecar 唯一寫者 = crawl agent 的 `resolve commit`（包 flock）。
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
SLUG_MAP = os.path.join(BP, 'slug_map.json')

SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')
SOL_SUFFIX = '_sol'

# 衍生狀態（見模組 docstring）
OWNED = 'owned'
READY = 'ready'
ABSENT = 'absent'
REVIEW = 'review'          # crawl agent 信心不足 → 待架構師人工裁決，不自動重試（見模組 docstring）
UNRESOLVED = 'unresolved'
STATUSES = (OWNED, READY, ABSENT, REVIEW, UNRESOLVED)


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


def load_resolution() -> dict:
    """resolver 寫的解析 sidecar：{slug: {id,hash,title,at} | {absent:true,at,note} | {review:true,...}}。"""
    return jsonio.read_json(RESOLUTION, {}) or {}


def save_resolution(updates: dict) -> dict:
    """把 {slug: entry} 合併進 resolution sidecar 並原子寫。寫者 = crawl agent 的 `resolve commit`。
    daemon 端 crawl 解析單隻序列化（__crawl_resolve__ key），但 flock 仍是必要邊界：單隻 agent 一批
    多筆 commit、與 controller 重啟/手動 CLI 可能並發 read-merge-write → flock 互斥防互蓋（每筆 commit
    一次極短臨界區）。回合併後全表。"""
    import fcntl
    with open(RESOLUTION + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = load_resolution()
        cur.update(updates)
        jsonio.atomic_write_json(RESOLUTION, cur, indent=1)
        return cur


def status_of(slug: str, have: set, resolution: dict) -> str:
    if slug in have:
        return OWNED
    r = resolution.get(slug) or {}
    if r.get('absent'):
        return ABSENT
    if r.get('review'):
        return REVIEW          # 待架構師裁決：絕不落 UNRESOLVED（否則回 crawl agent 工作母體被重派）
    if r.get('id') and r.get('hash'):
        return READY
    return UNRESOLVED


# ── 下游 API：annotate / select_next / progress ───────────────────────────
def annotate(files: list[dict] | None = None, have: set | None = None,
             resolution: dict | None = None) -> list[dict]:
    """每個 target 附 status（build catalog / devctl 用）。參數可注入（測試）。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    rows = []
    for t in targets(files):
        r = dict(t)
        r['status'] = status_of(t['slug'], have, resolution)
        rows.append(r)
    return rows


def select_next(n: int, files: list[dict] | None = None, have: set | None = None,
                resolution: dict | None = None, exclude: set | None = None) -> list[dict]:
    """**確定性下載候選**：status==READY 的 target，按書單序取前 n → [{slug,id,hash,title}]
    （買書員直接拿去下載）。**零 LLM**。exclude = 該排除的 slug（下載失敗達上限者，由 caller 提供）。"""
    n = max(0, int(n))
    if n == 0:
        return []
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    exclude = exclude or set()
    # 註：解答本 target 獨立解析，可能在其主書尚未 owned 時就 READY 而先被選——刻意允許
    # （每本書獨立可得性，題本與主書解耦；catalog 仍各自顯示三態）。
    picks = []
    for t in targets(files):
        if len(picks) >= n:
            break
        if t['slug'] in exclude:
            continue
        if status_of(t['slug'], have, resolution) != READY:
            continue
        r = resolution[t['slug']]
        bid, bhash = r.get('id'), r.get('hash')
        # id/hash 須純量：sidecar 畸形（list/dict）會被 str() 成字面 "['123']" 流進 fetch URL → 404
        # 靜默丟書。寧可此 target 暫不下載（保持 READY、不入候選），不污染下游。
        if not (isinstance(bid, (str, int)) and isinstance(bhash, (str, int))):
            continue
        picks.append({'slug': t['slug'], 'id': str(bid), 'hash': str(bhash),
                      'title': r.get('title') or t['title']})
    return picks


def unresolved_targets(files: list[dict] | None = None, have: set | None = None,
                       resolution: dict | None = None) -> list[dict]:
    """status==UNRESOLVED 的 target（書單序）——crawl agent 的工作母體（State 1：在書單、未確認
    z-lib 連結）。daemon 切批派給 agent；agent 也可 `resolve queue` 自查。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    return [t for t in targets(files)
            if status_of(t['slug'], have, resolution) == UNRESOLVED]


def is_trustworthy(entry: dict | None) -> bool:
    """resolution entry 是否由**現役（agent-judged）演算法**產出。判據 = 有 `by` 戳記
    （agent / auto-exact / agent-rehome）。舊確定性 resolver（2026-06 廢棄、自承不可靠）的遺留 entry
    無 `by`、帶 `conf` 欄位 → **不可信**（換演算法時未失效的 stale cache）。水位母數只認可信者，
    否則 stale legacy 撐滿池 → 新 resolver 永不喚醒 → 舊錯誤凍結（self-perpetuating stale-cache trap）。"""
    return bool(entry) and 'by' in entry


def pool_counts(files: list[dict] | None = None, have: set | None = None,
                resolution: dict | None = None) -> dict:
    """爬書水位母數。confirmed = READY = 已確認 z-lib 連結、未 owned 的解析池。
    **confirmed_trustworthy** = 其中由現役演算法解出者（`_crawl_resolve_due` 用此，非 confirmed）——
    legacy entry 不算數，確保換演算法後 resolver 會醒來重解。unresolved = State 1（待 agent 解析）。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    pr = progress(files, have, resolution)
    o = pr['overall']
    trust = sum(1 for t in targets(files)
                if status_of(t['slug'], have, resolution) == READY
                and is_trustworthy(resolution.get(t['slug'])))
    return {'confirmed': o[READY], 'confirmed_trustworthy': trust, 'ready': o[READY],
            'unresolved': o[UNRESOLVED], 'review': o[REVIEW], 'owned': o[OWNED], 'absent': o[ABSENT]}


def progress(files: list[dict] | None = None, have: set | None = None,
             resolution: dict | None = None) -> dict:
    """各領域 + 整體的狀態統計（dev 頁收錄進度）。"""
    rows = annotate(files, have, resolution)

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
            resolution: dict | None = None) -> dict:
    """UI 收錄表結構：field → sublist → 主書（status + 解答本 sol_status）+ 各層統計。
    build 烤成 data/catalog.json 供 reader library 渲染收錄表；status 對應 UI 三態：
    owned/ready→已收錄或排隊中、absent→無法收錄、review/unresolved→待收錄（公開 UI 不分
    待裁/待解析，皆「待收錄」；review 的架構師待裁細節只在 /dev 面板顯示）。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    pr = progress(files, have, resolution)
    fields = []
    for f in files:
        subs = []
        for sl in (f.get('sublists') or []):
            books = []
            for b in (sl.get('books') or []):
                slug = b.get('slug', '')
                e = {'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
                     'status': status_of(slug, have, resolution)}
                if b.get('edition_pref'):
                    e['edition_pref'] = b['edition_pref']
                if b.get('solution', True):
                    e['sol_status'] = status_of(f'{slug}{SOL_SUFFIX}', have, resolution)
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
        print(f'整體：{o[OWNED]}/{o["total"]} 收錄 · ready {o[READY]} · '
              f'absent {o[ABSENT]} · review {o[REVIEW]} · unresolved {o[UNRESOLVED]}（主書 {o["main"]}）')
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
