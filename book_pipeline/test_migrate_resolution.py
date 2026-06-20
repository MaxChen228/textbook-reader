"""migrate_resolution_status 單元測試：uv run python -m book_pipeline.test_migrate_resolution

舊 legacy entry 加 status 欄（resolved/not_found/review）、保守不定生死、冪等、保留舊旗標、畸形不動、
dry-run 不寫。用 tmp resolution 檔重指 bl.RESOLUTION，不碰 repo；teardown_function 還原（pytest 同 process）。"""

import os
import tempfile

from book_pipeline import booklists as bl
from book_pipeline import jsonio
from book_pipeline import migrate_resolution_status as mig

_ORIG_RESOLUTION = bl.RESOLUTION


def teardown_function(function):
    bl.RESOLUTION = _ORIG_RESOLUTION


def _isolate(entries):
    d = tempfile.mkdtemp(prefix='migres_')
    path = os.path.join(d, 'crawl_resolution.json')
    jsonio.atomic_write_json(path, entries, indent=1)
    bl.RESOLUTION = path
    return path


def test_classify():
    assert mig.classify({'id': '1', 'hash': 'a', 'by': 'agent'}) == 'resolved'
    assert mig.classify({'absent': True}) == 'not_found'        # 保守：別版二分交 LLM 重查
    assert mig.classify({'review': True}) == 'review'
    assert mig.classify({'status': 'resolved', 'id': '1', 'hash': 'a'}) is None  # 已遷移 → 冪等跳過
    assert mig.classify({'note': 'junk'}) is None               # 畸形（無旗標）→ 不動
    print('✓ classify：resolved/not_found/review 推導 + 已遷移/畸形回 None')


def test_migrate_adds_status_preserves_flags():
    path = _isolate({
        'a': {'id': '1', 'hash': 'h', 'by': 'agent', 'title': 'A'},
        'b': {'absent': True, 'note': 'no version', 'by': 'agent'},
        'c': {'review': True, 'note': 'ambiguous'},
    })
    r = mig.migrate(dry_run=False)
    res = jsonio.read_json(path, {})
    assert res['a']['status'] == 'resolved' and res['a']['title'] == 'A'      # 加 status、保留其餘欄
    assert res['b']['status'] == 'not_found' and res['b']['absent'] is True   # 保留 legacy 旗標（兩階段切換）
    assert res['b']['note'] == 'no version'
    assert res['c']['status'] == 'review' and res['c']['review'] is True
    assert r['by_status'] == {'resolved': 1, 'not_found': 1, 'review': 1}
    # status_of 對遷移後 entry 判讀一致（向後相容）
    assert bl.status_of('b', set(), res) == bl.NOT_FOUND
    assert bl.status_of('c', set(), res) == bl.REVIEW
    print('✓ migrate：加 status 欄、保留 legacy 旗標與其餘欄、status_of 判讀一致')


def test_migrate_idempotent():
    path = _isolate({'a': {'id': '1', 'hash': 'h', 'by': 'agent'}})
    mig.migrate(dry_run=False)
    first = jsonio.read_json(path, {})
    r2 = mig.migrate(dry_run=False)                             # 二次重跑
    assert r2['by_status'] == {} and r2['skipped'] == 1        # 全跳過、零變更
    assert jsonio.read_json(path, {}) == first                 # 內容不變
    print('✓ migrate：冪等（二次重跑零變更、全 skipped）')


def test_migrate_dry_run_no_write():
    path = _isolate({'a': {'absent': True}})
    before = jsonio.read_json(path, {})
    r = mig.migrate(dry_run=True)
    assert r['dry_run'] and r['by_status'] == {'not_found': 1}
    assert jsonio.read_json(path, {}) == before                # dry-run 不寫盤
    print('✓ migrate：--dry-run 只算不寫')


def test_migrate_malformed_untouched():
    path = _isolate({'a': {'note': 'no flags'}})
    r = mig.migrate(dry_run=False)
    assert r['malformed'] == ['a'] and r['by_status'] == {}
    assert 'status' not in jsonio.read_json(path, {})['a']      # 畸形不動
    print('✓ migrate：畸形 entry（無旗標）不動、列入報告')


if __name__ == '__main__':
    test_classify()
    test_migrate_adds_status_preserves_flags()
    test_migrate_idempotent()
    test_migrate_dry_run_no_write()
    test_migrate_malformed_untouched()
    print('\n全部通過 ✅')
