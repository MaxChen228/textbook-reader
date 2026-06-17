#!/usr/bin/env python3
"""book_pipeline/math_normalize.py — 確定性 LaTeX 正規化（Layer 1）。

純函式 + 有序規則目錄，機械修 MinerU OCR 產的壞 LaTeX。每條規則 **100% 安全
且冪等**：對自身輸出 no-op、對正確式子 no-op。靠 Layer 2（math_validate）兜底
回歸——誤改會讓淨殘餘上升，立刻暴露。

parser.py 兩處用：
  - eq tex：block_to_struct 內，extract_eq_label 前（R1 把 \\tag{$..$}→\\tag{..}
    才能被 label_re 命中）。
  - 所有 md/tex：normalize_chunk_math() 單一 post-pass（鏡像 assign_catalog_ids），
    結構上涵蓋每個 block，不漏任何 call site。

規則樣本取自實測壞清單（math_validate）：
  R1 fix_tag_math       : \\tag{$7^{\\prime$}} → \\tag{$7^{\\prime}$}  (Math-mode / ^ in text)
  R2 double_superscript : a^{x}^{y}            → a^{x y}              (Double superscript)
  R3 double_subscript   : a_{x}_{y}            → a_{x y}              (Double subscript)
"""
from __future__ import annotations

import re


# ── R1：修 \tag{...} 內畸形的 $ ──────────────────────────────────────────────
# \tag 參數是「文字模式」，撇號被 OCR 讀成 $ 後常錯位（\tag{$7^{\prime$}}）。原意是
# 數學上標 7′，需在文字模式裡用 $...$ 切回數學。修法：剝光裸 $ 後，若內容含數學構造
# （^ _ 或 \命令）就重新包成正確的 $...$（→ \tag{$7^{\prime}$}，渲染為 7′）；純文字
# （如 7'）則不包。\$（跳脫）不動。冪等。
_TAG_RE = re.compile(r"\\tag\*?\s*\{")
_TAG_MATH_RE = re.compile(r"[\^_]|\\[A-Za-z]")


def _fix_tag_arg(arg: str) -> str:
    cleaned = re.sub(r"(?<!\\)\$", "", arg)  # 剝裸 $（保留 \$）
    if _TAG_MATH_RE.search(cleaned):
        return "$" + cleaned + "$"
    return cleaned


def _fix_tag_math(tex: str) -> str:
    if r"\tag" not in tex or "$" not in tex:
        return tex
    out: list[str] = []
    i = 0
    n = len(tex)
    while i < n:
        m = _TAG_RE.match(tex, i)
        if not m:
            out.append(tex[i])
            i += 1
            continue
        # 從 '{' 起算 brace 深度，找對應 '}'
        brace_start = m.end() - 1
        depth = 0
        j = brace_start
        while j < n:
            c = tex[j]
            if c == "\\" and j + 1 < n:  # 跳過跳脫字元（\{ \} \$）
                j += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:  # 不平衡 → 不動，留給 Layer 2 上報
            out.append(tex[i])
            i += 1
            continue
        arg = tex[brace_start + 1:j]
        if "$" in arg:
            arg = _fix_tag_arg(arg)
        out.append(tex[i:brace_start + 1])
        out.append(arg)
        out.append("}")
        i = j + 1
    return "".join(out)


# ── R2/R3：相鄰雙上/下標合併 ─────────────────────────────────────────────────
# OCR 把 a^{x y} 拆成 a^{x}^{y}（TeX「Double superscript」硬錯）。僅合併「brace
# 緊鄰 brace、且 brace 內無巢狀」的保守形，合成空白分隔。不碰 a^{x} ^{y}（有空白
# = 多半是兩個獨立物件）也不碰巢狀。
_DOUBLE_SUP = re.compile(r"\^\{([^{}]*)\}\^\{([^{}]*)\}")
_DOUBLE_SUB = re.compile(r"_\{([^{}]*)\}_\{([^{}]*)\}")


def _merge_double_scripts(tex: str) -> str:
    prev = None
    cur = tex
    # 反覆套用直到穩定（處理 a^{x}^{y}^{z}）；brace 數量單調遞減故必收斂。
    while cur != prev:
        prev = cur
        cur = _DOUBLE_SUP.sub(lambda m: "^{" + m.group(1) + " " + m.group(2) + "}", cur)
        cur = _DOUBLE_SUB.sub(lambda m: "_{" + m.group(1) + " " + m.group(2) + "}", cur)
    return cur


# ── 規則目錄（有序套用）──────────────────────────────────────────────────────
_TEX_RULES = (
    _fix_tag_math,
    _merge_double_scripts,
)


def normalize_tex(tex: str) -> str:
    """裸 TeX（已剝 $$ 包殼）→ 正規化。冪等。"""
    if not tex:
        return tex
    for rule in _TEX_RULES:
        tex = rule(tex)
    return tex


# ── md inline：只動 $...$ / $$...$$ 內，文字區 byte-identical ─────────────────
def _iter_dollar_regions(text: str):
    """yield (start, end, open, close, inner)。只認 reader 用的 $ / $$。"""
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            if j == -1:
                return
            yield (i, j + 2, "$$", "$$", text[i + 2:j])
            i = j + 2
        elif text[i] == "$":
            j = text.find("$", i + 1)
            if j == -1 or "\n" in text[i + 1:j]:
                i += 1
                continue
            yield (i, j + 1, "$", "$", text[i + 1:j])
            i = j + 1
        else:
            i += 1


def normalize_md_inline(md: str) -> str:
    """md 文字內的數學區正規化；文字本身原樣。冪等。"""
    if not md or "$" not in md:
        return md
    out: list[str] = []
    cursor = 0
    for start, end, od, cd, inner in _iter_dollar_regions(md):
        out.append(md[cursor:start])
        out.append(od + normalize_tex(inner) + cd)
        cursor = end
    out.append(md[cursor:])
    return "".join(out)


# ── chunk post-pass（parser 用，鏡像 assign_catalog_ids 的走訪）────────────────
_MD_FIELDS = ("md", "caption", "footnote", "title")


def _normalize_block(block: dict) -> None:
    if not isinstance(block, dict):
        return
    if block.get("t") == "eq" and isinstance(block.get("tex"), str):
        block["tex"] = normalize_tex(block["tex"])
    for f in _MD_FIELDS:
        v = block.get(f)
        if isinstance(v, str):
            block[f] = normalize_md_inline(v)


def normalize_chunk_math(chunk: dict) -> None:
    """就地正規化一個 chapter/appendix chunk 的所有 math。冪等、可重 parse。"""
    if isinstance(chunk.get("title"), str):
        chunk["title"] = normalize_md_inline(chunk["title"])
    for block in chunk.get("body", []) or []:
        _normalize_block(block)
    for prob in chunk.get("problems", []) or []:
        for sec in ("body", "solution"):
            for block in prob.get(sec, []) or []:
                _normalize_block(block)
