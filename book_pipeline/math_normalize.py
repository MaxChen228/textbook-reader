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


# ── R4：收斂條件乘號 \ifmmode \times \else \texttimes \fi → \times ────────────
# 原始碼把「math/text 雙態相容」的條件乘號整段被 OCR 讀進公式（\ifmmode 判模式：
# 數學區用 \times、文字區用 \texttimes）。reader 一律數學區 → 此段恆等於 \times；
# 且 \ifmmode/\texttimes 在 MathJax 皆 undefined（整段現為殘餘）。折成 \times。冪等。
_COND_TIMES = re.compile(r"\\ifmmode\s*\\times\s*\\else\s*\\texttimes\s*\\fi")


def _fix_cond_times(tex: str) -> str:
    if r"\ifmmode" not in tex:
        return tex
    return _COND_TIMES.sub(r"\\times", tex)


# ── R5：移除 \bgroup/\egroup/\aftergroup 群組噪訊 ─────────────────────────────
# MinerU 把相位角/定界符 OCR 成 \mathopen{}\mathclose\bgroup … \aftergroup\egroup 的
# 成對群組噪訊（\bgroup/\egroup/\aftergroup 在 MathJax 全 undefined）。安全性論證：
# 凡含這些 token 的式子本就 render 失敗 → 移除只能 fail→pass 或維持，絕不讓既有過關式
# 變壞（回歸閘天然安全）。只錨定在 \bgroup/\egroup 上動手——\mathopen/\mathclose 單獨
# 是合法命令，僅在其緊鄰 \bgroup 時連帶移除（成對噪訊），不碰獨立使用。冪等（到 fixpoint）。
_NOISE_OPEN = re.compile(r"\\mathopen\s*\{\s*\}\s*\\mathclose\s*\\bgroup")
_NOISE_CLOSE = re.compile(r"\\mathclose\s*\\bgroup")
_NOISE_BARE = re.compile(r"\\(?:aftergroup|bgroup|egroup)")


def _remove_group_noise(tex: str) -> str:
    if not ("\\bgroup" in tex or "\\egroup" in tex or "\\aftergroup" in tex):
        return tex
    prev = None
    cur = tex
    while cur != prev:
        prev = cur
        cur = _NOISE_OPEN.sub("", cur)   # \mathopen{}\mathclose\bgroup（成對開）
        cur = _NOISE_CLOSE.sub("", cur)  # 殘留 \mathclose\bgroup（巢狀閉）
        cur = _NOISE_BARE.sub("", cur)   # 任何剩餘裸 token（含 \aftergroup\egroup）
    return cur


# ── R6：MathType 斜線殘體 \mathord{ \left/ \vphantom... \right. \kern - delimiterspace } → / ─
# OCR 常把「斜線表示商/相量」讀成 MathType 的定界符殘體，核心訊號固定包含
# \left/ + \vphantom + \kern - delimiterspace，常包在 \mathord/\mathbin 中。
# 這段在 MathJax 會因 \kern 維度缺失直接 hard fail；折成字面 / 可保留兩側運算元，
# 例如 {R \mathord{...} Q} → {R / Q}。只命中含 delimiterspace 的 slash 殘體，
# 正確 TeX 不會出現此 token 組合。冪等。
_SLASH_WRAPPER_CMDS = (r"\mathord", r"\mathbin")
_KERN_DELIM_RE = re.compile(r"\\kern\s*-\s*\\?delimiterspace")


def _read_braced_group(tex: str, start: int) -> tuple[str, int] | None:
    if start >= len(tex) or tex[start] != "{":
        return None
    depth = 0
    i = start
    n = len(tex)
    while i < n:
        c = tex[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return tex[start + 1:i], i + 1
        i += 1
    return None


def _is_mathtype_slash_body(body: str) -> bool:
    stripped = body.strip()
    if not stripped.startswith((r"\left/", "/")):
        return False
    return r"\vphantom" in stripped and _KERN_DELIM_RE.search(stripped) is not None


def _collapse_mathtype_slash(tex: str) -> str:
    if r"\vphantom" not in tex or "delimiterspace" not in tex:
        return tex
    out: list[str] = []
    i = 0
    n = len(tex)
    while i < n:
        matched = False
        for cmd in _SLASH_WRAPPER_CMDS:
            if not tex.startswith(cmd, i):
                continue
            j = i + len(cmd)
            while j < n and tex[j].isspace():
                j += 1
            group = _read_braced_group(tex, j)
            if not group:
                continue
            body, end = group
            if _is_mathtype_slash_body(body):
                out.append("/")
                i = end
                matched = True
                break
        if matched:
            continue
        out.append(tex[i])
        i += 1
    return "".join(out)


# ── R7：相量角度殘體 \underline{{\left/ ... \left. \right.}} → \underline{\angle ...} ──────
# 工程書常把極座標/相量角度印成 underlined angle；OCR 會讀成
# \underline{{\left/ θ \left. \right.}}（有時多包一層空 brace）。這段在 MathJax 因
# 畸形 \left...\right. + 多餘 brace 失敗；折成可渲染且語意等價的 \underline{\angle θ}。
# 只命中含 \underline + \left/ + \left. + \right. 且不含 \vphantom/delimiterspace
# 的殘體；正確 slash / underline 不碰。冪等。
def _strip_outer_braces(text: str) -> str:
    cur = text.strip()
    while cur.startswith("{") and cur.endswith("}"):
        group = _read_braced_group(cur, 0)
        if not group:
            break
        inner, end = group
        if end != len(cur):
            break
        cur = inner.strip()
    return cur


def _extract_underlined_angle(body: str) -> str | None:
    inner = _strip_outer_braces(body)
    if not inner.startswith(r"\left/"):
        return None
    if r"\left." not in inner or r"\right." not in inner:
        return None
    if r"\vphantom" in inner or "delimiterspace" in inner:
        return None
    angle = inner[len(r"\left/"):].replace(r"\left.", "").replace(r"\right.", "").strip()
    return angle or None


def _collapse_underlined_angle(tex: str) -> str:
    if r"\underline" not in tex or r"\left/" not in tex:
        return tex
    out: list[str] = []
    i = 0
    n = len(tex)
    while i < n:
        if not tex.startswith(r"\underline", i):
            out.append(tex[i])
            i += 1
            continue
        j = i + len(r"\underline")
        while j < n and tex[j].isspace():
            j += 1
        group = _read_braced_group(tex, j)
        if not group:
            out.append(tex[i])
            i += 1
            continue
        body, end = group
        angle = _extract_underlined_angle(body)
        if angle is None:
            out.append(tex[i:end])
            i = end
            continue
        out.append(r"\underline{\angle " + angle + "}")
        i = end
    return "".join(out)


# ── 規則目錄（有序套用）──────────────────────────────────────────────────────
_TEX_RULES = (
    _fix_tag_math,
    _merge_double_scripts,
    _fix_cond_times,
    _remove_group_noise,
    _collapse_mathtype_slash,
    _collapse_underlined_angle,
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
