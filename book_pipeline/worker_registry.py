#!/usr/bin/env python3
"""book_pipeline.worker_registry — 進行中 LLM worker 的即時註冊表（/dev 工人面板資料源）。

daemon 每派一個 headless claude worker（dispatch_llm）就 register；worker stdout 走
stream-json，每個 tool_use / text 事件 event() 進來 → 累積「最近 N 條工具調用/發言 +
總調用數」；worker 結束 unregister。寫 dev/workers.json（gitignore，節流原子寫），dev
頁直接 fetch 渲染。同進程多執行緒（_advance_parallel）並發 → threading.Lock 保護。

崩潰安全：launchd 每 tick spawn 全新進程 → tick 起頭 reset() 清空（含死進程殘留）。
讀端（dev 頁 / devctl）另以 pid 存活 + started 時齡過濾 stale，雙重保險。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS_PATH = os.path.join(ROOT, 'dev', 'workers.json')

RECENT_CAP = 200        # 每 worker 保留最近幾條事件（/dev 工人面板可滾完整 tool-use；
                        # 200 涵蓋幾乎所有 audit 的工具調用數，仍封頂避免 workers.json 爆量）
_FLUSH_THROTTLE = 1.0   # 秒；event 高頻 → 合併寫盤

_lock = threading.Lock()
_workers: dict[str, dict] = {}
_tick_pid = os.getpid()
_last_flush = 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _flush_locked(force: bool = False) -> None:
    global _last_flush
    now = time.monotonic()
    if not force and now - _last_flush < _FLUSH_THROTTLE:
        return
    _last_flush = now
    data = {'updated_at': _now(), 'tick_pid': _tick_pid,
            'workers': list(_workers.values())}
    os.makedirs(os.path.dirname(WORKERS_PATH), exist_ok=True)
    tmp = WORKERS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, WORKERS_PATH)


def reset(tick_pid: int | None = None) -> None:
    """tick 起頭呼叫：清空註冊表（含上次崩潰殘留），記錄本 tick pid。"""
    global _workers, _tick_pid
    with _lock:
        _workers = {}
        _tick_pid = tick_pid or os.getpid()
        _flush_locked(force=True)


def register(key: str, slug: str | None, verb: str, pid: int, provider: str) -> None:
    with _lock:
        _workers[key] = {
            'key': key, 'slug': slug, 'verb': verb, 'pid': pid,
            'provider': provider, 'started': _now(), 'updated': _now(),
            'total_calls': 0, 'recent': []}
        _flush_locked(force=True)


def event(key: str, kind: str, label: str) -> None:
    """kind='tool'（工具調用，計入 total_calls）或 'text'（worker 發言）。"""
    with _lock:
        w = _workers.get(key)
        if not w:
            return
        w['updated'] = _now()
        if kind == 'tool':
            w['total_calls'] += 1
        w['recent'].append({'t': _now(), 'kind': kind, 'label': label[:240]})
        w['recent'] = w['recent'][-RECENT_CAP:]
        _flush_locked()


def unregister(key: str) -> None:
    with _lock:
        _workers.pop(key, None)
        _flush_locked(force=True)


def load() -> dict:
    """讀端用（devctl / 一般查詢）。讀不到回空。"""
    try:
        return json.load(open(WORKERS_PATH)) or {'workers': []}
    except Exception:
        return {'workers': []}
