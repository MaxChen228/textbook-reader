#!/usr/bin/env python3
"""book_pipeline.trace — 回溯「一本/一批書到底發生了什麼」的統一入口（read-only forensic）。

定位（四分關注點，互不重疊）：
  status        當下階段 frontier 儀表板（現在每本卡哪）
  devctl        daemon 即時控制 / 健康（kick/reload/incident/snapshot）
  trace（本檔） 回溯：單書時間線⊕每階段 LLM session、批次溯源漏斗、session 全對話
  pipeline_queue work queue 機制 + first_seen 資料層

本檔**只組合既有資料 API**（book_timeline 階段轉移、agent_history session 歷程、status.assess
階段判定、pipeline_state 戳），不持有任何新真相、不寫盤 → 零技術債、可安全並行於 daemon。
devctl 的 timeline/history 子命令 delegate 到這裡（render_book/render_session），單一實作。

用法：
  uv run python -m book_pipeline.trace book <slug>        # 單書時間線 ⊕ 每階段 LLM session
  uv run python -m book_pipeline.trace session <id>       # 某 session 完整逐事件對話
  uv run python -m book_pipeline.trace cohort --since 12h  # 某時間段入庫 cohort 溯源漏斗
  uv run python -m book_pipeline.trace stuck              # 全時段需人工裁決的卡關書
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from book_pipeline import agent_history as hist
from book_pipeline import book_timeline as tl
from book_pipeline import status as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 共用小工具 ───────────────────────────────────────────────────────────────
def _parse_since(expr: str) -> datetime:
    """--since → aware UTC cutoff。支援 relative `12h`/`3d`/`90m`/`45min`、`today`、ISO（視為本地）。"""
    expr = expr.strip().lower()
    now = datetime.now(timezone.utc)
    if expr in ('today', '今天', '今日'):
        return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(min|m|h|d)', expr)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        delta = {'min': timedelta(minutes=n), 'm': timedelta(minutes=n),
                 'h': timedelta(hours=n), 'd': timedelta(days=n)}[unit]
        return now - delta
    try:
        d = datetime.fromisoformat(expr)
        return (d if d.tzinfo else d.astimezone()).astimezone(timezone.utc)
    except ValueError:
        raise SystemExit(f"無法解析 --since {expr!r}（用 12h / 3d / today / 2026-06-19[ HH:MM]）")


def _fmt_span(seconds: float) -> str:
    if seconds < 90:
        return f'{int(seconds)}s'
    if seconds < 5400:
        return f'{seconds/60:.0f}m'
    return f'{seconds/3600:.1f}h'


def _iso(s: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _hist_by_slug() -> dict:
    """讀 agent_history/index.json 一次，聚合每書 LLM 派工量：{slug:{sess,fail,secs}}。"""
    try:
        idx = json.load(open(os.path.join(ROOT, 'dev', 'agent_history', 'index.json'))) or []
    except Exception:
        return {}
    out: dict = {}
    for r in (idx if isinstance(idx, list) else []):
        s = r.get('slug')
        if not s:
            continue
        d = out.setdefault(s, {'sess': 0, 'fail': 0, 'secs': 0})
        d['sess'] += 1
        d['fail'] += 0 if r.get('ok') else 1
        d['secs'] += int(r.get('duration_s') or 0)
    return out


# ── render（devctl 也呼叫這兩個 → 單一實作）───────────────────────────────────
def render_book(slug: str) -> int:
    """單書完整時間線：階段轉移（book_timeline）⊕ 每場 LLM session（agent_history）合併時序。"""
    stages = tl.load_all().get(slug, [])
    sessions = hist.sessions_for(slug)  # 新→舊
    state = st._pstate().get(slug) or {}
    fs = state.get('first_seen_at')

    # 統一事件流：階段點 + session 點，按 UTC 時間排序
    events = []
    for e in stages:
        events.append((_iso(e.get('at')), 'stage', e))
    for s in sessions:
        events.append((_iso(s.get('started')), 'sess', s))
    events = [ev for ev in events if ev[0]]
    events.sort(key=lambda x: x[0])

    print(f"\n📖 {slug}")
    if fs:
        print(f"   入庫 first_seen: {fs.replace('T', ' ').replace('+00:00', '')} UTC")
    if not events:
        print("   （無時間線事件——未 ingest 或歷史未記）")
        return 0
    for at, kind, e in events:
        ts = at.strftime('%m-%d %H:%M:%S')
        if kind == 'stage':
            seed = ' (回填)' if e.get('seeded') else ''
            print(f"   {ts}  ● {e.get('stage', '')}{seed}")
        else:
            ok = '✓' if e.get('ok') else f"✗rc={e.get('rc')}"
            dur = _fmt_span(e.get('duration_s') or 0)
            prov = f"{e.get('provider', '?')}·{e.get('model', '?')}"
            print(f"   {ts}  ▶ {e.get('verb', '?'):14} {prov} · {dur} · {e.get('total_calls', 0)}call {ok}")
            print(f"   {'':17}    └ session id={e.get('id')}  → trace session <id> 看全對話")
    return 0


def render_session(sid: str, as_json: bool = False) -> int:
    """某 session 完整逐事件（tool 調用 + LLM 發言，原文不截）。"""
    evs = hist.load_session(sid)
    if as_json:
        print(json.dumps(evs, ensure_ascii=False, indent=2))
        return 0
    if not evs:
        print(f"⚠ session {sid} 無事件（id 錯或已被歷史上限淘汰）", file=sys.stderr)
        return 1
    print(f"\n🧵 session {sid} — {len(evs)} 事件")
    for e in evs:
        icon = '🔧' if e.get('kind') == 'tool' else '💬'
        at = (e.get('t') or '').replace('T', ' ').replace('+00:00', '')
        print(f"   {at}  {icon} {e.get('label', '')}")
    return 0


# ── cohort 溯源漏斗 ──────────────────────────────────────────────────────────
def _terminal(slug: str, r: dict, e: dict) -> tuple[str, str]:
    """每本書恰好歸一終態（零缺口）：deployed / stuck / inflight，回 (bucket, 原因)。"""
    if st._deployed(slug):
        return ('deployed', '已上站')
    sr = st._stuck_reason(slug, r, e)
    if sr:
        return ('stuck', f'{sr[0]}：{sr[1]}' if sr[1] else sr[0])
    return ('inflight', f"處理中（{r['stage']}）")


def cohort_report(since: str) -> int:
    pending, raw = st._load_pending(), st._raw_slug_map()
    cutoff = _parse_since(since)
    state, agg = st._pstate(), _hist_by_slug()
    dated, undated = [], []
    for s in st.all_slugs(pending, raw):
        ts = st._entry_ts(s, state)
        (dated if ts else undated).append((ts, s))
    cohort = sorted([(ts, s) for ts, s in dated if ts >= cutoff], reverse=True)

    cut_local = cutoff.astimezone().strftime('%Y-%m-%d %H:%M')
    print(f"=== cohort 溯源：入庫 ≥ {cut_local}（本地）· {len(cohort)} 本 ===")
    print(f"{'入庫':<12} {'slug':36} {'終態':8} {'入庫→上站':>9} {'LLM(場/敗/時)':>14}  原因/階段")
    buckets = {'deployed': 0, 'inflight': 0, 'stuck': 0}
    stuck_by_reason: dict = {}
    tot_sess = tot_fail = tot_secs = 0
    for ts, s in cohort:
        r = st.assess(s, pending, raw)
        e = state.get(s) or {}
        bucket, reason = _terminal(s, r, e)
        buckets[bucket] += 1
        if bucket == 'stuck':
            stuck_by_reason[reason.split('：')[0]] = stuck_by_reason.get(reason.split('：')[0], 0) + 1
        # 入庫→上站耗時
        dep = _iso(e.get('deployed_at'))
        span = _fmt_span((dep - ts).total_seconds()) if (dep and bucket == 'deployed') else '—'
        h = agg.get(s, {})
        tot_sess += h.get('sess', 0); tot_fail += h.get('fail', 0); tot_secs += h.get('secs', 0)
        llm = f"{h.get('sess',0)}/{h.get('fail',0)}/{_fmt_span(h.get('secs',0))}" if h else '—'
        icon = {'deployed': '✅', 'inflight': '⏳', 'stuck': '⚠'}[bucket]
        print(f"{ts.astimezone().strftime('%m-%d %H:%M'):<12} {s:36} {icon}{bucket:7} "
              f"{span:>9} {llm:>14}  {reason}")

    print(f"\n=== 漏斗（零缺口：✅+⏳+⚠ == 入庫 {len(cohort)}）===")
    print(f"  ✅ 已上站 {buckets['deployed']} · ⏳ 處理中 {buckets['inflight']} · ⚠ 卡關 {buckets['stuck']}")
    if stuck_by_reason:
        print("  卡關拆解：" + "，".join(f"{k}×{v}" for k, v in sorted(stuck_by_reason.items())))
    print(f"  LLM 總計：{tot_sess} 場 · {tot_fail} 失敗重試 · {_fmt_span(tot_secs)} 累計派工")
    if undated:  # backfill 後恆空；殘留即觀測缺口 → 硬提示
        print(f"\n⚠ {len(undated)} 本無 first_seen 時間戳（pipeline_queue --backfill-first-seen）："
              + ', '.join(s for _, s in undated[:8]))
    return 0


def stuck_report() -> int:
    pending, raw = st._load_pending(), st._raw_slug_map()
    state = st._pstate()
    rows = []
    for s in st.all_slugs(pending, raw):
        r = st.assess(s, pending, raw)
        sr = st._stuck_reason(s, r, state.get(s) or {})
        if sr:
            rows.append((st._entry_ts(s, state), s, sr[0], sr[1]))
    dated = sorted((r for r in rows if r[0]), key=lambda x: x[0], reverse=True)
    rows = dated + [r for r in rows if not r[0]]
    print(f"=== ⚠ 卡關清單：{len(rows)} 本需人工裁決 ===")
    print(f"{'入庫':<12} {'slug':36} {'原因':12} note")
    for ts, s, reason, note in rows:
        tl_ = ts.astimezone().strftime('%m-%d %H:%M') if ts else '—'
        print(f"{tl_:<12} {s:36} {reason:12} {note}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='book_pipeline.trace',
                                 description='回溯一本/一批書發生了什麼（read-only forensic）。')
    sub = ap.add_subparsers(dest='cmd', required=True)
    pb = sub.add_parser('book', help='單書時間線 ⊕ 每階段 LLM session')
    pb.add_argument('slug')
    psn = sub.add_parser('session', help='某 session 完整對話')
    psn.add_argument('id')
    psn.add_argument('--json', action='store_true')
    pc = sub.add_parser('cohort', help='某時間段入庫 cohort 溯源漏斗')
    pc.add_argument('--since', required=True, metavar='WHEN', help='12h / 3d / 90min / today / ISO')
    sub.add_parser('stuck', help='全時段需人工裁決的卡關書')
    args = ap.parse_args(argv)

    if args.cmd == 'book':
        return render_book(args.slug)
    if args.cmd == 'session':
        return render_session(args.id, as_json=args.json)
    if args.cmd == 'cohort':
        return cohort_report(args.since)
    if args.cmd == 'stuck':
        return stuck_report()
    return 1


if __name__ == '__main__':
    sys.exit(main())
