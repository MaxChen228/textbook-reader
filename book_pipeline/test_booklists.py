"""書目狀態衍生層單元測試（合格存在五態）：uv run python -m book_pipeline.test_booklists

涵蓋：targets 攤平/解答本衍生/排序、五態衍生（OWNED 保命 / QUALIFIED 四維全過 / PENDING 有連結未驗 /
CANDIDATE 無連結 / REJECTED 真無或不夠格）、_public_status 摺疊、select_next 只取 QUALIFIED、
pending/unresolved_targets、pool_counts 鍵、progress、validate、reconcile_owned。
全部合成 fixture + 注入 have/resolution/all_eds（edition dict）→ 不碰磁碟、純函式可重現。"""

import json
import os
import tempfile

from book_pipeline import booklists as bl


def _file(field_id, order, sublists):
    return {'field': field_id, 'field_id': field_id, 'order': order, 'sublists': sublists}


def _sl(name, books):
    return {'name': name, 'books': books}


def _b(slug, solution=True, title=None, author='A. Author', edition_pref=''):
    d = {'slug': slug, 'title': title or slug.title(), 'author': author}
    if edition_pref:
        d['edition_pref'] = edition_pref
    if solution is not True:
        d['solution'] = solution
    return d


def _ed(eligible=True, version_ok=True, sol_aligned=None):
    """合成 edition：維① eligible、維③ version(matches_pref)、維④ sol_alignment。"""
    e = {'qualification': {'eligible': eligible} if eligible is not None else {},
         'version': {'label': '1st', 'matches_pref': version_ok} if version_ok is not None else None,
         'sol_alignment': {'aligned': sol_aligned} if sol_aligned is not None else None}
    return e


def _fixture():
    return [
        _file('physics', 10, [
            _sl('電動力學', [_b('jackson'), _b('griffiths_em', solution=False)]),
        ]),
        _file('math', 20, [
            _sl('分析', [_b('rudin')]),
        ]),
    ]


def test_targets_solution_expansion_and_order():
    ts = bl.targets(_fixture())
    slugs = [t['slug'] for t in ts]
    assert slugs == ['jackson', 'jackson_sol', 'griffiths_em', 'rudin', 'rudin_sol'], slugs
    sol = next(t for t in ts if t['slug'] == 'jackson_sol')
    assert sol['kind'] == 'solution' and sol['of'] == 'jackson'
    print('✓ targets：solution!=false 衍生 <slug>_sol 緊鄰主書、false 不衍生、跨領域按 order')


def test_status_five_states():
    """五態：OWNED 保命最優先 / QUALIFIED 四維全過 / PENDING 有連結未驗 / CANDIDATE 無連結 / REJECTED。"""
    have = {'jackson'}
    found = {'status': 'found', 'id': '1', 'hash': 'a', 'by': 'agent'}
    # OWNED 最優先（即使有連結、即使無 editions、即使未驗）
    assert bl.status_of('jackson', have, {'jackson': found}, None) == bl.OWNED
    # QUALIFIED：有連結 + 四維全過（主書三維）
    assert bl.status_of('m', set(), {'m': found}, _ed(eligible=True, version_ok=True)) == bl.QUALIFIED
    # PENDING：有連結但維③未過（版本未確認）
    assert bl.status_of('m', set(), {'m': found}, _ed(eligible=True, version_ok=None)) == bl.PENDING
    # PENDING：有連結但維③版本不符偏好
    assert bl.status_of('m', set(), {'m': found}, _ed(eligible=True, version_ok=False)) == bl.PENDING
    # PENDING：有連結但維①未驗（eligible None）—— 遷入存量 eligible=True 故多直接看版本，這測 None 情形
    assert bl.status_of('m', set(), {'m': found}, _ed(eligible=None, version_ok=True)) == bl.PENDING
    # CANDIDATE：無連結（無 resolution）
    assert bl.status_of('m', set(), {}, _ed(eligible=True, version_ok=True)) == bl.CANDIDATE
    assert bl.status_of('m', set(), {}, None) == bl.CANDIDATE
    # REJECTED：z-lib 真無
    assert bl.status_of('m', set(), {'m': {'status': 'not_found'}}, None) == bl.REJECTED
    assert bl.status_of('m', set(), {'m': {'absent': True}}, None) == bl.REJECTED  # legacy 向後相容
    # REJECTED：LLM 判不夠格（即使有連結）
    assert bl.status_of('m', set(), {'m': found}, _ed(eligible=False)) == bl.REJECTED
    # 舊常數已移除
    for old in ('READY', 'UNRESOLVED', 'NOT_FOUND', 'REVIEW', 'VERSION_UNAVAILABLE'):
        assert not hasattr(bl, old), old
    print('✓ status：五態（OWNED 保命 / QUALIFIED 四維 / PENDING 有連結未驗 / CANDIDATE 無連結 / REJECTED）')


