#!/usr/bin/env python3
"""book_pipeline/math_validate.py — MathJax ground-truth 渲染驗證 (browser gate).

math_audit.py 管結構/overlay drift（資料層）；本模組管「reader 的 MathJax 引擎
到底渲不渲得出來」。手法：把每條公式餵進與 reader 同款引擎（render_check.js，
mathjax-full），但移除 noerrors/noundefined → 兩種 reader 失敗模式（黃底 parse
error、紅字未定義巨集）都現形成 data-mjx-error，~100% recall。

只驗 source（en），不碰 .zh.json overlay（翻譯漂移是 /translate-book 的事）。

用法：
  uv run python -m book_pipeline.math_validate <slug> [--json]
  uv run python -m book_pipeline.math_validate --all [--json]
報告寫 mineru_data/<slug>/parsed/_math_report.json（gitignore，可重生）。
缺 node_modules → graceful skip（status=skipped），絕不 crash daemon tick。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from book_pipeline import math_audit
from book_pipeline.math_audit import DATA, iter_units, _math_regions

ROOT = Path(__file__).resolve().parent.parent
RENDER_CHECK = ROOT / "book_pipeline" / "render_check.js"
MACROS_FILE = ROOT / "book_pipeline" / "math_macros.json"
NODE_HEAP_MB = 6144

UNDEF_RE = re.compile(r"[Uu]ndefined control sequence\s*(\\[A-Za-z@]+|\\.)")


def all_slugs() -> list[str]:
    return sorted(p.parent.parent.name for p in DATA.glob("*/parsed/book.json"))


def macros_version() -> str:
    try:
        return hashlib.sha1(MACROS_FILE.read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        return "none"


def locator_to_target(locator: str) -> dict[str, str]:
    """iter_units locator → apply_math_overrides 的 {chunk, selector}（橋接兩套文法）。
      ch03:body[7]                 → {chunk: ch03, selector: body[7]}
      ch03:problem[5].solution[1]  → {chunk: ch03, selector: problem:5:solution[1]}
      ch03:title                   → {chunk: ch03, selector: title}
    重複 problem num 的 occ 消歧本橋接不處理（取首個，比照 catalog selector 限制）。"""
    chunk, rest = locator.split(":", 1)
    if rest.startswith("problem[") and "]." in rest:
        num = rest[len("problem["):rest.index("].")]
        after = rest[rest.index("].") + 2:]   # 'body[2]' / 'solution[1]'
        field, idx = after.split("[", 1)
        return {"chunk": chunk, "selector": f"problem:{num}:{field}[{idx[:-1]}]"}
    return {"chunk": chunk, "selector": rest}  # body[N] / title


def _display_for(text: str, start: int, field: str) -> bool:
    """從 region 起點的 delimiter 還原 display/inline。eq field 一律 display。"""
    if field == "tex":
        return True
    if text.startswith("$$", start) or text.startswith(r"\[", start):
        return True
    return False  # $...$ 或 \(...\)


def collect_formulas(slug: str) -> dict[tuple[str, bool], dict[str, Any]]:
    """{(tex, display): {occ, locators[]}}，只取 source。"""
    out: dict[tuple[str, bool], dict[str, Any]] = {}
    for unit in iter_units(slug, overlay=False):
        for start, _end, inner in _math_regions(unit.text, unit.field):
            tex = inner.strip()
            if not tex:
                continue
            key = (tex, _display_for(unit.text, start, unit.field))
            rec = out.get(key)
            if rec is None:
                out[key] = {"occ": 1, "locators": [unit.locator],
                            "hits": [(unit.locator, unit.field)]}
            else:
                rec["occ"] += 1
                if len(rec["locators"]) < 12:
                    rec["locators"].append(unit.locator)
                    rec["hits"].append((unit.locator, unit.field))
    return out


def node_available() -> bool:
    return RENDER_CHECK.is_file() and (RENDER_CHECK.parent / "node_modules" / "mathjax-full").is_dir()


def run_render(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """items=[{i,s,d}] → {i: {ok, err}}。spawn render_check.js。"""
    if not items:
        return {}
    proc = subprocess.run(
        ["node", f"--max-old-space-size={NODE_HEAP_MB}", str(RENDER_CHECK)],
        input=json.dumps(items, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"render_check.js exit {proc.returncode}: {proc.stderr.strip()[:400]}")
    verdicts: dict[int, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        v = json.loads(line)
        verdicts[v["i"]] = v
    return verdicts


def categorize(err: str) -> tuple[str, str | None]:
    """(category, detail) — detail 對 undefined_macro 是巨集名。"""
    e = (err or "").lower()
    m = UNDEF_RE.search(err or "")
    if m:
        return "undefined_macro", m.group(1)
    if ("double superscript" in e or "double subscript" in e
            or "double exponent" in e or "double subscripts" in e):
        return "double_script", None
    if ("missing $" in e or "math mode" in e or "math-mode" in e or "not properly terminated" in e
            or "only be used in math" in e or "in restricted" in e):
        return "math_mode", None
    if "extra alignment" in e or "misplaced alignment" in e or "no more columns" in e:
        return "alignment", None
    if "missing {" in e or "missing }" in e or "extra }" in e or "extra close brace" in e or "argument" in e:
        return "missing_brace", None
    if ("\\left" in (err or "") or "extra \\left" in e or "missing \\left" in e
            or "\\right" in (err or "") or "delimiter for \\left" in e):
        return "left_right", None
    return "other", None


def validate_book(slug: str) -> dict[str, Any]:
    formulas = collect_formulas(slug)
    keys = list(formulas.keys())
    spans_total = sum(formulas[k]["occ"] for k in keys)
    if not node_available():
        return {
            "slug": slug, "status": "skipped", "reason": "node_modules/mathjax-full missing",
            "macros_version": macros_version(),
            "stats": {"spans_total": spans_total, "unique": len(keys), "bad_unique": 0, "bad_occ": 0},
            "by_category": {}, "by_macro": {}, "findings": [],
        }

    items = [{"i": idx, "s": tex, "d": display} for idx, (tex, display) in enumerate(keys)]
    verdicts = run_render(items)

    findings: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()
    by_macro: Counter[str] = Counter()
    bad_occ = 0
    for idx, (tex, display) in enumerate(keys):
        v = verdicts.get(idx)
        if v is None or v.get("ok"):
            continue
        rec = formulas[(tex, display)]
        cat, detail = categorize(v.get("err", ""))
        by_category[cat] += 1
        if cat == "undefined_macro" and detail:
            by_macro[detail] += 1
        bad_occ += rec["occ"]
        # target 附 field：field=='tex' → eq block 用 fix_eq_tex；其餘（md/caption/footnote/title）
        # → fix_inline_math 且 field 即 anchor 的 key。少了它 agent 得回讀 parsed 猜欄、填錯即 skip-drift。
        targets: list[dict[str, str]] = []
        for loc, fld in rec["hits"]:
            t = {**locator_to_target(loc), "field": fld}
            if t not in targets:
                targets.append(t)
        findings.append({
            "category": cat, "detail": detail, "err": (v.get("err") or "")[:200],
            "tex": tex, "display": display, "occ": rec["occ"],
            "locators": rec["locators"], "targets": targets,
        })
    findings.sort(key=lambda f: (-f["occ"], f["category"]))

    return {
        "slug": slug,
        "status": "pass" if not findings else "fail",
        "macros_version": macros_version(),
        "stats": {
            "spans_total": spans_total, "unique": len(keys),
            "bad_unique": len(findings), "bad_occ": bad_occ,
        },
        "by_category": dict(by_category.most_common()),
        "by_macro": dict(by_macro.most_common()),
        "findings": findings,
    }


def write_report(slug: str, report: dict[str, Any]) -> None:
    path = DATA / slug / "parsed" / "_math_report.json"
    if path.parent.is_dir():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def read_report(slug: str) -> dict[str, Any] | None:
    path = DATA / slug / "parsed" / "_math_report.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def aggregate_reports() -> dict[str, Any]:
    """讀全 corpus 既有 _math_report.json，跨書聚合成 sweep 的 pattern-mining 視圖。

    groups 依「完全相同的 (tex, display)」跨書合併、按總出現次數排序：高頻在前 = 最該泛化
    （加巨集 / normalize 規則一次清全 corpus，**只有跨書視野做得出來**）；尾端低頻 one-off
    交 per-slug override。每組附各書 slug + occ + targets（chunk/selector，可直接寫 override）。"""
    cur = macros_version()
    by_category: Counter[str] = Counter()
    by_macro: Counter[str] = Counter()
    groups: dict[tuple[str, bool], dict[str, Any]] = {}
    books = books_with_residual = bad_occ = bad_unique = 0
    stale: list[str] = []
    for slug in all_slugs():
        rep = read_report(slug)
        if not rep or rep.get("status") == "skipped":
            continue
        books += 1
        if rep.get("macros_version") and rep["macros_version"] != cur:
            stale.append(slug)
        findings = rep.get("findings") or []
        if findings:
            books_with_residual += 1
        for f in findings:
            occ = int(f.get("occ") or 0)
            bad_occ += occ
            bad_unique += 1
            cat = f.get("category") or "other"
            by_category[cat] += occ
            if cat == "undefined_macro" and f.get("detail"):
                by_macro[f["detail"]] += occ
            key = (f.get("tex") or "", bool(f.get("display")))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "tex": key[0], "display": key[1],
                    "category": cat, "detail": f.get("detail"),
                    "err": f.get("err"), "total_occ": 0, "books": [],
                }
            g["total_occ"] += occ
            g["books"].append({"slug": slug, "occ": occ, "targets": f.get("targets") or []})
    ranked = sorted(groups.values(), key=lambda g: (-g["total_occ"], g["category"]))
    return {
        "macros_version": cur,
        "corpus": {"books": books, "books_with_residual": books_with_residual,
                   "bad_occ": bad_occ, "bad_unique": bad_unique},
        "by_category": dict(by_category.most_common()),
        "by_macro": dict(by_macro.most_common()),
        "stale_books": stale,
        "groups": ranked,
    }


def _print_aggregate(agg: dict[str, Any]) -> None:
    c = agg["corpus"]
    print(f"CORPUS math residual @macros={agg['macros_version']}: "
          f"books={c['books']} with_residual={c['books_with_residual']} "
          f"bad_occ={c['bad_occ']} bad_unique={c['bad_unique']}")
    if agg["stale_books"]:
        print(f"⚠ {len(agg['stale_books'])} 書 report 用舊 macros（殘餘數可能失真，需重 validate）")
    print("by_category:", " ".join(f"{k}={v}" for k, v in agg["by_category"].items()))
    if agg["by_macro"]:
        print("top_macros:", " ".join(f"{k}={v}" for k, v in list(agg["by_macro"].items())[:20]))
    print("\n── top groups（跨書合併，高頻先泛化）──")
    for g in agg["groups"][:30]:
        slugs = sorted({b["slug"] for b in g["books"]})
        dm = "$$" if g["display"] else "$"
        print(f"  ×{g['total_occ']:<4} [{g['category']}] {len(slugs)}書  {dm}{g['tex'][:80]}{dm}")
        if g.get("err"):
            print(f"        err: {g['err'][:120]}")


def _print_human(report: dict[str, Any]) -> None:
    s = report["stats"]
    print(f"{report['slug']}: {report['status']}  "
          f"unique={s['unique']} bad_unique={s['bad_unique']} bad_occ={s['bad_occ']}")
    if report["by_category"]:
        print("  by_category:", " ".join(f"{k}={v}" for k, v in report["by_category"].items()))
    if report["by_macro"]:
        top = list(report["by_macro"].items())[:12]
        print("  top_macros:", " ".join(f"{k}={v}" for k, v in top))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m book_pipeline.math_validate")
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--aggregate", action="store_true",
                    help="讀全 corpus 既有 _math_report.json 聚合（不重跑 render；sweep 的 pattern-mining 入口）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.aggregate:
        agg = aggregate_reports()
        if args.json:
            print(json.dumps(agg, ensure_ascii=False, indent=2))
        else:
            _print_aggregate(agg)
        return 0

    slugs = all_slugs() if args.all else ([args.slug] if args.slug else [])
    if not slugs:
        ap.error("give a slug or --all")

    agg_cat: Counter[str] = Counter()
    agg_macro: Counter[str] = Counter()
    total_bad_unique = total_bad_occ = total_unique = 0
    reports: list[dict[str, Any]] = []
    for slug in slugs:
        rep = validate_book(slug)
        write_report(slug, rep)
        reports.append(rep)
        if rep["status"] == "skipped":
            print(f"{slug}: skipped ({rep['reason']})", file=sys.stderr)
            continue
        agg_cat.update(rep["by_category"])
        agg_macro.update(rep["by_macro"])
        total_bad_unique += rep["stats"]["bad_unique"]
        total_bad_occ += rep["stats"]["bad_occ"]
        total_unique += rep["stats"]["unique"]
        if not args.json:
            _print_human(rep)

    if args.json:
        print(json.dumps(reports if args.all else reports[0], ensure_ascii=False, indent=2))
    elif args.all:
        print("=" * 60)
        print(f"CORPUS: books={len(slugs)} unique={total_unique} "
              f"bad_unique={total_bad_unique} bad_occ={total_bad_occ}")
        print("by_category:", " ".join(f"{k}={v}" for k, v in agg_cat.most_common()))
        print("top_macros:", " ".join(f"{k}={v}" for k, v in agg_macro.most_common(30)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
