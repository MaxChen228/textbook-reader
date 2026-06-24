#!/usr/bin/env python3
"""book_pipeline.book_qc — 新進書「對不對、完不完整」的零誤判 detector（純函式）。

book_audit（唯讀報告）與 pipeline_tick 部署前 gate 共用此層。每個 detector 都是
**零誤判優先**：寧可漏（讓 sweep/人工補）也不誤旗標合法書。回傳 reason 字串或 None。

判定維度：
  partial_source   首正章號 > 1（正常書必含 ch1 或明確 front-matter）→ 殘卷/分卷下冊
  chapter_gap      正章號序列相鄰差 > GAP_THRESHOLD → 中段缺失
  companion        title 含 Study Guide / Solutions Manual / Instructor / in Focus … → 週邊/錯版本
  title_mismatch   SoT 主書名 token 大量不見於落地 title（且非 companion 已捕）→ 配錯書
  empty_chapter    body_count == 0 的章 → parse 斷裂
  no_problems_extracted  rules 宣告有題（inline/pbi）卻 parsed 0 題 → 習題整批靜默丟失

BLOCKING 子集 = 應攔下不自動上站、標 review 的硬缺陷（empty_chapter 僅警示不阻擋）。
"""
from __future__ import annotations

import re

# 週邊書/錯版本標記：落地 title 含這些字 = 不是素課本本體
COMPANION_RE = re.compile(
    r"\b(?:study\s+guide|solutions?\s+manual|instructor|workbook|"
    r"lab(?:oratory)?\s+manual|test\s+bank|in\s+focus)\b",
    re.IGNORECASE,
)
# SoT 書名標示「第二卷以後」→ 首章號 > 1 是正確接續（如 Cohen QM Vol2 自 ch8 起），不旗標。
# 阿拉伯 2–99 + 羅馬 II–XXX（≥2，長者優先避免部分匹配）；vol/part 共用同一數字 alternation。
_ROMAN_GE2 = (r"xxx|xxix|xxviii|xxvii|xxvi|xxv|xxiv|xxiii|xxii|xxi|xx|xix|xviii|xvii|xvi|"
              r"xv|xiv|xiii|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii")
_VOL_GE2_RE = re.compile(
    rf"\b(?:vol(?:ume)?|part)\.?\s*(?:[2-9]|[1-9][0-9]|{_ROMAN_GE2})\b",
    re.IGNORECASE,
)
# 章號缺口門檻：相鄰章號差 > 此值 → 中段缺失（容忍正常的小跳號）
GAP_THRESHOLD = 3
# title token 比對：兩書名重疊係數（min 分母）< 此值 → 配錯書。
# 刻意保守（近乎零重疊才旗標）——短書名常共享一個泛用 token（biology/chemistry），
# 配錯書的主防線是 companion + partial_source，title_mismatch 只當「書名南轅北轍」的補網。
TITLE_OVERLAP_MIN = 0.34

# 純語法停詞（不含實詞）：實詞如 principles/applications/introduction 是書名的區別性
# token，洗掉會讓不同書名假性重疊（如 Strang 兩本書都剩 {linear,algebra}）→ 假陰。
_STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "with",
         "vol", "volume", "edition", "ed"}

# 應攔下不自動上站的硬缺陷（命中即標 review）；empty_chapter 不在內（僅警示）
BLOCKING = ("partial_source", "chapter_gap", "companion", "title_mismatch",
            "no_problems_extracted")


def tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if t not in _STOP and len(t) > 1}


def _int_nums(chapter_nums) -> list[int]:
    return sorted(n for n in (chapter_nums or []) if isinstance(n, int))


def partial_source_reason(chapter_nums, sot_title: str = "") -> str | None:
    """首正章號 > 1 → 源檔是中段分卷/殘卷。appendix 章號不該傳進來。
    SoT 書名標示第二卷以後（Vol2/Part II…）則豁免——首章 > 1 是正確接續。"""
    nums = _int_nums(chapter_nums)
    if nums and nums[0] > 1 and not _VOL_GE2_RE.search(sot_title or ""):
        return f"partial_source(starts@{nums[0]})"
    return None


