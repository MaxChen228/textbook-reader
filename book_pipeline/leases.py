"""(book, stage) 租約原語 —— 控制迴圈與脫離 worker 解耦的核心。

第一性原理：系統真理 = 磁碟產物 + 外部資源實況；租約不是「儲存的管線狀態」，
而是「此刻誰在跑」的可觀察事實（從活著的 pid 推導），故不違反「推導不儲存」總綱。

一個 (verb, slug) 對應一個租約檔。租約自我過期 —— active() 掃描時即時 reap：
  - pid 已死/被回收  → 工人結束/崩潰 → unlink，該 transition 重入 frontier（自癒重試）
  - pid 活且超 TTL   → 卡死 runaway → 兩段式殺（先 SIGTERM 留租約寬限、下個 scan 才 SIGKILL）
  - pid 活且未逾時   → 真正在跑     → 視 active，frontier 扣掉，不重複派工

故單一機制統一吃掉：重複派工、worker 卡死逾時、crash 恢復、重複 deploy。

pid 重用防護：acquire 時用 `ps -o lstart=,comm=` 擷取進程「身分 token」（啟動時刻+命令名）
存入租約；active 重查並比對——pid 被 OS 回收給無關進程時 token 不符 → 視為死、unlink 但
**絕不 killpg**（那是別人的進程）。lstart 解析度 1 秒，疊加 comm 比對，誤判機率可忽略。

並發假設：active() 會做 reap 副作用（unlink/kill）。跨進程由 launchd flock 序列化 tick；
進程內（reactive 模式下 controller 掃描+reap 與多 worker thread 的 acquire/release 並發操作同一
LEASE_DIR）由模組級 _lock 序列化 → acquire/release/active 皆 thread-safe（破壞性 reap 的「單一
呼叫者」前提在進程內靠此鎖成立，非僅靠 identity-token + atomic-replace 湊巧兜底）。
"""

import hashlib
import json
import os
import signal
import subprocess
import threading
import time

# 與 pipeline_tick 同根：本檔位於 book_pipeline/ 下，_BP 即指 book_pipeline/。
_BP = os.path.dirname(os.path.abspath(__file__))
LEASE_DIR = os.path.join(_BP, '.leases')

# 預設 TTL **0 = 無限**（對齊 dispatch_llm 的 LLM_TIMEOUT 取消上限）：runaway 殺由 codex 主力
# 後已無病理，TTL 殺只會從另一條路誤殺真複雜的長 agent。crash 恢復不靠 TTL——pid 已死的孤兒租約
# 仍即時釋放（見 active() 上方分支）。env 可重設正整數臨時重新加上限（與 LLM_TIMEOUT 同步重設）。
DEFAULT_TTL = int(os.environ.get('BOOK_PIPELINE_LEASE_TTL', '0'))
# runaway SIGTERM 後給多久自清，逾此 active() 才補 SIGKILL（對齊 pipeline_tick 既有 5s 寬限，
# 但兩段式跨 scan、非阻塞 sleep）。
KILL_GRACE = int(os.environ.get('BOOK_PIPELINE_LEASE_KILL_GRACE', '5'))

# 進程內序列化 acquire/release/active（見模組 docstring「並發假設」）。reactive 模式下 reap 與
# worker 的 acquire/release 並發同一 LEASE_DIR；此鎖把「單一呼叫者」前提在進程內坐實。
_lock = threading.Lock()


# 檔名長度上限：macOS（APFS/HFS+）單一檔名 ≤255 bytes。租約檔尾接 `.json`，原子寫再加 `.tmp`
# → 後綴最多 9 bytes，故 base 名保守封頂 200。逾此（呼叫端把「整批 slug」join 成單一 slug 傳入，
# 如 crawl_resolve 一次解析數十本書）必爆 ENAMETOOLONG → 整個 worker 異常空轉。primitive 不變式：
# _key 永遠回合法長度檔名，與呼叫端語意無關。
_KEY_MAX = 200


def _key(verb: str, slug: str | None) -> str:
    """(verb, slug) → 檔名安全的租約 key。slug=None（如 crawl_plan）只用 verb。
    超長（呼叫端把整批 id/slug join 成單一 slug）→ 截斷 + 全文 sha1 後綴：保長度合法、且
    determinism 不破（同 (verb,slug)→同 key、不同長 key 雜湊近不碰撞）。"""
    raw = f'{verb}_{slug}' if slug else verb
    safe = ''.join(c if (c.isalnum() or c in '_-.') else '_' for c in raw)
    if len(safe) <= _KEY_MAX:
        return safe
    digest = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]
    return f'{safe[:_KEY_MAX - 17]}_{digest}'  # 17 = '_' + 16 hex，總長 = _KEY_MAX


