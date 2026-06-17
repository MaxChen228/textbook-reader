"""resolver 純邏輯單元測試：uv run python -m book_pipeline.test_resolve

涵蓋 confidence 標題/作者匹配、pick（類型過濾+取最高信心）、resolve_target（resolved/absent/review
三路）。用合成書 + FakeClient（不打網路）。下載/額度不在本模組職責內，不測。"""

from book_pipeline import resolve as rs


def _book(title, author, bid='1', bhash='h', mb=10.0, ext='pdf'):
    return {'id': bid, 'hash': bhash, 'title': title, 'author': author,
            'extension': ext, 'filesize': int(mb * 1e6), 'year': 2010,
            'publisher': 'X', 'pages': 600}


class FakeClient:
    def __init__(self, books):
        self._books = books

    def search(self, q, **kw):
        return self._books


def _t(slug, title, author, kind='main'):
    t = {'slug': slug, 'title': title, 'author': author, 'kind': kind, 'field_id': 'physics'}
    if kind == 'solution':
        t['of'] = slug[:-4]
    return t


def test_confidence():
    jackson = _book('Classical Electrodynamics', 'John David Jackson')
    assert rs.confidence('Classical Electrodynamics', 'Jackson', jackson) == 1.0
    # stopword 不灌水：'Introduction to' 被濾掉，仍精準
    assert rs.confidence('Introduction to Quantum Mechanics', 'Griffiths',
                         _book('Quantum Mechanics', 'D. J. Griffiths')) == 1.0
    # 完全不相干 → 0
    assert rs.confidence('Classical Electrodynamics', 'Jackson',
                         _book('Organic Chemistry', 'Clayden')) == 0.0
    # 標題中但作者錯 → 只拿標題分（0.6）
    assert rs.confidence('Classical Electrodynamics', 'Jackson',
                         _book('Classical Electrodynamics', 'Nobody')) == 0.6
    print('✓ confidence：標題詞重疊×0.6 + 作者命中×0.4、stopword 不灌水')


def test_author_word_boundary():
    # 'Hall' 不該子字串假命中 'Marshall'（詞界比對）→ 作者 0 分、僅標題分
    b = _book('Principles of Neural Science', 'Eric Marshall')  # 非 Hall
    assert rs.confidence('Principles of Neural Science', 'Hall', b) == 0.6
    # 真正同姓（詞界命中）→ 滿分
    assert rs.confidence('Quantum Mechanics', 'Hall',
                         _book('Quantum Mechanics', 'Brian C. Hall')) == 1.0
    print('✓ confidence：作者詞界比對（Hall 不命中 Marshall、真同姓才算）')


def test_query_surname():
    assert rs.query_surname('John David Jackson') == 'jackson'      # 取最後 token（姓）
    assert rs.query_surname('Goldstein, Poole & Safko') == 'goldstein'  # 第一作者
    assert rs.query_surname('Sakurai & Napolitano') == 'sakurai'
    assert rs.query_surname('') == ''
    print('✓ query_surname：取第一作者的姓（最後 name token）')


def test_pick_type_filter():
    target_main = _t('jackson_electrodynamics', 'Classical Electrodynamics', 'Jackson')
    books = [
        _book('Classical Electrodynamics Solutions Manual', 'Jackson', bid='SOL'),  # is_solution
        _book('Classical Electrodynamics', 'J. D. Jackson', bid='MAIN'),
    ]
    best, conf = rs.pick(target_main, books)
    assert best['id'] == 'MAIN', best        # 主書 target 不選解答本
    assert conf == 1.0

    target_sol = _t('jackson_electrodynamics_sol',
                    'Classical Electrodynamics — Solutions', 'Jackson', kind='solution')
    best, conf = rs.pick(target_sol, books)
    assert best['id'] == 'SOL', best         # 解答本 target 只選解答本
    print('✓ pick：主書 target 排除解答本、解答本 target 只收解答本、取最高信心')


def test_pick_skips_non_pdf():
    target = _t('rudin_analysis', 'Principles of Mathematical Analysis', 'Rudin')
    books = [_book('Principles of Mathematical Analysis', 'Rudin', ext='epub'),
             _book('Principles of Mathematical Analysis', 'Walter Rudin', bid='PDF', ext='pdf')]
    best, _ = rs.pick(target, books)
    assert best['id'] == 'PDF', best
    print('✓ pick：跳過非 pdf')


def test_resolve_target_resolved():
    t = _t('jackson_electrodynamics', 'Classical Electrodynamics', 'Jackson')
    cl = FakeClient([_book('Classical Electrodynamics', 'J. D. Jackson', bid='42', bhash='ab')])
    action, entry = rs.resolve_target(cl, t)
    assert action == 'resolved' and entry['id'] == '42' and entry['hash'] == 'ab', (action, entry)
    assert entry['conf'] >= rs.MAIN_THRESHOLD and 'at' in entry
    print('✓ resolve_target：信心足 → resolved（帶 id/hash/conf/at）')


def test_resolve_target_solution_absent():
    t = _t('peskin_qft_sol', 'An Introduction to QFT — Solutions', 'Peskin', kind='solution')
    cl = FakeClient([_book('Quantum Field Theory in a Nutshell', 'Zee')])  # 非該書解答、且非 solution
    action, entry = rs.resolve_target(cl, t)
    assert action == 'absent' and entry['absent'] is True, (action, entry)
    print('✓ resolve_target：解答本查無 → absent（永不再查）')


def test_resolve_target_solution_needs_author():
    # 解答本標題全中但作者不符（generic sol 標題的典型跨書假陽性）→ conf 0.6 < SOL_THRESHOLD → absent。
    # 擋的就是 Dummit↔Gallian、Shankar↔Griffiths 這種「同類書解答本標題撞詞」誤配。
    t = _t('taylor_mechanics_sol', 'Classical Mechanics — Solutions', 'Taylor', kind='solution')
    cl = FakeClient([_book('Classical Mechanics Solutions Manual', 'Goldstein', bid='WRONG')])
    action, entry = rs.resolve_target(cl, t)
    assert action == 'absent', (action, entry)          # 作者無佐證的解答本不採（0.6 < 0.65）
    # 對照：同搜尋結果但作者佐證（Taylor 命中）→ conf ≥0.7 → resolved
    cl2 = FakeClient([_book('Classical Mechanics Solutions Manual', 'John R. Taylor', bid='OK', bhash='z')])
    action2, entry2 = rs.resolve_target(cl2, t)
    assert action2 == 'resolved' and entry2['id'] == 'OK', (action2, entry2)
    print('✓ resolve_target：解答本須作者佐證（標題全中作者不符→absent，作者命中→resolved）')


def test_resolve_target_main_review():
    t = _t('mtw_gravitation', 'Gravitation', 'Misner, Thorne & Wheeler')
    cl = FakeClient([_book('Organic Chemistry', 'Clayden'),
                     _book('Linear Algebra', 'Strang')])  # 都不匹配
    action, entry = rs.resolve_target(cl, t)
    assert action == 'review' and entry['review'] is True, (action, entry)
    assert 'candidates' in entry
    print('✓ resolve_target：主書信心不足 → review（不自動重試、列候選待裁決）')


if __name__ == '__main__':
    test_confidence()
    test_author_word_boundary()
    test_query_surname()
    test_pick_type_filter()
    test_pick_skips_non_pdf()
    test_resolve_target_resolved()
    test_resolve_target_solution_absent()
    test_resolve_target_solution_needs_author()
    test_resolve_target_main_review()
    print('\n全部通過 ✅')
