"""math_validate 純字串契約測試：uv run python -m book_pipeline.test_math_validate

守的是 corpus-level math sweep 兩條「LLM↔確定性碼」橋接的真相：

1. categorize(err)：把 MathJax render_check.js 的失敗訊息歸成 7 類，且對
   undefined_macro 抽出巨集名。這輸出**直接驅動** aggregate_reports 的 pattern-mining
   ——by_macro 高頻巨集優先泛化（加 macro 一次清全 corpus）。錯分類 → sweep 補錯巨集、
   白燒 LLM。純 substring 比對極脆：MathJax 升級換措辭、或某分支被別的分支搶先吃掉，
   都會無聲改變分類。本測試把每類的代表訊息→分類結果釘死，措辭一變即 RED。

2. locator_to_target(locator)：iter_units 產的 locator 文法 → apply_math_overrides
   吃的 {chunk, selector} 文法。這是 math 修復把 override 套回正確 block 的唯一橋。
   橋接文法一漂移 → override 全填錯欄、套錯 block（skip-drift，靜默損毀）。把四種
   locator 形態的轉換釘死。

全 hermetic、純函式、零 I/O、零 node、零真實資料。
"""

from book_pipeline import math_validate


# ── categorize：7 類分類契約 + undefined_macro 抽巨集名 ──────────────────────
#
# 為何重要：categorize 是純 substring 比對（見 math_validate.py L122-141），分支有
# 嚴格優先序（undefined → double_script → math_mode → alignment → missing_brace →
# left_right → other）。任何一條被改、或 MathJax 換訊息措辭，都會悄悄改變 by_category /
# by_macro 統計 → sweep 補錯巨集。本測試逐類釘死「代表訊息 → 期望分類」這條契約。

def test_categorize_seven_categories_and_macro_extract():
    # undefined_macro：UNDEF_RE 抽出巨集名（含反斜線），detail 是 pattern-mining 的鍵。
    # 用 raw string 確保 \foobar 是真的 backslash，比照 MathJax 真實錯訊。
    cat, detail = math_validate.categorize(r"Undefined control sequence \foobar")
    assert cat == "undefined_macro", cat
    assert detail == r"\foobar", repr(detail)  # detail 餵 by_macro 高頻泛化，必含反斜線

    # double_script：'Double superscript' / 'Double subscript' 兩措辭皆歸 double_script。
    assert math_validate.categorize("Double superscript") == ("double_script", None)
    assert math_validate.categorize("Double subscript") == ("double_script", None)

    # math_mode：'Missing $ inserted' 的 'missing $' 命中 math_mode 分支。
    assert math_validate.categorize("Missing $ inserted") == ("math_mode", None)

    # alignment：'Extra alignment tab' 命中 'extra alignment'。
    assert math_validate.categorize("Extra alignment tab") == ("alignment", None)

    # missing_brace：'Missing } inserted' 命中 'missing }'。
    assert math_validate.categorize("Missing } inserted") == ("missing_brace", None)

    # left_right：'Extra \left' 靠 '\\left' in err 命中（純字面比對，非 lower 後）。
    assert math_validate.categorize(r"Extra \left") == ("left_right", None)

    # other 兜底三態：None / 空字串 / 不認得的字串，全歸 other（不誤入任何實類）。
    assert math_validate.categorize(None) == ("other", None)
    assert math_validate.categorize("") == ("other", None)
    assert math_validate.categorize("some totally unrelated message") == ("other", None)

    print("✓ categorize 7 類分類契約 + undefined_macro 抽巨集名（措辭變異即 RED）")


# ── categorize 分支「優先序」——必須用「同時命中多分支」的 input 才守得住 ──────────
#
# 為何另立一條：上面的 7 類測試全是「單命中」input（只含一個分支的觸發詞），把分支
# 重排也不會翻——對「順序」其實是套套邏輯。真正守順序的唯一辦法是餵「同時含 N 個分支
# 觸發詞」的字串，只有正確的優先序才會給出文件承諾的類別。下列每條都至少跨兩分支。

