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
未刷新 → 吐 ⏳ 警示（daemon 死了不會靜默假裝沒事）③半截/缺檔該輪跳過 ④controller phase 翻轉（reload/排空）也報。

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
STALE_SEC = 180  # status.json 逾此未刷新 → daemon 可能死了，吐警示不靜默


def _label(r: dict) -> str:
    """單書當前里程碑標籤：上站完成 → 'deployed'，否則用 stage 字串（'0.5 OCR處理中'…）。"""
    return 'deployed' if r.get('deployed') else (r.get('stage') or '?')


def _is_terminal(label: str) -> bool:
    """終態 = 已上架 ∨ 卡關(X 無源/未ingest) ∨ 需人工裁決(R 書況/audit-blocked/qc拒)。"""
    return label == 'deployed' or label[:1] in ('R', 'X')


def _icon(label: str) -> str:
    if label == 'deployed':
        return '✅'
    if label[:1] == 'X':
        return '⚠'
    if label[:1] == 'R':
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
        watch = [s for s, r in rows.items() if not _is_terminal(_label(r))]
    prev = {s: _label(rows[s]) for s in watch if s in rows}
    prev_phase = ((snap.get('code') or {}).get('phase'))

    _emit(f"▶ 監看 {len(watch)} 本 [kinds={kinds}]：" + '、'.join(watch))
    for s in watch:  # 開場各報一次目前位置（建立基線，之後只報變化）
        if s in prev:
            _emit(f"  · {s}  {prev[s]}")

    stale_warned = False
    while True:
        time.sleep(interval)
        snap, age = _read_snapshot()
        if snap is None:
            continue
        if age > STALE_SEC:
            if not stale_warned:
                _emit(f"⏳ status.json 已 {int(age)}s 未刷新 — daemon 是否在跑？（devctl status 查）")
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
            r = rows.get(s)
            if not r:
                continue
            cur = _label(r)
            if cur == prev.get(s):
                continue
            prev[s] = cur
            if kinds == 'done' and not _is_terminal(cur):
                continue  # done 模式：中間階段不吵
            verb = (f"（{r.get('gate_verb')} 閘）" if r.get('gated') else '')
            _emit(f"{_icon(cur)} {s}  → {cur}{verb}")

        if not persistent and all(_is_terminal(prev.get(s, '')) for s in watch):
            up = sum(1 for s in watch if prev.get(s) == 'deployed')
            bad = len(watch) - up
            _emit(f"✓ 全部終態：{up} 上架 / {bad} 待裁決")
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
