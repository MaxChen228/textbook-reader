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
  uv run python -m book_pipeline.devctl kick               # 硬殺重啟（棄在飛工作；緊急/卡死用）
  uv run python -m book_pipeline.devctl reload             # 優雅載入新碼：排空在飛 worker 後退出（零浪費）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

from book_pipeline import status as st
from book_pipeline import math_validate as mv
from book_pipeline import mineru_budget as bud
from book_pipeline import worker_registry as wr
from book_pipeline import agent_history as hist
from book_pipeline import book_timeline as tl
from book_pipeline import pipeline_queue as q
from book_pipeline import pipeline_run_state as prs
from book_pipeline import trace

ROOT = st.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
REPORTS = os.path.join(BP, 'reports')
DAEMON_LOG = os.path.join(REPORTS, 'daemon.log')
STDOUT_LOG = os.path.join(REPORTS, 'daemon.stdout.log')
ERR_LOG = os.path.join(REPORTS, 'launchd.err.log')
PENDING_PATH = os.path.join(BP, '_pending_batches.json')
SNAPSHOT_PATH = os.path.join(ROOT, 'dev', 'status.json')
DETAIL_DIR = os.path.join(ROOT, 'dev', 'detail')  # per-book {timeline,sessions}：抽屜 on-demand 撿，逐出 status.json 核（僅抽屜用、佔 books[] ~82%）
SYSTEM_PATH = os.path.join(ROOT, 'dev', 'system.json')  # 系統健康（errors/recent_log/corpus_sessions）：逐出核、系統面板 ~8s 慢輪詢
PROPOSALS_PATH = os.path.join(ROOT, 'dev', 'proposals.json')
PLIST_LABEL = 'com.textbookreader.bookpipeline'
# 反應式架構：daemon 走 launchd StartInterval（非固定時刻）。一個 controller 跑有界 observe→
# 派工→harvest→sleep 迴圈，排空或達牆鐘即退；launchd 每 TICK_INTERVAL_S 重拉（flock 序列化）。
# 「下次約」= 上次結束 + 此間隔（controller 正在跑時無意義 → 回 None，前端顯示「正在跑」）。
# 須與 plist StartInterval 一致（改一邊要改另一邊）。
TICK_INTERVAL_S = int(os.environ.get('BOOK_PIPELINE_TICK_INTERVAL', '900'))
# 每書 snapshot 帶幾場最近 session 摘要（完整史在 index.json / 逐事件在 sessions/*.jsonl）。
SESSIONS_PER_BOOK = 40

# daemon.log 時間戳為 UTC（datetime.now(timezone.utc)），格式 [YYYY-MM-DD HH:MM:SS]
TS_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
ERROR_RE = re.compile(r'未設定|Traceback|Exception|Error|ERROR|錯誤|failed|FAILED|❌|拒絕|denied', re.I)
# 成功摘要常含 `errors=0`/`0 failed`（convert_images 冪等轉檔摘要），其中 "errors"/"failed"
# 子字串會誤觸 ERROR_RE。這類計數行只要無強錯誤標記（Traceback/❌/FAILED…）就非真錯誤，
# 排除以免 false positive。真失敗會是 `N failed`（N>0），故 `\b0 failed\b` 是精確良性標記。
BENIGN_RE = re.compile(r'\berrors?=0\b|\b0 failed\b', re.I)
# FAILED 用 (?-i:) 局部關閉忽略大小寫：只抓大寫吼叫式標記，不吃成功摘要的小寫 `0 failed`。
STRONG_ERR_RE = re.compile(r'Traceback|Exception|❌|(?-i:FAILED)|未設定|拒絕|denied', re.I)


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


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ''


def code_status() -> dict:
    """跑中 controller 載入的 git 版本 vs 工作目錄 HEAD（答『daemon 跑哪版碼、離最新多遠、會不會自動更新』）。
    running=None → 無 live controller（閒置，下次 launchd respawn 直接載 HEAD）。behind>0 → 落後，
    下次優雅退出/respawn 自動跟上（毋須 kick）。消除上線後『是不是舊碼』的 forensics。"""
    from book_pipeline import pipeline_tick as pt
    info = pt.controller_info()
    running = (info or {}).get('sha')
    head = _git(['rev-parse', '--short', 'HEAD']) or '?'
    behind = None
    if running and running != '?' and head != '?':
        if running == head:
            behind = 0
        else:
            c = _git(['rev-list', '--count', f'{running}..HEAD'])
            behind = int(c) if c.isdigit() else None
    return {'running': running, 'head': head, 'behind': behind,
            'started': (info or {}).get('started')}


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
ZLIB_PROBE_TIMEOUT_S = 12  # live 探測（all_remaining 依序登入 N 帳號）的硬上限——逾時退回 stale 快取
_zlib_probe_lock = threading.Lock()  # single-flight：同時只跑一隻探測，絕不疊 N×login


