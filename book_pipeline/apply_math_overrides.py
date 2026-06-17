#!/usr/bin/env python3
"""Apply tracked math-fix overrides to ignored parsed JSON (Phase 2 sweep handoff).

數學 sweep 的 reviewable 交付格式，比照 catalog_overrides：parsed/*.json 是
generated/ignored，每筆修復寫成 git 追蹤的 override，parser 重建後可重播。

與 catalog_overrides 的關鍵差別 = **guard 失配即 skip（不 raise）**，對齊 corpus overlay
哲學：源頭一漂移（重 OCR/重 audit 改了式子），舊修復自動停用、其餘照常套，而非炸掉整批。
冪等：已套用者（new 在、old 不在）直接 noop。

actions：
  fix_eq_tex      — 換 eq block 的 tex；expect=舊 tex 精確 guard（必填）。
  fix_inline_math — 換 md/caption/footnote/title 內一段數學子字串；anchor=該欄內容指紋 guard。

selector 複用 catalog 文法（apply_catalog_overrides）：`body[N]` / `problem:NUM:field[N]`；
另加 `title`（chunk 頂層 title 欄，catalog 文法沒有）。locator→selector 由
math_validate.locator_to_target 產（report findings 已附 targets，agent 直接抄）。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from book_pipeline.apply_catalog_overrides import (
    DATA_DIR,
    _backup_once,
    _chunk_path,
    _load_json,
    _select_block,
    _write_json,
)
from book_pipeline.build_catalogs import build_catalogs
from book_pipeline.translate import overlay_anchor

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE_DIR = ROOT / 'book_pipeline' / 'math_overrides'

_INLINE_FIELDS = {'md', 'caption', 'footnote', 'title'}
# selector → block 解析可能丟的例外（漂移），一律吞成 skip-drift。
_DRIFT_ERRORS = (LookupError, ValueError, IndexError, KeyError, TypeError)


def _resolve_field(data: dict[str, Any], selector: str, field: str) -> tuple[dict[str, Any], str]:
    """(holder, key)：title → (chunk, 'title')；其餘 → (block, field)。"""
    if selector == 'title':
        return data, 'title'
    return _select_block(data, selector), field


def _apply_fix_eq_tex(slug: str, ov: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> str:
    path = _chunk_path(slug, ov['chunk'])
    data = _load_json(path)
    try:
        block = _select_block(data, ov['selector'])
    except _DRIFT_ERRORS:
        return 'skip-drift'
    if not isinstance(block, dict) or block.get('t') != 'eq':
        return 'skip-drift'
    expect = ov.get('expect')
    if expect is None:
        raise ValueError(f"{ov.get('id', '<ov>')}: fix_eq_tex 需 expect（舊 tex guard）")
    cur = block.get('tex')
    if not isinstance(cur, str):
        return 'skip-drift'
    new = ov['new']
    if cur.strip() == new.strip():
        return 'noop'  # 已套用（重 parse 後若源頭已等於 new）
    if cur.strip() != expect.strip():
        return 'skip-drift'  # 源頭漂移 → 停用本筆
    block['tex'] = new
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)
    return 'applied'


def _apply_fix_inline_math(slug: str, ov: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> str:
    field = ov.get('field', 'md')
    if field not in _INLINE_FIELDS:
        raise ValueError(f"{ov.get('id', '<ov>')}: unsupported field {field!r}")
    path = _chunk_path(slug, ov['chunk'])
    data = _load_json(path)
    try:
        holder, key = _resolve_field(data, ov['selector'], field)
    except _DRIFT_ERRORS:
        return 'skip-drift'
    value = holder.get(key)
    if not isinstance(value, str):
        return 'skip-drift'
    anchor = ov.get('anchor')
    if anchor and overlay_anchor({key: value}) != anchor:
        return 'skip-drift'  # 該欄內容指紋漂移 → 停用本筆
    old, new = ov['old'], ov['new']
    if old == new:
        return 'noop'  # 無實質改變（對齊 fix_eq_tex 的 cur==new 前置判斷，免無謂 write/build_catalogs）
    if old not in value:
        return 'noop' if new in value else 'skip-drift'  # 已套用 / 子字串漂移
    # all=true → 同欄全部同式一次換（occ>1：同一壞式在一欄出現多次，預設只換首處清不掉其餘）。
    holder[key] = value.replace(old, new) if ov.get('all') else value.replace(old, new, 1)
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)
    return 'applied'


_ACTIONS = {
    'fix_eq_tex': _apply_fix_eq_tex,
    'fix_inline_math': _apply_fix_inline_math,
}


def apply_overrides(slug: str) -> dict[str, int]:
    """套用 math_overrides/<slug>.json。回各 action×結果（applied/noop/skip-drift）計數。"""
    path = OVERRIDE_DIR / f'{slug}.json'
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = _load_json(path)
    backup_dir = (DATA_DIR / slug / 'parsed' / '_override_backups'
                  / ('math-' + datetime.now().strftime('%Y%m%d-%H%M%S')))
    backed_up: set[Path] = set()
    stats: Counter[str] = Counter()
    for ov in spec.get('overrides') or []:
        action = ov.get('action')
        fn = _ACTIONS.get(action)
        if fn is None:
            raise ValueError(f'unsupported action: {action!r}')
        stats[f'{action}:{fn(slug, ov, backup_dir, backed_up)}'] += 1
    if backed_up:  # tex/md 改動極少動 catalog，但比照 catalog override 保持一致
        build_catalogs(slug)
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(prog='python -m book_pipeline.apply_math_overrides')
    ap.add_argument('slug')
    args = ap.parse_args()
    stats = apply_overrides(args.slug)
    print(f"[math-overrides] {args.slug}: {stats}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
