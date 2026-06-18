"""crawl 解析 agent 派工水位邏輯：uv run python -m book_pipeline.test_crawl_resolve

_crawl_resolve_due（解析池<水位 ∧ 有 unresolved 才派）+ do_crawl_resolve（due 才派一隻、批次=BATCH、
不打網路、不真派 agent）。monkeypatch pool_counts/unresolved_targets/dispatch_llm。"""

from book_pipeline import pipeline_tick as pt
from book_pipeline import booklists as bl


def _pc(confirmed, unresolved):
    return {'confirmed': confirmed, 'unresolved': unresolved, 'ready': confirmed,
            'queued': 0, 'review': 0, 'owned': 0, 'absent': 0}     # 鏡像真實 pool_counts 形狀（含 review 桶）


def test_due():
    o = bl.pool_counts
    try:
        bl.pool_counts = lambda: _pc(50, 10)
        assert pt._crawl_resolve_due()[0] is True        # 池低 + 有 unresolved → 派
        bl.pool_counts = lambda: _pc(100, 10)
        assert pt._crawl_resolve_due()[0] is False       # 池達水位 → 不派
        bl.pool_counts = lambda: _pc(50, 0)
        assert pt._crawl_resolve_due()[0] is False       # 無 unresolved → 不派（可 idle 收斂）
        print('✓ _crawl_resolve_due：池<水位 ∧ 有 unresolved 才派')
    finally:
        bl.pool_counts = o


def test_dispatch_one_agent_batch():
    o1, o2, o3 = bl.pool_counts, bl.unresolved_targets, pt.dispatch_llm
    cap = []
    try:
        bl.pool_counts = lambda: _pc(30, 50)
        bl.unresolved_targets = lambda: [{'slug': f'b{i}', 'kind': 'main'} for i in range(50)]
        pt.dispatch_llm = lambda verb, slug, dry, label=None: cap.append((verb, slug, dry, label)) or 0
        rc = pt.do_crawl_resolve(dry=False)
        assert rc == 0 and len(cap) == 1, cap
        verb, slug, dry, label = cap[0]
        assert verb == 'crawl' and dry is False
        assert len(slug.split(',')) == pt.CRAWL_RESOLVE_BATCH, len(slug.split(','))  # 單批 = BATCH
        assert label == '__crawl_resolve__', label  # lease/registry 穩定鍵，batch slug 只進 prompt
        print(f'✓ do_crawl_resolve：派 1 隻 crawl agent、批次 {pt.CRAWL_RESOLVE_BATCH} 本、label 穩定鍵')
    finally:
        bl.pool_counts, bl.unresolved_targets, pt.dispatch_llm = o1, o2, o3


def test_not_due_no_dispatch():
    o1, o3 = bl.pool_counts, pt.dispatch_llm
    cap = []
    try:
        bl.pool_counts = lambda: _pc(120, 50)            # 池滿
        pt.dispatch_llm = lambda *a: cap.append(a) or 0
        assert pt.do_crawl_resolve(dry=False) == 0 and cap == []
        print('✓ do_crawl_resolve：池滿 → 不派 agent')
    finally:
        bl.pool_counts, pt.dispatch_llm = o1, o3


if __name__ == '__main__':
    test_due()
    test_dispatch_one_agent_batch()
    test_not_due_no_dispatch()
    print('\n全部通過 ✅')
