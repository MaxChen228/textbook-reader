#!/usr/bin/env python3
"""book_pipeline.booklists — 書目狀態衍生層（「合格存在」四維模型，2026-06 重構後的 shim）。

[architect note — Phase 3 真相源切換，務必內化]

舊模型：booklists/*.json wishlist 是唯一 SoT，狀態 = join(inventory, resolution) 的六態。
新模型（「合格存在」）：一本書「合格存在」⟺ 四維全過——① 夠格收錄 ② z-lib 有可下載連結
③ 版本號確認 ④（有解答本則）解答本與母書版本對齊。任一未驗 = 不算數。真相分三層：
  fields.json            領域骨架（人工、git）
  editions/<slug>.json   每本合格書的完整記錄：身份 + 分類 + 四維結論（LLM agent + 遷移、git）
  crawl_resolution.json  純連結快取 {status: found|not_found, id, hash, …}（resolver、gitignore）

**本模組現為 shim**：對外 API（catalog/select_next/pool_counts/progress/…）簽名與回傳形狀全不變、
下游（build/bake_json、devctl、pipeline_tick）零感知；但**狀態真相已切到 editions + resolution**。
status_of 由六態收斂成五態（衍生，無 runtime buffer）：
  OWNED      已在 inventory（實體在手，**保命最優先**，永不因未驗降級）
  QUALIFIED  四維全過、未 owned → 買書員下載候選（= 真正的解析池）
  PENDING    有連結、但維③④或①未全過（待 /restock 回查；存量遷入的 100 本 ready 多落此）
  CANDIDATE  無連結（resolver/restock 工作母體）
  REJECTED   z-lib 真無（resolution not_found）∪ LLM 判不夠格（editions eligible=False）→ 無法收錄

公開摺疊（reader 收錄表零改）：_public_status 把五態折回舊公開字串——OWNED→owned、QUALIFIED/PENDING
→ready（皆有連結；reader shelfState 把 owned 以外全歸「待收錄」故 QUALIFIED/PENDING 不分）、CANDIDATE
→unresolved、REJECTED→absent。reader 仍只見舊字串 → index.html 零改。

[Phase 3 邊界——保守等價] catalog 仍以 booklists/*.json 為**結構骨架**（領域/子單/書序、universe=607），
只把每本的「狀態」改由 editions+resolution 衍生 → data/catalog.json 與切換前位元等價（驗收過）。把
universe 切到 editions（沒連結的書消失）、editions.catalog() 接管、廢 booklists/*.json 是 **Phase 6**
的「顯式切換」。版本/解答/夠格一律 LLM 親判（禁 regex 抽 title、禁字串比對當裁決）——本模組只讀判斷結果。
"""
from __future__ import annotations

import argparse
import glob
import os
import re

from book_pipeline import editions as ed
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

# 衍生五態（見模組 docstring）。
OWNED = 'owned'
QUALIFIED = 'qualified'    # 四維全過、未 owned → 買書員下載候選
PENDING = 'pending'        # 有連結、維③④①未全過 → /restock 回查母體
CANDIDATE = 'candidate'    # 無連結 → resolver/restock 工作母體
REJECTED = 'rejected'      # z-lib 真無 ∪ 判不夠格 → 無法收錄
STATES = (OWNED, QUALIFIED, PENDING, CANDIDATE, REJECTED)

# resolution 純連結快取的 status 值（與五態不同層；維②連結事實）。
LINK_FOUND = 'found'
LINK_NOT_FOUND = 'not_found'

# 公開層（reader 收錄表）只認舊字串，五態經 _public_status 摺疊 → 前端零改。
PUBLIC_ABSENT = 'absent'   # 對外「無法收錄」（REJECTED 的公開名）
_PUBLIC = {OWNED: 'owned', QUALIFIED: 'ready', PENDING: 'ready',
           CANDIDATE: 'unresolved', REJECTED: PUBLIC_ABSENT}