def test_solution_dim_four():
    """解答本維④：QUALIFIED 需 sol_alignment.aligned；母書 N/A。"""
    found = {'status': 'found', 'id': '1', 'hash': 'a', 'by': 'agent'}
    # 解答本三維過但維④未對齊 → PENDING
    assert bl.status_of('m_sol', set(), {'m_sol': found},
                        _ed(eligible=True, version_ok=True, sol_aligned=None)) == bl.PENDING
    # 維④對齊 → QUALIFIED
    assert bl.status_of('m_sol', set(), {'m_sol': found},
                        _ed(eligible=True, version_ok=True, sol_aligned=True)) == bl.QUALIFIED
    # 維④判不對齊 → PENDING（仍未合格、不下載）
    assert bl.status_of('m_sol', set(), {'m_sol': found},
                        _ed(eligible=True, version_ok=True, sol_aligned=False)) == bl.PENDING
    print('✓ 解答本維④：對齊才 QUALIFIED、未對齊/未驗 PENDING、母書 N/A')


def test_public_status_folding():
    assert bl._public_status(bl.OWNED) == 'owned'
    assert bl._public_status(bl.QUALIFIED) == 'ready'      # 有連結 → reader 待收錄
    assert bl._public_status(bl.PENDING) == 'ready'        # 有連結（未驗）→ 仍待收錄（reader 不分）
    assert bl._public_status(bl.CANDIDATE) == 'unresolved'
    assert bl._public_status(bl.REJECTED) == bl.PUBLIC_ABSENT
    print('✓ _public_status：五態摺疊回舊公開字串（QUALIFIED/PENDING→ready、reader 零改）')


def test_select_next_only_qualified():
    """select_next 只取 QUALIFIED（合格書）；PENDING（有連結未驗）不下載。"""
    files = _fixture()
    found = lambda i, h: {'status': 'found', 'id': i, 'hash': h, 'by': 'agent', 'title': f'T{i}'}
    resolution = {'jackson': found('20', 'j'), 'rudin': found('10', 'r')}
    # jackson 合格、rudin 只有連結未驗 → 只下載 jackson
    all_eds = {'jackson': _ed(True, True), 'rudin': _ed(True, None)}
    picks = bl.select_next(5, files, set(), resolution, all_eds=all_eds)
    assert [p['slug'] for p in picks] == ['jackson'], picks
    assert picks[0]['id'] == '20' and picks[0]['hash'] == 'j'
    # rudin 也合格 → 按書單序 jackson 先
    all_eds['rudin'] = _ed(True, True)
    assert [p['slug'] for p in bl.select_next(5, files, set(), resolution, all_eds=all_eds)] == ['jackson', 'rudin']
    assert bl.select_next(0, files, set(), resolution, all_eds=all_eds) == []
    print('✓ select_next：只取 QUALIFIED（PENDING 不下載）、按書單序、限量 n、帶 id/hash/title')


