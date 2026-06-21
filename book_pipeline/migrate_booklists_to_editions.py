#!/usr/bin/env python3
"""book_pipeline.migrate_booklists_to_editions — 一次性結構遷移：把 booklists SoT 的「已連結書」
（owned ∪ ready，舊 status_of 判定）落成 editions/<slug>.json 完整記錄。

「合格存在」重構 Phase 2。零 LLM、冪等、可逆、零遺失、owned 保命：
  - **只遷 owned ∪ ready（424 本）**：信任人工夠格（D4）→ qualification.eligible=True、
    verified_at=None（維① 已信任、但未經 agent 親查戳，待 /restock 回填）；version/sol_alignment 留 None
    （維③④ 待 /restock 親查）。identity 取代 booklists book{} 條目、classification 帶齊領域歸類（D8 硬約束）。
  - **unresolved / review / version_unavailable / not_found → 不建 editions 檔**（新模型：沒連結/沒驗的書
    不在合格目錄；not_found 的「查無記憶」留在 crawl_resolution.json，本遷移**完全不碰 resolution**）。
  - **冪等**：editions.ensure 只補缺欄、不蓋已有 → 重跑安全（/restock 已親查的版本/eligible 結論不被覆蓋）。
  - **可逆**：產物全是 editions/*.json，刪檔即回退；booklists/* 與 crawl_resolution.json 一字不動。
  - **owned 保命**：owned 書一律建檔（即使無 resolution entry，新 dims 的 link 維由 have 推導）；分類帶齊。

resolution 退化（resolved→found…）是另一支 migrate_resolution_to_links.py、且其 RUN 推遲到 Phase 3
（與真相源切換原子化，避免本階段舊 booklists.status_of 誤判 review/version_unavailable）。

用法：uv run python -m book_pipeline.migrate_booklists_to_editions [--dry-run]
"""
from __future__ import annotations

import argparse
from collections import Counter

from book_pipeline import booklists as bl
from book_pipeline import editions as ed


def _owned_or_linked(slug: str, have: set, resolution: dict) -> bool:
    """凍結判據：owned ∪ 有可信 z-lib 連結（與 booklists.status_of 的五態演進**解耦**——本遷移是一次性
    歷史產物，須穩定可重跑）。涵蓋新 status:'found' 與舊 'resolved'/legacy id+hash（皆須 by 戳記可信）；
    not_found/absent 排除。"""
    if slug in have:
        return True
    r = resolution.get(slug) or {}
    if r.get('status') in ('not_found',) or r.get('absent'):
        return False
    has_link = r.get('status') in ('found', 'resolved') or (r.get('id') and r.get('hash'))
    return bool(has_link) and bl.is_trustworthy(r)


def _legacy_targets(files: list[dict]) -> list[dict]:
    """由 booklist 封存結構（_archive/*.json）攤平 target——遷移**自成一體**，**不**用 booklists.targets()
    （那已切 editions universe、會循環）。沿用舊兩層攤平邏輯：主書 + solution!=false 衍生 _sol。
    order=(field_order, sublist_idx, book_idx, kind_rank) 供 plan 落 classification.order。"""
    out = []
    for f in files:
        for si, sl in enumerate(f.get('sublists') or []):
            for bi, b in enumerate(sl.get('books') or []):
                slug = b.get('slug', '')
                base = (f.get('order', 9999), si, bi)
                out.append({'slug': slug, 'title': b.get('title', ''), 'author': b.get('author', ''),
                            'edition_pref': b.get('edition_pref', ''), 'field': f.get('field', ''),
                            'field_id': f.get('field_id', ''), 'subject': sl.get('name', ''),
                            'kind': 'main', 'of': None, 'order': base + (0,)})
                if b.get('solution', True):
                    out.append({'slug': f'{slug}{bl.SOL_SUFFIX}',
                                'title': f"{b.get('title', '')} — Solutions", 'author': b.get('author', ''),
                                'edition_pref': b.get('edition_pref', ''), 'field': f.get('field', ''),
                                'field_id': f.get('field_id', ''), 'subject': sl.get('name', ''),
                                'kind': 'solution', 'of': slug, 'order': base + (1,)})
    return out


def plan(targets_: list[dict], have: set, resolution: dict) -> dict:
    """純核心（無 I/O，可測）：targets × (have, resolution) → {slug: editions defaults}。
    只收 owned ∪ 有可信連結；其餘略過。has_solution 由「該主書是否衍生 _sol target」判定。"""
    sol_parents = {t['of'] for t in targets_ if t['kind'] == 'solution' and t.get('of')}
    todo: dict[str, dict] = {}
    for t in targets_:
        slug = t['slug']
        if not _owned_or_linked(slug, have, resolution):
            continue
        is_sol = t['kind'] == 'solution'
        todo[slug] = {
            'identity': {
                'title': t.get('title', ''), 'author': t.get('author', ''),
                'edition_pref': t.get('edition_pref', ''),
                'has_solution': (not is_sol) and (slug in sol_parents),
                'promoted_from': 'migration',
            },
            'classification': {'field_id': t.get('field_id', ''),
                               'subject': t.get('subject', ''),
                               'order': list(t['order'])},
            'qualification': {'eligible': True, 'verified_at': None},
            'by': 'migration',
        }
    return todo


def migrate(dry_run: bool = False) -> dict:
    """讀真實 SoT/inventory/resolution → plan → editions.ensure（冪等補骨架）。回統計報告。"""
    files = bl.load_files()                               # 讀 booklists/_archive/（BOOKLISTS_DIR 已指封存）
    ts = _legacy_targets(files)                           # 自成一體攤平（不用已切 editions 的 bl.targets）
    have = bl.have_slugs()
    resolution = bl.load_resolution()
    todo = plan(ts, have, resolution)
    existing = set(ed.load_all().keys())                 # 遷移前已有的 editions 檔（冪等報告用）
    if not dry_run:
        for slug, defaults in todo.items():
            ed.ensure(slug, defaults)                     # 只補缺欄、不蓋已有（flock + atomic 在 ensure 內）
    created = sorted(s for s in todo if s not in existing)
    refreshed = sorted(s for s in todo if s in existing)  # 已有檔 → ensure 僅補缺欄（多半 no-op）
    by_kind = Counter('solution' if s.endswith(bl.SOL_SUFFIX) else 'main' for s in todo)
    return {'total_targets': len(ts), 'to_migrate': len(todo),
            'created': created, 'refreshed': refreshed, 'by_kind': dict(by_kind),
            'skipped_unlinked': len(ts) - len(todo), 'dry_run': dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(
        description='booklists owned∪ready → editions/<slug>.json（冪等、可逆、零 LLM、owned 保命）')
    ap.add_argument('--dry-run', action='store_true', help='只算不寫')
    args = ap.parse_args()
    r = migrate(dry_run=args.dry_run)
    tag = '[dry-run] ' if r['dry_run'] else ''
    print(f"{tag}{r['total_targets']} targets：遷入 editions {r['to_migrate']} "
          f"（主書 {r['by_kind'].get('main', 0)} · 解答本 {r['by_kind'].get('solution', 0)}）"
          f" · 略過未連結 {r['skipped_unlinked']}")
    print(f"  新建 {len(r['created'])} · 既有補骨架 {len(r['refreshed'])}")
    if r['created'][:12]:
        print(f"  新建樣本：{' '.join(r['created'][:12])}{' …' if len(r['created']) > 12 else ''}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
