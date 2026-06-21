#!/usr/bin/env python3
"""book_pipeline.migrate_fields — 抽 booklists/*.json 檔頭 → fields.json（冪等、可逆、零 LLM）。

「合格存在」重構 Phase 1：把領域顯示名 + 排序從 9 個 booklists 檔頭抽出成單一 fields.json
（三層架構的領域骨架層）。
  冪等：重跑產生位元相同的 fields.json（同序、同欄）。
  可逆：fields.json 內容全來自 booklists 檔頭 → 刪檔即回退、零資訊新增。
  booklists/*.json 不動（Phase 1-2 仍是真相源；Phase 3 才切 editions+fields）。

  uv run python -m book_pipeline.migrate_fields [--dry-run]
"""
from __future__ import annotations

import argparse
import json

from book_pipeline import booklists, fields as fields_mod, jsonio


def build() -> list[dict]:
    """從 booklists 檔頭抽 [{field_id, field, order}]（按 (order, field_id) 排序）。"""
    out = []
    for f in booklists.load_files():
        fid = f.get('field_id')
        if not fid:
            continue
        out.append({'field_id': fid, 'field': f.get('field', ''),
                    'order': f.get('order', 9999)})
    out.sort(key=lambda d: (d['order'], d['field_id']))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='抽 booklists 檔頭 → fields.json（冪等可逆）')
    ap.add_argument('--dry-run', action='store_true', help='只印將寫入內容、不落盤')
    args = ap.parse_args()

    rows = build()
    cur = jsonio.read_json(fields_mod.FIELDS_JSON, None)
    same = (cur == rows)
    if args.dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f'\n{len(rows)} fields → {fields_mod.FIELDS_JSON}'
              f'（{"已是最新、冪等無變" if same else "將寫入/更新"}）')
        return 0
    if same:
        print(f'✓ 已是最新（{len(rows)} fields），冪等無變')
        return 0
    jsonio.atomic_write_json(fields_mod.FIELDS_JSON, rows, indent=1)
    print(f'✓ 寫入 {len(rows)} fields → {fields_mod.FIELDS_JSON}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
