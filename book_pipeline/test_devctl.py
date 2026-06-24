"""devctl._derive_running 測試：daemon 運轉判定（log-marker ∨ controller 進程實活）。

回歸守衛：log-marker 啟發法在高日誌量/長命/detached-respawn controller 下偽陰
（『reactive loop start』滾出 log tail 窗 → last_start=None → running=False），
而 controller 進程其實活著在跑 40 worker。偽陰會誤導『running_now=False → kick』
誤殺健康 controller。controller_alive 權威覆寫修正之。
"""
from __future__ import annotations

from datetime import datetime, timezone

from book_pipeline import devctl as dc


def _t(sec: int) -> datetime:
    return datetime(2026, 6, 24, 8, 0, sec, tzinfo=timezone.utc)


def test_running_by_log_marker_start_only():
    # 有 start、無 end → 運轉中（log 推導）
    assert dc._derive_running(_t(10), None, controller_alive=False) is True


def test_running_by_log_marker_start_after_end():
    # start 比 end 新 → 運轉中
    assert dc._derive_running(_t(20), _t(10), controller_alive=False) is True


def test_stopped_when_end_after_start_and_no_process():
    # end 比 start 新 ∧ 無 controller 進程 → 真停止
    assert dc._derive_running(_t(10), _t(20), controller_alive=False) is False


def test_controller_alive_overrides_log_end():
    # end 比 start 新（log 看似停）但 controller 進程活著 → 權威覆寫為運轉（drain/長命）
    assert dc._derive_running(_t(10), _t(20), controller_alive=True) is True


def test_controller_alive_overrides_missing_start_marker():
    # 核心 bug：start marker 滾出 log tail 窗（last_start=None）但 controller 活著 → 不偽陰
    assert dc._derive_running(None, _t(20), controller_alive=True) is True
    assert dc._derive_running(None, None, controller_alive=True) is True


def test_stopped_when_no_markers_and_no_process():
    # 無 marker ∧ 無進程 → 真停止（不誤判運轉）
    assert dc._derive_running(None, None, controller_alive=False) is False
    assert dc._derive_running(None, _t(20), controller_alive=False) is False


if __name__ == '__main__':
    test_running_by_log_marker_start_only()
    test_running_by_log_marker_start_after_end()
    test_stopped_when_end_after_start_and_no_process()
    test_controller_alive_overrides_log_end()
    test_controller_alive_overrides_missing_start_marker()
    test_stopped_when_no_markers_and_no_process()
    print('全部通過 ✅')
