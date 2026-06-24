"""book_qc detector 測試：以 petrucci/campbell 真實缺陷的合成案例 + 正常書反例。

零誤判優先：每個 detector 都附「合法書不該觸發」的反例。
"""
from book_pipeline import book_qc as qc


# --- partial_source（petrucci：源 PDF 只有後半本，首章 15）---

def test_partial_source_flags_mid_book_start():
    assert qc.partial_source_reason([15, 16, 17, 27]) == "partial_source(starts@15)"


def test_partial_source_clean_when_starts_at_one():
    assert qc.partial_source_reason([1, 2, 3, 44]) is None


def test_partial_source_ignores_appendix_only_and_empty():
    assert qc.partial_source_reason([]) is None
    # 非 int（附錄如 'A'）被濾掉，不誤判
    assert qc.partial_source_reason(["A", "B"]) is None


def test_partial_source_exempts_volume_two_plus():
    # SoT 本就要 Volume 2（Cohen QM Vol2 自 ch8 起）→ 不旗標
    assert qc.partial_source_reason([8, 9, 10], "Quantum Mechanics, Volume 2") is None
    assert qc.partial_source_reason([8, 9], "Quantum Mechanics, Volume II") is None
    assert qc.partial_source_reason([5, 6], "Some Book, Part 2") is None
    # Volume 1 / 無卷標示 → 殘卷仍旗標
    assert qc.partial_source_reason([15, 16], "General Chemistry") == "partial_source(starts@15)"
    assert qc.partial_source_reason([8, 9], "Foundations, Volume 1") == "partial_source(starts@8)"


def test_partial_source_volume_roman_and_part_branches():
    # part 分支 roman + vol 高號（≥9）皆須豁免（補 review 揪出的不對稱缺口）
    assert qc.partial_source_reason([5, 6], "Some Treatise, Part VI") is None
    assert qc.partial_source_reason([3, 4], "Series, Part V") is None
    assert qc.partial_source_reason([20, 21], "Handbook, Volume IX") is None
    assert qc.partial_source_reason([30, 31], "Handbook, Volume X") is None
    assert qc.partial_source_reason([12, 13], "Course of Theoretical Physics, Volume 12") is None
    # 卷標示為 1 / 無 → 仍旗標（不被 roman 子串誤豁免）
    assert qc.partial_source_reason([5, 6], "Part I: Foundations") == "partial_source(starts@5)"
    assert qc.partial_source_reason([5, 6], "Vivid Mechanics") == "partial_source(starts@5)"


# --- chapter_gap（中段缺失）---

def test_chapter_gap_flags_large_hole():
    # 格式為邊界章號 from->to（比差值更有用）：3 與 14 間缺失
    r = qc.chapter_gap_reason([1, 2, 3, 14, 15])
    assert r and "3->14" in r


def test_chapter_gap_tolerates_small_jumps():
    # 相鄰差 ≤ GAP_THRESHOLD(3) 視為合法跳號（如卷分界）
    assert qc.chapter_gap_reason([1, 2, 5, 8]) is None


# --- companion（campbell：Study Guide / in Focus）---

def test_companion_flags_study_guide():
    assert qc.companion_reason("Study Guide for Campbell Biology in Focus") == "companion"


def test_companion_flags_solutions_manual_and_instructor():
    assert qc.companion_reason("Student Solutions Manual") == "companion"
    assert qc.companion_reason("Instructor's Resource Guide") == "companion"


def test_companion_clean_on_plain_textbook():
    assert qc.companion_reason("Chemistry: The Central Science") is None
    # "guide" 單字不算（需 study guide）
    assert qc.companion_reason("A Guide to Quantum Field Theory") is None


# --- title_mismatch（配錯書）---

def test_title_mismatch_flags_divergent_title():
    # 南轅北轍（近乎零重疊）才旗標——保守設計
    r = qc.title_mismatch_reason("Campbell Biology", "Organic Chemistry: Structure and Function")
    assert r and r.startswith("title_mismatch")


def test_title_mismatch_conservative_on_shared_generic_token():
    # 僅共享一個泛用 token（biology）→ 50% > 門檻 0.34 → 不旗標（交給 companion/author 補網）
    assert qc.title_mismatch_reason("Campbell Biology", "Molecular Biology of the Cell") is None


def test_title_mismatch_clean_on_match_despite_subtitle():
    # SoT 主 token 全命中 → 不旗標（即使有額外副標）
    assert qc.title_mismatch_reason(
        "Quantum Mechanics", "Quantum Mechanics: Non-relativistic Theory") is None


def test_title_mismatch_clean_when_landed_is_subset_of_sot():
    # 落地短書名是 SoT 長書名子集（folland: 落地『Real Analysis』）→ 重疊係數 100% 不誤判
    assert qc.title_mismatch_reason(
        "Real Analysis: Modern Techniques and Their Applications", "Real Analysis") is None


def test_title_mismatch_flags_wrong_book_same_author():
    # razavi: SoT 要 Design of Analog CMOS，落地是 Fundamentals of Microelectronics → 0% 旗標
    r = qc.title_mismatch_reason(
        "Design of Analog CMOS Integrated Circuits", "Fundamentals of Microelectronics")
    assert r and r.startswith("title_mismatch")


def test_title_mismatch_skips_when_no_sot():
    assert qc.title_mismatch_reason("", "anything") is None


