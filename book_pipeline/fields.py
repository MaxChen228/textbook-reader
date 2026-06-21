#!/usr/bin/env python3
"""book_pipeline.fields — 領域骨架（field 顯示名 + 排序）單一真相源。

[三層資料架構的「領域骨架」層——人工維護、git 追蹤、低頻]

「合格存在」四維重構把書目從 booklists/<field_id>.json（領域檔內嵌書清單）拆成三層正交：
  fields.json（本模組）        領域列表 + 排序，人工維護。
  editions/<slug>.json         每本合格書的完整記錄（身份/分類/四維結論），LLM agent + 遷移寫。
  crawl_resolution.json        純連結快取（gitignore，高頻）。
editions 的 classification.field_id 是外鍵，join 回本檔取顯示名與 field_order（排收錄表）。

本模組純讀。fields.json 內容全抽自舊 booklists 檔頭 → 由 migrate_fields.py 生成、可逆。
"""
from __future__ import annotations

import os

from book_pipeline import jsonio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELDS_JSON = os.path.join(ROOT, 'book_pipeline', 'fields.json')


def load(path: str | None = None) -> list[dict]:
    """讀 fields.json → [{field_id, field, order}]（按 (order, field_id) 排序）。
    無檔 / 非 list → 回 []（容錯，不讓缺檔炸下游）。"""
    data = jsonio.read_json(path or FIELDS_JSON, None)
    if not isinstance(data, list):
        return []
    out = [d for d in data if isinstance(d, dict) and d.get('field_id')]
    out.sort(key=lambda f: (f.get('order', 9999), f.get('field_id', '')))
    return out


def by_id(path: str | None = None) -> dict:
    """{field_id: {field_id, field, order}} 映射（join 用）。"""
    return {f['field_id']: f for f in load(path)}


def order_of(field_id: str, path: str | None = None) -> int:
    """field_id → 領域排序（找不到回 9999，排末尾）。"""
    return (by_id(path).get(field_id) or {}).get('order', 9999)


def name_of(field_id: str, path: str | None = None) -> str:
    """field_id → 領域顯示名（找不到回 field_id 本身，不丟資訊）。"""
    return (by_id(path).get(field_id) or {}).get('field') or field_id
