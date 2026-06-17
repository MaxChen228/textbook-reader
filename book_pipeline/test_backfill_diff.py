"""backfill_math 公式級 diff / 閘判決單測：diff_reports + gate_verdict + _reparse。

跑：uv run python -m book_pipeline.test_backfill_diff
"""
import book_pipeline.backfill_math as bm


def _rep(bad_occ, locs):
    """合成 _math_report：每個 locator 一條 finding。"""
    return {"stats": {"bad_occ": bad_occ, "bad_unique": len(locs)},
            "findings": [{"tex": f"bad{i}", "locators": [loc]} for i, loc in enumerate(locs)]}


def test_diff_reports_fixed_collateral_stillbad():
    before = _rep(2, ["ch01:body[0]", "ch01:body[1]"])
    after = _rep(2, ["ch01:body[1]", "ch01:body[2]"])
    d = bm.diff_reports(before, after)
    assert d["fixed"] == ["ch01:body[0]"]          # 壞→好
    assert d["collateral"] == ["ch01:body[2]"]     # 好→壞（誤傷）
    assert d["still_bad"] == ["ch01:body[1]"]
    assert d["before_occ"] == 2 and d["after_occ"] == 2


def test_diff_reports_none_and_empty():
    assert bm.diff_reports(None, None) == {"fixed": [], "collateral": [], "still_bad": [],
                                           "before_occ": 0, "after_occ": 0}
    # skipped/缺 findings：不崩
    d = bm.diff_reports({"stats": {"bad_occ": 0}}, _rep(1, ["ch01:body[0]"]))
    assert d["collateral"] == ["ch01:body[0]"] and d["after_occ"] == 1


def test_gate_verdict_net_improvement_passes():
    before = {"b1": _rep(5, [f"ch01:body[{i}]" for i in range(5)])}
    after = {"b1": _rep(2, ["ch01:body[3]", "ch01:body[4]"])}
    v = bm.gate_verdict(before, after)
    assert v["ok"] is True
    assert v["before_occ"] == 5 and v["after_occ"] == 2 and v["delta"] == -3
    assert v["fixed_total"] == 3 and v["regressed"] == [] and v["collateral"] == []


def test_gate_verdict_regression_fails_even_if_corpus_drops():
    # b1 改善 -4，b2 惡化 +2 → corpus -2 但 b2 上升 → 不過（edge case 須先 override）
    before = {"b1": _rep(6, [f"ch01:body[{i}]" for i in range(6)]), "b2": _rep(1, ["ch02:body[0]"])}
    after = {"b1": _rep(2, ["ch01:body[0]", "ch01:body[1]"]),
             "b2": _rep(3, ["ch02:body[0]", "ch02:body[1]", "ch02:body[2]"])}
    v = bm.gate_verdict(before, after)
    assert v["delta"] == -2 and v["ok"] is False
    assert [r["slug"] for r in v["regressed"]] == ["b2"]
    assert any(c["slug"] == "b2" for c in v["collateral"])


def test_gate_verdict_no_net_change_fails():
    before = {"b1": _rep(3, ["ch01:body[0]", "ch01:body[1]", "ch01:body[2]"])}
    after = {"b1": _rep(3, ["ch01:body[0]", "ch01:body[1]", "ch01:body[2]"])}
    v = bm.gate_verdict(before, after)
    assert v["delta"] == 0 and v["ok"] is False    # 必須嚴格下降才採用


def test_gate_verdict_ok_by_occ_not_locator_count_under_12cap():
    # 核心契約：findings locators 有 12-cap，但 ok/delta 取自 stats.bad_occ（精確、不截斷）。
    # 一條 occ=20 的式子修好：locators 僅列 12，但 before_occ=20→after_occ=0、ok=True。
    before = {"b1": {"stats": {"bad_occ": 20},
                     "findings": [{"tex": "bad", "locators": [f"ch01:body[{i}]" for i in range(12)]}]}}
    after = {"b1": {"stats": {"bad_occ": 0}, "findings": []}}
    v = bm.gate_verdict(before, after)
    assert v["before_occ"] == 20 and v["after_occ"] == 0 and v["delta"] == -20 and v["ok"] is True
    # 反向：locator 集合相同(churn)但 occ 不降 → delta=0 → 不過（cap 不影響此判定）
    after2 = {"b1": {"stats": {"bad_occ": 20},
                     "findings": [{"tex": "bad2", "locators": [f"ch01:body[{i}]" for i in range(12)]}]}}
    assert bm.gate_verdict(before, after2)["ok"] is False


def test_gate_verdict_collateral_surfaced_when_passing_with_override():
    # 規則修了 b1 的 5 條、誤傷 1 條但被同變更 override 回去 → after 該位置已不在 findings
    # （模擬 override 後）：before 5 壞、after 1 壞且非新位置 → ok
    before = {"b1": _rep(5, [f"ch01:body[{i}]" for i in range(5)])}
    after = {"b1": _rep(1, ["ch01:body[4]"])}
    v = bm.gate_verdict(before, after)
    assert v["ok"] is True and v["collateral"] == []


def test_reparse_reapplies_catalog_and_math_overrides():
    calls = []
    orig_parse = bm.parse_book
    orig_catalog = bm.apply_catalog_overrides
    orig_math = bm.apply_math_overrides
    bm.parse_book = lambda slug: calls.append(("parse", slug))
    bm.apply_catalog_overrides = lambda slug: calls.append(("catalog", slug))
    bm.apply_math_overrides = lambda slug: calls.append(("math", slug))
    try:
        status = bm._reparse("demo")
    finally:
        bm.parse_book = orig_parse
        bm.apply_catalog_overrides = orig_catalog
        bm.apply_math_overrides = orig_math
    assert calls == [("parse", "demo"), ("catalog", "demo"), ("math", "demo")]
    assert status == "parsed + catalog-overrides + math-overrides"


if __name__ == "__main__":
    test_diff_reports_fixed_collateral_stillbad();   print("✓ diff_reports：fixed/collateral/still_bad")
    test_diff_reports_none_and_empty();              print("✓ diff_reports：None/缺 findings 邊界")
    test_gate_verdict_net_improvement_passes();      print("✓ gate_verdict：淨降無 regression → pass")
    test_gate_verdict_regression_fails_even_if_corpus_drops(); print("✓ gate_verdict：任一書上升 → fail（即使 corpus 降）")
    test_gate_verdict_no_net_change_fails();         print("✓ gate_verdict：無淨降 → fail（須嚴格下降）")
    test_gate_verdict_ok_by_occ_not_locator_count_under_12cap(); print("✓ gate_verdict：ok 依 bad_occ（12-cap 不影響判定）")
    test_gate_verdict_collateral_surfaced_when_passing_with_override(); print("✓ gate_verdict：collateral 已 override → pass")
    test_reparse_reapplies_catalog_and_math_overrides(); print("✓ _reparse：parse 後會重套 catalog + math overrides")
    print("\n全部通過 ✅")