def invalidate_zlib_cache() -> None:
    """事件式失效：爬書一輪剛花掉額度後呼叫，刪快取 → 下個 snapshot 立刻重抓 live 餘額，
    消除『剛爬完仍顯示舊額度（4/30 vs live 0/30）』的 5 分 staleness 窗。"""
    try:
        os.remove(ZLIB_CACHE)
    except OSError:
        pass  # 不存在或刪除失敗皆無關緊要（快取本就可重建）


def write_zlib_cache(accts: list) -> dict:
    """把一份**權威 live 帳號額度**寫進 dev/zlib_quota.json（gate 與 /dev 顯示共用同一快取）。
    買書員 drain 每次權威查 `limits` 後回寫 → 額度恢復即時反映在 gate＋dashboard（免等 300s TTL）；
    確認 0 時寫 fresh-0 也順手 throttle 下次 re-probe（見 pipeline_tick._zlib_remaining_cached）。"""
    total = sum(a['remaining'] for a in accts if a.get('remaining') is not None)
    total_limit = sum(a['limit'] for a in accts if a.get('limit') is not None)  # 每帳號 10/日 × N（不再硬編 30）
    snap = {'accounts': accts, 'total_remaining': total, 'total_limit': total_limit,
            'fetched_at': time.time(),
            'fetched_at_utc': _now_utc().isoformat(),  # 前端 relTime 用（naive local 會被當 UTC 致 +8h 偏移）
            'fetched_at_local': datetime.now().isoformat(timespec='seconds')}
    os.makedirs(os.path.dirname(ZLIB_CACHE), exist_ok=True)
    tmp = ZLIB_CACHE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, ZLIB_CACHE)
    return snap


def _zlib_probe_bounded() -> dict | None:
    """live 探測 zlib 額度並回寫快取，但**封頂在 ZLIB_PROBE_TIMEOUT_S**——`all_remaining` 依序
    登入 N 帳號（每個 login+profile 可達 90s），N 個慢帳號相加最壞達分鐘級；過去無上限直接 hang，
    凍住整個 build_snapshot → status.json 卡死數分鐘 → 已完成 agent session 看似消失（本 bug 根因）。

    single-flight（_zlib_probe_lock）：已有探測在跑就回 None，絕不疊 N×login。逾時則回 None（呼叫端
    退回 stale 快取）；探測 thread 為 daemon，長駐 controller 內會自行跑完回寫快取供下次 snapshot 撿，
    一次性 heartbeat 進程則隨進程退出——無論如何 snapshot 絕不阻塞超過 timeout。"""
    if not _zlib_probe_lock.acquire(blocking=False):
        return None  # 已有探測在飛 → 不疊，直接用 stale 快取
    box: dict = {}

    def _run():
        try:
            from book_pipeline import crawl_zlib as cz
            box['snap'] = write_zlib_cache(cz.all_remaining())
        except Exception:
            pass
        finally:
            _zlib_probe_lock.release()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(ZLIB_PROBE_TIMEOUT_S)
    return box.get('snap')  # 逾時則 thread 仍在背景跑（鎖未釋放，擋住疊探測），此處回 None


def zlib_status() -> dict:
    """各 zlib 帳號今日餘額。讀 dev/zlib_quota.json 快取；過期才**限時**打網路刷新。
    zlib 故障/慢 → 回最後快取（含 stale 標記），絕不讓 snapshot 失敗或卡死。"""
    cache = None
    if os.path.exists(ZLIB_CACHE):
        try:
            cache = json.load(open(ZLIB_CACHE))
        except Exception:
            cache = None
    fresh = cache and (time.time() - cache.get('fetched_at', 0)) < ZLIB_TTL_S
    if fresh:
        return {**cache, 'stale': False}
    snap = _zlib_probe_bounded()  # 限時 live 探測；逾時/single-flight 擋下 → None
    if snap is not None:
        return {**snap, 'stale': False}
    if cache:
        return {**cache, 'stale': True}  # 退回 stale 快取——snapshot 照常新鮮，只是額度欄稍舊
    return {'accounts': [], 'total_remaining': None, 'stale': True}


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
        if started and (now - started).total_seconds() > 21600:
            continue  # 起 >6h = pid-reuse 殘留兜底。agent 時間上限已取消（可長跑），故不再用 1h；6h 無真 agent 觸及，純孤兒防護（pid 存活已在上一行擋過大宗）
        out.append(w)
    return out


# ── 書本階段 ─────────────────────────────────────────────────────────────────
def _pretty_title(slug: str) -> str:
    """slug → 可讀書名（產線卡片用）。e.g. jackson_electrodynamics → Jackson Electrodynamics。"""
    return slug.replace('_', ' ').replace('-', ' ').title()


