#!/usr/bin/env python3
"""book_pipeline.test_zlib_control_state — zlib 帳號停用態 I/O ＋ 買書下載閘的回歸鎖。

兩層：
A. **狀態 I/O（zlib_control_state，dep-light）**：write_disabled/disabled_emails roundtrip；
   缺檔/壞檔 → 空集（fail-open，絕不因狀態壞而誤擋好帳號）；null/空字串濾除。
B. **crawl_zlib 整合（停用→不下載的關鍵鏈）**：account_remaining_live 對停用帳號短路回
   remaining=0/disabled=True 且**不 login**；pick_account 全停用回 None（買書員 slots 空 → 不抓）；
   **改停用檔同進程立即生效（無 cache、無須重啟 daemon）** → 正是「啟動後/暫停中再停用會生效」
   這條 live-read 的回歸鎖（使用者直接問的點）。

全程把停用態路徑導去 tempdir、mock 帳號清單與 Client（免讀 ~/.secrets、免網路），
**絕不碰真 .control/**（避免擾動 live daemon）。"""
from __future__ import annotations

import json
import os
import tempfile

from book_pipeline import crawl_zlib as cz
from book_pipeline import zlib_control_state as zcs


def _redirect(tmp: str):
    """把停用態路徑導去 tmp，回 restore()。"""
    saved = (zcs.CONTROL_DIR, zcs.ACCOUNT_STATE_PATH)
    zcs.CONTROL_DIR = tmp
    zcs.ACCOUNT_STATE_PATH = os.path.join(tmp, 'zlib_account_state.json')

    def restore():
        zcs.CONTROL_DIR, zcs.ACCOUNT_STATE_PATH = saved
    return restore


class _FakeClient:
    """假 zlib client：profile() 回固定額度；記登入次數，供「停用帳號不 login」斷言。"""
    calls = 0

    def __init__(self, account: int = 0):
        self.account = account

    def profile(self):
        _FakeClient.calls += 1
        return {'downloads_today': 2, 'downloads_limit': 10, 'isPremium': False}


def _mock_crawl(undo: list):
    """mock cz._accounts（固定兩帳號，免 ~/.secrets）+ cz.Client（fake，免網路）。把還原塞進 undo。"""
    saved_accts, saved_client = cz._accounts, cz.Client
    cz._accounts = lambda: [{'email': 'a@x', 'password': 'p'},
                            {'email': 'b@x', 'password': 'p'}]
    cz.Client = _FakeClient
    _FakeClient.calls = 0
    undo.append(lambda: (setattr(cz, '_accounts', saved_accts),
                         setattr(cz, 'Client', saved_client)))


# ── A. 狀態 I/O（dep-light，與 sidecar 共用）────────────────────────────
def test_state_roundtrip_and_fail_open():
    restore = _redirect(tempfile.mkdtemp())
    try:
        assert zcs.disabled_emails() == set(), '缺檔 → 空集（fail-open）'
        zcs.write_disabled({'a@x', 'b@x'})
        assert zcs.disabled_emails() == {'a@x', 'b@x'}
        # 壞檔 → fail-open 空集（不因狀態壞誤擋好帳號）
        with open(zcs.ACCOUNT_STATE_PATH, 'w') as f:
            f.write('{ broken')
        assert zcs.disabled_emails() == set(), '壞檔 → fail-open 空集'
        # null/空字串濾除（防人工壞檔把 email=None 帳號誤短路成停用）
        with open(zcs.ACCOUNT_STATE_PATH, 'w') as f:
            json.dump({'disabled': ['a@x', None, '']}, f)
        assert zcs.disabled_emails() == {'a@x'}
    finally:
        restore()
    print('✓ state I/O：write/read roundtrip；缺檔/壞檔 fail-open 空集；null/空字串濾除')


# ── B. crawl_zlib 整合：停用 → 不下載 ─────────────────────────────────
def test_disabled_short_circuit_no_login():
    restore = _redirect(tempfile.mkdtemp())
    undo = []
    _mock_crawl(undo)
    try:
        zcs.write_disabled({'a@x'})                  # 只停 acct0
        r0 = cz.account_remaining_live(0)
        assert r0['disabled'] is True and r0['remaining'] == 0 and r0['limit'] == 0
        assert _FakeClient.calls == 0, '停用帳號短路、不 login（省查詢＋流量控制）'
        # 未停用帳號照常查（走 fake client）→ remaining = 10-2 = 8
        r1 = cz.account_remaining_live(1)
        assert r1['disabled'] is False and r1['remaining'] == 8
        assert _FakeClient.calls == 1, '只有未停用帳號才 login'
    finally:
        for u in undo:
            u()
        restore()
    print('✓ crawl_zlib：停用帳號 account_remaining_live 短路 remaining=0/disabled=True 且不 login')


def test_pick_account_none_when_all_disabled():
    restore = _redirect(tempfile.mkdtemp())
    undo = []
    _mock_crawl(undo)
    try:
        assert cz.pick_account() is not None, '全啟用 → 有額度帳號可選'
        zcs.write_disabled({'a@x', 'b@x'})           # 全停用
        assert cz.pick_account() is None, '全停用 → pick_account None → 買書員無槽可抓（下載路徑切斷）'
    finally:
        for u in undo:
            u()
        restore()
    print('✓ crawl_zlib：全帳號停用 → pick_account 回 None（買書員下載路徑被切斷）')


def test_disable_takes_effect_live_no_restart():
    """**使用者直接問的回歸鎖**：同進程改停用檔後立刻被讀到（無 cache）→ 啟動後/暫停中再停用會生效。"""
    restore = _redirect(tempfile.mkdtemp())
    undo = []
    _mock_crawl(undo)
    try:
        # t0：acct1 啟用、查得到額度
        assert cz.account_remaining_live(1)['disabled'] is False
        # t1：同進程停用 acct1（模擬 daemon 已起、暫停中按下停用）→ 下次讀立即反映
        zcs.write_disabled({'b@x'})
        r = cz.account_remaining_live(1)
        assert r['disabled'] is True and r['remaining'] == 0, '改檔後立即生效（無 cache、無須重啟）'
    finally:
        for u in undo:
            u()
        restore()
    print('✓ crawl_zlib：停用態改檔同進程即時反映（live-read，無 cache）→ 啟動後再停用會生效')


if __name__ == '__main__':
    test_state_roundtrip_and_fail_open()
    test_disabled_short_circuit_no_login()
    test_pick_account_none_when_all_disabled()
    test_disable_takes_effect_live_no_restart()
    print('\n全部通過 ✅')
