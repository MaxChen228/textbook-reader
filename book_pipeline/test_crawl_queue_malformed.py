"""crawl 購物清單畸形輸入韌性測試：uv run python -m book_pipeline.test_crawl_queue_malformed

守的契約邊界 = 「producer ↔ 確定性 merge gate（_merge_plan_into_queue）」。_merge 把一批選好的書
append 進持久購物清單、daemon 端權威去重，是清單的**唯一寫入閘**。cc1be2e 起 refill 餵入的是
確定性 booklists.select_next 結果（非 LLM），但 _merge 仍是**防禦邊界**：canon resolver（書名→
z-lib id/hash）回傳變異、或未來再有 producer 餵入時，畸形輸入**絕不得 crash 或靜默損毀清單**。
後果不是「少一本書」，而是：
  ① 畸形若拋例外 → 上層 try/except 吞掉整批選書、且不寫冷卻時戳 → _refill_due 永遠 True → churn；
  ② 非純量 id 被 str() 成字面 "['123']" 入清單 → 流進 fetch URL → 404 → 累積 fails → 靜默丟書，真因永不浮現。

本檔釘住兩類長存防護：
  1. _merge 的型別守衛（畸形 plan/books/條目/id-hash → graceful skip 或視同空，合法照收）。
  2. 確定性 refill 的 churn 止血（補不出 ready → 寫 refill_exhausted_at 冷卻時戳 → _refill_due False）。
全 hermetic：重導 module 全域路徑到 tmp、stub booklists，絕不碰真實購物清單/書單/狀態檔。
"""

import os
import tempfile

from book_pipeline import pipeline_tick as pt


# ─────────────────────────────────────────────────────────────────────────────
# _merge 對 None / 空計畫 fail-safe（_read_crawl_plan 在 planner 遺物清除後已移除，故直接測 _merge）
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_none_and_empty_plan_noop():
    """fail-safe 回歸鎖：上游給 None（讀檔失敗/無計畫）或空計畫 → _merge 回 0、不污染清單、不冒泡。
    沒這條，None 流進 `plan.get` 會 AttributeError 把整輪 refill 打掛。傳真實清單進去並驗其分毫未動。"""
    q = [{'slug': 'kept', 'id': '1', 'hash': 'a'}]
    assert pt._merge_plan_into_queue(q, None, set()) == 0, 'None 計畫須視同補 0 本、不 crash'
    assert pt._merge_plan_into_queue(q, {}, set()) == 0, '空 dict 計畫同視同補 0 本'
    assert pt._merge_plan_into_queue(q, {'books': []}, set()) == 0, '空 books 補 0 本'
    assert q == [{'slug': 'kept', 'id': '1', 'hash': 'a'}], '空/None 計畫不得改動既有清單'
    print('✓ _merge 對 None/空計畫 fail-safe：回 0、清單不動（不冒泡 AttributeError）')


# ─────────────────────────────────────────────────────────────────────────────
# _merge_plan_into_queue：型別守衛（畸形 graceful、合法照收）。正向對照鎖死「不誤殺合法」。
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_int_id_normalized_and_accepted():
    """正向對照：id/hash 常是 JSON number（無引號）。純量 int 經 str() 還原成正確字串後收入 ——
    合法輸入，必須收。與 test_merge_plan_nonscalar_id_hash_rejected 成對：純量 OK、容器才該擋。
    整體 dump 全欄位 → 釘住正規化只動 id/hash、不誤改 slug/title、fails 一律初始化 0。"""
    q = []
    added = pt._merge_plan_into_queue(
        q, {'books': [{'slug': 'good_int', 'id': 123, 'hash': 456, 'title': 'T'}]}, set())
    assert added == 1, 'int id/hash 是合法輸入，須收入清單'
    assert q[0] == {'slug': 'good_int', 'id': '123', 'hash': '456', 'title': 'T', 'fails': 0}, \
        'int id/hash 須各自 str() 正規化、slug/title 原樣、fails 初始 0（全欄位釘死防漂移）'
    assert isinstance(q[0]['id'], str) and isinstance(q[0]['hash'], str), \
        'id/hash 入清單後型別必為 str（下游 fetch 拼 URL 直接用，留 int 會 crash 子程序）'
    print('✓ int id/hash 各自正規化成 "123"/"456" 並收入（合法純量對照，全欄位釘死）')