def _cover_url(slug: str, res_cover: str | None = None) -> str | None:
    """書封 URL（相對 dev/ 頁）。優先已部署 webp → OCR 階段 mineru cover.jpg → resolution sidecar 的
    z-lib 封面 URL（pre-ingest 書尚無本地封面，但 crawl 解析時 enrich_links 已存 z-lib CDN 封面，
    讓『待 OCR/待 ingest』卡也有真封面、不掉字首色塊佔位）；皆無回 None。nginx mount repo 根 → 本地兩路徑可直讀。

    附 ?v=<mtime> cache-buster：(1) 封面換版即換 URL，繞過 /img 的 immutable 長快取；(2) 繞過
    瀏覽器/CF 在 nginx 開放 cover.jpg 白名單『之前』對該路徑快取下來的 404（否則破圖殘留到 hard reload）。"""
    webp = os.path.join(ROOT, 'img', slug, 'cover.webp')
    if os.path.exists(webp):
        return f'../img/{slug}/cover.webp?v={int(os.path.getmtime(webp))}'
    jpg = os.path.join(ROOT, 'book_pipeline', 'mineru_data', slug, 'cover.jpg')
    if os.path.exists(jpg):
        return f'../book_pipeline/mineru_data/{slug}/cover.jpg?v={int(os.path.getmtime(jpg))}'
    return res_cover or None  # pre-ingest：退而求 z-lib CDN 封面（絕對 URL，瀏覽器直載）


def _write_detail(slug: str, timeline: list, sessions: list) -> None:
    """寫 per-book dev/detail/<slug>.json（抽屜 on-demand 撿）。內容未變則不重寫 → 保 mtime、條件式
    GET 續回 304、多數書每 60s 心跳零寫。只由時間軸唯一寫手（60s devsnapshot）呼叫，合單寫手不變量。"""
    payload = json.dumps({'slug': slug, 'timeline': timeline, 'sessions': sessions}, ensure_ascii=False)
    path = os.path.join(DETAIL_DIR, f'{slug}.json')
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == payload:
                return  # 內容相同 → 不動檔（mtime 不變、續回 304）
    except OSError:
        pass
    os.makedirs(DETAIL_DIR, exist_ok=True)
    tmp = path + f'.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(payload)
    os.replace(tmp, path)


def _prune_detail(keep: set) -> None:
    """清 dev/detail/ 內不再屬 SoT 的孤兒（書從 booklists 移除後殘留）。只 60s detail 寫手呼叫。"""
    try:
        for fn in os.listdir(DETAIL_DIR):
            if fn.endswith('.json') and fn[:-5] not in keep:
                try:
                    os.remove(os.path.join(DETAIL_DIR, fn))
                except OSError:
                    pass
    except OSError:
        pass  # DETAIL_DIR 尚未建立 → 無孤兒可清


