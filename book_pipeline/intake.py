#!/usr/bin/env python3
"""book_pipeline/intake.py — 批次運書員：一鍵帶 N 本合格新書入庫並開閘推進到上架。

補的操作性缺口：crawl lane 閘門是 all-or-nothing（gate_allows(None,'crawl') 只認 slug=='*' 規則）
→ 無法「只爬這 N 本」；手動繞 select_next + _fetch_book + 逐本 gates allow 太瑣碎易錯（campaign
首輪踩過 PYTHONPATH/額度槽分配/scratchpad hack）。本工具一鍵完成、確定性、可觀測、低 token：

  pick   = booklists.select_next(N)（合格池 QUALIFIED 確定性前 N 本，排除已失敗達上限）
  gates  = default=allow + 只 hold crawl lane → SIGUSR1 喚醒 controller（放行本批 + 全 corpus owned
           收尾工作；只擋自動 crawl 新書 → intake 直接 fetch 是唯一新書來源＝節奏控制）
  fetch  = 並行 _fetch_book（複用買書員 primitive + zlib 額度槽分配）落 raw_pdfs（繞過 crawl 閘）
  → daemon observe 自動推進 qc→ingest→audit→catalog→deploy（本批 + 既有 backlog 齊跑，只擋 crawl）。

設計選擇：直接 fetch（非開 crawl lane）因 lane all-or-nothing 會超抓（room 可達 CAP=30）；直接
fetch 精準 N 本、零多餘 PDF（避免多餘書躺 ingest 成日後 OCR 風暴）。--dry-run 只預覽不動。

用法：uv run python -m book_pipeline.intake [N] [--dry-run]   （N 預設 5）
注意：rounds 設計為循序（每輪 review 在上一批 deploy 後）→ 本工具原子取代 gates 規則；若上一批
尚未全 deploy 就跑會把它們 hold 在閘（中性停、非淘汰），故 review 確認上批上架後再跑下一輪。
"""
from __future__ import annotations

import argparse
import os
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed

from book_pipeline import booklists
from book_pipeline import pipeline_gates as pg
from book_pipeline import pipeline_queue as q
from book_pipeline import pipeline_tick as pt

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pick(n: int, exclude: set[str] | None = None) -> list[dict]:
    """合格池確定性前 n 本（排除已失敗達上限者 + 呼叫端指定）。"""
    blocked = set(q.crawl_blocked_slugs(pt.MAX_FETCH_FAILS)) | set(exclude or set())
    return booklists.select_next(n, exclude=blocked)


def set_gates_for(slugs: list[str]) -> int | None:
    """設閘門 = **default=allow + 只 hold crawl lane** → SIGUSR1 喚醒。回 controller pid。
    為何非舊「default=hold + 逐本 allow」（該設計有兩個致命缺陷，2026-06-23 改）：
      ① sol_ingest/sol_extract **派工在母書**——fetch 的是 `<x>_sol` 子書，逐本只 allow 子書 slug，
         母書（既有 owned）沒 allow → 被 default=hold 擋死，子書永遠 merge 不進母書（R3 實際卡住 3 本）。
      ② default=hold **連帶凍結全 corpus 既有 sol backlog**（~12 本 deployed 書的 sol 收尾工作），
         daemon 閒置、合法工作卡住、看板工人 0。
    intake 直接 `_fetch_book` 繞過 crawl 閘 → hold crawl **不影響本批入庫**，卻保留『只有 intake 是
    新書來源』的節奏控制（campaign 一輪 5 本）。default=allow 讓本批 + 全 corpus owned 收尾工作齊跑。
    slugs 參數保留供呼叫端 log；閘門本身不再 per-slug（idempotent，backfill 多輪 fetch 重設無副作用）。"""
    pg.set_gates("allow", [{"slug": "*", "stage": "crawl", "action": "hold"}])
    pid = pt.controller_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pid = None
    return pid


