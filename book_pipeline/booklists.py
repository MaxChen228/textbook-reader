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
緊接其主書排序。其狀態同樣由 inventory + resolution 衍生（查證左移後由書單管理 skill 入口親查：
z-lib 真無正版即標 not_found 永不再排、有書但無對應版次標 version_unavailable 可重查，
殺掉舊系統「每 tick 重新確認 Peskin 沒解答」的空轉）。

狀態六態（衍生，純 join inventory + resolution sidecar，無任何 runtime buffer。2026-06 查證左移把
舊單一 absent 拆成 not_found/version_unavailable）：
  owned                已在 mineru_data/ 或 raw_pdfs/（不再爬）
  ready                已確認 zlib id/hash、未 owned → 買書員直接下載的候選（= 解析池）
  version_unavailable  書在 z-lib 但無對應版次 → 待重查（recheck_after 到期自動回工作母體；
                       公開層歸「待收錄」非「無法收錄」——殺掉「只有別版→永久放棄」的僵化）
  review               agent 信心不足、待**架構師**人工裁決 → **不自動重試**（不回 agent 工作母體）
  unresolved           尚未解析（agent 工作母體：select_next 的上游、解析池的 State 1）
  not_found            z-lib 真無此書/解答 → 永不再排（catalog 顯示「無法收錄」；含 legacy {absent:true}）

公開層（reader 收錄表 / pool_counts 的 absent 桶）只認舊五態字串，內部六態經 _public_status 摺疊
（not_found→absent、version_unavailable→unresolved）→ 前端零改、行為等價；UI 細分留下游 surface 階段。

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
from datetime import datetime, timezone

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

# 衍生狀態（見模組 docstring）。2026-06 查證左移：舊單一 ABSENT 拆成 NOT_FOUND（永久查無）+
# VERSION_UNAVAILABLE（這次沒查到對的版、可重查）——殺掉「只有別版→永久放棄」的僵化。
OWNED = 'owned'
READY = 'ready'
VERSION_UNAVAILABLE = 'version_unavailable'  # 書在 z-lib 但無對應版次 → 待重查（recheck_after 到期回工作母體）
REVIEW = 'review'          # crawl agent 信心不足 → 待架構師人工裁決，不自動重試（見模組 docstring）
UNRESOLVED = 'unresolved'
NOT_FOUND = 'not_found'    # z-lib 真無此書/解答 → 永不再查（含 legacy {absent:true} 無 status 者）
STATUSES = (OWNED, READY, VERSION_UNAVAILABLE, REVIEW, UNRESOLVED, NOT_FOUND)

# 公開層（reader 收錄表 / pool_counts）只認舊五態字串，新內部六態經此摺疊 → 前端零改、行為等價；
# UI 細分（version_unavailable 顯「可重查」）留到下游 surface 同步那階段。
PUBLIC_ABSENT = 'absent'  # 對外「無法收錄」字串（內部 NOT_FOUND 的公開名）


def _public_status(s: str) -> str:
    """內部六態 → reader 收錄表公開字串：not_found→absent（無法收錄）、
    version_unavailable→unresolved（待收錄，可重查；UI 細分留下游階段），其餘同名。"""
    if s == NOT_FOUND:
        return PUBLIC_ABSENT
    if s == VERSION_UNAVAILABLE:
        return UNRESOLVED
    return s


def _recheck_due(recheck_after: str | None, now: datetime | None = None) -> bool:
    """version_unavailable 的 recheck_after 是否到期（到期→回工作母體重查）。
    壞/缺時戳 → 保守回 False（維持 version_unavailable、不空轉）。"""
    if not recheck_after:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        ra = datetime.fromisoformat(recheck_after)
    except (ValueError, TypeError):
        return False
    if ra.tzinfo is None:
        ra = ra.replace(tzinfo=timezone.utc)
    return now >= ra


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


def targets(files: list[dict] | None = None, include_discovered: bool = True) -> list[dict]:
    """攤平 SoT → 有序 target 清單（含衍生的解答本目標）。
    每個主書 → 一個 main target；主書 solution!=False → 緊接一個 <slug>_sol solution target。
    回傳每筆：{slug,title,author,edition_pref,field,field_id,subject,kind,of,order,source}，按 order 排序。
    order=(field_order, sublist_idx, book_idx, kind_rank)——主書(0) 緊鄰其解答本(1)，跨書按書單序。
    include_discovered（預設 True）：合併 discovery 機器候選層（`discovered/`）——排在所有人工正典之後
    （order 首碼 10000）、撞人工 slug 則跳（人工優先），標 source='discovered'，與人工 target 同等走查證
    流程（status_of/select_next/unresolved_targets 自動納入）。discovered 為空時行為與舊完全等價。"""
    files = load_files() if files is None else files
    out = []
    for f, si, sl, bi, b in _iter_books(files):
        slug = b.get('slug', '')
        base = (f.get('order', 9999), si, bi)
        out.append({
            'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
            'edition_pref': b.get('edition_pref', ''), 'field': f.get('field', ''),
            'field_id': f.get('field_id', ''), 'subject': sl.get('name', ''),
            'kind': 'main', 'of': None, 'order': base + (0,), 'source': 'booklist',
        })
        if b.get('solution', True):
            out.append({
                'slug': f'{slug}{SOL_SUFFIX}', 'title': f"{b.get('title', '')} — Solutions",
                'author': b.get('author', ''), 'edition_pref': b.get('edition_pref', ''),
                'field': f.get('field', ''), 'field_id': f.get('field_id', ''),
                'subject': sl.get('name', ''), 'kind': 'solution', 'of': slug,
                'order': base + (1,), 'source': 'booklist',
            })
    if include_discovered:
        out += _discovered_targets({t['slug'] for t in out})
    out.sort(key=lambda t: t['order'])
    return out