def test_categorize_branch_precedence_under_collision():
    # (1) undefined_macro 最高優先：即使訊息又含 'Double superscript'，仍歸 undefined_macro
    #     且照樣抽出巨集名。若 double_script 被提前，這條會翻成 ('double_script', None)。
    assert math_validate.categorize(
        r"Undefined control sequence \alpha (also Double superscript)"
    ) == ("undefined_macro", r"\alpha")

    # (2) math_mode('missing $') 早於 missing_brace('missing }')：餵同時含兩者的字串，
    #     正確序給 math_mode；若 missing_brace 分支被提前到 math_mode 前，翻成 missing_brace。
    assert math_validate.categorize(
        "Missing $ inserted and missing } too"
    ) == ("math_mode", None)

    # (3) missing_brace('argument') 早於 left_right('\\left')：'Missing argument for \left'
    #     同時命中 'argument'（missing_brace）與 '\left'（left_right）。code 把 missing_brace
    #     列在 left_right 前 → 歸 missing_brace。這把「left_right 不會先吃掉含 argument 的 \left
    #     錯訊」這條真實優先序釘死；兩分支互換即 RED。
    assert math_validate.categorize(
        r"Missing argument for \left"
    ) == ("missing_brace", None)

    # (4) double_script 早於 math_mode：含 'Double superscript' 又含 'missing $' 的字串
    #     仍歸 double_script（double_script 分支在 math_mode 之前）。
    assert math_validate.categorize(
        "Double superscript with missing $ context"
    ) == ("double_script", None)

    print("✓ categorize 分支優先序（多命中 collision，重排即 RED）")


# ── categorize：undefined_macro 抽巨集名（UNDEF_RE）的健全性 ─────────────────────
#
# detail 直接當 by_macro Counter 的鍵 → 跨書 pattern-mining 高頻巨集優先泛化。抽錯/抽空
# → 加錯 macro、白燒 LLM。把 regex 的關鍵自由度（大小寫、@、單字元 escape、抽不到時的
# 退讓）逐一釘死，正則一被收緊就 RED。

def test_categorize_undefined_macro_extraction_robustness():
    # 大小寫不敏感：MathJax 有時小寫 'undefined'，UNDEF_RE 用 [Uu] —— 仍須命中且抽名。
    assert math_validate.categorize(
        r"undefined control sequence \beta"
    ) == ("undefined_macro", r"\beta")

    # 巨集名含 '@'（LaTeX 內部巨集，如 \pgf@foo）：[A-Za-z@]+ 必須收進 @。
    assert math_validate.categorize(
        r"Undefined control sequence \pgf@foo"
    ) == ("undefined_macro", r"\pgf@foo")

    # 單字元 escape（\, \! 之類）：靠 regex 的 '\\.' 退路抽出。
    assert math_validate.categorize(
        r"Undefined control sequence \,"
    ) == ("undefined_macro", r"\,")

    # 退讓：訊息含 'undefined control sequence' 但其後無反斜線巨集 → 抽不到 → 不可冒充
    #     undefined_macro（否則 by_macro 會被 None 鍵污染），須落到 other。
    assert math_validate.categorize(
        "Undefined control sequence (no macro)"
    ) == ("other", None)

    print("✓ categorize undefined_macro 抽名健全性（大小寫/@/單字元/抽不到退讓）")


# ── locator_to_target：iter_units locator → apply_math_overrides selector 橋接文法 ──
#
# 為何重要：這是 math sweep 把 override 寫回正確 block 的唯一文法橋（見 L51-63）。
# 四種 locator 形態的轉換規則一旦漂移，override 就套到錯的 chunk/欄位 → skip-drift
# 靜默損毀。逐形態釘死。

