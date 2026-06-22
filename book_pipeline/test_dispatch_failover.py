"""dispatch_llm provider failover：uv run python -m book_pipeline.test_dispatch_failover

驗證 failover 涵蓋「服務中斷」而非僅「額度」（RC2，2026-06-18）：codex-pool 掛掉（outage）→ 自動
落鏈上下一 provider；額度（limit）亦然；但「agent 真跑了卻任務失敗」(reason=None) 不換 provider。
monkeypatch _run_one 模擬各 provider 結果，不起子進程、不打網路。"""

from book_pipeline import pipeline_tick as pt


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
    # codex-pool 中斷 → 落 codex（OAuth），回 codex 的 rc=0、不再試 claude
    calls, restore = _patch({'codex-pool': (1, 'outage'), 'codex': (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0, rc
        assert calls == ['codex-pool', 'codex'], calls
        print('✓ outage：codex-pool 中斷 → failover 到 codex(OAuth)、止於首個成功')
    finally:
        restore()


def test_limit_fails_over():
    calls, restore = _patch({'codex-pool': (1, 'limit'), 'codex': (1, 'limit'),
                             'claude': (0, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 0 and calls == ['codex-pool', 'codex', 'claude'], (rc, calls)
        print('✓ limit：逐個撞額度 → 一路 failover 到 claude(Max 保底)')
    finally:
        restore()


def test_task_failure_does_not_fail_over():
    # reason=None（agent 真跑了卻 rc≠0）→ 不換 provider，rc 直接回呼叫端
    calls, restore = _patch({'codex-pool': (2, None)})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == 2 and calls == ['codex-pool'], (rc, calls)
        print('✓ task-failure：有事件卻失敗 → 不 failover（換 provider 無益、防雙寫）')
    finally:
        restore()


def test_all_unavailable_defers():
    calls, restore = _patch({'codex-pool': (1, 'outage'), 'codex': (1, 'outage'),
                             'claude': (1, 'outage')})
    try:
        rc = pt.dispatch_llm('audit', 'x', dry=False)
        assert rc == -2, rc                               # 全鏈耗盡 → defer
        assert calls == ['codex-pool', 'codex', 'claude'], calls
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
