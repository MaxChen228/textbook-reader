#!/usr/bin/env python3
"""book_pipeline.migrate_resolution_status — 一次性結構遷移：給 legacy crawl_resolution.json entry
補顯式 `status` 欄（零 LLM、冪等、可逆）。

查證左移把舊單一 absent 拆成 not_found / version_unavailable。`status_of` 已向後相容（無 status 的
legacy entry 仍正確判讀），故本遷移**非啟動必須**——它把隱含態固化成顯式欄，讓下游/稽核一眼可讀，
並為 version_unavailable 重查鋪路。安全網：
  - **保守不定生死**（鐵律 1）：legacy {absent:true} 一律先落 not_found（永久）——真正的
    not_found vs version_unavailable 二分由入口 LLM 重查落定，**禁在此硬編碼判**。存量 absent 雖約
    82% 性質可重查，但遷移不賭、交 skill 親查時自然把可重查者改判回來。
  - **冪等**：已有 status 的 entry 跳過 → 重跑安全。
  - **並存可逆**：保留 legacy 旗標（absent/review）不刪 → 兩階段切換、回滾即「移除新增的 status 欄」。
  - **零遺失**：原 entry 其餘欄位原樣保留。

用法：uv run python -m book_pipeline.migrate_resolution_status [--dry-run]
"""
from __future__ import annotations

import argparse
import fcntl
from collections import Counter

from book_pipeline import booklists as bl
from book_pipeline import jsonio


def classify(entry: dict) -> str | None:
    """legacy entry → 應補的 status；已遷移（有 status）或無法判（畸形）回 None。"""
    if not isinstance(entry, dict) or entry.get('status'):
        return None                       # 非 dict 或已遷移 → 冪等跳過
    if entry.get('id') and entry.get('hash'):
        return 'resolved'
    if entry.get('absent'):
        return 'not_found'                # 保守：真無/別版二分交入口 LLM 重查（禁此處定生死）
    if entry.get('review'):
        return 'review'
    return None                           # 畸形（無 id/hash/absent/review）→ 不動、報告


def plan(resolution: dict) -> dict:
    """回 {todo:{slug:new_status}, skipped:int(已遷移), malformed:[slug]}。"""
    todo, skipped, malformed = {}, 0, []
    for slug, e in resolution.items():
        st = classify(e)
        if st:
            todo[slug] = st
        elif isinstance(e, dict) and e.get('status'):
            skipped += 1
        else:
            malformed.append(slug)
    return {'todo': todo, 'skipped': skipped, 'malformed': malformed}


def migrate(dry_run: bool = False) -> dict:
    """flock 下 read-plan-write。dry_run 只算不寫。回統計報告。"""
    with open(bl.RESOLUTION + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        res = bl.load_resolution()
        p = plan(res)
        if not dry_run and p['todo']:
            for slug, st in p['todo'].items():
                res[slug]['status'] = st       # 純加欄；保留所有 legacy 旗標與其餘欄位
            jsonio.atomic_write_json(bl.RESOLUTION, res, indent=1)
    return {**p, 'by_status': dict(Counter(p['todo'].values())),
            'total': len(res), 'dry_run': dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description='resolution status 欄一次性結構遷移（冪等、可逆、零 LLM）')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    r = migrate(dry_run=args.dry_run)
    tag = '[dry-run] ' if r['dry_run'] else ''
    print(f"{tag}總 {r['total']} entry：待補 status {sum(r['by_status'].values())} "
          f"{r['by_status']} · 已遷移跳過 {r['skipped']} · 畸形 {len(r['malformed'])}")
    if r['malformed']:
        print(f"  畸形（無 id/hash/absent/review，不動）：{' '.join(r['malformed'][:20])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
