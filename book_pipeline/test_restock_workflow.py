"""/restock 四維落盤端到端測試：uv run python -m book_pipeline.test_restock_workflow

驅動 **真實落盤表面**（resolve.cmd_commit 寫維②連結 + editions.cmd_set 寫維①③④），再經
booklists.status_of/select_next/pending_targets 驗五態衍生——證明「四維綁成 AND、全過才 QUALIFIED」
這條合格存在契約真的串得起來（skill 是 markdown、無法單測；這裡測它呼叫的 CLI 串接）。

隔離：monkeypatch bl.targets（合成書單）、bl.RESOLUTION/ed.EDITIONS_DIR（tmp）、rs.enrich_links/
resolution_qc（擋網路）。不碰 repo、不打網路。"""

import argparse

from book_pipeline import booklists as bl
from book_pipeline import editions as ed
from book_pipeline import resolve as rs


def _targets():
    """合成書單：1 主書 + 其解答本 + 1 待 discovery 新書（dummy 領域）。"""
    def mk(slug, kind='main', of=None, title=None):
        return {'slug': slug, 'title': title or slug, 'author': 'Au', 'edition_pref': '3rd',
                'field': '資訊', 'field_id': 'cs', 'subject': '演算法', 'kind': kind, 'of': of,
                'order': (30, 0, 0, 0 if kind == 'main' else 1), 'source': 'booklist'}
    return [mk('algo'), mk('algo_sol', kind='solution', of='algo', title='algo — Solutions'),
            mk('newbook')]


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(bl, 'targets', lambda *a, **k: _targets())
    monkeypatch.setattr(bl, 'load_files', lambda *a, **k: [])      # targets 已注入，骨架不需
    monkeypatch.setattr(bl, 'have_slugs', lambda *a, **k: set())
    monkeypatch.setattr(bl, 'RESOLUTION', str(tmp_path / 'res.json'))
    monkeypatch.setattr(ed, 'EDITIONS_DIR', str(tmp_path / 'editions'))
    monkeypatch.setattr(rs, 'enrich_links', lambda *a, **k: {})
    monkeypatch.setattr(rs, 'resolution_qc', lambda *a, **k: {'advisory': [], 'block': []})


def _commit_found(slug, i='1', h='h'):
    rs.cmd_commit(argparse.Namespace(slug=slug, id=i, hash=h, title='T', author='Au', mb=5.0,
                                     absent=False, force=False, note=None, status=None, by='restock'))


def _set(slug, **kw):
    base = dict(slug=slug, label=None, year=None, publisher=None, isbn=None, matches_pref=None,
                confidence=None, sol_aligned=None, parent_version=None, sol_version=None, basis=None,
                field_id=None, subject=None, eligible=None, evidence=None, source=None, by='restock')
    base.update(kw)
    ed.cmd_set(argparse.Namespace(**base))


def _state(slug):
    return bl.status_of(slug, set(), bl.load_resolution(), ed.load(slug))


def test_four_dim_main_book_and_or(monkeypatch, tmp_path):
    """主書四維 AND：CANDIDATE →(found) PENDING →(version matches_pref) QUALIFIED。缺一即 PENDING。"""
    _setup(monkeypatch, tmp_path)
    assert _state('algo') == bl.CANDIDATE                       # 無連結
    _commit_found('algo')
    assert _state('algo') == bl.PENDING                        # 有連結，維③①未驗（存量遷入 eligible 但此處全新）
    _set('algo', eligible=True)                                # 維①
    assert _state('algo') == bl.PENDING                        # 仍缺維③
    _set('algo', label='3rd', matches_pref=True, confidence='high')  # 維③
    assert _state('algo') == bl.QUALIFIED                      # 四維全過（主書 ④N/A）
    # select_next 此時取得 algo（合格才下載）
    picks = [p['slug'] for p in bl.select_next(5, ed.load_all(), set(), bl.load_resolution())]
    assert picks == ['algo'], picks
    print('✓ 主書：CANDIDATE→PENDING→QUALIFIED（維②①③ AND）、select_next 只取合格')