def books_status(write_timeline: bool = False) -> dict:
    pending = st._load_pending()
    raw = st._raw_slug_map()
    slugs = st.all_slugs(pending, raw)
    state = q._load_state()
    math_by_book = mv.residual_by_book()  # 每書數學殘餘（reports 真相）→ 書本抽屜顯示
    sess_by_slug = hist.sessions_grouped(limit=SESSIONS_PER_BOOK) if write_timeline else {}  # 僅 detail 寫手(60s)需；讀 index 一次迴圈外分發
    from book_pipeline import booklists
    res_cov = booklists.load_resolution()  # 迴圈外讀一次：pre-ingest 書封面退而求 sidecar z-lib 封面
    rows = []
    todos = []
    for s in slugs:
        # 用 daemon 同款 assess_full（state-aware：認 qc/triage 拒、catalog accept、deploy gate）
        # → dashboard 與 daemon 永不分歧（根治「看板顯示與實際派工不一致」的矛盾源）。
        r = q.assess_full(s, pending, raw, state)
        r.setdefault('prob', 0)
        r.setdefault('sol', 0)
        r['sol_book'] = st._exists(f'{s}_sol', 'unified', 'content_list.json')
        r['title'] = _pretty_title(s)
        r['cover'] = _cover_url(s, (res_cov.get(s) or {}).get('cover'))
        deployed = os.path.exists(os.path.join(ROOT, 'data', s, 'book.json'))
        r['deployed'] = deployed  # 產線「上站完成」站定位用（book.json 已烤出）
        r['math_bad'] = math_by_book.get(s)  # 數學殘餘 occ（None=未驗/缺）→ 抽屜顯示，不 gate
        # 時間軸 + agent session 摘要逐出 status.json 核（佔 books[] ~82%、僅抽屜用）→ per-book
        # dev/detail/<slug>.json，抽屜 on-demand 撿。觀測式時間軸 = deployed-aware label（已部署→
        # 'deployed'，否則 stage）的 append-on-change 歷史，**只准單一寫手**（60s devsnapshot，永遠
        # 跑最新碼）寫：controller 事件式刷新用記憶體舊碼，若也 observe→版本歪斜輪流蓋寫、歷史無限
        # 亂跳（billingsley churn 根因）。故 controller(write_timeline=False) 只產輕量核、不碰 detail；
        # 唯 60s 心跳 observe + 寫 detail。既有書回填 deployed_at（唯一留存歷史時戳）。完整逐事件仍由
        # 網頁點開時 fetch sessions/<id>.jsonl，故 detail 只掛輕量摘要（封頂避免爆量）。
        if write_timeline:
            dep_at = (state.get(s) or {}).get('deployed_at')
            if deployed and dep_at:
                tl.seed(s, 'deployed', dep_at)
            tl.observe(s, 'deployed' if deployed else r.get('stage', ''))
            _write_detail(s, tl.get(s), sess_by_slug.get(s, []))
        rows.append(r)
        non_opt = [p for p in r['todo'].split() if p != '—' and not p.endswith('(可選)')]
        if non_opt:
            todos.append({'slug': s, 'todo': ' '.join(non_opt)})
    if write_timeline:  # detail 唯一寫手順手清孤兒：SoT 移除的書其 detail 檔不再覆寫 → 刪（防磁碟殘渣，P3 review nit）
        _prune_detail(set(slugs))
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
def crawl_status(books_snap: dict, zlib_snap: dict) -> dict:
    """爬書下載狀態（回答 /dev『下一輪會抓哪些具體書、何時抓』）。
    queue = **解析池前 N 本待下載書**（select_next 即時推導，無購物清單 buffer；額度0也照列）。
    backlog = pipeline 待消化深度（drain backpressure 用）。複用已算好的 books/zlib，不重打網路。"""
    from book_pipeline import pipeline_tick as pt
    from book_pipeline import booklists, pipeline_queue as q
    rows = books_snap['books']
    backlog = pt._crawl_backlog(rows)
    room = max(0, pt.CRAWL_INFLIGHT_CAP - backlog)  # pipeline 還能容納幾本在飛
    R = zlib_snap.get('total_remaining')
    blocked = q.crawl_blocked_slugs(pt.MAX_FETCH_FAILS)
    # 展示「下一輪會抓的書」：取 room（或預設一屏）本解析池 ready，排除失敗達上限者
    show = booklists.select_next(max(room, 20), exclude=blocked)
    pool = booklists.pool_counts()
    n_ready = pool['ready']                            # 解析池整體可下載書數（含未展示）
    cap = min(len(show), room)
    if isinstance(R, int):
        cap = min(cap, R)
    if n_ready == 0:
        state, reason = 'idle', '解析池無 ready · 待人工 /restock 補合格書'
    elif R == 0:
        state, reason = 'quota_empty', f'解析池 {n_ready} 本可下載 · 今日額度0 · 重置後自動抓'
    elif room <= 0:
        state, reason = 'holding', f'解析池 {n_ready} 本可下載 · pipeline 滿（backlog {backlog}）· 待消化'
    else:
        state, reason = 'draining', f'解析池 {n_ready} 本可下載 · 有額度 · 下輪抓 {cap} 本'
    res = booklists.load_resolution()                      # href/cover 存此（resolve commit 時 enrich；sidecar 短 hash 推不出公開 URL）
    qview = [{'slug': b['slug'], 'title': b.get('title', ''),
              'is_sol': b['slug'].endswith('_sol'),
              'url': res.get(b['slug'], {}).get('href', ''),
              'cover': res.get(b['slug'], {}).get('cover', ''),
              'fails': q.crawl_fail_count(b['slug'])} for b in show]
    # live 下載看板（買書員逐本 下載中→✓/✗，跨進程讀 dev/crawl_live.json）：正在抓時覆寫 state/reason，
    # 讓 status.json 自身也誠實反映「正在下載」（前端另有 2s 直撿 crawl_live.json 做即時卡牌）。
    live = pt.read_crawl_live()
    if live and live.get('active'):
        n_dl = sum(1 for b in live['books'] if b.get('state') == 'downloading')
        n_ok = sum(1 for b in live['books'] if b.get('state') == 'done')
        acct = '+'.join(str(a) for a in (live.get('accounts') or []))
        state = 'downloading'
        reason = f'⬇ 正在下載 {n_dl} 本' + (f' · ✓{n_ok} 已落地' if n_ok else '') + (f' · 帳號 {acct}' if acct else '')
    return {'queue': qview, 'count': n_ready, 'backlog': backlog, 'room': room,
            'high': pt.CRAWL_INFLIGHT_CAP, 'state': state, 'reason': reason, 'live': live}


