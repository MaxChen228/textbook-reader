"""math_normalize 單測：每規則 before/after（真實樣本）+ 冪等 + 正確式 no-op。

跑：uv run python -m book_pipeline.test_math_normalize
"""
from book_pipeline.math_normalize import (
    normalize_chunk_math,
    normalize_md_inline,
    normalize_tex,
)

# 真實壞樣本（取自 math_validate 實測）→ 期望修復後
TEX_CASES = [
    # R1 fix_tag_math：撇號被 OCR 成錯位 $ → 剝後重包正確 $...$（\tag 參數是文字模式，
    # ^ 需切回數學）→ 渲染為 7′
    (r"| a - b | ^ {2} = \operatorname{Re} a \bar {b}, \tag {$7^{\prime$}}",
     r"| a - b | ^ {2} = \operatorname{Re} a \bar {b}, \tag {$7^{\prime}$}"),
    (r"x = y \tag{$3'$}", r"x = y \tag{3'}"),  # 純文字撇號：不需數學模式
    (r"a \tag*{$12^{\prime\prime$}}", r"a \tag*{$12^{\prime\prime}$}"),
    # R2 double superscript
    (r"a^{x}^{y}", r"a^{x y}"),
    (r"e^{i}^{\theta}^{t}", r"e^{i \theta t}"),
    # R3 double subscript
    (r"a_{i}_{j}", r"a_{i j}"),
    # 混合
    (r"T_{\mu}_{\nu}^{a}^{b}", r"T_{\mu \nu}^{a b}"),
]

# 必須完全不動（正確 LaTeX / 巢狀 / 含空白的相鄰）
NOOP_CASES = [
    r"\frac{a}{b} + x^{2}",
    r"a^{x^{2}}",            # 巢狀上標：合法，勿動
    r"\sum_{i=1}^{n} a_i",   # 上下標同時：合法
    r"x^{a} ^{b}",           # 有空白：兩個獨立物件，勿合併
    r"\tag{eq:1}",           # tag 無 $：勿動
    r"\text{price is \$5}",  # 跳脫 \$：勿剝
    r"\left( \frac{1}{2} \right)",
]


def test_tex_rules_fix_samples():
    for src, want in TEX_CASES:
        assert normalize_tex(src) == want, f"{src!r} → {normalize_tex(src)!r} != {want!r}"


def test_tex_idempotent():
    for src, _ in TEX_CASES:
        once = normalize_tex(src)
        assert normalize_tex(once) == once, f"not idempotent: {src!r}"


def test_tex_noop_on_valid():
    for src in NOOP_CASES:
        assert normalize_tex(src) == src, f"unexpectedly changed: {src!r} → {normalize_tex(src)!r}"


def test_md_inline_only_touches_math():
    md = r"看這條 $a^{x}^{y}$ 與 $$b_{i}_{j}$$ 之間的文字 \$不是數學\$ 原樣"
    out = normalize_md_inline(md)
    assert "$a^{x y}$" in out
    assert "$$b_{i j}$$" in out
    assert "之間的文字" in out
    # 文字區（非 $ 包夾）byte-identical
    assert normalize_md_inline(out) == out  # idempotent


def test_md_inline_noop_when_no_dollar():
    s = "純文字 no math here a^{x}^{y} 不在 $ 內就不動"
    assert normalize_md_inline(s) == s


def test_normalize_chunk_math_walks_everything():
    chunk = {
        "title": r"標題 $a^{x}^{y}$",
        "body": [
            {"t": "eq", "tex": r"z_{i}_{j}"},
            {"t": "p", "md": r"正文 $p^{a}^{b}$"},
            {"t": "fig", "caption": r"圖 $c_{m}_{n}$"},
        ],
        "problems": [
            {"num": "1.1",
             "body": [{"t": "eq", "tex": r"\tag{$5'$} q^{u}^{v}"}],
             "solution": [{"t": "p", "md": r"解 $r^{s}^{t}$"}]},
        ],
    }
    normalize_chunk_math(chunk)
    assert chunk["title"] == r"標題 $a^{x y}$"
    assert chunk["body"][0]["tex"] == r"z_{i j}"
    assert chunk["body"][1]["md"] == r"正文 $p^{a b}$"
    assert chunk["body"][2]["caption"] == r"圖 $c_{m n}$"
    assert chunk["problems"][0]["body"][0]["tex"] == r"\tag{5'} q^{u v}"
    assert chunk["problems"][0]["solution"][0]["md"] == r"解 $r^{s t}$"
    # 冪等：再跑一次不變
    import copy
    snap = copy.deepcopy(chunk)
    normalize_chunk_math(chunk)
    assert chunk == snap


if __name__ == "__main__":
    test_tex_rules_fix_samples();            print("✓ R1-R3 修復真實樣本")
    test_tex_idempotent();                   print("✓ tex 冪等")
    test_tex_noop_on_valid();                print("✓ 正確式/巢狀/跳脫 no-op")
    test_md_inline_only_touches_math();      print("✓ md inline 只動數學區")
    test_md_inline_noop_when_no_dollar();    print("✓ md 無 $ 不動")
    test_normalize_chunk_math_walks_everything(); print("✓ chunk post-pass 全走訪 + 冪等")
    print("\n全部通過 ✅")
