#!/usr/bin/env python3
"""book_pipeline.devctl — pipeline 開發監控的單一真相源（網頁 + 我的 CLI 共用同一份 JSON）。

設計：所有狀態（書本階段、daemon 健康、MinerU 額度、進行中 ingest、錯誤、log 尾段）
集中由 build_snapshot() 演算成一個 dict。
  - 網頁 books.wordnexus.lol/dev/ fetch `dev/status.json`（由 `devctl snapshot` 定時寫）。
  - 我（Claude）跑 `devctl status` / `devctl incident` 拿同一份 dict，除錯時與使用者螢幕同源。

純讀取、無 LLM、無對外計費動作 → 可被 launchd 每數分鐘安全重跑刷新 status.json。

用法：
  uv run python -m book_pipeline.devctl status [--json]   # 完整快照（人讀表 / --json 機讀）
  uv run python -m book_pipeline.devctl snapshot           # 寫 dev/status.json（launchd 刷新用）
  uv run python -m book_pipeline.devctl errors [--since-min 120]
  uv run python -m book_pipeline.devctl incident           # 出事時的全貌 dump（status+errors+log+進程）
  uv run python -m book_pipeline.devctl kick               # 立刻手動觸發一輪 tick
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

from book_pipeline import status as st
from book_pipeline import mineru_budget as bud
from book_pipeline import worker_registry as wr
from book_pipeline import book_timeline as tl
from book_pipeline import pipeline_queue as q

ROOT = st.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
REPORTS = os.path.join(BP, 'reports')
DAEMON_LOG = os.path.join(REPORTS, 'daemon.log')
STDOUT_LOG = os.path.join(REPORTS, 'daemon.stdout.log')
ERR_LOG = os.path.join(REPORTS, 'launchd.err.log')
PENDING_PATH = os.path.join(BP, '_pending_batches.json')
SNAPSHOT_PATH = os.path.join(ROOT, 'dev', 'status.json')
PLIST_LABEL = 'com.textbookreader.bookpipeline'
# 反應式架構：daemon 走 launchd StartInterval（非固定時刻）。一個 controller 跑有界 observe→
# 派工→harvest→sleep 迴圈，排空或達牆鐘即退；launchd 每 TICK_INTERVAL_S 重拉（flock 序列化）。
# 「下次約」= 上次結束 + 此間隔（controller 正在跑時無意義 → 回 None，前端顯示「正在跑」）。
# 須與 plist StartInterval 一致（改一邊要改另一邊）。
TICK_INTERVAL_S = int(os.environ.get('BOOK_PIPELINE_TICK_INTERVAL', '900'))

# daemon.log 時間戳為 UTC（datetime.now(timezone.utc)），格式 [YYYY-MM-DD HH:MM:SS]
TS_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
ERROR_RE = re.compile(r'未設定|Traceback|Exception|Error|ERROR|錯誤|failed|FAILED|❌|拒絕|denied', re.I)
# 成功摘要常含 `errors=0`/`unmatched=N`，其中 "errors" 子字串會誤觸 ERROR_RE。這類
# 計數行只要無強錯誤標記（Traceback/❌/FAILED…）就非真錯誤，排除以免 false positive。
BENIGN_RE = re.compile(r'\berrors?=0\b', re.I)
STRONG_ERR_RE = re.compile(r'Traceback|Exception|❌|FAILED|未設定|拒絕|denied', re.I)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)


def _parse_ts_iso(s: str | None) -> datetime | None:
    """ISO8601（worker_registry 寫的 started/updated）→ aware datetime；失敗回 None。"""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _tail(path: str, n: int) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8', errors='replace') as f:
        return [l.rstrip('\n') for l in f.readlines()[-n:]]


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ''


# ── daemon 健康 ──────────────────────────────────────────────────────────────
def daemon_health() -> dict:
    out = _sh(['launchctl', 'list'])
    installed = False
    last_exit = None
    running_pid = None
    for line in out.splitlines():
        if PLIST_LABEL in line:
            installed = True
            cols = line.split('\t')
            if len(cols) >= 2:
                running_pid = None if cols[0] in ('-', '') else int(cols[0])
                try:
                    last_exit = int(cols[1])
                except ValueError:
                    last_exit = None
            break

    # 解析 daemon.log 取最後一次 tick/迴圈 start / end（UTC）。兼容單次 tick 與反應式迴圈兩種標記。
    last_start = last_end = None
    for line in _tail(DAEMON_LOG, 400):
        if 'tick start' in line or 'reactive loop start' in line:
            last_start = _parse_ts(line) or last_start
        elif 'tick end' in line or 'reactive loop end' in line:
            last_end = _parse_ts(line) or last_end

    now = _now_utc()
    last_dur = None
    if last_start and last_end and last_end >= last_start:
        last_dur = int((last_end - last_start).total_seconds())
    # 「運轉中」由 log 標記推導（比 _proc_info 時序穩）：有 start 且尚無更新的 end → controller 在跑。
    running = bool(last_start and (last_end is None or last_start > last_end))
    tick_proc = _proc_info('pipeline_tick')
    # StartInterval 架構：下次 ≈ 上次結束 + 間隔；運轉中不可預測 → None（前端顯示「正在跑」）。
    if running:
        next_eta = None
    elif last_end:
        next_eta = max(0, int((last_end + timedelta(seconds=TICK_INTERVAL_S) - now).total_seconds()))
    else:
        next_eta = None

    llm_proc = _proc_info('claude -p')

    return {
        'installed': installed,
        'last_exit_code': last_exit,
        'tick_running': running,
        'tick_proc': tick_proc,
        'last_tick_start_utc': last_start.isoformat() if last_start else None,
        'last_tick_end_utc': last_end.isoformat() if last_end else None,
        'last_tick_duration_s': last_dur,
        'next_tick_eta_s': next_eta,
        'in_tick_now': running,
        'llm_job': llm_proc,  # 正在跑的 headless claude -p（None=無）
    }


def _proc_info(needle: str) -> dict | None:
    """pgrep + ps：回傳第一個匹配進程的 pid/elapsed/cmd（None=無）。"""
    pids = _sh(['pgrep', '-f', needle]).split()
    # 排除自己（devctl 本身命令列可能含 needle 字串）
    me = str(os.getpid())
    pids = [p for p in pids if p != me]
    for pid in pids:
        info = _sh(['ps', '-o', 'pid=,etime=,command=', '-p', pid]).strip()
        if not info:
            continue
        parts = info.split(None, 2)
        if len(parts) < 3:
            continue
        cmd = parts[2]
        if 'devctl' in cmd:  # 別把自己當成 tick/llm
            continue
        # 嘗試從 claude -p 命令抽出 slug
        slug = None
        ms = re.search(r'slug=([a-z0-9_]+)', cmd)
        if ms:
            slug = ms.group(1)
        return {'pid': int(parts[0]), 'elapsed': parts[1], 'slug': slug,
                'cmd': cmd[:160]}
    return None


# ── MinerU 額度 ──────────────────────────────────────────────────────────────
def budget_status() -> dict:
    # MinerU 無每日硬上限：>1000 頁僅降解析優先級（排隊變慢）、非拒絕。故只報「今日已送頁數」
    # 這個資訊量（負載均衡用），不畫 used/cap 進度條以免誤導成「額度將耗盡」。
    accts = {a: {'used': bud.account_used(a)} for a in bud.ACCOUNTS}
    return {'date_utc': bud._today(), 'accounts': accts}


# ── zlib 帳號額度（網路查詢，TTL 快取）────────────────────────────────────────
ZLIB_CACHE = os.path.join(ROOT, 'dev', 'zlib_quota.json')
ZLIB_TTL_S = 300  # snapshot 高頻重建；zlib 餘額至多每 5 分打一次網路（登入查 downloads_today）


def invalidate_zlib_cache() -> None:
    """事件式失效：爬書一輪剛花掉額度後呼叫，刪快取 → 下個 snapshot 立刻重抓 live 餘額，
    消除『剛爬完仍顯示舊額度（4/30 vs live 0/30）』的 5 分 staleness 窗。"""
    try:
        os.remove(ZLIB_CACHE)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def zlib_status() -> dict:
    """各 zlib 帳號今日餘額。讀 dev/zlib_quota.json 快取；過期才打網路刷新。
    zlib 故障 → 回最後快取（含 stale 標記），絕不讓 snapshot 失敗。"""
    cache = None
    if os.path.exists(ZLIB_CACHE):
        try:
            cache = json.load(open(ZLIB_CACHE))
        except Exception:
            cache = None
    fresh = cache and (time.time() - cache.get('fetched_at', 0)) < ZLIB_TTL_S
    if fresh:
        return {**cache, 'stale': False}
    try:
        from book_pipeline import crawl_zlib as cz
        accts = cz.all_remaining()
        total = sum(a['remaining'] for a in accts if a.get('remaining') is not None)
        snap = {'accounts': accts, 'total_remaining': total,
                'fetched_at': time.time(),
                'fetched_at_local': datetime.now().isoformat(timespec='seconds')}
        os.makedirs(os.path.dirname(ZLIB_CACHE), exist_ok=True)
        tmp = ZLIB_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, ZLIB_CACHE)
        return {**snap, 'stale': False}
    except Exception as e:
        if cache:
            return {**cache, 'stale': True, 'error': str(e)}
        return {'accounts': [], 'total_remaining': None, 'stale': True, 'error': str(e)}


# ── 進行中 ingest ────────────────────────────────────────────────────────────
def in_flight_ingest() -> list:
    if not os.path.exists(PENDING_PATH):
        return []
    try:
        return json.load(open(PENDING_PATH)) or []
    except Exception:
        return []


# ── 進行中 LLM 工人（即時工具調用）─────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def workers() -> list:
    """進行中 LLM worker（dev/workers.json）。過濾 stale：pid 已死或起 > LLM_TIMEOUT。
    每 worker 含 slug/verb/provider/total_calls + 最近 5 條工具調用/發言（recent）。"""
    data = wr.load()
    out = []
    now = _now_utc()
    for w in data.get('workers', []):
        pid = w.get('pid')
        if isinstance(pid, int) and not _pid_alive(pid):
            continue
        started = _parse_ts_iso(w.get('started'))
        if started and (now - started).total_seconds() > 3600:
            continue  # 超 1h 必為殘留（LLM_TIMEOUT 40min）
        out.append(w)
    return out


# ── 書本階段 ─────────────────────────────────────────────────────────────────
def _pretty_title(slug: str) -> str:
    """slug → 可讀書名（產線卡片用）。e.g. jackson_electrodynamics → Jackson Electrodynamics。"""
    return slug.replace('_', ' ').replace('-', ' ').title()


def _cover_url(slug: str) -> str | None:
    """書封 URL（相對 dev/ 頁）。優先已部署 webp，退而求 OCR 階段的 mineru cover.jpg；皆無回 None
    （前端以標題首字產生佔位卡）。nginx mount repo 根 → 兩路徑皆可直讀。

    附 ?v=<mtime> cache-buster：(1) 封面換版即換 URL，繞過 /img 的 immutable 長快取；(2) 繞過
    瀏覽器/CF 在 nginx 開放 cover.jpg 白名單『之前』對該路徑快取下來的 404（否則破圖殘留到 hard reload）。"""
    webp = os.path.join(ROOT, 'img', slug, 'cover.webp')
    if os.path.exists(webp):
        return f'../img/{slug}/cover.webp?v={int(os.path.getmtime(webp))}'
    jpg = os.path.join(ROOT, 'book_pipeline', 'mineru_data', slug, 'cover.jpg')
    if os.path.exists(jpg):
        return f'../book_pipeline/mineru_data/{slug}/cover.jpg?v={int(os.path.getmtime(jpg))}'
    return None


def books_status() -> dict:
    pending = st._load_pending()
    raw = st._raw_slug_map()
    slugs = st.all_slugs(pending, raw)
    state = q._load_state()
    rows = []
    todos = []
    for s in slugs:
        r = st.assess(s, pending, raw)
        r['title'] = _pretty_title(s)
        r['cover'] = _cover_url(s)
        # 觀測式時間軸：deployed-aware label（已部署 → 'deployed'，否則用 stage）→
        # observe 冪等 append-on-change，建出每書階段轉換史。既有書回填 deployed_at（唯一
        # 留存的歷史時戳），否則它們只會從此刻起顯示 deployed、丟失過去。
        deployed = os.path.exists(os.path.join(ROOT, 'data', s, 'book.json'))
        dep_at = (state.get(s) or {}).get('deployed_at')
        if deployed and dep_at:
            tl.seed(s, 'deployed', dep_at)
        label = 'deployed' if deployed else r.get('stage', '')
        tl.observe(s, label)
        r['timeline'] = tl.get(s)
        r['deployed'] = deployed  # 產線「上站完成」站定位用（book.json 已烤出）
        rows.append(r)
        non_opt = [p for p in r['todo'].split() if p not in ('—', 'translate(可選)')]
        if non_opt:
            todos.append({'slug': s, 'todo': ' '.join(non_opt)})
    return {'books': rows, 'actionable': todos, 'total': len(rows)}


# ── 錯誤掃描 ─────────────────────────────────────────────────────────────────
def _last_tick_start() -> datetime | None:
    """daemon.log 最後一次 '=== tick start ===' 的 UTC 時戳。"""
    last = None
    for line in _tail(DAEMON_LOG, 400):
        if 'tick start' in line:
            last = _parse_ts(line) or last
    return last


def scan_errors(since_min: int = 180) -> list:
    # 錯誤窗以「最近一次 tick start」為地板：只反映最新一輪 tick 的錯誤，
    # 已解決的舊錯誤在下一輪 clean tick 後自動消失（stdout.log 多數行無日期戳，
    # 否則永遠掃得到、誤判已修復的問題仍存在）。
    cutoff = _now_utc() - timedelta(minutes=since_min)
    tick_floor = _last_tick_start()
    if tick_floor and tick_floor > cutoff:
        cutoff = tick_floor
    out = []
    for path, src in ((DAEMON_LOG, 'daemon.log'), (STDOUT_LOG, 'stdout'),
                      (ERR_LOG, 'launchd.err')):
        lines = _tail(path, 300)
        tss = [_parse_ts(l) for l in lines]
        # 每行的「有效 ts」：取至該行為止最後見到的時戳；檔頭無時戳的行
        # 向後找第一個時戳近似（否則早期錯誤永遠 eff=None 被一律收，誤判長存）。
        eff_ts = [None] * len(lines)
        carry = None
        for i, ts in enumerate(tss):
            if ts:
                carry = ts
            eff_ts[i] = carry
        fwd = None
        for i in range(len(lines) - 1, -1, -1):
            if tss[i]:
                fwd = tss[i]
            if eff_ts[i] is None:
                eff_ts[i] = fwd
        for line, eff in zip(lines, eff_ts):
            if ERROR_RE.search(line):
                # LLM 工人工具調用/發言回顯（`[slug] 🔧 …` / `💬 …`）是活動紀錄非錯誤；
                # 其 Bash 命令字串常含 error/grep error 等誤觸 ERROR_RE。一律略過。
                if '🔧' in line or '💬' in line:
                    continue
                if BENIGN_RE.search(line) and not STRONG_ERR_RE.search(line):
                    continue  # errors=0 成功摘要，非真錯誤
                if eff and eff < cutoff:
                    continue
                out.append({'src': src, 'ts_utc': eff.isoformat() if eff else None,
                            'line': line.strip()[:300]})
    return out[-40:]


# ── 快照組裝 ─────────────────────────────────────────────────────────────────
def build_snapshot(since_min: int = 180) -> dict:
    now = _now_utc()
    return {
        'generated_at_utc': now.isoformat(),
        'generated_at_local': datetime.now().isoformat(timespec='seconds'),
        'daemon': daemon_health(),
        'budget': budget_status(),
        'zlib': zlib_status(),
        'workers': workers(),
        'in_flight_ingest': in_flight_ingest(),
        'errors': scan_errors(since_min),
        'recent_log': _tail(DAEMON_LOG, 40),
        **books_status(),
    }


def write_snapshot() -> str:
    snap = build_snapshot()
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    tmp = SNAPSHOT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SNAPSHOT_PATH)  # 原子寫，避免網頁讀到半截
    return SNAPSHOT_PATH


# ── 人讀輸出 ─────────────────────────────────────────────────────────────────
def _print_human(snap: dict) -> None:
    d = snap['daemon']
    print(f"⏱  generated {snap['generated_at_local']} (local)")
    light = '🟢' if d['installed'] and d['last_exit_code'] in (0, None) else '🔴'
    print(f"{light} daemon installed={d['installed']} last_exit={d['last_exit_code']} "
          f"running_now={d['tick_running']}")
    if d['last_tick_start_utc']:
        dur = d['last_tick_duration_s']
        dur_s = '跑中' if dur is None else f'{dur}s'
        print(f"   last tick start {d['last_tick_start_utc']} "
              f"dur={dur_s}  next≈{d['next_tick_eta_s']}s")
    wk = snap.get('workers') or []
    if wk:
        print(f"\n🤖 進行中 LLM 工人 ({len(wk)})：")
        for w in wk:
            print(f"   · {w.get('slug') or w.get('verb')} [{w.get('verb')}] "
                  f"pid {w.get('pid')} · {w.get('provider')} · 共 {w.get('total_calls', 0)} 次調用")
            for r in (w.get('recent') or [])[-5:]:
                icon = '🔧' if r.get('kind') == 'tool' else '💬'
                print(f"        {icon} {(r.get('label') or '')[:100]}")
    b = snap['budget']
    print(f"\n💳 MinerU 今日送頁（{b['date_utc']} UTC · 無上限，>1000 僅降優先）:")
    for a, v in b['accounts'].items():
        print(f"   {a}: 已送 {v['used']} 頁")
    z = snap.get('zlib') or {}
    zt = z.get('total_remaining')
    print(f"\n📥 zlib 額度（總剩 {zt if zt is not None else '?'}/30"
          f"{' · STALE' if z.get('stale') else ''}）:")
    for a in (z.get('accounts') or []):
        rem = a.get('remaining')
        print(f"   {a.get('email')}: 剩 {rem if rem is not None else '查無'}"
              f"/{a.get('limit') if a.get('limit') is not None else '?'}")
    if snap['in_flight_ingest']:
        print(f"\n📤 進行中 ingest: {len(snap['in_flight_ingest'])} 批")
    errs = snap['errors']
    print(f"\n{'🔴' if errs else '🟢'} 錯誤 ({len(errs)}):")
    for e in errs[-10:]:
        print(f"   [{e['src']}] {e['line']}")
    print(f"\n📚 書本 ({snap['total']}) — 待辦 {len(snap['actionable'])}:")
    for t in snap['actionable']:
        print(f"   {t['slug']}: {t['todo']}")


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='devctl')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_st = sub.add_parser('status', help='完整快照')
    p_st.add_argument('--json', action='store_true')
    sub.add_parser('snapshot', help='寫 dev/status.json')
    p_er = sub.add_parser('errors', help='只看錯誤')
    p_er.add_argument('--since-min', type=int, default=180)
    p_er.add_argument('--json', action='store_true')
    sub.add_parser('incident', help='出事全貌 dump（給 Claude 除錯）')
    sub.add_parser('kick', help='立刻觸發一輪 tick')
    p_tl = sub.add_parser('timeline', help='某書（或全部）的階段時間軸')
    p_tl.add_argument('slug', nargs='?', help='省略 = 全部書')
    p_tl.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    if args.cmd == 'timeline':
        write_snapshot()  # 先觀測一次，確保時間軸含當下階段
        allt = tl.load_all()
        if args.json:
            print(json.dumps(allt if not args.slug else allt.get(args.slug, []),
                             ensure_ascii=False, indent=2))
            return 0
        slugs = [args.slug] if args.slug else sorted(allt)
        for s in slugs:
            evs = allt.get(s, [])
            print(f"\n📖 {s}")
            for j, e in enumerate(evs):
                at = (e['at'] or '').replace('T', ' ').replace('+00:00', '')
                span = ''
                if j + 1 < len(evs):
                    dt = (_parse_ts_iso(evs[j + 1]['at']) - _parse_ts_iso(e['at'])).total_seconds()
                    span = f"  ▸ {int(dt)}s" if dt < 90 else (
                        f"  ▸ {dt/60:.0f}m" if dt < 5400 else f"  ▸ {dt/3600:.1f}h")
                seed = ' (回填)' if e.get('seeded') else ''
                print(f"   {at} UTC  {e['stage']}{seed}{span}")
        return 0

    if args.cmd == 'status':
        snap = build_snapshot()
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            _print_human(snap)
        return 0

    if args.cmd == 'snapshot':
        path = write_snapshot()
        print(f'wrote {path}')
        return 0

    if args.cmd == 'errors':
        errs = scan_errors(args.since_min)
        if args.json:
            print(json.dumps(errs, ensure_ascii=False, indent=2))
        else:
            for e in errs:
                print(f"[{e['src']}] {e.get('ts_utc') or '?'} {e['line']}")
        return 0

    if args.cmd == 'incident':
        # 給 Claude 的全貌：結構化快照 + 較長 log + 進程細節，一次到位
        snap = build_snapshot(since_min=720)
        bundle = {
            'snapshot': snap,
            'daemon_log_tail': _tail(DAEMON_LOG, 80),
            'stdout_tail': _tail(STDOUT_LOG, 60),
            'launchd_err_tail': _tail(ERR_LOG, 40),
            'launchctl': _sh(['launchctl', 'list']).splitlines()[:1]
            + [l for l in _sh(['launchctl', 'list']).splitlines() if PLIST_LABEL in l],
        }
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == 'kick':
        uid = os.getuid()
        r = subprocess.run(['launchctl', 'kickstart', '-k',
                            f'gui/{uid}/{PLIST_LABEL}'])
        print('kicked' if r.returncode == 0 else f'failed rc={r.returncode}')
        return r.returncode

    return 1


if __name__ == '__main__':
    sys.exit(main())
