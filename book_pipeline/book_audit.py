#!/usr/bin/env python3
"""book_pipeline.book_audit — 新進書「是不是對的、完整的那本書」唯讀體檢。

現有管線只驗「能 parse / 能渲染」，從不驗書本身對不對、完不完整 → campbell 抓成
學習指南、petrucci 少半本都靜默上站。本工具把這類人工 scorecard 固化成可重複的批次
報告：純讀 corpus + booklists SoT，不改任何資料、不 gate、零 LLM。

零誤判訊號（phase 2 會抽成共用 detector + gate）：
  partial_source   首章號 ≠ 1（正常書必含 ch1 或明確 front-matter）→ 殘卷/分卷下冊
  chapter_gap      章號序列有大缺口 → 中段缺失
  companion        落地 title 含 Study Guide / Solutions Manual / Instructor / in Focus …
  title_mismatch   SoT 主書名 token 大量不見於落地 title → 配錯書
  empty_chapter    body_count == 0 的章 → parse 斷裂

用法：
  uv run python -m book_pipeline.book_audit <slug> [slug ...]   # 指定批次
  uv run python -m book_pipeline.book_audit                     # 全部已上站書
  uv run python -m book_pipeline.book_audit --json <slug ...>   # 結構化輸出
"""
from __future__ import annotations

import json
import re
import sys

from textbooks import corpus
from book_pipeline import booklists as bl
from book_pipeline import math_validate as mv

# 週邊書/錯版本標記：落地 title 含這些字 = 不是素課本本體
COMPANION_RE = re.compile(
    r"\b(?:study\s+guide|solutions?\s+manual|instructor|workbook|"
    r"lab(?:oratory)?\s+manual|test\s+bank|in\s+focus)\b",
    re.IGNORECASE,
)
# 章號缺口門檻：序列相鄰章號差 > 此值 → 視為中段缺失（容忍正常的非連續編號如卷分界）
GAP_THRESHOLD = 3
_STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "with", "vol", "volume",
         "edition", "ed", "principles", "modern", "applications", "introduction",
         "fundamentals", "general"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP and len(t) > 1}


def _sot_map() -> dict[str, dict]:
    """booklists SoT：slug → {title, author}（書單點名要的那本）。"""
    return {t["slug"]: t for t in bl.targets()}


def audit_book(slug: str, sot: dict | None = None, residual: dict | None = None) -> dict:
    """單本唯讀體檢 → 結構化訊號 + flags。不改任何資料。"""
    book = corpus.load_book(slug)
    if not book:
        return {"slug": slug, "flags": ["load_fail"], "missing": True}
    sot = sot if sot is not None else _sot_map().get(slug)
    residual = residual if residual is not None else mv.residual_by_book()

    chs = book.get("chapters") or []
    apps = book.get("appendices") or []
    nums = [c.get("num") for c in chs if isinstance(c.get("num"), int)]
    landed_title = book.get("title") or ""
    sot_title = (sot or {}).get("title") or ""

    flags: list[str] = []

    # partial_source / chapter_gap：只看正章（appendix 不算）
    first = min(nums) if nums else None
    if first is not None and first > 1:
        flags.append(f"partial_source(starts@{first})")
    gaps = []
    for a, b in zip(sorted(nums), sorted(nums)[1:]):
        if b - a > GAP_THRESHOLD:
            gaps.append((a, b))
    if gaps:
        flags.append("chapter_gap" + ",".join(f"{a}->{b}" for a, b in gaps))

    # companion / title_mismatch（需有 SoT 才能比）
    if COMPANION_RE.search(landed_title):
        flags.append("companion")
    if sot_title:
        st, lt = _tokens(sot_title), _tokens(landed_title)
        if st:
            overlap = len(st & lt) / len(st)
            if overlap < 0.5:
                flags.append(f"title_mismatch({overlap:.0%})")

    # empty_chapter
    empties = [c.get("num") for c in (chs + apps) if c.get("body_count", 0) == 0]
    if empties:
        flags.append(f"empty_chapter({len(empties)})")

    def S(k):
        return sum(c.get(k, 0) for c in chs) + sum(c.get(k, 0) for c in apps)

    return {
        "slug": slug,
        "title": landed_title,
        "sot_title": sot_title,
        "edition": book.get("edition"),
        "n_ch": len(chs),
        "n_app": len(apps),
        "ch_nums": nums,
        "body": S("body_count"),
        "eq": S("equation_count"),
        "fig": S("figure_count"),
        "table": S("table_count"),
        "prob": S("problem_count"),
        "empty_chapters": empties,
        "math_residual": int(residual.get(slug, 0)),
        "flags": flags,
        "missing": False,
    }


def _all_deployed_slugs() -> list[str]:
    return sorted(b["slug"] for b in corpus.list_books())


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    as_json = "--json" in argv
    slugs = [a for a in argv if not a.startswith("--")]
    if not slugs:
        slugs = _all_deployed_slugs()

    sot, residual = _sot_map(), mv.residual_by_book()
    rows = [audit_book(s, sot.get(s), residual) for s in slugs]

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 1 if any(r["flags"] for r in rows) else 0

    print(f"{'slug':30} {'ch/app':>7} {'body':>6} {'eq':>5} {'math':>5}  flags")
    suspects = 0
    for r in rows:
        if r.get("missing"):
            print(f"{r['slug']:30}  LOAD-FAIL")
            suspects += 1
            continue
        flagstr = "  ".join(r["flags"]) if r["flags"] else "✓"
        if r["flags"]:
            suspects += 1
        chapp = f"{r['n_ch']}/{r['n_app']}"
        print(f"{r['slug']:30} {chapp:>7} {r['body']:>6} "
              f"{r['eq']:>5} {r['math_residual']:>5}  {flagstr}")
    print(f"\n{len(rows)} 本掃描 · {suspects} 本有旗標")
    return 1 if suspects else 0


if __name__ == "__main__":
    sys.exit(main())