def _discovered_targets(manual_slugs: set) -> list[dict]:
    """discovery 機器候選 → target（排在人工正典後 order 首碼 10000、撞人工/彼此 slug 跳、標 source）。
    discovered 為空（無目錄/無檔）→ 回 []，targets 行為與舊等價。"""
    from book_pipeline import discovered
    out = []
    for ci, c in enumerate(discovered.iter_candidates()):
        slug = c.get('slug', '')
        if not slug or slug in manual_slugs:
            continue
        manual_slugs.add(slug)                  # discovered 內部也去重（同 slug 跨領域檔只取首見）
        base = (10000, ci, 0)
        out.append({
            'slug': slug, 'title': c.get('title', ''), 'author': c.get('author', ''),
            'edition_pref': c.get('edition_pref', ''), 'field': c.get('field', ''),
            'field_id': c.get('field_id', ''), 'subject': c.get('subject', ''),
            'kind': 'main', 'of': None, 'order': base + (0,), 'source': 'discovered',
        })
        if c.get('solution', True):
            out.append({
                'slug': f'{slug}{SOL_SUFFIX}', 'title': f"{c.get('title', '')} — Solutions",
                'author': c.get('author', ''), 'edition_pref': c.get('edition_pref', ''),
                'field': c.get('field', ''), 'field_id': c.get('field_id', ''),
                'subject': c.get('subject', ''), 'kind': 'solution', 'of': slug,
                'order': base + (1,), 'source': 'discovered',
            })
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
    """resolver 寫的解析 sidecar（連結+狀態，高頻 enrich、gitignore）。顯式 `status` 欄為主，
    向後相容 legacy 旗標（無 status 的舊 entry）。entry 形態：
      {status:'resolved', id,hash,title,at}            已確認 z-lib 連結（READY）
      {status:'version_unavailable', recheck_after,at}  書在但無對的版、可重查
      {status:'not_found', at}                          z-lib 真無 → 永不再查
      {status:'review', note,at}                        歧義 → 架構師裁決
      legacy（無 status）：{id,hash} / {absent:true} / {review:true} 由 status_of 向後相容判讀。
    版本判斷（edition/sol_alignment/evidence）不在此——在 git 追蹤的 editions/<slug>.json（見 editions.py）。"""
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


def status_of(slug: str, have: set, resolution: dict, now: datetime | None = None) -> str:
    """衍生六態。認顯式 `status` 欄為主，向後相容 legacy 旗標（無 status 的舊 entry）。
    version_unavailable 的 recheck_after 到期 → 回 UNRESOLVED（自動回工作母體重查）。"""
    if slug in have:
        return OWNED
    r = resolution.get(slug) or {}
    st = r.get('status')
    if st == NOT_FOUND or (r.get('absent') and not st):
        return NOT_FOUND       # 永久查無（顯式 not_found 或 legacy {absent:true}）：絕不回工作母體
    if st == VERSION_UNAVAILABLE:
        # 這次沒對的版（可重查）：recheck_after 到期 → 回 UNRESOLVED 重查；未到 → 暫不排
        # （既不算工作母體 State 1、公開層也歸「待收錄」而非「無法收錄」）。
        return UNRESOLVED if _recheck_due(r.get('recheck_after'), now) else VERSION_UNAVAILABLE
    if st == REVIEW or r.get('review'):
        return REVIEW          # 待架構師裁決：絕不落 UNRESOLVED（否則回 crawl agent 工作母體被重派）
    if st == 'resolved' or (r.get('id') and r.get('hash')):
        # provenance gate：只有現役（agent-judged）演算法解出的才算 READY。舊確定性 resolver 的
        # legacy entry（無 by 戳記、~9% 假陽性）→ 視為**未解析**，回 unresolved_targets 交 agent 重解、
        # 且永不進 select_next 下載候選（杜絕 stale 誤配書被 drain 下載）。換演算法時 stale cache 經此
        # 自動失效。owned 書於最上面先判 OWNED、不受影響（已有 PDF，誤配交 audit 抓，不重解）。
        return READY if is_trustworthy(r) else UNRESOLVED
    return UNRESOLVED


