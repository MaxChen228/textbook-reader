#!/usr/bin/env python3
"""book_pipeline.jsonio — 狀態檔讀寫的單一範式（消滅散落各模組的多種寫法）。

repo-root 相對的 JSON 狀態檔（pipeline_state / mineru_budget / slug_map / crawl_manifest…）
都該走這裡：
  寫【原子】 atomic_write_json：tmp + os.replace（同檔系統原子 rename）→ launchd/SIGKILL 在
            寫一半截斷只會留下半截的 tmp，正檔永遠完整；讀者絕不見半截。
  讀【容錯】 read_json：不存在→default；毀損(JSONDecodeError)→改名 .corrupt-<ts> 保全壞檔後回
            default（可安全重起、舊資料留供搶救），絕不讓一個壞檔靜默清空全部狀態。

跨進程/跨執行緒的 RMW 互斥仍由各呼叫端的 fcntl/threading lock 負責——本模組只保證『單次寫不留半截』。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def atomic_write_json(path: str, obj, *, indent: int | None = None,
                      ensure_ascii: bool = False) -> None:
    """原子寫：同目錄 tmp（帶 pid 避免並行寫者互踩）寫完後 os.replace 到正檔。"""
    tmp = f'{path}.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    """容錯讀：不存在→default；毀損→改名保全後 default；暫時性 OSError→default。
    回傳值為 None（檔內容是字面 null）時亦回 default，與舊 `json.load(...) or {}` 語義一致。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return default if data is None else data
    except (json.JSONDecodeError, ValueError):
        try:
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            os.replace(path, f'{path}.corrupt-{ts}')
        except OSError:
            pass
        return default
    except OSError:
        return default
