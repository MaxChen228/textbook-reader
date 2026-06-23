#!/usr/bin/env python3
"""book_pipeline.test_crawl_zlib_quota — zlib 額度查詢核心回歸鎖。

disable 流量控制 2026-06-23 移除後（連同 test_zlib_control_state.py 一併刪），買書員與
crawl 額度耗盡閘門仍依賴這條額度查詢分母——本檔鎖住保留路徑（取代舊測試對核心的覆蓋）：
- account_remaining_live：真查 profile 回 used/limit/remaining/premium（**無 disabled 欄、不再短路**）；
  登入失敗回 remaining=None（不誤判為 0）。
- pick_account：選 remaining 最多者；全耗盡回 None（買書員無槽）；登入失敗帳號跳過、不誤選。

全程 mock cz._accounts + cz.Client（免讀 ~/.secrets、免網路）。"""
from __future__ import annotations

from book_pipeline import crawl_zlib as cz


class _FakeClient:
    """假 zlib client：profile() 依 account 回不同額度；account==9 模擬登入失敗（拋 SystemExit）。
    used: acct0=8(剩2)、acct1=2(剩8)、其餘=5(剩5)；premium: acct>=1。"""
    def __init__(self, account: int = 0):
        self.account = account

    def profile(self):
        if self.account == 9:
            raise SystemExit('登入失敗（mock）')
        used = {0: 8, 1: 2}.get(self.account, 5)
        return {'downloads_today': used, 'downloads_limit': 10,
                'isPremium': self.account >= 1}


def _mock(undo: list, n: int = 3):
    saved = (cz._accounts, cz.Client)
    cz._accounts = lambda: [{'email': f'a{i}@x', 'password': 'p'} for i in range(n)]
    cz.Client = _FakeClient
    undo.append(lambda: (setattr(cz, '_accounts', saved[0]),
                         setattr(cz, 'Client', saved[1])))


def test_account_remaining_live_real_query():
    undo = []
    _mock(undo)
    try:
        r = cz.account_remaining_live(1)
        assert r['used'] == 2 and r['limit'] == 10 and r['remaining'] == 8
        assert r['premium'] is True
        assert 'disabled' not in r, 'disabled 欄已隨 disable 系統移除'
    finally:
        for u in undo:
            u()
    print('✓ account_remaining_live 真查 profile 回 used/limit/remaining/premium、無 disabled 欄')


def test_account_remaining_live_login_failure():
    undo = []
    _mock(undo, n=10)
    try:
        r = cz.account_remaining_live(9)
        assert r['remaining'] is None and r['limit'] is None, '登入失敗 remaining/limit=None'
        assert 'error' in r, '登入失敗帶 error'
        assert 'disabled' not in r
    finally:
        for u in undo:
            u()
    print('✓ account_remaining_live 登入失敗回 remaining=None（不誤判為 0）')


def test_pick_account_picks_max_remaining():
    undo = []
    _mock(undo)  # acct0 剩2、acct1 剩8、acct2 剩5
    try:
        assert cz.pick_account() == 1, '選 remaining 最多者（acct1=8）'
    finally:
        for u in undo:
            u()
    print('✓ pick_account 選 remaining 最多者')


def test_pick_account_none_when_all_exhausted():
    saved = (cz._accounts, cz.Client)
    cz._accounts = lambda: [{'email': f'a{i}@x', 'password': 'p'} for i in range(2)]

    class _Exhausted:
        def __init__(self, account=0):
            pass

        def profile(self):
            return {'downloads_today': 10, 'downloads_limit': 10, 'isPremium': False}
    cz.Client = _Exhausted
    try:
        assert cz.pick_account() is None, '全帳號耗盡 → None（買書員無槽可抓）'
    finally:
        cz._accounts, cz.Client = saved
    print('✓ pick_account 全耗盡回 None')


def test_pick_account_skips_login_failure():
    undo = []
    _mock(undo, n=10)  # acct9 登入失敗（remaining=None），其餘有額度（max=acct1 剩8）
    try:
        acct = cz.pick_account()
        assert acct is not None and acct != 9, '登入失敗帳號跳過、不誤選'
    finally:
        for u in undo:
            u()
    print('✓ pick_account 跳過登入失敗帳號（remaining=None 不誤選）')


if __name__ == '__main__':
    test_account_remaining_live_real_query()
    test_account_remaining_live_login_failure()
    test_pick_account_picks_max_remaining()
    test_pick_account_none_when_all_exhausted()
    test_pick_account_skips_login_failure()
    print('\n全部通過 ✅')
