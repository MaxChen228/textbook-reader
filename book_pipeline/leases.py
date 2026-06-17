"""(book, stage) 租約原語 —— 控制迴圈與脫離 worker 解耦的核心。

第一性原理：系統真理 = 磁碟產物 + 外部資源實況；租約不是「儲存的管線狀態」，
而是「此刻誰在跑」的可觀察事實（從活著的 pid 即可推導），故不違反「推導不儲存」總綱。

一個 (verb, slug) 對應一個租約檔。租約自我過期 —— active() 掃描時即時 reap：
  - pid 已死         → 工人正常結束/崩潰 → unlink，該 transition 重入 frontier（自癒重試）
  - pid 活著但超 TTL → 卡死 runaway     → killpg 殺整個 process group + unlink（吃掉舊 timeout-kill）
  - pid 活著且未逾時 → 真正在跑          → 視為 active，frontier 扣掉，不重複派工

故單一機制統一吃掉：重複派工、worker 卡死逾時、crash 恢復、重複 deploy。
"""

import json
import os
import signal
import time

# 與 pipeline_tick 同根：ROOT = book_pipeline 的上一層。本檔位於 book_pipeline/ 下。
_BP = os.path.dirname(os.path.abspath(__file__))
LEASE_DIR = os.path.join(_BP, '.leases')

# 預設 TTL 對齊 dispatch_llm 的 LLM_TIMEOUT（1h）；呼叫端可覆寫。
DEFAULT_TTL = int(os.environ.get('BOOK_PIPELINE_LEASE_TTL', '3600'))


def _key(verb: str, slug: str | None) -> str:
    """(verb, slug) → 檔名安全的租約 key。slug=None（如 crawl_plan）只用 verb。"""
    raw = f'{verb}_{slug}' if slug else verb
    return ''.join(c if (c.isalnum() or c in '_-.') else '_' for c in raw)


def _path(verb: str, slug: str | None) -> str:
    return os.path.join(LEASE_DIR, _key(verb, slug) + '.json')


def acquire(verb: str, slug: str | None, pid: int, ttl: int | None = None) -> str:
    """寫一張 (verb, slug) 租約，回租約檔路徑。呼叫端在 spawn 脫離 worker 後立即呼叫。"""
    os.makedirs(LEASE_DIR, exist_ok=True)
    path = _path(verb, slug)
    rec = {
        'verb': verb,
        'slug': slug,
        'pid': int(pid),
        'started_at': time.time(),
        'ttl': int(ttl if ttl is not None else DEFAULT_TTL),
    }
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(rec, f)
    os.replace(tmp, path)  # 原子寫，避免讀到半截
    return path


def release(verb: str, slug: str | None) -> None:
    """worker 正常完成後主動釋放租約（亦由 active() 在偵測 pid 已死時代為清掉）。"""
    try:
        os.unlink(_path(verb, slug))
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    """signal 0 探活：不送訊號只做權限/存在檢查。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但非本使用者（理論上不會發生，保守視為活）
    return True


def _kill_group(pid: int) -> None:
    """殺掉 worker 的整個 process group（worker 以 start_new_session=True 啟動 → pid 即 pgid）。
    TTL 本身已是寬限期，runaway 直接 SIGKILL（先 SIGTERM 給一次自清機會、不阻塞等待）。"""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return


def active(now: float | None = None, log=None) -> list[dict]:
    """掃 LEASE_DIR、即時 reap，回「真正在跑」的租約清單（每筆含 verb/slug/pid/started_at/ttl）。
    reap 副作用：死 pid → unlink；超 TTL 仍活 → killpg + unlink。log 可選（runaway 殺工通報）。"""
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        names = os.listdir(LEASE_DIR)
    except FileNotFoundError:
        return out
    for name in names:
        if not name.endswith('.json') or name.endswith('.tmp'):
            continue
        path = os.path.join(LEASE_DIR, name)
        try:
            with open(path) as f:
                rec = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        pid = int(rec.get('pid', 0))
        verb, slug = rec.get('verb'), rec.get('slug')
        alive = _pid_alive(pid)
        if not alive:
            _safe_unlink(path)  # 工人已結束（正常或崩潰）→ 釋放，transition 重入 frontier
            continue
        age = now - float(rec.get('started_at', now))
        ttl = float(rec.get('ttl', DEFAULT_TTL))
        if age > ttl:
            if log:
                log(f'⏱ 租約逾時 {verb} {slug or ""}（age={int(age)}s>ttl={int(ttl)}s，pid={pid}）→ killpg + 釋放')
            _kill_group(pid)
            _safe_unlink(path)
            continue
        out.append(rec)
    return out


def is_active(verb: str, slug: str | None, now: float | None = None) -> bool:
    """單點查詢某 (verb, slug) 是否有活租約（會順帶 reap 該筆若已死/逾時）。"""
    return any(r.get('verb') == verb and r.get('slug') == slug
              for r in active(now=now))


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
