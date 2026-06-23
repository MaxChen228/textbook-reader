#!/usr/bin/env python3
"""book_pipeline.test_provider_chain — chain runtime override 的 I/O + llm_policy 整合回歸鎖。

釘住 invariant：
① **fail-open**（與 gates fail-safe hold 相反）：缺檔/壞檔/未知 provider/結構違規 → load_override None
   → llm_policy 退回碼層 DEFAULT（派工絕不因壞檔癱瘓）。
② **set_chain 驗證**：空/未知 provider/重複 → ValueError；合法 → 原子落盤、load 讀回。
③ **clear 冪等**：刪檔回 DEFAULT、無檔不報錯。
④ **llm_policy 三層優先級**：DEFAULT ← runtime override ← env（env 最高）；effective_chain() 反映之。
⑤ **chain override 不污染其他欄**：codex_model/effort/timeout 仍來自 DEFAULT/STAGE。

全程把控制檔路徑導去 tempdir、env 用後即清，**絕不碰真 .control/ 或污染 daemon 環境**。"""
from __future__ import annotations

import json
import os
import tempfile

from book_pipeline import llm_policy as lp
from book_pipeline import provider_chain as pc


def _redirect(tmp: str):
    """把 pc 的控制檔路徑導去 tmp，回 restore()（鎖檔/atomic_write 由 CONTROL_DIR 即時推導）。"""
    saved = (pc.CONTROL_DIR, pc.CHAIN_PATH)
    pc.CONTROL_DIR = tmp
    pc.CHAIN_PATH = os.path.join(tmp, 'provider_chain.json')

    def restore():
        pc.CONTROL_DIR, pc.CHAIN_PATH = saved
    return restore


def _write_raw(obj):
    with open(pc.CHAIN_PATH, 'w', encoding='utf-8') as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False)


def _clear_env():
    for k in ('BOOK_PIPELINE_PROVIDER_CHAIN',):
        os.environ.pop(k, None)


def test_fail_open_missing_broken():
    restore = _redirect(tempfile.mkdtemp())
    try:
        assert not os.path.exists(pc.CHAIN_PATH)
        assert pc.load_override() is None, '缺檔（常態）→ 無 override'
        _write_raw('{ broken json')
        assert pc.load_override() is None, '壞 json → fail-open None'
        _write_raw('[1,2,3]')
        assert pc.load_override() is None, 'top-level 非 dict → None'
        _write_raw({'chain': []})
        assert pc.load_override() is None, '空 chain → None'
        _write_raw({'chain': 'codex'})
        assert pc.load_override() is None, 'chain 非 list → None'
        _write_raw({'chain': ['codex', 'codexx']})
        assert pc.load_override() is None, '含未知 provider → 整份作廢 None'
        _write_raw({'chain': ['codex', 123]})
        assert pc.load_override() is None, 'chain 元素型別錯 → None'
        _write_raw({'nope': ['codex']})
        assert pc.load_override() is None, '缺 chain 鍵 → None'
    finally:
        restore()
    print('✓ chain：fail-open（缺檔/壞檔/未知provider/結構違規 → load_override None 退 DEFAULT）')


def test_set_chain_validation_and_atomic():
    restore = _redirect(tempfile.mkdtemp())
    try:
        ch = pc.set_chain(['codex', 'codex-pool', 'claude'])
        assert ch == ('codex', 'codex-pool', 'claude')
        assert json.load(open(pc.CHAIN_PATH)) == {'chain': ['codex', 'codex-pool', 'claude']}, '原子落盤'
        assert pc.load_override() == ('codex', 'codex-pool', 'claude'), 'load 讀回'
        # 空 → ValueError
        try:
            pc.set_chain([]); assert False, '空應 ValueError'
        except ValueError:
            pass
        # 未知 provider → ValueError
        try:
            pc.set_chain(['codex', 'kimi']); assert False, '未知 provider 應 ValueError'
        except ValueError:
            pass
        # 重複 → ValueError
        try:
            pc.set_chain(['codex', 'codex']); assert False, '重複應 ValueError'
        except ValueError:
            pass
        # 單一 provider 合法
        assert pc.set_chain(['claude']) == ('claude',)
    finally:
        restore()
    print('✓ chain：set_chain 驗證（空/未知/重複→ValueError）+ 單一合法 + 原子落盤')


