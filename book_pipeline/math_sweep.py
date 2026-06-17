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
import socket
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterator

from book_pipeline.apply_math_overrides import (
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


# ── 可觀測性：即時串流 + 歷史回溯（寫進 dev/，nginx 直服務、dev 頁相對 fetch；gitignore）──
# 比照 dev/workers.json / dev/agent_history 既有模式。live=當前批次串流快照（throttle 重寫），
# history=append-only jsonl（每批一條完成記錄，prune 末 200 批）。daemon 子程序直寫，dev 頁輪詢。
_ROOT = Path(__file__).resolve().parent.parent
_DEV_DIR = _ROOT / "dev"
_LIVE_PATH = _DEV_DIR / "math_live.json"
_HISTORY_PATH = _DEV_DIR / "math_history.jsonl"
_HISTORY_KEEP = 200
_LIVE_THROTTLE = 0.4  # 串流期間最短重寫間隔（秒），避免每 token 一次 IO


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


# ── sweep batch：批量打自架 LLM（gpt-5.3-codex-spark）逐條改寫，render 守門 ──────
#
# 經濟學（實測）：spark 是 reasoning 模型，主成本 completion 隨條數線性、batch 攤不平；
# batch 唯一省的是 input overhead 的零頭。故 N 不為「省 token」衝大——N=40 是 wall-clock/
# 重試粒度/不爆 completion 的甜蜜點。真正的省在：payload 精簡（只送 i/tex/err）、render
# 本機守門（<1ms，不過不落地）、帶 retry 池≤2 輪、長式分流。

DEFAULT_MODEL = "gpt-5.3-codex-spark"
_LLM_SYS = (
    '你是 LaTeX 修復器。每條給壞 tex（OCR 殘體）與其 MathJax 編譯錯誤，回**最小修正、'
    '語意不變、可被 MathJax 渲染**的正確 tex。逐條只回 JSONL，每行一個物件 '
    '{"i":<原序號>,"tex":"<正確 tex>"}，不要 markdown 圍欄、不要解釋、不要多餘字。'
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


def _parse_jsonl(text: str) -> dict[int, str]:
    """容錯解析模型輸出 → {i: new_tex}。逐行抓 {...}，忽略 markdown 圍欄/解釋/壞行。"""
    out: dict[int, str] = {}
    for ln in text.splitlines():
        ln = ln.strip().strip("`").strip()
        if not (ln.startswith("{") and ln.endswith("}")):
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if "i" in o and isinstance(o.get("tex"), str):
            try:
                out[int(o["i"])] = o["tex"]
            except (ValueError, TypeError):            # 模型回非數字 i → 跳過該條，不中斷解析
                continue
    return out


def _batched(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _clip(s: str, n: int = 60) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _process_pool(pool: list, batch_n: int, *, model: str, base: str, auth: str,
                  accepted: dict[str, list], gid_new: dict[str, str], verbose: bool = False,
                  pool_name: str = "", rnd: int = 0, seq: list[int] | None = None) -> list:
    """跑一個池一輪：分批打 LLM → 解析 → 每條 render 守門 → 過則收 override 進 accepted。
    回 next_pool（模型漏回 / render 不過 / 整批失敗者，供下輪重試）。無法定位者丟棄不重試。
    verbose → 逐條 log「書 · 舊 tex → 新 tex · render 過/不過」（daemon 想看處理流程時開）。

    可觀測性：每批寫 dev/math_live.json（串流期 throttle 重寫模型原文）+ 完成後 append
    dev/math_history.jsonl（含 payload/原文/逐條判決），供 dev 頁即時看 + 歷史回溯。
    seq=[next_batch_no] 可變單元素 list，跨池累進全域批次序號。"""
    nxt: list = []
    if seq is None:
        seq = [0]
    for grp in _batched(pool, batch_n):
        bno = seq[0]
        seq[0] += 1
        items = [{"i": i, "gid": g, "slug": s, "err": f.get("err") or "",
                  "tex": f.get("tex") or "", "display": bool(f.get("display"))}
                 for i, (g, s, f) in enumerate(grp)]
        payload = [{"i": it["i"], "err": it["err"], "tex": it["tex"]} for it in items]
        base_rec = {"ts": _now_iso(), "pool": pool_name, "round": rnd, "batch": bno,
                    "model": model, "n": len(grp), "items": items}

        # 串流：on_delta throttle 重寫 live，讓 dev 頁看模型逐字生成
        last = [0.0]

        def _on_delta(full: str, _br=base_rec, _last=last) -> None:
            now = time.monotonic()
            if now - _last[0] < _LIVE_THROTTLE:
                return
            _last[0] = now
            _live_write({**_br, "state": "streaming", "raw": full, "verdicts": []})

        _live_write({**base_rec, "state": "streaming", "raw": "", "verdicts": []})
        try:
            raw_text = _call_llm(payload, model=model, base=base, auth=auth, on_delta=_on_delta)
            ans = _parse_jsonl(raw_text)
        except Exception as e:  # 連線/逾時/HTTP → 整批重試
            _log(f"  ⚠ 批失敗（{len(grp)} 條重試）：{e}")
            rec = {**base_rec, "state": "error", "raw": "", "error": str(e),
                   "verdicts": [{"i": it["i"], "gid": it["gid"], "slug": it["slug"],
                                 "outcome": "batch_fail"} for it in items]}
            _live_write(rec)
            _history_append(rec)
            nxt.extend(grp)
            continue

        verdicts: list[dict[str, Any]] = []
        for i, (gid, slug, f) in enumerate(grp):
            new = ans.get(i)
            v_rec: dict[str, Any] = {"i": i, "gid": gid, "slug": slug,
                                     "tex": f.get("tex") or "", "new": new or ""}
            if not new:                                   # 模型漏回
                if verbose:
                    _log(f"  · {slug} 模型漏回 · {_clip(f.get('tex'))}")
                v_rec["outcome"] = "missing"
                verdicts.append(v_rec)
                nxt.append((gid, slug, f))
                continue
            try:
                v = run_render([{"i": 0, "s": new, "d": bool(f.get("display"))}]).get(0) or {}
            except Exception as e:                        # render_check.js 偶發非零退出 → 該條重試
                _log(f"  ⚠ render 異常（1 條重試）：{e}")
                v_rec["outcome"] = "render_err"
                verdicts.append(v_rec)
                nxt.append((gid, slug, f))
                continue
            if not v.get("ok"):                           # render 守門：不過不落地
                if verbose:
                    _log(f"  ✗ {slug} render 不過 · {_clip(f.get('tex'))} → {_clip(new)}")
                v_rec["outcome"] = "render_fail"
                v_rec["render_err"] = v.get("err") or ""
                verdicts.append(v_rec)
                nxt.append((gid, slug, f))
                continue
            try:
                accepted[slug].extend(finding_to_overrides(slug, f, new))
                gid_new[gid] = new
                v_rec["outcome"] = "accepted"
                if verbose:
                    _log(f"  ✓ {slug} · {_clip(f.get('tex'))} → {_clip(new)}")
            except ValueError:                            # 無 targets / 空 tex → 無法定位，棄
                v_rec["outcome"] = "locate_fail"
                if verbose:
                    _log(f"  ⊘ {slug} 無法定位（無 targets/空 tex）· {_clip(f.get('tex'))}")
            verdicts.append(v_rec)

        rec = {**base_rec, "state": "done", "raw": raw_text, "verdicts": verdicts}
        _live_write(rec)
        _history_append(rec)
    return nxt


def cmd_batch(a: argparse.Namespace) -> int:
    """list → 分批打 spark → render 守門 → per-book 一次 merge+apply+重驗。JSON 結果印 stdout。"""
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
    accepted: dict[str, list] = defaultdict(list)
    gid_new: dict[str, str] = {}
    still: list = []
    seq = [0]  # 跨池累進的全域批次序號（給可觀測性記錄定址）
    for name, (pool, bn) in pools.items():
        for rnd in range(a.rounds):
            if not pool:
                break
            _log(f"[{name}] round {rnd + 1}/{a.rounds}：{len(pool)} 條（批 {bn}）")
            pool = _process_pool(pool, bn, model=a.model, base=base, auth=auth,
                                 accepted=accepted, gid_new=gid_new, verbose=getattr(a, 'verbose', False),
                                 pool_name=name, rnd=rnd, seq=seq)
        still.extend(pool)
    # 收尾：live 標 idle（保留末批內容供 dev 頁顯示「最近一批」，但狀態非 streaming）
    try:
        if _LIVE_PATH.exists():
            cur = json.loads(_LIVE_PATH.read_text(encoding="utf-8"))
            if cur.get("state") in ("streaming", "done"):
                _live_write({**cur, "state": "idle"})
    except Exception:
        pass

    # 落地：每書一次 merge + apply + 重驗（避免每條重驗整書）
    remaining: dict[str, int] = {}
    for slug, ovs in accepted.items():
        merge_overrides(slug, ovs)
        apply_overrides(slug)
        rep = validate_book(slug)
        write_report(slug, rep)
        remaining[slug] = rep.get("stats", {}).get("bad_unique", 0)

    out = {"ok": True, "accepted": len(gid_new), "still_failing": len(still),
           "books_touched": len(accepted), "remaining_by_book": remaining}
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
            mark = {"accepted": "✓", "render_fail": "✗", "render_err": "⚠",
                    "missing": "·", "locate_fail": "⊘", "batch_fail": "✗"}.get(v.get("outcome"), "?")
            line = f"  {mark} {v.get('slug')} · {_clip(v.get('tex'))}"
            if v.get("new"):
                line += f" → {_clip(v.get('new'))}"
            print(line)
    if not recs:
        print("（無歷史批次）")
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

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