def test_merge_wellformed_dedup_unchanged():
    """正向對照：合法 plan 的 happy path 不被任何守衛改寫——已在清單／inventory 的 slug 去重、
    slug 不合法（非 [a-z0-9_]{1,64}）拒收、缺 id/hash 跳過。確保加守衛不誤殺合法輸入。"""
    q = [{'slug': 'already_queued'}]
    plan = {'books': [
        {'slug': 'already_queued', 'id': '1', 'hash': 'a'},  # 已在清單 → 去重
        {'slug': 'in_inventory', 'id': '2', 'hash': 'b'},    # 已有書 → 去重（have）
        {'slug': 'Bad Slug!', 'id': '3', 'hash': 'c'},        # 非法 slug → 拒
        {'slug': 'no_hash', 'id': '4'},                       # 缺 hash → 跳
        {'slug': 'fresh_ok', 'id': '5', 'hash': 'e', 'title': 'T'},  # 唯一合法新書
    ]}
    added = pt._merge_plan_into_queue(q, plan, {'in_inventory'})
    assert added == 1, '只有 fresh_ok 該入'
    slugs = {b['slug'] for b in q}
    assert 'fresh_ok' in slugs and len(q) == 2
    nb = next(b for b in q if b['slug'] == 'fresh_ok')
    assert nb == {'slug': 'fresh_ok', 'id': '5', 'hash': 'e', 'title': 'T', 'fails': 0}
    print('✓ 合法 plan happy path：去重/非法 slug/缺欄位正確處理，唯一新書收入')


def test_merge_plan_list_toplevel_graceful():
    """畸形：producer 漏 {'books':[...]} 外層、直接吐裸陣列 plan=[...]。
    無守衛時 `plan.get('books')` → AttributeError: 'list' object has no attribute 'get'。
    契約：non-dict plan 視同空計畫回 0、不拋例外 → 讓 refill 正常走「補 0 本→進冷卻」分支。"""
    plan = [{'slug': 'x', 'id': '1', 'hash': 'a'}]  # 裸陣列：漏掉 {'books':...} 外層
    q = []
    added = pt._merge_plan_into_queue(q, plan, set())
    assert added == 0, 'non-dict plan 須視同空計畫回 0'
    assert q == [], '畸形 plan 不得污染清單'
    print('✓ 韌性：裸陣列 plan（漏 {books:..} 外層）視同空計畫回 0，不 AttributeError')


def test_merge_plan_str_entries_skip():
    """畸形：books 條目為裸 str（producer 只列 slug 名）；或 books 整個是 str。
    無守衛時 `b.get('slug')` 對 str → AttributeError；books='abc' 則迭代字元逐個 .get crash。
    契約：每個非 dict 條目 skip+continue（單一畸形不連坐整批）；books 非 list 視同空。
    混合 [str, 合法dict] → 合法那本仍須收入（added==1）。"""
    # (a) books 條目混入裸 str：壞的跳過、好的照收
    q = []
    added = pt._merge_plan_into_queue(
        q, {'books': ['just_slug', {'slug': 'real_one', 'id': '9', 'hash': 'z'}]}, set())
    assert added == 1, '畸形 str 條目跳過、合法 dict 條目仍須收（不連坐）'
    assert [b['slug'] for b in q] == ['real_one']
    # (b) books 整個是 str → 視同空、回 0、不 crash
    q2 = []
    assert pt._merge_plan_into_queue(q2, {'books': 'abc'}, set()) == 0
    assert q2 == []
    print('✓ 韌性：books 裸 str 條目跳過、合法 dict 仍收（不連坐）；books 整個是 str 視同空')


