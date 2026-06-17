"""mark_math_accepted 契約：夾值一律以 report.stats.bad_occ（ground truth）為準，**不吃**
state.math 快取——存量書 state 無 bad_occ 時仍正確夾（回歸：冷啟空窗→接受任意 occ 的 bug）。
report 不存在 → raise，絕不 silent 接受。

跑：uv run python -m book_pipeline.test_math_accept
"""
import os
import tempfile

from book_pipeline import pipeline_queue as q
from book_pipeline import math_validate as mv


class _Env:
    """隔離 STATE_PATH/STATE_LOCK 到 temp + 假 read_report（bad=None → 無 report）。"""

    def __init__(self, bad, seed_state=None):
        self.bad = bad
        self.seed_state = seed_state

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory()
        self._old = (q.STATE_PATH, q.STATE_LOCK, mv.read_report)
        q.STATE_PATH = os.path.join(self._td.name, 'state.json')
        q.STATE_LOCK = os.path.join(self._td.name, 'state.lock')
        if self.seed_state:
            q._save_state(self.seed_state)
        mv.read_report = lambda slug: ({'stats': {'bad_occ': self.bad}}
                                       if self.bad is not None else None)
        return self

    def __exit__(self, *a):
        q.STATE_PATH, q.STATE_LOCK, mv.read_report = self._old
        self._td.cleanup()


def test_clamp_uses_report_not_state():
    # state 無 math.bad_occ（存量書冷啟），report 說殘餘=4 → accept 99 必夾到 4，不接受任意值
    with _Env(bad=4):
        q.mark_math_accepted('bk', 99, 'src destroyed')
        assert q.math_accepted('bk') == 4


def test_clamp_ignores_stale_state_bad_occ():
    # state.math.bad_occ 是過時快取(=100)，report 才是 ground truth(=3) → 夾到 3（非 50、非 100）
    with _Env(bad=3, seed_state={'bk': {'math': {'bad_occ': 100}}}):
        q.mark_math_accepted('bk', 50, 'x')
        assert q.math_accepted('bk') == 3


def test_accept_below_residual_kept():
    with _Env(bad=10):
        q.mark_math_accepted('bk', 2, 'x')
        assert q.math_accepted('bk') == 2


def test_no_report_raises():
    raised = False
    with _Env(bad=None):
        try:
            q.mark_math_accepted('bk', 1, 'x')
        except ValueError:
            raised = True
    assert raised, '無 report 應 raise，不可 silent 接受任意 occ'
    # 且不得落盤
    with _Env(bad=None):
        assert q.math_accepted('bk') == 0


def test_reason_and_timestamp_persisted():
    with _Env(bad=5):
        q.mark_math_accepted('bk', 1, 'source page torn')
        m = q.math_info('bk')
        assert m.get('accepted_reason') == 'source page torn'
        assert m.get('accepted_at')


def test_accepted_total_sums_across_books_skips_meta():
    with _Env(bad=9) as e:
        q.mark_math_accepted('a', 2, 'x')
        # 第二本：換 report 殘餘再 accept；__math__ meta 不得計入
        mv.read_report = lambda slug: {'stats': {'bad_occ': 9}}
        q.mark_math_accepted('b', 3, 'x')
        s = q._load_state()
        s.setdefault(q.MATH_STATE_KEY, {})['last_sweep'] = {'accepted': 999}  # 雜訊
        q._save_state(s)
        assert q.math_accepted_total() == 5


if __name__ == "__main__":
    test_clamp_uses_report_not_state();          print("✓ 夾值取 report（state 無 bad_occ 冷啟）")
    test_clamp_ignores_stale_state_bad_occ();    print("✓ 忽略過時 state.bad_occ，report 為準")
    test_accept_below_residual_kept();           print("✓ occ < 殘餘 → 原值保留")
    test_no_report_raises();                     print("✓ 無 report → raise 且不落盤")
    test_reason_and_timestamp_persisted();       print("✓ reason/at 落盤")
    test_accepted_total_sums_across_books_skips_meta(); print("✓ total 跨書加總、跳過 __math__")
    print("\n全部通過 ✅")
