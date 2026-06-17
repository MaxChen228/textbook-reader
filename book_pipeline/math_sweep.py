#!/usr/bin/env python3
"""book_pipeline/math_sweep.py — corpus 數學殘餘「逐條 override」待辦閉環。

agent 只面對兩個 subcommand（極簡，消滅原「規則+全 corpus gate」死結）：
  list   列出全 corpus render 殘餘待辦（gid + 當前壞 tex + 編譯錯誤）。唯讀。
  fix    給某 gid 一條正確 tex → 單條 render 驗證 → 寫 override → apply → 單書重驗。

為何重構（見 plan / 代碼註解）：殘餘 ~95% occ==1（單發），泛化規則零槓桿卻要跑
~30min 全 corpus gate（backfill_math 序列重 parse+重渲染全 96 書）→ 塞不進 agent
walltime → 死結、殘餘永不歸零。改成可列舉分母（findings）逐條改寫，驗證從
O(corpus)≈30min 降到 O(單式)<1ms。不污染 parsed：走 math_overrides/<slug>.json
（apply 端 guard 失配即 skip-drift、冪等）。

數據源 = 既有 _math_report.json findings（math_validate 產），list 不重跑 render。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Iterator

from book_pipeline.math_validate import iter_reports, read_report


def _gid(slug: str, tex: str) -> str:
    """全域穩定 id = <slug>:<sha1(tex)[:8]>。

    綁 tex 內容（非 findings 列表 index）→ report 重生後 findings 順序變動也不漂移。
    同書內同 tex 經 collect_formulas dedup 成單一 finding，故 (slug, tex) → 唯一 gid。"""
    h = hashlib.sha1((tex or "").encode("utf-8")).hexdigest()[:8]
    return f"{slug}:{h}"


def iter_todo(*, book: str | None = None,
              category: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """yield (slug, finding) 全 corpus 殘餘待辦。book/category 為可選過濾。

    book 指定 → 只讀該書 report（含 skipped 則空）；否則走 iter_reports（全 corpus，
    已跳過 skipped/缺檔）。順序 = report 內 findings 順序（validate_book 已按 -occ 排）。"""
    if book:
        rep = read_report(book)
        reps: list[tuple[str, dict]] = (
            [(book, rep)] if rep and rep.get("status") != "skipped" else []
        )
    else:
        reps = iter_reports()
    for slug, rep in reps:
        for f in rep.get("findings") or []:
            if category and f.get("category") != category:
                continue
            yield slug, f


def _todo_row(slug: str, f: dict[str, Any]) -> dict[str, Any]:
    """一條 finding → agent-friendly 精簡待辦列（只給做決策必需的欄位，省 token）。"""
    tex = f.get("tex") or ""
    return {
        "gid": _gid(slug, tex),
        "slug": slug,
        "category": f.get("category"),
        "display": bool(f.get("display")),
        "occ": f.get("occ", 1),
        "targets": len(f.get("targets") or []),
        "err": f.get("err") or "",
        "tex": tex,
    }


def collect_todo(*, book: str | None = None, category: str | None = None,
                 limit: int | None = None) -> list[dict[str, Any]]:
    rows = [_todo_row(s, f) for s, f in iter_todo(book=book, category=category)]
    if limit:
        rows = rows[:limit]
    return rows


def _print_human(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        d = "$$" if r["display"] else "$ "
        print(f"  {r['gid']:24s} ×{r['occ']:<3} [{r['category']:<14}] {d} {r['tex'][:90]}")
        if r["err"]:
            print(f"  {'':24s}    err: {r['err'][:100]}")
    print(f"\n總計 {len(rows)} 條待辦")


def cmd_list(a: argparse.Namespace) -> int:
    rows = collect_todo(book=a.book, category=a.category, limit=a.limit)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_human(rows)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m book_pipeline.math_sweep")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出全 corpus render 殘餘待辦（唯讀）")
    p_list.add_argument("--book", help="只列某書 slug")
    p_list.add_argument("--category", help="只列某分類（math_mode/double_script/…）")
    p_list.add_argument("--limit", type=int, help="最多列幾條")
    p_list.add_argument("--json", action="store_true", help="JSON 輸出（agent 用）")
    p_list.set_defaults(func=cmd_list)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
