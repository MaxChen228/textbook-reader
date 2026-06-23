"""watch.py 標籤/終態邏輯測試（push 事件流的純函式核）。

重點＝_sol 解答書對 snapshot 隱形（綁母書）→ 解析到母書、用 sol 進度當終態判據；別讓 _sol 永不終態
卡死監控（R3 dogfood 抓到的缺口）。全 hermetic：合成 rows dict，不讀真 status.json。
"""
from __future__ import annotations

from book_pipeline import watch as w


def _rows(*books):
    return {b['slug']: b for b in books}


# ── 主書標籤 ──────────────────────────────────────────────────────────────────
def test_main_label_deployed_and_stage():
    rows = _rows({'slug': 'a', 'deployed': True, 'stage': '4 sol已merge'},
                 {'slug': 'b', 'deployed': False, 'stage': '0.5 OCR處理中'})
    assert w._slug_label(rows, 'a') == 'deployed'      # deployed 優先
    assert w._slug_label(rows, 'b') == '0.5 OCR處理中'
    assert w._slug_label(rows, 'missing') == '?'


# ── _sol 解析到母書 ───────────────────────────────────────────────────────────
def test_sol_label_resolves_parent():
    rows = _rows(
        {'slug': 'ext', 'deployed': True, 'stage': '3 parsed', 'todo': 'sol_extract(ext_sol)'},
        {'slug': 'ing', 'deployed': True, 'stage': '3 parsed', 'todo': 'sol_ingest(ing_sol)'},
        {'slug': 'done', 'deployed': True, 'stage': '4 sol已merge', 'todo': '—'},
        {'slug': 'rej', 'deployed': False, 'stage': 'R qc拒', 'todo': '—'},
        {'slug': 'pre', 'deployed': False, 'stage': '1 待audit', 'todo': 'audit'},
        {'slug': 'parkd', 'deployed': True, 'stage': '3 parsed', 'todo': '—'},  # 無 sol todo 且未 merge
    )
    assert w._slug_label(rows, 'ext_sol') == 'sol·extract中'      # 母書已 deployed 仍報 sol-pending
    assert w._slug_label(rows, 'ing_sol') == 'sol·ingest中'
    assert w._slug_label(rows, 'done_sol') == 'sol·已merge'
    assert w._slug_label(rows, 'rej_sol') == 'sol·母書R qc拒'      # 母書卡關 → sol 無法 merge
    assert w._slug_label(rows, 'pre_sol') == 'sol·母書1 待audit'  # 母書前置 → sol 不能動
    assert w._slug_label(rows, 'parkd_sol') == 'sol·待裁決'        # _pending/_escalated → proposal
    assert w._slug_label(rows, 'orphan_sol') == 'sol·母書缺'       # 母書不在 snapshot


# ── 終態判定（含 _sol 諸態）────────────────────────────────────────────────────
def test_is_terminal():
    T, F = True, False
    cases = {
        'deployed': T, 'sol·已merge': T, 'sol·待裁決': T, 'sol·母書缺': T,
        'R qc拒': T, 'X 無源': T, 'sol·母書R qc拒': T, 'sol·母書X 無源': T,
        '0.5 OCR處理中': F, '1 待audit': F, '3 parsed': F,
        'sol·ingest中': F, 'sol·extract中': F, 'sol·母書1 待audit': F,
    }
    for label, want in cases.items():
        assert w._is_terminal(label) is want, (label, want)


def test_icon():
    assert w._icon('deployed') == '✅' and w._icon('sol·已merge') == '✅'
    assert w._icon('X 無源') == '⚠' and w._icon('sol·母書X 無源') == '⚠'
    assert w._icon('R qc拒') == '🔴' and w._icon('sol·母書R qc拒') == '🔴'
    assert w._icon('sol·待裁決') == '🔴' and w._icon('sol·母書缺') == '🔴'
    assert w._icon('sol·ingest中') == '▸' and w._icon('0.5 OCR處理中') == '▸'


if __name__ == '__main__':
    import sys
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
