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


if __name__ == '__main__':
    test_save_and_load()
    test_save_merges()
    test_ensure_idempotent()
    test_load_all()
    print('\n全部通過 ✅')
