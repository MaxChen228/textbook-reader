"""crawl 買書員（無 buffer，直讀解析池）單元測試：uv run python -m book_pipeline.test_crawl_queue

涵蓋 2026-06 簡化後的下載路徑：買書員 drain 每 tick 直接 booklists.select_next 取解析池 ready 並行抓，
下載失敗計數持久在 pipeline_state（q.crawl_fail_*）、達上限即 exclude 出候選（不卡隊頭）。額度0/pipeline
滿/無 ready → 不抓。另含 controller-state / reload-marker / wake 信號的端到端。無購物清單 buffer、無 refill。"""

import json
import os
import tempfile

from book_pipeline import pipeline_tick as pt
from book_pipeline import pipeline_queue as q

# 這些測試直接改共享模組全域（非 monkeypatch fixture）——teardown 還原，避免污染其他測試檔（pytest 同
# process）。最關鍵是 booklists.select_next：被換成回傳假 'a'/'b' 的 stub，若不還原會讓後續任何呼叫真
# select_next 的測試（如 test_restock_workflow）拿到假資料而誤判。
_ORIG = {k: getattr(pt, k, None) for k in (
    'log', '_crawl_backlog', '_zlib_accounts_remaining', '_fetch_book',
    '_zlib_remaining_cached', 'CONTROLLER_STATE', 'RELOAD_REQUEST')}
_ORIG['select_next'] = pt.booklists.select_next
_ORIG['set_touched'] = pt.hist.set_touched


def teardown_function(function):
    pt.booklists.select_next = _ORIG['select_next']
    pt.hist.set_touched = _ORIG['set_touched']
    for k in ('log', '_crawl_backlog', '_zlib_accounts_remaining', '_fetch_book',
              '_zlib_remaining_cached', 'CONTROLLER_STATE', 'RELOAD_REQUEST'):
        if _ORIG[k] is not None:
            setattr(pt, k, _ORIG[k])


def _setup():
    d = tempfile.mkdtemp(prefix='crawlq_')
    # 失敗計數存 pipeline_state → 導向 temp，hermetic（不碰真實 state）
    q.STATE_PATH = os.path.join(d, 'pipeline_state.json')
    q.STATE_LOCK = os.path.join(d, 'pipeline_state.lock')
    pt.CONTROLLER_STATE = os.path.join(d, '.controller.json')
    pt.RELOAD_REQUEST = os.path.join(d, 'reload_request')
    pt.log = lambda *a, **k: None
    pt.hist.set_touched = lambda *a, **k: None
    from book_pipeline import devctl  # drain 內 import → 直接 stub 該函式
    devctl.invalidate_zlib_cache = lambda *a, **k: None
    return d


def _b(slug):
    return {'slug': slug, 'id': '1', 'hash': 'a', 'title': slug}


def _stub_pool(books):
    """stub select_next：回 books 中不在 exclude 的前 n 本（鏡像真實語意）。"""
    def _sn(n, *a, exclude=None, **k):
        ex = exclude or set()
        return [b for b in books if b['slug'] not in ex][:n]
    pt.booklists.select_next = _sn


def test_drain_fetches_pool_and_counts_fails():
    _setup()
    _stub_pool([_b('a'), _b('b'), _b('c')])
    pt._crawl_backlog = lambda rows: 0                       # room=CAP
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 5}]
    pt._fetch_book = lambda b: b['slug'] if b['slug'] in ('a', 'c') else None  # a,c 成功、b 失敗
    crawled = pt.drain_crawl_queue([], dry=False)
    assert set(crawled) == {'a', 'c'}, crawled
    assert q.crawl_fail_count('b') == 1                       # b 失敗 +1（持久）
    assert q.crawl_fail_count('a') == 0 and q.crawl_fail_count('c') == 0  # 成功者無計數
    print('✓ drain：直接取解析池抓、成功回 slug、失敗 q.crawl_fail+1（無 buffer、無 LLM）')


def test_drain_blocks_after_max_fails():
    _setup()
    _stub_pool([_b('a')])
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 5}]
    pt._fetch_book = lambda b: None                          # 持續失敗
    for _ in range(pt.MAX_FETCH_FAILS):
        pt.drain_crawl_queue([], dry=False)
    assert q.crawl_fail_count('a') >= pt.MAX_FETCH_FAILS
    assert 'a' in q.crawl_blocked_slugs(pt.MAX_FETCH_FAILS)   # 達上限 → 排除出候選
    # 下一輪 select_next 收到 exclude={a} → 無候選 → 不再抓（不卡隊頭）
    assert pt.drain_crawl_queue([], dry=False) == []
    print('✓ drain：連續失敗達上限 → q.crawl_blocked 排除、不再無限重試卡隊頭')


