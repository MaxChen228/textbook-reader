#!/usr/bin/env python3
"""book_pipeline.discovered — discovery 找到的候選新書（機器擴張層，git 追蹤、人可否決）。

[architect note — 書單自我生長的命脈]

「持續餵足量好書」的命脈是書單能自己長。discovery mode（書單管理 skill）找夠格新書——已收書的
參考文獻輻射、權威課程書目種子——寫進本層，候選**自動流入查證**：`booklists.targets()` 合併本層、
與人工正典 target 同等走 resolve+editions 查證 + book_qc gate，故新書與人工書一樣經完整品質把關。
人在 git diff 抽查、隨時刪 entry 否決，或把好候選晉升進 booklists 人工正典化。

與 booklists/*.json 的分界（鐵律 2 的延伸）：
  booklists/*.json   = 人工正典（agent 絕不寫）。
  discovered/*.json  = 機器候選（agent 寫、標 by:discovery；人可否決移除 / 晉升進 booklists）。
晉升 = 架構師把候選手動搬進 booklists 後、從 discovered 移除（避免兩處重複，targets 合併時人工優先）。

schema discovered/<field_id>.json：{field_id, field, candidates:[{slug,title,author,subject,
  solution,by,at,note}]}。slug 規則同 booklists（[a-z0-9_]{1,64}、不得 _sol 結尾）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime, timezone

from book_pipeline import jsonio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCOVERED_DIR = os.path.join(ROOT, 'book_pipeline', 'discovered')
SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')   # 同 booklists（不 import 避循環：booklists import 本模組）
SOL_SUFFIX = '_sol'


def _path(field_id: str) -> str:
    return os.path.join(DISCOVERED_DIR, f'{field_id}.json')


def load_all() -> dict:
    """{field_id: {field_id, field, candidates:[...]}} 全表（容錯：非候選檔跳過）。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(DISCOVERED_DIR, '*.json'))):
        d = jsonio.read_json(p, None)
        if isinstance(d, dict) and isinstance(d.get('candidates'), list):
            out[os.path.basename(p)[:-5]] = d
    return out


def iter_candidates() -> list[dict]:
    """全 discovered 候選攤平（供 booklists.targets 合併）；各筆附 field_id/field。"""
    out = []
    for fid, d in sorted(load_all().items()):
        for c in (d.get('candidates') or []):
            out.append({**c, 'field_id': fid, 'field': d.get('field', fid)})
    return out


def add(field_id: str, field: str, candidates: list[dict], existing_slugs: set) -> dict:
    """去重寫入新候選（冪等）：撞 existing_slugs（人工正典+inventory）或已在本 discovered、slug 非法、
    _sol 結尾者一律跳過。flock + 原子寫。回 {added, skipped, field_id}。"""
    import fcntl
    os.makedirs(DISCOVERED_DIR, exist_ok=True)
    p = _path(field_id)
    with open(p + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = jsonio.read_json(p, None) or {'field_id': field_id, 'field': field, 'candidates': []}
        have = set(existing_slugs) | {c.get('slug') for c in cur['candidates']}
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        added = []
        for c in candidates:
            slug = c.get('slug', '')
            if not SLUG_RE.match(slug) or slug.endswith(SOL_SUFFIX) or slug in have:
                continue
            have.add(slug)
            added.append({'slug': slug, 'title': c.get('title', ''), 'author': c.get('author', ''),
                          'subject': c.get('subject', ''), 'solution': c.get('solution', True),
                          'by': 'discovery', 'at': now, 'note': c.get('note', '')})
        if added:
            cur['candidates'].extend(added)
            cur['field'] = field or cur.get('field') or field_id
            jsonio.atomic_write_json(p, cur, indent=1)
        return {'added': len(added), 'skipped': len(candidates) - len(added), 'field_id': field_id}


def remove(field_id: str, slug: str) -> bool:
    """否決：從 discovered 移除一個候選（人工否決 / 晉升後清理）。回是否真的移除。"""
    import fcntl
    p = _path(field_id)
    with open(p + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = jsonio.read_json(p, None)
        if not isinstance(cur, dict):
            return False
        before = len(cur.get('candidates') or [])
        cur['candidates'] = [c for c in (cur.get('candidates') or []) if c.get('slug') != slug]
        if len(cur['candidates']) == before:
            return False
        jsonio.atomic_write_json(p, cur, indent=1)
        return True


# ── CLI（discovery mode worker 寫候選 / 架構師否決用）─────────────────────────────────
def cmd_add(args) -> int:
    """加一個候選（去重 vs 人工正典 booklists + inventory + 既有 discovered）。skipped 回 rc=1。"""
    from book_pipeline import booklists   # 延遲 import（booklists import 本模組，避模組級循環）
    existing = {t['slug'] for t in booklists.targets(include_discovered=False)} | booklists.have_slugs()
    cand = {'slug': args.slug, 'title': args.title, 'author': args.author,
            'subject': args.subject or '', 'solution': not args.no_solution, 'note': args.note or ''}
    r = add(args.field_id, args.field, [cand], existing)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r['added'] else 1


def cmd_list(args) -> int:
    cands = iter_candidates()
    if args.field_id:
        cands = [c for c in cands if c['field_id'] == args.field_id]
    print(json.dumps({'count': len(cands), 'candidates': cands}, ensure_ascii=False, indent=2))
    return 0


def cmd_remove(args) -> int:
    ok = remove(args.field_id, args.slug)
    print(json.dumps({'removed': ok, 'field_id': args.field_id, 'slug': args.slug}, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description='discovered：discovery 候選新書（機器擴張層，人可否決）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('add', help='加候選（去重 vs 人工正典+inventory+既有；skipped 回 rc=1）')
    p.add_argument('field_id')
    p.add_argument('--field', required=True, help='領域中文顯示名')
    p.add_argument('--slug', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--author', required=True)
    p.add_argument('--subject', default=None)
    p.add_argument('--no-solution', dest='no_solution', action='store_true', help='此書無解答本（同 booklists solution:false）')
    p.add_argument('--note', default=None, help='來源（如：griffiths_qm 參考文獻輻射）')
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser('list', help='列候選')
    p.add_argument('field_id', nargs='?', default=None)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser('remove', help='否決：移除一個候選')
    p.add_argument('field_id')
    p.add_argument('slug')
    p.set_defaults(fn=cmd_remove)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
