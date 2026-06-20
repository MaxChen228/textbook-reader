#!/usr/bin/env python3
"""book_pipeline.agent_history — 每本書的 LLM agent 完整歷程歸檔（append-only，/dev 抽屜歷史源）。

worker_registry 是「此刻誰在跑」的 live 面板（記憶體、recent 砍到 200 條、label 砍到 240 字）；
本模組是「跑過哪些 session、完整做了什麼」的持久史，兩者互補。三維度都在 pipeline_tick
的 _run_one() 收斂後才進來，故此處 schema 統一：
  verb    = audit / catalog_audit / math_sweep / qc / sol_extract / crawl（書單查證；舊歸檔或記 crawl_plan）（6 種 LLM 任務）
  harness = claude-cli（claude；舊歸檔的 kimi 記錄亦歸此）/ codex-cli（codex 與 codex-pool 同一 CLI）/ ccnexus-http（math_sweep 走 HTTP batch）
  model   = claude / gpt-5.4（由 provider 推導，caller 傳入；kimi 已下架，僅存於舊歸檔）

生命週期（與 worker_registry 並行呼叫，但寫【完整原文】不截字、不封頂）：
  start()  ── 派工起頭，建 in-mem session meta。
  event()  ── 每個工具調用 / LLM 發言 → 完整一行寫 sessions/<id>.jsonl（每行一事件）。
  finish() ── 收尾（含 timeout / 撞額度的失敗 session 也記）→ 摘要 append 到 index.json。

corpus-level（slug=None）的 math_sweep / crawl_plan：session 記 slug=None，事後由 caller 以
set_touched() 回填它實際改動的書清單 → 每書抽屜以「slug 命中 OR ∈ touched」查得。

落地 dev/agent_history/（gitignore，比照 workers.json/status.json，standby 機器產物各機獨立）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DIR = os.path.join(ROOT, 'dev', 'agent_history')
SESS_DIR = os.path.join(HIST_DIR, 'sessions')
INDEX_PATH = os.path.join(HIST_DIR, 'index.json')

# 全域上限：index 超出即刪最舊（含其 JSONL）。append 序≈時間序，故砍頭。env 可覆寫。
MAX_SESSIONS = int(os.environ.get('BOOK_PIPELINE_HISTORY_CAP', '3000'))
# reconcile：JSONL 在、index 無對應 row（finish 沒跑成——daemon 被砍 / daemon thread 退出未走
# finally）。index 是 JSONL 的【衍生快取】，可重建 → 不刪、改【從 JSONL 還原 row】。pid 已死即還原；
# pid 仍活但 JSONL 逾此秒數沒更新（pid 重用嫌疑）也還原；其餘留給其 live session 自行 finish。
_ORPHAN_AGE_S = 3600

_lock = threading.Lock()
_sessions: dict[str, dict] = {}  # key -> in-mem meta（start→finish 生命週期；崩潰後新進程自然空）
_last_by_verb: dict[str, str] = {}  # verb -> 本進程最後 finish 的 session id（set_touched 精準回填用）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _harness_of(provider: str) -> str:
    if provider in ('codex', 'codex-pool'):  # codex-pool = codex CLI 走 ccNexus 池（對齊 _is_codex）
        return 'codex-cli'
    if provider == 'ccnexus':
        return 'ccnexus-http'  # math_sweep 走 ccNexus /v1/chat/completions（HTTP batch，非 CLI harness）
    return 'claude-cli'  # claude（及舊歸檔的 kimi 記錄）同一 CLI harness


def _session_id(slug: str | None, verb: str, pid: int, started: str) -> str:
    # 排序友善（時戳前綴）+ 可讀 + 唯一（pid 區分同秒同 verb 的並行子工）
    ts = started.replace('+00:00', 'Z').replace('-', '').replace(':', '')
    return f'{ts}-{verb}-{slug or "corpus"}-{pid}'


def _load_index() -> tuple[list, bool]:
    """讀 index → (rows, ok)。檔不存在 → ([], True)（正常空）。
    毀損（JSON 壞）→ 改名保全該檔後 ([], True)（可安全重起，舊資料留 .corrupt-* 供搶救）。
    暫時性讀取失敗（OSError，非毀損）→ ([], False)：寫端據此【放棄本次寫入】，
    絕不以空清單覆寫 index.json（否則一次讀失敗就抹掉全部歷史）。"""
    if not os.path.exists(INDEX_PATH):
        return [], True
    try:
        with open(INDEX_PATH, encoding='utf-8') as f:
            return (json.load(f) or []), True
    except (json.JSONDecodeError, ValueError):
        try:
            os.replace(INDEX_PATH, INDEX_PATH + '.corrupt-'
                       + _now().replace(':', '').replace('+00:00', 'Z'))
        except OSError:
            pass
        return [], True
    except OSError:
        return [], False


def _read_index() -> list:
    """讀端便利包裝：拿不到就回空（讀端 graceful，不影響資料安全）。"""
    return _load_index()[0]


def _write_index_locked(rows: list) -> None:
    os.makedirs(HIST_DIR, exist_ok=True)
    tmp = INDEX_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, INDEX_PATH)  # 原子寫，避免網頁/devctl 讀到半截


def _sess_path(sid: str) -> str:
    return os.path.join(SESS_DIR, sid + '.jsonl')


def _meta_path(sid: str) -> str:
    return os.path.join(SESS_DIR, sid + '.meta.json')


def _write_meta(sid: str, meta: dict) -> None:
    """旁路 metadata（provider/harness/model）：start() 即落盤，供 controller 被 SIGKILL（繞過 finish）
    後 reconcile 從 JSONL 重建時讀回——否則重建 row 的 provider 只能 null。原子寫，best-effort。"""
    try:
        os.makedirs(SESS_DIR, exist_ok=True)
        tmp = _meta_path(sid) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, _meta_path(sid))
    except OSError:
        pass  # sidecar 純加分；寫不成最多退回 provider=null，不影響主流程


def _read_meta(sid: str) -> dict:
    try:
        with open(_meta_path(sid), encoding='utf-8') as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _remove_meta(sid: str) -> None:
    try:
        os.remove(_meta_path(sid))
    except OSError:
        pass


def _pid_of(sid: str) -> int | None:
    """從 session id 末段取回子工 pid（_session_id 以 -<pid> 結尾）。"""
    tail = sid.rsplit('-', 1)
    return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else None


def _pid_alive(pid: int) -> bool:
    """signal 0 探活：不存在→False；無權 signal（別 user）→保守視為活、不重建。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _reconstruct_row(sid: str, path: str) -> dict | None:
    """從孤兒 JSONL 還原一筆 index row（finish 沒跑成的補登記）。id 已編碼 ts/verb/slug；
    events/calls/ended 由 JSONL 內容導出；provider/harness/model 從 start() 落的 sidecar 讀回
    （無 sidecar 才退回 None）。rc 無從還原 → None，並標 reconstructed=True 供 UI 中性呈現：
    finish 從未執行 → 結束碼本就未知，不謊報成功亦不謊報失敗。"""
    parts = sid.split('-')
    if len(parts) < 4:
        return None
    ts, verb = parts[0], parts[1]
    slug = '-'.join(parts[2:-1])  # corpus session 的 slug 段為字面 "corpus"
    slug = None if slug == 'corpus' else slug
    try:
        started_dt = datetime.strptime(ts, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    calls = events = 0
    last_t = None
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                events += 1
                if ev.get('kind') == 'tool':
                    calls += 1
                if ev.get('t'):
                    last_t = ev['t']
    except OSError:
        return None
    started = started_dt.isoformat(timespec='seconds')
    ended = last_t or started
    try:
        dur = int((datetime.fromisoformat(ended) - started_dt).total_seconds())
    except Exception:
        dur = None
    meta = _read_meta(sid)  # start() 落的 provider/harness/model（被 SIGKILL 繞過 finish 時的唯一來源）
    return {
        'id': sid, 'slug': slug, 'verb': verb, 'provider': meta.get('provider'),
        'harness': meta.get('harness'), 'model': meta.get('model'),
        'started': started, 'ended': ended, 'duration_s': dur,
        'total_calls': calls, 'events': events, 'rc': None, 'ok': None,
        'touched': [], 'reconstructed': True}


# ── 生命週期 ───────────────────────────────────────────────────────────────────
def start(key: str, slug: str | None, verb: str, pid: int, provider: str, model: str) -> None:
    started = _now()
    with _lock:
        os.makedirs(SESS_DIR, exist_ok=True)  # 一次建好；event() 熱路徑不再每事件 makedirs
        sid = _session_id(slug, verb, pid, started)
        _sessions[key] = {
            'id': sid, 'slug': slug, 'verb': verb,
            'pid': pid, 'provider': provider, 'harness': _harness_of(provider),
            'model': model, 'started': started, 'calls': 0, 'events': 0}
        # 旁路 metadata：provider/harness/model 即落 sidecar。index 維持「只記已收尾/重建 row」不變量
        # （running session 不污染歷史面板）；若 controller 被 SIGKILL 繞過 finish，reconcile 從 JSONL
        # 重建時讀此 sidecar 補回 provider（否則只能 null＝原 bug 的成因之一）。
        _write_meta(sid, {'provider': provider, 'harness': _harness_of(provider), 'model': model})


def event(key: str, kind: str, label: str) -> None:
    """完整原文寫盤（不截字、不封頂）。kind='tool'（計入 total_calls）或 'text'（LLM 發言）。"""
    with _lock:
        s = _sessions.get(key)
        if not s:
            return
        if kind == 'tool':
            s['calls'] += 1
        s['events'] += 1
        line = json.dumps({'t': _now(), 'kind': kind, 'label': label}, ensure_ascii=False)
        with open(_sess_path(s['id']), 'a', encoding='utf-8') as f:
            f.write(line + '\n')  # 逐行 flush：crash-safe，已寫的事件不丟


def finish(key: str, rc: int) -> str | None:
    """收尾：摘要 append 到 index.json，回 session id。timeout/撞額度的失敗也記（rc!=0, ok=False）。"""
    ended = _now()
    with _lock:
        s = _sessions.pop(key, None)
        if not s:
            return None
        try:
            dur = int((datetime.fromisoformat(ended)
                       - datetime.fromisoformat(s['started'])).total_seconds())
        except Exception:
            dur = None
        os.makedirs(SESS_DIR, exist_ok=True)
        sp = _sess_path(s['id'])
        if not os.path.exists(sp):
            open(sp, 'a', encoding='utf-8').close()  # 零事件 session 也留空檔，免網頁 fetch 404
        row = {
            'id': s['id'], 'slug': s['slug'], 'verb': s['verb'], 'provider': s['provider'],
            'harness': s['harness'], 'model': s['model'], 'started': s['started'],
            'ended': ended, 'duration_s': dur, 'total_calls': s['calls'],
            'events': s['events'], 'rc': rc, 'ok': rc == 0, 'touched': []}
        rows, ok = _load_index()
        if not ok:
            return s['id']  # index 暫時讀不到 → 跳過歸檔（JSONL 仍在），絕不覆寫抹除全史
        # 冪等：若 reconcile 已先還原同 id（慢 session 被誤判 stale）→ 以權威 finish 取代之
        for i, r in enumerate(rows):
            if r.get('id') == row['id']:
                rows[i] = row
                break
        else:
            rows.append(row)
        _prune_locked(rows)
        _write_index_locked(rows)
        _remove_meta(s['id'])  # 權威 row 已落 index → sidecar 冗餘，清掉
        _last_by_verb[s['verb']] = s['id']  # set_touched 精準回填用（本進程最後一場該 verb）
        return s['id']


def set_touched(verb: str, touched: list[str]) -> str | None:
    """corpus-level 收尾回填：把【本進程剛 finish 的那場該 verb session】標記其實際改動的書清單。
    用 finish 記下的確切 id 定位（非掃 index 猜「最新一筆 verb」）→ 即使未來並行 corpus 作業也不標錯。"""
    with _lock:
        sid = _last_by_verb.get(verb)
        if not sid:
            return None
        rows, ok = _load_index()
        if not ok:
            return None
        for r in rows:
            if r.get('id') == sid:
                r['touched'] = list(touched)
                _write_index_locked(rows)
                return sid
    return None


def _prune_locked(rows: list) -> None:
    while len(rows) > MAX_SESSIONS:
        old = rows.pop(0)
        for pth in (_sess_path(old['id']), _meta_path(old['id'])):
            try:
                os.remove(pth)
            except OSError:
                pass


# ── 讀端（devctl / CLI；網頁直接 fetch JSONL）────────────────────────────────────
def sessions_for(slug: str, limit: int | None = None) -> list:
    """某書的歷史 session 摘要（slug 命中 OR ∈ touched），新→舊。"""
    rows = _read_index()
    out = [r for r in rows if r.get('slug') == slug or slug in (r.get('touched') or [])]
    out.reverse()
    return out[:limit] if limit else out


def sessions_grouped(limit: int | None = None) -> dict:
    """讀 index 一次，建 slug → session 摘要（新→舊，per-slug 封頂）。devctl 批次掛書用，
    避免每書各 _read_index() 一次（snapshot 對 N 本書會把整個 index 讀 N 遍）。
    corpus session（slug=None）依其 touched 歸入每本被改動的書。"""
    by: dict = {}
    for r in reversed(_read_index()):  # 新→舊
        keys = set(r.get('touched') or [])
        if r.get('slug') is not None:
            keys.add(r['slug'])
        for k in keys:
            lst = by.setdefault(k, [])
            if limit is None or len(lst) < limit:
                lst.append(r)
    return by


def corpus_sessions(limit: int | None = None) -> list:
    """非單書（slug=None）的 session 摘要：crawl_plan / math_sweep 等跨全書作業，新→舊。
    /dev『Corpus 作業』面板用（這些不掛在任何一本書下）。"""
    rows = [r for r in _read_index() if r.get('slug') is None]
    rows.reverse()
    return rows[:limit] if limit else rows


def load_session(sid: str) -> list:
    """某 session 的完整事件（CLI 用；網頁直接 fetch sessions/<id>.jsonl）。"""
    out = []
    try:
        with open(_sess_path(sid), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:
        pass
    return out


def reconcile() -> int:
    """自癒孤兒：JSONL 在、index 無對應 row（finish 沒跑成——daemon 被 SIGKILL / daemon thread 退出
    未走 finally）。index 是 JSONL 衍生快取 → 【還原】row 而非刪除（舊版直接 os.remove 等於銷毀
    可救的真實歷程）；provider/harness/model 由 _reconstruct_row 從 start() 落的 sidecar 讀回。pid 已死
    即還原；pid 仍活但 JSONL 逾 _ORPHAN_AGE_S 沒更新（pid 重用嫌疑）也還原；其餘（pid 活且新鮮＝可能
    仍在跑）留給其 live session 自行 finish。回還原筆數。低頻呼叫（devctl 心跳 60s）即可，純清潔不影響正確性。"""
    import time
    with _lock:
        if not os.path.isdir(SESS_DIR):
            return 0
        rows, ok = _load_index()
        if not ok:
            return 0  # index 暫讀不到 → 本輪不動，下次心跳再來（絕不把全部誤判成孤兒）
        known = {r.get('id') for r in rows}
        live = {s['id'] for s in _sessions.values()}
        now = time.time()
        recovered = []
        for fn in os.listdir(SESS_DIR):
            if not fn.endswith('.jsonl'):
                continue  # .meta.json sidecar 不是 session，跳過
            sid = fn[:-6]
            if sid in known or sid in live:
                continue
            p = os.path.join(SESS_DIR, fn)
            try:
                fresh = now - os.path.getmtime(p) <= _ORPHAN_AGE_S
            except OSError:
                continue
            pid = _pid_of(sid)
            if pid is not None and _pid_alive(pid) and fresh:
                continue  # pid 活 + JSONL 新 → 可能仍在跑，留給它自己 finish（免重複登記）
            row = _reconstruct_row(sid, p)
            if row is not None:
                recovered.append(row)
        if recovered:
            rows.extend(recovered)
            rows.sort(key=lambda r: r.get('started') or '')  # 還原 row 插回時間序（prune 砍頭靠此序）
            _prune_locked(rows)
            _write_index_locked(rows)
        return len(recovered)