def _path(verb: str, slug: str | None) -> str:
    return os.path.join(LEASE_DIR, _key(verb, slug) + '.json')


def _proc_identity(pid: int) -> str | None:
    """進程身分 token = 啟動時刻(lstart)+命令名(comm)。pid 不存在回 None。
    用於防 pid 重用：同 pid 但 token 不同 = OS 把 pid 回收給別的進程。"""
    if pid <= 0:
        return None
    try:
        r = subprocess.run(['ps', '-o', 'lstart=,comm=', '-p', str(pid)],
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    tok = r.stdout.strip()
    return tok or None


def acquire(verb: str, slug: str | None, pid: int, ttl: int | None = None) -> str:
    """寫一張 (verb, slug) 租約，回租約檔路徑。呼叫端在 spawn 脫離 worker 後立即呼叫。"""
    with _lock:
        os.makedirs(LEASE_DIR, exist_ok=True)
        path = _path(verb, slug)
        rec = {
            'verb': verb,
            'slug': slug,
            'pid': int(pid),
            'identity': _proc_identity(int(pid)),  # pid 重用二次校驗用
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
    with _lock:
        _safe_unlink(_path(verb, slug))


def _killpg(pid: int, sig) -> None:
    """對 worker 的 process group 發一個訊號（worker 以 start_new_session=True 啟動 → pid=pgid）。
    呼叫端已用 identity token 確認是「我們的」進程才呼叫，故不會誤殺。"""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def active(now: float | None = None, log=None) -> list[dict]:
    """掃 LEASE_DIR、即時 reap，回「真正在跑」的租約清單（每筆含 verb/slug/pid/started_at/ttl）。
    reap 副作用見模組 docstring。進程內由 _lock 序列化（reactive 安全），跨進程由 launchd flock。
    log 可選（kill 通報）。"""
    with _lock:
        return _active_locked(time.time() if now is None else now, log)


def _active_locked(now: float, log) -> list[dict]:
    out: list[dict] = []
    try:
        names = os.listdir(LEASE_DIR)
    except FileNotFoundError:
        return out
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(LEASE_DIR, name)
        try:
            with open(path) as f:
                rec = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        pid = int(rec.get('pid', 0))
        verb, slug = rec.get('verb'), rec.get('slug')
        ident = _proc_identity(pid)
        # pid 已死，或 pid 被回收給別人（token 不符）→ 工人已終結 → 釋放，絕不殺
        if ident is None or ident != rec.get('identity'):
            _safe_unlink(path)
            continue
        age = now - float(rec.get('started_at', now))
        ttl = float(rec.get('ttl', DEFAULT_TTL))
        if ttl <= 0 or age <= ttl:  # ttl<=0 ⇒ 無限（不按時殺），只剩 pid-死回收兜底
            out.append(rec)  # 健康、真正在跑
            continue
        # age > ttl → runaway，兩段式殺（非阻塞、跨 scan 給寬限）
        termed_at = rec.get('termed_at')
        if termed_at is None:
            if log:
                log(f'⏱ 租約逾時 {verb} {slug or ""}（age={int(age)}s>ttl={int(ttl)}s，pid={pid}）→ SIGTERM，寬限 {KILL_GRACE}s')
            _killpg(pid, signal.SIGTERM)
            rec['termed_at'] = now
            _rewrite(path, rec)  # 留租約：frontier 仍扣（殺人進行中不重派）
        elif now - float(termed_at) >= KILL_GRACE:
            if log:
                log(f'⏱ 寬限到 {verb} {slug or ""}（pid={pid}）→ SIGKILL + 釋放')
            _killpg(pid, signal.SIGKILL)
            _safe_unlink(path)
        # else：寬限中，保留租約、等下個 scan
    return out


def is_active(verb: str, slug: str | None, now: float | None = None) -> bool:
    """單點查詢某 (verb, slug) 是否有活租約（會順帶 reap）。接線後高頻查詢應改用 active() 一次回 set。"""
    return any(r.get('verb') == verb and r.get('slug') == slug
              for r in active(now=now))


def _rewrite(path: str, rec: dict) -> None:
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ── 孤兒 agent 進程回收 ───────────────────────────────────────────────────────
# 病理：controller 被 kick -k 硬殺（SIGKILL 跳過 finally、不對子工 process group 發 SIGTERM）或
# 崩潰 → 其 codex/claude 子進程孤兒化（reparent 到 launchd，PPID==1）。LLM_TIMEOUT 移除後
# （codex 主力卡死病理消失）孤兒 codex 再無自我超時、永不自死 → 每次 kick 累積 → load 飆升、
# 餓死觀測心跳。leases.active() 的 TTL=0 無限 + identity 仍相符 → 把活著的孤兒當「健康在跑」
# 永不殺（或 lease 檔已清則純隱形）→ 兩路都讓孤兒永生。解法：controller 啟動 + 週期主動回收。
#
# 鑑別子（全部成立才算「本 repo 的孤兒 agent」，絕不誤殺）：
#   PPID==1（父已死）∧ daemon agent 簽名（'codex' ∨ 'claude'+'--add-dir'）∧ 屬本 repo
#   （ROOT 在 argv，或 argv 缺 ROOT 的 node 孫程序退而查 cwd==ROOT）。
# 安全性：活 controller 的子 agent fork 即 PPID==controller（非 1，永不誤命中）；互動
# Claude Code session（`claude --dangerously-skip-permissions`）無 'codex'、無 '--add-dir'、
# argv 無 ROOT → 三重排除。startup 呼叫時 flock 已序列化（前代必已死）；運行中呼叫亦安全
# （fork 語意保證活子工非 PPID==1）。
_ROOT = os.path.dirname(_BP)
_ORPHAN_CACHE = {'at': 0.0, 'val': {'count': 0, 'oldest_sec': 0}}
_ORPHAN_CACHE_TTL = 10  # count_orphans 走 1s snapshot 熱路徑 → 快取避免每次 ps


def _parse_etime(et: str) -> int:
    """macOS ps etime（[[dd-]hh:]mm:ss）→ 秒。macOS 無 etimes(raw)，只能解格式化串。"""
    try:
        et = et.strip()
        days = 0
        if '-' in et:
            d, et = et.split('-', 1)
            days = int(d)
        parts = [int(p) for p in et.split(':')]
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        else:
            h, m, s = 0, 0, parts[0]
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0


def _cwd_is(pid: int, root: str) -> bool:
    """lsof 查 pid 的 cwd 是否在 root 下（僅對 argv 缺 ROOT 的 node 孫程序兜底，故呼叫稀少）。"""
    try:
        out = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return root in out


def _agent_orphans(root: str | None = None) -> list[dict]:
    """掃出前代 controller 遺留的孤兒 agent 進程（判據見上方註解）。回 [{pid, pgid, age_sec}]。"""
    root = root or _ROOT
    try:
        out = subprocess.run(['ps', '-axo', 'pid=,ppid=,etime=,command='],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    res = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, et_s, cmd = parts
        if ppid_s != '1':
            continue
        low = cmd.lower()
        sig = ('codex' in low) or ('claude' in low and '--add-dir' in cmd)
        if not sig:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        # 專案歸屬：ROOT 在 argv（codex `-C ROOT` / claude `--add-dir ROOT`，cheap）；
        # 缺者（codex node 孫程序 argv 未必帶 ROOT）退查 cwd==ROOT。
        if root not in cmd and not _cwd_is(pid, root):
            continue
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        res.append({'pid': pid, 'pgid': pgid, 'age_sec': _parse_etime(et_s)})
    return res


def count_orphans(root: str | None = None) -> dict:
    """唯讀觀測：{count, oldest_sec}（10s 快取，避免 1s snapshot 熱路徑每次 ps）。/dev 健康欄用。"""
    now = time.time()
    if now - _ORPHAN_CACHE['at'] < _ORPHAN_CACHE_TTL:
        return dict(_ORPHAN_CACHE['val'])
    orphs = _agent_orphans(root)
    val = {'count': len(orphs),
           'oldest_sec': max((o['age_sec'] for o in orphs), default=0)}
    _ORPHAN_CACHE['at'] = now
    _ORPHAN_CACHE['val'] = val
    return dict(val)


def reap_orphans(root: str | None = None, log=None) -> dict:
    """殺掉所有孤兒 agent process group（SIGKILL）。回 {reaped, groups, oldest_sec}。
    安全性見上方註解（永不碰活 controller 的子 worker）。controller 啟動 + 週期呼叫。"""
    orphs = _agent_orphans(root)
    if not orphs:
        return {'reaped': 0, 'groups': 0, 'oldest_sec': 0}
    oldest = max(o['age_sec'] for o in orphs)
    groups: set[int] = set()
    for o in orphs:
        pg = o['pgid']
        if pg not in groups:
            groups.add(pg)
            try:
                os.killpg(pg, signal.SIGKILL)  # 殺整組 → 連帶同組 node 孫程序
            except OSError:
                pass
        try:
            os.kill(o['pid'], signal.SIGKILL)  # 個別兜底（脫離 group 者）
        except OSError:
            pass
    _ORPHAN_CACHE['at'] = 0.0  # 剛殺完 → 失效快取，下次觀測重算
    if log:
        h, m = oldest // 3600, (oldest % 3600) // 60
        log(f'🧟 回收 {len(orphs)} 個孤兒 agent 進程（{len(groups)} 組，最老 {h}h{m}m）— 前代 controller 殘留')
    return {'reaped': len(orphs), 'groups': len(groups), 'oldest_sec': oldest}
