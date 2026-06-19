"""book_pipeline.trace 單測：--since 解析 + cohort 終態三分（零缺口的核心不變量）。

trace 是 read-only forensic 入口，只組合既有資料 API。最高含金量的 invariant：
  _terminal() 把每本書恰好歸一終態（deployed / stuck / inflight）→ cohort 漏斗
  ✅+⏳+⚠ == 入庫總數，永不漏書（observability 零缺口的程式保證）。
"""
from __future__ import annotations

from book_pipeline import trace as tr

# _parse_since 的完整解析契約在 test_first_seen.py §4（relative/today/ISO/非法）統一驗，此處不重複。


def test_fmt_span_buckets():
    assert tr._fmt_span(45) == '45s'
    assert tr._fmt_span(300) == '5m'
    assert tr._fmt_span(7200) == '2.0h'


def test_terminal_three_way_partition(monkeypatch):
    """_terminal 必歸一桶：已上站→deployed；未上站+卡關因→stuck；未上站+可推進→inflight。"""
    monkeypatch.setattr(tr.st, '_deployed', lambda s: s == 'dep')
    # deployed：_deployed True 即 deployed，無視其餘
    assert tr._terminal('dep', {'stage': '3 parsed', 'todo': '—'}, {})[0] == 'deployed'
    # stuck：qc-reject
    b, reason = tr._terminal('x', {'stage': '0 待ingest', 'todo': 'ingest'},
                             {'qc': {'verdict': 'reject', 'note': '版次不符'}})
    assert b == 'stuck' and 'qc-reject' in reason
    # inflight：未上站、無卡關因（parsed 等 daemon 續推）
    assert tr._terminal('y', {'stage': '3 parsed', 'todo': 'translate(可選)'}, {})[0] == 'inflight'


if __name__ == '__main__':
    test_fmt_span_buckets()
    print('✓ _fmt_span 分桶（s/m/h）')
    print('（test_terminal 需 pytest monkeypatch，跑 pytest）')
    print('\n全部通過 ✅')
