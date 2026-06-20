#!/usr/bin/env python3
"""book_pipeline.pipeline_run_state — 系統「運行/暫停」的最小狀態 I/O。

刻意**零重依賴（只 json/os）＋零憑證**：同時被 host 的 pipeline_tick/devctl（全依賴）與
docker sidecar dev_control（唯讀代碼、不得碰憑證）共用，作為「運行/暫停」單一真相。狀態檔同置
`book_pipeline/.control/`（sidecar 已窄掛 rw、gitignore、per-machine runtime）。

**預設暫停（fail-safe，與 zlib 停用態 fail-open 刻意相反）**：狀態檔不存在/壞/`running!=True`
→ 視為暫停。理由：① 使用者要求「部署後預設暫停」（fresh deploy 無狀態檔 → 暫停，待人工按啟動）；
② fail-safe——狀態壞時寧可停產、不亂跑（zlib 是「降流量」故 fail-open；這是「停命脈」故預設相反才對）。
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_DIR = os.path.join(ROOT, 'book_pipeline', '.control')
RUN_STATE_PATH = os.path.join(CONTROL_DIR, 'pipeline_run_state.json')


def is_paused() -> bool:
    """系統是否暫停。預設暫停：只有顯式 {"running": true} 才回 False（運行）。"""
    try:
        d = json.load(open(RUN_STATE_PATH))
        return d.get('running') is not True
    except Exception:
        return True


def set_running(running: bool) -> None:
    """原子寫運行/暫停態（目錄不存在則建）。"""
    os.makedirs(CONTROL_DIR, exist_ok=True)
    tmp = f'{RUN_STATE_PATH}.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'running': bool(running)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RUN_STATE_PATH)