def chapter_gap_reason(chapter_nums) -> str | None:
    """正章號序列相鄰差 > GAP_THRESHOLD → 中段缺失。"""
    nums = _int_nums(chapter_nums)
    gaps = [(a, b) for a, b in zip(nums, nums[1:]) if b - a > GAP_THRESHOLD]
    if gaps:
        return "chapter_gap(" + ",".join(f"{a}->{b}" for a, b in gaps) + ")"
    return None


def companion_reason(landed_title: str) -> str | None:
    """落地 title 含週邊書/錯版本標記。"""
    return "companion" if COMPANION_RE.search(landed_title or "") else None


def title_overlap(sot_title: str, landed_title: str) -> float | None:
    """兩書名重疊係數（min 分母）：短書名是長書名子集（落地『Real Analysis』vs SoT
    『Real Analysis: Modern Techniques…』）算 1.0。任一方無 token → None（無從比對）。
    0.0 = 零個共享區別性 token = 鐵定配錯書（resolver 強閘判據）。"""
    st, lt = tokens(sot_title), tokens(landed_title)
    if not st or not lt:
        return None
    return len(st & lt) / min(len(st), len(lt))


def title_mismatch_reason(sot_title: str, landed_title: str) -> str | None:
    """重疊係數過低 → 配錯書。任一方無 token 則跳過（無從比對）。"""
    ov = title_overlap(sot_title, landed_title)
    if ov is not None and ov < TITLE_OVERLAP_MIN:
        return f"title_mismatch({ov:.0%})"
    return None


def empty_chapter_reason(chapters, appendices=None) -> str | None:
    """body_count == 0 且 problem_count == 0 的章/附錄（parse 斷裂）。僅警示，不在 BLOCKING。
    題本（內容全在 problems[]、body=0）不算空——跳過有 problems 的章。"""
    # 正章 key 是 num、附錄是 id —— 各取其識別碼
    units = list(chapters or []) + list(appendices or [])
    empties = [u.get("num") or u.get("id") for u in units
               if u.get("body_count", 0) == 0 and u.get("problem_count", 0) == 0]
    return f"empty_chapter({len(empties)})" if empties else None


def total_problem_count(book: dict) -> int:
    """全書 parsed 題數＝各章/附錄 problem_count 加總（book.json 已帶此摘要欄、零額外 I/O）。"""
    units = list(book.get("chapters") or []) + list(book.get("appendices") or [])
    return sum(int(u.get("problem_count") or 0) for u in units)


def declared_problems_missing_reason(rules, problem_count: int) -> str | None:
    """extract_rules 宣告本書有習題（inline_problems=true 或任一章 problems_block_idx 非 null）
    卻 parsed 全書 0 題 → audit 的 problem_start_re / problems_block_idx 對不上 OCR 版式
    （實證：clayden 用 NO_PROBLEM_MATCH sentinel 永不命中、devore/wald 正則對不上排版），
    整批習題靜默丟失、書照樣上站。習題是教科書核心，丟光等同殘卷 → BLOCKING。
    純理論書（inline_problems=false ∧ 全章 problems_block_idx=null）合法無題 → **不旗標**
    （誤判防線：34 本理論書/專著實證正確放行）。rules 缺/壞→None（無從判斷，fail-open）。"""
    if problem_count > 0 or not isinstance(rules, dict):
        return None
    declared = bool(rules.get("inline_problems")) or any(
        isinstance(c, dict) and c.get("problems_block_idx") is not None
        for c in (rules.get("chapters") or [])
    )
    return "no_problems_extracted" if declared else None


def detect(book: dict, sot_title: str = "", rules: dict | None = None) -> list[str]:
    """全維度旗標（含警示級）。book = corpus.load_book() 形狀；rules = extract_rules.yaml
    （None=不查習題完整性，向下相容舊呼叫）。"""
    chs = book.get("chapters") or []
    apps = book.get("appendices") or []
    nums = [c.get("num") for c in chs]
    landed = book.get("title") or ""
    flags = [
        partial_source_reason(nums, sot_title),
        chapter_gap_reason(nums),
        companion_reason(landed),
        title_mismatch_reason(sot_title, landed),
        empty_chapter_reason(chs, apps),
        declared_problems_missing_reason(rules, total_problem_count(book)),
    ]
    return [f for f in flags if f]


def blocking_reasons(flags) -> list[str]:
    """flags 中屬硬缺陷（應攔下標 review）的子集。"""
    return [f for f in flags if f.split("(")[0] in BLOCKING]
