"""書目狀態衍生層單元測試（合格存在五態，universe=editions）：uv run python -m book_pipeline.test_booklists

涵蓋：targets 由 editions 派生（主書記錄 + has_solution 衍生 _sol、按 fields 排序、discovered 併入）、
五態衍生（OWNED 保命 / QUALIFIED 四維 / PENDING 有連結未驗 / CANDIDATE 無連結 / REJECTED）、_public_status
摺疊、select_next 只取 QUALIFIED、pending_targets + recheck cooldown（resting 不重派）、crawl_work_remaining
latch、pool_counts 鍵、catalog 形狀（no-editions 書消失）、validate（editions 完整性）、reconcile。
全部注入 all_eds/have/resolution → 不碰 editions 磁碟；fields 用 tmp fixture 重指 FIELDS_JSON。"""

import datetime as dt
import os
import tempfile

from book_pipeline import booklists as bl
from book_pipeline import fields as fields_mod
from book_pipeline import jsonio

_ORIG_FIELDS = fields_mod.FIELDS_JSON
_NOW = dt.datetime(2026, 6, 21, tzinfo=dt.timezone.utc)


def setup_function(function):
    p = os.path.join(tempfile.mkdtemp(prefix='blfields_'), 'fields.json')
    jsonio.atomic_write_json(p, [{'field_id': 'physics', 'field': '物理', 'order': 10},
                                 {'field_id': 'math', 'field': '數學', 'order': 20}], indent=1)
    fields_mod.FIELDS_JSON = p


def teardown_function(function):
    fields_mod.FIELDS_JSON = _ORIG_FIELDS


def _rec(field_id, subject, order, has_solution=False, eligible=True, version_ok=None,
         sol_aligned=None, title='T', author='Au', edition_pref='', checked_at=None):
    """合成 editions 記錄（universe 單位）。order=(_, subject_rank, book_rank, _)；targets 重算 field_order。"""
    return {
        'identity': {'title': title, 'author': author, 'edition_pref': edition_pref,
                     'has_solution': has_solution, 'promoted_from': 'migration'},
        'classification': {'field_id': field_id, 'subject': subject, 'order': list(order)},
        'qualification': {'eligible': eligible},
        'version': {'label': '1st', 'matches_pref': version_ok} if version_ok is not None else None,
        'sol_alignment': {'aligned': sol_aligned} if sol_aligned is not None else None,
        'checked_at': checked_at,
    }


def _found(i='1', h='h'):
    return {'status': 'found', 'id': i, 'hash': h, 'by': 'agent'}


def test_targets_from_editions():
    """targets 由 editions 主書記錄派生：has_solution → 衍生 _sol、按 (field_order, subject, book) 排序。"""
    eds = {'jackson': _rec('physics', '電動力學', (0, 0, 0, 0), has_solution=True),
           'rudin': _rec('math', '分析', (0, 0, 0, 0), has_solution=False)}
    ts = bl.targets(eds)
    slugs = [t['slug'] for t in ts]
    assert slugs == ['jackson', 'jackson_sol', 'rudin'], slugs   # physics(10)<math(20)；jackson 衍生 sol
    j = next(t for t in ts if t['slug'] == 'jackson')
    assert j['field'] == '物理' and j['field_id'] == 'physics'     # field 顯示名 join fields.json
    sol = next(t for t in ts if t['slug'] == 'jackson_sol')
    assert sol['kind'] == 'solution' and sol['of'] == 'jackson'
    assert 'rudin_sol' not in slugs                              # has_solution=False → 不衍生
    print('✓ targets：由 editions 主書派生 + has_solution 衍生 _sol + fields 排序/顯示名')


def test_status_five_states():
    eds = {'m': _rec('physics', 's', (0, 0, 0, 0))}
    assert bl.status_of('m', {'m'}, {'m': _found()}, eds['m']) == bl.OWNED        # OWNED 最優先
    eds['m']['version'] = {'matches_pref': True}
    assert bl.status_of('m', set(), {'m': _found()}, eds['m']) == bl.QUALIFIED    # 四維全過
    eds['m']['version'] = None
    assert bl.status_of('m', set(), {'m': _found()}, eds['m']) == bl.PENDING      # 有連結缺維③
    assert bl.status_of('m', set(), {}, eds['m']) == bl.CANDIDATE                 # 無連結
    assert bl.status_of('m', set(), {'m': {'status': 'not_found'}}, eds['m']) == bl.REJECTED
    eds['m']['qualification'] = {'eligible': False}
    assert bl.status_of('m', set(), {'m': _found()}, eds['m']) == bl.REJECTED     # 判不夠格（有連結仍 REJECTED）
    for old in ('READY', 'UNRESOLVED', 'NOT_FOUND', 'REVIEW', 'VERSION_UNAVAILABLE'):
        assert not hasattr(bl, old), old
    print('✓ status：五態（OWNED 保命 / QUALIFIED / PENDING / CANDIDATE / REJECTED）')


