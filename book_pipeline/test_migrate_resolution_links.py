"""migrate_resolution_to_links 單元測試：uv run python -m book_pipeline.test_migrate_resolution_links

transform 退化規則（resolved→found / absent→not_found / review·vu→strip+legacy_status / 同名不動）+
連結資料保留 + 冪等護欄（legacy_status 防 id/hash 殘留被誤升 found）+ migrate 冪等 + dry-run 不寫。
隔離 tmp resolution 檔重指 bl.RESOLUTION。"""

import os
import tempfile

from book_pipeline import booklists as bl
from book_pipeline import jsonio
from book_pipeline import migrate_resolution_to_links as mig

_ORIG_RESOLUTION = bl.RESOLUTION


def teardown_function(function):
    bl.RESOLUTION = _ORIG_RESOLUTION


def _isolate(entries):
    path = os.path.join(tempfile.mkdtemp(prefix='res_links_'), 'crawl_resolution.json')
    jsonio.atomic_write_json(path, entries, indent=1)
    bl.RESOLUTION = path
    return path


def test_transform_rules():
    assert mig.transform({'status': 'resolved', 'id': '1', 'hash': 'h', 'by': 'agent'})[1] == 'found'
    assert mig.transform({'id': '1', 'hash': 'h'})[1] == 'found'        # legacy 無 status
    assert mig.transform({'absent': True})[1] == 'not_found'            # legacy absent
    assert mig.transform({'status': 'not_found'}) is None              # 同名不動
    assert mig.transform({'status': 'found', 'id': '1'}) is None       # 已退化不動
    rev = mig.transform({'status': 'review', 'id': '1', 'hash': 'h', 'note': 'ambig'})
    assert rev[1] == 'strip' and rev[0].get('status') is None and rev[0]['legacy_status'] == 'review'
    assert rev[0]['id'] == '1' and rev[0]['note'] == 'ambig'           # 連結/其餘資料保留
    vu = mig.transform({'status': 'version_unavailable', 'recheck_after': 'x'})
    assert vu[1] == 'strip' and vu[0]['legacy_status'] == 'version_unavailable'
    # 冪等護欄：已 strip（有 legacy_status + 殘留 id/hash）不得被誤升回 found
    assert mig.transform({'legacy_status': 'review', 'id': '1', 'hash': 'h'}) is None
    assert mig.transform({'note': 'junk'}) is None                     # 畸形不動
    print('✓ transform：四規則 + 連結保留 + legacy_status 冪等護欄')


def test_found_preserves_all_link_data():
    e, action = mig.transform({'status': 'resolved', 'id': '42', 'hash': 'abc', 'by': 'agent',
                               'title': 'X', 'href': '/b/42', 'cover': 'c.jpg', 'at': 't'})
    assert action == 'found'
    assert e == {'status': 'found', 'id': '42', 'hash': 'abc', 'by': 'agent',
                 'title': 'X', 'href': '/b/42', 'cover': 'c.jpg', 'at': 't'}
    print('✓ found：連結資料全保留、僅 status 改名')


def test_migrate_idempotent_and_dry_run():
    _isolate({'a': {'status': 'resolved', 'id': '1', 'hash': 'h', 'by': 'agent'},
              'b': {'status': 'not_found'},
              'c': {'status': 'review', 'id': '9', 'hash': 'k'},
              'd': {'id': '2', 'hash': 'g'}})
    r1 = mig.migrate()
    assert r1['changed'] == 3 and r1['untouched'] == 1                 # a,c,d 變；b 不動
    res = bl.load_resolution()
    assert res['a']['status'] == 'found' and res['d']['status'] == 'found'
    assert res['b']['status'] == 'not_found'
    assert res['c'].get('status') is None and res['c']['legacy_status'] == 'review' and res['c']['id'] == '9'
    r2 = mig.migrate()                                                  # 冪等
    assert r2['changed'] == 0 and bl.load_resolution() == res
    print('✓ migrate：退化正確 + 冪等（含 strip 後不回升 found）')


def test_dry_run_no_write():
    path = _isolate({'a': {'status': 'resolved', 'id': '1', 'hash': 'h'}})
    before = jsonio.read_json(path)
    mig.migrate(dry_run=True)
    assert jsonio.read_json(path) == before                            # dry-run 不落盤
    print('✓ dry-run：不寫檔')


if __name__ == '__main__':
    test_transform_rules()
    test_found_preserves_all_link_data()
    test_migrate_idempotent_and_dry_run()
    test_dry_run_no_write()
    print('全部通過 ✅')
