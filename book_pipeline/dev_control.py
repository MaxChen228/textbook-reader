#!/usr/bin/env python3
"""book_pipeline.dev_control — /dev 面板的最小寫回控制服務（docker sidecar，不暴露 host）。

職責：把 /dev 面板的控制點擊落成 `.control/` 狀態檔，讓運維可從面板互動（面板本身是 nginx
唯讀靜態、無寫回路徑）。兩類控制：
  ① zlib 帳號停用/啟用（流量控制）：停 N 帳號 → remaining 歸 0 → 買書員自然跳過 → 上限 10×(10−N)/日。
  ② 系統暫停/啟動：暫停 → daemon reactive loop 停派一切新工（pipeline_run_state，≤LOOP_POLL 生效）。
狀態 I/O 全走 dep-light 共用模組（zlib_control_state / pipeline_run_state），與 host 端單一真相。

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
  POST /pause   {paused:bool}         → 改寫運行/暫停態、回 {"ok":true,"paused":bool}
絕不 echo 任何憑證/email body 到 log。
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from book_pipeline import zlib_control_state as st
from book_pipeline import pipeline_run_state as prs

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
        body = self._read_json_body()
        if body is None:
            return self._json(400, {'ok': False, 'error': 'bad body'})
        if self.path == '/account':
            return self._post_account(body)
        if self.path == '/pause':
            return self._post_pause(body)
        return self._json(404, {'ok': False, 'error': 'not found'})

    def _read_json_body(self) -> dict | None:
        """讀 body（Content-Length 上限 4096）→ dict；長度非法/壞 json → None。"""
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n <= 0 or n > 4096:
                return None
            return json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return None

    def _post_account(self, body: dict):
        email = body.get('email')
        enabled = body.get('enabled')
        # email 須在帳號清單內（無憑證驗證，擋 typo/任意值寫入停用集）；enabled 須 bool
        if not isinstance(enabled, bool) or email not in _known_emails():
            return self._json(400, {'ok': False, 'error': 'bad params'})
        dis = st.disabled_emails()
        dis.discard(email) if enabled else dis.add(email)
        st.write_disabled(dis)
        return self._json(200, {'ok': True, **_summary()})

    def _post_pause(self, body: dict):
        # 系統暫停/啟動。sidecar 無法 signal host controller（跨容器）→ 靠 daemon 每 cycle（≤LOOP_POLL,
        # ~20s）輪詢 pipeline_run_state 生效；面板以樂觀更新填這 ≤20s 窗。
        paused = body.get('paused')
        if not isinstance(paused, bool):
            return self._json(400, {'ok': False, 'error': 'bad params'})
        prs.set_running(not paused)
        return self._json(200, {'ok': True, 'paused': prs.is_paused()})


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
