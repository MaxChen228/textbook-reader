"""crawl 購物清單（buffer）機制單元測試：uv run python -m book_pipeline.test_crawl_queue

涵蓋 producer-buffer-consumer 解耦的正確性：
  買書員 drain（確定性，劃掉/失敗計數/達上限丟棄/進場去重/額度0保留/pipeline 滿不抓）、
  crawl 小弟 refill（merge 去重/wishlist 枯竭進冷卻防 churn）、_drain_due / _refill_due。
這是「劃掉=確定性、agent 只補貨」架構的地基——drain 與 agent 徹底解耦，故測得硬一點。"""

import json
import os
import tempfile

from book_pipeline import pipeline_tick as pt


def _setup():
    d = tempfile.mkdtemp(prefix='crawlq_')
    pt.CRAWL_QUEUE = os.path.join(d, 'crawl_queue.json')
    pt.CRAWL_PLAN = os.path.join(d, 'crawl_plan.json')
    pt.CRAWL_REFILL_FORCE = os.path.join(d, 'crawl_refill_request')
    pt.CONTROLLER_STATE = os.path.join(d, '.controller.json')
    pt.RELOAD_REQUEST = os.path.join(d, 'reload_request')
    pt.log = lambda *a, **k: None
    pt.hist.set_touched = lambda *a, **k: None
    from book_pipeline import devctl  # drain 內 import → 直接 stub 該函式
    devctl.invalidate_zlib_cache = lambda *a, **k: None
    return d


def _write_queue(books, exhausted_at=None):
    json.dump({'books': books, 'count': len(books), 'reason': '', 'refill_exhausted_at': exhausted_at},
              open(pt.CRAWL_QUEUE, 'w'))


def _read_queue():
    return json.load(open(pt.CRAWL_QUEUE))['books']


def _q(slug, fails=0):
    return {'slug': slug, 'id': '1', 'hash': 'a', 'title': slug, 'fails': fails}


def test_drain_crosses_off_fetched():
    _setup()
    _write_queue([_q('a'), _q('b'), _q('c')])
    pt._have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: 0                       # room=CRAWL_HIGH
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 5}]
    pt._fetch_book = lambda b: b['slug'] if b['slug'] in ('a', 'c') else None  # a,c 成功、b 失敗
    crawled = pt.drain_crawl_queue([], dry=False)
    assert set(crawled) == {'a', 'c'}, crawled
    q = _read_queue()
    assert [b['slug'] for b in q] == ['b'], q               # a,c 劃掉、b 保留
    assert q[0]['fails'] == 1                                # b 失敗計數 +1
    print('✓ drain：成功的劃掉、失敗的留下且 fails+1（確定性，無 LLM）')


def test_drain_drops_after_max_fails():
    _setup()
    _write_queue([_q('a', fails=pt.MAX_FETCH_FAILS - 1)])
    pt._have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 5}]
    pt._fetch_book = lambda b: None                          # 再失敗一次 → 達上限
    pt.drain_crawl_queue([], dry=False)
    assert _read_queue() == [], _read_queue()                # 達 MAX_FETCH_FAILS → 移出清單
    print('✓ drain：連續失敗達上限 → 移出清單（待 refill 補替代）')


def test_drain_dedup_on_load():
    _setup()
    _write_queue([_q('have1'), _q('new1')])
    pt._have_slugs = lambda: {'have1'}                       # have1 已存在 inventory
    pt._crawl_backlog = lambda rows: 99                      # room=0 → 本輪不抓，只測進場去重存回
    pt.drain_crawl_queue([], dry=False)
    assert [b['slug'] for b in _read_queue()] == ['new1']
    print('✓ drain：進場去重劃掉已存在書（即使本輪不抓也存回）')


def test_drain_quota_zero_preserves():
    _setup()
    _write_queue([_q('a'), _q('b')])
    pt._have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_accounts_remaining = lambda: [{'account': 0, 'remaining': 0}]  # 額度0
    def _no_fetch(b): raise AssertionError('額度0 不該呼叫 _fetch_book')
    pt._fetch_book = _no_fetch
    crawled = pt.drain_crawl_queue([], dry=False)
    assert crawled == []
    assert [b['slug'] for b in _read_queue()] == ['a', 'b']  # 原封保留待明日
    print('✓ drain：額度0 → 不抓、清單原封保留（不 latch）')


def test_drain_pipeline_full_holds():
    _setup()
    _write_queue([_q('a')])
    pt._have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: pt.CRAWL_HIGH           # room=0
    def _no_quota(): raise AssertionError('room=0 不該查額度')
    pt._zlib_accounts_remaining = _no_quota
    crawled = pt.drain_crawl_queue([], dry=False)
    assert crawled == []
    assert [b['slug'] for b in _read_queue()] == ['a']       # 清單保留待消化
    print('✓ drain：pipeline 滿 → 連額度都不查、不抓（純 backpressure）')


def test_due_predicates():
    _setup()
    pt._have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: 0
    pt._zlib_remaining_cached = lambda: 5
    _write_queue([])                                         # 清單空
    assert pt._drain_due([]) is False                        # 無貨可抓
    assert pt._refill_due() is True                          # < 水位 → 該補
    _write_queue([_q(f's{i}') for i in range(pt.CRAWL_LOW + 1)])  # 清單滿
    assert pt._refill_due() is False                         # ≥ 水位 → 不補
    assert pt._drain_due([]) is True                         # 有貨+有額度+有空間 → 該買
    pt._zlib_remaining_cached = lambda: 0                    # 額度快取 0
    assert pt._drain_due([]) is False                        # → 不買（可 idle 收斂）
    print('✓ due：drain/refill 各自正確（清單長度 × 額度 × 水位，互不耦合）')