def fetch_all(batch: list[dict]) -> list[str]:
    """並行下載（複用買書員 _fetch_book + 額度槽分配）。失敗者 bump_crawl_fail（達上限後 select_next
    自動排除死連結，不再每輪重選浪費 slot）→ _fetch_book 失敗 log 已含真實原因。回成功 slug 清單。"""
    accts = pt._zlib_accounts_remaining()
    slots = [a["account"] for a in (accts or []) for _ in range(max(0, a.get("remaining") or 0))]
    for i, b in enumerate(batch):
        if i < len(slots):
            b["account"] = slots[i]
    ok: list[str] = []
    with ThreadPoolExecutor(max_workers=min(pt.CRAWL_PARALLEL, len(batch) or 1)) as ex:
        futs = {ex.submit(pt._fetch_book, b): b for b in batch}
        for f in as_completed(futs):
            b = futs[f]
            try:
                s = f.result()
            except Exception as e:  # noqa: BLE001
                s = None
                print(f"❌ {b.get('slug')} 異常：{e}", flush=True)
            if s:
                ok.append(s)
                q.clear_crawl_fail(s)
                print(f"✅ {s}（{b.get('_mb', '?')} MB）", flush=True)
            else:
                fails = q.bump_crawl_fail(b.get("slug"))  # 死連結計數 → 達上限自動排除（見 daemon.log 原因）
                print(f"❌ {b.get('slug')} fetch 失敗（累計 {fails} 次，原因見 daemon.log）", flush=True)
    return ok


def run(n: int, dry: bool) -> int:
    if dry:
        batch = pick(n)
        print(f"選中 {len(batch)} 本：{[b['slug'] for b in batch]}", flush=True)
        rem = pt._zlib_remaining_cached()
        print(f"DRY：將 fetch（失敗自動 backfill 補到 {n}）→ gates(default=allow + hold crawl)；"
              f"zlib 額度(快取) {rem}", flush=True)
        return 0
    # backfill 迴圈：fetch 失敗（死連結/額度）即補選替補，直到湊滿 n 本或合格池耗盡。
    success: list[str] = []
    tried: set[str] = set()
    while len(success) < n:
        cand = pick(n - len(success), exclude=tried | set(success))
        if not cand:
            print("合格池耗盡（剩餘全 owned/pending/rejected/失敗達上限）→ 餘額靠 /restock 填", flush=True)
            break
        for b in cand:
            tried.add(b["slug"])
        print(f"嘗試 {len(cand)} 本：{[b['slug'] for b in cand]}", flush=True)
        success += fetch_all(cand)
    if not success:
        print("本輪零入庫", flush=True)
        return 0
    pid = set_gates_for(success)  # 設 default=allow + hold crawl（放行本批 + 全 backlog，只擋自動爬新書）
    print(f"\n入庫 {len(success)}/{n}：{success}", flush=True)
    # 主書 vs _sol 解答書分類（_sol 不自己上架、merge 進母書）；母書未 deployed 的 _sol 會 block（無處 merge）。
    mains = [s for s in success if not s.endswith('_sol')]
    sols = [s for s in success if s.endswith('_sol')]
    if sols:
        print(f"  主書 {len(mains)}（→上架）｜解答書 {len(sols)}（→merge 進母書，不自己上架）：", flush=True)
        for s in sols:
            parent = s[:-4]
            ok = os.path.exists(os.path.join(_ROOT, 'data', parent, 'book.json'))
            print(f"    {'✓' if ok else '⚠'} {s} → 母書 {parent}"
                  + ('' if ok else '（母書未上架 → sol 將 block、需先處理母書）'), flush=True)
    print(f"gates：default=allow + hold crawl（本批 + 全 corpus owned 收尾齊跑、只擋自動爬新書；"
          f"controller pid={pid} 已喚醒）", flush=True)
    print("daemon 自動推進；觀測：uv run python -m book_pipeline.watch " + ' '.join(success)
          + "（_sol 經母書解析）/ devctl status / /dev", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m book_pipeline.intake",
        description="批次帶 N 本合格新書入庫並開閘推進到上架（campaign 批次運書員）")
    ap.add_argument("n", type=int, nargs="?", default=5, help="本數（預設 5）")
    ap.add_argument("--dry-run", action="store_true", help="只預覽選哪幾本，不設閘不下載")
    a = ap.parse_args(argv)
    return run(a.n, a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
