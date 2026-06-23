"""dispatch_llm provider failover：uv run python -m book_pipeline.test_dispatch_failover

驗證 failover 涵蓋「服務中斷」而非僅「額度」（RC2，2026-06-18）：鏈首掛掉（outage）→ 自動落鏈上
下一 provider；額度（limit）亦然；但「agent 真跑了卻任務失敗」(reason=None) 不換 provider。
**期待順序從生效鏈即時派生**（lp.resolve_dispatch('audit').chain）→ 改 DEFAULT/override/env 順序測試
自動跟、不寫死特定 provider 名，零維護債。monkeypatch _run_one 模擬各 provider 結果，不起子進程、不打網路。"""

from book_pipeline import llm_policy as lp
from book_pipeline import pipeline_tick as pt


def _chain():
    """dispatch_llm 實際遵循的 provider 順序（= resolve_dispatch 的生效鏈）。鏈固定 3 段（kimi 下架後
    codex/codex-pool/claude，順序由碼層常態或 runtime override 決定）；若未來變鏈長，unpack 會明確炸
    → 提醒同步更新測試，不靜默誤判。"""
    chain = list(lp.resolve_dispatch('audit').chain)
    assert len(chain) == 3, f'測試假設 3-provider 鏈，得到 {chain}'
    return chain


def _patch(results):
    """results: {provider: (rc, reason)}。回 (calls 累積串, restore 函式)。每次重置 exhausted 共享集。"""
    calls = []
    orig = pt._run_one
    with pt._exhausted_lock:
        pt._exhausted_providers.clear()

    def fake(provider, todo_verb, slug, prompt, spec):
        calls.append(provider)
        return results.get(provider, (0, None))
    pt._run_one = fake
    return calls, (lambda: setattr(pt, '_run_one', orig))


def test_outage_fails_over():
    # 鏈首中斷（outage）→ 落鏈上第二個，回第二個的 rc=0、不再試第三個
    p0, p1, _p2 = _chain()
    calls, restore = _patch({p0: (1, 'outage'), p1: (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0, rc
        assert calls == [p0, p1], calls
        print(f'✓ outage：{p0} 中斷 → failover 到 {p1}、止於首個成功')
    finally:
        restore()


def test_limit_fails_over():
    p0, p1, p2 = _chain()
    calls, restore = _patch({p0: (1, 'limit'), p1: (1, 'limit'), p2: (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0 and calls == [p0, p1, p2], (rc, calls)
        print(f'✓ limit：逐個撞額度 → 一路 failover 到 {p2}(保底)')
    finally:
        restore()


def test_task_failure_does_not_fail_over():
    # reason=None（agent 真跑了卻 rc≠0）→ 不換 provider，rc 直接回呼叫端
    p0, _p1, _p2 = _chain()
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


if __name__ == '__main__':
    test_outage_fails_over()
    test_limit_fails_over()
    test_task_failure_does_not_fail_over()
    test_all_unavailable_defers()
    test_outage_classification()
    print('\n全部通過 ✅')
