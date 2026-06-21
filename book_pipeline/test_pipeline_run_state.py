#!/usr/bin/env python3
"""book_pipeline.test_pipeline_run_state — 系統「運行/暫停」狀態 I/O 的回歸鎖。

釘住兩條使用者倚賴的 invariant：
① **預設暫停（fail-safe）**：狀態檔不存在/壞/running!=true → is_paused() True
   （fresh deploy 即暫停，待人工按啟動）。
② **set_running 即時生效、原子落盤**：寫 running:true → 同進程立刻 is_paused() False
   （無 cache、無須重啟）；reverse 亦然 → 對應「啟動後改狀態馬上被讀到」。

全程把狀態路徑導去 tempdir，**絕不碰真 .control/**（避免擾動 live daemon）。"""
from __future__ import annotations

import json
import os
import tempfile

from book_pipeline import pipeline_run_state as prs


def _redirect(tmp: str):
    """把 prs 的狀態路徑導去 tmp，回 restore() 還原 module 全域。"""
    saved = (prs.CONTROL_DIR, prs.RUN_STATE_PATH)
    prs.CONTROL_DIR = tmp
    prs.RUN_STATE_PATH = os.path.join(tmp, 'pipeline_run_state.json')

    def restore():
        prs.CONTROL_DIR, prs.RUN_STATE_PATH = saved
    return restore


def test_default_paused_when_missing():
    restore = _redirect(tempfile.mkdtemp())
    try:
        assert not os.path.exists(prs.RUN_STATE_PATH)
        assert prs.is_paused() is True, '缺檔 → 預設暫停（fail-safe）'
    finally:
        restore()
    print('✓ run_state：狀態檔不存在 → is_paused True（fresh deploy 預設暫停，待人工啟動）')


def test_set_running_roundtrip_live():
    restore = _redirect(tempfile.mkdtemp())
    try:
        prs.set_running(True)
        assert prs.is_paused() is False, 'running:true → 運行'
        # 同進程改檔後立即反映（無 cache）→ 對應「啟動後改狀態馬上生效」
        prs.set_running(False)
        assert prs.is_paused() is True, 'running:false → 暫停（即時、無須重啟）'
        # 確認真寫進檔（原子寫，非僅記憶體）
        assert json.load(open(prs.RUN_STATE_PATH)) == {'running': False}
    finally:
        restore()
    print('✓ run_state：set_running True/False 同進程即時反映（無 cache）＋ 原子落盤')


def test_only_explicit_true_runs_else_fail_safe():
    restore = _redirect(tempfile.mkdtemp())
    try:
        # 只有顯式布林 running:true 才運行；非布林（字串）一律暫停
        with open(prs.RUN_STATE_PATH, 'w') as f:
            json.dump({'running': 'yes'}, f)
        assert prs.is_paused() is True, 'running 非布林 true（字串）→ 暫停'
        # 壞檔 → fail-safe 暫停（寧停產不亂跑）
        with open(prs.RUN_STATE_PATH, 'w') as f:
            f.write('{ broken json')
        assert prs.is_paused() is True, '壞檔 → fail-safe 暫停'
    finally:
        restore()
    print('✓ run_state：running 非布林 true / 壞檔 → 一律 fail-safe 暫停（只認顯式 running:true）')


if __name__ == '__main__':
    test_default_paused_when_missing()
    test_set_running_roundtrip_live()
    test_only_explicit_true_runs_else_fail_safe()
    print('\n全部通過 ✅')