def test_locator_to_target_four_forms():
    # 這四形必須對齊 producer（math_audit.iter_units L76/L83/L100）實際 emit 的文法：
    #   title          → f"{stem}:title"
    #   body[N]        → f"{stem}:body[{idx}]"
    #   problem[P].S[M]→ f"{stem}:problem[{prob}].{sec}[{idx}]"，sec ∈ {body, solution}
    # 偏離 producer 的合成 input 沒有契約意義，故以下全用真實可產出的 locator。

    # body[N]：直接原樣當 selector，chunk 取冒號前。
    assert math_validate.locator_to_target("ch03:body[7]") == {
        "chunk": "ch03", "selector": "body[7]",
    }

    # problem[N].field[M]：跨文法轉換 —— problem[5].solution[1] → problem:5:solution[1]。
    # 這是兩套文法差最大的一形（[].格式 → :冒號:格式），最易漂移，必釘。
    assert math_validate.locator_to_target("ch03:problem[5].solution[1]") == {
        "chunk": "ch03", "selector": "problem:5:solution[1]",
    }

    # title：無索引，原樣當 selector。
    assert math_validate.locator_to_target("ch03:title") == {
        "chunk": "ch03", "selector": "title",
    }

    # appendix：producer 的 stem 來自 Path(file).stem，corpus 內實際是 'appA'/'appII'（**無空格**），
    # 故真實 appendix locator 形如 'appA:body[2]'，與 ch 同走 body 分支。釘住它確保 appendix
    # 不是被遺忘的冷路徑。（先前版本用 'app A' 帶空格屬虛構文法，已修正。）
    assert math_validate.locator_to_target("appII:body[2]") == {
        "chunk": "appII", "selector": "body[2]",
    }

    print("✓ locator_to_target 四形態橋接文法（body/problem.field/title/appendix）")


# ── locator_to_target：producer 真實會 emit、卻被原測試漏掉的形態 ─────────────────
#
# 為何補：原測試只覆蓋 problem[5].solution[1]（單位數 num、唯一 field、有 .field）。但 producer
# 還會 emit：(a) problem[N].body[M]（sec 迴圈含 'body'，非只 solution）、(b) 多位數 problem num
# 與多位數 idx、(c) problem[N] 不接 .field（理論上 _field_units 不產 block-less problem，但若漂移
# 出此形，須走 else 原樣保留而非崩）。任一形被轉錯 → override 套到錯 block，skip-drift 靜默損毀。

def test_locator_to_target_producer_real_variants():
    # (a) problem[N].body[M]：sec 可為 'body'（非只 solution），同樣轉成 :冒號: 文法。
    #     若 code 硬編 'solution' 而非動態抽 field，這條會 RED。
    assert math_validate.locator_to_target("ch01:problem[2].body[0]") == {
        "chunk": "ch01", "selector": "problem:2:body[0]",
    }

    # (b) 多位數 problem num + 多位數 sub-idx：num='10'、idx='3' 都不可被截成單字元。
    #     若有人用 rest[8] 之類固定位移取 num，單位數 input 仍綠、這條立刻 RED。
    assert math_validate.locator_to_target("ch12:problem[10].solution[3]") == {
        "chunk": "ch12", "selector": "problem:10:solution[3]",
    }

    # (c) problem[N] 無 .field：不滿足 if 的 '].' 條件 → 走 else 原樣保留。守「未知 problem
    #     形態不被誤拆」這條退讓，避免 split 出 IndexError 把 daemon tick 炸掉。
    assert math_validate.locator_to_target("ch03:problem[5]") == {
        "chunk": "ch03", "selector": "problem[5]",
    }

    # (d) split(':', 1) 的 maxsplit 守恆：selector 端的 :冒號: 是 output 才有；萬一未來 chunk 名
    #     本身含冒號，maxsplit=1 確保只切首冒號、chunk 後段不被吞進 selector。這裡用一個含冒號的
    #     合成 chunk 名直接驗 maxsplit 不退化（非虛構 producer 文法，而是純粹守 split 參數）。
    assert math_validate.locator_to_target("vol1:ch3:body[0]") == {
        "chunk": "vol1", "selector": "ch3:body[0]",
    }

    print("✓ locator_to_target producer 真實變體（problem.body/多位數/無 field/maxsplit）")


if __name__ == '__main__':
    test_categorize_seven_categories_and_macro_extract()
    test_categorize_branch_precedence_under_collision()
    test_categorize_undefined_macro_extraction_robustness()
    test_locator_to_target_four_forms()
    test_locator_to_target_producer_real_variants()
    print('\n全部通過 ✅')
