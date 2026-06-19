#!/usr/bin/env python3
"""book_pipeline.status — 每本書在 pipeline 的真實階段（單一真相，取代瞎猜）。

讀「實際資料」而非檔名臆測，避免歷史教訓：parsed/ 是 gitignore（git 看不到）、
problems 是 ch.json 的獨立鍵（非 body 內 block）、sol_extract 產物只在 parsed 內。

階段（依序）：
  待ingest   raw_pdfs/<file>.pdf 在、unified 未產（待本地一條龍 ingest）
  OCR處理中 _pending_batches.json 有此 slug（已 PUT、unified 未組，雲端 OCR 進行中）→ 收割即完成
  ingest    unified/content_list.json
  audit     extract_rules.yaml
  parse     parsed/book.json
  sol       parsed/ch*.json 的 problems[].solution 填充（需有 <slug>_sol 解答本）
  translate parsed/*.zh.json

todo 欄是「中性動詞」（ingest/audit/parse/sol_extract/translate），
不綁特定 skill 名——/book-pipeline 讀此欄映射到對應 reference 流程。

用法：
  uv run python -m book_pipeline.status            # 全表 + 待辦（pipeline dashboard）
  uv run python -m book_pipeline.status <slug>     # 單本細節

時間段 cohort / 卡關清單 / 單書時間線 / session 全對話 → 統一走 `book_pipeline.trace`
（本檔只管「當下階段 frontier」；trace 組合本檔的 _entry_ts/_stuck_reason/assess 做回溯）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

from book_pipeline.catalog_audit import audit_catalog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'book_pipeline', 'mineru_data')


def _exists(slug: str, *parts: str) -> bool:
    return os.path.exists(os.path.join(DATA, slug, *parts))


def _deployed(slug: str) -> bool:
    """已上站 = data/<slug>/book.json 已烤出（build_all 產物）。"""
    return os.path.exists(os.path.join(ROOT, 'data', slug, 'book.json'))


def _pstate() -> dict:
    """讀 pipeline_state.json（不快取：長駐 daemon 內 state 會被 mark_* 改寫，快取會讀到舊值
    → 自看不見剛寫的 catalog_accepted/qc，重新 churn。cohort 路徑一次載入後往下傳，不逐 slug 重讀）。"""
    try:
        return json.load(open(os.path.join(ROOT, 'book_pipeline', 'pipeline_state.json'))) or {}
    except Exception:
        return {}


# 入庫時間戳：first_seen_at 是 durable 單一真相（每 observe idempotent 蓋、見 pipeline_queue），
# 補登後每書恆有 → 零缺口。舊戳（deployed_at/階段戳）僅作 first_seen 尚未補上時的 fallback。
_TS_FALLBACK = ('first_seen_at', 'deployed_at', 'catalog_llm_at')


def _entry_ts(slug: str, state: dict | None = None) -> datetime | None:
    """回傳該書入庫時間（aware UTC datetime）：first_seen_at 優先，無則退階段戳最早值。"""
    e = (state or _pstate()).get(slug) or {}
    if isinstance(e.get('first_seen_at'), str):
        try:
            d = datetime.fromisoformat(e['first_seen_at'])
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    cands = [e[k] for k in _TS_FALLBACK[1:] if isinstance(e.get(k), str)]
    m = e.get('math')
    if isinstance(m, dict) and isinstance(m.get('at'), str):
        cands.append(m['at'])
    out = []
    for s in cands:
        try:
            d = datetime.fromisoformat(s)
            out.append(d if d.tzinfo else d.replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    return min(out) if out else None


def _catalog_accepted(slug: str) -> bool:
    """catalog 殘留已 accept（det+LLM 修完仍殘、源頭缺不可修）→ 不再當強制待辦。"""
    return bool((_pstate().get(slug) or {}).get('catalog_accepted'))


def sol_stats(slug: str) -> tuple[int, int]:
    """回傳 (problem 總數, 有 solution 數)。schema：ch*.json/app*.json 的 problems 鍵。"""
    tot = sol = 0
    for chf in glob.glob(f'{DATA}/{slug}/parsed/ch*.json') + glob.glob(f'{DATA}/{slug}/parsed/app*.json'):
        if '.zh.' in chf:
            continue
        try:
            blocks = json.load(open(chf))
        except Exception:
            continue
        for pr in blocks.get('problems', []):
            tot += 1
            if pr.get('solution'):
                sol += 1
    return tot, sol


def _sol_pending(slug: str) -> bool:
    """sol 本標記 _pending（主書品質不足，不該 merge）→ 不再提示 sol_extract。"""
    p = os.path.join(DATA, f'{slug}_sol', 'sol_rules.yaml')
    if not os.path.exists(p):
        return False
    try:
        import yaml
        return bool((yaml.safe_load(open(p)) or {}).get('_pending'))
    except Exception:
        return False


# catalog critical 計數快取：audit_catalog 是重活（全文 regex + 逐圖存在性檢查，~0.23s/書），
# 但輸入（catalogs.json / content_list.json / catalog_overrides）多數 observe 間不變。以這三者
# 的 (mtime_ns, size) 為指紋，磁碟持久化 slug→(指紋,critical)：長駐 controller 重觀測與 60s
# devsnapshot 皆 warm-hit → build_queue 從 ~14s 塌到 <1s（僅剛變動的書重算）。指紋變即自動失效。
_CRIT_CACHE_PATH = os.path.join(ROOT, 'book_pipeline', '.catalog_crit_cache.json')
_crit_cache: dict | None = None


def _catalog_fingerprint(slug: str) -> list:
    sig = []
    for p in (os.path.join(DATA, slug, 'parsed', 'catalogs.json'),
              os.path.join(DATA, slug, 'unified', 'content_list.json'),
              os.path.join(ROOT, 'book_pipeline', 'catalog_overrides', f'{slug}.json')):
        try:
            st = os.stat(p)
            sig.append([st.st_mtime_ns, st.st_size])
        except OSError:
            sig.append([0, 0])
    return sig


def _catalog_critical(slug: str) -> int:
    """Return catalog semantic critical count without writing audit reports（指紋快取）。"""
    if not _exists(slug, 'parsed', 'catalogs.json'):
        return 0
    global _crit_cache
    if _crit_cache is None:
        try:
            _crit_cache = json.load(open(_CRIT_CACHE_PATH)) or {}
        except Exception:
            _crit_cache = {}
    fp = _catalog_fingerprint(slug)
    hit = _crit_cache.get(slug)
    if hit and hit.get('fp') == fp:
        return hit['crit']
    try:
        crit = int(audit_catalog(slug, write_report=False).get('critical') or 0)
    except Exception:
        crit = 1
    _crit_cache[slug] = {'fp': fp, 'crit': crit}
    try:  # 原子寫；多進程競寫最壞=覆蓋彼此更新，下個 cycle 自癒（derived 值，低風險）
        tmp = _CRIT_CACHE_PATH + f'.tmp{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump(_crit_cache, f)
        os.replace(tmp, _CRIT_CACHE_PATH)
    except Exception:
        pass
    return crit


def _math_residual(slug: str):
    """讀 _math_report.json 殘餘 bad_occ（None=未驗/skip/缺）。track-only 資訊，不 gate。
    status 是 pipeline_queue 的下層、不能 import 它，故直接讀檔（非經 state）。"""
    p = os.path.join(DATA, slug, 'parsed', '_math_report.json')
    try:
        r = json.load(open(p))
    except Exception:
        return None
    if r.get('status') == 'skipped':
        return None
    return int(r.get('stats', {}).get('bad_occ') or 0)


def _load_pending() -> set:
    """_pending_batches.json 內已 submit、等 receiver poll 的 slug。"""
    p = os.path.join(ROOT, 'book_pipeline', '_pending_batches.json')
    try:
        return {e['slug'] for e in (json.load(open(p)) or [])}
    except Exception:
        return set()


def _raw_slug_map() -> dict:
    """raw_pdfs/ 實際存在的 PDF → slug（查 slug_map.json）。雲端無 raw_pdfs 時回空。"""
    p = os.path.join(ROOT, 'book_pipeline', 'slug_map.json')
    try:
        m = (json.load(open(p)) or {}).get('map', {})
    except Exception:
        m = {}
    out = {}
    for fn, slug in m.items():
        if os.path.exists(os.path.join(ROOT, 'raw_pdfs', fn)):
            out[slug] = fn
    return out


def raw_slug_map() -> dict:
    """Public wrapper for tools that need the raw PDF registry."""
    return _raw_slug_map()


def all_slugs(pending: set | None = None, raw: dict | None = None) -> list[str]:
    """Return all main-book slugs visible to the pipeline dashboard."""
    pending = pending if pending is not None else _load_pending()
    raw = raw if raw is not None else _raw_slug_map()
    data_slugs = {os.path.basename(p.rstrip('/'))
                  for p in glob.glob(f'{DATA}/*/') if not os.path.basename(p.rstrip('/')).endswith('_sol')}
    slugs = (data_slugs | set(raw) | pending)
    return sorted(s for s in slugs if not s.endswith('_sol'))


def assess(slug: str, pending: set = frozenset(), raw: dict = None) -> dict:
    raw = raw or {}
    has_sol_book = _exists(f'{slug}_sol', 'unified', 'content_list.json')
    has_zh = bool(glob.glob(f'{DATA}/{slug}/parsed/*.zh.json'))
    if not _exists(slug, 'unified', 'content_list.json'):
        # 前置三態皆待本地一條龍 ingest（動詞統一）；階段名保留診斷區分
        if slug in pending:
            return {'slug': slug, 'stage': '0.5 OCR處理中', 'todo': 'ingest', 'sol_book': has_sol_book}
        if slug in raw:
            return {'slug': slug, 'stage': '0 待ingest', 'todo': 'ingest', 'sol_book': has_sol_book}
        return {'slug': slug, 'stage': 'X 未ingest', 'todo': 'ingest', 'sol_book': has_sol_book}
    if not _exists(slug, 'extract_rules.yaml'):
        return {'slug': slug, 'stage': '1 待audit', 'todo': 'audit', 'sol_book': has_sol_book}
    if not _exists(slug, 'parsed', 'book.json'):
        return {'slug': slug, 'stage': '2 待parse', 'todo': 'parse', 'sol_book': has_sol_book}
    tot, sol = sol_stats(slug)
    todo = []
    catalog_critical = _catalog_critical(slug)
    if catalog_critical:
        # catalog_audit 只在「上站前」當強制 gate（須修到可接受才服務）。一旦已上站或已 accept
        # （det+LLM 修完仍殘、多為 MinerU 源頭缺）→ 降可選：不再無限重審/重生 catalogs.json/燒 LLM。
        # （殘留多為 C2 空 caption / C7 缺 id 等 OCR 源頭缺，重審也補不出 → churn 無收益。）
        opt = '(可選)' if (_deployed(slug) or _catalog_accepted(slug)) else ''
        todo.append(f'catalog_audit({catalog_critical}){opt}')
    if has_sol_book and sol == 0 and not _sol_pending(slug):
        # sol_extract 同 catalog_audit：上站前是 gate（解答併入主書才完整），但**已上站**後降可選。
        # 否則一本「已部署、解答書卻 merge 不成」的書（如 griffiths_qm3）會讓 advance loop 每輪重派
        # 昂貴的 sol_extract LLM、reactive loop 永不 idle——與 catalog 同構的 post-deploy busy-loop。
        sol_opt = '(可選)' if _deployed(slug) else ''
        todo.append(f'sol_extract({slug}_sol){sol_opt}')
    if not has_zh:
        todo.append('translate(可選)')
    stage = '4 sol已merge' if (tot and sol) else '3 parsed'
    if has_zh:
        stage += ' +zh'
    return {'slug': slug, 'stage': stage, 'todo': ' '.join(todo) or '—',
            'prob': tot, 'sol': sol, 'sol_book': has_sol_book,
            'math_bad': _math_residual(slug)}


def _stuck_reason(slug: str, r: dict, e: dict) -> tuple[str, str] | None:
    """卡關偵測（需人工裁決，非自動可推進）：qc-reject / 書況 review / 仍未 ingest。回 (原因, note)。"""
    qc = e.get('qc') or {}
    if qc.get('verdict') == 'reject':
        return ('qc-reject', qc.get('note', '')[:72])
    if e.get('book_qc'):
        return ('book_qc', '；'.join((e['book_qc'] or {}).get('reasons', []))[:72])
    if r['stage'].startswith(('0', 'X')):
        return (r['stage'], r['todo'])
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog='book_pipeline.status',
                                 description='每本書在 pipeline 的真實階段（單一真相）。')
    ap.add_argument('slug', nargs='?', help='單本細節（省略=全表）')
    args = ap.parse_args()

    pending = _load_pending()
    raw = _raw_slug_map()
    # 全 slug = mineru_data 既有 ∪ raw_pdfs 待上傳 ∪ pending 中（皆去 _sol）
    slugs = [args.slug] if args.slug else all_slugs(pending, raw)
    rows = [assess(s, pending, raw) for s in slugs]
    print(f"{'slug':20} {'階段':<16} {'題/解':>10} {'_sol':>5}  待辦")
    todos = []
    for r in rows:
        ps = f"{r.get('sol',0)}/{r.get('prob',0)}" if r.get('prob') else '—'
        mb = r.get('math_bad')
        todo_disp = r['todo'] + (f'  ·math殘{mb}' if mb else '')
        print(f"{r['slug']:20} {r['stage']:<16} {ps:>10} {'有' if r['sol_book'] else '':>5}  {todo_disp}")
        non_optional = ' '.join(p for p in r['todo'].split() if not p.endswith('(可選)'))
        if non_optional and non_optional != '—':
            todos.append((r['slug'], non_optional))
    if todos:
        print('\n=== 待辦（非可選）===')
        for slug, t in todos:
            print(f'  {slug}: {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
