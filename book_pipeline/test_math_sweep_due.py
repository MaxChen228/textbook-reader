"""收斂模型單測：_sweep_decision 純判定（residual_unaccepted>0 且非 fixpoint → 派）。

跑：uv run python -m book_pipeline.test_math_sweep_due
"""
from book_pipeline.pipeline_tick import _sweep_decision

M = "macros-A"


def _ls(before, after, touched=(), macros=M):
    return {"macros_version": macros, "residual_before": before,
            "residual_after": after, "touched": list(touched)}


def test_no_node_never_due():
    assert _sweep_decision(False, 999, 0, None, M) == (False, "no-node")


def test_converged_true_zero_or_all_accepted():
    assert _sweep_decision(True, 0, 0, None, M) == (False, "converged")
    assert _sweep_decision(True, 5, 5, _ls(9, 5), M) == (False, "converged")   # 剩餘全已 accept
    assert _sweep_decision(True, 5, 6, None, M)[0] is False                    # accepted>total → ≤0


def test_first_sweep_due():
    assert _sweep_decision(True, 10, 0, None, M) == (True, "due")
    assert _sweep_decision(True, 10, 0, {}, M) == (True, "due")


def test_fixpoint_stops_busy_loop():
    # 上次同 macros 沒降(before==after)也沒改書 → 原地踏步 → 不派
    ls = _ls(5, 5, touched=[])
    assert _sweep_decision(True, 5, 0, ls, M) == (False, "fixpoint")


def test_progress_keeps_firing_toward_zero():
    # 上次降了殘餘(10→5)，現殘餘=5、同 macros → 仍派（繼續朝 0 收斂）
    assert _sweep_decision(True, 5, 0, _ls(10, 5, touched=["b1"]), M) == (True, "due")
    # 即使 touched 空，只要 after<before（progressed）也續派
    assert _sweep_decision(True, 5, 0, _ls(8, 5, touched=[]), M) == (True, "due")


def test_macros_change_breaks_fixpoint():
    # agent 改了 macros（新規則）→ 不論上次如何，重新 baseline 再派
    assert _sweep_decision(True, 5, 0, _ls(5, 5, touched=[], macros="old"), M) == (True, "due")


def test_external_residual_change_breaks_fixpoint():
    # 新書部署使 total 變動（≠上次 residual_after）→ 非 fixpoint → 派
    assert _sweep_decision(True, 7, 0, _ls(5, 5, touched=[]), M) == (True, "due")


def test_accepted_reduces_unaccepted_to_due_boundary():
    assert _sweep_decision(True, 4, 4, None, M) == (False, "converged")
    assert _sweep_decision(True, 4, 3, None, M) == (True, "due")               # 未解 1 → 派


if __name__ == "__main__":
    test_no_node_never_due();                        print("✓ 缺 node → 永不派")
    test_converged_true_zero_or_all_accepted();      print("✓ 真 0 / 全 accept → converged")
    test_first_sweep_due();                          print("✓ 首次 sweep → due")
    test_fixpoint_stops_busy_loop();                 print("✓ fixpoint（沒降沒改書）→ 不派")
    test_progress_keeps_firing_toward_zero();        print("✓ 有進展 → 續派朝 0")
    test_macros_change_breaks_fixpoint();            print("✓ 改 macros → 破 fixpoint")
    test_external_residual_change_breaks_fixpoint(); print("✓ 殘餘外部變動 → 破 fixpoint")
    test_accepted_reduces_unaccepted_to_due_boundary(); print("✓ accepted 邊界")
    print("\n全部通過 ✅")
