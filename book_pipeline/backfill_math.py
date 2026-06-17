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

from book_pipeline.apply_catalog_overrides import apply_overrides as apply_catalog_overrides
from book_pipeline.apply_math_overrides import apply_overrides as apply_math_overrides
from book_pipeline.math_validate import (
    all_slugs,
    node_available,
    read_report,
    validate_book,
    write_report,
)
from book_pipeline.parser import parse_book


def _bad_locators(report: dict | None) -> dict[str, str]:
    """{locator: tex} — 展開 report.findings 的 locators。locator（chunk:位置）跨 normalize 穩定，
    故能追「同一位置」的壞→好/好→壞（tex 被規則改寫不影響位置身分）。
    注意：findings 的 locators 有 12-cap，高 occ 式會截斷 → 位置級 diff 為 best-effort 指引；
    權威淨值仍以 stats.bad_occ（精確）為準。"""
    out: dict[str, str] = {}
    for f in (report or {}).get("findings") or []:
        tex = f.get("tex")
        for loc in f.get("locators") or []:
            out.setdefault(loc, tex)
    return out


def diff_reports(before: dict | None, after: dict | None) -> dict:
    """單書 before/after _math_report → 位置級 diff（純函式，可單測）。
    fixed=壞→好；collateral=好→壞（規則誤傷，須補 override）；still_bad=壞→壞。"""
    bb, ab = _bad_locators(before), _bad_locators(after)
    bset, aset = set(bb), set(ab)
    bo = ((before or {}).get("stats") or {}).get("bad_occ", 0)
    ao = ((after or {}).get("stats") or {}).get("bad_occ", 0)
    return {
        "fixed": sorted(bset - aset),
        "collateral": sorted(aset - bset),
        "still_bad": sorted(bset & aset),
        "before_occ": bo, "after_occ": ao,
    }


def gate_verdict(before_by_slug: dict[str, dict | None],
                 after_by_slug: dict[str, dict | None]) -> dict:
    """純函式：{slug: full report} before/after → 閘判決 + 公式級彙總。
    ok ⟺ corpus 殘餘**嚴格下降** 且 **無任一書殘餘上升**（collateral 須在同一變更內 override 掉，
    否則 ok=False 並把 collateral 位置列出來餵給 override 步驟）。對齊使用者準則「新規則一定要比舊
    的好；edge case 只能靠 override」。"""
    slugs = sorted(set(before_by_slug) | set(after_by_slug))
    before_occ = after_occ = fixed_n = 0
    regressed: list[dict] = []
    collateral: list[dict] = []
    per_book: list[dict] = []
    for s in slugs:
        d = diff_reports(before_by_slug.get(s), after_by_slug.get(s))
        before_occ += d["before_occ"]; after_occ += d["after_occ"]
        fixed_n += len(d["fixed"])
        if d["after_occ"] > d["before_occ"]:
            regressed.append({"slug": s, "before": d["before_occ"], "after": d["after_occ"]})
        if d["collateral"]:
            collateral.append({"slug": s, "locators": d["collateral"]})
        if d["before_occ"] != d["after_occ"] or d["fixed"] or d["collateral"]:
            per_book.append({"slug": s, **d})
    delta = after_occ - before_occ
    return {
        "ok": delta < 0 and not regressed,
        "before_occ": before_occ, "after_occ": after_occ, "delta": delta,
        "fixed_total": fixed_n, "regressed": regressed,
        "collateral": collateral, "per_book": per_book,
    }


def _reparse(slug: str) -> str:
    """重 parse + （有則）重套 catalog/math overrides。回狀態字串。"""
    parse_book(slug)
    statuses = ["parsed"]
    try:
        apply_catalog_overrides(slug)
        statuses.append("catalog-overrides")
    except FileNotFoundError:
        pass
    except Exception as e:  # override 壞不該擋 backfill
        statuses.append(f"catalog-override ERR: {e}")
    try:
        apply_math_overrides(slug)
        statuses.append("math-overrides")
    except FileNotFoundError:
        pass
    except Exception as e:  # override 壞不該擋 backfill
        statuses.append(f"math-override ERR: {e}")
    return " + ".join(statuses)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    slugs = argv or all_slugs()

    if not node_available():
        print("✗ node_modules/mathjax-full 缺：先 `npm --prefix book_pipeline install`", file=sys.stderr)
        return 1

    # 完整 before 報告（含 findings）必須在 reparse/revalidate 覆寫前快照，才能做位置級 diff
    before_reports = {s: read_report(s) for s in slugs}

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
    after_reports: dict[str, dict] = {}
    agg_cat: Counter[str] = Counter()
    for i, slug in enumerate(slugs, 1):
        rep = validate_book(slug)
        write_report(slug, rep)
        after_reports[slug] = rep
        if rep["status"] == "skipped":
            continue
        agg_cat.update(rep["by_category"])
        d = diff_reports(before_reports.get(slug), rep)
        if d["before_occ"] != d["after_occ"]:
            print(f"  {slug}: bad_occ {d['before_occ']}→{d['after_occ']}  "
                  f"(fixed {len(d['fixed'])}, collateral {len(d['collateral'])})")

    v = gate_verdict(before_reports, after_reports)
    print("\n" + "=" * 60)
    print(f"CORPUS bad_occ: {v['before_occ']} → {v['after_occ']}  (Δ {v['delta']:+d}, fixed {v['fixed_total']})")
    print("residual by_category:", " ".join(f"{cat}={cnt}" for cat, cnt in agg_cat.most_common()))
    if v["collateral"]:
        n = sum(len(c["locators"]) for c in v["collateral"])
        print(f"\n⚠ COLLATERAL（好→壞，{n} 處／{len(v['collateral'])} 書，須補 override）:")
        for c in v["collateral"]:
            print(f"  {c['slug']}: {', '.join(c['locators'][:8])}{' …' if len(c['locators']) > 8 else ''}")
    if v["regressed"]:
        print(f"\n⚠ REGRESSIONS（殘餘上升，須查）: "
              + "  ".join(f"{r['slug']}:{r['before']}→{r['after']}" for r in v["regressed"]))
    else:
        print("\n✓ 無任何書殘餘上升")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
