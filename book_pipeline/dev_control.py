#!/usr/bin/env python3
"""book_pipeline.dev_control — /dev 面板的最小寫回控制服務（docker sidecar，不暴露 host）。

唯一職責：把 /dev 面板對 zlib 帳號的「停用/啟用」點擊落成停用態狀態檔，讓流量控制可從面板
互動（面板本身是 nginx 唯讀靜態、無寫回路徑）。停用 N 帳號 → 該帳號 remaining 歸 0 →
買書員（drain_crawl_queue，每 tick 權威 live 查停用態）自然跳過 → 當日上限 10×(10−N)/日。

安全邊界（縱深三層）：
  1. CF Access 信箱閘（app textbook-dev，只給 max970228）= 邊界第一道。
  2. nginx `location ^~ /dev/api/`：無 Cf-Access-Authenticated-User-Email header → 403，
     並把該 header proxy 給本服務。
  3. 本服務復驗 header == max970228@gmail.com 才動狀態檔（縱使 nginx 配置漏勾也擋）。
本服務**不 publish port**（只在 compose 內網 devcontrol:8002，nginx 經內網 DNS proxy_pass）→
零 host/Tailscale 暴露面。**零憑證**：不掛 ~/.secrets、不 import crawl_zlib（其頂層 import
requests＋讀憑證檔）；email 合法性改用 dev/zlib_quota.json 的 account email 清單（無密碼）驗證。
狀態 I/O 與 host 端 crawl_zlib 共用 zlib_control_state（dep-light 單一真相）。

端點：
  GET  /health                       → {"ok":true}
  POST /account {email,enabled:bool} → 改寫停用集、回 {"ok":true,"disabled":[...],"daily_cap":N}
絕不 echo 任何憑證/email body 到 log。
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from book_pipeline import zlib_control_state as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZLIB_QUOTA = os.path.join(ROOT, 'dev', 'zlib_quota.json')  # 唯讀：取 account email 清單（無密碼）驗證
ALLOWED_EMAIL = 'max970228@gmail.com'
CF_HEADER = 'Cf-Access-Authenticated-User-Email'
PORT = int(os.environ.get('DEV_CONTROL_PORT', '8002'))
PER_ACCOUNT_QUOTA = 10  # z-library 免費每帳號 10 下載/日


def _known_emails() -> list[str]:
    """帳號 email 清單（無密碼）：取自 dev/zlib_quota.json（devsnapshot/買書員權威回寫）。
    sidecar 不得碰 ~/.secrets 憑證，故 email 合法性以此清單為準。檔缺/壞 → 空清單。"""
    try:
        d = json.load(open(ZLIB_QUOTA))
        return [a.get('email') for a in (d.get('accounts') or []) if a.get('email')]
    except Exception:
        return []


def _summary() -> dict:
    known = _known_emails()
    dis = st.disabled_emails() & set(known)  # 只算仍存在的帳號（理論壞檔殘留 email 不計入 cap）
    cap = PER_ACCOUNT_QUOTA * max(0, len(known) - len(dis))
    return {'disabled': sorted(st.disabled_emails()), 'daily_cap': cap}


class Handler(BaseHTTPRequestHandler):
    server_version = 'devcontrol/1'

    def log_message(self, fmt, *args):
        # 絕不 echo body（含 email）；只把 method+path+status 記到 stderr（docker logs）。
        sys.stderr.write('devcontrol %s\n' % (fmt % args))

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            return self._json(200, {'ok': True})
        return self._json(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        # 縱深防禦：nginx 已擋空 header，這裡復驗精確 email（單一身分）
        if self.headers.get(CF_HEADER) != ALLOWED_EMAIL:
            return self._json(403, {'ok': False, 'error': 'forbidden'})
        if self.path != '/account':
            return self._json(404, {'ok': False, 'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n <= 0 or n > 4096:
                return self._json(400, {'ok': False, 'error': 'bad length'})
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return self._json(400, {'ok': False, 'error': 'bad json'})
        email = body.get('email')
        enabled = body.get('enabled')
        # email 須在帳號清單內（無憑證驗證，擋 typo/任意值寫入停用集）；enabled 須 bool
        if not isinstance(enabled, bool) or email not in _known_emails():
            return self._json(400, {'ok': False, 'error': 'bad params'})
        dis = st.disabled_emails()
        dis.discard(email) if enabled else dis.add(email)
        st.write_disabled(dis)
        return self._json(200, {'ok': True, **_summary()})


def main() -> int:
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)  # 0.0.0.0 僅 compose 內網（未 publish）
    sys.stderr.write(f'devcontrol listening :{PORT}\n')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
