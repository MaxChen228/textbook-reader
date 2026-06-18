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
import base64
import datetime
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterator

from book_pipeline.apply_math_overrides import (
    OVERRIDE_DIR,
    apply_overrides,
    finding_to_overrides,
    merge_overrides,
)
from book_pipeline.math_validate import (
    iter_reports,
    node_available,
    read_report,
    run_render,
    validate_book,
    write_report,
)


def _log(msg: str) -> None:
    """進度印 stderr（stdout 留給 JSON 結果，agent/管線解析）。"""
    print(msg, file=sys.stderr, flush=True)


# ── 可觀測性：聚合進度 live + 歷史回溯（寫進 dev/，nginx 直服務、dev 頁相對 fetch；gitignore）──
# 比照 dev/workers.json / dev/agent_history 既有模式。live=聚合進度快照（schema 2：在工作+多快，
# 每批完成重寫），history=append-only jsonl（每批一條判決記錄，prune 末 200 批，供 `sweep raw` 取證）。
_ROOT = Path(__file__).resolve().parent.parent
_DEV_DIR = _ROOT / "dev"
_LIVE_PATH = _DEV_DIR / "math_live.json"
_HISTORY_PATH = _DEV_DIR / "math_history.jsonl"
_HISTORY_KEEP = 200


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, obj: Any) -> None:
    """同目錄 temp + os.replace 原子落盤（dev 頁 fetch 永不讀到半截）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # 可觀測性失敗絕不拖垮 batch 主流程
        pass


def _live_write(rec: dict[str, Any]) -> None:
    _atomic_write_json(_LIVE_PATH, rec)


def _history_append(rec: dict[str, Any]) -> None:
    """append 一條完成記錄 + prune 末 _HISTORY_KEEP 批（讀全檔重寫，量小可接受）。"""
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if _HISTORY_PATH.exists():
            lines = _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(rec, ensure_ascii=False))
        lines = lines[-_HISTORY_KEEP:]
        tmp = _HISTORY_PATH.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, _HISTORY_PATH)
    except Exception:
        pass


def _gid(slug: str, tex: str, display: bool) -> str:
    """全域穩定 id = <slug>:<sha1(display\\0tex)[:8]>。

    綁 finding 的真 dedup 鍵 (tex, display)（collect_formulas 以 (tex,_display_for) dedup）
    而非 findings 列表 index → report 重生後順序變動也不漂移。納入 display 是必要的：同書
    同一 tex 同時以 inline($...$) 與 display($$...$$) 出現 = 兩條獨立 finding，少了 display
    兩者撞同一 gid，fix 反查會取錯條、用錯 render 模式套錯 override。"""
    h = hashlib.sha1(f"{int(bool(display))}\x00{tex or ''}".encode("utf-8")).hexdigest()[:8]
    return f"{slug}:{h}"


# ── 語意守門（render 守門之上的第二道閘）──────────────────────────────────
# render gate 只驗「MathJax 能否編譯」；但語意空洞的字串是**合法 LaTeX、照樣編譯過**：
# ``（空字串）、`\mathrm{~~}`（純 nbsp 空白）、`{\let\mathbf\relax \mathbf{}\mathbf{}…}`
# （把 \mathbf 重定義成空、塞空盒中和垃圾）全部 render ok=true（實測）。LLM 面對「源文已毀、
# 無公式可救」時的局部理性就是吐這種能 render 的空殼/中和式蒙混過關——實證：cohen ch14 整條
# 改寫成 `$\mathrm{~~}$`（reader 顯示空白）、dummit ch10 用 \let 中和成一排空 \mathbf{}。這些
# 都過了 render gate、落地成「已修」的謊（比留 OCR 殘體更糟：殘體會 render error 示警，空殼是靜默）。
# 語意 gate 攔下 → 不落地（回流重試池；終究留作可見殘餘或交 §8 math-accept，絕不偽裝成已修）。
#
# 只攔「零誤殺」的兩類：空殼（去格式/結構後無任何內容字元）、TeX 程式原語（\let \def…無內容用途）。
# 退化重複（\alpha×30）**刻意不納入**確定性 gate——與合法資料表欄位規格 `{c c c c}`、化學濃度
# `[\mathrm{B}]/[\mathrm{B}]` 的重複糾纏、易誤殺；那類交「源文已毀 → math-accept 誠實終態」處理。
_TEX_PRIMITIVE = re.compile(
    r"\\(?:let|def|edef|gdef|xdef|catcode|relax|csname|expandafter|futurelet"
    r"|newcommand|renewcommand|providecommand)\b")
_CTRL_SEQ = re.compile(r"\\[A-Za-z@]+")
# 格式/字體/間距/結構 wrapper：本身**不承載內容**，剝掉不該算「有東西」。改採黑名單制——
# 只列這些已知無內容的控制序列當「可刪」，其餘任何 \xxx 一律當內容（佔位 §）。
# 為何翻成黑名單（原白名單制是 bug 源）：白名單只收了幾十個希臘字母/算子，凡白名單外的合法
# 符號（`\complement` `\upharpoonright` `\nexists` `\boxtimes`…）都被當格式刪光 → core 變空
# → 合法式被誤判 empty_shell 永久退回、無法收斂（實證：brown_lemay `\complement{\upharpoonright}`）。
# 黑名單只需窮舉「確定無內容」的 wrapper（封閉小集），新符號自動歸內容、零誤殺。
_FORMAT_CTRL = re.compile(
    r"\\(?:math(?:rm|bf|it|sf|tt|cal|frak|bb|scr|normal)|boldsymbol|pmb|mathversion"
    r"|text(?:rm|bf|it|sf|tt|normal|up|sc|md|color)?|mbox|hbox|operatorname|mathop"
    r"|left|right|middle|bigl|bigr|Bigl|Bigr|biggl|biggr|Biggl|Biggr|big|Big|bigg|Bigg"
    r"|displaystyle|textstyle|scriptstyle|scriptscriptstyle|limits|nolimits"
    r"|begin|end|phantom|hphantom|vphantom|smash|strut|mathstrut|null|substack"
    r"|hspace|vspace|kern|mkern|mskip|raisebox"
    r"|quad|qquad|space|nobreakspace|thinspace|negthinspace|medspace|thickspace)\b")


def semantic_reason(new: str) -> str | None:
    r"""render ok 後的語意守門：回 reject 原因（None=通過）。純函式、零磁碟、可單測。
    只攔零誤殺兩類；合法短式（$N_2$ $\sqrt2$ $\alpha=1$ $\mu\text{A}$ $\complement{\upharpoonright}$）全放行。"""
    s = (new or "").strip()
    for a, b in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if s.startswith(a) and s.endswith(b) and len(s) >= len(a) + len(b):
            s = s[len(a):len(s) - len(b)].strip()
            break
    if _TEX_PRIMITIVE.search(s):
        return "tex_primitive"
    core = _FORMAT_CTRL.sub("", s)                  # 已知無內容的格式/字體/間距 wrapper → 刪
    core = _CTRL_SEQ.sub("§", core)                 # 其餘任何控制序列 → 視為內容（佔位 §）
    core = re.sub(r"[\^_{}&~\\,;:!\s]", "", core)   # 結構/nbsp/空白/標點控制 → 刪
    if not core:
        return "empty_shell"
    return None


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

    # 語意守門：render 過但空殼/含 TeX 原語 → 擋下不落地（見 semantic_reason）。
    if (sem := semantic_reason(a.new)):
        return emit({"ok": False, "gid": a.gid, "slug": slug, "stage": "semantic",
                     "error": f"new 通過 render 但語意空洞（{sem}）→ 擋下不落地",
                     "hint": "源文已毀不可救者用 `devctl math-accept`，勿塞空殼/中和式蒙混"}, 1)

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
    if rep.get("status") == "skipped":
        return emit({"ok": False, "gid": a.gid, "slug": slug, "stage": "revalidate",
                     "warn": "node 不可用，重驗 skipped → 無法確認是否清掉（override 已寫）"}, 1)
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


# ── sweep batch：批量打自架 LLM（預設 gpt-5.4）逐條改寫，render 守門 ──────
#
# 經濟學（實測）：reasoning 模型主成本 completion 隨條數線性、batch 攤不平；batch 唯一省的
# 是 input overhead 的零頭。故 N 不為「省 token」衝大——N=40 是 wall-clock/重試粒度/不爆
# completion 的甜蜜點。真正的省在：payload 精簡（只送 i/tex/err）、render 本機守門（<1ms，
# 不過不落地）、帶 retry 池≤2 輪、長式分流。
#
# 模型選 5.4（與 qc/audit 派工的 codex 家族家規一致、池子白名單內）：實測對「信心型幻覺」遠
# 穩於 5.3-codex-spark——spark 把化學 N₂ 的 OCR 殘體 `\Nu_2` 修成物理頻率 `\nu_2`（小寫、能
# render、語意全錯，閘攔不到），5.4/5.5 正確還原 `N_2`；5.4 延遲較 5.5 省 ~35%、品質追平，故定
# 為預設。env BOOK_PIPELINE_MATH_MODEL 可運維臨時凌駕。

DEFAULT_MODEL = os.environ.get("BOOK_PIPELINE_MATH_MODEL", "gpt-5.4")
# 強約束 + few-shot：render gate 只能擋確定性空殼/原語，攔不到「信心型幻覺」（把噪音編成
# \mathrm{width} 這種看似合法卻無中生有的內容）。源頭治理在 prompt——明令禁止臆造/空殼/中和，
# 並給「源文已毀」一個誠實出口 unrecoverable（→ 系統標 math-accept 終態），取代「假修蒙混」。
# token input 成本不計（攤平在 render 守門前、且品質遠重於零頭 token）。
_LLM_SYS = (
    "你是嚴謹的 LaTeX OCR 修復器。輸入每條為一個 JSON 物件 "
    '{"i":序號,"err":MathJax編譯錯誤,"tex":壞tex}——tex 是教科書數學式經 OCR 後的殘體，'
    "err 是它丟進 MathJax 的錯誤。任務：在**不臆造、不改變數學語意**的前提下，回最小修正、"
    "可被 MathJax 渲染的正確 tex。\n\n"
    "鐵律（違反即為破壞資料，比不修更糟）：\n"
    "1. 只做最小必要修正：補漏的 {}、修雙上下標（a^b^c→a^{bc}）、補 OCR 誤切的 \\left/\\right 配對。"
    "保留所有原有符號、上下標、結構，不增不減語意。\n"
    "2. 嚴禁臆造內容：看不懂的符號別猜成英文單字或無關符號。OCR 把 \\omega 切成 'w'、有把握可還原 "
    "\\omega；但**絕不可**把一團噪音編成 \\mathrm{width} 這種「看似合法卻無中生有」的內容。\n"
    "3. 嚴禁空殼蒙混：絕不回 \\mathrm{~~}、空 {}、$$ $$、或用 \\let/\\def/\\relax 把巨集中和成空白"
    "來「騙過渲染」。能渲染但語意空洞＝製造靜默錯誤，明令禁止（系統另有守門會擋下並退回）。\n"
    "4. 源文已毀就誠實說：若 tex 已是不可逆 OCR 噪音（大段重複 ^{\\mathrm{~~}}、整排空 \\mathbf{}、"
    "字符堆疊到無法辨識原式），**不要硬修也不要編造**，回 {\"i\":序號,\"unrecoverable\":true}——"
    "系統會標為「源文已毀」誠實終態，遠優於塞假式子。\n"
    "5. unrecoverable 是最後手段、門檻要高：只要還能辨識原式骨架（分數/積分/矩陣/求和/上下標…）就修，不要逃。\n\n"
    "輸出：逐條只回 JSONL，每行一物件，二選一：\n"
    '  {"i":序號,"tex":"<正確 tex>"}      ← 修好了\n'
    '  {"i":序號,"unrecoverable":true}     ← 源文已毀、無可救\n'
    "不要 markdown 圍欄、不要解釋、不要多餘字。\n\n"
    "範例：\n"
    '  輸入 {"i":0,"err":"Double exponent","tex":"e^i\\omega t^2"}\n'
    '  輸出 {"i":0,"tex":"e^{i\\omega t^2}"}\n'
    '  輸入 {"i":1,"err":"Missing close brace","tex":"\\frac{a}{b"}\n'
    '  輸出 {"i":1,"tex":"\\frac{a}{b}"}\n'
    '  輸入 {"i":2,"err":"Double subscript","tex":"\\sum_{n=1^\\infty a_n"}\n'
    '  輸出 {"i":2,"tex":"\\sum_{n=1}^{\\infty} a_n"}\n'
    '  輸入 {"i":3,"err":"...","tex":"^{\\mathrm{~~}}{}^{\\mathrm{~~}}{}^{\\mathrm{~~}}{}^{\\mathrm{~~}}"}\n'
    '  輸出 {"i":3,"unrecoverable":true}   （整串只剩重複空白佔位，原式不可逆）\n'
    '  輸入 {"i":4,"err":"...","tex":"\\mathbf{}\\mathbf{}\\mathbf{}\\mathbf{}"}\n'
    '  輸出 {"i":4,"unrecoverable":true}   （一排空盒，無內容可救；嚴禁回 \\let 中和）'
)


def _ccnexus_base() -> str:
    """base url：env 覆寫優先；否則雙機判定（felix=本機 127.0.0.1、其他=felix Tailscale IP）。"""
    if (b := os.environ.get("CCNEXUS_BASE_URL")):
        return b.rstrip("/")
    return ("http://127.0.0.1:3021"
            if "chenliangyus" in socket.gethostname().lower()
            else "http://100.118.39.104:3021")


def _ccnexus_auth() -> str:
    """讀 ~/.secrets/ccnexus.env → Basic Auth header 值（base64）。"""
    env: dict[str, str] = {}
    with open(os.path.expanduser("~/.secrets/ccnexus.env"), encoding="utf-8") as fh:
        for ln in fh:
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, _, v = ln.strip().partition("=")
                env[k] = v
    u, pw = env.get("CCNEXUS_ADMIN_USER"), env.get("CCNEXUS_ADMIN_PASS")
    if not u or not pw:
        raise RuntimeError("~/.secrets/ccnexus.env 缺 CCNEXUS_ADMIN_USER / CCNEXUS_ADMIN_PASS")
    return base64.b64encode(f"{u}:{pw}".encode()).decode()


def _call_llm(payload: list[dict[str, Any]], *, model: str, base: str, auth: str,
              timeout: int = 300, on_delta: Callable[[str], None] | None = None) -> str:
    """送一批 payload（[{i,err,tex}]）打 /v1/chat/completions（stream），回拼接後的全文。
    on_delta(full_text_so_far)：每收一段 SSE delta 回呼（給即時串流可觀測性，呼叫端自 throttle）。"""
    body = {
        "model": model, "stream": True,
        "messages": [
            {"role": "system", "content": _LLM_SYS},
            {"role": "user", "content": "\n".join(json.dumps(x, ensure_ascii=False) for x in payload)},
        ],
    }
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    out: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            s = raw.decode("utf-8", "ignore").strip()
            if not s.startswith("data:"):
                continue
            s = s[5:].strip()
            if s == "[DONE]":
                break
            try:
                o = json.loads(s)
            except ValueError:
                continue
            ch = o.get("choices") or []                # usage-only / keepalive chunk 無 choices
            if ch:
                piece = ch[0].get("delta", {}).get("content") or ""
                if piece:
                    out.append(piece)
                    if on_delta is not None:
                        on_delta("".join(out))
    return "".join(out)


def _parse_jsonl(text: str) -> dict[int, dict[str, Any]]:
    """容錯解析模型輸出 → {i: {"tex": str}} 或 {i: {"unrec": True}}。逐行抓 {...}，忽略 markdown
    圍欄/解釋/壞行。兩種合法回應：修好（含 str tex）、或宣告源文已毀（unrecoverable:true）。"""
    out: dict[int, dict[str, Any]] = {}
    for ln in text.splitlines():
        ln = ln.strip().strip("`").strip()
        if not (ln.startswith("{") and ln.endswith("}")):
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if "i" not in o:
            continue
        try:
            i = int(o["i"])
        except (ValueError, TypeError):                # 模型回非數字 i → 跳過該條，不中斷解析
            continue
        if isinstance(o.get("tex"), str):
            out[i] = {"tex": o["tex"]}
        elif o.get("unrecoverable") is True:
            out[i] = {"unrec": True}
    return out


def _batched(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _clip(s: str, n: int = 60) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


# 8 worker 並發時序列化 node render：render <1s、LLM 才是分鐘級瓶頸 → 鎖 render 幾乎不損並行，
# 又把記憶體封頂在「單一 node 進程」（否則 8×6GB heap 直接撐爆 felix）。
_render_lock = threading.Lock()


def _run_one_batch(grp: list, bno: int, *, model: str, base: str, auth: str,
                   pool_name: str, rnd: int) -> dict[str, Any]:
    """純 worker（給 ThreadPoolExecutor 並發跑）：對一批 (gid,slug,f) 打 LLM → 解析 → **批次** render
    守門（一次 node spawn 驗整批，過去每式一 spawn）→ 語意守門。**不碰任何共享狀態、不寫檔**——
    live/history/merge/apply/accept 全交主線程序列做（原子性）。回結果 dict：
      accepts [(slug,gid,new,[override])] · unrec [(slug,occ)] · retry [(gid,slug,f)] · verdicts/raw/meta。"""
    meta = {"ts": _now_iso(), "pool": pool_name, "round": rnd, "batch": bno, "model": model, "n": len(grp)}
    payload = [{"i": k, "err": f.get("err") or "", "tex": f.get("tex") or ""}
               for k, (_g, _s, f) in enumerate(grp)]
    try:
        raw_text = _call_llm(payload, model=model, base=base, auth=auth)   # 8 並發 → 不做逐 token 串流
        ans = _parse_jsonl(raw_text)
    except Exception as e:  # 連線/逾時/HTTP → 整批重試
        return {**meta, "state": "error", "error": str(e), "raw": "",
                "accepts": [], "unrec": [], "retry": list(grp),
                "verdicts": [{"gid": g, "slug": s, "outcome": "batch_fail"} for g, s, _ in grp]}

    # 批次 render 守門：蒐集所有「模型回了 tex」的候選，一次 run_render 驗整批（render 鎖序列化）。
    cand = [(k, ans[k]["tex"], bool(grp[k][2].get("display")))
            for k in ans if ans[k].get("tex") is not None and 0 <= k < len(grp)]
    rmap: dict[int, dict[str, Any]] = {}
    if cand:
        with _render_lock:
            try:
                rmap = run_render([{"i": k, "s": new, "d": d} for k, new, d in cand])
            except Exception:
                rmap = {}                                  # 整批 render 異常 → 全數落入 render_err 重試

    accepts: list = []
    unrec: list = []
    retry: list = []
    verdicts: list[dict[str, Any]] = []
    for k, (gid, slug, f) in enumerate(grp):
        ent = ans.get(k)
        new = (ent or {}).get("tex") if ent else None
        vr: dict[str, Any] = {"gid": gid, "slug": slug, "tex": f.get("tex") or "", "new": new or ""}
        if ent and ent.get("unrec"):                       # 模型誠實宣告源文已毀 → 終態，不重試
            vr["outcome"] = "unrecoverable"
            unrec.append((slug, int(f.get("occ") or 1)))
        elif not new:                                      # 漏回 / 非 str 非 unrec → 重試
            vr["outcome"] = "missing"
            retry.append((gid, slug, f))
        elif (v := rmap.get(k)) is None:                   # 批次 render 異常 → 重試
            vr["outcome"] = "render_err"
            retry.append((gid, slug, f))
        elif not v.get("ok"):                              # render 守門：不過不落地
            vr["outcome"] = "render_fail"
            vr["render_err"] = v.get("err") or ""
            retry.append((gid, slug, f))
        elif (sem := semantic_reason(new)):                # 語意守門：render 過但空殼/原語 → 不落地
            vr["outcome"] = "semantic_fail"
            vr["semantic"] = sem
            retry.append((gid, slug, f))
        else:
            try:
                ovs = finding_to_overrides(slug, f, new)
                accepts.append((slug, gid, new, ovs))
                vr["outcome"] = "accepted"
            except ValueError:                             # 無 targets / 空 tex → 無法定位，棄不重試
                vr["outcome"] = "locate_fail"
        verdicts.append(vr)
    return {**meta, "state": "done", "raw": raw_text,
            "accepts": accepts, "unrec": unrec, "retry": retry, "verdicts": verdicts}


def _write_agg_live(*, started: float, total: int, done: int, accepted: int, unrec: int,
                    retry: int, hard: int, workers: int, active: int, running: bool) -> None:
    """聚合進度快照（schema 2）→ dev/math_live.json。8 worker 並發下不再有單一 token 串流，
    改報「在工作 + 多快」：吞吐(條/分)、進度(done/total)、ETA、活躍 worker 數。dev 頁直讀。"""
    el = max(time.monotonic() - started, 1e-6)
    rate = done / el * 60.0
    _live_write({
        "schema": 2, "ts": _now_iso(), "state": "running" if running else "idle",
        "workers": workers, "active": active, "total": total, "done": done,
        "accepted": accepted, "unrecoverable": unrec, "retry_pending": retry, "hard_residual": hard,
        "elapsed_s": round(el, 1), "rate_per_min": round(rate, 1),
        "eta_s": round((total - done) / (done / el)) if done and total > done else (0 if done else None),
    })


def cmd_batch(a: argparse.Namespace) -> int:
    """list → 分批打 LLM → render 守門 → per-book 一次 merge+apply+重驗。JSON 結果印 stdout。"""
    work = [(_gid(s, f.get("tex") or "", bool(f.get("display"))), s, f)
            for s, f in iter_todo(book=a.book, category=a.category)]
    if a.limit is not None:
        work = work[:a.limit]
    if not work:
        print(json.dumps({"ok": True, "accepted": 0, "msg": "無待辦"}, ensure_ascii=False))
        return 0

    LONG = 400  # 長式（>400 字元）分流，用較小批避免吃掉整批注意力
    long_n = min(a.n, max(6, a.n // 5))   # clamp：--n<6 時 long 批不該反比 short 大
    pools = {
        "short": ([w for w in work if len(w[2].get("tex") or "") <= LONG], a.n),
        "long":  ([w for w in work if len(w[2].get("tex") or "") > LONG], long_n),
    }
    if a.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "total": len(work),
                          "short": len(pools["short"][0]), "long": len(pools["long"][0]),
                          "base": _ccnexus_base(), "model": a.model}, ensure_ascii=False, indent=2))
        return 0

    if not node_available():   # render 守門是 batch 的全部安全基礎；node 缺 → graceful 中止，不裸炸
        print(json.dumps({"ok": False, "error": "node_modules/mathjax-full 缺 → 無 render 守門，"
                          "batch 中止（先 npm --prefix book_pipeline install）"}, ensure_ascii=False))
        return 1

    base, auth = _ccnexus_base(), _ccnexus_auth()
    workers = max(1, getattr(a, "workers", 8))
    verbose = getattr(a, "verbose", False)
    accepted: dict[str, list] = defaultdict(list)
    gid_new: dict[str, str] = {}
    unrec: dict[str, int] = {}   # slug → 模型判源文已毀的 occ 累計（收尾轉 math-accept 誠實終態）
    still: list = []
    # 進度聚合（dev 頁「在工作 + 多快」）：done=已到終態（accept/unrec/locate_fail），retry 暫不算 done。
    started = time.monotonic()
    total = len(work)
    cnt = {"done": 0, "accepted": 0, "unrec": 0, "locate": 0}
    seq = 0  # 全域批次序號（history 定址）

    # 派工：每池每輪把 batch 攤平給 ThreadPoolExecutor(workers) 並發跑純 worker；as_completed 在**主
    # 線程序列**合併結果（accepted/gid_new/unrec/history/live 全在此寫 → 零競態、原子）。
    _write_agg_live(started=started, total=total, done=0, accepted=0, unrec=0,
                    retry=0, hard=0, workers=workers, active=0, running=True)
    for name, (pool, bn) in pools.items():
        for rnd in range(a.rounds):
            if not pool:
                break
            batches = list(_batched(pool, bn))
            _log(f"[{name}] round {rnd + 1}/{a.rounds}：{len(pool)} 條 → {len(batches)} 批 × {workers} worker 並發")
            next_pool: list = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {}
                for grp in batches:
                    futs[ex.submit(_run_one_batch, grp, seq, model=a.model, base=base,
                                   auth=auth, pool_name=name, rnd=rnd)] = len(grp)
                    seq += 1
                pending = len(futs)
                for fut in as_completed(futs):
                    res = fut.result()
                    for slug, gid, new, ovs in res["accepts"]:
                        accepted[slug].extend(ovs)
                        gid_new[gid] = new
                    for slug, occ in res["unrec"]:
                        unrec[slug] = unrec.get(slug, 0) + occ
                    next_pool.extend(res["retry"])
                    n_acc, n_unr = len(res["accepts"]), len(res["unrec"])
                    n_loc = res["n"] - n_acc - n_unr - len(res["retry"])
                    cnt["accepted"] += n_acc; cnt["unrec"] += n_unr; cnt["locate"] += n_loc
                    cnt["done"] += n_acc + n_unr + n_loc
                    pending -= 1
                    if res.get("error"):
                        _log(f"  ⚠ 批 #{res['batch']} 失敗（{res['n']} 條重試）：{res['error']}")
                    elif verbose:
                        _log(f"  批 #{res['batch']}：✓{n_acc} ⊘unrec{n_unr} ↻{len(res['retry'])}")
                    _history_append({k: res[k] for k in
                                     ("ts", "pool", "round", "batch", "model", "n", "state", "verdicts")
                                     if k in res})
                    _write_agg_live(started=started, total=total, done=cnt["done"],
                                    accepted=cnt["accepted"], unrec=cnt["unrec"],
                                    retry=len(next_pool), hard=0, workers=workers,
                                    active=min(workers, pending), running=True)
            pool = next_pool
        still.extend(pool)
    _write_agg_live(started=started, total=total, done=cnt["done"], accepted=cnt["accepted"],
                    unrec=cnt["unrec"], retry=0, hard=len(still), workers=workers,
                    active=0, running=False)

    # 落地：每書一次 merge + apply + 重驗（避免每條重驗整書）。unrec-only 書無 override 改動，
    # 仍重驗以拿到當前 bad_occ 供 mark_math_accepted 夾值。
    remaining: dict[str, int] = {}
    for slug in set(accepted) | set(unrec):
        if accepted.get(slug):
            merge_overrides(slug, accepted[slug])
            apply_overrides(slug)
        rep = validate_book(slug)
        write_report(slug, rep)
        remaining[slug] = rep.get("stats", {}).get("bad_unique", 0)

    # 源文已毀 → 誠實終態 math-accept（退出無限重試；mark 端夾到 report 殘餘、累進既有 accepted）。
    marked = 0
    if unrec:
        from book_pipeline import pipeline_queue as q
        st = q._load_state()
        for slug, occ in unrec.items():
            prev = int(((st.get(slug) or {}).get("math") or {}).get("accepted") or 0)
            try:
                q.mark_math_accepted(slug, prev + occ, "batch: 模型判源文已毀不可渲染（unrecoverable）")
                marked += occ
            except ValueError:                    # 無 report（已 revalidate，理論不該發生）→ 跳過
                pass

    el = max(time.monotonic() - started, 1e-6)
    out = {"ok": True, "accepted": len(gid_new), "unrecoverable": marked,
           "still_failing": len(still), "books_touched": len(set(accepted) | set(unrec)),
           "workers": workers, "elapsed_s": round(el, 1), "rate_per_min": round(total / el * 60.0, 1),
           "remaining_by_book": remaining}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _read_history(tail: int = 20, *, gid: str | None = None,
                  slug: str | None = None) -> list[dict[str, Any]]:
    """讀 dev/math_history.jsonl 末 tail 批（可選 gid/slug 篩選，篩 verdicts 含該 gid/slug 的批）。"""
    if not _HISTORY_PATH.exists():
        return []
    recs: list[dict[str, Any]] = []
    for ln in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue
    if gid:
        recs = [r for r in recs if any(v.get("gid") == gid for v in r.get("verdicts", []))]
    if slug:
        recs = [r for r in recs if any(v.get("slug") == slug for v in r.get("verdicts", []))]
    return recs[-tail:]


def cmd_raw(a: argparse.Namespace) -> int:
    """回溯模型原始回應：--live 看當前/最近一批串流，否則印歷史末 N 批（含原文+逐條判決）。"""
    if a.live:
        if _LIVE_PATH.exists():
            print(_LIVE_PATH.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"state": "none", "msg": "尚無 live 批次"}, ensure_ascii=False))
        return 0
    recs = _read_history(a.tail, gid=a.gid, slug=a.book)
    if a.json:
        print(json.dumps(recs, ensure_ascii=False, indent=2))
        return 0
    for r in recs:
        head = f"[{r.get('ts')}] {r.get('pool')}·r{r.get('round')}·#{r.get('batch')} · {r.get('state')} · n={r.get('n')}"
        print(head)
        for v in r.get("verdicts", []):
            mark = {"accepted": "✓", "render_fail": "✗", "render_err": "⚠", "semantic_fail": "⊘",
                    "unrecoverable": "✗", "missing": "·", "locate_fail": "⊘",
                    "batch_fail": "✗"}.get(v.get("outcome"), "?")
            line = f"  {mark} {v.get('slug')} · {_clip(v.get('tex'))}"
            if v.get("new"):
                line += f" → {_clip(v.get('new'))}"
            print(line)
    if not recs:
        print("（無歷史批次）")
    return 0


def _scan_bad_overrides(book: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """掃 math_overrides，回 {slug: [被語意 gate 攔下的 override, …]}（唯讀）。
    抓的是「render 過但空殼/原語」的舊 gateless 落地（gate 上線前產出 / gate 調整後重掃）。"""
    files = ([OVERRIDE_DIR / f"{book}.json"] if book
             else sorted(OVERRIDE_DIR.glob("*.json")))
    out: dict[str, list[dict[str, Any]]] = {}
    for fp in files:
        if not fp.is_file() or fp.name.startswith("_"):
            continue
        spec = json.loads(fp.read_text(encoding="utf-8"))
        bad = [o for o in (spec.get("overrides") or []) if semantic_reason(o.get("new", ""))]
        if bad:
            out[fp.stem] = bad
    return out


def cmd_purge(a: argparse.Namespace) -> int:
    """移除語意 gate 攔下的壞落地（render 過但空殼/中和式），canonical 復原：剔 override →
    重 parse（從 mineru_data 重生乾淨 parsed）→ 重套剩餘 override → 重驗。壞式回流成誠實殘餘
    （render error 可見、計入殘餘），不再偽裝成已修。--dry-run 只報不改。"""
    bad = _scan_bad_overrides(a.book)
    if not bad:
        print(json.dumps({"ok": True, "purged": 0, "msg": "無語意空殼落地"}, ensure_ascii=False))
        return 0
    plan = {slug: [{"id": o.get("id"), "reason": semantic_reason(o.get("new", "")),
                    "new": (o.get("new") or "")[:60]} for o in ovs]
            for slug, ovs in bad.items()}
    if a.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "books": len(bad),
                          "total": sum(len(v) for v in bad.values()), "plan": plan},
                         ensure_ascii=False, indent=2))
        return 0

    from book_pipeline import parser as bp_parser
    result: dict[str, Any] = {}
    for slug, bad_ovs in bad.items():
        fp = OVERRIDE_DIR / f"{slug}.json"
        spec = json.loads(fp.read_text(encoding="utf-8"))
        bad_ids = {o.get("id") for o in bad_ovs}
        kept = [o for o in (spec.get("overrides") or []) if o.get("id") not in bad_ids]
        spec["overrides"] = kept
        tmp = fp.with_name(fp.name + ".tmp")
        tmp.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, fp)
        bp_parser.parse_book(slug)               # 重生乾淨 parsed（壞式回原始 OCR 殘體）
        apply_overrides(slug)                     # 重套剩餘 good override
        rep = validate_book(slug)
        write_report(slug, rep)
        result[slug] = {"removed": len(bad_ids), "kept": len(kept),
                        "bad_occ_after": rep.get("stats", {}).get("bad_occ")}
        _log(f"  purge {slug}：剔 {len(bad_ids)} 條空殼、重 parse+重套（剩 override {len(kept)}）"
             f" → 殘餘 {rep.get('stats', {}).get('bad_occ')} occ")
    print(json.dumps({"ok": True, "purged": sum(len(v) for v in bad.values()),
                      "books": result}, ensure_ascii=False, indent=2))
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

    p_fix = sub.add_parser(
        "fix", help="把某 gid 的壞式改寫成正確 tex（render 驗證→override→apply→重驗）")
    p_fix.add_argument("--gid", required=True, help="待辦 gid（從 `sweep list` 抄）")
    p_fix.add_argument("--new", required=True,
                       help="正確 tex（eq 給裸 tex、inline 給裸 inner）。源文已毀不可渲染者改用 "
                            "`devctl math-accept`，勿硬塞語意錯的式子")
    p_fix.set_defaults(func=cmd_fix)

    p_batch = sub.add_parser(
        "batch", help="批量打自架 LLM 逐條改寫殘餘（render 守門 + retry + 長式分流）")
    p_batch.add_argument("--n", type=int, default=40, help="短式每批條數（預設 40）")
    p_batch.add_argument("--workers", type=int, default=8, help="並發 worker 數（預設 8；批量打 LLM）")
    p_batch.add_argument("--rounds", type=int, default=2, help="retry 輪數（預設 2）")
    p_batch.add_argument("--model", default=DEFAULT_MODEL, help=f"模型（預設 {DEFAULT_MODEL}）")
    p_batch.add_argument("--book", help="只處理某書 slug")
    p_batch.add_argument("--category", help="只處理某分類")
    p_batch.add_argument("--limit", type=int, help="最多處理幾條（試水溫用）")
    p_batch.add_argument("--dry-run", action="store_true", help="只印規模/分池/base，不打 LLM 不落地")
    p_batch.add_argument("--verbose", action="store_true", help="逐條 log 書·舊→新·render 過/不過（看處理流程）")
    p_batch.set_defaults(func=cmd_batch)

    p_raw = sub.add_parser("raw", help="回溯模型原始回應（即時 live / 歷史批次）")
    p_raw.add_argument("--live", action="store_true", help="印當前/最近一批 live 串流快照")
    p_raw.add_argument("--tail", type=int, default=20, help="印歷史末 N 批（預設 20）")
    p_raw.add_argument("--gid", help="只看含某 gid 的批次")
    p_raw.add_argument("--book", help="只看含某 slug 的批次")
    p_raw.add_argument("--json", action="store_true", help="JSON 輸出（完整原文+判決）")
    p_raw.set_defaults(func=cmd_raw)

    p_purge = sub.add_parser(
        "purge", help="移除語意 gate 攔下的壞落地（空殼/中和式）→ 重 parse+重套+重驗")
    p_purge.add_argument("--book", help="只清某書 slug（預設全 corpus）")
    p_purge.add_argument("--dry-run", action="store_true", help="只報要剔哪些，不改檔/不重 parse")
    p_purge.set_defaults(func=cmd_purge)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
