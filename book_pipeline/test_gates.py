#!/usr/bin/env python3
"""book_pipeline.test_gates — 閘門決策核心 + 狀態 I/O 的回歸鎖。

釘住使用者倚賴的 invariant：
① **fail-safe 預設暫停**：缺檔/壞檔/缺鍵/值非法/未知 stage → gate_allows 全 hold、gates_active True
   （承接 test_pipeline_run_state 的三條 fail-safe 回歸）。
② **first-match-wins**：依序第一條 match 的 rule 說了算（順序即優先級、零隱藏權重）。
③ **corpus lane slug=None 只 match slug=="*"**。
④ **gates_active = default=='hold'**（不含「任一 hold rule」）。
⑤ **兩目標場景**端到端：只做 math sweep / 只推 N 本不 crawl。
⑥ **CLI mutator 驗證 + 原子落盤**：未知 stage→ValueError、rm 越界→IndexError、寫即生效。

全程把狀態路徑導去 tempdir，**絕不碰真 .control/**（避免擾動 live daemon）。"""
from __future__ import annotations

import json
import os
import tempfile

from book_pipeline import pipeline_gates as pg


def _redirect(tmp: str):
    """把 pg 的狀態路徑導去 tmp，回 restore() 還原 module 全域（鎖檔/atomic_write 皆由 CONTROL_DIR 即時推導）。"""
    saved = (pg.CONTROL_DIR, pg.GATES_PATH)
    pg.CONTROL_DIR = tmp
    pg.GATES_PATH = os.path.join(tmp, 'gates.json')

    def restore():
        pg.CONTROL_DIR, pg.GATES_PATH = saved
    return restore


def _write_raw(obj):
    """直寫原始內容（繞 mutator，測 fail-safe / 手改檔情境）。"""
    with open(pg.GATES_PATH, 'w', encoding='utf-8') as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False)


def test_fail_safe_missing_broken():
    restore = _redirect(tempfile.mkdtemp())
    try:
        # 缺檔 → 全 hold
        assert not os.path.exists(pg.GATES_PATH)
        assert pg.load_gates() == {'default': 'hold', 'rules': []}
        assert pg.gate_allows('anybook', 'audit') is False
        assert pg.gate_allows(None, 'math_sweep') is False
        assert pg.gates_active() is True
        # 壞 json → fail-safe hold
        _write_raw('{ broken json')
        assert pg.gate_allows('x', 'parse') is False
        assert pg.gates_active() is True
    finally:
        restore()
    print('✓ gates：缺檔/壞檔 → fail-safe 全 hold + gates_active True')


def test_fail_safe_bad_structure():
    restore = _redirect(tempfile.mkdtemp())
    try:
        # 缺 default 鍵 → hold
        _write_raw({'rules': []})
        assert pg.gate_allows('x', 'audit') is False
        # default 值非法 → hold
        _write_raw({'default': 'maybe', 'rules': []})
        assert pg.gate_allows('x', 'audit') is False
        # 未知 stage（假閘）→ 整份 fail-safe hold（不靜默丟單條）
        _write_raw({'default': 'allow', 'rules': [{'slug': '*', 'stage': 'triage', 'action': 'allow'}]})
        assert pg.load_gates() == {'default': 'hold', 'rules': []}
        assert pg.gate_allows('x', 'audit') is False, 'triage 假閘 → 整份 fail-safe hold'
        # rule 缺欄/型別錯 → hold
        _write_raw({'default': 'allow', 'rules': [{'slug': '*', 'action': 'allow'}]})
        assert pg.gate_allows('x', 'audit') is False
    finally:
        restore()
    print('✓ gates：缺鍵/值非法/未知 stage/壞 rule → 整份 fail-safe hold（嚴格、可見）')


def test_first_match_wins():
    restore = _redirect(tempfile.mkdtemp())
    try:
        # {*,audit,hold} 在前、{slugX,*,allow} 在後 → (slugX,audit) 第一條 match 是 hold → 擋下
        pg.set_gates('allow', [
            {'slug': '*', 'stage': 'audit', 'action': 'hold'},
            {'slug': 'slugX', 'stage': '*', 'action': 'allow'},
        ])
        assert pg.gate_allows('slugX', 'audit') is False, '順序在前的 hold 先命中'
        assert pg.gate_allows('slugX', 'parse') is True, 'slugX 非 audit → 第二條 allow'
        assert pg.gate_allows('other', 'audit') is False, '其他書 audit 被第一條擋'
        assert pg.gate_allows('other', 'parse') is True, '無 match → default allow'
        # 對調順序 → 結果相反（證明順序即優先級）
        pg.set_gates('allow', [
            {'slug': 'slugX', 'stage': '*', 'action': 'allow'},
            {'slug': '*', 'stage': 'audit', 'action': 'hold'},
        ])
        assert pg.gate_allows('slugX', 'audit') is True, '對調後 slugX 全放行先命中'
        assert pg.gate_allows('other', 'audit') is False
    finally:
        restore()
    print('✓ gates：first-match-wins（順序即優先級、無隱藏權重）')


def test_corpus_lane_none_matches_only_star():
    restore = _redirect(tempfile.mkdtemp())
    try:
        pg.set_gates('hold', [
            {'slug': 'book1', 'stage': '*', 'action': 'allow'},  # 具體 slug
            {'slug': '*', 'stage': 'math_sweep', 'action': 'allow'},  # 星號
        ])
        # corpus lane slug=None：只 match 星號規則
        assert pg.gate_allows(None, 'math_sweep') is True
        assert pg.gate_allows(None, 'gc') is False, 'None 不 match book1 規則 → default hold'
        assert pg.gate_allows(None, 'crawl') is False
        # 具體 slug 仍 match 自己的規則
        assert pg.gate_allows('book1', 'gc') is True
    finally:
        restore()
    print('✓ gates：corpus lane slug=None 只 match slug=="*"')


