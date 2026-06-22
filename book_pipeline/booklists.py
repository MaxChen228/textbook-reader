#!/usr/bin/env python3
"""book_pipeline.booklists — 書目狀態衍生層（「合格存在」四維模型；universe = editions）。

[architect note — 真相源演進，務必內化]

舊模型：人工 booklists/*.json wishlist 是 universe（含「在書單、沒連結」的中間態）。**已廢**。
新模型（「合格存在」）：一本書「合格存在」⟺ 四維全過——① 夠格收錄 ② z-lib 有可下載連結 ③ 版本號確認
④（有解答本則）解答本與母書版本對齊。**沒連結/沒驗的書不存在於 universe**。三層真相：
  fields.json            領域骨架（人工、git）：領域顯示名 + 排序。
  editions/<slug>.json   **universe**——每本「存在」的書（owned ∪ 已連結 ∪ discovery 候選）的完整記錄：
                         身份 identity + 分類 classification + 四維結論（LLM agent / 遷移寫、git）。
  crawl_resolution.json  純連結快取 {status: found|not_found}（resolver、gitignore）= 維②。

**universe 切換（Phase 6）**：targets()/catalog() 改由 **editions 派生**（不再讀 booklists/*.json）→ 沒
editions 檔的書（舊 86 unresolved 無連結 + not_found）自然從收錄表消失，落實「沒連結＝不存在」。解答本
target 由主書 identity.has_solution 衍生（即使 _sol 尚無 editions、無連結＝CANDIDATE，resolver 去找）。
discovery 機器候選（discovered/，無 editions）併入 targets 工作母體、撞 slug 跳。

五態（衍生，無 runtime buffer）：
  OWNED      已在 inventory（實體在手，**保命最優先**，永不因未驗降級）
  QUALIFIED  四維全過、未 owned → 買書員下載候選（= 合格解析池）
  PENDING    有連結、但維③④①未全過（待 /restock 回查）
  CANDIDATE  無連結（resolver/restock 工作母體；含尚未查的 discovered/衍生 _sol）
  REJECTED   z-lib 真無（resolution not_found）∪ LLM 判不夠格（editions eligible=False）→ 無法收錄

公開摺疊（reader 收錄表零改）：_public_status 把五態折回舊字串——OWNED→owned、QUALIFIED/PENDING→ready
（皆有連結；reader shelfState 把 owned 以外全歸「待收錄」故不分）、CANDIDATE→unresolved、REJECTED→absent。

鐵律：版本/解答/夠格一律 LLM 親判（禁 regex 抽 title、禁字串比對當裁決）——本模組只讀判斷結果。
booklists/*.json 已移 booklists/_archive/（可逆封存）；load_files() 僅供遷移腳本讀封存，非 SoT。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import os
import re

from book_pipeline import editions as ed
from book_pipeline import fields as fields_mod
from book_pipeline import jsonio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BP = os.path.join(ROOT, 'book_pipeline')
# booklists/*.json 已退役為 SoT（universe 改 editions）→ 封存於 booklists/_archive/。load_files 指向封存，
# 僅供一次性遷移腳本（migrate_fields / migrate_booklists_to_editions）讀人工正典歷史；targets/catalog 不讀它。
BOOKLISTS_DIR = os.path.join(BP, 'booklists', '_archive')
RESOLUTION = os.path.join(BP, 'crawl_resolution.json')
DATA_DIR = os.path.join(BP, 'mineru_data')
RAW = os.path.join(ROOT, 'raw_pdfs')
SLUG_MAP = os.path.join(BP, 'slug_map.json')

SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')
SOL_SUFFIX = '_sol'

# PENDING 回查冷卻（防 busy-loop）：取代舊 version_unavailable 的 recheck_after。一本書親查過仍 PENDING
# （如偏好版次 z-lib 真的暫無），其 editions.checked_at 在此窗內 → daemon 暫不重派（resting）；窗到期 →
# 回工作母體重查。checked_at=None（從未親查，如遷入存量）→ 永遠 actionable（該查）。env 可調。
RECHECK_COOLDOWN_DAYS = int(os.environ.get('BOOK_PIPELINE_PENDING_RECHECK_DAYS', '30'))

# 衍生五態（見模組 docstring）。
OWNED = 'owned'
QUALIFIED = 'qualified'
PENDING = 'pending'
CANDIDATE = 'candidate'
REJECTED = 'rejected'
STATES = (OWNED, QUALIFIED, PENDING, CANDIDATE, REJECTED)

# resolution 純連結快取 status（維②；與五態不同層）。
LINK_FOUND = 'found'
LINK_NOT_FOUND = 'not_found'

# 公開層（reader 收錄表）只認舊字串，五態經此摺疊 → 前端零改。
PUBLIC_ABSENT = 'absent'
_PUBLIC = {OWNED: 'owned', QUALIFIED: 'ready', PENDING: 'ready',
           CANDIDATE: 'unresolved', REJECTED: PUBLIC_ABSENT}

_UNSET = object()


def _public_status(state: str) -> str:
    """五態 → reader 公開字串（QUALIFIED/PENDING→ready、CANDIDATE→unresolved、REJECTED→absent、OWNED→owned）。"""
    return _PUBLIC.get(state, 'unresolved')


# ── 封存讀取（僅遷移腳本用；非 SoT）───────────────────────────────────────────
def load_files(dirpath: str | None = None) -> list[dict]:
    """讀封存的 booklists/*.json（按 (order, field_id) 排序）。**非 universe**——universe 是 editions。
    僅供一次性遷移腳本（migrate_fields / migrate_booklists_to_editions）讀人工正典封存。targets()/catalog()
    不再呼叫此函式。封存後（booklists/ 移 _archive/）回 []。"""
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


# ── universe：targets 由 editions 派生 ───────────────────────────────────────
def _mk_target(slug, ident, fid, subject, base3, kind, of, source) -> dict:
    """組 target dict（field 顯示名 join fields.json；order 第一碼用 fields.json live 排序）。"""
    return {
        'slug': slug, 'title': ident.get('title', ''), 'author': ident.get('author', ''),
        'edition_pref': ident.get('edition_pref', ''),
        'field': fields_mod.name_of(fid), 'field_id': fid, 'subject': subject,
        'kind': kind, 'of': of, 'order': base3 + (0 if kind == 'main' else 1,), 'source': source,
    }


def targets(all_eds: dict | None = None, include_discovered: bool = True) -> list[dict]:
    """攤平 universe（editions 主書記錄）→ 有序 target 清單（含衍生解答本）。
    每主書 editions 記錄 → 一 main target；identity.has_solution → 緊接一 <slug>_sol target（即使 _sol 尚無
    editions/無連結＝CANDIDATE，resolver 去找）。order=(field_order〔fields.json live〕, subject_rank, book_rank,
    kind_rank)。include_discovered：併入 discovered 機器候選（無 editions、order 首碼 10000、撞 slug 跳）。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    out = []
    for slug, e in all_eds.items():
        if slug.endswith(SOL_SUFFIX):
            continue                                   # 解答本由其主書 has_solution 衍生（不獨立成 main）
        ident = e.get('identity') or {}
        cls = e.get('classification') or {}
        fid = cls.get('field_id', '')
        raw = cls.get('order') or (9999, 0, 0)
        base3 = (fields_mod.order_of(fid), raw[1] if len(raw) > 1 else 0, raw[2] if len(raw) > 2 else 0)
        out.append(_mk_target(slug, ident, fid, cls.get('subject', ''), base3, 'main', None,
                              ident.get('promoted_from') or 'editions'))
        if ident.get('has_solution'):
            sol_slug = f'{slug}{SOL_SUFFIX}'
            sol_ident = (all_eds.get(sol_slug) or {}).get('identity') or {}
            out.append(_mk_target(
                sol_slug,
                {'title': f"{ident.get('title', '')} — Solutions",
                 'author': sol_ident.get('author') or ident.get('author', ''),
                 'edition_pref': sol_ident.get('edition_pref') or ident.get('edition_pref', ''),
                 'promoted_from': sol_ident.get('promoted_from') or ident.get('promoted_from')},
                fid, cls.get('subject', ''), base3, 'solution', slug,
                sol_ident.get('promoted_from') or ident.get('promoted_from') or 'editions'))
    if include_discovered:
        out += _discovered_targets({t['slug'] for t in out})
    out.sort(key=lambda t: t['order'])
    return out


def _discovered_targets(seen: set) -> list[dict]:
    """discovery 機器候選（discovered/，尚無 editions）→ target（order 首碼 10000、撞 seen/彼此 slug 跳）。
    已被 /restock 寫過 editions 的 discovered 書已在 editions universe，不重複（seen 去重）。空 → []。"""
    from book_pipeline import discovered
    out = []
    for ci, c in enumerate(discovered.iter_candidates()):
        slug = c.get('slug', '')
        if not slug or slug in seen:
            continue
        seen.add(slug)
        fid, subj, base3 = c.get('field_id', ''), c.get('subject', ''), (10000, ci, 0)
        ident = {'title': c.get('title', ''), 'author': c.get('author', ''),
                 'edition_pref': c.get('edition_pref', ''), 'promoted_from': 'discovered'}
        out.append(_mk_target(slug, ident, fid, subj, base3, 'main', None, 'discovered'))
        if c.get('solution', True):
            sol_ident = {**ident, 'title': f"{c.get('title', '')} — Solutions"}
            out.append(_mk_target(f'{slug}{SOL_SUFFIX}', sol_ident, fid, subj, base3,
                                  'solution', slug, 'discovered'))
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
    """resolver 寫的純連結快取（gitignore，高頻）。entry：{status:'found', id, hash, …} 維②有連結 /
    {status:'not_found', …} z-lib 真無 → REJECTED。版本/解答/夠格在 editions（見 editions.py）。"""
    return jsonio.read_json(RESOLUTION, {}) or {}


def save_resolution(updates: dict) -> dict:
    """{slug: entry} 合併進 resolution 並原子寫（flock 互斥）。寫者 = resolver 的 `resolve commit`。"""
    import fcntl
    with open(RESOLUTION + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = load_resolution()
        cur.update(updates)
        jsonio.atomic_write_json(RESOLUTION, cur, indent=1)
        return cur


def is_trustworthy(entry: dict | None) -> bool:
    """resolution entry 是否由現役（agent-judged）演算法產出（有 `by` 戳記）。供下游/resolver 用。"""
    return bool(entry) and 'by' in entry


def status_of(slug: str, have: set, resolution: dict, edition=_UNSET) -> str:
    """衍生五態（**owned 保命最優先**）。維②讀 resolution.status=='found'；維①③④讀 editions
    （edition 注入：迴圈呼叫一次 ed.load_all 傳入；單次呼叫不傳 → 自 ed.load）。"""
    if slug in have:
        return OWNED
    e = ed.load(slug) if edition is _UNSET else edition
    r = resolution.get(slug) or {}
    st = r.get('status')
    if st == LINK_NOT_FOUND or (r.get('absent') and not st):
        return REJECTED
    if (((e or {}).get('qualification')) or {}).get('eligible') is False:
        return REJECTED
    if st == LINK_FOUND:
        return QUALIFIED if ed.qualifies(slug, e, resolution, have) else PENDING
    return CANDIDATE


# ── 下游 API：annotate / select_next / pending / candidate / progress ───────
def annotate(all_eds: dict | None = None, have: set | None = None,
             resolution: dict | None = None, include_discovered: bool = True) -> list[dict]:
    """每個 target 附 `state`（五態）。all_eds 一次 load_all 供 universe + 狀態共用。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    rows = []
    for t in targets(all_eds, include_discovered=include_discovered):
        r = dict(t)
        r['state'] = status_of(t['slug'], have, resolution, all_eds.get(t['slug']))
        rows.append(r)
    return rows


def select_next(n: int, all_eds: dict | None = None, have: set | None = None,
                resolution: dict | None = None, exclude: set | None = None) -> list[dict]:
    """**確定性下載候選**：status==QUALIFIED（四維全過）的 target，按序取前 n → [{slug,id,hash,title}]。
    **零 LLM**。只下載合格書（PENDING 有連結未驗不下載，等 /restock 升 QUALIFIED）。exclude=失敗達上限。"""
    n = max(0, int(n))
    if n == 0:
        return []
    all_eds = ed.load_all() if all_eds is None else all_eds
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    exclude = exclude or set()
    picks = []
    for t in targets(all_eds):
        if len(picks) >= n:
            break
        if t['slug'] in exclude:
            continue
        if status_of(t['slug'], have, resolution, all_eds.get(t['slug'])) != QUALIFIED:
            continue
        r = resolution[t['slug']]
        bid, bhash = r.get('id'), r.get('hash')
        if not (isinstance(bid, (str, int)) and isinstance(bhash, (str, int))):
            continue
        picks.append({'slug': t['slug'], 'id': str(bid), 'hash': str(bhash),
                      'title': r.get('title') or t['title']})
    return picks


def _targets_in_state(state: str, all_eds=None, have=None, resolution=None) -> list[dict]:
    all_eds = ed.load_all() if all_eds is None else all_eds
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    return [t for t in targets(all_eds)
            if status_of(t['slug'], have, resolution, all_eds.get(t['slug'])) == state]


def unresolved_targets(all_eds=None, have=None, resolution=None) -> list[dict]:
    """status==CANDIDATE 的 target（無連結；resolver/restock 工作母體，含衍生 _sol / discovered）。"""
    return _targets_in_state(CANDIDATE, all_eds, have, resolution)


def _pending_resting(ed_rec: dict | None, now: _dt.datetime) -> bool:
    """PENDING 書是否在 recheck cooldown 內 resting（近期已親查、暫不重派，防 busy-loop）。
    checked_at None（從未親查）→ 不 resting（該查）；時戳壞 → 保守不 resting。"""
    ca = (ed_rec or {}).get('checked_at')
    if not ca:
        return False
    try:
        t = _dt.datetime.fromisoformat(ca)
    except (ValueError, TypeError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return (now - t) < _dt.timedelta(days=RECHECK_COOLDOWN_DAYS)


def pending_targets(all_eds=None, have=None, resolution=None, now=None) -> list[dict]:
    """**actionable** PENDING target（有連結但維③④①未全過 → /restock 存量回查母體）。排除 recheck
    cooldown 內 resting 者（近期親查過仍 PENDING＝偏好版暫無 → 暫不重派，防 busy-loop；窗到期自動回母體）。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return [t for t in _targets_in_state(PENDING, all_eds, have, resolution)
            if not _pending_resting(all_eds.get(t['slug']), now)]


def crawl_work_remaining(all_eds=None, have=None, resolution=None) -> int:
    """庫存查證工作母體大小 = CANDIDATE（無連結）+ actionable PENDING（排除 cooldown resting）。
    供互動 /restock（使用者親自 fan-out 填書單）與 `resolve queue` / devctl 面板讀取——**非 daemon**
    （daemon 已不自主 resolve）。resting 的 PENDING 窗到期後自動回母體 → 週期性可重查（有界）。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    return (len(unresolved_targets(all_eds, have, resolution))
            + len(pending_targets(all_eds, have, resolution)))


def pool_counts(all_eds=None, have=None, resolution=None) -> dict:
    """爬書水位母數（向後相容鍵 + 新鍵）。confirmed/ready/qualified_ready=QUALIFIED；unresolved/candidate
    =CANDIDATE；pending=PENDING。"""
    o = progress(all_eds, have, resolution)['overall']
    return {'confirmed': o[QUALIFIED], 'ready': o[QUALIFIED], 'qualified_ready': o[QUALIFIED],
            'pending': o[PENDING], 'unresolved': o[CANDIDATE], 'candidate': o[CANDIDATE],
            'owned': o[OWNED], 'rejected': o[REJECTED], 'absent': o[REJECTED],
            'not_found': o[REJECTED], 'review': 0, 'version_unavailable': 0}


def progress(all_eds=None, have=None, resolution=None, include_discovered: bool = True) -> dict:
    """各領域 + 整體的五態統計（含向後相容公開鍵）。include_discovered：公開 catalog 計數傳 False。"""
    rows = annotate(all_eds, have, resolution, include_discovered=include_discovered)

    def tally(rs: list[dict]) -> dict:
        c = {s: 0 for s in STATES}
        for r in rs:
            c[r['state']] += 1
        c['total'] = len(rs)
        c['main'] = sum(1 for r in rs if r['kind'] == 'main')
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


def catalog(all_eds=None, have=None, resolution=None) -> dict:
    """UI 收錄表結構：field → sublist → 主書（公開 status + 解答本 sol_status）+ 各層統計。
    universe = editions（沒 editions 的書不在收錄表→落實「沒連結＝不存在」）；領域順序/顯示名 join fields.json。
    狀態經 _public_status 摺疊成 UI 三態 → reader 零改。build 烤成 data/catalog.json。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    have = have_slugs() if have is None else have
    resolution = load_resolution() if resolution is None else resolution
    pr = progress(all_eds, have, resolution, include_discovered=False)

    def pub(slug):
        return _public_status(status_of(slug, have, resolution, all_eds.get(slug)))

    # 公開收錄表只認 editions universe（discovered 不列入公開檔，與舊「只認人工正典」等義）
    mains = [t for t in targets(all_eds, include_discovered=False) if t['kind'] == 'main']
    # field_id → {field, order}；按 fields.json 排序，未知 field_id 殿後
    flist = fields_mod.load()
    forder = {f['field_id']: i for i, f in enumerate(flist)}
    fname = {f['field_id']: f['field'] for f in flist}
    # 分組：field_id → subject → [main target]（保 targets 內既有序＝classification.order）
    grouped: dict[str, dict[str, list]] = {}
    for t in mains:
        grouped.setdefault(t['field_id'], {}).setdefault(t['subject'], []).append(t)
    fields_out = []
    for fid in sorted(grouped, key=lambda x: (forder.get(x, 9999), x)):
        subs = []
        for subj, ts in grouped[fid].items():
            books = []
            for t in ts:
                slug = t['slug']
                ident = (all_eds.get(slug) or {}).get('identity') or {}
                e = {'slug': slug, 'title': t['title'], 'author': t['author'], 'status': pub(slug)}
                if t.get('edition_pref'):
                    e['edition_pref'] = t['edition_pref']
                if ident.get('has_solution'):
                    e['sol_status'] = pub(f'{slug}{SOL_SUFFIX}')
                books.append(e)
            subs.append({'name': subj, 'books': books})
        fields_out.append({'field': fname.get(fid, fid), 'field_id': fid,
                           'order': forder.get(fid, 9999), 'sublists': subs,
                           'stats': pr['by_field'].get(fname.get(fid, fid), {})})
    return {'fields': fields_out, 'overall': pr['overall']}


# ── 驗證 / 對賬（assembly 與 CI 用，不自動改 SoT）──────────────────────────
def validate(all_eds: dict | None = None) -> list[str]:
    """editions universe 完整性：slug 合法/唯一、identity/classification 必備、主書 slug 不以 _sol 結尾、
    解答本記錄的母書須存在。回錯誤清單（空=通過）。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    errs = []
    for slug, e in all_eds.items():
        if not SLUG_RE.match(slug):
            errs.append(f'slug 不合法（須 [a-z0-9_]{{1,64}}）：{slug!r}')
            continue
        ident = e.get('identity') or {}
        cls = e.get('classification') or {}
        if not ident.get('title'):
            errs.append(f'{slug}: editions 缺 identity.title')
        if not ident.get('author'):
            errs.append(f'{slug}: editions 缺 identity.author')
        if not cls.get('field_id'):
            errs.append(f'{slug}: editions 缺 classification.field_id')
        if slug.endswith(SOL_SUFFIX):
            parent = slug[:-len(SOL_SUFFIX)]
            if parent not in all_eds:
                errs.append(f'{slug}: 解答本記錄的母書 {parent} 無 editions（孤兒）')
    return errs


def reconcile_owned(all_eds: dict | None = None, have: set | None = None) -> dict:
    """inventory ↔ universe 對賬：
      inventory_not_in_universe：inventory 有主書但 editions 無記錄 → **異常**（owned 必有 editions）。
      owned_sol_not_in_universe：inventory 有解答本但其主書 identity.has_solution≠true（未衍生該目標）。
      in_universe_not_inventory：editions 有主書、inventory 無 → 正常（待收錄缺口），僅供概覽。"""
    all_eds = ed.load_all() if all_eds is None else all_eds
    have = have_slugs() if have is None else have
    ts = targets(all_eds)
    main_slugs = {t['slug'] for t in ts if t['kind'] == 'main'}
    sol_targets = {t['slug'] for t in ts if t['kind'] == 'solution'}
    have_main = {s for s in have if not s.endswith(SOL_SUFFIX)}
    have_sol = {s for s in have if s.endswith(SOL_SUFFIX)}
    return {'inventory_not_in_sot': sorted(have_main - main_slugs),
            'owned_sol_not_in_sot': sorted(have_sol - sol_targets),
            'in_sot_not_inventory': sorted(main_slugs - have)}


# ── CLI（ops / dry-run / CI）───────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description='書目狀態衍生 SoT 工具（合格存在五態，universe=editions）')
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
        print(f'✓ editions universe 通過（{len(ts0)} targets，{len([t for t in ts0 if t["kind"]=="main"])} 主書）')
        return 0
    if args.cmd == 'reconcile':
        r = reconcile_owned()
        bad, badsol = r['inventory_not_in_sot'], r['owned_sol_not_in_sot']
        print(f'inventory 有但 editions 無記錄（{len(bad)}）：{" ".join(bad) or "（無，✓）"}')
        print(f'owned 題本但主書 has_solution≠true（{len(badsol)}）：{" ".join(badsol) or "（無，✓）"}')
        print(f'editions 有但尚未收錄（{len(r["in_sot_not_inventory"])}，待收錄缺口）')
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
