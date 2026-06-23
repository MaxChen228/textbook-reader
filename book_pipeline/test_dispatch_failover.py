"""dispatch_llm provider failover：uv run python -m book_pipeline.test_dispatch_failover

驗證 failover 涵蓋「服務中斷」而非僅「額度」（RC2，2026-06-18）：鏈首掛掉（outage）→ 自動落鏈上
下一 provider；額度（limit）亦然；但「agent 真跑了卻任務失敗」(reason=None) 不換 provider。
**期待順序從生效鏈即時派生**（lp.resolve_dispatch('audit').chain）→ 改 DEFAULT/override/env 順序測試
自動跟、不寫死特定 provider 名，零維護債。monkeypatch _run_one 模擬各 provider 結果，不起子進程、不打網路。"""

import os

from book_pipeline import llm_policy as lp
from book_pipeline import pipeline_tick as pt


def _chain():
    """dispatch_llm 實際遵循的 provider 順序（= resolve_dispatch 的生效鏈）。長度**不寫死**——碼層常態
    3 段（codex/codex-pool/claude），但 runtime override（devctl chain set，如禁 claude 只留
    codex,codex-pool）可使其 2 段。測試驗 failover **語意**而非特定鏈長 → 對 ≥2 段皆成立、不隨營運
    換鏈而假紅（2026-06-24：禁 claude 後鏈剩 2 段，舊 len==3 硬斷言誤殺 4 測）。"""
    chain = list(lp.resolve_dispatch('audit').chain)
    assert len(chain) >= 2, f'failover 測試需 ≥2-provider 鏈，得到 {chain}'
    return chain


def _patch(results):
    """results: {provider: (rc, reason)}。回 (calls 累積串, restore 函式)。每次重置 exhausted 共享集；
    並把 LOG 暫導向 os.devnull——dispatch_llm 的 failover 行（『⚠ codex 撞額度』『❌ 全 provider 不可用
    audit x』）不該污染真 reports/daemon.log，否則 ops 看板（devctl status / /dev）冒假 🔴 cry-wolf
    （2026-06-23 使用者從 /dev 看板撞見這批合成 slug='x' 的假 outage）。"""
    calls = []
    orig = pt._run_one
    orig_log = pt.LOG
    pt.LOG = os.devnull
    with pt._exhausted_lock:
        pt._exhausted_at.clear()

    def fake(provider, todo_verb, slug, prompt, spec):
        calls.append(provider)
        return results.get(provider, (0, None))
    pt._run_one = fake

    def restore():
        pt._run_one = orig
        pt.LOG = orig_log
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


if __name__ == '__main__':
    test_outage_fails_over()
    test_limit_fails_over()
    test_task_failure_does_not_fail_over()
    test_all_unavailable_defers()
    test_outage_classification()
    test_exhaustion_ttl_expires()
    test_codex_pool_pins_endpoint()
    print('\n全部通過 ✅')
