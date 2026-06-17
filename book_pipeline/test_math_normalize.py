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
    # R4 fix_cond_times：條件乘號（math/text 雙態相容）→ \times（尾隨 { } 原樣保留）
    (r"\mathrm { S U } ( 2 ) \ifmmode \times \else \texttimes \fi { } \mathrm { S U } ( 2 )",
     r"\mathrm { S U } ( 2 ) \times { } \mathrm { S U } ( 2 )"),
    # R5 移除群組噪訊：成對開（\mathopen{}\mathclose\bgroup）/ 閉（\aftergroup\egroup）
    (r"\mathbf { Z } _ { \mathrm { T h } } = 4 . 4 7 3 \mathopen { } \mathclose \bgroup / - 7 . 6 4 ^ { \circ } ~ \Omega",
     r"\mathbf { Z } _ { \mathrm { T h } } = 4 . 4 7 3  / - 7 . 6 4 ^ { \circ } ~ \Omega"),
    (r"\mathrm { d } _ { X } \mathopen { } \mathclose \bgroup \left( x , \mathfrak { p } \aftergroup \egroup \right) < \delta",
     r"\mathrm { d } _ { X }  \left( x , \mathfrak { p }   \right) < \delta"),
    # R5 巢狀：含殘留 \mathclose\bgroup（無 \mathopen{} 前綴）→ 收斂成平衡式
    (r"\beta _ { h } \mathopen { } \mathclose \bgroup \left( \varphi _ { 1 * } \mathclose \bgroup \left( \left[ f \right] \aftergroup \egroup \right) \aftergroup \egroup \right)",
     r"\beta _ { h }  \left( \varphi _ { 1 * }  \left( \left[ f \right]   \right)   \right)"),
    # R5 裸 \bgroup（無配對的純噪訊）
    (r"\mathbb { A } ^ { 2 } \bgroup ?", r"\mathbb { A } ^ { 2 }  ?"),
    # R6 MathType slash residue：\mathord/\mathbin + \left/ + \vphantom + \kern - delimiterspace → /
    (r"{ R \mathord { \left/ { \vphantom { R Q } } \right. \kern - delimiterspace } Q }",
     r"{ R / Q }"),
    (r"{ { \partial T } \mathord { \left/ { \vphantom { { \partial T } { \partial y } } } \right. \kern - delimiterspace } { \partial y } }",
     r"{ { \partial T } / { \partial y } }"),
    (r"{ \tilde { S } } ( p ) = - { p \mathord { \left/ { \vphantom { p } } \right. \kern - delimiterspace } p ^ { 2 } }",
     r"{ \tilde { S } } ( p ) = - { p / p ^ { 2 } }"),
    (r"\boldsymbol { q } \mathbin { \left/ \vphantom { \left( \boldsymbol { q } \mathbin { \left/ \vphantom { \left( \boldsymbol { x } + \boldsymbol { \hat { x } } \right) } \right.}  \kern - delimiterspace \right.} \left( \boldsymbol { x } + \boldsymbol { \hat { x } } \right)  \kern - delimiterspace \right.} \left( \boldsymbol { x } - \boldsymbol { \hat { x } } + \boldsymbol { \hat { x } } \right)",
     r"\boldsymbol { q } / \left( \boldsymbol { x } - \boldsymbol { \hat { x } } + \boldsymbol { \hat { x } } \right)"),
    # R7 underlined angle residue：\underline{{\left/ ... \left. \right.}} → \underline{\angle ...}
    (r"\mathbf {V} _ {a n} = V _ {p} \underline {{\left/ 0 ^ {\circ} \left. \right.}}",
     r"\mathbf {V} _ {a n} = V _ {p} \underline{\angle 0 ^ {\circ}}"),
    (r"\frac {z _ {1}}{z _ {2}} = \frac {r _ {1}}{r _ {2}} \underline {{{\left/ \phi_ {1} - \phi_ {2} \left. \right.}}}",
     r"\frac {z _ {1}}{z _ {2}} = \frac {r _ {1}}{r _ {2}} \underline{\angle \phi_ {1} - \phi_ {2}}"),
    (r"G (j 0) = 1 \underline {{\left/ 0 ^ {\circ} \left. \right.}} \quad \text { and } \quad G \left(j \frac {1}{T}\right) = \frac {1}{\sqrt {2}} \underline {{\left/ - 4 5 ^ {\circ} \left. \right.}}",
     r"G (j 0) = 1 \underline{\angle 0 ^ {\circ}} \quad \text { and } \quad G \left(j \frac {1}{T}\right) = \frac {1}{\sqrt {2}} \underline{\angle - 4 5 ^ {\circ}}"),
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
    r"x \times y",                       # R4：合法 \times 勿動
    r"a \ifmmode b \fi",                 # R4：非「條件乘號」形的 \ifmmode 不碰
    r"\mathopen{(} x \mathclose{)}",     # R5：獨立 \mathopen/\mathclose（無 \bgroup）合法，勿動
    r"\mathord{\cdot}",                  # R6：非 slash 殘體的 \mathord 勿動
    r"\underline{\angle \theta}",        # R7：已正確的 underlined angle 勿動
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
    test_tex_rules_fix_samples();            print("✓ R1-R7 修復真實樣本")
    test_tex_idempotent();                   print("✓ tex 冪等")
    test_tex_noop_on_valid();                print("✓ 正確式/巢狀/跳脫 no-op")
    test_md_inline_only_touches_math();      print("✓ md inline 只動數學區")
    test_md_inline_noop_when_no_dollar();    print("✓ md 無 $ 不動")
    test_normalize_chunk_math_walks_everything(); print("✓ chunk post-pass 全走訪 + 冪等")
    print("\n全部通過 ✅")
