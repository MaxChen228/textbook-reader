"""書單 SoT 純邏輯單元測試：uv run python -m book_pipeline.test_booklists

涵蓋：targets 攤平與解答本衍生/排序、五態衍生（含 review 不回工作母體/不算 confirmed）、
select_next 確定性選書（含 exclude/畸形 sidecar 守衛）、progress 統計、validate、reconcile_owned。
全部用合成 fixture + 注入 have/resolution → 不碰磁碟、純函式可重現。"""

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
    if solution is not True:          # 預設 true → 省略；只在 false 顯式
        d['solution'] = solution
    return d


def _fixture():
    # 兩領域：physics(order10)、math(order20)。含 solution=false 一本（jackson）測「不衍生解答本」。
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
    # jackson 衍生 jackson_sol（緊鄰）；griffiths_em solution=false → 無 _sol；跨領域按 order。
    assert slugs == ['jackson', 'jackson_sol', 'griffiths_em', 'rudin', 'rudin_sol'], slugs
    sol = next(t for t in ts if t['slug'] == 'jackson_sol')
    assert sol['kind'] == 'solution' and sol['of'] == 'jackson'
    assert next(t for t in ts if t['slug'] == 'jackson')['kind'] == 'main'
    print('✓ targets：solution!=false 衍生 <slug>_sol 緊鄰主書、false 不衍生、跨領域按 order')


def test_status_five_states():
    have = {'jackson'}
    resolution = {'jackson_sol': {'id': '1', 'hash': 'a'},      # ready
                  'rudin': {'id': '2', 'hash': 'b'},            # ready
                  'griffiths_em': {'absent': True},             # absent
                  'rudin_sol': {'review': True}}                # review（待架構師裁決）
    assert bl.status_of('jackson', have, resolution) == bl.OWNED
    assert bl.status_of('rudin', have, resolution) == bl.READY
    assert bl.status_of('jackson_sol', have, resolution) == bl.READY
    assert bl.status_of('griffiths_em', have, resolution) == bl.ABSENT
    assert bl.status_of('rudin_sol', have, resolution) == bl.REVIEW   # ≠ UNRESOLVED
    assert bl.REVIEW != bl.UNRESOLVED and bl.REVIEW in bl.STATUSES
    assert not hasattr(bl, 'QUEUED')                            # queued 態已廢（無 runtime buffer）
    # 真未解析（resolution 無此筆）才 UNRESOLVED
    assert bl.status_of('rudin_sol', have, {}) == bl.UNRESOLVED
    # owned 優先於 resolution（即使有解析，已 owned 就 owned）
    assert bl.status_of('jackson', have, {'jackson': {'id': '9', 'hash': 'z'}}) == bl.OWNED
    print('✓ status：owned>ready/absent/review/unresolved 五態 + owned 優先 + review≠unresolved + 無 queued')


def test_review_excluded_from_worklist_and_pool():
    """review 的核心契約：不回 crawl agent 工作母體（unresolved_targets）、不算 confirmed 解析池水位
    （pool_counts），自成一桶。沒這條，review 書每 cycle 被重派、燒 token、池永補不滿 → daemon 不收斂。"""
    files = _fixture()  # targets: jackson, jackson_sol, griffiths_em, rudin, rudin_sol
    have = set()
    resolution = {'jackson': {'review': True},                 # 主書 review
                  'jackson_sol': {'id': '1', 'hash': 'a'},      # ready（confirmed）
                  'rudin': {'absent': True}}                    # absent
    # rudin_sol、griffiths_em 無 resolution → 真 unresolved
    worklist = [t['slug'] for t in bl.unresolved_targets(files, have, resolution)]
    assert 'jackson' not in worklist, worklist                 # review 不回工作母體（殺重派迴圈）
    assert set(worklist) == {'griffiths_em', 'rudin_sol'}, worklist
    pc = bl.pool_counts(files, have, resolution)
    assert pc['review'] == 1 and pc['confirmed'] == 1, pc      # review 自成一桶、confirmed = ready
    assert pc['unresolved'] == 2, pc
    print('✓ review：排除於 unresolved_targets（不重派）+ pool_counts 自成桶（不算 confirmed）→ 保收斂')


def test_select_next_deterministic_ready_only_in_order():
    files = _fixture()
    have = set()
    resolution = {
        'rudin': {'id': '10', 'hash': 'r', 'title': 'Baby Rudin'},
        'jackson': {'id': '20', 'hash': 'j'},
        'jackson_sol': {'absent': True},            # absent → 不選
    }
    picks = bl.select_next(5, files, have, resolution)
    # 只有 ready 的 jackson、rudin；按書單序 jackson(physics) 先；帶 resolution 的 id/hash/title。
    assert [p['slug'] for p in picks] == ['jackson', 'rudin'], picks
    assert picks[0]['id'] == '20' and picks[0]['hash'] == 'j'
    assert picks[1]['title'] == 'Baby Rudin'        # resolution.title 覆蓋 SoT title
    assert bl.select_next(1, files, have, resolution)[0]['slug'] == 'jackson'  # 限量取前 n
    assert bl.select_next(0, files, have, resolution) == []
    print('✓ select_next：只取 ready、按書單序、限量 n、帶 resolution 的 id/hash/title（零 LLM）')


