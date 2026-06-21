"""庫存查證 agent 派工水位：uv run python -m book_pipeline.test_crawl_resolve

_crawl_resolve_due（合格池 qualified_ready < CRAWL_POOL_LOW ∧ crawl_work_remaining>0 才派；work_remaining
= candidate + actionable pending，recheck cooldown 阻 PENDING busy-loop → 母體枯竭即 latch）+
do_crawl_resolve（due 才派一隻、無 batch、label __restock__、不真派 agent）。monkeypatch pool_counts /
crawl_work_remaining / ed.load_all / dispatch_llm（try/finally 還原）。"""

from book_pipeline import pipeline_tick as pt
from book_pipeline import booklists as bl


def _pc(qualified_ready):
    return {'qualified_ready': qualified_ready, 'confirmed': qualified_ready, 'ready': qualified_ready,
            'candidate': 0, 'unresolved': 0, 'pending': 0, 'owned': 0, 'rejected': 0,
            'absent': 0, 'not_found': 0, 'review': 0, 'version_unavailable': 0}


def _patch(qualified_ready, work):
    """共用：mock pool_counts（水位）+ crawl_work_remaining（actionable 母體）+ ed.load_all（避免讀磁碟）。"""
    bl.pool_counts = lambda *a, **k: _pc(qualified_ready)
    bl.crawl_work_remaining = lambda *a, **k: work
    bl.ed.load_all = lambda *a, **k: {}
    bl.have_slugs = lambda *a, **k: set()
    bl.load_resolution = lambda *a, **k: {}


def _save():
    return (bl.pool_counts, bl.crawl_work_remaining, bl.ed.load_all, bl.have_slugs, bl.load_resolution)


def _restore(s):
    bl.pool_counts, bl.crawl_work_remaining, bl.ed.load_all, bl.have_slugs, bl.load_resolution = s


def test_due():
    s = _save()
    try:
        _patch(50, 10); assert pt._crawl_resolve_due()[0] is True    # 合格池低 + 有 actionable 母體 → 派
        _patch(100, 10); assert pt._crawl_resolve_due()[0] is False  # 合格池達水位 → 不派
        _patch(50, 0); assert pt._crawl_resolve_due()[0] is False    # 母體枯竭（含 PENDING 全 resting）→ latch
        print('✓ _crawl_resolve_due：合格池<水位 ∧ work_remaining>0 才派；母體枯竭/全 resting→latch')
    finally:
        _restore(s)


def test_dispatch_one_agent_no_batch():
    s = _save(); o = pt.dispatch_llm
    cap = []
    try:
        _patch(30, 60)
        pt.dispatch_llm = lambda verb, slug, dry, label=None: cap.append((verb, slug, dry, label)) or 0
        rc = pt.do_crawl_resolve(dry=False)
        assert rc == 0 and len(cap) == 1, cap
        verb, slug, dry, label = cap[0]
        assert verb == 'crawl' and dry is False
        assert slug == '', repr(slug)                     # 無 batch slug（agent 自查工作母體）
        assert label == '__restock__', label              # lease/registry 穩定 singleton 鍵
        print('✓ do_crawl_resolve：派 1 隻庫存查證 agent、無 batch、label __restock__')
    finally:
        _restore(s); pt.dispatch_llm = o


def test_not_due_no_dispatch():
    s = _save(); o = pt.dispatch_llm
    cap = []
    try:
        pt.dispatch_llm = lambda *a, **k: cap.append(a) or 0
        _patch(120, 50); assert pt.do_crawl_resolve(dry=False) == 0 and cap == []   # 合格池滿
        _patch(30, 0); assert pt.do_crawl_resolve(dry=False) == 0 and cap == []     # 母體枯竭 latch
        print('✓ do_crawl_resolve：池滿 / 母體枯竭 → 不派 agent')
    finally:
        _restore(s); pt.dispatch_llm = o


if __name__ == '__main__':
    test_due()
    test_dispatch_one_agent_no_batch()
    test_not_due_no_dispatch()
    print('\n全部通過 ✅')