def test_gates_active_is_default_hold_only():
    restore = _redirect(tempfile.mkdtemp())
    try:
        # default hold + 有 allow rule → 仍 active（值得保活）
        pg.set_gates('hold', [{'slug': '*', 'stage': 'math_sweep', 'action': 'allow'}])
        assert pg.gates_active() is True
        # default allow + 有 hold rule（只擋 crawl）→ 不 active（該 idle-exit）
        pg.set_gates('allow', [{'slug': '*', 'stage': 'crawl', 'action': 'hold'}])
        assert pg.gates_active() is False, 'default allow → 不保活，縱有 hold rule'
        pg.set_gates('allow', [])
        assert pg.gates_active() is False
    finally:
        restore()
    print('✓ gates：gates_active == (default==hold)，不含「任一 hold rule」')


def test_scenario_math_only():
    restore = _redirect(tempfile.mkdtemp())
    try:
        pg.set_gates('hold', [{'slug': '*', 'stage': 'math_sweep', 'action': 'allow'}])
        assert pg.gate_allows(None, 'math_sweep') is True
        assert pg.gate_allows(None, 'gc') is False
        assert pg.gate_allows(None, 'crawl') is False
        assert pg.gate_allows('anybook', 'audit') is False
        assert pg.gate_allows('anybook', 'deploy') is False
        assert pg.gates_active() is True
    finally:
        restore()
    print('✓ gates：場景①只做 math sweep（其餘全 held、保活）')


def test_scenario_push_n_books_no_crawl():
    restore = _redirect(tempfile.mkdtemp())
    try:
        six = ['demmel', 'knuth', 'mandl', 'mitchell', 'simmons', 'akenine']
        pg.set_gates('hold', [{'slug': s, 'stage': '*', 'action': 'allow'} for s in six])
        # 那幾本全程放行
        for s in six:
            assert pg.gate_allows(s, 'ingest') is True
            assert pg.gate_allows(s, 'audit') is True
            assert pg.gate_allows(s, 'deploy') is True
        # 其他書全 held、crawl（不派新書）全 held（default + slug=None 不 match 具體 slug）
        assert pg.gate_allows('otherbook', 'audit') is False
        assert pg.gate_allows(None, 'crawl') is False, '不派新書：global crawl held'
        assert pg.gate_allows('demmel', 'crawl') is True  # 那本若是 crawl 候選也放行（已 owned 不會觸發）
    finally:
        restore()
    print('✓ gates：場景②只推 N 本不 crawl')


def test_next_gate_status():
    restore = _redirect(tempfile.mkdtemp())
    try:
        pg.set_gates('hold', [{'slug': 'b', 'stage': 'parse', 'action': 'allow'}])
        assert pg.next_gate_status('b', None) is False, '無待辦 → 不 gated'
        assert pg.next_gate_status('b', 'parse') is False, '下一閘放行 → 不 gated'
        assert pg.next_gate_status('b', 'audit') is True, '下一閘被 held → gated'
    finally:
        restore()
    print('✓ gates：next_gate_status（snapshot per-book gated 欄）')


def test_mutators_validation_and_atomic():
    restore = _redirect(tempfile.mkdtemp())
    try:
        # 缺檔 base = fail-safe hold；add allow rule → hold 基線 + 例外
        g = pg.add_rule('*', 'math_sweep', 'allow')
        assert g == {'default': 'hold', 'rules': [{'slug': '*', 'stage': 'math_sweep', 'action': 'allow'}]}
        assert json.load(open(pg.GATES_PATH)) == g, '原子落盤、即時生效'
        # 未知 stage → ValueError（擋 silent no-op 假閘）
        try:
            pg.add_rule('*', 'triage', 'hold')
            assert False, '未知 stage 應 ValueError'
        except ValueError:
            pass
        # action 非法 → ValueError
        try:
            pg.add_rule('*', 'audit', 'maybe')
            assert False
        except ValueError:
            pass
        # rm 越界 → IndexError
        try:
            pg.rm_rule(5)
            assert False
        except IndexError:
            pass
        # set_default 保留 rules
        g = pg.set_default('allow')
        assert g['default'] == 'allow' and len(g['rules']) == 1
        # clear_rules 留 default
        g = pg.clear_rules()
        assert g == {'default': 'allow', 'rules': []}
        # pause/resume subsume
        assert pg.pause() == {'default': 'hold', 'rules': []}
        assert pg.gates_active() is True
        assert pg.resume() == {'default': 'allow', 'rules': []}
        assert pg.gates_active() is False
        # rm by index
        pg.add_rule('a', 'audit', 'hold')
        pg.add_rule('b', 'parse', 'allow')
        g = pg.rm_rule(0)
        assert g['rules'] == [{'slug': 'b', 'stage': 'parse', 'action': 'allow'}]
    finally:
        restore()
    print('✓ gates：mutator 驗證（未知 stage/action/越界）+ 原子落盤 + pause/resume subsume')


if __name__ == '__main__':
    test_fail_safe_missing_broken()
    test_fail_safe_bad_structure()
    test_first_match_wins()
    test_corpus_lane_none_matches_only_star()
    test_gates_active_is_default_hold_only()
    test_scenario_math_only()
    test_scenario_push_n_books_no_crawl()
    test_next_gate_status()
    test_mutators_validation_and_atomic()
    print('\n全部通過 ✅')
