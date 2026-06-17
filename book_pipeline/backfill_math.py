#!/usr/bin/env python3
"""book_pipeline/backfill_math.py — 一次性把 Layer 0+1 套到全 corpus 並量測。

before（套用前）取自現有 _math_report.json（基線）。逐書重 parse（Layer 1 正規化
自動折進）→ 若有 catalog_overrides 重套（重 parse 會洗掉，順序必為 parse→apply）
→ 重新 math_validate（Layer 0 macros + Layer 1 normalize 皆 active）。輸出 per-book
before→after 與 corpus 總計。

用法：uv run python -m book_pipeline.backfill_math [slug ...]   # 不帶 = 全部
"""
from __future__ import annotations

import json
import sys
import traceback
from collections import Counter

from book_pipeline.apply_catalog_overrides import apply_overrides
from book_pipeline.math_audit import DATA
from book_pipeline.math_validate import all_slugs, node_available, validate_book
from book_pipeline.parser import parse_book


def _before_stats(slug: str) -> dict | None:
    path = DATA / slug / "parsed" / "_math_report.json"
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
        return r.get("stats")
    except Exception:
        return None


def _reparse(slug: str) -> str:
    """重 parse + （有則）重套 catalog override。回狀態字串。"""
    parse_book(slug)
    try:
        apply_overrides(slug)
        return "parsed+overrides"
    except FileNotFoundError:
        return "parsed"
    except Exception as e:  # override 壞不該擋 backfill
        return f"parsed (override ERR: {e})"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    slugs = argv or all_slugs()

    if not node_available():
        print("✗ node_modules/mathjax-full 缺：先 `npm --prefix book_pipeline install`", file=sys.stderr)
        return 1

    before = {s: _before_stats(s) for s in slugs}

    print(f"=== backfill: {len(slugs)} 書 重 parse + 重套 override ===")
    for i, slug in enumerate(slugs, 1):
        try:
            status = _reparse(slug)
        except Exception:
            print(f"  [{i}/{len(slugs)}] {slug}: PARSE FAILED")
            traceback.print_exc()
            continue
        print(f"  [{i}/{len(slugs)}] {slug}: {status}", flush=True)

    print("\n=== re-validate（Layer 0 macros + Layer 1 normalize active）===")
    tot_before_occ = tot_after_occ = 0
    tot_before_uniq = tot_after_uniq = 0
    agg_cat: Counter[str] = Counter()
    regressions: list[str] = []
    for i, slug in enumerate(slugs, 1):
        rep = validate_book(slug)
        # write_report 由 validate path 外部呼叫；這裡顯式寫
        from book_pipeline.math_validate import write_report
        write_report(slug, rep)
        if rep["status"] == "skipped":
            continue
        b = before.get(slug) or {}
        b_occ, b_uniq = b.get("bad_occ", 0), b.get("bad_unique", 0)
        a_occ, a_uniq = rep["stats"]["bad_occ"], rep["stats"]["bad_unique"]
        tot_before_occ += b_occ; tot_after_occ += a_occ
        tot_before_uniq += b_uniq; tot_after_uniq += a_uniq
        agg_cat.update(rep["by_category"])
        if a_occ > b_occ:
            regressions.append(f"{slug}: {b_occ}→{a_occ}")
        if b_occ != a_occ:
            print(f"  {slug}: bad_occ {b_occ}→{a_occ}  bad_unique {b_uniq}→{a_uniq}")

    print("\n" + "=" * 60)
    print(f"CORPUS bad_occ:    {tot_before_occ} → {tot_after_occ}")
    print(f"CORPUS bad_unique: {tot_before_uniq} → {tot_after_uniq}")
    print("residual by_category:", " ".join(f"{k}={v}" for k, v in agg_cat.most_common()))
    if regressions:
        print("\n⚠ REGRESSIONS（殘餘上升，須查）:")
        for r in regressions:
            print("  " + r)
    else:
        print("\n✓ 無任何書殘餘上升")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
