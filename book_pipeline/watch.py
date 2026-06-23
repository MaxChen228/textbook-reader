#!/usr/bin/env python3
"""book_pipeline/watch.py — live 階段轉移事件流（push 式觀測，餵 `Monitor` 工具）。

觀測面第五軸。前四軸都是「拉」：status=當下 frontier 快照、trace=事後 forensic、devctl=控制、
pipeline_queue=work-queue。本工具補唯一缺的「推」——**狀態一推進就吐一行**。每輪讀 daemon 自己維護的
dev/status.json（單一真相、零重算、與 /dev 字字一致），diff 各書 stage，**只在變化時** print 一行；
接到 harness 的 `Monitor` 工具即成被動通知流（agent 零輪詢、notification 自動進對話）。

選擇接收哪類事件（--kinds，即「我要哪種資訊」）：
  all   每次階段轉移都報（預設；等於 live trace）
  done  只報終態（✅上架 / ⚠卡關X / 🔴裁決R），中間階段靜默（「跑完叫我」模式）
範圍：位置參數 slug… = 只盯這幾本（campaign cohort）；無參數 = 盯所有在飛（未上架且非終態）書。

終止：被盯的書全達終態 → 印總結後 exit 0（Monitor 收到乾淨結尾）。--persistent 則永不自退（TaskStop 收）。
robust（silence ≠ success）：①終態涵蓋 deploy∧stuck∧blocked，不只 happy path ②status.json 逾 STALE_SEC
未刷新 → **交叉 workers.json**（高頻寫的存活信號）分級：workers 仍新=daemon 活只是大快照滯後（walltime 排空/
重負載，資訊性非死、不 cry-wolf）／workers 也舊=可能真停了（真警示）③半截/缺檔該輪跳過 ④controller phase 翻轉也報。

用法：uv run python -m book_pipeline.watch [slug ...] [--kinds all|done] [--interval 20] [--persistent]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, 'dev', 'status.json')
WORKERS = os.path.join(ROOT, 'dev', 'workers.json')  # controller 高頻寫，比大快照靈敏的存活信號
STALE_SEC = 180  # status.json 逾此未刷新 → 疑似 daemon 死；但需交叉 workers.json 排除「健康但快照滯後」


def _slug_label(rows: dict, slug: str) -> str:
    """slug 的當前里程碑標籤。主書：上站→'deployed'、否則 stage 字串。
    `_sol` 解答書對 snapshot **隱形**（綁母書）→ 解析到母書、回 **sol 進度**（母書驅動 merge，
    記憶 sol-bound-to-parent）：母書 stage『4 sol已merge』=成功、todo 帶 sol_ingest/sol_extract=處理中、
    母書卡關(R/X)或還在前置(0/1/2)=sol 動不了、無 sol todo 且未 merge=_pending/_escalated 已開 proposal。"""
    if slug.endswith('_sol'):
        return _sol_label(rows.get(slug[:-4]))
    r = rows.get(slug)
    if not r:
        return '?'
    return 'deployed' if r.get('deployed') else (r.get('stage') or '?')


def _sol_label(pr: dict | None) -> str:
    if not pr:
        return 'sol·母書缺'                       # 母書不在 snapshot（未 owned/ingest）→ 待裁決
    stage = pr.get('stage') or '?'
    if stage[:1] in ('R', 'X'):
        return f'sol·母書{stage}'                 # 母書卡關 → 無法 merge（終態·待裁決）
    if 'sol已merge' in stage:
        return 'sol·已merge'                      # 終態·成功
    todo = pr.get('todo') or ''
    if 'sol_ingest' in todo:
        return 'sol·ingest中'
    if 'sol_extract' in todo:
        return 'sol·extract中'
    if stage[:1] in ('0', '1', '2'):
        return f'sol·母書{stage}'                 # 母書仍前置（OCR/audit/parse）→ sol 尚不能動
    # 母書 stage 3/4 但當下無 sol todo：須分辨兩種——
    #   母書未 deploy → 仍在 catalog_audit/deploy 收尾，sol_ingest todo 尚未浮現（**非終態**，
    #     sol_ingest 待母書上架才派；R3 pozar_microwave 回收途中誤判 '待裁決' 的根因）；
    #   母書已 deploy 卻仍無 sol 動作 → _pending/_escalated 真待裁決（終態·proposal）。
    if not pr.get('deployed'):
        return f'sol·母書{stage}'                 # 收尾中、sol 尚未輪到（非終態）
    return 'sol·待裁決'                           # 母書已上架卻無 sol 動作 = _pending/_escalated（終態·proposal）


def _is_terminal(label: str) -> bool:
    """終態 = 主書已上架 ∨ sol 已merge ∨ 卡關(X) ∨ 需人工裁決(R / sol·母書R·X / sol·待裁決 / sol·母書缺)。"""
    if label in ('deployed', 'sol·已merge', 'sol·待裁決', 'sol·母書缺'):
        return True
    if label[:1] in ('R', 'X'):
        return True
    return label.startswith('sol·母書R') or label.startswith('sol·母書X')


def _icon(label: str) -> str:
    if label in ('deployed', 'sol·已merge'):
        return '✅'
    if label[:1] == 'X' or label.startswith('sol·母書X'):
        return '⚠'
    if label[:1] == 'R' or label.startswith('sol·母書R') or label in ('sol·待裁決', 'sol·母書缺'):
        return '🔴'
    return '▸'


def _read_snapshot() -> tuple[dict | None, float]:
    """回 (snapshot, 落後秒數)；缺檔/半截 → (None, 0)。"""
    try:
        age = time.time() - os.path.getmtime(SNAPSHOT)
        with open(SNAPSHOT, encoding='utf-8') as f:
            return json.load(f), age
    except (OSError, ValueError):
        return None, 0.0


def _workers_age() -> float | None:
    """dev/workers.json 落後秒數（controller 高頻寫 → 比 200KB status.json 靈敏的存活信號）；缺檔→None。"""
    try:
        return time.time() - os.path.getmtime(WORKERS)
    except OSError:
        return None


def _staleness_msg(status_age: float, workers_age: float | None) -> str | None:
    """status.json 逾 STALE_SEC 未刷新時的分級警示（避免 cry-wolf）：
    workers.json 仍新鮮 → daemon 活著、只是大快照滯後（walltime 排空 / build_snapshot 重負載）→ 資訊性、非死；
    workers.json 也舊或缺 → daemon 可能真停了 → 真警示叫人查。回 None＝status.json 夠新、無需警示。"""
    if status_age <= STALE_SEC:
        return None
    if workers_age is not None and workers_age <= STALE_SEC:
        return (f"⏳ status.json 滯後 {int(status_age)}s，但 daemon 仍活"
                f"（workers.json {int(workers_age)}s 前更新）— 多半 walltime 排空/重負載快照延遲、非死")
    wa = '缺' if workers_age is None else f'{int(workers_age)}s'
    return (f"⏳ status.json {int(status_age)}s ∧ workers.json {wa} 均未更新 — "
            f"daemon 可能停了（devctl status 查）")


def _emit(line: str) -> None:
    print(line, flush=True)  # 每行一個 Monitor 事件 → 必須 line-flush


def run(slugs: list[str], kinds: str, interval: float, persistent: bool) -> int:
    snap, _ = _read_snapshot()
    while snap is None:  # 開機等 daemon 寫出第一份快照
        time.sleep(interval)
        snap, _ = _read_snapshot()
    rows = {r['slug']: r for r in snap.get('books', [])}

    if slugs:
        watch = list(slugs)
    else:  # 無參數：盯所有在飛（未終態）書
        watch = [s for s in rows if not _is_terminal(_slug_label(rows, s))]
    prev = {s: _slug_label(rows, s) for s in watch}  # _sol 經母書解析，不再因隱形被丟
    prev_phase = ((snap.get('code') or {}).get('phase'))

    _emit(f"▶ 監看 {len(watch)} 本 [kinds={kinds}]：" + '、'.join(watch))
    for s in watch:  # 開場各報一次目前位置（建立基線，之後只報變化）
        _emit(f"  · {s}  {prev[s]}")

    stale_warned = False
    while True:
        time.sleep(interval)
        snap, age = _read_snapshot()
        if snap is None:
            continue
        msg = _staleness_msg(age, _workers_age())
        if msg:
            if not stale_warned:
                _emit(msg)
                stale_warned = True
            continue
        stale_warned = False
        rows = {r['slug']: r for r in snap.get('books', [])}

        # controller phase 翻轉（running↔draining）= reload/排空，也是值得知道的狀態推進
        phase = (snap.get('code') or {}).get('phase')
        if phase and phase != prev_phase:
            _emit(f"🔄 controller → {phase}")
            prev_phase = phase

        for s in watch:
            cur = _slug_label(rows, s)
            if cur == prev.get(s):
                continue
            prev[s] = cur
            if kinds == 'done' and not _is_terminal(cur):
                continue  # done 模式：中間階段不吵
            r = rows.get(s)  # _sol 不在 rows（gate 觀測只對主書）
            verb = (f"（{r.get('gate_verb')} 閘）" if r and r.get('gated') else '')
            _emit(f"{_icon(cur)} {s}  → {cur}{verb}")

        if not persistent and all(_is_terminal(prev.get(s, '')) for s in watch):
            done = sum(1 for s in watch if prev.get(s) in ('deployed', 'sol·已merge'))
            bad = len(watch) - done
            _emit(f"✓ 全部終態：{done} 完成（上架/已merge） / {bad} 待裁決")
            return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog='python -m book_pipeline.watch',
        description='live 階段轉移事件流（push 觀測，餵 Monitor 工具）')
    ap.add_argument('slugs', nargs='*', help='只盯這幾本；省略=所有在飛書')
    ap.add_argument('--kinds', choices=('all', 'done'), default='all',
                    help='all=每次轉移都報（預設）；done=只報終態')
    ap.add_argument('--interval', type=float, default=20.0, help='輪詢秒數（預設 20，本地讀檔便宜）')
    ap.add_argument('--persistent', action='store_true', help='永不自退（全終態也續盯，TaskStop 收）')
    a = ap.parse_args(argv)
    return run(a.slugs, a.kinds, a.interval, a.persistent)


if __name__ == '__main__':
    raise SystemExit(main())