def test_solution_dim_four():
    base = dict(version_ok=True)
    sol_pend = _rec('physics', 's', (0, 0, 0, 1), **base)
    assert bl.status_of('m_sol', set(), {'m_sol': _found()}, sol_pend) == bl.PENDING   # 缺維④
    sol_ok = _rec('physics', 's', (0, 0, 0, 1), sol_aligned=True, **base)
    assert bl.status_of('m_sol', set(), {'m_sol': _found()}, sol_ok) == bl.QUALIFIED
    print('✓ 解答本維④：sol_aligned 才 QUALIFIED')


def test_public_status_folding():
    assert bl._public_status(bl.OWNED) == 'owned'
    assert bl._public_status(bl.QUALIFIED) == 'ready' and bl._public_status(bl.PENDING) == 'ready'
    assert bl._public_status(bl.CANDIDATE) == 'unresolved'
    assert bl._public_status(bl.REJECTED) == 'absent'
    print('✓ _public_status：五態摺疊回舊公開字串（reader 零改）')


def test_select_next_only_qualified():
    eds = {'jackson': _rec('physics', 's', (0, 0, 0, 0), version_ok=True),
           'rudin': _rec('math', 's', (0, 0, 0, 0), version_ok=None)}  # rudin PENDING（缺版本）
    res = {'jackson': _found('20', 'j'), 'rudin': _found('10', 'r')}
    picks = bl.select_next(5, eds, set(), res)
    assert [p['slug'] for p in picks] == ['jackson'], picks       # 只 QUALIFIED；PENDING 不下載
    eds['rudin']['version'] = {'matches_pref': True}
    assert [p['slug'] for p in bl.select_next(5, eds, set(), res)] == ['jackson', 'rudin']
    assert bl.select_next(0, eds, set(), res) == []
    print('✓ select_next：只取 QUALIFIED、按序、限量 n')


def test_select_next_owned_exclude_malformed():
    eds = {'jackson': _rec('physics', 's', (0, 0, 0, 0), version_ok=True),
           'rudin': _rec('math', 's', (0, 0, 0, 0), version_ok=True)}
    res = {'jackson': _found('1', 'a'), 'rudin': _found('2', 'b')}
    assert bl.select_next(5, eds, {'jackson'}, res, exclude={'rudin'}) == []
    assert [p['slug'] for p in bl.select_next(5, eds, {'jackson'}, res)] == ['rudin']
    res['rudin'] = {'status': 'found', 'id': ['2'], 'hash': 'b', 'by': 'a'}      # 畸形 id
    assert bl.select_next(5, eds, {'jackson'}, res) == []
    print('✓ select_next：owned/exclude 不選、畸形 id 跳過')


def test_pending_candidate_targets():
    eds = {'a': _rec('physics', 's', (0, 0, 0, 0), version_ok=None),    # 有連結未驗 → PENDING
           'b': _rec('math', 's', (0, 0, 1, 0))}                        # 無連結 → CANDIDATE
    res = {'a': _found()}
    pend = [t['slug'] for t in bl.pending_targets(eds, set(), res, now=_NOW)]
    cand = [t['slug'] for t in bl.unresolved_targets(eds, set(), res)]
    assert pend == ['a'], pend
    assert cand == ['b'], cand
    print('✓ pending_targets（有連結未驗）/ unresolved_targets（無連結）分流')


def test_pending_recheck_cooldown():
    """recheck cooldown：近期親查仍 PENDING（resting）→ 不在 pending_targets / crawl_work_remaining（防
    busy-loop）；checked_at=None（從未查）或窗到期 → actionable。"""
    recent = (_NOW - dt.timedelta(days=5)).isoformat()
    old = (_NOW - dt.timedelta(days=bl.RECHECK_COOLDOWN_DAYS + 5)).isoformat()
    eds = {'fresh': _rec('physics', 's', (0, 0, 0, 0), version_ok=None, checked_at=None),   # 從未查
           'resting': _rec('physics', 's', (0, 0, 1, 0), version_ok=None, checked_at=recent),  # 近期查過
           'expired': _rec('physics', 's', (0, 0, 2, 0), version_ok=None, checked_at=old)}   # 窗到期
    res = {s: _found() for s in eds}
    pend = {t['slug'] for t in bl.pending_targets(eds, set(), res, now=_NOW)}
    assert pend == {'fresh', 'expired'}, pend                    # resting 排除
    assert bl.crawl_work_remaining(eds, set(), res) == 2          # latch 只算 actionable
    # 全 resting → work_remaining 0（收斂、不 busy-loop）
    eds2 = {'r': _rec('physics', 's', (0, 0, 0, 0), version_ok=None, checked_at=recent)}
    assert bl.crawl_work_remaining(eds2, set(), {'r': _found()}) == 0
    print('✓ recheck cooldown：resting 排除工作母體、checked_at=None/窗到期 actionable、全 resting→latch')


