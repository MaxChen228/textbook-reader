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


def test_ensure_fills_null_skeleton_groups():
    """ensure：頂層 None 是待補骨架，不應阻止 migration/backfill 寫入 identity/classification。"""
    _isolate()
    ed.save('x', {'identity': None, 'classification': None, 'qualification': None})
    ed.ensure('x', {
        'identity': {'title': 'Concrete Mathematics', 'author': 'Graham, Knuth, Patashnik',
                     'edition_pref': '', 'has_solution': False, 'promoted_from': 'migration'},
        'classification': {'field_id': 'math', 'subject': '離散數學', 'order': [1, 2, 3, 0]},
        'qualification': {'eligible': True, 'verified_at': None},
    })
    e = ed.load('x')
    assert e['identity']['title'] == 'Concrete Mathematics'
    assert e['classification']['field_id'] == 'math'
    assert e['qualification']['eligible'] is True
    before = dict(e)
    ed.ensure('x', {'identity': {'title': 'Wrong'}})
    assert ed.load('x') == before
    print('✓ editions：ensure 會補頂層 None 骨架且不覆蓋既有非空值')


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


def test_new_groups_blank_and_ensure():
    """identity/classification/qualification 3 group：blank 初始 None、ensure 補進舊檔不蓋已有。"""
    _isolate()
    b = ed.blank()
    assert b['identity'] is None and b['classification'] is None and b['qualification'] is None
    # 舊格式檔（只有版本欄）→ ensure 補進 3 新 group
    ed.save('legacy', {'version': {'label': '2nd', 'matches_pref': True}})
    e0 = ed.load('legacy')
    assert e0['classification'] is None and e0['qualification'] is None  # save 也經 blank 骨架補齊
    ed.save('legacy2', {'confidence': 'high'})
    # 模擬真·舊檔（手寫缺新欄）：直接寫一個沒有新 group 的檔，再 ensure
    import os
    from book_pipeline import jsonio
    p = os.path.join(ed.EDITIONS_DIR, 'truly_old.json')
    jsonio.atomic_write_json(p, {'version': {'label': '1st'}, 'confidence': 'low'}, indent=1)
    ed.ensure('truly_old')
    e = ed.load('truly_old')
    assert e['version']['label'] == '1st'                       # 原值不動
    assert 'identity' in e and 'classification' in e and 'qualification' in e  # 缺欄補上 None
    assert e['identity'] is None and e['qualification'] is None
    print('✓ editions：3 新 group blank=None + ensure 補進真·舊檔不蓋已有值')


def test_dims_four_dimensions():
    """dims 四維純判定：eligible/link/version/sol_alignment 各自獨立、解答本維④專屬。"""
    _isolate()
    # 全空 → 四維皆 False（解答本 N/A 維除外）
    d = ed.dims('x', None, {})
    assert d == {'eligible': False, 'link': False, 'version': False, 'sol_alignment': True}
    # 維① eligible：qualification.eligible 須恰為 True（None/False 不算）
    assert ed.dims('x', {'qualification': {'eligible': True}}, {})['eligible'] is True
    assert ed.dims('x', {'qualification': {'eligible': False}}, {})['eligible'] is False
    assert ed.dims('x', {'qualification': {'eligible': None}}, {})['eligible'] is False
    # 維② link：resolution found 或 owned（have）
    assert ed.dims('x', {}, {'x': {'status': 'found'}})['link'] is True
    assert ed.dims('x', {}, {'x': {'status': 'not_found'}})['link'] is False
    assert ed.dims('x', {}, {}, have={'x'})['link'] is True      # owned → 實體在手視同有連結
    # 維③ version：須親查完成 AND matches_pref True
    assert ed.dims('x', {'version': {'label': '3rd', 'matches_pref': True}}, {})['version'] is True
    assert ed.dims('x', {'version': {'label': '2nd', 'matches_pref': False}}, {})['version'] is False
    assert ed.dims('x', {'version': {'label': '2nd'}}, {})['version'] is False  # 沒 matches_pref → 未確認
    # 維④ sol_alignment：僅 _sol slug；母書 N/A=True、解答本須 aligned True
    assert ed.dims('foo_sol', {'sol_alignment': {'aligned': True}}, {})['sol_alignment'] is True
    assert ed.dims('foo_sol', {'sol_alignment': {'aligned': False}}, {})['sol_alignment'] is False
    assert ed.dims('foo_sol', {}, {})['sol_alignment'] is False  # 解答本未查 → False
    assert ed.dims('foo', {}, {})['sol_alignment'] is True       # 非解答本 → N/A 通過
    print('✓ editions：dims 四維獨立判定（owned 視同有連結、version 須 matches_pref、解答本維④專屬）')