_UNSET = object()          # status_of 的 edition 哨兵（區分「未傳→自 load」與「傳了 None＝無檔」）


def _public_status(state: str) -> str:
    """五態 → reader 收錄表公開字串（QUALIFIED/PENDING→ready〔皆有連結〕、CANDIDATE→unresolved、
    REJECTED→absent、OWNED→owned）。reader shelfState 再把 owned 以外全歸「待收錄」。"""
    return _PUBLIC.get(state, 'unresolved')


# ── 載入 SoT 骨架（Phase 3：universe 仍取自 booklists/*.json；Phase 6 切 editions）──────────────
def load_files(dirpath: str | None = None) -> list[dict]:
    """讀 booklists/*.json，按 (order, field_id) 排序。容錯：壞檔/非書單檔跳過。每檔附 `_path`。"""
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
    """攤平骨架 → 有序 target 清單（含衍生解答本）。每筆：{slug,title,author,edition_pref,field,
    field_id,subject,kind,of,order,source}，按 order 排序。order=(field_order, sublist_idx, book_idx,
    kind_rank)。include_discovered：合併 discovery 機器候選（排人工正典後 order 首碼 10000、撞 slug 跳）。"""
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
    """discovery 機器候選 → target（排人工正典後 order 首碼 10000、撞 slug 跳、標 source='discovered'）。"""
    from book_pipeline import discovered
    out = []
    for ci, c in enumerate(discovered.iter_candidates()):
        slug = c.get('slug', '')
        if not slug or slug in manual_slugs:
            continue
        manual_slugs.add(slug)
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


# ── inventory / resolution ────────────────────────────────────────────────
def have_slugs() -> set:
    """已存在、不再爬的 slug：mineru_data/* 任何書（含 _sol、in-flight）∪ raw_pdfs 內合法 slug 檔名者。"""
    have = set()
    for p in glob.glob(os.path.join(DATA_DIR, '*')):
        if os.path.isdir(p):
            have.add(os.path.basename(p))
    sm = (jsonio.read_json(SLUG_MAP, {}) or {}).get('map', {})
    for p in glob.glob(os.path.join(RAW, '*.pdf')):
        fn = os.path.basename(p)
        slug = sm.get(fn)
        if not slug:
            base = fn[:-4]
            slug = base if SLUG_RE.match(base) else None
        if slug:
            have.add(slug)
    return have


def load_resolution() -> dict:
    """resolver 寫的純連結快取（gitignore，高頻 enrich）。新模型 entry 形態：
      {status:'found', id, hash, title, href, cover, by, at}   維②有可下載連結
      {status:'not_found', at}                                 z-lib 真無 → REJECTED（查無記憶）
      （legacy_status 欄＝舊 review/version_unavailable 退化殘留，新碼忽略、僅可逆 breadcrumb。）
    版本/解答/夠格判斷不在此——在 git 追蹤的 editions/<slug>.json（見 editions.py）。"""
    return jsonio.read_json(RESOLUTION, {}) or {}


