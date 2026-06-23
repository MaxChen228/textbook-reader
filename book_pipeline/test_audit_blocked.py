"""audit_blocked 持久化標記測試（Phase 3-B）：

殺 aitchison 式空轉——audit agent 跑完(rc==0)卻產不出 extract_rules.yaml 且已開 engine 提案
（schema 表達不了）→ 標 audit_blocked → assess 回 review 終態、不再每 cycle 重派。

涵蓋四面：① pipeline_queue mark/read/clear roundtrip ② status.assess 見標記轉 'R audit-blocked'
③ status._stuck_reason 認 audit_blocked（→ cohort/stuck 可見）④ pipeline_tick._has_open_engine_proposal。
全 hermetic：重導 STATE_PATH/ROOT/DATA 到 tmp，finally 還原，絕不污染真實狀態。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile

from book_pipeline import pipeline_queue as pq
from book_pipeline import status as st


# ── ① pipeline_queue marker roundtrip ────────────────────────────────────────
@contextlib.contextmanager
def _tmp_state():
    d = tempfile.mkdtemp(prefix='audit_blocked_test_')
    saved = (pq.STATE_PATH, pq.STATE_LOCK)
    pq.STATE_PATH = os.path.join(d, 'pipeline_state.json')
    pq.STATE_LOCK = os.path.join(d, '.state.lock')
    try:
        yield d
    finally:
        (pq.STATE_PATH, pq.STATE_LOCK) = saved


def test_marker_roundtrip():
    with _tmp_state():
        assert pq.audit_blocked_review('bk') is None
        pq.mark_audit_blocked('bk', ['schema 表達不了'])
        m = pq.audit_blocked_review('bk')
        assert m and m['review'] is True and m['reasons'] == ['schema 表達不了'] and m.get('at')
        pq.clear_audit_blocked('bk')
        assert pq.audit_blocked_review('bk') is None


def test_clear_removes_empty_shell():
    """clear 後若該 slug 殼空（無其他標記）→ 整個 slug 鍵移除，不留 {slug:{}}。"""
    with _tmp_state():
        pq.mark_audit_blocked('bk', ['x'])
        pq.clear_audit_blocked('bk')
        assert 'bk' not in json.load(open(pq.STATE_PATH))


def test_clear_preserves_sibling_markers():
    """clear audit_blocked 不誤刪同 slug 的其他標記（如 qc verdict）。"""
    with _tmp_state():
        pq.set_qc('bk', 'pass', 'ok', 'claude')
        pq.mark_audit_blocked('bk', ['x'])
        pq.clear_audit_blocked('bk')
        s = json.load(open(pq.STATE_PATH))
        assert s.get('bk', {}).get('qc', {}).get('verdict') == 'pass'
        assert 'audit_blocked' not in s.get('bk', {})


# ── ②③ status.assess + _stuck_reason ─────────────────────────────────────────
@contextlib.contextmanager
def _sandbox():
    d = tempfile.mkdtemp(prefix='audit_blocked_assess_')
    data = os.path.join(d, 'book_pipeline', 'mineru_data')
    os.makedirs(data, exist_ok=True)
    saved = (st.ROOT, st.DATA)
    st.ROOT = d
    st.DATA = data
    try:
        yield d, data
    finally:
        (st.ROOT, st.DATA) = saved


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f) if not isinstance(obj, str) else f.write(obj)


def test_assess_audit_blocked_overrides_wait_audit():
    """unified 在、rules 不在＝待audit；但有 audit_blocked 標記 → 轉 'R audit-blocked' / todo '—'
    （不再回 audit 動詞 → daemon advance 見 'R' 前綴即停、不重派）。"""
    with _sandbox() as (root, data):
        _write(os.path.join(data, 'bk', 'unified', 'content_list.json'), [])
        # 無標記 → 正常待audit
        r = st.assess('bk')
        assert r['stage'] == '1 待audit' and r['todo'] == 'audit', r
        # 寫 audit_blocked 標記（_pstate 讀 ROOT/book_pipeline/pipeline_state.json）
        _write(os.path.join(root, 'book_pipeline', 'pipeline_state.json'),
               {'bk': {'audit_blocked': {'review': True, 'reasons': ['schema 表達不了']}}})
        r = st.assess('bk')
        assert r['stage'] == 'R audit-blocked' and r['todo'] == '—', r


def test_stuck_reason_reports_audit_blocked():
    e = {'audit_blocked': {'review': True, 'reasons': ['combined 2-volume 非連續多區附錄']}}
    r = {'stage': 'R audit-blocked', 'todo': '—'}
    assert st._stuck_reason('bk', r, e) == ('audit-blocked', 'combined 2-volume 非連續多區附錄')


def test_stuck_reason_qc_reject_precedes_audit_blocked():
    """qc-reject 與 audit_blocked 不會並存於正常流程，但確認既有 qc 分支不被新增分支搶先。"""
    e = {'qc': {'verdict': 'reject', 'note': '殘卷'},
         'audit_blocked': {'review': True, 'reasons': ['x']}}
    assert st._stuck_reason('bk', {'stage': 'R', 'todo': '—'}, e)[0] == 'qc-reject'


def test_stuck_reason_early_inflight_stages_not_stuck():
    """剛 intake、正在/即將處理的早期階段（待qc/待ingest/OCR處理中）= 在飛、非卡關（cohort 歸 ⏳）。
    回歸：原 startswith(('0','X')) 把雲端 OCR 中的書誤判 stuck、漏斗虛報卡關。"""
    for stage in ('0.2 待qc', '0 待ingest', '0.3 待ingest', '0.5 OCR處理中'):
        assert st._stuck_reason('bk', {'stage': stage, 'todo': 'ingest'}, {}) is None, stage


def test_stuck_reason_no_source_is_stuck():
    """'X 無源'=無 PDF 源、需人工找源 → 真卡關。"""
    assert st._stuck_reason('bk', {'stage': 'X 無源', 'todo': '—'}, {}) == ('X 無源', '—')


# ── ④ pipeline_tick._has_open_engine_proposal ────────────────────────────────
def test_has_open_engine_proposal(monkeypatch):
    from book_pipeline import pipeline_tick as pt
    from book_pipeline import proposals as pr

    fake = [
        {'id': 'P-2026-06-19-foo-book', 'status': 'proposed', 'domain': 'engine'},
        {'id': 'P-2026-06-19-bar-book', 'status': 'superseded', 'domain': 'engine'},
        {'id': 'P-2026-06-19-baz-book', 'status': 'proposed', 'domain': 'catalog'},
    ]
    monkeypatch.setattr(pr, 'load_all', lambda: fake)
    assert pt._has_open_engine_proposal('foo_book') is True
    assert pt._has_open_engine_proposal('bar_book') is False   # superseded 不算開
    assert pt._has_open_engine_proposal('baz_book') is False   # 非 engine 不算
    assert pt._has_open_engine_proposal('other_book') is False  # 無提案


def test_has_open_engine_proposal_parked_is_open(monkeypatch):
    """parked engine 提案（已分類·等外部）仍是 blocker → 不該讓母書 audit churn（算開）。"""
    from book_pipeline import pipeline_tick as pt
    from book_pipeline import proposals as pr
    monkeypatch.setattr(pr, 'load_all',
                        lambda: [{'id': 'P-2026-06-23-foo-book', 'status': 'parked',
                                  'domain': 'engine',
                                  'unblock': {'kind': 'engine-capability', 'target': 'parser.py'}}])
    assert pt._has_open_engine_proposal('foo_book') is True


def test_has_open_engine_proposal_matches_suffixed_id(monkeypatch):
    """同書多提案會有 -2/-3 後綴 id；前綴比對須命中。"""
    from book_pipeline import pipeline_tick as pt
    from book_pipeline import proposals as pr
    monkeypatch.setattr(pr, 'load_all',
                        lambda: [{'id': 'P-2026-06-19-conway-functional-analysis-2',
                                  'status': 'proposed', 'domain': 'engine'}])
    assert pt._has_open_engine_proposal('conway_functional_analysis') is True


def test_has_open_engine_proposal_rejects_sol_suffix(monkeypatch):
    """_sol 提案（id 帶 -sol）不可讓母書被誤標 audit_blocked（後綴只認 -<digits>）。"""
    from book_pipeline import pipeline_tick as pt
    from book_pipeline import proposals as pr
    monkeypatch.setattr(pr, 'load_all',
                        lambda: [{'id': 'P-2026-06-19-foo-book-sol',
                                  'status': 'proposed', 'domain': 'engine'}])
    assert pt._has_open_engine_proposal('foo_book') is False


if __name__ == '__main__':
    import sys
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
