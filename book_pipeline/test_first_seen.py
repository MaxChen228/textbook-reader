"""first_seen 入庫戳 + status cohort/stuck 查詢的契約測試。

跑：uv run python -m book_pipeline.test_first_seen

state 寫入以暫存 STATE_PATH 隔離（走真實 _state_lock + atomic_write，不碰真 pipeline_state.json）；
status 的純解析（_parse_since/_entry_ts）直接驗。
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from book_pipeline import pipeline_queue as q
from book_pipeline import status as st
from book_pipeline import trace as tr  # _parse_since 屬 forensic 入口（cohort），住 trace


# ── state 隔離：把 q 的 STATE_PATH/LOCK 指到 tmp，跑完還原 ──────────────────────
class _TmpState:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory(prefix='first_seen_')
        d = Path(self._d.name)
        self._orig = (q.STATE_PATH, q.STATE_LOCK)
        q.STATE_PATH = str(d / 'pipeline_state.json')
        q.STATE_LOCK = str(d / 'pipeline_state.lock')
        return self

    def __exit__(self, *a):
        q.STATE_PATH, q.STATE_LOCK = self._orig
        self._d.cleanup()


# ── 1. stamp_first_seen：idempotent，首蓋後永不覆寫 ────────────────────────────
def test_stamp_first_seen_idempotent():
    with _TmpState():
        q.stamp_first_seen('axler', when='2026-01-01T00:00:00+00:00')
        assert q.first_seen('axler') == '2026-01-01T00:00:00+00:00'
        # 二次蓋（不同時間）必須被忽略 —— 入庫時間是 durable、不可被後續觀測改寫
        q.stamp_first_seen('axler', when='2026-06-01T00:00:00+00:00')
        assert q.first_seen('axler') == '2026-01-01T00:00:00+00:00', '既存戳被覆寫了'


def test_stamp_first_seen_defaults_to_now():
    with _TmpState():
        q.stamp_first_seen('shankar')
        ts = q.first_seen('shankar')
        assert ts and datetime.fromisoformat(ts).tzinfo is not None  # aware UTC


# ── 2. ensure_first_seen：批補缺者、跳過已存、回新蓋數 ─────────────────────────
def test_ensure_first_seen_backfills_only_missing():
    with _TmpState():
        q.stamp_first_seen('a', when='2026-01-01T00:00:00+00:00')
        n = q.ensure_first_seen(['a', 'b', 'c'], infer=False)
        assert n == 2, f'應只新蓋 b,c（a 已存）得 {n}'
        assert q.first_seen('a') == '2026-01-01T00:00:00+00:00'  # 沒被動
        assert q.first_seen('b') and q.first_seen('c')
        # 再跑全已存 → 0、不寫
        assert q.ensure_first_seen(['a', 'b', 'c'], infer=False) == 0


def test_ensure_first_seen_empty():
    with _TmpState():
        assert q.ensure_first_seen([], infer=False) == 0


# ── 3. _infer_first_seen：取現有階段戳的最早值（補登歷史書較『現在』準）──────────
def test_infer_first_seen_picks_earliest_stamp():
    entry = {
        'deployed_at': '2026-03-01T12:00:00+00:00',
        'catalog_llm_at': '2026-02-15T08:00:00+00:00',   # 最早
        'math': {'at': '2026-04-01T00:00:00+00:00'},
    }
    got = q._infer_first_seen('whatever', entry)
    assert got == '2026-02-15T08:00:00+00:00', got


def test_infer_first_seen_no_evidence_falls_back_to_now():
    # 無任何階段戳、無檔案系統證據 → 退『現在』（aware、可解析）
    got = q._infer_first_seen('nonexistent-slug-xyz', {})
    d = datetime.fromisoformat(got)
    assert d.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - d).total_seconds()) < 60


# ── 4. trace._parse_since：relative / today / ISO / 非法 ───────────────────────
def test_parse_since_relative_units():
    now = datetime.now(timezone.utc)
    for expr, lo, hi in [('12h', 11.9, 12.1), ('3d', 71.9, 72.1),
                         ('90min', 1.49, 1.51), ('90m', 1.49, 1.51)]:
        cut = tr._parse_since(expr)
        hours = (now - cut).total_seconds() / 3600
        assert lo <= hours <= hi, f'{expr} → {hours}h 不在 [{lo},{hi}]'


def test_parse_since_today_and_iso():
    assert tr._parse_since('today').tzinfo is not None
    iso = tr._parse_since('2026-06-19')
    assert iso.tzinfo is not None and iso.astimezone().hour == 0  # 本地當日 00:00


def test_parse_since_invalid_raises():
    try:
        tr._parse_since('garbage')
        assert False, '非法 --since 應 SystemExit'
    except SystemExit:
        pass


# ── 5. status._entry_ts：first_seen_at 優先、無則退階段戳、全無則 None ──────────
def test_entry_ts_prefers_first_seen():
    state = {'x': {'first_seen_at': '2026-05-01T00:00:00+00:00',
                   'deployed_at': '2026-01-01T00:00:00+00:00'}}
    assert st._entry_ts('x', state) == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_entry_ts_fallback_to_stage_stamp():
    state = {'y': {'deployed_at': '2026-02-01T00:00:00+00:00',
                   'catalog_llm_at': '2026-01-10T00:00:00+00:00'}}
    # 無 first_seen_at → 退階段戳最早值
    assert st._entry_ts('y', state) == datetime(2026, 1, 10, tzinfo=timezone.utc)


def test_entry_ts_none_when_no_evidence():
    assert st._entry_ts('z', {'z': {}}) is None
    assert st._entry_ts('absent', {}) is None


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'✓ {name}')
    print('\n全部通過 ✅')
