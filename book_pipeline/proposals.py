#!/usr/bin/env python3
r"""book_pipeline/proposals.py — 通用「建議系統」（任何 agent 一行 CLI 提案 → owner 決策）。

動機：pipeline 各階段（math sweep / catalog audit / sol_extract / 未來新 agent）跑到一半常會
發現「值得跨書泛化、但 autonomous 不該擅自動核心碼」的改進點。過去只有 math sweep 能手寫
markdown 進 `_proposals.md`——格式易漂移、單檔多寫者會互踩、無法程式化盤點。本模組把它升成
**通用、並行安全、schema 強制**的建議佇列：

  儲存：`book_pipeline/proposals.d/<id>.json`，一案一檔（source of truth）。各 agent 寫自己的
        id 檔（O_EXCL 認領 id）→ 多 agent 並行零爭用；jsonio 原子寫保證不留半截。
  視圖：`book_pipeline/proposals.d/_index.md`（`render` 由 JSON 生成的人類可讀總表）。

子指令：
  propose   任何 agent 一行提案（自動 id/時戳、驗詞彙、原子寫、印 id）← 核心提交入口
  list      列提案（--domain / --status 過濾）
  show      單案完整內容
  resolve   owner 記決策（--status accepted|rejected|superseded --resolution …）
  lint      schema 驗證（id 唯一/合檔名、domain/type/status 詞彙、已決議者附決議）
  render    由 JSON store 重生 _index.md 人類視圖
  check     [domain hook] 對 proposed 比對 live 殘餘（math：數 aggregate occ）
  gate      [domain hook] 回歸閘（math：backfill + 任一書殘餘上升即非零退出）

── 採納決策樹/閘/生命週期是 owner（架構師）知識，正本在此 docstring（代碼即真相），**不**放進
   agent 讀的 references/*.md（見記憶 docs-audience-separation / agent-field-of-view）──

生命週期：
  agent  `propose …`（任何階段隨手提案，status=proposed）
  owner  `list --status proposed` 盤點 → `check`（math 揪 already-resolved）→ 依決策樹定案
         →（採納碼層改動後）`gate` 全 corpus 回歸 → `resolve` 記狀態 → commit（daemon 不 commit）

決策樹（對每條 proposed）：
  1. 真符號缺定義（單一無歧義展開、跨多書）          → accepted，升 Layer 0 macro（math_macros.json）
  2. 機械 OCR pattern（確定性轉換、語意保持）         → accepted，升 Layer 1 規則（math_normalize.py）
  3. OCR 黏字偽巨集（\Nu/\muA/\cdotE，token 歧義）    → rejected(pseudo-macro-guard[+semantically-ambiguous])
                                                        改 per-slug override；偽巨集登入 test 禁收清單
  4. 已被 override 清掉 / 單書 / 不可逆亂碼            → rejected(already-resolved|single-book) 或 §8 accept

升 Layer 0/1 的強制閘（accepted macro/normalize 必過，缺一不可）：
  (a) before/after fixture 寫進 test_math_macros.py / test_math_normalize.py
  (b) 冪等 f(f(x))==f(x)；對正確式 no-op
  (c) `proposals gate` 全 corpus 回歸：任一書殘餘上升即回退
  (d) macros：對照 test_no_ocr_glue_pseudomacros 偽巨集邊界；改 math_macros.json 必跑 build.gen_macros
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from book_pipeline.jsonio import atomic_write_json, read_json

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "book_pipeline" / "proposals.d"
INDEX = STORE / "_index.md"

STATUSES = {"proposed", "accepted", "rejected", "superseded"}
RESOLVED = {"accepted", "rejected", "superseded"}
REJECT_CODES = {
    "pseudo-macro-guard", "already-resolved", "semantically-ambiguous",
    "single-book", "unsafe", "superseded", "out-of-scope",
}
ID_RE = re.compile(r"^P-\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9_-]*$")

# ── domain registry：加 domain = 加一筆。types=該 domain 合法升級標的；checker=live 比對 hook ──
DOMAINS: dict[str, dict[str, Any]] = {
    "math": {"types": {"macro", "normalize-rule", "override"}, "checker": "math"},
    "catalog": {"types": {"override", "rule"}, "checker": None},
}

FIELDS = ("evidence", "proposal", "risk", "disposition")  # 散文欄位


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slugify(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return out[:32] or "x"


def _path(pid: str) -> Path:
    return STORE / f"{pid}.json"


def load_all() -> list[dict[str, Any]]:
    if not STORE.is_dir():
        return []
    out = []
    for p in sorted(STORE.glob("P-*.json")):
        d = read_json(str(p), default=None)
        if isinstance(d, dict):
            out.append(d)
    return out


def _claim_id(base: str) -> str:
    """O_EXCL 認領唯一 id（並行 agent 同 base 互不覆蓋）。回傳已建空檔的 id。"""
    STORE.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        pid = base if n == 1 else f"{base}-{n}"
        try:
            fd = os.open(str(_path(pid)), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return pid
        except FileExistsError:
            continue
    raise RuntimeError(f"無法配置 id（{base}-* 已滿）")


def propose(*, domain: str, type_: str, title: str, slug: str | None = None,
            detect: list[str] | None = None, source: str = "agent",
            **prose: str) -> str:
    """建一條提案 → 回傳 id。schema 不合即 raise（CLI 層轉錯誤訊息）。"""
    if domain not in DOMAINS:
        raise ValueError(f"未知 domain {domain!r}（已註冊：{sorted(DOMAINS)}）")
    if type_ not in DOMAINS[domain]["types"]:
        raise ValueError(f"domain={domain} 的 type {type_!r} 不在 {sorted(DOMAINS[domain]['types'])}")
    if not title.strip():
        raise ValueError("title 不可空")
    pid = _claim_id(f"P-{_today()}-{_slugify(slug or title)}")
    rec = {
        "id": pid, "domain": domain, "type": type_, "status": "proposed",
        "title": title.strip(), "detect": detect or [], "source": source,
        "resolution": "", "created": _now(), "updated": _now(),
    }
    for k in FIELDS:
        rec[k] = (prose.get(k) or "").strip()
    atomic_write_json(str(_path(pid)), rec, indent=2)
    return pid


def resolve(pid: str, *, status: str, resolution: str, disposition: str | None = None) -> dict:
    rec = read_json(str(_path(pid)), default=None)
    if not isinstance(rec, dict):
        raise ValueError(f"找不到提案 {pid}")
    if status not in STATUSES:
        raise ValueError(f"狀態 {status!r} 不在 {sorted(STATUSES)}")
    rec["status"] = status
    rec["resolution"] = resolution.strip()
    if disposition is not None:
        rec["disposition"] = disposition.strip()
    rec["updated"] = _now()
    atomic_write_json(str(_path(pid)), rec, indent=2)
    return rec


def lint(recs: list[dict[str, Any]]) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    for r in recs:
        pid = r.get("id", "")
        tag = f"«{(r.get('title') or pid)[:36]}»"
        if not pid or not ID_RE.match(pid):
            errs.append(f"{tag}: id 缺/格式違反 P-YYYY-MM-DD-slug：{pid!r}"); continue
        if pid in seen:
            errs.append(f"{tag}: id 重複：{pid}")
        seen.add(pid)
        if not _path(pid).is_file():
            errs.append(f"{tag}: id 與檔名不符（應為 {pid}.json）")
        dom = r.get("domain")
        if dom not in DOMAINS:
            errs.append(f"{tag}: domain {dom!r} 未註冊")
        elif r.get("type") not in DOMAINS[dom]["types"]:
            errs.append(f"{tag}: type {r.get('type')!r} 不屬 domain={dom}")
        st = r.get("status")
        if st not in STATUSES:
            errs.append(f"{tag}: status {st!r} 不在 {sorted(STATUSES)}")
        res = (r.get("resolution") or "").strip()
        if st in RESOLVED and not res:
            errs.append(f"{tag}: status={st} 須附 resolution")
        if st == "rejected" and res:
            bad = [c for c in re.split(r"[ ,+，、]+", res) if c and c not in REJECT_CODES]
            if bad:
                errs.append(f"{tag}: rejected 理由代碼 {bad} 不在 {sorted(REJECT_CODES)}")
    return errs


# ── math domain hooks ────────────────────────────────────────────────────────
def _math_live_occ(detect: list[str], groups: list[dict[str, Any]]) -> int:
    if not detect:
        return -1
    return sum(g.get("total_occ", 0) for g in groups
               if any(tok in (g.get("tex") or "") for tok in detect))


# ── render（JSON store → _index.md 人類視圖）──────────────────────────────────
def render(recs: list[dict[str, Any]]) -> str:
    lines = [
        "# 建議佇列（proposals）— 由 JSON store 自動生成，請勿手改",
        "",
        "正本 = `book_pipeline/proposals.d/<id>.json`（一案一檔）。新增/改狀態一律走 CLI：",
        "`uv run python -m book_pipeline.proposals {propose|resolve|list|check|gate}`。",
        "決策樹/閘/生命週期（owner 知識）正本：`book_pipeline/proposals.py` 模組 docstring。",
        "",
    ]
    by_dom: dict[str, list[dict]] = {}
    for r in recs:
        by_dom.setdefault(r.get("domain", "?"), []).append(r)
    order = {"proposed": 0, "accepted": 1, "rejected": 2, "superseded": 3}
    for dom in sorted(by_dom):
        rs = sorted(by_dom[dom], key=lambda r: (order.get(r.get("status"), 9), r.get("id", "")))
        n_prop = sum(1 for r in rs if r.get("status") == "proposed")
        lines.append(f"## domain: {dom}  （{len(rs)} 條；proposed={n_prop}）\n")
        for r in rs:
            lines.append(f"### {r.get('id')} — {r.get('title')}")
            meta = f"- {r.get('status')} | type={r.get('type')} | source={r.get('source')}"
            if r.get("detect"):
                meta += f" | 偵測={' '.join(r['detect'])}"
            lines.append(meta)
            if r.get("resolution"):
                lines.append(f"- 決議：{r['resolution']}")
            for k, label in (("disposition", "處置"), ("evidence", "證據"),
                             ("proposal", "提議"), ("risk", "風險")):
                if r.get(k):
                    lines.append(f"- {label}：{r[k]}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_index(recs: list[dict[str, Any]] | None = None) -> None:
    recs = load_all() if recs is None else recs
    STORE.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(render(recs), encoding="utf-8")


# ── CLI ──────────────────────────────────────────────────────────────────────
def cmd_propose(a: argparse.Namespace) -> int:
    try:
        pid = propose(domain=a.domain, type_=a.type, title=a.title, slug=a.slug,
                      detect=a.detect or [], source=a.source,
                      evidence=a.evidence, proposal=a.proposal,
                      risk=a.risk, disposition=a.disposition)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    write_index()
    print(pid)
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    recs = [r for r in load_all()
            if (not a.domain or r.get("domain") == a.domain)
            and (not a.status or r.get("status") == a.status)]
    if not recs:
        print("（無符合提案）"); return 0
    w = max(len(r.get("id", "")) for r in recs)
    for r in sorted(recs, key=lambda r: (r.get("domain", ""), r.get("status", ""), r.get("id", ""))):
        print(f"  {r.get('id',''):<{w}}  [{r.get('status','?'):<10}] "
              f"{r.get('domain','?'):<8} {r.get('type','?'):<14} {r.get('title','')}")
    print(f"\n共 {len(recs)} 條；proposed={sum(1 for r in recs if r.get('status')=='proposed')}")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    rec = read_json(str(_path(a.id)), default=None)
    if not isinstance(rec, dict):
        print(f"❌ 找不到 {a.id}", file=sys.stderr); return 2
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_resolve(a: argparse.Namespace) -> int:
    try:
        rec = resolve(a.id, status=a.status, resolution=a.resolution, disposition=a.disposition)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr); return 2
    errs = lint([rec])
    if errs:
        print("❌ 決策後 schema 不合：" + "；".join(errs), file=sys.stderr); return 2
    write_index()
    print(f"✓ {a.id} → {a.status}（{a.resolution}）")
    return 0


def cmd_lint(a: argparse.Namespace) -> int:
    errs = lint(load_all())
    # 順帶驗 _index.md 與 store 同步（生成檔不得手改漂移）
    if INDEX.is_file() and INDEX.read_text(encoding="utf-8") != render(load_all()):
        errs.append("_index.md 與 store 不同步 → 跑 `proposals render`")
    if errs:
        print("❌ proposals lint 失敗：")
        for e in errs:
            print(f"  {e}")
        return 1
    print(f"✓ proposals lint 通過（{len(load_all())} 條）")
    return 0


def cmd_render(a: argparse.Namespace) -> int:
    write_index()
    print(f"✓ 已生成 {INDEX.relative_to(ROOT)}")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    from book_pipeline import math_validate as mv
    agg = mv.aggregate_reports()
    groups = agg.get("groups", [])
    props = [r for r in load_all() if r.get("status") == "proposed" and r.get("domain") == "math"]
    if not props:
        print("無 math proposed 提案。"); return 0
    print(f"live aggregate @macros={agg.get('macros_version')}  corpus bad_occ={agg['corpus']['bad_occ']}\n")
    for r in props:
        occ = _math_live_occ(r.get("detect") or [], groups)
        flag = ("（無偵測子，手動 review）" if occ == -1
                else "→ already-resolved 候選（live 0 occ）" if occ == 0
                else f"→ live {occ} occ")
        print(f"  {r['id']:<30} [{r.get('type',''):<14}] {flag}")
    return 0


def cmd_gate(a: argparse.Namespace) -> int:
    """真實數據閘（取代人審）：snapshot 完整 before 報告 → backfill 重 parse/套 override/重渲染
    → 公式級 gate_verdict。採用準則（使用者定）：**嚴格淨降 且 無任一書殘餘上升**——任何規則必有
    edge case（好→壞 collateral），collateral 不丟規則、須在同一變更內補 override 掉，gate 才過。"""
    from book_pipeline import math_validate as mv
    from book_pipeline.backfill_math import gate_verdict
    if not mv.node_available():
        print("⚠ node_modules/mathjax-full 缺 → 無法驗證，gate 跳過（非通過）。", file=sys.stderr)
        return 0
    slugs = a.slug or mv.all_slugs()
    before = {s: mv.read_report(s) for s in slugs}
    tot_before = sum((before[s] or {}).get("stats", {}).get("bad_occ", 0) for s in slugs)
    print(f"gate: 回歸前 corpus 殘餘 {tot_before} occ（{len(slugs)} 書）→ 跑 backfill_math…")
    rc = subprocess.run(["uv", "run", "python", "-m", "book_pipeline.backfill_math", *a.slug],
                        cwd=ROOT).returncode
    if rc != 0:
        print(f"❌ backfill_math 退出碼 {rc}", file=sys.stderr); return rc
    after = {s: mv.read_report(s) for s in slugs}
    v = gate_verdict(before, after)
    print(f"\ngate: 回歸後 corpus 殘餘 {v['after_occ']} occ（Δ {v['delta']:+d}, fixed {v['fixed_total']}）")
    if v["collateral"]:
        n = sum(len(c["locators"]) for c in v["collateral"])
        print(f"⚠ collateral 好→壞 {n} 處（須補 override 後重跑）:")
        for c in v["collateral"]:
            print(f"    {c['slug']}: {', '.join(c['locators'][:8])}{' …' if len(c['locators']) > 8 else ''}")
    if v["regressed"]:
        print(f"❌ {len(v['regressed'])} 書殘餘上升 → 補 override 或回退：")
        for r in sorted(v["regressed"], key=lambda r: r["before"] - r["after"]):
            print(f"    {r['slug']}: {r['before']} → {r['after']} (+{r['after'] - r['before']})")
        return 1
    if v["delta"] >= 0:
        print("❌ 無淨改善（Δ≥0）→ 此變更不採用（新規則一定要比舊的好）。")
        return 1
    print(f"✓ 嚴格淨降 {v['delta']:+d}、無書上升 — 閘通過（採用）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m book_pipeline.proposals")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="任何 agent 一行提案")
    p.add_argument("--domain", required=True, choices=sorted(DOMAINS))
    p.add_argument("--type", required=True, help="升級標的（依 domain，見 lint）")
    p.add_argument("--title", required=True)
    p.add_argument("--slug", help="id 用 slug（預設由 title 推導）")
    p.add_argument("--detect", nargs="*", help="供 check 數 live occ 的 token")
    p.add_argument("--source", default="agent", help="提案者（agent 標籤 / owner）")
    p.add_argument("--evidence", default="")
    p.add_argument("--proposal", default="")
    p.add_argument("--risk", default="")
    p.add_argument("--disposition", default="")
    p.set_defaults(fn=cmd_propose)

    p = sub.add_parser("list", help="列提案")
    p.add_argument("--domain", choices=sorted(DOMAINS))
    p.add_argument("--status", choices=sorted(STATUSES))
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="單案完整內容")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("resolve", help="owner 記決策")
    p.add_argument("id")
    p.add_argument("--status", required=True, choices=sorted(RESOLVED))
    p.add_argument("--resolution", required=True, help="accepted→規則名/commit；rejected→理由代碼")
    p.add_argument("--disposition", help="補充去向（如 per-slug override）")
    p.set_defaults(fn=cmd_resolve)

    sub.add_parser("lint", help="schema 驗證").set_defaults(fn=cmd_lint)
    sub.add_parser("render", help="重生 _index.md").set_defaults(fn=cmd_render)
    sub.add_parser("check", help="[math] proposed 比對 live 殘餘").set_defaults(fn=cmd_check)
    p = sub.add_parser("gate", help="[math] 回歸閘（backfill + 殘餘不得上升）")
    p.add_argument("slug", nargs="*")
    p.set_defaults(fn=cmd_gate)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
