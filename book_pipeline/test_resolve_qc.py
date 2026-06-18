"""resolver 下載前書況閘測試：companion 只套 main、解答本豁免、title 0% 才 block。

校準依據：全 143 筆現存 resolved 候選零假陽（31 筆 companion 全是解答本，正確豁免）。
"""
from book_pipeline import resolve as rv


def _main(title="Design of Analog CMOS Integrated Circuits"):
    return {"slug": "x", "kind": "main", "title": title}


def _sol(base="Principles of Mathematical Analysis"):
    return {"slug": "x_sol", "kind": "solution", "title": f"{base} — Solutions"}


# --- companion：main 阻擋、solution 豁免 ---

def test_companion_blocks_main_target():
    # campbell：main 目標抓到 Study Guide
    r = rv.resolution_qc(_main("Campbell Biology"), "Study Guide for Campbell Biology in Focus")
    assert any("companion" in b for b in r["block"])


def test_companion_exempt_for_solution_target():
    # 解答本書名本就含 solutions manual → 不 block（否則秒殺合法解答本）
    r = rv.resolution_qc(_sol(), "Solutions Manual to Walter Rudin's Principles of Mathematical Analysis")
    assert r["block"] == []


def test_instructor_manual_exempt_for_solution():
    r = rv.resolution_qc(_sol("Linear Algebra and Its Applications"),
                         "INSTRUCTOR'S SOLUTIONS MANUAL LINEAR ALGEBRA AND ITS APPLICATIONS")
    assert r["block"] == []


# --- title_mismatch：0% 才 block，低重疊只 advisory ---

def test_title_mismatch_zero_overlap_blocks_main():
    # razavi：候選書名與 SoT 零重疊
    r = rv.resolution_qc(_main("Design of Analog CMOS Integrated Circuits"),
                         "Fundamentals of Microelectronics")
    assert any("0%" in b for b in r["block"])


def test_partial_overlap_warns_not_blocks():
    # 低重疊但非 0%（共享 1/3 區別性 token）→ advisory 提示但不硬擋（避免誤殺改寫書名的合法書）
    r = rv.resolution_qc(_main("Introduction to Linear Algebra"),
                         "Abstract Algebra: Theory and Practice")
    assert r["block"] == [] and r["advisory"]


def test_correct_book_clean():
    r = rv.resolution_qc(_main("Design of Analog CMOS Integrated Circuits"),
                         "Design of Analog CMOS Integrated Circuits, 2nd ed")
    assert r["block"] == [] and r["advisory"] == []


def test_subset_title_clean():
    # 候選短書名是 SoT 子集 → 100% 重疊、不誤判
    r = rv.resolution_qc(_main("Real Analysis: Modern Techniques and Their Applications"),
                         "Real Analysis")
    assert r["block"] == [] and r["advisory"] == []


def test_empty_candidate_failopen():
    assert rv.resolution_qc(_main(), "") == {"advisory": [], "block": []}


# --- 全現存 resolved 零假陽（回歸護欄）---

def test_no_false_positive_on_existing_resolved():
    from book_pipeline import booklists as bl
    res = bl.load_resolution()
    sot = {t["slug"]: t for t in bl.targets()}
    blocked = []
    for slug, e in res.items():
        if not (isinstance(e, dict) and e.get("id")):
            continue
        t = sot.get(slug)
        if not t:
            continue
        r = rv.resolution_qc(t, e.get("title", ""))
        if r["block"]:
            blocked.append((slug, e.get("title", ""), r["block"]))
    assert blocked == [], f"現存 resolved 不該被 block（假陽）：{blocked}"
