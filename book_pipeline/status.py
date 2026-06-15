#!/usr/bin/env python3
"""book_pipeline.status — 每本書在 pipeline 的真實階段（單一真相，取代瞎猜）。

讀「實際資料」而非檔名臆測，避免歷史教訓：parsed/ 是 gitignore（git 看不到）、
problems 是 ch.json 的獨立鍵（非 body 內 block）、sol_extract 產物只在 parsed 內。

階段（依序）：
  待ingest   raw_pdfs/<file>.pdf 在、unified 未產（待本地一條龍 ingest）
  ingest中斷 _pending_batches.json 有此 slug（已 PUT、unified 未組）→ 重跑同指令冪等補完
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
"""
from __future__ import annotations

import glob
import json
import os
import sys

from book_pipeline.catalog_audit import audit_catalog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'book_pipeline', 'mineru_data')


def _exists(slug: str, *parts: str) -> bool:
    return os.path.exists(os.path.join(DATA, slug, *parts))


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


def _catalog_critical(slug: str) -> int:
    """Return catalog semantic critical count without writing audit reports."""
    if not _exists(slug, 'parsed', 'catalogs.json'):
        return 0
    try:
        return int(audit_catalog(slug, write_report=False).get('critical') or 0)
    except Exception:
        return 1


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
            return {'slug': slug, 'stage': '0.5 ingest中斷', 'todo': 'ingest', 'sol_book': has_sol_book}
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
        todo.append(f'catalog_audit({catalog_critical})')
    if has_sol_book and sol == 0 and not _sol_pending(slug):
        todo.append(f'sol_extract({slug}_sol)')
    if not has_zh:
        todo.append('translate(可選)')
    stage = '4 sol已merge' if (tot and sol) else '3 parsed'
    if has_zh:
        stage += ' +zh'
    return {'slug': slug, 'stage': stage, 'todo': ' '.join(todo) or '—',
            'prob': tot, 'sol': sol, 'sol_book': has_sol_book}


def main() -> int:
    pending = _load_pending()
    raw = _raw_slug_map()
    # 全 slug = mineru_data 既有 ∪ raw_pdfs 待上傳 ∪ pending 中（皆去 _sol）
    slugs = all_slugs(pending, raw)
    if len(sys.argv) > 1:
        slugs = [sys.argv[1]]
    rows = [assess(s, pending, raw) for s in slugs]
    print(f"{'slug':20} {'階段':<16} {'題/解':>10} {'_sol':>5}  待辦")
    todos = []
    for r in rows:
        ps = f"{r.get('sol',0)}/{r.get('prob',0)}" if r.get('prob') else '—'
        print(f"{r['slug']:20} {r['stage']:<16} {ps:>10} {'有' if r['sol_book'] else '':>5}  {r['todo']}")
        non_optional = ' '.join(p for p in r['todo'].split() if p != 'translate(可選)')
        if non_optional and non_optional != '—':
            todos.append((r['slug'], non_optional))
    if todos:
        print('\n=== 待辦（非可選）===')
        for slug, t in todos:
            print(f'  {slug}: {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
