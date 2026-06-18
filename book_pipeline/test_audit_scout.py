"""audit_scout 確定性 heuristic 單元測試：uv run python -m book_pipeline.test_audit_scout

鎖住三個易回歸的核心 heuristic：filter_types 規則、heading_text_level 排除 None 投票、
章節候選收斂（section 排除 + TOC 偏好 + 單調首現）。純合成 block，不碰磁碟。"""

from book_pipeline import audit_scout as s


def _t(text, level=None, typ='text', page=0):
    return {'type': typ, 'text': text, 'text_level': level, 'page_idx': page}


def test_filter_types():
    blocks = ([_t('h', typ='header')] * 3 + [_t('1', typ='page_number')] * 2
              + [_t('r', typ='ref_text')] * 6 + [_t('body')])
    tc, ft = s._filter_types(blocks)
    assert 'header' in ft and 'page_number' in ft
    assert 'ref_text' in ft                      # count 6 > 5 → 進
    blocks2 = blocks[:5] + [_t('r', typ='ref_text')] * 3  # ref_text count 3
    _, ft2 = s._filter_types(blocks2)
    assert 'ref_text' not in ft2                  # ≤5 → 不進
    print('✓ filter_types：常規雜訊進、ref_text 僅 count>5 進')


def test_heading_lvl_excludes_none():
    # section heading 在 lvl2（leveled）；body 段落也以 'N.M' 開頭但 text_level=None（不可灌票）
    blocks = ([_t('2.1 Foo', level=2)] * 5 + [_t('2.2 Bar', level=2)] * 5
              + [_t('1.3 body prose starts numbered', level=None)] * 40)
    lvl, dist = s._heading_lvl(blocks)
    assert lvl == 2, (lvl, dist)                  # None 不灌票 → 取 lvl2
    assert None not in dist
    print('✓ heading_text_level：排除 None 投票（lvl2 書不被 body None 灌成 1）')


def test_chapter_convergence():
    # lvl1 書：章標與 section 同層；含 TOC（尾頁碼）+ section(N.M) 干擾 + 正文裸章號
    H = [
        (10, 5, 'Chapter 1 Intro 3'),     # TOC（尾頁碼）
        (11, 5, 'Chapter 2 Next 29'),     # TOC
        (50, 28, '1'),                    # 正文章 1（裸號，非 TOC）
        (60, 30, '1.1 Section'),          # section → 須排除
        (90, 54, '2'),                    # 正文章 2
        (95, 55, '2.3 Another section'),  # section
        (130, 80, '3 Methods'),           # 正文章 3
        (200, 120, 'Index'),              # 非章
    ]
    picks, raw = s._chapter_candidates(H)
    nums = [c['num'] for c in picks]
    assert nums == [1, 2, 3], picks               # 單調 1,2,3、無 section、無跳號
    assert picks[0]['idx'] == 50 and picks[1]['idx'] == 90  # 偏好非 TOC 的正文出現
    assert picks[2]['text'].startswith('3 Methods')
    print('✓ chapter：排除 N.M section + 偏好非 TOC 正文出現 + 單調首現收斂')


if __name__ == '__main__':
    test_filter_types()
    test_heading_lvl_excludes_none()
    test_chapter_convergence()
    print('\n全部通過 ✅')
