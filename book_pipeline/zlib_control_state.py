#!/usr/bin/env python3
"""book_pipeline.zlib_control_state — zlib 帳號停用態的最小狀態 I/O（流量控制）。

刻意**零重依賴（只 json/os）＋零憑證**：故可同時被 host 的 crawl_zlib（買書/額度，需
requests ＋ ~/.secrets 憑證）與 docker sidecar dev_control（面板寫回，唯讀代碼＋**不得碰憑證**）
共用，作為「停用集」格式的單一真相源。

刻意**不**含帳號清單解析（email↔acctN、`_accounts`）——那需 ~/.secrets/zlib_accounts.json
（含密碼），留在 crawl_zlib、host-only；sidecar 改用 dev/zlib_quota.json 的 account email
清單做驗證（無憑證）。

狀態檔置於專屬目錄 book_pipeline/.control/：① _write_disabled 走 atomic-rename（換 inode），
單檔 bind mount 在 Docker-Desktop 下會斷，故 sidecar 須掛「目錄」rw；② 專屬目錄 → 那個 rw 掛載
最窄（只一個控制平面狀態檔，碰不到 *.py 原始碼）。fail-open：狀態檔不存在/壞 → 視為全啟用。
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_DIR = os.path.join(ROOT, 'book_pipeline', '.control')
ACCOUNT_STATE_PATH = os.path.join(CONTROL_DIR, 'zlib_account_state.json')


def disabled_emails() -> set:
    """停用帳號 email 集。不存在/壞檔 → 空集（fail-open，絕不因狀態檔壞而誤擋好帳號）。
    濾掉 falsy（None/''）：杜絕人工壞檔塞 null 致「無 email 帳號（email=None）被誤短路成停用」。"""
    try:
        d = json.load(open(ACCOUNT_STATE_PATH))
        return {e for e in (d.get('disabled') or []) if e}
    except Exception:
        return set()


def write_disabled(emails: set) -> None:
    """原子寫停用集（email 排序穩定 diff）。目錄不存在則建（host CLI 與 sidecar 首次寫共用此路徑）。"""
    os.makedirs(CONTROL_DIR, exist_ok=True)
    tmp = f'{ACCOUNT_STATE_PATH}.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'disabled': sorted(emails)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNT_STATE_PATH)
