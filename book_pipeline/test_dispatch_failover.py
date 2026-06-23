"""dispatch_llm provider failover：uv run python -m book_pipeline.test_dispatch_failover

驗證 failover 涵蓋「服務中斷」而非僅「額度」（RC2，2026-06-18）：鏈首掛掉（outage）→ 自動落鏈上
下一 provider；額度（limit）亦然；但「agent 真跑了卻任務失敗」(reason=None) 不換 provider。
**期待順序從生效鏈即時派生**（lp.resolve_dispatch('audit').chain）→ 改 DEFAULT/override/env 順序測試
自動跟、不寫死特定 provider 名，零維護債。monkeypatch _run_one 模擬各 provider 結果，不起子進程、不打網路。"""

import os

from book_pipeline import llm_policy as lp
from book_pipeline import pipeline_tick as pt


# 測試用**固定** failover 鏈（hermetic）：絕不讀 live `resolve_dispatch` 生效鏈——runtime override
# （devctl chain set，如使用者「只用 codex-pool」會使生效鏈剩 1 段）會讓 failover 測試假紅。_patch 把
# `_resolve_dispatch` pin 成此鏈，故測試永遠驗的是 failover **語意**、與當下營運鏈無關（2026-06-24 定）。
_HCHAIN = ('codex', 'codex-pool', 'claude')


def _chain():
    """failover 測試用的固定 3-provider 鏈（hermetic，見 _HCHAIN）。"""
    return list(_HCHAIN)


def _patch(results, chain=_HCHAIN):
    """results: {provider: (rc, reason)}。回 (calls 累積串, restore 函式)。
    - pin `_resolve_dispatch` → 固定 chain（hermetic，不受 runtime provider_chain.json override 影響）。
    - 重置 exhausted 共享集；LOG 暫導向 os.devnull——dispatch_llm 的 failover 行（『⚠ codex 撞額度』
      『❌ 全 provider 不可用 audit x』）不該污染真 reports/daemon.log，否則 ops 看板（devctl status /
      /dev）冒假 🔴 cry-wolf（2026-06-23 使用者從 /dev 看板撞見這批合成 slug='x' 的假 outage）。"""
    calls = []
    orig_run, orig_resolve, orig_log = pt._run_one, pt._resolve_dispatch, pt.LOG
    pt.LOG = os.devnull
    pt._resolve_dispatch = lambda verb: lp.DispatchSpec(chain=tuple(chain), codex_model='gpt-5.4')
    with pt._exhausted_lock:
        pt._exhausted_at.clear()

    def fake(provider, todo_verb, slug, prompt, spec):
        calls.append(provider)
        return results.get(provider, (0, None))
    pt._run_one = fake

    def restore():
        pt._run_one, pt._resolve_dispatch, pt.LOG = orig_run, orig_resolve, orig_log
        with pt._exhausted_lock:
            pt._exhausted_at.clear()
    return calls, restore