def test_merge_plan_nonscalar_id_hash_rejected():
    """畸形（靜默損毀）: id 被包成陣列 {'id':['123']}（truthy 過了 `b.get('id')` 的存在檢查），
    `str(b['id'])` → 字面字串 "['123']" 入清單 → 流進 fetch URL → 404 → 累積 fails → 靜默丟書，
    真因（id 型別錯）永不浮現。契約：id/hash 強制 isinstance(.,(str,int))，list/dict 視為無效 skip。
    此條目須 added==0、不入 queue。（int id 的正向對照已在 test_merge_int_id_normalized_and_accepted）"""
    q = []
    added = pt._merge_plan_into_queue(
        q, {'books': [{'slug': 'good1', 'id': ['123'], 'hash': 'ab'}]}, set())
    assert added == 0, "list id 是無效型別，須 skip（不得 str() 成字面 \"['123']\" 入清單）"
    assert q == [], '畸形 id 不得污染清單（否則靜默損毀流進 fetch URL）'
    print('✓ 韌性：非純量 id（list/dict）skip，不 str() 成字面值靜默損毀流進 fetch URL')


# ─────────────────────────────────────────────────────────────────────────────
# 確定性 refill（cc1be2e，零 LLM）：補不出 ready → 寫冷卻時戳 → _refill_due False（churn 止血）
# ─────────────────────────────────────────────────────────────────────────────

def test_refill_no_ready_converges_to_cooldown():
    """churn 止血：書單暫無可補的 ready（剩 review/absent 或待解析）時，refill 須補 0 本並寫
    refill_exhausted_at 冷卻時戳 → _refill_due 後續 False，不每個 observe cycle 空轉重跑。
    沒這條，低水位會讓 reactive loop 每 cycle 重跑 refill（雖無 LLM，仍是無謂 churn + 可能反覆起
    resolver 子進程）。釘住確定性 refill 的收斂契約。

    hermetic：重導 CRAWL_QUEUE/LOG 到 tmp（檔缺 → _load_queue_full 種子成空清單），stub
    booklists.select_next→[]（無 ready）、has_unresolved→False（無待解析、不觸發 resolver 子進程）、
    have_slugs→set()，及 pt._have_slugs→set()（_refill_due 用）。絕不碰真實購物清單/書單/狀態檔。"""
    d = tempfile.mkdtemp(prefix='refill_cooldown_')
    saved = {k: getattr(pt, k) for k in ('CRAWL_QUEUE', 'LOG', '_have_slugs')}
    bl = pt.booklists
    bl_saved = {k: getattr(bl, k) for k in ('select_next', 'has_unresolved', 'have_slugs')}
    try:
        pt.CRAWL_QUEUE = os.path.join(d, 'crawl_queue.json')   # 檔缺 → 清單空 → want>0 真進補貨
        pt.LOG = os.path.join(d, 'daemon.log')
        pt._have_slugs = lambda: set()
        bl.have_slugs = lambda *a, **k: set()
        bl.select_next = lambda *a, **k: []                    # 無 ready 可補
        bl.has_unresolved = lambda *a, **k: False              # 無待解析 → 不跑 resolver

        added = pt.refill_crawl_queue(dry=False)
        assert added == 0, '無 ready 須補 0 本'
        # 契約核心：冷卻時戳被寫 → _refill_due 後續 False → churn 止血
        full = pt._load_queue_full()
        assert full.get('refill_exhausted_at') is not None, \
            '補不出 ready 須寫 refill_exhausted_at 冷卻時戳（否則低水位每 cycle 空轉 refill churn）'
        assert pt._refill_due() is False, '冷卻中 _refill_due 須為 False（停 churn）'
    finally:
        for k, v in saved.items():
            setattr(pt, k, v)
        for k, v in bl_saved.items():
            setattr(bl, k, v)
    print('✓ churn 止血：無 ready → refill 補 0 本 → 寫冷卻時戳 → _refill_due False（不空轉重跑）')


if __name__ == '__main__':
    test_merge_none_and_empty_plan_noop()
    test_merge_int_id_normalized_and_accepted()
    test_merge_wellformed_dedup_unchanged()
    test_merge_plan_list_toplevel_graceful()
    test_merge_plan_str_entries_skip()
    test_merge_plan_nonscalar_id_hash_rejected()
    test_refill_no_ready_converges_to_cooldown()
    print('\n全部通過 ✅')
