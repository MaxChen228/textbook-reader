"""proposals 護欄測試（Phase 3-A）：自動 supersede 已被 repair 涵蓋的假 catalog 缺口。

核心純函式 catalog_resolved_candidates 依注入解耦（slug_of / critical_fn），故免 parsed 資料即可測：
① _is_catalog_gap 只認 caption 類、不誤觸 conway/poole 章節題號缺口 ② critical>0 不動 ③ slug 無法歸戶跳過
④ _slug_resolver 截斷碰撞（carey a/b）回 None、正常書與 -N 後綴正確歸戶。
"""
from __future__ import annotations

from book_pipeline import proposals as P


def _rec(pid, status='proposed', domain='engine', type='tooling-gap', title='', evidence='', proposal=''):
    return {'id': pid, 'status': status, 'domain': domain, 'type': type,
            'title': title, 'evidence': evidence, 'proposal': proposal}


# ── ① _is_catalog_gap ─────────────────────────────────────────────────────────
def test_is_catalog_gap_matches_caption():
    assert P._is_catalog_gap(_rec('P-2026-06-19-x', title='catalog 無法把相鄰 text 圖說綁回 image'))
    assert P._is_catalog_gap(_rec('P-2026-06-19-x', evidence='empty_captions=77 多 image block 共用圖說'))


def test_is_catalog_gap_excludes_scope_guard_patch():
    """scope_guard patch 記錄（type=patch）標題常帶階段名 'catalog_audit'（含子字串 catalog）→
    不得被當 caption 缺口、不得被 supersede-resolved 蓋上錯誤的 repair-covered 理由。"""
    assert not P._is_catalog_gap(_rec(
        'P-2026-06-21-x', type='patch',
        title='worker 越界改核心碼：book_pipeline/parser.py（catalog_audit x）',
        evidence='scope_guard bracket ... parser.py modified'))


def test_is_catalog_gap_excludes_structural():
    """conway/poole 章節/題號切分缺口不含 caption 字 → 不誤觸（這是避免誤 supersede 真缺口的關鍵）。"""
    assert not P._is_catalog_gap(_rec('P-2026-06-19-conway',
                                      title='inline exercises 被提早切到下一節',
                                      evidence='context-aware numeric-list filtering'))


def test_is_catalog_gap_excludes_non_proposed_and_other_domain():
    assert not P._is_catalog_gap(_rec('P-x', status='superseded', title='caption 圖說'))
    assert not P._is_catalog_gap(_rec('P-x', domain='math', title='caption 圖說'))


# ── ② catalog_resolved_candidates ────────────────────────────────────────────
def test_candidates_only_resolved_caption_gaps():
    recs = [
        _rec('P-2026-06-19-a', title='catalog 圖說 綁不回'),          # caption, crit 0 → 選
        _rec('P-2026-06-19-b', title='catalog empty_caption shard'),  # caption, crit 5 → 不選
        _rec('P-2026-06-19-c', title='inline 習題切錯'),               # 非 caption → 不選
    ]
    slug_of = lambda r: {'P-2026-06-19-a': 'aa', 'P-2026-06-19-b': 'bb',
                         'P-2026-06-19-c': 'cc'}.get(r['id'])
    critical_fn = lambda s: {'aa': 0, 'bb': 5, 'cc': 0}[s]
    got = P.catalog_resolved_candidates(recs, slug_of, critical_fn)
    assert [r['id'] for r, _ in got] == ['P-2026-06-19-a']


def test_candidates_skips_unresolvable_slug():
    """slug_of 回 None（截斷碰撞無法歸戶）→ 跳過，絕不臆測 supersede。"""
    recs = [_rec('P-2026-06-19-a', title='catalog 圖說')]
    got = P.catalog_resolved_candidates(recs, lambda r: None, lambda s: 0)
    assert got == []


def test_candidates_skips_unknown_critical():
    """critical_fn 回 -1（無法判定，如 parsed 不存在）→ 不視為已解、不 supersede。"""
    recs = [_rec('P-2026-06-19-a', title='catalog 圖說')]
    got = P.catalog_resolved_candidates(recs, lambda r: 'aa', lambda s: -1)
    assert got == []


# ── ④ _slug_resolver ─────────────────────────────────────────────────────────
def test_slug_resolver_normal_and_suffixed():
    universe = ['artin_algebra', 'chaikin_lubensky_condensed_matter']
    slug_of = P._slug_resolver(universe)
    assert slug_of(_rec('P-2026-06-18-artin-algebra')) == 'artin_algebra'
    # -N 後綴 id 仍歸戶母書（chaikin 截斷成 ...matte）
    assert slug_of(_rec('P-2026-06-19-chaikin-lubensky-condensed-matte-5')) == 'chaikin_lubensky_condensed_matter'


def test_slug_resolver_returns_none_on_truncation_collision():
    """carey a/b 截斷後 key 相同（base 不同）→ 無法由 id 區分 → None（不歸戶、不誤 supersede）。"""
    universe = ['carey_sundberg_advanced_organic_a', 'carey_sundberg_advanced_organic_b']
    slug_of = P._slug_resolver(universe)
    # 兩者 slugify 同 key（[:32] 截掉尾字母）
    assert P._slugify('carey_sundberg_advanced_organic_a') == P._slugify('carey_sundberg_advanced_organic_b')
    assert slug_of(_rec('P-2026-06-19-' + P._slugify('carey_sundberg_advanced_organic_a'))) is None


def test_slug_resolver_none_when_no_match():
    slug_of = P._slug_resolver(['artin_algebra'])
    assert slug_of(_rec('P-2026-06-19-some-other-book')) is None


def test_slug_resolver_rejects_sol_suffix():
    """_sol 提案不可前綴誤歸母書：guardrail universe 已排除 _sol，且後綴只認 `-<digits>`（拒 `-sol`）
    → _sol 提案回 None、不會被當母書、不會拿母書 critical 誤 supersede。"""
    slug_of = P._slug_resolver(['lee_smooth_manifolds'])  # 比照 guardrail：universe 不含 _sol
    assert slug_of(_rec('P-2026-06-19-lee-smooth-manifolds-sol')) is None
    # 同書第 N 案的 -N 後綴仍正確歸戶（不被誤殺）
    assert slug_of(_rec('P-2026-06-19-lee-smooth-manifolds-2')) == 'lee_smooth_manifolds'


if __name__ == '__main__':
    import sys
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