def test_outage_fails_over():
    # 鏈首中斷（outage）→ 落鏈上第二個，回第二個的 rc=0、不再試其後
    p0, p1 = _chain()[:2]
    calls, restore = _patch({p0: (1, 'outage'), p1: (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0, rc
        assert calls == [p0, p1], calls
        print(f'✓ outage：{p0} 中斷 → failover 到 {p1}、止於首個成功')
    finally:
        restore()


def test_limit_fails_over():
    # 鏈上除末位外逐個撞額度 → 一路 failover 到末位（保底）成功。對任意鏈長成立。
    chain = _chain()
    *heads, last = chain
    calls, restore = _patch({**{p: (1, 'limit') for p in heads}, last: (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0 and calls == chain, (rc, calls)
        print(f'✓ limit：逐個撞額度 → 一路 failover 到 {last}(保底)')
    finally:
        restore()


def test_task_failure_does_not_fail_over():
    # reason=None（agent 真跑了卻 rc≠0）→ 不換 provider，rc 直接回呼叫端
    p0 = _chain()[0]
    calls, restore = _patch({p0: (2, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 2 and calls == [p0], (rc, calls)
        print('✓ task-failure：有事件卻失敗 → 不 failover（換 provider 無益、防雙寫）')
    finally:
        restore()


def test_all_unavailable_defers():
    chain = _chain()
    calls, restore = _patch({p: (1, 'outage') for p in chain})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == -2, rc                               # 全鏈耗盡 → defer
        assert calls == chain, calls
        print('✓ 全鏈不可用 → -2 defer（下個 cycle 重試）')
    finally:
        restore()


def test_outage_classification():
    # _run_one 的分類：零事件 或 5xx/連線標記 → outage；額度標記 → limit；rc=0 → None
    assert pt._hit_outage('http error 503: service unavailable')
    assert pt._hit_outage('connection refused')
    assert not pt._hit_outage('some normal text')
    assert pt._hit_limit('codex-pool', 'rate limit exceeded')
    print('✓ _hit_outage/_hit_limit 標記分類正確')


def test_exhaustion_ttl_expires():
    """回歸：暫態額度標記須 TTL 後自動失效（reactive loop 不每 cycle clear，永久標記會黏死整個 walltime
    落 claude——2026-06-23 實證 codex 撞額度後 50min 全 claude）。標記→TTL 內 skip→過 TTL 自動清、重探。"""
    import time
    with pt._exhausted_lock:
        pt._exhausted_at.clear()
        now = time.monotonic()
        pt._exhausted_at['codex'] = now
        assert pt._is_exhausted('codex', now) is True                       # 剛標記 → 跳過
        assert pt._is_exhausted('codex', now + pt._EXHAUST_TTL - 1) is True  # TTL 內 → 仍跳過
        assert pt._is_exhausted('codex', now + pt._EXHAUST_TTL + 1) is False # 過 TTL → 重探
        assert 'codex' not in pt._exhausted_at, '過 TTL 須清除戳記（下次乾淨重探）'
        assert pt._is_exhausted('claude', now) is False                     # 未標記 → 不跳過
    print('✓ exhaustion TTL：暫態標記過期自動重探、不黏死 controller 命')


def test_codex_pool_pins_endpoint():
    """codex-pool 派工 model 必帶 `@codex-pool/` 前綴（繞過 ccNexus 默認輪詢→下架 kimi→400
    tokenization failed，2026-06-24 CLI 路徑實證）；**原生 codex（OAuth 直連）絕不帶前綴、絕不帶
    -p nexus**（pin 會把主力 provider 強推進 ccNexus 打掛——advisor 點名最該守的負向案例）。
    鎖死 _build_llm_cmd 與 _display_model 兩 model 出口，及 pin_codex_pool 冪等。"""
    spec = lp.resolve_dispatch('audit')

    def model_of(cmd):
        return cmd[cmd.index('--model') + 1]

    pool_cmd = pt._build_llm_cmd('codex-pool', 'x', spec)
    codex_cmd = pt._build_llm_cmd('codex', 'x', spec)
    # codex-pool：pin @codex-pool/ + 帶 nexus profile
    assert model_of(pool_cmd).startswith('@codex-pool/'), f'codex-pool 須 pin：{model_of(pool_cmd)}'
    assert '-p' in pool_cmd and 'nexus' in pool_cmd, 'codex-pool 須帶 -p nexus profile'
    # 負向（advisor 點名最該守）：原生 codex 保持裸名、不得 pin、不得帶 -p nexus
    assert not model_of(codex_cmd).startswith('@'), f'原生 codex 不得 pin：{model_of(codex_cmd)}'
    assert '-p' not in codex_cmd, '原生 codex 不得帶 -p nexus（會走 ccNexus 打掛主力）'
    # 顯示出口同步 pin（面板/歷程不誤導實際打哪個 endpoint）
    assert pt._display_model('codex-pool', spec).startswith('@codex-pool/')
    assert not pt._display_model('codex', spec).startswith('@')
    # pin_codex_pool 冪等：已帶 @ 前綴尊重原值（運維可釘別池）；裸名加前綴
    assert lp.pin_codex_pool('@other/m') == '@other/m'
    assert lp.pin_codex_pool('gpt-5.4') == '@codex-pool/gpt-5.4'
    # math_sweep（HTTP path）與 CLI 同 pin（兩條 codex-pool 路徑一致）
    assert lp.math_sweep_model().startswith('@codex-pool/')
    print('✓ codex-pool pin @codex-pool/ + 原生 codex 裸名（兩向鎖死）+ math/CLI 同 pin')


def test_single_provider_chain_no_exhaust():
    """**單一 provider 鏈不套 exhaustion**：outage 不標 exhausted、連兩次 dispatch 都真試該 provider。
    根因＝sole provider 沒有 next 可 failover，標 exhausted 零失效益、只剩跨 cycle 300s 全停的純害（一個
    間歇 blip 黑名單唯一 provider 5 分鐘）。2026-06-24 dogfood：codex-pool-only 下間歇 400→outage→codex-pool
    被標 exhausted→catalog_audit backlog 卡死 ~5min/blip。對比 multi-provider 仍標 exhausted（既有測試涵蓋）。"""
    calls, restore = _patch({'codex-pool': (1, 'outage')}, chain=('codex-pool',))
    try:
        rc1 = pt.dispatch_llm('audit', 'x', dry=False)
        rc2 = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc1 == -2 and rc2 == -2, (rc1, rc2)               # 無 fallback → 兩次皆 -2 defer
        assert calls == ['codex-pool', 'codex-pool'], \
            f'單一 provider 須每 cycle 都真試、不因 exhaust 跳過：{calls}'
        with pt._exhausted_lock:
            assert 'codex-pool' not in pt._exhausted_at, '單一 provider 鏈不該標 exhausted（否則暫態 blip 黑名單 300s）'
        print('✓ 單一 provider 鏈：outage 不標 exhausted、每 cycle 重試（暫態靠 defer-retry 化解，不全停 300s）')
    finally:
        restore()


def test_multi_provider_still_exhausts():
    """回歸守衛：multi-provider 鏈**仍**標 exhausted（fast-failover 語意不被單一 provider 修法波及）。
    鏈首 outage → 標 exhausted + 串接下一個；確認 _exhausted_at 留下鏈首戳記。"""
    calls, restore = _patch({'codex': (1, 'outage'), 'codex-pool': (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0 and calls == ['codex', 'codex-pool'], (rc, calls)
        with pt._exhausted_lock:
            assert 'codex' in pt._exhausted_at, 'multi-provider 鏈首 outage 須標 exhausted（fast-failover）'
        print('✓ multi-provider 仍標 exhausted（單一 provider 修法未波及 failover 語意）')
    finally:
        restore()


if __name__ == '__main__':
    test_outage_fails_over()
    test_limit_fails_over()
    test_task_failure_does_not_fail_over()
    test_all_unavailable_defers()
    test_outage_classification()
    test_exhaustion_ttl_expires()
    test_codex_pool_pins_endpoint()
    test_single_provider_chain_no_exhaust()
    test_multi_provider_still_exhausts()
    print('\n全部通過 ✅')
