"""math_validate 結構聚類單測：skeleton 簽章 + cluster_findings（純函式，不碰磁碟）。

跑：uv run python -m book_pipeline.test_math_cluster
"""
from book_pipeline.math_validate import cluster_findings, skeleton, token_signals


def test_skeleton_same_pathology_diff_content():
    # 雙上標：內文不同 → 同骨架（這正是 exact-tex 看不見、cluster 要抓的）
    assert skeleton(r"a^{x}^{y}") == skeleton(r"p^{m}^{n}")
    # 相量 \left/ θ° \left.\right.：不同符號/數字 → 同骨架
    a = skeleton(r"V _ {a n} \left/ 0 ^ {\circ} \left. \right.")
    b = skeleton(r"I _ {b c} \left/ 9 ^ {\circ} \left. \right.")
    assert a == b, f"{a!r} != {b!r}"


def test_skeleton_keeps_control_seqs_and_structure():
    # 控制序列與結構符保留（診斷用），識別符→a、數字→0
    assert skeleton(r"\frac{1}{2}") == skeleton(r"\frac{9}{8}")
    assert skeleton(r"\frac{1}{2}") != skeleton(r"\sqrt{2}")          # 不同控制字 → 不同簇
    assert skeleton(r"a^{x}^{y}") != skeleton(r"a^{x^{2}}")           # 雙上標 vs 巢狀上標：結構不同
    s = skeleton(r"\kern - delimiterspace")
    assert s == r"\kern ·", s                                          # 控制字保留、內容塌縮成 ·
    # 空 {} 與有內容 {·} 可區分（保 missing-arg 訊號）
    assert skeleton(r"\mathbf{}") != skeleton(r"\mathbf{x}")


def test_skeleton_idempotent_stable():
    # 對自身輸出再跑一次：骨架穩定（identifiers/數字已抽掉、控制字/結構符無變化）
    for tex in [r"a^{x}^{y}", r"\frac{\alpha}{\beta} + 3", r"\left/ 0 \left. \right."]:
        once = skeleton(tex)
        assert skeleton(once) == once, f"unstable: {tex!r} → {once!r} → {skeleton(once)!r}"


def test_skeleton_empty():
    assert skeleton("") == ""
    assert skeleton(None) == ""  # type: ignore[arg-type]


def test_skeleton_lone_backslash_terminates():
    # 回歸：尾端裸 backslash 曾讓內容 run 無法前進 → 無窮迴圈（真實 corpus 有此壞式）
    assert skeleton("a \\") == "· \\"     # · 空格 單一 backslash
    assert skeleton("\\") == "\\"          # 純裸 backslash
    out = skeleton("x^{2} \\right.\\")     # 不崩、不卡
    assert out.startswith("· ^ { · } \\right")


def _f(category, tex, occ, detail=None, err="e"):
    return {"category": category, "tex": tex, "occ": occ, "detail": detail,
            "err": err, "display": False, "targets": []}


def test_cluster_groups_by_skeleton():
    items = [
        ("axler_linalg", _f("double_script", r"a^{x}^{y}", 3)),
        ("thomas_calculus", _f("double_script", r"p^{m}^{n}", 2)),   # 同骨架、別書
        ("evans_pde", _f("missing_brace", r"\mathbf{}", 1)),          # 別簇
    ]
    clusters = cluster_findings(items)
    # 雙上標兩書合一簇
    ds = [c for c in clusters if c["category"] == "double_script"]
    assert len(ds) == 1, [c["signature"] for c in clusters]
    c = ds[0]
    assert c["total_occ"] == 5
    assert c["book_count"] == 2
    assert c["uniques"] == 2                                          # 兩條相異壞式
    assert {b["slug"] for b in c["books"]} == {"axler_linalg", "thomas_calculus"}


def test_cluster_macro_keyed_by_detail():
    items = [
        ("do_carmo_dg", _f("undefined_macro", r"\Nu \colon S", 3, detail=r"\Nu")),
        ("rudin_analysis", _f("undefined_macro", r"\Nu \geq 0", 2, detail=r"\Nu")),
        ("axler_linalg", _f("undefined_macro", r"\Chi", 1, detail=r"\Chi")),
    ]
    clusters = cluster_findings(items)
    nu = [c for c in clusters if c["signature"] == r"\Nu"]
    assert len(nu) == 1 and nu[0]["total_occ"] == 5 and nu[0]["book_count"] == 2
    assert any(c["signature"] == r"\Chi" for c in clusters)


def test_cluster_sorted_by_occ_then_books():
    items = [
        ("b1", _f("other", r"\a{1}", 2)),
        ("b1", _f("double_script", r"x^{1}^{2}", 10)),
        ("b2", _f("double_script", r"y^{3}^{4}", 5)),
    ]
    clusters = cluster_findings(items)
    assert clusters[0]["category"] == "double_script"   # 15 occ 在前
    assert clusters[0]["total_occ"] == 15
    assert clusters[-1]["total_occ"] == 2


def test_cluster_examples_capped_at_3():
    items = [("b%d" % i, _f("double_script", r"x^{%d}^{%d}" % (i, i), 1)) for i in range(6)]
    clusters = cluster_findings(items)
    assert len(clusters) == 1
    assert len(clusters[0]["examples"]) == 3
    assert clusters[0]["uniques"] == 6 and clusters[0]["book_count"] == 6


def test_token_signals_ranks_by_books_then_occ():
    items = [
        ("b1", _f("other", r"\mathord{\left/ \vphantom{x}\right.\kern - delimiterspace}", 4)),
        ("b2", _f("other", r"a \vphantom{y} b", 2)),          # \vphantom 跨 2 書
        ("b1", _f("double_script", r"\frac{a}{b}^{x}^{y}", 1)),
    ]
    sig = token_signals(items)
    top = {t["token"]: t for t in sig}
    assert top[r"\vphantom"]["book_count"] == 2
    assert top[r"\vphantom"]["occ"] == 6          # 4 + 2
    assert top[r"\vphantom"]["uniques"] == 2
    assert top[r"\kern"]["book_count"] == 1
    # 書數高者排前
    assert sig[0]["token"] == r"\vphantom"


def test_token_signals_dedup_per_formula():
    # 同一式重複出現同 token → uniques/occ 只計一次
    items = [("b1", _f("other", r"\kern \kern \kern", 3))]
    sig = token_signals(items)
    kern = [t for t in sig if t["token"] == r"\kern"][0]
    assert kern["uniques"] == 1 and kern["occ"] == 3 and kern["book_count"] == 1


if __name__ == "__main__":
    test_skeleton_same_pathology_diff_content();   print("✓ 同病灶不同內文 → 同骨架")
    test_skeleton_keeps_control_seqs_and_structure(); print("✓ 控制序列/結構符保留、內容抽象")
    test_skeleton_idempotent_stable();             print("✓ 骨架穩定")
    test_skeleton_empty();                         print("✓ 空輸入")
    test_cluster_groups_by_skeleton();             print("✓ 依骨架跨書聚類")
    test_cluster_macro_keyed_by_detail();          print("✓ undefined_macro 依巨集名聚類")
    test_cluster_sorted_by_occ_then_books();       print("✓ 依 occ→書數 排序")
    test_cluster_examples_capped_at_3();           print("✓ 樣本上限 3、uniques/book_count 正確")
    test_token_signals_ranks_by_books_then_occ();  print("✓ token 直方圖依書數→occ 排序")
    test_token_signals_dedup_per_formula();        print("✓ token 每式去重")
    print("\n全部通過 ✅")