def test_select_next_skips_owned_and_excludes():
    files = _fixture()
    resolution = {'jackson': {'id': '1', 'hash': 'a'}, 'rudin': {'id': '2', 'hash': 'b'}}
    # jackson owned → 不選；rudin 在 exclude（如下載失敗達上限）→ 不選
    picks = bl.select_next(5, files, have={'jackson'}, resolution=resolution, exclude={'rudin'})
    assert picks == [], picks
    # 移除 exclude → rudin 回到候選
    picks2 = bl.select_next(5, files, have={'jackson'}, resolution=resolution)
    assert [p['slug'] for p in picks2] == ['rudin'], picks2
    print('✓ select_next：owned 不重選 + exclude（失敗達上限）排除、解除後回候選')


def test_select_next_skips_malformed_resolution():
    """sidecar 畸形守衛：id/hash 非純量（list/dict）→ 跳過不入候選（不被 str() 成 "[\'123\']" 污染 fetch URL）。"""
    files = _fixture()
    resolution = {'jackson': {'id': ['1'], 'hash': 'a'},        # id 是 list → 畸形
                  'rudin': {'id': '2', 'hash': {'x': 1}}}       # hash 是 dict → 畸形
    assert bl.select_next(5, files, have=set(), resolution=resolution) == []
    # 修正成純量 → 正常收
    resolution['jackson'] = {'id': '1', 'hash': 'a'}
    assert [p['slug'] for p in bl.select_next(5, files, have=set(), resolution=resolution)] == ['jackson']
    print('✓ select_next：畸形 id/hash（非純量）跳過、純量照收（防 fetch URL 污染靜默丟書）')


def test_progress_tally():
    files = _fixture()
    have = {'jackson'}
    resolution = {'rudin': {'id': '1', 'hash': 'a'}, 'griffiths_em': {'absent': True}}
    pr = bl.progress(files, have, resolution=resolution)
    o = pr['overall']
    # targets: jackson(owned) jackson_sol(unresolved) griffiths_em(absent) rudin(ready) rudin_sol(unresolved)
    assert o['total'] == 5 and o['main'] == 3
    assert o[bl.OWNED] == 1 and o[bl.READY] == 1 and o[bl.ABSENT] == 1 and o[bl.UNRESOLVED] == 2
    assert pr['by_field']['physics'][bl.OWNED] == 1
    print('✓ progress：整體 + 各領域五態統計正確')


def test_validate_catches_problems():
    bad = [
        _file('x', 1, [_sl('s', [_b('Good'), _b('bad slug'), _b('dup')])]),
        _file('y', 2, [_sl('s', [_b('dup'), _b('main_sol')])]),  # dup 重複 + _sol 結尾主書
    ]
    # Good 含大寫 → 非法（slug 須小寫）
    errs = bl.validate(bad)
    joined = ' | '.join(errs)
    assert any('Good' in e for e in errs), errs           # 大寫非法
    assert any('bad slug' in e for e in errs), errs        # 空格非法
    assert any('重複' in e and 'dup' in e for e in errs), errs
    assert any('main_sol' in e and '_sol' in e for e in errs), errs
    assert bl.validate(_fixture()) == []                   # 乾淨 fixture 通過
    print('✓ validate：大寫/空格非法 slug、dup、_sol 結尾主書全抓；乾淨書單通過')


def test_reconcile_owned():
    files = _fixture()  # main: jackson(sol=true→jackson_sol target), griffiths_em(sol=false→無), rudin
    # feynman_em2 主書漏列；jackson_sol 是合法解答本目標（不算漏）；
    # griffiths_em_sol owned 但主書 solution=false → 該主書應改 true（owned_sol_not_in_sot 抓它）
    have = {'jackson', 'rudin', 'feynman_em2', 'jackson_sol', 'griffiths_em_sol'}
    r = bl.reconcile_owned(files, have)
    assert r['inventory_not_in_sot'] == ['feynman_em2'], r       # 漏列的 owned 主書
    assert r['owned_sol_not_in_sot'] == ['griffiths_em_sol'], r  # owned 題本但主書 solution=false
    assert 'griffiths_em' in r['in_sot_not_inventory']            # SoT 有、未收錄（正常缺口）
    print('✓ reconcile：抓漏列主書 + owned 題本未被 SoT 涵蓋（主書誤標 solution=false）')


def test_load_files_orders_by_order():
    d = tempfile.mkdtemp(prefix='bl_')
    json.dump(_file('zzz', 5, [_sl('s', [_b('a')])]), open(os.path.join(d, 'zzz.json'), 'w'))
    json.dump(_file('aaa', 99, [_sl('s', [_b('b')])]), open(os.path.join(d, 'aaa.json'), 'w'))
    json.dump({'not': 'a booklist'}, open(os.path.join(d, 'junk.json'), 'w'))  # 非書單檔跳過
    files = bl.load_files(d)
    assert [f['field_id'] for f in files] == ['zzz', 'aaa'], files  # order 5 先於 99，非檔名序
    print('✓ load_files：按 order 排序（非檔名）、非書單檔容錯跳過')


if __name__ == '__main__':
    test_targets_solution_expansion_and_order()
    test_status_five_states()
    test_review_excluded_from_worklist_and_pool()
    test_select_next_deterministic_ready_only_in_order()
    test_select_next_skips_owned_and_excludes()
    test_select_next_skips_malformed_resolution()
    test_progress_tally()
    test_validate_catches_problems()
    test_reconcile_owned()
    test_load_files_orders_by_order()
    print('\n全部通過 ✅')
