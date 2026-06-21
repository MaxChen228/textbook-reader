#!/usr/bin/env python3
"""book_pipeline.migrate_resolution_to_links — crawl_resolution.json 退化成「純連結快取」。

「合格存在」重構：新模型把 resolution 降為純連結層 {status: found|not_found, id, hash, href, cover,
by, at}；版本/解答/夠格判斷全上移 editions。退化規則（零 LLM、冪等、可逆、零遺失）：
  resolved（或 legacy id+hash）→ status:'found'（保留 id/hash/by/title/at/href/cover 等連結資料）
  not_found（或 legacy absent）→ status:'not_found'（查無記憶；新舊同名）
  review / version_unavailable → 移除 status 態欄（這些「態」上移 editions/qualification 層）、把原值存
                                 `legacy_status` breadcrumb（可逆 + 冪等護欄）、保留連結資料 → 新模型回落
                                 CANDIDATE 由 /restock 重查（架構師亦可裁 review 書）。
  已退化（status:'found' / 'not_found' / 有 legacy_status）/ 畸形 → 不動（冪等）。

**RUN 時機 = Phase 3**（與 booklists→editions 真相源切換原子化）。在 Phase 2 跑會讓**舊** booklists.status_of
誤判 review/version_unavailable（舊碼靠 status 態欄分流）→ 違反 Phase 2「真相源仍舊」。故 Phase 2 只寫碼
+ 測 + dry-run 驗，**不 live**。

用法：uv run python -m book_pipeline.migrate_resolution_to_links [--dry-run]
"""
from __future__ import annotations

import argparse
import fcntl
from collections import Counter

from book_pipeline import booklists as bl
from book_pipeline import jsonio


def transform(entry: dict) -> tuple[dict, str] | None:
    """單一 entry → (new_entry, action) 或 None（不動）。action ∈ {found, not_found, strip}。"""
    if not isinstance(entry, dict):
        return None
    if entry.get('legacy_status'):
        return None                          # 已退化的 review/version_unavailable → 冪等不動（在 id/hash 判前攔）
    st = entry.get('status')
    if st in ('found', 'not_found'):
        return None                          # 已是目標純連結態 → 冪等不動
    e = dict(entry)
    if st == 'resolved' or (not st and e.get('id') and e.get('hash')):
        e['status'] = 'found'
        return e, 'found'
    if not st and e.get('absent'):
        e['status'] = 'not_found'
        return e, 'not_found'
    if st in ('review', 'version_unavailable'):
        e['legacy_status'] = st              # 可逆 breadcrumb + 冪等護欄（防 id/hash 殘留被誤升 found）
        del e['status']
        return e, 'strip'
    return None                              # 畸形（無 status/id/hash/absent）→ 不動、報告


def plan(resolution: dict) -> dict:
    """回 {todo:{slug:new_entry}, by_action:Counter, untouched:int}。"""
    todo, by_action, untouched = {}, Counter(), 0
    for slug, e in resolution.items():
        t = transform(e)
        if t is None:
            untouched += 1
        else:
            todo[slug], action = t
            by_action[action] += 1
    return {'todo': todo, 'by_action': by_action, 'untouched': untouched}


def migrate(dry_run: bool = False) -> dict:
    """flock 下 read-plan-write（與 resolver/CLI 並發互斥）。dry_run 只算不寫。"""
    with open(bl.RESOLUTION + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        res = bl.load_resolution()
        p = plan(res)
        if not dry_run and p['todo']:
            res.update(p['todo'])
            jsonio.atomic_write_json(bl.RESOLUTION, res, indent=1)
    return {'by_action': dict(p['by_action']), 'changed': len(p['todo']),
            'untouched': p['untouched'], 'total': len(res), 'dry_run': dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(
        description='resolution 退化成純連結快取（冪等、可逆、零 LLM；RUN 時機=Phase 3）')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    r = migrate(dry_run=args.dry_run)
    tag = '[dry-run] ' if r['dry_run'] else ''
    print(f"{tag}總 {r['total']} entry：退化 {r['changed']} {r['by_action']} · 不動 {r['untouched']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