def test_select_next_owned_exclude_malformed():
    files = _fixture()
    found = lambda i, h: {'status': 'found', 'id': i, 'hash': h, 'by': 'agent'}
    resolution = {'jackson': found('1', 'a'), 'rudin': found('2', 'b')}
    all_eds = {'jackson': _ed(True, True), 'rudin': _ed(True, True)}
    # owned 不選 + exclude 不選
    assert bl.select_next(5, files, {'jackson'}, resolution, exclude={'rudin'}, all_eds=all_eds) == []
    assert [p['slug'] for p in bl.select_next(5, files, {'jackson'}, resolution, all_eds=all_eds)] == ['rudin']
    # 畸形 id/hash（非純量）跳過
    resolution['rudin'] = {'status': 'found', 'id': ['2'], 'hash': 'b', 'by': 'agent'}
    assert bl.select_next(5, files, {'jackson'}, resolution, all_eds=all_eds) == []
    print('✓ select_next：owned/exclude 不選、畸形 id/hash 跳過（防 fetch URL 污染）')


def test_pending_and_candidate_targets():
    files = _fixture()  # jackson, jackson_sol, griffiths_em, rudin, rudin_sol
    found = {'status': 'found', 'id': '1', 'hash': 'a', 'by': 'agent'}
    resolution = {'jackson': found, 'rudin': {'status': 'not_found'}}
    all_eds = {'jackson': _ed(True, None)}   # jackson 有連結未驗 → PENDING
    pend = [t['slug'] for t in bl.pending_targets(files, set(), resolution, all_eds=all_eds)]
    cand = [t['slug'] for t in bl.unresolved_targets(files, set(), resolution, all_eds=all_eds)]
    assert pend == ['jackson'], pend                          # 有連結未驗 = /restock 回查母體
    assert 'rudin' not in cand and 'rudin' not in pend        # not_found → REJECTED，不在工作母體
    assert set(cand) == {'jackson_sol', 'griffiths_em', 'rudin_sol'}, cand  # 無連結 = CANDIDATE
    print('✓ pending_targets（有連結未驗）/ unresolved_targets（無連結）分流、REJECTED 不入母體')


def test_pool_counts_keys():
    files = _fixture()
    found = {'status': 'found', 'id': '1', 'hash': 'a', 'by': 'agent'}
    resolution = {'jackson': found, 'griffiths_em': {'status': 'not_found'}, 'rudin': found}
    all_eds = {'jackson': _ed(True, True), 'rudin': _ed(True, None)}  # jackson 合格、rudin PENDING
    pc = bl.pool_counts(files, set(), resolution, all_eds=all_eds)
    assert pc['confirmed'] == 1 and pc['ready'] == 1 and pc['qualified_ready'] == 1  # 只 jackson
    assert pc['pending'] == 1                                  # rudin
    assert pc['not_found'] == 1 and pc['absent'] == 1 and pc['rejected'] == 1  # griffiths_em
    assert pc['unresolved'] == pc['candidate']                # 向後相容鍵
    assert pc['review'] == 0 and pc['version_unavailable'] == 0  # 廢態留 0 鍵
    print('✓ pool_counts：confirmed/ready=QUALIFIED、pending、unresolved=candidate、廢態 0 鍵向後相容')


def test_progress_tally():
    files = _fixture()
    have = {'jackson'}
    found = {'status': 'found', 'id': '1', 'hash': 'a', 'by': 'agent'}
    resolution = {'rudin': found, 'griffiths_em': {'status': 'not_found'}}
    all_eds = {'rudin': _ed(True, True)}
    pr = bl.progress(files, have, resolution, all_eds=all_eds)
    o = pr['overall']
    # jackson(OWNED) jackson_sol(CANDIDATE) griffiths_em(REJECTED) rudin(QUALIFIED) rudin_sol(CANDIDATE)
    assert o['total'] == 5 and o['main'] == 3
    assert o[bl.OWNED] == 1 and o[bl.QUALIFIED] == 1 and o[bl.REJECTED] == 1 and o[bl.CANDIDATE] == 2
    assert o['ready'] == 1 and o['not_found'] == 1 and o['absent'] == 1  # 向後相容鍵
    assert pr['by_field']['physics'][bl.OWNED] == 1
    print('✓ progress：五態統計 + 向後相容公開鍵')


