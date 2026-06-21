"""fields 領域骨架 + migrate_fields 單元測試：uv run python -m book_pipeline.test_fields

load 排序/容錯、by_id/order_of/name_of、migrate_fields.build 抽檔頭冪等。用 tmp 檔重指路徑，不碰 repo；
teardown 還原模組變數。"""

import os
import tempfile

from book_pipeline import fields, jsonio

_ORIG = fields.FIELDS_JSON


def teardown_function(function):
    fields.FIELDS_JSON = _ORIG


def _write(rows):
    p = os.path.join(tempfile.mkdtemp(prefix='fields_'), 'fields.json')
    jsonio.atomic_write_json(p, rows, indent=1)
    fields.FIELDS_JSON = p
    return p


def test_load_sorts_and_filters():
    _write([{'field_id': 'cs', 'field': '資訊', 'order': 30},
            {'field_id': 'math', 'field': '數學', 'order': 10},
            {'field': '無 id 應被濾掉', 'order': 5},   # 缺 field_id → 過濾
            'not a dict'])                              # 非 dict → 過濾
    out = fields.load()
    assert [f['field_id'] for f in out] == ['math', 'cs']   # 按 order 排序
    assert len(out) == 2                                     # 壞列已濾


def test_load_missing_returns_empty():
    fields.FIELDS_JSON = '/nonexistent/fields.json'
    assert fields.load() == []                              # 缺檔容錯不炸


def test_by_id_order_name():
    _write([{'field_id': 'cs', 'field': '資訊', 'order': 30},
            {'field_id': 'math', 'field': '數學', 'order': 10}])
    assert fields.order_of('math') == 10 and fields.order_of('cs') == 30
    assert fields.order_of('unknown') == 9999              # 找不到 → 末尾
    assert fields.name_of('cs') == '資訊'
    assert fields.name_of('unknown') == 'unknown'          # 找不到 → 回 id 本身


def test_migrate_build_from_booklists():
    """migrate_fields.build 抽真實 booklists 檔頭：每領域一筆、按 order 排、冪等。"""
    from book_pipeline import migrate_fields
    rows = migrate_fields.build()
    assert len(rows) >= 1
    assert all({'field_id', 'field', 'order'} <= set(r) for r in rows)
    orders = [r['order'] for r in rows]
    assert orders == sorted(orders)                        # 已按 order 排序
    fids = [r['field_id'] for r in rows]
    assert len(fids) == len(set(fids))                     # field_id 唯一
    assert rows == migrate_fields.build()                  # 冪等：重跑相同


if __name__ == '__main__':
    test_load_sorts_and_filters()
    test_load_missing_returns_empty()
    test_by_id_order_name()
    test_migrate_build_from_booklists()
    print('全部通過 ✅')
