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
from datetime import datetime, timezone, timedelta

from book_pipeline import status as st
from book_pipeline import mineru_budget as bud

ROOT = st.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
REPORTS = os.path.join(BP, 'reports')
DAEMON_LOG = os.path.join(REPORTS, 'daemon.log')
STDOUT_LOG = os.path.join(REPORTS, 'daemon.stdout.log')
ERR_LOG = os.path.join(REPORTS, 'launchd.err.log')
PENDING_PATH = os.path.join(BP, '_pending_batches.json')
SNAPSHOT_PATH = os.path.join(ROOT, 'dev', 'status.json')
PLIST_LABEL = 'com.textbookreader.bookpipeline'
# daily 架構：tick 每天 08:30 台灣 = 00:30 UTC（與 plist StartCalendarInterval 一致）
TICK_UTC_HOUR, TICK_UTC_MIN = 0, 30

# daemon.log 時間戳為 UTC（datetime.now(timezone.utc)），格式 [YYYY-MM-DD HH:MM:SS]
TS_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
ERROR_RE = re.compile(r'未設定|Traceback|Exception|Error|ERROR|錯誤|failed|FAILED|❌|拒絕|denied', re.I)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)


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

    # 解析 daemon.log 取最後一次 tick start / end（UTC）
    last_start = last_end = None
    for line in _tail(DAEMON_LOG, 400):
        if 'tick start' in line:
            last_start = _parse_ts(line) or last_start
        elif 'tick end' in line:
            last_end = _parse_ts(line) or last_end

    now = _now_utc()
    last_dur = None
    if last_start and last_end and last_end >= last_start:
        last_dur = int((last_end - last_start).total_seconds())
    # daily：下次 tick = 下個 00:30 UTC（=08:30 台灣），固定時刻、不依賴上次 tick
    nxt = now.replace(hour=TICK_UTC_HOUR, minute=TICK_UTC_MIN, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    next_eta = int((nxt - now).total_seconds())

    tick_proc = _proc_info('pipeline_tick')
    llm_proc = _proc_info('claude -p')

    return {
        'installed': installed,
        'last_exit_code': last_exit,
        'tick_running': bool(tick_proc),
        'tick_proc': tick_proc,
        'last_tick_start_utc': last_start.isoformat() if last_start else None,
        'last_tick_end_utc': last_end.isoformat() if last_end else None,
        'last_tick_duration_s': last_dur,
        'next_tick_eta_s': next_eta,
        'in_tick_now': bool(tick_proc),
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
    accts = {}
    for a in bud.ACCOUNTS:
        used = bud.account_used(a)
        accts[a] = {'used': used, 'remaining': bud.account_remaining(a),
                    'daily_cap': bud.PRIORITY_PAGES}
    return {'date_utc': bud._today(), 'daily_cap': bud.PRIORITY_PAGES, 'accounts': accts}


# ── 進行中 ingest ────────────────────────────────────────────────────────────
def in_flight_ingest() -> list:
    if not os.path.exists(PENDING_PATH):
        return []
    try:
        return json.load(open(PENDING_PATH)) or []
    except Exception:
        return []


# ── 書本階段 ─────────────────────────────────────────────────────────────────
def books_status() -> dict:
    pending = st._load_pending()
    raw = st._raw_slug_map()
    slugs = st.all_slugs(pending, raw)
    rows = []
    todos = []
    for s in slugs:
        r = st.assess(s, pending, raw)
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
    if d['llm_job']:
        j = d['llm_job']
        print(f"   🤖 LLM 工人在跑：{j['slug']} (pid {j['pid']}, {j['elapsed']})")
    b = snap['budget']
    print(f"\n💳 MinerU 額度 ({b['date_utc']} UTC):")
    for a, v in b['accounts'].items():
        print(f"   {a}: 用 {v['used']}/{v['daily_cap']}，剩 {v['remaining']}")
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
    args = ap.parse_args(argv)

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