def test_catalog_shape_and_public_status():
    """catalog 結構（field→sublist→主書 status + sol_status）+ 公開狀態摺疊。"""
    files = _fixture()
    have = {'jackson'}
    found = {'status': 'found', 'id': '1', 'hash': 'a', 'by': 'agent'}
    resolution = {'rudin': found, 'jackson_sol': {'status': 'not_found'}}
    all_eds = {'rudin': _ed(True, None)}   # rudin 有連結未驗 → PENDING → public 'ready'
    cat = bl.catalog(files, have, resolution, all_eds=all_eds)
    assert set(cat) == {'fields', 'overall'}
    by_slug = {b['slug']: b for f in cat['fields'] for sl in f['sublists'] for b in sl['books']}
    assert by_slug['jackson']['status'] == 'owned'             # owned
    assert by_slug['jackson']['sol_status'] == 'absent'        # jackson_sol not_found → absent
    assert by_slug['rudin']['status'] == 'ready'              # PENDING → public ready
    assert by_slug['griffiths_em']['status'] == 'unresolved'  # 無連結 → candidate → unresolved
    assert 'sol_status' not in by_slug['griffiths_em']        # solution=false → 無 sol_status
    print('✓ catalog：結構正確 + 五態摺疊回公開三態（reader 零改）')


def test_validate_catches_problems():
    bad = [
        _file('x', 1, [_sl('s', [_b('Good'), _b('bad slug'), _b('dup')])]),
        _file('y', 2, [_sl('s', [_b('dup'), _b('main_sol')])]),
    ]
    errs = bl.validate(bad)
    assert any('Good' in e for e in errs) and any('bad slug' in e for e in errs)
    assert any('重複' in e and 'dup' in e for e in errs)
    assert any('main_sol' in e and '_sol' in e for e in errs)
    assert bl.validate(_fixture()) == []
    print('✓ validate：大寫/空格非法 slug、dup、_sol 結尾主書全抓；乾淨書單通過')


def test_reconcile_owned():
    files = _fixture()
    have = {'jackson', 'rudin', 'feynman_em2', 'jackson_sol', 'griffiths_em_sol'}
    r = bl.reconcile_owned(files, have)
    assert r['inventory_not_in_sot'] == ['feynman_em2']
    assert r['owned_sol_not_in_sot'] == ['griffiths_em_sol']
    assert 'griffiths_em' in r['in_sot_not_inventory']
    print('✓ reconcile：抓漏列主書 + owned 題本未被 SoT 涵蓋')


def test_load_files_orders_by_order():
    d = tempfile.mkdtemp(prefix='bl_')
    json.dump(_file('zzz', 5, [_sl('s', [_b('a')])]), open(os.path.join(d, 'zzz.json'), 'w'))
    json.dump(_file('aaa', 99, [_sl('s', [_b('b')])]), open(os.path.join(d, 'aaa.json'), 'w'))
    json.dump({'not': 'a booklist'}, open(os.path.join(d, 'junk.json'), 'w'))
    files = bl.load_files(d)
    assert [f['field_id'] for f in files] == ['zzz', 'aaa'], files
    print('✓ load_files：按 order 排序（非檔名）、非書單檔容錯跳過')


if __name__ == '__main__':
    for fn in [test_targets_solution_expansion_and_order, test_status_five_states,
               test_solution_dim_four, test_public_status_folding, test_select_next_only_qualified,
               test_select_next_owned_exclude_malformed, test_pending_and_candidate_targets,
               test_pool_counts_keys, test_progress_tally, test_catalog_shape_and_public_status,
               test_validate_catches_problems, test_reconcile_owned, test_load_files_orders_by_order]:
        fn()
    print('\n全部通過 ✅')
