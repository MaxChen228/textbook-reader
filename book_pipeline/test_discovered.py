"""discovered 候選層 + targets 合併單元測試：uv run python -m book_pipeline.test_discovered

add 去重（vs existing + 既有候選 + 非法 slug + _sol）、iter_candidates、remove、cmd_add rc、
booklists.targets 合併 discovered（人工優先、排其後、標 source、衍生 _sol、撞人工跳、可關閉）。
用 tmpdir 重指 discovered.DISCOVERED_DIR，不碰 repo；teardown 還原（booklists.targets 也讀同模組變數）。"""

import tempfile

from book_pipeline import booklists as bl
from book_pipeline import discovered as dc

_ORIG = dc.DISCOVERED_DIR


def teardown_function(function):
    dc.DISCOVERED_DIR = _ORIG


def _isolate():
    dc.DISCOVERED_DIR = tempfile.mkdtemp(prefix='discovered_')


def test_add_dedup():
    _isolate()
    existing = {'griffiths_qm', 'rudin'}
    cands = [
        {'slug': 'sakurai_qm', 'title': 'Modern QM', 'author': 'Sakurai'},   # 新 → 收
        {'slug': 'rudin', 'title': 'dup', 'author': 'X'},                     # 撞 existing → skip
        {'slug': 'BAD SLUG', 'title': 'x', 'author': 'y'},                    # 非法 slug → skip
        {'slug': 'foo_sol', 'title': 'x', 'author': 'y'},                     # _sol 結尾 → skip
    ]
    r = dc.add('physics', '物理', cands, existing)
    assert r['added'] == 1 and r['skipped'] == 3, r
    r2 = dc.add('physics', '物理', [{'slug': 'sakurai_qm', 'title': 'x', 'author': 'y'}], existing)
    assert r2['added'] == 0, r2                                   # 已在候選 → 冪等 skip
    print('✓ discovered.add：去重（existing/既有/非法/_sol）+ 冪等')


def test_iter_and_remove():
    _isolate()
    dc.add('physics', '物理', [{'slug': 'a_book', 'title': 'A', 'author': 'X'}], set())
    dc.add('math', '數學', [{'slug': 'b_book', 'title': 'B', 'author': 'Y'}], set())
    cands = dc.iter_candidates()
    assert {c['slug'] for c in cands} == {'a_book', 'b_book'}
    assert all('field_id' in c and 'field' in c for c in cands)
    assert dc.remove('physics', 'a_book') is True
    assert dc.remove('physics', 'a_book') is False               # 已移除 → False
    assert {c['slug'] for c in dc.iter_candidates()} == {'b_book'}
    print('✓ discovered：iter_candidates 附 field + remove 否決')


def _ed(field_id, subject, order, title='T', author='Au'):
    return {'identity': {'title': title, 'author': author, 'edition_pref': '',
                         'has_solution': False, 'promoted_from': 'migration'},
            'classification': {'field_id': field_id, 'subject': subject, 'order': list(order)},
            'qualification': {'eligible': True}, 'version': None, 'sol_alignment': None, 'checked_at': None}


def test_targets_merges_discovered():
    """booklists.targets 合併 discovered（universe=editions）：排 editions 後、標 source、衍生 _sol、撞 slug 跳、可關閉。"""
    _isolate()
    all_eds = {'taylor_mech': _ed('physics', '力學', (10, 0, 0, 0))}   # editions universe（人工正典遷入）
    dc.add('physics', '物理', [
        {'slug': 'sakurai_qm', 'title': 'Modern QM', 'author': 'Sakurai'},   # 新 main + 衍生 _sol
        {'slug': 'taylor_mech', 'title': 'dup', 'author': 'X'},              # 撞 editions → 跳
    ], {'taylor_mech'})
    ts = bl.targets(all_eds)
    slugs = [t['slug'] for t in ts]
    assert 'sakurai_qm' in slugs and 'sakurai_qm_sol' in slugs
    assert slugs.index('taylor_mech') < slugs.index('sakurai_qm')   # editions 優先（排前、order<10000）
    sk = next(t for t in ts if t['slug'] == 'sakurai_qm')
    assert sk['source'] == 'discovered' and sk['kind'] == 'main'
    assert next(t for t in ts if t['slug'] == 'taylor_mech')['source'] != 'discovered'  # editions 書
    assert slugs.count('taylor_mech') == 1                          # 撞 editions 被跳、不重複
    base = [t['slug'] for t in bl.targets(all_eds, include_discovered=False)]
    assert 'sakurai_qm' not in base and 'taylor_mech' in base       # 可關閉 → 純 editions universe
    print('✓ targets：合併 discovered（editions 優先、排其後、標 source、衍生 _sol、撞 slug 跳、可關閉）')


def test_cmd_add_rc(monkeypatch):
    """cmd_add：新候選 rc=0、去重 skip rc=1（existing 用 booklists 人工+inventory）。"""
    import argparse
    _isolate()
    monkeypatch.setattr(bl, 'targets', lambda include_discovered=True: [{'slug': 'existing_book'}])
    monkeypatch.setattr(bl, 'have_slugs', lambda: set())
    ns = argparse.Namespace(field_id='physics', field='物理', slug='new_book', title='New',
                            author='A', subject='QM', no_solution=False, note='test')
    assert dc.cmd_add(ns) == 0                                    # 新 → rc 0
    ns2 = argparse.Namespace(field_id='physics', field='物理', slug='existing_book', title='X',
                             author='Y', subject='', no_solution=False, note='')
    assert dc.cmd_add(ns2) == 1                                   # 撞 existing → skip → rc 1
    print('✓ discovered cmd_add：新候選 rc=0、去重 skip rc=1')


if __name__ == '__main__':
    test_add_dedup()
    test_iter_and_remove()
    test_targets_merges_discovered()
    print('（test_cmd_add_rc 需 pytest monkeypatch，於 pytest 跑）')
    print('\n全部通過 ✅')