def test_drain_clears_fail_on_success():
    _setup()
    q.bump_crawl_fail('a'); q.bump_crawl_fail('a')           # 預置 2 次失敗
    _stub_pool([_b('a')])
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 5}]
    pt._fetch_book = lambda b: b['slug']                     # 這次成功
    pt.drain_crawl_queue([], dry=False)
    assert q.crawl_fail_count('a') == 0                       # 成功 → 清失敗計數（下次不被誤排除）
    print('✓ drain：抓成功 → 清除累積失敗計數（源頭好轉自癒）')


def test_drain_quota_zero_no_fetch():
    _setup()
    _stub_pool([_b('a'), _b('b')])
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 0}]  # 額度0
    def _no_fetch(b): raise AssertionError('額度0 不該呼叫 _fetch_book')
    pt._fetch_book = _no_fetch
    assert pt.drain_crawl_queue([], dry=False) == []
    print('✓ drain：額度0 → 不抓、不 latch（下輪自動重探）')


def test_drain_pipeline_full_holds():
    _setup()
    _stub_pool([_b('a')])
    pt._crawl_backlog = lambda rows: pt.CRAWL_INFLIGHT_CAP    # room=0
    def _no_quota(): raise AssertionError('room=0 不該查額度')
    pt._zlib_accounts_remaining = _no_quota
    assert pt.drain_crawl_queue([], dry=False) == []
    print('✓ drain：pipeline 滿 → 連額度都不查、不抓（純 backpressure）')


def test_drain_due_predicate():
    _setup()
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_remaining_cached = lambda: 5
    _stub_pool([])                                           # 解析池無 ready
    assert pt._drain_due([]) is False                        # 無 ready 可抓
    _stub_pool([_b('a')])
    assert pt._drain_due([]) is True                         # 有 ready+有額度+有空間 → 該買
    pt._zlib_remaining_cached = lambda: 0                    # 額度快取 0
    assert pt._drain_due([]) is False                        # → 不買（可 idle 收斂）
    pt._zlib_remaining_cached = lambda: 5
    pt._crawl_backlog = lambda rows: pt.CRAWL_INFLIGHT_CAP   # pipeline 滿
    assert pt._drain_due([]) is False
    print('✓ drain_due：解析池有 ready × 額度 × pipeline 空間，三者皆備才 due')


def test_controller_state_roundtrip():
    _setup()
    assert pt.controller_info() is None              # 無 statefile
    assert pt.controller_pid() is None
    assert pt.wake_controller() is False             # 無 controller → 不送、回 False（呼叫端改 kick）
    pt._write_controller_state()                      # 寫本進程 pid + sha（活著）
    info = pt.controller_info()
    assert info and info['pid'] == os.getpid() and 'sha' in info  # 含版本 → 觀測「跑哪版碼」
    assert pt.controller_pid() == os.getpid()
    json.dump({'pid': 999999, 'sha': 'dead'}, open(pt.CONTROLLER_STATE, 'w'))  # 必死 pid → 探活 None
    assert pt.controller_info() is None
    pt._clear_controller_state()
    assert pt.controller_info() is None
    print('✓ controller state：寫/探活/sha/死pid/清 正確（版本觀測 + 喚醒定址）')


def test_reload_marker_roundtrip():
    _setup()
    assert pt._reload_pending() is False
    pt.request_reload()
    assert pt._reload_pending() is True               # 丟請求 → loop 下個 observe 認它優雅退出
    pt._clear_reload()
    assert pt._reload_pending() is False
    print('✓ reload marker：request/peek/clear 三態正確（優雅 reload 信號）')


def test_wake_controller_sends_signal():
    import signal as _sig
    import time as _t
    _setup()
    fired = []
    old = _sig.signal(_sig.SIGUSR1, lambda *a: fired.append(1))
    try:
        pt._write_controller_state()                  # statefile 指向本進程
        assert pt.wake_controller() is True           # 送 SIGUSR1 給自己
        _t.sleep(0.05)                                 # 讓 handler 跑
        assert fired, 'SIGUSR1 應觸發 handler（= reactive loop 的 wake.set）'
    finally:
        _sig.signal(_sig.SIGUSR1, old)
        pt._clear_controller_state()
    print('✓ wake_controller：os.kill SIGUSR1 端到端送達 controller（立即喚醒，不殺在飛 worker）')


if __name__ == '__main__':
    test_drain_fetches_pool_and_counts_fails()
    test_drain_blocks_after_max_fails()
    test_drain_clears_fail_on_success()
    test_drain_quota_zero_no_fetch()
    test_drain_pipeline_full_holds()
    test_drain_due_predicate()
    test_controller_state_roundtrip()
    test_reload_marker_roundtrip()
    test_wake_controller_sends_signal()
    print('\n全部通過 ✅')