def math_health() -> dict:
    """corpus-level 數學殘餘健康度（track-only）：總殘餘 / 未解(residual_unaccepted) / 已 accept、
    收斂判定（due|converged|fixpoint|no-node）、上次 sweep、殘餘最多的書。
    答 /dev『數學式壞了多少、是否還會被 sweep、哪幾本最髒、哪些已過 sweep』。"""
    from book_pipeline import pipeline_tick as pt
    state = q._load_state()
    sweep = q.math_sweep_state(state)
    touched = set(sweep.get('touched') or [])
    by_book = mv.residual_by_book()  # reports = ground truth（非 state，避免冷啟空窗顯示 0）
    books = [{'slug': s, 'bad_occ': n, 'in_last_sweep': s in touched}
             for s, n in by_book.items() if n]
    books.sort(key=lambda b: -b['bad_occ'])
    total = sum(by_book.values())
    accepted = q.math_accepted_total(state)
    due, reason = pt._sweep_decision(mv.node_available(), total, accepted,
                                     sweep or None, mv.macros_version())
    return {
        'node_available': mv.node_available(),
        'macros_version': mv.macros_version(),
        'corpus_bad_occ': total,
        'accepted_total': accepted,                 # 已 accept（不可渲染）的殘餘
        'residual_unaccepted': total - accepted,    # 收斂目標 → 0
        'due': due,                                 # 真相同 daemon（fixpoint/macros/node）
        'reason': reason,                           # due / converged / fixpoint / no-node
        'books_with_residual': len(books),
        'top_books': books[:20],
        'last_sweep': sweep or None,
        'running': q.math_batch_running(state),       # batch 正在打 API（純 API、無 agent）→ /dev 顯處理中
        'last_batch': q.math_last_batch(state),        # 上次 batch：解幾條/觸幾書/殘餘 before→after
        'recent_batches': _math_recent_batches(5),     # 末 5 批 LLM 逐批判決摘要（incident 可觀測，無原文省 size）
    }


def _math_recent_batches(n: int = 5) -> list:
    """末 n 批 LLM 處理摘要（讀 dev/math_history.jsonl，省去 raw 原文）：給 incident dump 看
    『模型逐批解了什麼/漏回/render 不過』。完整原文用 `math_sweep raw --tail N --json` 或 /dev 即時看。"""
    from book_pipeline import math_sweep as ms
    out = []
    for r in ms._read_history(n):
        c: dict = {}
        for v in r.get('verdicts', []):
            c[v.get('outcome')] = c.get(v.get('outcome'), 0) + 1
        out.append({'ts': r.get('ts'), 'pool': r.get('pool'), 'batch': r.get('batch'),
                    'state': r.get('state'), 'n': r.get('n'), 'outcomes': c})
    return out


def booklist_progress() -> dict:
    """書目收錄進度（/dev 收錄表分母，合格存在五態）：整體 + 各領域 owned/qualified/pending/candidate/
    rejected 統計（另含向後相容鍵 ready=qualified、unresolved=candidate、absent=rejected）。答『universe
    （editions）共幾本、收了幾 %、各領域進度、還有多少待查連結/待驗證/合格待下載/無法收錄』。"""
    from book_pipeline import booklists
    return booklists.progress()


def build_snapshot(since_min: int = 180, write_timeline: bool = False) -> dict:
    now = _now_utc()
    bs = books_status(write_timeline=write_timeline)
    zl = zlib_status()
    wks = workers()
    return {
        'generated_at_utc': now.isoformat(),
        'generated_at_local': datetime.now().isoformat(timespec='seconds'),
        'paused': prs.is_paused(),  # 系統暫停態（pause flag）→ /dev 暫停/啟動鈕 + daemon 是否派工
        'daemon': daemon_health(),
        'code': code_status(),
        'budget': budget_status(),
        'zlib': zl,
        'workers': wks,
        'in_flight_ingest': in_flight_ingest(),
        'errors': scan_errors(since_min),
        'recent_log': _tail(DAEMON_LOG, 40),
        'crawl': crawl_status(bs, zl),
        'booklists': booklist_progress(),
        'math': math_health(),
        'corpus_sessions': hist.corpus_sessions(limit=50),  # 非單書 agent 作業（math_sweep 等）
        **bs,
    }