def test_pool_counts_keys():
    eds = {'j': _rec('physics', 's', (0, 0, 0, 0), version_ok=True),
           'g': _rec('physics', 's', (0, 0, 1, 0)),
           'r': _rec('math', 's', (0, 0, 0, 0), version_ok=None)}
    res = {'j': _found(), 'g': {'status': 'not_found'}, 'r': _found()}
    pc = bl.pool_counts(eds, set(), res)
    assert pc['confirmed'] == 1 and pc['ready'] == 1 and pc['qualified_ready'] == 1   # j
    assert pc['pending'] == 1 and pc['rejected'] == 1 and pc['not_found'] == 1
    assert pc['unresolved'] == pc['candidate'] and pc['review'] == 0
    print('✓ pool_counts：合格/pending/rejected + 向後相容鍵')


def test_progress_tally():
    eds = {'j': _rec('physics', 's', (0, 0, 0, 0)),                # owned（下方 have）
           'r': _rec('math', 's', (0, 0, 0, 0), version_ok=True),  # QUALIFIED
           'g': _rec('physics', 's', (0, 0, 1, 0))}                # not_found→REJECTED
    res = {'r': _found(), 'g': {'status': 'not_found'}}
    pr = bl.progress(eds, {'j'}, res)
    o = pr['overall']
    assert o[bl.OWNED] == 1 and o[bl.QUALIFIED] == 1 and o[bl.REJECTED] == 1
    assert o['ready'] == 1 and o['absent'] == 1                   # 向後相容鍵
    print('✓ progress：五態統計 + 向後相容鍵')


def test_catalog_shape_and_no_link_vanish():
    """catalog universe = editions：no-editions 書消失（落實沒連結＝不存在）；五態摺疊回公開三態。"""
    eds = {'jackson': _rec('physics', '電動力學', (0, 0, 0, 0), has_solution=True),
           'rudin': _rec('math', '分析', (0, 0, 0, 0), version_ok=None)}
    res = {'rudin': _found(), 'jackson_sol': {'status': 'not_found'}}
    cat = bl.catalog(eds, {'jackson'}, res)
    by_slug = {b['slug']: b for f in cat['fields'] for sl in f['sublists'] for b in sl['books']}
    assert set(by_slug) == {'jackson', 'rudin'}                  # 只 editions 主書；ghost 書不在
    assert by_slug['jackson']['status'] == 'owned'
    assert by_slug['jackson']['sol_status'] == 'absent'          # jackson_sol not_found → absent
    assert by_slug['rudin']['status'] == 'ready'                 # PENDING → public ready
    assert 'sol_status' not in by_slug['rudin']                  # has_solution=False
    assert 'ghost' not in by_slug                                # 沒 editions 的書不在收錄表
    print('✓ catalog：universe=editions（no-editions 書消失）+ 五態摺疊三態 + sol_status')


def test_validate_editions_integrity():
    good = {'m': _rec('physics', 's', (0, 0, 0, 0), has_solution=True),
            'm_sol': _rec('physics', 's', (0, 0, 0, 1))}
    assert bl.validate(good) == []
    bad = {'M': _rec('physics', 's', (0, 0, 0, 0)),               # 大寫非法
           'x': {'identity': {}, 'classification': {}},           # 缺 title/author/field_id
           'orphan_sol': _rec('physics', 's', (0, 0, 0, 1))}      # 母書 orphan 無 editions
    errs = bl.validate(bad)
    assert any('M' in e for e in errs) and any('x' in e for e in errs)
    assert any('orphan' in e and '母書' in e for e in errs), errs
    print('✓ validate：editions slug/identity/classification 完整性 + 孤兒解答本')


def test_reconcile_owned():
    eds = {'jackson': _rec('physics', 's', (0, 0, 0, 0), has_solution=True),
           'rudin': _rec('math', 's', (0, 0, 0, 0))}
    have = {'jackson', 'rudin', 'feynman_em2', 'jackson_sol', 'rudin_sol'}
    r = bl.reconcile_owned(eds, have)
    assert r['inventory_not_in_sot'] == ['feynman_em2']           # owned 但 editions 無記錄
    assert r['owned_sol_not_in_sot'] == ['rudin_sol']            # owned 題本但 rudin has_solution=false
    print('✓ reconcile：owned 無 editions / owned 題本主書未衍生')


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