def test_qualifies_all_four():
    """qualifies = 四維 AND。主書三維全過即合格；解答本需四維全過。"""
    _isolate()
    main_ok = {'qualification': {'eligible': True}, 'version': {'label': '3rd', 'matches_pref': True}}
    res = {'m': {'status': 'found'}}
    assert ed.qualifies('m', main_ok, res) is True              # 主書：①②③ 全過、④N/A
    assert ed.qualifies('m', main_ok, {}) is False              # 缺連結 → 不合格
    # 解答本：再缺維④ → 不合格；補上 aligned 才合格
    sol = dict(main_ok)
    assert ed.qualifies('m_sol', sol, {'m_sol': {'status': 'found'}}) is False
    sol = {**main_ok, 'sol_alignment': {'aligned': True}}
    assert ed.qualifies('m_sol', sol, {'m_sol': {'status': 'found'}}) is True
    print('✓ editions：qualifies 四維 AND（主書三維、解答本四維全過才合格）')


def test_cmd_set_classification_and_eligible():
    """CLI set 新增 --field-id/--subject/--eligible：寫 classification + qualification（含 verified_at 戳）。"""
    import argparse
    _isolate()
    ns = argparse.Namespace(slug='m', label=None, year=None, publisher=None, isbn=None,
                            matches_pref=None, confidence=None, sol_aligned=None,
                            parent_version=None, sol_version=None, basis=None,
                            field_id='cs', subject='演算法', eligible=True,
                            evidence=None, source=None, by='restock')
    ed.cmd_set(ns)
    e = ed.load('m')
    assert e['classification'] == {'field_id': 'cs', 'subject': '演算法'}
    assert e['qualification']['eligible'] is True and e['qualification']['verified_at']
    # 二次只給 subject → classification 子 dict merge（field_id 保留）
    ns2 = argparse.Namespace(slug='m', label=None, year=None, publisher=None, isbn=None,
                             matches_pref=None, confidence=None, sol_aligned=None,
                             parent_version=None, sol_version=None, basis=None,
                             field_id=None, subject='圖論', eligible=None,
                             evidence=None, source=None, by='restock')
    ed.cmd_set(ns2)
    e2 = ed.load('m')
    assert e2['classification'] == {'field_id': 'cs', 'subject': '圖論'}      # field_id 保留、subject 更新
    assert e2['qualification']['eligible'] is True                            # 沒給 eligible → 前次保留
    print('✓ editions CLI：--field-id/--subject merge + --eligible 寫 qualification + verified_at 戳')


def test_cmd_set_identity():
    """CLI set 身份欄：title/author/edition_pref/has_solution/promoted_from 子 dict merge。"""
    import argparse
    _isolate()
    ns = argparse.Namespace(slug='m', label=None, year=None, publisher=None, isbn=None,
                            matches_pref=None, confidence=None, sol_aligned=None,
                            parent_version=None, sol_version=None, basis=None,
                            title='Introduction to Algorithms', author='Cormen et al.',
                            edition_pref='4th', has_solution=True, promoted_from='discovery',
                            field_id=None, subject=None, eligible=None,
                            evidence=None, source=None, by='restock')
    ed.cmd_set(ns)
    e = ed.load('m')
    assert e['identity'] == {'title': 'Introduction to Algorithms', 'author': 'Cormen et al.',
                             'edition_pref': '4th', 'has_solution': True,
                             'promoted_from': 'discovery'}
    ns2 = argparse.Namespace(slug='m', label=None, year=None, publisher=None, isbn=None,
                             matches_pref=None, confidence=None, sol_aligned=None,
                             parent_version=None, sol_version=None, basis=None,
                             title=None, author='Thomas H. Cormen et al.', edition_pref=None,
                             has_solution=None, promoted_from=None,
                             field_id=None, subject=None, eligible=None,
                             evidence=None, source=None, by='restock')
    ed.cmd_set(ns2)
    e2 = ed.load('m')
    assert e2['identity']['title'] == 'Introduction to Algorithms'
    assert e2['identity']['author'] == 'Thomas H. Cormen et al.'
    assert e2['identity']['has_solution'] is True
    print('✓ editions CLI：identity 子 dict merge')


if __name__ == '__main__':
    test_save_and_load()
    test_save_merges()
    test_ensure_idempotent()
    test_ensure_fills_null_skeleton_groups()
    test_load_all()
    test_cmd_set_and_merge()
    test_cmd_set_sol_alignment()
    test_new_groups_blank_and_ensure()
    test_dims_four_dimensions()
    test_qualifies_all_four()
    test_cmd_set_classification_and_eligible()
    test_cmd_set_identity()
    print('\n全部通過 ✅')