def _atomic_write_json(path: str, obj) -> None:
    """原子寫 JSON（tmp+os.replace），避免網頁讀到半截。pid 後綴防多寫手 tmp 撞名。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f'.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_snapshot(write_timeline: bool = False) -> str:
    hist.reconcile()  # 順手清死孤兒 JSONL（finish 前被 SIGKILL 的殘檔）；低頻心跳即可
    # write_timeline 預設 False：controller 的事件式刷新（記憶體舊碼）只更新 live status.json、不碰歷史；
    # 只有 60s devsnapshot CLI 傳 True 寫時間軸 + per-book detail → 單一寫手、版本一致，杜絕歷史 churn。
    snap = build_snapshot(write_timeline=write_timeline)
    # 系統健康欄（errors/recent_log/corpus_sessions，非 live book 資料）逐出核 → system.json：
    # 讓核 status.json 純 live、可 1s 輪詢；system.json 前端 ~8s 慢輪詢。純讀、無單寫手不變量（不像
    # timeline append），故 controller 每事件寫亦安全 → 系統健康 ≤8s 新鮮，不必等 60s。
    system = {'generated_at_utc': snap.get('generated_at_utc'),
              'generated_at_local': snap.get('generated_at_local')}
    for k in ('errors', 'recent_log', 'corpus_sessions'):
        system[k] = snap.pop(k, None)
    _atomic_write_json(SNAPSHOT_PATH, snap)   # 核（已逐出 system 欄）
    _atomic_write_json(SYSTEM_PATH, system)
    try:
        write_proposals()  # 順手寫 proposals 側欄 feed（獨立檔；隨 8s 事件驅動 + 60s 心跳自動刷新）
    except Exception:
        pass  # fail-safe：proposals 出錯絕不擋 status.json 寫出
    return SNAPSHOT_PATH


# ── proposals 側欄 feed（/dev 即時看 agent 提出的提案）─────────────────────────
def proposals_feed(resolved_limit: int = 30) -> dict:
    """當前 proposals（book_pipeline/proposals.d/）→ /dev proposals 側欄資料源。
    proposed（待決議）帶完整散文欄位（evidence/proposal/risk/disposition/detect）供展開；
    已決議（accepted/rejected/superseded）僅摘要、近 resolved_limit 筆 → 避免長 diff 體積膨脹。
    proposed 排最前、各組內 created 倒序。純讀、無寫（裁決走 proposals resolve CLI）。"""
    from book_pipeline import proposals as pr

    def _clip(s: str, n: int = 6000) -> str:  # 防極端長 diff 撐爆 feed；完整見 CLI proposals show <id>
        s = s or ''
        return s if len(s) <= n else s[:n] + f'\n…（截斷 {len(s) - n} 字元，完整見 proposals show）'

    recs = pr.load_all()
    counts = {s: 0 for s in ('proposed', 'accepted', 'rejected', 'superseded')}
    by_domain: dict[str, int] = {}
    proposed, resolved = [], []
    for r in recs:
        stt = r.get('status') or 'proposed'
        counts[stt] = counts.get(stt, 0) + 1
        dom = r.get('domain') or '?'
        by_domain[dom] = by_domain.get(dom, 0) + 1
        item = {
            'id': r.get('id'), 'domain': dom, 'type': r.get('type'),
            'status': stt, 'title': r.get('title') or r.get('id'),
            'slug': r.get('slug') or None, 'source': r.get('source') or '',
            'created': r.get('created'), 'updated': r.get('updated'),
            'resolution': r.get('resolution') or '',
        }
        if stt == 'proposed':
            item.update({
                'evidence': _clip(r.get('evidence')), 'proposal': _clip(r.get('proposal')),
                'risk': _clip(r.get('risk')), 'disposition': _clip(r.get('disposition')),
                'detect': r.get('detect') or [],
            })
            proposed.append(item)
        else:
            resolved.append(item)
    proposed.sort(key=lambda x: x.get('created') or '', reverse=True)
    resolved.sort(key=lambda x: x.get('updated') or x.get('created') or '', reverse=True)
    return {
        'generated_at_utc': _now_utc().isoformat(),
        'total': len(recs),
        'counts': counts,
        'by_domain': by_domain,
        'items': proposed + resolved[:resolved_limit],
    }


def write_proposals() -> str:
    feed = proposals_feed()
    os.makedirs(os.path.dirname(PROPOSALS_PATH), exist_ok=True)
    tmp = PROPOSALS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROPOSALS_PATH)  # 原子寫，避免網頁讀到半截
    return PROPOSALS_PATH


# ── 人讀輸出 ─────────────────────────────────────────────────────────────────
def _print_human(snap: dict) -> None:
    d = snap['daemon']
    print(f"⏱  generated {snap['generated_at_local']} (local)")
    if snap.get('paused'):
        print("⏸  系統暫停中（pause flag）→ 不派任何新工；devctl resume 啟動")
    light = '🟢' if d['installed'] and d['last_exit_code'] in (0, None) else '🔴'
    print(f"{light} daemon installed={d['installed']} last_exit={d['last_exit_code']} "
          f"running_now={d['tick_running']}")
    if d['last_tick_start_utc']:
        dur = d['last_tick_duration_s']
        dur_s = '跑中' if dur is None else f'{dur}s'
        print(f"   last tick start {d['last_tick_start_utc']} "
              f"dur={dur_s}  next≈{d['next_tick_eta_s']}s")
    c = snap.get('code') or {}
    if c.get('running'):
        b = c.get('behind')
        tag = ('✅ 最新' if b == 0 else
               (f'⏳ 落後 HEAD {b} commit（下次 reload/respawn 自動跟上，毋須 kick）' if b
                else 'HEAD 未知'))
        print(f"   code={c['running']} · HEAD={c.get('head')} · {tag}")
    elif c.get('head'):
        print(f"   code=閒置（無 live controller）· HEAD={c.get('head')}（下次 respawn 直接載）")
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
    zcap = z.get('total_limit') or '?'
    print(f"\n📥 zlib 額度（總剩 {zt if zt is not None else '?'}/{zcap}"
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
    m = snap.get('math') or {}
    if m:
        node = '' if m.get('node_available') else '（node 缺，未驗證）'
        sweep = m.get('last_sweep') or {}
        sw = (f" · 上次 sweep {sweep.get('at','?')} 改 {len(sweep.get('touched') or [])} 書 "
              f"→殘{sweep.get('residual_after','?')}") if sweep else ' · 尚未 sweep'
        flag = '🔴' if m.get('due') else '🟢'
        acc = f"（已 accept {m.get('accepted_total')}）" if m.get('accepted_total') else ''
        print(f"\n{flag} 數學殘餘 {node}: corpus {m.get('corpus_bad_occ')} occ · "
              f"未解 {m.get('residual_unaccepted')}{acc} occ [{m.get('reason')}]"
              f" · {m.get('books_with_residual')} 書有殘{sw}")
        for b in (m.get('top_books') or [])[:8]:
            print(f"   {b['slug']}: {b['bad_occ']} occ{'  ✓已sweep' if b.get('in_last_sweep') else ''}")
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
    p_pr = sub.add_parser('proposals', help='當前 proposals feed（/dev 側欄資料源）')
    p_pr.add_argument('--json', action='store_true')
    p_er = sub.add_parser('errors', help='只看錯誤')
    p_er.add_argument('--since-min', type=int, default=180)
    p_er.add_argument('--json', action='store_true')
    sub.add_parser('incident', help='出事全貌 dump（給 Claude 除錯）')
    sub.add_parser('kick', help='硬殺重啟（棄在飛工作；緊急用）')
    sub.add_parser('reload', help='優雅載入新碼：排空在飛 worker 後退出（零浪費）')
    sub.add_parser('pause', help='暫停系統：停派一切新工（在飛 worker 自然收尾）；預設暫停')
    sub.add_parser('resume', help='啟動系統：恢復派工（解除暫停）')
    p_tl = sub.add_parser('timeline', help='某書（或全部）的階段時間軸')
    p_tl.add_argument('slug', nargs='?', help='省略 = 全部書')
    p_tl.add_argument('--json', action='store_true')
    p_hi = sub.add_parser('history', help='某書的歷史 agent session（audit/catalog_audit/math_sweep…）')
    p_hi.add_argument('slug', help='書 slug')
    p_hi.add_argument('--session', help='展開某 session id 的完整逐事件')
    p_hi.add_argument('--json', action='store_true')
    p_ma = sub.add_parser('math-accept',
                          help='[math sweep] 標記某書 N 條殘餘為「源文已毀、不可渲染」accept（§8，極少用）')
    p_ma.add_argument('--slug', required=True)
    p_ma.add_argument('--occ', type=int, required=True, help='accept 的殘餘 occ 數（夾至當前 bad_occ）')
    p_ma.add_argument('--reason', required=True, help='稽核理由（為何連 override 成可渲染都做不到）')
    args = ap.parse_args(argv)

    if args.cmd == 'math-accept':
        bad = (mv.read_report(args.slug) or {}).get('stats', {}).get('bad_occ')
        if bad is None:
            print(f'⚠ {args.slug} 無 math report（先 math_validate）', file=sys.stderr); return 1
        q.mark_math_accepted(args.slug, args.occ, args.reason)
        acc = q.math_accepted(args.slug)
        print(f'✓ {args.slug} math accept {acc}/{bad} occ（reason: {args.reason}）'
              f' → residual_unaccepted 扣除；真 0 政策下應極少，owner 可稽核 pipeline_state')
        return 0

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

    if args.cmd == 'history':
        if args.session:  # 完整對話回放 → 單一實作在 trace（devctl/trace 共用）
            return trace.render_session(args.session, as_json=args.json)
        sess = hist.sessions_for(args.slug)
        if args.json:
            print(json.dumps(sess, ensure_ascii=False, indent=2))
            return 0
        print(f"\n📖 {args.slug} — {len(sess)} 場 agent session（新→舊）")
        for r in sess:
            corp = ' (corpus)' if r.get('slug') is None else ''
            dur = r.get('duration_s')
            dur_s = '?' if dur is None else (f'{dur}s' if dur < 90 else f'{dur//60}m{dur%60:02d}s')
            mark = ('◷重建' if r.get('reconstructed')
                    else ('✓' if r.get('ok') else f"✗rc={r.get('rc')}"))
            at = (r.get('started') or '').replace('T', ' ').replace('+00:00', '')
            print(f"   {at} UTC  {r.get('verb')}{corp} · {r.get('model') or '—'}({r.get('harness') or '—'}) · "
                  f"{dur_s} · {r.get('total_calls', 0)}call · {mark}")
            print(f"        id={r.get('id')}")
        print("   → 階段⊕session 合併時間線：trace book " + args.slug)
        return 0

    if args.cmd == 'status':
        snap = build_snapshot()
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            _print_human(snap)
        return 0

    if args.cmd == 'snapshot':
        path = write_snapshot(write_timeline=True)  # 60s 心跳 plist = 時間軸唯一寫手（永遠最新碼）
        print(f'wrote {path}')
        return 0

    if args.cmd == 'proposals':
        feed = proposals_feed()
        if args.json:
            print(json.dumps(feed, ensure_ascii=False, indent=2))
            return 0
        c = feed['counts']
        print(f"提案 {feed['total']}：proposed {c['proposed']} · accepted {c['accepted']} · "
              f"rejected {c['rejected']} · superseded {c['superseded']}　by-domain {feed['by_domain']}")
        for it in feed['items']:
            mark = '🟡待決' if it['status'] == 'proposed' else f"·{it['status']}"
            print(f"   {mark}  {it['id']}  [{it['domain']}/{it['type']}] {(it['title'] or '')[:54]}")
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

    if args.cmd == 'reload':
        # 優雅上線新碼：丟 reload marker + SIGUSR1 喚醒 → controller 排空在飛 worker 後退出，
        # launchd 載入新碼。零浪費（對比 kick 硬殺棄工作）。無 live controller（閒置）→ 直接 kick 起新碼。
        from book_pipeline import pipeline_tick as pt
        pt.request_reload()
        if pt.wake_controller():
            cs = code_status()
            b = cs.get('behind')
            stale = f'（目前落後 {b} commit）' if b else ''
            print(f'🔄 已請求優雅 reload{stale}：controller 排空在飛 worker 後退出 → 立即自動拉起新碼（零浪費、零空檔）')
            print('   無在飛工作 → 秒級完成；有 audit 在飛 → 排空後才換（不棄工作）。devctl status 的 code 變 HEAD 即完成。')
            return 0
        uid = os.getuid()
        r = subprocess.run(['launchctl', 'kickstart', f'gui/{uid}/{PLIST_LABEL}'])
        print('🔄 無 live controller（閒置）→ 已 kick 起新碼' if r.returncode == 0
              else f'reload 請求已下；kick 失敗 rc={r.returncode}（下個 StartInterval 自動載新碼）')
        return r.returncode

    if args.cmd == 'kick':
        # 強制重啟（kickstart -k → SIGTERM）。controller 已 SIGTERM-safe：收訊號即快殺在飛 LLM 子工、
        # 排空收尾（finish 全跑、不留未收尾幽靈）再退，launchd 隨即重拉。日常上線新碼請優先用 `reload`
        # （等在飛 worker 自然跑完、零浪費）；`kick` 用於要立刻換碼/重啟、可接受中止在飛工作時。
        uid = os.getuid()
        r = subprocess.run(['launchctl', 'kickstart', '-k',
                            f'gui/{uid}/{PLIST_LABEL}'])
        print('kicked（已安全：在飛 worker 被快殺收尾、記錄保全）' if r.returncode == 0
              else f'failed rc={r.returncode}')
        return r.returncode

    if args.cmd in ('pause', 'resume'):
        # 寫運行/暫停旗標（.control/，與 sidecar/pipeline_tick 共用單一真相）；暫停＝停派新工、
        # 在飛 worker 自然收尾（同 reload 不殺）。送 SIGUSR1 喚醒 live controller → 立即 re-observe
        # 重判暫停閘（暫停即停下個 cycle 派工、resume 即恢復），秒級生效；無 live controller（閒置/
        # 停機）→ 旗標已落盤，下次 controller 起來自然遵守。
        from book_pipeline import pipeline_tick as pt
        prs.set_running(args.cmd == 'resume')
        woke = pt.wake_controller()
        if args.cmd == 'pause':
            print('⏸  系統已暫停：停派一切新工（在飛 worker 自然收尾）。'
                  + ('已喚醒 controller 立即生效。' if woke else '無 live controller → 下次起來即遵守。'))
        else:
            print('▶️  系統已啟動：恢復派工。'
                  + ('已喚醒 controller 立即恢復。' if woke else '無 live controller（閒置/停機）→ 下次重拉即派工。'))
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
