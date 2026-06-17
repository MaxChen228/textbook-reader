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

from book_pipeline.apply_math_overrides import (
    apply_overrides,
    finding_to_overrides,
    merge_overrides,
)
from book_pipeline.math_validate import (
    iter_reports,
    read_report,
    run_render,
    validate_book,
    write_report,
)


def _gid(slug: str, tex: str, display: bool) -> str:
    """全域穩定 id = <slug>:<sha1(display\\0tex)[:8]>。

    綁 finding 的真 dedup 鍵 (tex, display)（collect_formulas 以 (tex,_display_for) dedup）
    而非 findings 列表 index → report 重生後順序變動也不漂移。納入 display 是必要的：同書
    同一 tex 同時以 inline($...$) 與 display($$...$$) 出現 = 兩條獨立 finding，少了 display
    兩者撞同一 gid，fix 反查會取錯條、用錯 render 模式套錯 override。"""
    h = hashlib.sha1(f"{int(bool(display))}\x00{tex or ''}".encode("utf-8")).hexdigest()[:8]
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
        "gid": _gid(slug, tex, bool(f.get("display"))),
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
    if limit is not None:
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


def _find_by_gid(gid: str) -> tuple[str, dict[str, Any] | None]:
    """gid → (slug, finding)。gid 前綴即 slug，只讀該書 report 找 (tex,display) hash 命中者。
    查無 → (slug, None)（report 過期/已修/gid 打錯）。"""
    slug = gid.split(":", 1)[0]
    rep = read_report(slug)
    if not rep or rep.get("status") == "skipped":
        return slug, None
    for f in rep.get("findings") or []:
        if _gid(slug, f.get("tex") or "", bool(f.get("display"))) == gid:
            return slug, f
    return slug, None


def _gid_present(slug: str, gid: str, rep: dict[str, Any]) -> bool:
    return any(
        _gid(slug, f.get("tex") or "", bool(f.get("display"))) == gid
        for f in rep.get("findings") or []
    )


def cmd_fix(a: argparse.Namespace) -> int:
    """gid + 正確 tex → 單條 render 驗證（O(1)，取代 30min 全 corpus gate）→ 寫 override
    → apply → 單書重驗確認消失。全程 JSON 輸出（agent-friendly、省 token）。"""
    def emit(obj: dict[str, Any], rc: int) -> int:
        print(json.dumps(obj, ensure_ascii=False))
        return rc

    slug, finding = _find_by_gid(a.gid)
    if finding is None:
        return emit({"ok": False, "gid": a.gid, "slug": slug,
                     "error": "gid 查無對應待辦（已修 / report 過期 / 打錯）；先跑 `sweep list`"}, 1)

    # 單條 render 驗證 new（display 與 finding 對齊）——不過即擋下、不落地。
    verdict = run_render([{"i": 0, "s": a.new, "d": bool(finding.get("display"))}]).get(0) or {}
    if not verdict.get("ok"):
        return emit({"ok": False, "gid": a.gid, "slug": slug, "stage": "render",
                     "error": f"new tex 仍渲染失敗：{verdict.get('err') or 'unknown'}",
                     "hint": "改寫後重試（override 未落地）"}, 1)

    # 產 override（每 target 一條，共用 new）→ 併入 override file → apply 到 parsed。
    try:
        ovs = finding_to_overrides(slug, finding, a.new)
    except ValueError as e:
        return emit({"ok": False, "gid": a.gid, "slug": slug, "stage": "build_override",
                     "error": str(e)}, 1)
    merged = merge_overrides(slug, ovs)
    apply_stats = apply_overrides(slug)

    # 單書重驗（O(單書) 秒級，非全 corpus）→ 確認該 gid 從殘餘消失。
    rep = validate_book(slug)
    write_report(slug, rep)
    still = _gid_present(slug, a.gid, rep)
    out: dict[str, Any] = {
        "ok": not still, "gid": a.gid, "slug": slug,
        "overrides": merged, "apply": apply_stats,
        "book_remaining": rep.get("stats", {}).get("bad_unique", 0),
    }
    if still:
        out["warn"] = ("override 已寫但該式仍在殘餘 → 多半 selector 漂移被 apply skip-drift"
                       "（檢查 apply 結果），或 new 經套用後仍非可渲染")
    return emit(out, 0 if not still else 1)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m book_pipeline.math_sweep")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出全 corpus render 殘餘待辦（唯讀）")
    p_list.add_argument("--book", help="只列某書 slug")
    p_list.add_argument("--category", help="只列某分類（math_mode/double_script/…）")
    p_list.add_argument("--limit", type=int, help="最多列幾條")
    p_list.add_argument("--json", action="store_true", help="JSON 輸出（agent 用）")
    p_list.set_defaults(func=cmd_list)

    p_fix = sub.add_parser(
        "fix", help="把某 gid 的壞式改寫成正確 tex（render 驗證→override→apply→重驗）")
    p_fix.add_argument("--gid", required=True, help="待辦 gid（從 `sweep list` 抄）")
    p_fix.add_argument("--new", required=True,
                       help="正確 tex（eq 給裸 tex、inline 給裸 inner）。源文已毀不可渲染者改用 "
                            "`devctl math-accept`，勿硬塞語意錯的式子")
    p_fix.set_defaults(func=cmd_fix)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
