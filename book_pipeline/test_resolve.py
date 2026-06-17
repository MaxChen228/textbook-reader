"""resolve harness 純邏輯單元測試：uv run python -m book_pipeline.test_resolve

涵蓋 advisory 原語（confidence 標題/作者匹配、query_surname、pick 類型過濾）、確定性快速路徑
（exact_match 只採零歧義主書、_norm_title）、commit 的 ghost 守門。用合成書，不打網路。
search/inspect/commit-resolved 走真網路與 booklists，靠 agent dogfood 驗，不在本單元測試。"""

import argparse

from book_pipeline import resolve as rs


def _book(title, author, bid='1', bhash='h', mb=10.0, ext='pdf'):
    return {'id': bid, 'hash': bhash, 'title': title, 'author': author,
            'extension': ext, 'filesize': int(mb * 1e6), 'year': 2010,
            'publisher': 'X', 'pages': 600}


def _t(slug, title, author, kind='main'):
    t = {'slug': slug, 'title': title, 'author': author, 'kind': kind, 'field_id': 'physics'}
    if kind == 'solution':
        t['of'] = slug[:-4]
    return t


# ── advisory 原語（信心分只當素材，不裁決）─────────────────────────────────
def test_confidence():
    jackson = _book('Classical Electrodynamics', 'John David Jackson')
    assert rs.confidence('Classical Electrodynamics', 'Jackson', jackson) == 1.0
    assert rs.confidence('Introduction to Quantum Mechanics', 'Griffiths',
                         _book('Quantum Mechanics', 'D. J. Griffiths')) == 1.0
    assert rs.confidence('Classical Electrodynamics', 'Jackson',
                         _book('Organic Chemistry', 'Clayden')) == 0.0
    # 標題中但作者錯 → 只拿標題分（0.6）→ advisory 上「可疑」帶
    assert rs.confidence('Classical Electrodynamics', 'Jackson',
                         _book('Classical Electrodynamics', 'Nobody')) == 0.6
    print('✓ confidence：標題詞重疊×0.6 + 作者命中×0.4（advisory）')


def test_author_word_boundary():
    b = _book('Principles of Neural Science', 'Eric Marshall')
    assert rs.confidence('Principles of Neural Science', 'Hall', b) == 0.6
    assert rs.confidence('Quantum Mechanics', 'Hall',
                         _book('Quantum Mechanics', 'Brian C. Hall')) == 1.0
    print('✓ confidence：作者詞界比對（Hall 不命中 Marshall）')


def test_query_surname():
    assert rs.query_surname('John David Jackson') == 'jackson'
    assert rs.query_surname('Goldstein, Poole & Safko') == 'goldstein'
    assert rs.query_surname('Sakurai & Napolitano') == 'sakurai'
    assert rs.query_surname('') == ''
    print('✓ query_surname：取第一作者的姓')


def test_pick_type_filter():
    target_main = _t('jackson_electrodynamics', 'Classical Electrodynamics', 'Jackson')
    books = [_book('Classical Electrodynamics Solutions Manual', 'Jackson', bid='SOL'),
             _book('Classical Electrodynamics', 'J. D. Jackson', bid='MAIN')]
    best, conf = rs.pick(target_main, books)
    assert best['id'] == 'MAIN' and conf == 1.0, best
    target_sol = _t('jackson_electrodynamics_sol',
                    'Classical Electrodynamics — Solutions', 'Jackson', kind='solution')
    best, _ = rs.pick(target_sol, books)
    assert best['id'] == 'SOL', best
    print('✓ pick：主書排除解答本、解答本只收解答本')


def test_pick_skips_non_pdf():
    target = _t('rudin_analysis', 'Principles of Mathematical Analysis', 'Rudin')
    books = [_book('Principles of Mathematical Analysis', 'Rudin', ext='epub'),
             _book('Principles of Mathematical Analysis', 'Walter Rudin', bid='PDF', ext='pdf')]
    best, _ = rs.pick(target, books)
    assert best['id'] == 'PDF', best
    print('✓ pick：跳過非 pdf')


# ── 確定性快速路徑：嚴格、零歧義才採，其餘交 agent ─────────────────────────
def test_norm_title():
    assert rs._norm_title('Calculus: Early Transcendentals') == 'calculus'
    assert rs._norm_title('Linear Algebra and Its Applications, 4th') == 'linear algebra and its applications'
    assert rs._norm_title('Fundamentals of Physics. Extended') == 'fundamentals of physics'
    print('✓ _norm_title：切副標題、留 alnum、收斂')


def test_exact_match_main():
    t = _t('lay_linear_algebra', 'Linear Algebra and Its Applications', 'Lay')
    books = [_book('Linear Algebra and Its Applications, 4th Edition', 'David C. Lay', bid='OK')]
    m = rs.exact_match(t, books)
    assert m and m['id'] == 'OK', m
    print('✓ exact_match：多詞 canonical 標題相等+作者命中 → 採用')


def test_exact_match_rejects_short_title():
    # 單詞 canonical 天生歧義 → 永不自動採（殺 chang 'Chemistry'→《Food Chemistry》假陽性）
    t = _t('chang_general_chemistry', 'Chemistry', 'Chang')
    assert rs.exact_match(t, [_book('Principles of Food Chemistry', 'John deMan', bid='WRONG')]) is None
    assert rs.exact_match(t, [_book('Chemistry', 'Raymond Chang', bid='C')]) is None  # 連真同名也交 agent
    print('✓ exact_match：單詞題名一律不自動採（交 agent）')


def test_exact_match_rejects_solution():
    t = _t('foo_sol', 'Foo Bar Baz — Solutions', 'Smith', kind='solution')
    assert rs.exact_match(t, [_book('Foo Bar Baz Solutions Manual', 'Smith', bid='S')]) is None
    print('✓ exact_match：解答本一律交 agent（跨書假陽性高發區）')


def test_exact_match_requires_author():
    t = _t('artin_algebra', 'Algebra Second', 'Artin')
    assert rs.exact_match(t, [_book('Algebra Second', 'Nobody Else', bid='X')]) is None
    print('✓ exact_match：標題相等但作者不符 → 不採')


def test_exact_match_rejects_ambiguous_dup():
    t = _t('foo', 'Linear Systems Theory', 'Smith')
    books = [_book('Linear Systems Theory', 'A Smith', bid='1'),
             _book('Linear Systems Theory', 'B Smith', bid='2')]
    assert rs.exact_match(t, books) is None
    print('✓ exact_match：同名多筆歧義 → 交 agent')


# ── commit 守門 ────────────────────────────────────────────────────────────
def test_commit_rejects_ghost():
    args = argparse.Namespace(slug='definitely_not_a_real_target_zzz', id=None, hash=None,
                              title=None, author=None, mb=None, absent=False, review=False, note=None)
    assert rs.cmd_commit(args) == 2          # 非書單 target → 拒絕落盤（杜絕 ghost）
    print('✓ cmd_commit：拒寫非書單 target')


if __name__ == '__main__':
    test_confidence()
    test_author_word_boundary()
    test_query_surname()
    test_pick_type_filter()
    test_pick_skips_non_pdf()
    test_norm_title()
    test_exact_match_main()
    test_exact_match_rejects_short_title()
    test_exact_match_rejects_solution()
    test_exact_match_requires_author()
    test_exact_match_rejects_ambiguous_dup()
    test_commit_rejects_ghost()
    print('\n全部通過 ✅')