# ── 下游 API：annotate / select_next / progress ───────────────────────────
def annotate(files: list[dict] | None = None, have: set | None = None,
             resolution: dict | None = None, include_discovered: bool = True) -> list[dict]:
    """每個 target 附 status（build catalog / devctl 用）。參數可注入（測試）。
    include_discovered：公開收錄表計數傳 False（只認人工正典）；/dev 水位傳 True（含機器候選）。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    rows = []
    for t in targets(files, include_discovered=include_discovered):
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
    無 `by`、帶 `conf` 欄位 → **不可信**（換演算法時未失效的 stale cache）。`status_of` 用此把 legacy
    解析降級成 UNRESOLVED → resolver 重解、drain 不下載 → stale cache 自動失效（見其註解）。"""
    return bool(entry) and 'by' in entry


def pool_counts(files: list[dict] | None = None, have: set | None = None,
                resolution: dict | None = None) -> dict:
    """爬書水位母數。confirmed = READY = 已確認 z-lib 連結、未 owned 的解析池。
    **READY 現已 ⟹ trustworthy**（`status_of` 把 legacy 無 by 解析降級為 UNRESOLVED）→ confirmed
    天然只含現役演算法解出者，無須再分 confirmed_trustworthy。unresolved = State 1（待 agent 解析，
    含被降級的 legacy）。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    pr = progress(files, have, resolution)
    o = pr['overall']
    return {'confirmed': o[READY], 'ready': o[READY],
            'unresolved': o[UNRESOLVED], 'review': o[REVIEW], 'owned': o[OWNED],
            'absent': o[PUBLIC_ABSENT],  # 向後相容（下游 devctl/dev 讀此鍵；= not_found）
            'not_found': o[NOT_FOUND], 'version_unavailable': o[VERSION_UNAVAILABLE]}


def progress(files: list[dict] | None = None, have: set | None = None,
             resolution: dict | None = None, include_discovered: bool = True) -> dict:
    """各領域 + 整體的狀態統計。include_discovered：公開 catalog 計數傳 False（只人工正典）、
    /dev pool_counts 傳 True（含 discovered 機器候選，驅動 dispatch 水位）。"""
    rows = annotate(files, have, resolution, include_discovered=include_discovered)

    def tally(rs: list[dict]) -> dict:
        c = {s: 0 for s in STATUSES}
        for r in rs:
            c[r['status']] += 1
        c['total'] = len(rs)
        c['main'] = sum(1 for r in rs if r['kind'] == 'main')
        c[PUBLIC_ABSENT] = c[NOT_FOUND]  # 向後相容公開桶（=無法收錄）；version_unavailable 另計、屬待收錄
        return c

    by_field: dict[str, list] = {}
    for r in rows:
        by_field.setdefault(r['field'], []).append(r)
    return {'overall': tally(rows),
            'by_field': {f: tally(rs) for f, rs in by_field.items()}}


def catalog(files: list[dict] | None = None, have: set | None = None,
            resolution: dict | None = None) -> dict:
    """UI 收錄表結構：field → sublist → 主書（status + 解答本 sol_status）+ 各層統計。
    build 烤成 data/catalog.json 供 reader library 渲染收錄表。status 經 _public_status 摺疊成 UI 三態：
    owned/ready→已收錄或排隊中、absent（含內部 not_found）→無法收錄、review/unresolved（含內部
    version_unavailable 可重查）→待收錄（公開 UI 不分待裁/待解析/可重查，皆「待收錄」；細節只在
    /dev 面板顯示）。前端只見舊五態字串 → reader 零改。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    pr = progress(files, have, resolution, include_discovered=False)  # 公開收錄表計數只認人工正典（discovered 不列入公開檔）
    fields = []
    for f in files:
        subs = []
        for sl in (f.get('sublists') or []):
            books = []
            for b in (sl.get('books') or []):
                slug = b.get('slug', '')
                e = {'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
                     'status': _public_status(status_of(slug, have, resolution))}
                if b.get('edition_pref'):
                    e['edition_pref'] = b['edition_pref']
                if b.get('solution', True):
                    e['sol_status'] = _public_status(status_of(f'{slug}{SOL_SUFFIX}', have, resolution))
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
        ts0 = targets(include_discovered=False)  # validate 是人工正典 schema 驗證 → 計數只認人工正典
        print(f'✓ 書單通過（{len(ts0)} targets，{len([t for t in ts0 if t["kind"]=="main"])} 主書）')
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
              f'not_found {o[NOT_FOUND]} · recheck {o[VERSION_UNAVAILABLE]} · '
              f'review {o[REVIEW]} · unresolved {o[UNRESOLVED]}（主書 {o["main"]}）')
        for fld, c in pr['by_field'].items():
            print(f'  {fld:18} {c[OWNED]:>3}/{c["total"]:<3} 收錄  '
                  f'(ready {c[READY]} · unresolved {c[UNRESOLVED]} · not_found {c[NOT_FOUND]})')
        return 0
    if args.cmd == 'next':
        for b in select_next(args.n):
            print(f'{b["slug"]:40} id={b["id"]} hash={b["hash"]}')
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
