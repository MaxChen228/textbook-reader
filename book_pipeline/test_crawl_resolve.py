"""庫存查證 agent 派工水位：uv run python -m book_pipeline.test_crawl_resolve

_crawl_resolve_due（合格池 qualified_ready < CRAWL_POOL_LOW ∧ 庫存母體 candidate+pending > 0 才派；
discovery 預設關閉 → 母體枯竭即 latch）+ do_crawl_resolve（due 才派一隻、無 batch、label __restock__、
不打網路、不真派 agent）。monkeypatch pool_counts/dispatch_llm（try/finally 還原）。"""

from book_pipeline import pipeline_tick as pt
from book_pipeline import booklists as bl


def _pc(qualified_ready, candidate, pending=0):
    """鏡像新 pool_counts 形狀（五態 + 向後相容鍵）。"""
    return {'qualified_ready': qualified_ready, 'confirmed': qualified_ready, 'ready': qualified_ready,
            'candidate': candidate, 'unresolved': candidate, 'pending': pending,
            'owned': 0, 'rejected': 0, 'absent': 0, 'not_found': 0, 'review': 0,
            'version_unavailable': 0}


def test_due():
    o = bl.pool_counts
    try:
        bl.pool_counts = lambda *a, **k: _pc(50, 10)
        assert pt._crawl_resolve_due()[0] is True         # 合格池低 + 有 candidate → 派
        bl.pool_counts = lambda *a, **k: _pc(50, 0, pending=8)
        assert pt._crawl_resolve_due()[0] is True         # 合格池低 + 有 pending（存量回查）→ 派
        bl.pool_counts = lambda *a, **k: _pc(100, 10)
        assert pt._crawl_resolve_due()[0] is False        # 合格池達水位 → 不派
        bl.pool_counts = lambda *a, **k: _pc(50, 0, pending=0)
        assert pt._crawl_resolve_due()[0] is False        # 母體枯竭（candidate+pending=0）→ latch、不派
        print('✓ _crawl_resolve_due：合格池<水位 ∧ (candidate∪pending)>0 才派；母體枯竭→latch（discovery 關）')
    finally:
        bl.pool_counts = o


def test_dispatch_one_agent_no_batch():
    o1, o2 = bl.pool_counts, pt.dispatch_llm
    cap = []
    try:
        bl.pool_counts = lambda *a, **k: _pc(30, 50, pending=12)
        pt.dispatch_llm = lambda verb, slug, dry, label=None: cap.append((verb, slug, dry, label)) or 0
        rc = pt.do_crawl_resolve(dry=False)
        assert rc == 0 and len(cap) == 1, cap
        verb, slug, dry, label = cap[0]
        assert verb == 'crawl' and dry is False
        assert slug == '', repr(slug)                     # 無 batch slug（agent 自查工作母體）
        assert label == '__restock__', label              # lease/registry 穩定 singleton 鍵
        print('✓ do_crawl_resolve：派 1 隻庫存查證 agent、無 batch、label __restock__')
    finally:
        bl.pool_counts, pt.dispatch_llm = o1, o2


def test_not_due_no_dispatch():
    o1, o2 = bl.pool_counts, pt.dispatch_llm
    cap = []
    try:
        pt.dispatch_llm = lambda *a, **k: cap.append(a) or 0
        bl.pool_counts = lambda *a, **k: _pc(120, 50)      # 合格池滿
        assert pt.do_crawl_resolve(dry=False) == 0 and cap == []
        bl.pool_counts = lambda *a, **k: _pc(30, 0, pending=0)  # 母體枯竭 latch
        assert pt.do_crawl_resolve(dry=False) == 0 and cap == []
        print('✓ do_crawl_resolve：池滿 / 母體枯竭 → 不派 agent')
    finally:
        bl.pool_counts, pt.dispatch_llm = o1, o2


if __name__ == '__main__':
    test_due()
    test_dispatch_one_agent_no_batch()
    test_not_due_no_dispatch()
    print('\n全部通過 ✅')
