"""跨進程 CPU 工具併發閘（flock N 槽 semaphore）。

第一性原理：LLM agent 是子進程、牆鐘 90% 卡在等 API（≈0 CPU），可放心放大併發；真正吃
CPU 的是它們**內部**呼叫的確定性工具——`parser.parse_book`（大書 30–50MB content_list 的
regex 規則化）與 `pdf_contactsheet.contactsheet`（PDF 渲圖）。把這兩類重活的「同時執行數」
封頂在 ≈核數，與 agent 併發**解耦**：可放幾十個 agent 在飛，CPU 活仍不 thrashing。

為何 flock 而非 O_CREAT|O_EXCL 鎖檔：flock 在持有進程死亡時由 OS **自動釋放** → crash-safe，
絕不留死鎖（O_EXCL 鎖檔在 SIGKILL/kick -k 後會殘留，永久堵死一個槽）。

fail-open 鐵則：閘自身任何異常都直接放行——絕不因「節流器壞了」擋住整條產線。
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SLOT_DIR = os.path.join(ROOT, 'book_pipeline', '.cpu_slots')
_POLL_S = 0.2  # 全槽滿時的重試間隔（重活以秒計，0.2s 輪詢延遲可忽略）


def slots() -> int:
    """同時可跑的 CPU 重活上限。env 覆寫，否則 = 核數 - 1（留一核給系統/IO/daemon 本身）。"""
    env = os.environ.get('BOOK_PIPELINE_CPU_TOOL_CONCURRENCY')
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, (os.cpu_count() or 4) - 1)


@contextlib.contextmanager
def cpu_slot(label: str = ''):
    """阻塞取得一個 CPU 槽（最多 slots() 個並發），離開即釋放。全滿則短睡輪詢等任一釋放。"""
    n = slots()
    held = None
    try:
        os.makedirs(_SLOT_DIR, exist_ok=True)
        while held is None:
            for i in range(n):
                fd = os.open(os.path.join(_SLOT_DIR, f's{i}'), os.O_CREAT | os.O_WRONLY, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = fd
                    break
                except OSError:
                    os.close(fd)
            if held is None:
                time.sleep(_POLL_S)
    except Exception:
        # fail-open：取槽過程任何異常 → 直接放行，不節流也不報錯
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)
        except OSError:
            pass


def cpu_bound(label: str = ''):
    """裝飾 CPU 重活函式：執行期間佔一個 CPU 槽。多進程/多 agent 並發呼叫時自動封頂在 slots()。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            with cpu_slot(label):
                return fn(*a, **k)
        return wrap
    return deco
