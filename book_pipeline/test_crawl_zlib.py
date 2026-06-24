"""crawl_zlib.reconcile_orphan_pdfs 測試：raw_pdfs 的 <slug>.pdf orphan 自癒補登 slug_map。

回歸守衛：slug_map（git-tracked）被 git 操作回退、PDF 卻留碟上 → daemon 經 _raw_slug_map
誤判『X 無源』永久卡死（taylor_terhaar 等 4 本實證卡 6 天）。reconcile 補登 <slug>.pdf 慣例
的現有 target orphan、放過 z-lib 原名/非 target、靜默跳過已 ingest，原子寫、idempotent。

hermetic：重導 cz.RAW/DATA/SLUG_MAP 到 tmp + monkeypatch booklists.targets，絕不碰真實狀態。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile

from book_pipeline import crawl_zlib as cz


@contextlib.contextmanager
def _sandbox(targets):
    d = tempfile.mkdtemp(prefix='reconcile_test_')
    raw = os.path.join(d, 'raw_pdfs')
    data = os.path.join(d, 'mineru_data')
    os.makedirs(raw)
    os.makedirs(data)
    import book_pipeline.booklists as bl
    saved = (cz.RAW, cz.DATA, cz.SLUG_MAP, bl.targets)
    cz.RAW, cz.DATA = raw, data
    cz.SLUG_MAP = os.path.join(d, 'slug_map.json')
    bl.targets = lambda: [{'slug': s} for s in targets]
    try:
        yield d, raw, data
    finally:
        (cz.RAW, cz.DATA, cz.SLUG_MAP, bl.targets) = saved


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').close()


def _slug_map(path):
    return (json.load(open(path)) or {}).get('map', {}) if os.path.exists(path) else {}


def test_reconcile_registers_target_orphan():
    with _sandbox(targets={'taylor_terhaar_mechanics', 'huang_sm'}) as (d, raw, data):
        _touch(os.path.join(raw, 'taylor_terhaar_mechanics.pdf'))
        _touch(os.path.join(raw, 'huang_sm.pdf'))
        r = cz.reconcile_orphan_pdfs()
        assert set(r['registered']) == {'taylor_terhaar_mechanics', 'huang_sm'}, r
        assert r['unresolved'] == [], r
        assert _slug_map(cz.SLUG_MAP) == {
            'taylor_terhaar_mechanics.pdf': 'taylor_terhaar_mechanics',
            'huang_sm.pdf': 'huang_sm',
        }


def test_reconcile_skips_zlib_named_and_non_target():
    with _sandbox(targets={'real_book'}) as (d, raw, data):
        _touch(os.path.join(raw, 'Introduction to Linear Algebra (Strang) (z-library.sk).pdf'))
        _touch(os.path.join(raw, 'not_a_target.pdf'))  # 慣例命名但非 target
        r = cz.reconcile_orphan_pdfs()
        assert r['registered'] == [], r
        assert len(r['unresolved']) == 2, r
        assert not os.path.exists(cz.SLUG_MAP), '無補登 → 不落盤'


def test_reconcile_skips_already_ingested():
    with _sandbox(targets={'ingested'}) as (d, raw, data):
        _touch(os.path.join(raw, 'ingested.pdf'))
        _touch(os.path.join(data, 'ingested', 'unified', 'content_list.json'))  # 已 ingest
        r = cz.reconcile_orphan_pdfs()
        assert r['registered'] == [] and r['unresolved'] == [], r  # 靜默跳過、不誤報 /restock


def test_reconcile_skips_already_registered_and_is_idempotent():
    with _sandbox(targets={'bk', 'fresh'}) as (d, raw, data):
        _touch(os.path.join(raw, 'bk.pdf'))
        _touch(os.path.join(raw, 'fresh.pdf'))
        with open(cz.SLUG_MAP, 'w') as f:
            json.dump({'map': {'fresh.pdf': 'fresh'}}, f)  # fresh 已登錄
        r1 = cz.reconcile_orphan_pdfs()
        assert r1['registered'] == ['bk'], r1  # 只補 bk、fresh 跳過
        r2 = cz.reconcile_orphan_pdfs()
        assert r2['registered'] == [], r2       # 第二次全已登錄 → no-op（idempotent）


if __name__ == '__main__':
    test_reconcile_registers_target_orphan()
    print('✓ 補登 <slug>.pdf 慣例的 target orphan + 原子落盤')
    test_reconcile_skips_zlib_named_and_non_target()
    print('✓ 放過 z-lib 原名/非 target → unresolved 交 /restock、無補登不落盤')
    test_reconcile_skips_already_ingested()
    print('✓ 靜默跳過已 ingest（raw 殘留）、不誤報 /restock')
    test_reconcile_skips_already_registered_and_is_idempotent()
    print('✓ 已登錄跳過 + 第二次 no-op（idempotent）')
    print('\n全部通過 ✅')