def test_clear_idempotent():
    restore = _redirect(tempfile.mkdtemp())
    try:
        pc.clear()  # 無檔不報錯
        pc.set_chain(['codex', 'claude'])
        assert pc.load_override() == ('codex', 'claude')
        pc.clear()
        assert pc.load_override() is None and not os.path.exists(pc.CHAIN_PATH), 'clear 刪檔回 DEFAULT'
        pc.clear()  # 冪等
    finally:
        restore()
    print('✓ chain：clear 冪等（刪檔回 DEFAULT、無檔不報錯）')


def test_llm_policy_three_layer_priority():
    """DEFAULT ← runtime override ← env（env 最高）；effective_chain() 反映之。"""
    restore = _redirect(tempfile.mkdtemp())
    _clear_env()
    try:
        # 無 override + 無 env → 碼層 DEFAULT
        assert lp.effective_chain() == lp.DEFAULT_DISPATCH.chain
        assert lp.resolve_dispatch('audit').chain == lp.DEFAULT_DISPATCH.chain
        # runtime override 凌駕 DEFAULT
        pc.set_chain(['claude', 'codex'])
        assert lp.effective_chain() == ('claude', 'codex'), 'override 凌駕 DEFAULT'
        assert lp.resolve_dispatch('qc').chain == ('claude', 'codex'), '所有 stage 同受 override（chain 不分 stage）'
        # env 凌駕 override
        os.environ['BOOK_PIPELINE_PROVIDER_CHAIN'] = 'codex-pool,claude'
        assert lp.effective_chain() == ('codex-pool', 'claude'), 'env 最高、蓋 override'
        _clear_env()
        assert lp.effective_chain() == ('claude', 'codex'), 'env 清掉 → 回 override'
        # 壞 override 檔（fail-open）→ 退 DEFAULT
        _write_raw('{ broken')
        assert lp.effective_chain() == lp.DEFAULT_DISPATCH.chain, '壞 override → fail-open 退 DEFAULT'
    finally:
        _clear_env()
        restore()
    print('✓ chain：llm_policy 三層優先級 DEFAULT←override←env + effective_chain + fail-open 退 DEFAULT')


def test_override_does_not_pollute_other_fields():
    """chain override 只動 chain；codex_model/effort/timeout 仍來自 DEFAULT/STAGE。"""
    restore = _redirect(tempfile.mkdtemp())
    _clear_env()
    try:
        pc.set_chain(['claude', 'codex'])
        spec = lp.resolve_dispatch('audit')  # audit 有 STAGE 覆寫 effort=high
        assert spec.chain == ('claude', 'codex')
        assert spec.codex_effort == 'high', 'audit effort 仍來自 STAGE_DISPATCH'
        assert spec.codex_model == lp.DEFAULT_DISPATCH.codex_model, 'model 仍來自 DEFAULT'
        assert spec.timeout == lp.DEFAULT_DISPATCH.timeout, 'timeout 仍來自 DEFAULT'
    finally:
        _clear_env()
        restore()
    print('✓ chain：override 只動 chain、不污染 model/effort/timeout')


def test_default_chain_is_codex_first():
    """碼層新常態：codex（原生 OAuth）→ codex-pool → claude。"""
    assert lp.DEFAULT_DISPATCH.chain == ('codex', 'codex-pool', 'claude'), '新常態 codex 優先'
    print('✓ chain：碼層 DEFAULT 新常態 = codex→codex-pool→claude')


if __name__ == '__main__':
    test_fail_open_missing_broken()
    test_set_chain_validation_and_atomic()
    test_clear_idempotent()
    test_llm_policy_three_layer_priority()
    test_override_does_not_pollute_other_fields()
    test_default_chain_is_codex_first()
    print('\n全部通過 ✅')