def test_title_mismatch_clean_on_stopword_only_titles():
    # 全 stopword（"General Chemistry" → general/chemistry，chemistry 留）仍能比
    assert qc.title_mismatch_reason("General Chemistry", "General Chemistry") is None


# --- empty_chapter ---

def test_empty_chapter_flags_zero_body():
    chs = [{"num": 1, "body_count": 10}, {"num": 2, "body_count": 0}]
    assert qc.empty_chapter_reason(chs) == "empty_chapter(1)"


def test_empty_chapter_clean():
    assert qc.empty_chapter_reason([{"num": 1, "body_count": 5}]) is None


def test_empty_chapter_skips_problem_only_chapters():
    # 題本（tamvakis）：body=0 但 problems 滿載 → 不算空
    chs = [{"num": n, "body_count": 0, "problem_count": 50} for n in range(1, 11)]
    assert qc.empty_chapter_reason(chs) is None


# --- detect 整合 + blocking 子集 ---

def test_detect_petrucci_like():
    book = {"title": "General Chemistry: Principles and Modern Applications",
            "chapters": [{"num": n, "body_count": 100} for n in range(15, 28)],
            "appendices": []}
    flags = qc.detect(book, "General Chemistry: Principles and Modern Applications")
    assert any(f.startswith("partial_source") for f in flags)
    assert qc.blocking_reasons(flags)  # partial_source 屬硬缺陷


def test_detect_campbell_like():
    book = {"title": "Study Guide for Campbell Biology in Focus",
            "chapters": [{"num": n, "body_count": 80} for n in range(1, 44)],
            "appendices": []}
    flags = qc.detect(book, "Campbell Biology")
    assert "companion" in flags
    assert "companion" in qc.blocking_reasons(flags)


def test_detect_clean_book_no_flags():
    book = {"title": "Gravitation",
            "chapters": [{"num": n, "body_count": 100} for n in range(1, 45)],
            "appendices": []}
    assert qc.detect(book, "Gravitation") == []


def test_blocking_excludes_empty_chapter():
    # empty_chapter 是警示級、不阻擋部署
    assert qc.blocking_reasons(["empty_chapter(2)"]) == []
    assert qc.blocking_reasons(["companion", "empty_chapter(2)"]) == ["companion"]


# --- no_problems_extracted（rules 宣告有題卻 parsed 0 題；clayden/devore/wald 實證受害者）---

def test_total_problem_count_sums_chapters_and_appendices():
    book = {"chapters": [{"problem_count": 5}, {"problem_count": 3}],
            "appendices": [{"problem_count": 2}]}
    assert qc.total_problem_count(book) == 10
    # 缺欄/None 視為 0
    assert qc.total_problem_count({"chapters": [{"body_count": 1}, {"problem_count": None}]}) == 0


def test_declared_inline_but_zero_flags():
    # devore-like：inline_problems=true 卻 0 題 → 正則對不上 OCR 版式、整批丟失
    assert qc.declared_problems_missing_reason({"inline_problems": True}, 0) == "no_problems_extracted"


def test_declared_pbi_but_zero_flags():
    # wald/clayden-like：章設 problems_block_idx 卻 0 題（任一章設了即算宣告）
    rules = {"inline_problems": False,
             "chapters": [{"problems_block_idx": 12}, {"problems_block_idx": None}]}
    assert qc.declared_problems_missing_reason(rules, 0) == "no_problems_extracted"


def test_declared_but_has_problems_clean():
    # 宣告有題且真有題（osborne 117）→ 不旗標
    assert qc.declared_problems_missing_reason({"inline_problems": True}, 117) is None


def test_not_declared_and_zero_is_legit_empty():
    # 純理論書/專著（angrist-like）：inline=false ∧ 全章 pbi=null ∧ 0 題 → 合法、不旗標（誤判防線）
    rules = {"inline_problems": False,
             "chapters": [{"problems_block_idx": None}, {"problems_block_idx": None}]}
    assert qc.declared_problems_missing_reason(rules, 0) is None
    assert qc.declared_problems_missing_reason({}, 0) is None


def test_rules_none_or_broken_fail_open():
    # rules 缺/壞 → None（無從判斷、fail-open，不旗標好書）
    assert qc.declared_problems_missing_reason(None, 0) is None
    assert qc.declared_problems_missing_reason("not a dict", 0) is None


def test_no_problems_extracted_is_blocking():
    assert "no_problems_extracted" in qc.blocking_reasons(["no_problems_extracted"])


def test_detect_flags_declared_but_empty_via_rules():
    # 端到端：detect 帶 rules → declared-but-empty 受害者被旗標且屬 BLOCKING（clayden 重現）
    book = {"title": "Organic Chemistry",
            "chapters": [{"num": n, "body_count": 100, "problem_count": 0} for n in range(1, 44)],
            "appendices": []}
    rules = {"inline_problems": False, "chapters": [{"problems_block_idx": 5}] * 38}
    flags = qc.detect(book, "Organic Chemistry", rules)
    assert "no_problems_extracted" in flags
    assert "no_problems_extracted" in qc.blocking_reasons(flags)


def test_detect_no_rules_skips_problem_check():
    # 向下相容：不傳 rules → 不查習題完整性（即使 0 題也不旗標）
    book = {"title": "X",
            "chapters": [{"num": 1, "body_count": 100, "problem_count": 0}], "appendices": []}
    assert qc.detect(book, "X") == []