def save_resolution(updates: dict) -> dict:
    """把 {slug: entry} 合併進 resolution 並原子寫（flock 互斥）。寫者 = resolver 的 `resolve commit`。"""
    import fcntl
    with open(RESOLUTION + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = load_resolution()
        cur.update(updates)
        jsonio.atomic_write_json(RESOLUTION, cur, indent=1)
        return cur


def is_trustworthy(entry: dict | None) -> bool:
    """resolution entry 是否由現役（agent-judged）演算法產出（有 `by` 戳記）。保留供下游/resolver 用。"""
    return bool(entry) and 'by' in entry


def status_of(slug: str, have: set, resolution: dict, edition=_UNSET) -> str:
    """衍生五態（**owned 保命最優先**）。維②連結讀 resolution.status=='found'；維①③④讀 editions
    （edition 注入：迴圈呼叫者一次 ed.load_all 傳入；單次呼叫不傳 → 自 ed.load）。"""
    if slug in have:
        return OWNED                                  # 實體在手：永不因未驗降級
    e = ed.load(slug) if edition is _UNSET else edition
    r = resolution.get(slug) or {}
    st = r.get('status')
    if st == LINK_NOT_FOUND or (r.get('absent') and not st):
        return REJECTED                               # z-lib 真無（顯式 not_found 或 legacy absent）
    if (((e or {}).get('qualification')) or {}).get('eligible') is False:
        return REJECTED                               # LLM 判不夠格
    if st == LINK_FOUND:
        return QUALIFIED if ed.qualifies(slug, e, resolution, have) else PENDING
    return CANDIDATE                                  # 無連結 → 工作母體


# ── 下游 API：annotate / select_next / pending / candidate / progress ───────
def annotate(files: list[dict] | None = None, have: set | None = None,
             resolution: dict | None = None, include_discovered: bool = True,
             all_eds: dict | None = None) -> list[dict]:
    """每個 target 附 `state`（五態）。參數可注入（測試）。all_eds 一次 load_all 供迴圈共用。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    all_eds = ed.load_all() if all_eds is None else all_eds
    rows = []
    for t in targets(files, include_discovered=include_discovered):
        r = dict(t)
        r['state'] = status_of(t['slug'], have, resolution, all_eds.get(t['slug']))
        rows.append(r)
    return rows


def select_next(n: int, files: list[dict] | None = None, have: set | None = None,
                resolution: dict | None = None, exclude: set | None = None,
                all_eds: dict | None = None) -> list[dict]:
    """**確定性下載候選**：status==QUALIFIED（四維全過）的 target，按書單序取前 n →
    [{slug,id,hash,title}]（買書員直接下載）。**零 LLM**。exclude = 下載失敗達上限該排除的 slug。
    新模型只下載合格書（有連結但未驗版本的 PENDING 書不下載，等 /restock 親查後升 QUALIFIED）。"""
    n = max(0, int(n))
    if n == 0:
        return []
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    all_eds = ed.load_all() if all_eds is None else all_eds
    exclude = exclude or set()
    picks = []
    for t in targets(files):
        if len(picks) >= n:
            break
        if t['slug'] in exclude:
            continue
        if status_of(t['slug'], have, resolution, all_eds.get(t['slug'])) != QUALIFIED:
            continue
        r = resolution[t['slug']]
        bid, bhash = r.get('id'), r.get('hash')
        if not (isinstance(bid, (str, int)) and isinstance(bhash, (str, int))):
            continue                                  # 畸形 id/hash（list/dict）→ 跳過防 fetch URL 污染
        picks.append({'slug': t['slug'], 'id': str(bid), 'hash': str(bhash),
                      'title': r.get('title') or t['title']})
    return picks


def _targets_in_state(state: str, files=None, have=None, resolution=None, all_eds=None) -> list[dict]:
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    all_eds = ed.load_all() if all_eds is None else all_eds
    return [t for t in targets(files)
            if status_of(t['slug'], have, resolution, all_eds.get(t['slug'])) == state]


def unresolved_targets(files=None, have=None, resolution=None, all_eds=None) -> list[dict]:
    """status==CANDIDATE 的 target（書單序）——無連結、resolver/restock 工作母體。"""
    return _targets_in_state(CANDIDATE, files, have, resolution, all_eds)


def pending_targets(files=None, have=None, resolution=None, all_eds=None) -> list[dict]:
    """status==PENDING 的 target（書單序）——有連結但維③④①未全過 → /restock 存量回查母體。"""
    return _targets_in_state(PENDING, files, have, resolution, all_eds)


def pool_counts(files=None, have=None, resolution=None, all_eds=None) -> dict:
    """爬書水位母數（向後相容鍵 + 新鍵）。confirmed/ready/qualified_ready = QUALIFIED（合格解析池）；
    unresolved = CANDIDATE（工作母體）；pending = PENDING（待回查）。"""
    o = progress(files, have, resolution, all_eds=all_eds)['overall']
    return {'confirmed': o[QUALIFIED], 'ready': o[QUALIFIED], 'qualified_ready': o[QUALIFIED],
            'pending': o[PENDING], 'unresolved': o[CANDIDATE], 'candidate': o[CANDIDATE],
            'owned': o[OWNED], 'rejected': o[REJECTED], 'absent': o[REJECTED],
            'not_found': o[REJECTED], 'review': 0, 'version_unavailable': 0}


def progress(files=None, have=None, resolution=None, include_discovered: bool = True,
             all_eds=None) -> dict:
    """各領域 + 整體的五態統計（含向後相容公開鍵）。include_discovered：公開 catalog 計數傳 False。"""
    rows = annotate(files, have, resolution, include_discovered=include_discovered, all_eds=all_eds)

    def tally(rs: list[dict]) -> dict:
        c = {s: 0 for s in STATES}
        for r in rs:
            c[r['state']] += 1
        c['total'] = len(rs)
        c['main'] = sum(1 for r in rs if r['kind'] == 'main')
        # 向後相容公開鍵（/dev 舊渲染 + CLI + pool_counts）
        c['ready'] = c[QUALIFIED]
        c['unresolved'] = c[CANDIDATE]
        c['not_found'] = c[REJECTED]
        c[PUBLIC_ABSENT] = c[REJECTED]
        c['review'] = 0
        c['version_unavailable'] = 0
        return c

    by_field: dict[str, list] = {}
    for r in rows:
        by_field.setdefault(r['field'], []).append(r)
    return {'overall': tally(rows),
            'by_field': {f: tally(rs) for f, rs in by_field.items()}}


def catalog(files=None, have=None, resolution=None, all_eds=None) -> dict:
    """UI 收錄表結構：field → sublist → 主書（公開 status + 解答本 sol_status）+ 各層統計。
    build 烤成 data/catalog.json 供 reader library 渲染。狀態經 _public_status 摺疊成 UI 三態
    （owned/ready→已收錄或排隊、absent→無法收錄、unresolved→待收錄）。前端只見舊字串 → reader 零改。
    [Phase 3] 結構骨架仍取自 booklists/*.json（universe 等價），狀態真相由 editions+resolution 衍生。"""
    files = load_files() if files is None else files
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    all_eds = ed.load_all() if all_eds is None else all_eds
    pr = progress(files, have, resolution, include_discovered=False, all_eds=all_eds)

    def pub(slug):
        return _public_status(status_of(slug, have, resolution, all_eds.get(slug)))

    fields = []
    for f in files:
        subs = []
        for sl in (f.get('sublists') or []):
            books = []
            for b in (sl.get('books') or []):
                slug = b.get('slug', '')
                e = {'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
                     'status': pub(slug)}
                if b.get('edition_pref'):
                    e['edition_pref'] = b['edition_pref']
                if b.get('solution', True):
                    e['sol_status'] = pub(f'{slug}{SOL_SUFFIX}')
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
    """SoT ↔ inventory 對賬（inventory_not_in_sot / owned_sol_not_in_sot 為異常、in_sot_not_inventory 正常）。"""
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
    ap = argparse.ArgumentParser(description='書目狀態衍生 SoT 工具（合格存在五態）')
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
        ts0 = targets(include_discovered=False)
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
        print(f'整體：{o[OWNED]}/{o["total"]} 收錄 · qualified {o[QUALIFIED]} · pending {o[PENDING]} · '
              f'candidate {o[CANDIDATE]} · rejected {o[REJECTED]}（主書 {o["main"]}）')
        for fld, c in pr['by_field'].items():
            print(f'  {fld:18} {c[OWNED]:>3}/{c["total"]:<3} 收錄  '
                  f'(qualified {c[QUALIFIED]} · pending {c[PENDING]} · candidate {c[CANDIDATE]} · rejected {c[REJECTED]})')
        return 0
    if args.cmd == 'next':
        for b in select_next(args.n):
            print(f'{b["slug"]:40} id={b["id"]} hash={b["hash"]}')
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
