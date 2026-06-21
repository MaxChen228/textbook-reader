"""migrate_booklists_to_editions 單元測試：uv run python -m book_pipeline.test_migrate_booklists

plan 分桶（只 owned∪ready）+ 分類帶齊（D8）+ qualification eligible/verified_at + has_solution 語意；
migrate 冪等 + owned 保命 + ensure 不蓋 /restock 親查值。隔離 editions tmp dir + monkeypatch bl 資料源。"""

import tempfile

from book_pipeline import booklists as bl
from book_pipeline import editions as ed
from book_pipeline import migrate_booklists_to_editions as mig

_ORIG_ED_DIR = ed.EDITIONS_DIR


def teardown_function(function):
    ed.EDITIONS_DIR = _ORIG_ED_DIR


def _t(slug, kind='main', of=None, **kw):
    return {'slug': slug, 'title': kw.get('title', slug), 'author': kw.get('author', 'Au'),
            'edition_pref': kw.get('edition_pref', ''), 'field': kw.get('field', '資訊'),
            'field_id': kw.get('field_id', 'cs'), 'subject': kw.get('subject', '演算法'),
            'kind': kind, 'of': of,
            'order': kw.get('order', (30, 0, 0, 0 if kind == 'main' else 1)), 'source': 'booklist'}


def test_plan_buckets_and_classification():
    """只收 owned∪ready；classification 帶齊、qualification 信任人工、has_solution 由 _sol target 推。"""
    ts = [_t('owned_book'), _t('owned_book_sol', kind='solution', of='owned_book'),
          _t('ready_book'), _t('unres_book'), _t('nf_book'), _t('rev_book')]
    have = {'owned_book', 'owned_book_sol'}
    resolution = {'ready_book': {'status': 'resolved', 'id': '1', 'hash': 'h', 'by': 'agent'},
                  'nf_book': {'status': 'not_found'}, 'rev_book': {'status': 'review'}}
    todo = mig.plan(ts, have, resolution)
    assert set(todo) == {'owned_book', 'owned_book_sol', 'ready_book'}   # unres/nf/rev 全略過
    c = todo['owned_book']['classification']
    assert c['field_id'] == 'cs' and c['subject'] == '演算法' and isinstance(c['order'], list)
    assert todo['owned_book']['qualification'] == {'eligible': True, 'verified_at': None}
    assert todo['owned_book']['identity']['promoted_from'] == 'migration'
    assert todo['owned_book']['identity']['has_solution'] is True      # 有衍生 _sol target
    assert todo['owned_book_sol']['identity']['has_solution'] is False  # 解答本自身不再有解答
    assert todo['ready_book']['identity']['has_solution'] is False     # 無 _sol target
    print('✓ plan：只 owned∪ready、分類帶齊、qualification 信任人工、has_solution 由 _sol target 推')


def test_migrate_idempotent_and_owned_safe(monkeypatch):
    """migrate 冪等；owned 帶齊分類不降級；ensure 絕不蓋 /restock 已親查的版本/verified_at。"""
    ed.EDITIONS_DIR = tempfile.mkdtemp(prefix='ed_mig_')
    ts = [_t('owned_book'), _t('owned_book_sol', kind='solution', of='owned_book'),
          _t('ready_book'), _t('unres_book')]
    have = {'owned_book', 'owned_book_sol'}
    res = {'ready_book': {'status': 'resolved', 'id': '1', 'hash': 'h', 'by': 'agent'}}
    monkeypatch.setattr(bl, 'load_files', lambda *a, **k: [])
    monkeypatch.setattr(bl, 'targets', lambda *a, **k: ts)
    monkeypatch.setattr(bl, 'have_slugs', lambda *a, **k: have)
    monkeypatch.setattr(bl, 'load_resolution', lambda *a, **k: res)

    r1 = mig.migrate()
    assert set(r1['created']) == {'owned_book', 'owned_book_sol', 'ready_book'}
    assert r1['skipped_unlinked'] == 1                                  # unres_book 略過
    e = ed.load('owned_book')
    assert e['classification']['field_id'] == 'cs' and e['qualification']['eligible'] is True
    assert e['version'] is None and e['sol_alignment'] is None          # 維③④ 標未驗
    assert e['by'] == 'migration'

    # /restock 後續親查 owned_book 版本 + 蓋 verified_at → 重跑遷移不得覆蓋（ensure 只補缺欄）
    ed.save('owned_book', {'version': {'label': '3rd', 'matches_pref': True},
                           'qualification': {'eligible': True, 'verified_at': '2026-06-21T00:00:00+00:00'}})
    r2 = mig.migrate()
    assert r2['created'] == [] and len(r2['refreshed']) == 3            # 全已存在
    e2 = ed.load('owned_book')
    assert e2['version']['label'] == '3rd'                              # 親查版本保留
    assert e2['qualification']['verified_at'] == '2026-06-21T00:00:00+00:00'  # 不被遷移 None 蓋回
    print('✓ migrate：冪等 + owned 帶齊分類不降級 + ensure 不蓋 /restock 親查值')


if __name__ == '__main__':
    test_plan_buckets_and_classification()
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
