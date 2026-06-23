#!/usr/bin/env python3
"""book_pipeline/intake.py — 批次運書員：一鍵帶 N 本合格新書入庫並開閘推進到上架。

補的操作性缺口：crawl lane 閘門是 all-or-nothing（gate_allows(None,'crawl') 只認 slug=='*' 規則）
→ 無法「只爬這 N 本」；手動繞 select_next + _fetch_book + 逐本 gates allow 太瑣碎易錯（campaign
首輪踩過 PYTHONPATH/額度槽分配/scratchpad hack）。本工具一鍵完成、確定性、可觀測、低 token：

  pick   = booklists.select_next(N)（合格池 QUALIFIED 確定性前 N 本，排除已失敗達上限）
  gates  = default=hold + 這 N 本 allow '*' + '*' math_sweep（原子取代舊規則）→ SIGUSR1 喚醒 controller
  fetch  = 並行 _fetch_book（複用買書員 primitive + zlib 額度槽分配）落 raw_pdfs
  → daemon observe 自動推進 qc→ingest→audit→catalog→deploy（只放行這 N 本，其餘 held）。

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


def pick(n: int, exclude: set[str] | None = None) -> list[dict]:
    """合格池確定性前 n 本（排除已失敗達上限者 + 呼叫端指定）。"""
    blocked = set(q.crawl_blocked_slugs(pt.MAX_FETCH_FAILS)) | set(exclude or set())
    return booklists.select_next(n, exclude=blocked)


def set_gates_for(slugs: list[str]) -> int | None:
    """default=hold + 這批 allow '*' + '*' math_sweep（原子取代）→ SIGUSR1 喚醒。回 controller pid。"""
    rules = [{"slug": "*", "stage": "math_sweep", "action": "allow"}]
    rules += [{"slug": s, "stage": "*", "action": "allow"} for s in slugs]
    pg.set_gates("hold", rules)
    pid = pt.controller_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pid = None
    return pid


def fetch_all(batch: list[dict]) -> list[str]:
    """並行下載（複用買書員 _fetch_book + 額度槽分配）。回成功 slug 清單。"""
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
                print(f"✅ {s}（{b.get('_mb', '?')} MB）", flush=True)
            else:
                print(f"❌ {b.get('slug')} fetch 失敗", flush=True)
    return ok


def run(n: int, dry: bool) -> int:
    batch = pick(n)
    slugs = [b["slug"] for b in batch]
    print(f"選中 {len(slugs)} 本：{slugs}", flush=True)
    if not batch:
        print("合格池空（全 owned/pending/rejected 或失敗達上限）→ 該跑 /restock 填書單", flush=True)
        return 0
    if dry:
        rem = pt._zlib_remaining_cached()
        print(f"DRY：將設 gates（default=hold + 這 {len(slugs)} 本 allow + math_sweep）"
              f"並 fetch；zlib 額度(快取) {rem}", flush=True)
        return 0
    pid = set_gates_for(slugs)
    print(f"gates：default=hold + {len(slugs)} 本 allow '*' + math_sweep（controller pid={pid} 已喚醒）",
          flush=True)
    ok = fetch_all(batch)
    print(f"\n入庫 {len(ok)}/{len(slugs)}：{ok}", flush=True)
    print("daemon 將自動推進到上架（uv run python -m book_pipeline.devctl status / /dev 觀測）", flush=True)
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