def test_refill_cooldown_blocks_churn():
    _setup()
    pt._have_slugs = lambda: set()
    _write_queue([], exhausted_at=pt.time.time())            # 剛進冷卻
    assert pt._refill_due() is False                         # 冷卻中 → 不再叫 agent（防 churn）
    _write_queue([], exhausted_at=pt.time.time() - pt.CRAWL_REFILL_COOLDOWN_S - 1)  # 過期
    assert pt._refill_due() is True
    print('✓ refill：wishlist 枯竭冷卻中不重派、過期才重試（無收斂迴圈病的解藥）')


def test_refill_merge_dedup():
    _setup()
    _write_queue([_q('queued1')])
    pt.booklists.have_slugs = lambda: {'owned1'}
    pt.booklists.has_unresolved = lambda *a, **k: False
    # 模擬書單 select_next 回（已排隊/已有/非法/新書各一）→ daemon 端 merge 只收新書
    pt.booklists.select_next = lambda n, *a, **k: [
        {'slug': 'queued1', 'id': '9', 'hash': 'z', 'title': 'x'},
        {'slug': 'owned1', 'id': '9', 'hash': 'z', 'title': 'x'},
        {'slug': 'Bad Slug', 'id': '9', 'hash': 'z', 'title': 'x'},
        {'slug': 'good1', 'id': '9', 'hash': 'z', 'title': 'x'}]
    added = pt.refill_crawl_queue(dry=False)
    assert added == 1, added
    assert sorted(b['slug'] for b in _read_queue()) == ['good1', 'queued1']
    print('✓ refill：merge 去重（清單已有/inventory已有/非法 slug 全擋，只新書進）')


def test_refill_exhaust_sets_cooldown():
    _setup()
    _write_queue([])
    pt.booklists.have_slugs = lambda: set()
    pt.booklists.select_next = lambda n, *a, **k: []        # 無 ready 可補
    pt.booklists.has_unresolved = lambda *a, **k: False     # 也無 unresolved → 不跑 resolver
    added = pt.refill_crawl_queue(dry=False)
    assert added == 0
    assert json.load(open(pt.CRAWL_QUEUE))['refill_exhausted_at'] is not None  # 進冷卻
    print('✓ refill：書單補不出 ready（剩 review/absent）→ 寫冷卻時戳（下次 _refill_due 收斂）')


def test_force_marker_roundtrip():
    _setup()
    assert pt._refill_force_pending() is False        # 起初無請求
    pt.request_refill()
    assert pt._refill_force_pending() is True          # 丟一張 → 待消費
    pt._clear_refill_force()
    assert pt._refill_force_pending() is False          # 消費後清掉
    print('✓ force marker：request/peek/clear 三態正確（CLI→daemon 的單向 signal）')


def test_force_refill_bypasses_watermark():
    _setup()
    _write_queue([_q(f's{i}') for i in range(pt.CRAWL_LOW)])  # 清單滿水位 → _refill_due False
    pt._have_slugs = lambda: set()
    pt.booklists.have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: pt.CRAWL_HIGH           # room=0 → 買書員 hold（純測 refill 由 marker 觸發）
    pt.booklists.has_unresolved = lambda *a, **k: False
    pt.booklists.select_next = lambda n, *a, **k: [{'slug': 'forced1', 'id': '9', 'hash': 'z', 'title': 'x'}]
    assert pt._refill_due() is False                         # 水位之上：正常不會補
    pt.request_refill()                                       # 手動強制
    pt.do_crawl_tick(dry=False, rows=[])
    assert 'forced1' in [b['slug'] for b in _read_queue()]    # 無視水位仍補進新書
    assert pt._refill_force_pending() is False                # marker 消費（恰一次）
    print('✓ force refill：無視水位/冷卻補貨 + 消費 marker（手動觸發語意）')


def test_force_refill_skip_when_full():
    _setup()
    _write_queue([_q(f's{i}') for i in range(pt.CRAWL_HIGH)])  # 清單已滿 HIGH → want<=0
    pt._have_slugs = lambda: set()
    pt.booklists.have_slugs = lambda: set()
    pt._crawl_backlog = lambda rows: pt.CRAWL_HIGH
    def _no_select(*a, **k): raise AssertionError('清單已滿不該 select_next 找 0 本')
    pt.booklists.select_next = _no_select
    pt.request_refill()
    pt.do_crawl_tick(dry=False, rows=[])                       # 不該拋（want<=0 守衛擋下）
    assert len(_read_queue()) == pt.CRAWL_HIGH                 # 清單原封
    assert pt._refill_force_pending() is False                # 請求仍消費（已盡力、別重試空轉）
    print('✓ force refill：清單已滿 → 跳過補貨但仍消費請求（不空叫 select_next 找 0 本）')


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
    test_drain_crosses_off_fetched()
    test_drain_drops_after_max_fails()
    test_drain_dedup_on_load()
    test_drain_quota_zero_preserves()
    test_drain_pipeline_full_holds()
    test_due_predicates()
    test_refill_cooldown_blocks_churn()
    test_refill_merge_dedup()
    test_refill_exhaust_sets_cooldown()
    test_force_marker_roundtrip()
    test_force_refill_bypasses_watermark()
    test_force_refill_skip_when_full()
    test_controller_state_roundtrip()
    test_reload_marker_roundtrip()
    test_wake_controller_sends_signal()
    print('\n全部通過 ✅')
