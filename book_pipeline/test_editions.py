"""editions 持久層單元測試：uv run python -m book_pipeline.test_editions

load/save merge、ensure 冪等補骨架（不蓋已有值）、load_all。用 tmpdir 重指 EDITIONS_DIR，不碰 repo；
teardown_function 還原模組變數，避免污染其他測試檔（pytest 同 process）。"""

import tempfile

from book_pipeline import editions as ed

_ORIG_DIR = ed.EDITIONS_DIR


def teardown_function(function):
    ed.EDITIONS_DIR = _ORIG_DIR


def _isolate():
    ed.EDITIONS_DIR = tempfile.mkdtemp(prefix='editions_')


def test_save_and_load():
    _isolate()
    assert ed.load('griffiths_qm') is None                      # 無檔 → None（尚未查證）
    ed.save('griffiths_qm', {'version': {'label': '3rd', 'matches_pref': True},
                             'confidence': 'high', 'by': 'booklist-manager'})
    e = ed.load('griffiths_qm')
    assert e['version']['label'] == '3rd' and e['confidence'] == 'high'
    assert e['evidence'] == [] and e['sources'] == []           # blank 骨架其餘欄補齊
    print('✓ editions：save 寫入 + load 讀回 + blank 骨架補齊')


def test_save_merges():
    _isolate()
    ed.save('x', {'version': {'label': '2nd'}, 'confidence': 'low'})
    ed.save('x', {'confidence': 'high'})                         # 只更新 confidence
    e = ed.load('x')
    assert e['version']['label'] == '2nd' and e['confidence'] == 'high'  # version 保留、confidence 覆蓋
    print('✓ editions：save merge（更新部分欄、保留其餘）')


def test_ensure_idempotent():
    _isolate()
    ed.save('x', {'version': {'label': '4th'}, 'confidence': 'high'})
    ed.ensure('x')                                              # 補骨架不該蓋已有值
    e = ed.load('x')
    assert e['version']['label'] == '4th' and e['confidence'] == 'high'  # 原值原樣保留
    assert 'sol_alignment' in e                                 # 缺欄補上
    ed.ensure('fresh', {'by': 'migrate'})                       # 全新 slug → 建骨架
    f = ed.load('fresh')
    assert f['version'] is None and f['by'] == 'migrate'
    before = dict(f)
    ed.ensure('fresh')                                          # 二次 → 冪等不變
    assert ed.load('fresh') == before
    print('✓ editions：ensure 補缺欄不蓋已有值、新 slug 建骨架、重跑冪等')


def test_load_all():
    _isolate()
    ed.save('a', {'version': {'label': '1st'}})
    ed.save('b', {'version': {'label': '2nd'}})
    allm = ed.load_all()
    assert set(allm) == {'a', 'b'} and allm['a']['version']['label'] == '1st'
    print('✓ editions：load_all 全表')


def test_cmd_set_and_merge():
    """CLI cmd_set：組裝 version + append evidence/source + 自動戳 + version 子 dict merge。"""
    import argparse
    _isolate()
    ns = argparse.Namespace(slug='griffiths_qm', label='3rd', year=2018, publisher='Cambridge',
                            isbn='9781107189638', matches_pref=True, confidence='high',
                            evidence=['multi-source consensus'], source=['zlib_detail:x/y'],
                            by='booklist-manager')
    ed.cmd_set(ns)
    e = ed.load('griffiths_qm')
    assert e['version'] == {'label': '3rd', 'year': 2018, 'publisher': 'Cambridge',
                            'isbn': '9781107189638', 'matches_pref': True}
    assert e['confidence'] == 'high' and e['evidence'] == ['multi-source consensus']
    assert e['sources'] == [{'note': 'zlib_detail:x/y'}] and e['by'] == 'booklist-manager' and e['checked_at']
    # 第二次只給 confidence → version 子 dict 整組保留、confidence 更新
    ns2 = argparse.Namespace(slug='griffiths_qm', label=None, year=None, publisher=None, isbn=None,
                             matches_pref=None, confidence='medium', evidence=None, source=None,
                             by='booklist-manager')
    ed.cmd_set(ns2)
    e2 = ed.load('griffiths_qm')
    assert e2['version']['label'] == '3rd' and e2['version']['year'] == 2018  # 整組版本保留
    assert e2['confidence'] == 'medium'                                       # confidence 更新
    assert e2['evidence'] == ['multi-source consensus']                       # 沒給 evidence → 前次保留
    # 第三次帶新 evidence/source → 真 append（累積查證軌跡、不丟前次）
    ns3 = argparse.Namespace(slug='griffiths_qm', label=None, year=None, publisher=None, isbn=None,
                             matches_pref=None, confidence=None, evidence=['second pass'],
                             source=['web:reconfirm'], by='booklist-manager')
    ed.cmd_set(ns3)
    e3 = ed.load('griffiths_qm')
    assert e3['evidence'] == ['multi-source consensus', 'second pass'], e3['evidence']
    assert e3['sources'] == [{'note': 'zlib_detail:x/y'}, {'note': 'web:reconfirm'}], e3['sources']
    print('✓ editions CLI：cmd_set version merge + evidence/source 真 append（多次查證不丟前次）+ 自動戳')


def test_cmd_set_sol_alignment():
    """解答本版本對齊（sol_alignment）寫入 + 子 dict merge（aligned 改判不丟 parent/sol_version）。"""
    import argparse
    _isolate()
    ns = argparse.Namespace(slug='halliday_sol', label=None, year=None, publisher=None, isbn=None,
                            matches_pref=None, confidence='high', sol_aligned=False,
                            parent_version='11th', sol_version='10th', basis='LLM 親判題號錯位',
                            evidence=None, source=None, by='booklist-manager')
    ed.cmd_set(ns)
    e = ed.load('halliday_sol')
    assert e['sol_alignment'] == {'aligned': False, 'parent_version': '11th',
                                  'sol_version': '10th', 'basis': 'LLM 親判題號錯位'}
    ns2 = argparse.Namespace(slug='halliday_sol', label=None, year=None, publisher=None, isbn=None,
                             matches_pref=None, confidence=None, sol_aligned=True,
                             parent_version=None, sol_version=None, basis=None,
                             evidence=None, source=None, by='booklist-manager')
    ed.cmd_set(ns2)                                                # 找到對版後改判 aligned=True
    e2 = ed.load('halliday_sol')
    assert e2['sol_alignment']['aligned'] is True and e2['sol_alignment']['parent_version'] == '11th'
    print('✓ editions CLI：sol_alignment 寫入 + 子 dict merge（aligned 改判不丟 parent/sol_version）')


if __name__ == '__main__':
    test_save_and_load()
    test_save_merges()
    test_ensure_idempotent()
    test_load_all()
    test_cmd_set_and_merge()
    test_cmd_set_sol_alignment()
    print('\n全部通過 ✅')