def test_four_dim_version_mismatch_stays_pending(monkeypatch, tmp_path):
    """只有別版（--no-matches-pref）→ 維③不過 → 留 PENDING、不被 select_next 下載；recheck cooldown 後回母體。"""
    import datetime as dt
    _setup(monkeypatch, tmp_path)
    _commit_found('algo')
    _set('algo', eligible=True, label='2nd', matches_pref=False, confidence='high')
    assert _state('algo') == bl.PENDING
    assert bl.select_next(5, ed.load_all(), set(), bl.load_resolution()) == []  # 不下載別版
    # 剛親查（checked_at=now）→ resting、暫不在回查母體（防 busy-loop）
    now0 = dt.datetime.now(dt.timezone.utc)
    pend_now = [t['slug'] for t in bl.pending_targets(ed.load_all(), set(), bl.load_resolution(), now=now0)]
    assert 'algo' not in pend_now, pend_now
    # cooldown 窗到期 → 回母體重查
    later = now0 + dt.timedelta(days=bl.RECHECK_COOLDOWN_DAYS + 1)
    pend_later = [t['slug'] for t in bl.pending_targets(ed.load_all(), set(), bl.load_resolution(), now=later)]
    assert 'algo' in pend_later, pend_later
    print('✓ 別版：維③ no-matches-pref → PENDING 不下載；剛查 resting、cooldown 到期回母體')


def test_solution_dim_four(monkeypatch, tmp_path):
    """解答本維④：三維過 + sol_aligned 才 QUALIFIED；no-sol-aligned 留 PENDING。"""
    _setup(monkeypatch, tmp_path)
    _commit_found('algo_sol')
    _set('algo_sol', eligible=True, label='3rd', matches_pref=True)
    assert _state('algo_sol') == bl.PENDING                    # 缺維④
    _set('algo_sol', sol_aligned=True, parent_version='3rd', sol_version='3rd', basis='同版')
    assert _state('algo_sol') == bl.QUALIFIED                  # 維④對齊 → 合格
    print('✓ 解答本：維④ sol_aligned 才 QUALIFIED、未對齊 PENDING')


def test_rejected_paths(monkeypatch, tmp_path):
    """REJECTED 兩路：not_found（z-lib 真無）/ 判不夠格（--no-eligible，即使有連結）。"""
    _setup(monkeypatch, tmp_path)
    # not_found
    rs.cmd_commit(argparse.Namespace(slug='algo', id=None, hash=None, title=None, author=None, mb=None,
                                     absent=False, force=False, note='真無', status='not_found', by='restock'))
    assert _state('algo') == bl.REJECTED
    # 判不夠格：有連結但 eligible=False → REJECTED（不夠格優先於 QUALIFIED）
    _commit_found('newbook')
    _set('newbook', eligible=False, evidence=['大眾科普'])      # --evidence 是 append list
    assert _state('newbook') == bl.REJECTED
    assert bl.select_next(5, ed.load_all(), set(), bl.load_resolution()) == []  # REJECTED 不下載
    print('✓ REJECTED：not_found + 判不夠格（有連結仍 REJECTED）皆不下載')


def test_owned_preservation(monkeypatch, tmp_path):
    """owned 保命：已收錄書即使無 editions/未驗，一律 OWNED、永不降級。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(bl, 'have_slugs', lambda *a, **k: {'algo'})
    # 即使 resolution 標 not_found、無 editions，owned 仍最優先
    rs.cmd_commit(argparse.Namespace(slug='algo', id=None, hash=None, title=None, author=None, mb=None,
                                     absent=False, force=False, note='x', status='not_found', by='restock'))
    assert bl.status_of('algo', {'algo'}, bl.load_resolution(), ed.load('algo')) == bl.OWNED
    print('✓ owned 保命：已收錄書一律 OWNED、不因 not_found/未驗降級')


def test_net_gain_termination_metric(monkeypatch, tmp_path):
    """自我終止判據＝qualified 淨增：progress overall 的 qualified 計數隨四維補齊單調上升。"""
    _setup(monkeypatch, tmp_path)
    base = bl.progress(ed.load_all(), set(), bl.load_resolution())['overall'][bl.QUALIFIED]
    assert base == 0
    _commit_found('algo'); _set('algo', eligible=True, label='3rd', matches_pref=True)
    after = bl.progress(ed.load_all(), set(), bl.load_resolution())['overall'][bl.QUALIFIED]
    assert after == base + 1                                    # 淨增 1（/restock 據此累計到 +100 終止）
    print('✓ 終止判據：progress qualified 計數隨四維補齊上升（/restock 累計淨增到 100）')


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
