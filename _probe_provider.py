#!/usr/bin/env python3
"""_probe_provider — 偵測「現在跑 agent 會落在三層 fallback 的哪一層」。

  uv run python _probe_provider.py            # 沿真實 chain 逐層探，落到第一個可用就停
  uv run python _probe_provider.py --all      # 不早停，把整條 chain 健康狀況全探出來
  uv run python _probe_provider.py --verb sol_extract --real-effort --timeout 180

三層 fallback（= book_pipeline.llm_policy 的 DEFAULT chain，env BOOK_PIPELINE_PROVIDER_CHAIN 可覆寫）：
  1. codex-pool —— ccNexus 池子（codex -p nexus + CCNEXUS_API_KEY 佔位；maxn970228 獨立額度）
  2. codex      —— 原生 OAuth（~/.codex/auth.json，ChatGPT 訂閱）
  3. claude     —— Claude Max 保底（claude -p）

判定忠於 dispatch_llm/_run_one 的真實 failover 規則：對每層**真發一個極短 prompt**，
  · rc==0 接上            → ✅ available，dispatch 會停在這層 → 這就是「現在會用的」
  · 撞額度(_hit_limit)     → 🔴 limit，dispatch 換下一層
  · 零事件 / 5xx / 連線錯  → 🟠 outage，dispatch 換下一層
  · 接上了但任務失敗       → ⚠ task-fail，dispatch 仍**停在這層**（reason=None 不 failover）
  · probe 逾時            → ⏱ 接不上 / 太慢，當「無法採用」續探下一層（標註：與真實任務 timeout 語意不同）

刻意只**複用 pipeline_tick 的純函式**（_build_llm_cmd / _llm_env / _hit_limit / _hit_outage /
_event_error_text / _resolve_dispatch），自己起 subprocess + 解析 JSONL，**絕不碰** worker_registry /
agent_history / leases / scope_guard → probe 不污染 daemon 的觀測面、租約、守衛指紋。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace

from book_pipeline import pipeline_tick as pt
from book_pipeline.llm_policy import KNOWN_PROVIDERS

# probe 用的極短 prompt：要 agent 秒回兩個字，落層判定不依賴回應內容、只看「接不接得上」。
PROBE_PROMPT = "Reply with exactly the two characters: OK  — do nothing else, call no tools."

ICON = {'available': '✅', 'limit': '🔴', 'outage': '🟠', 'task-fail': '⚠️', 'timeout': '⏱️'}
WHY = {
    'available': '接上、rc=0',
    'limit': '撞額度',
    'outage': '服務中斷（零事件 / 5xx / 連線錯）',
    'task-fail': '接上了但這次任務失敗',
    'timeout': 'probe 逾時，接不上或太慢',
}
# dispatch_llm 遇到這些狀態會「停在本層」（不 failover）→ 即「現在會用的 provider」。
LANDS_HERE = {'available', 'task-fail'}


def probe_one(provider: str, spec, prompt: str, timeout: int, verbose: bool) -> dict:
    """真發一次 probe，回 {status, rc, n_events, ms, err}。status ∈ ICON 的 key。
    複現 _run_one 的判定（額度 > 中斷 > 任務失敗），但零副作用（不註冊 worker/lease/hist）。"""
    cmd = pt._build_llm_cmd(provider, prompt, spec)
    env = pt._llm_env(provider)
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, cwd=pt.ROOT, env=env, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    err_parts: list[str] = []
    n_events = [0]

    def _pump():
        for line in p.stdout:  # type: ignore[union-attr]
            s = line.strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except Exception:
                err_parts.append(s)          # 非 JSON＝CLI/stderr 原生錯誤（認證/額度/crash）
                continue
            n_events[0] += 1                 # 成功 parse＝provider 確實接上並產出（zero=從未接上）
            if verbose:
                sys.stderr.write(f'    · {(ev.get("type") or "?")}\n')
            et = pt._event_error_text(provider, ev)
            if et:
                err_parts.append(et)

    th = threading.Thread(target=_pump, daemon=True)
    th.start()
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:                                 # 殺整個 process group（複製 _run_one 的逃生）
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            time.sleep(2)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        th.join(timeout=3)
        return {'status': 'timeout', 'rc': None, 'n_events': n_events[0],
                'ms': int((time.monotonic() - t0) * 1000), 'err': '(probe timeout)'}
    th.join(timeout=3)
    ms = int((time.monotonic() - t0) * 1000)
    err = '\n'.join(err_parts).lower()
    if rc == 0:
        status = 'available'
    elif pt._hit_limit(provider, err):
        status = 'limit'
    elif n_events[0] == 0 or pt._hit_outage(err):
        status = 'outage'
    else:
        status = 'task-fail'
    return {'status': status, 'rc': rc, 'n_events': n_events[0], 'ms': ms,
            'err': '\n'.join(err_parts)[-400:]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--verb', default='audit',
                    help='用哪個 stage 的派工 spec（model/effort/chain）；預設 audit。chain 不隨 verb 變，'
                         'effort/model 會（但不影響落層判定）')
    ap.add_argument('--all', action='store_true',
                    help='不早停：把整條 chain 每層健康狀況全探完（看全景）。預設探到第一個可用即停')
    ap.add_argument('--timeout', type=int, default=150, help='每層 probe 上限秒（預設 150）')
    ap.add_argument('--real-effort', action='store_true',
                    help='用 spec 的真實 codex effort（audit=high）。預設降為 low 以加速/省額度，落層判定不受影響')
    ap.add_argument('--skip', default='',
                    help='關掉某些層（逗號分隔），從 chain 移除後再探 → 看 fallback 改落哪。'
                         '可逆、不碰認證。例：--skip claude（關 Claude Code）/ --skip codex（關 codex OAuth）')
    ap.add_argument('--only', default='',
                    help='只探指定層（逗號分隔，等同關掉其餘全部），可驗證單層本身可用否。與 --skip 互斥')
    ap.add_argument('--verbose', action='store_true', help='逐事件印出（debug 接上情形）')
    args = ap.parse_args()

    spec = pt._resolve_dispatch(args.verb)
    if not args.real_effort and spec.codex_effort:
        spec = replace(spec, codex_effort='low')   # probe 加速：trivial prompt 不需要 high effort
    full_chain = list(spec.chain or ())
    closed: list[str] = []                          # 被關掉的層（僅本次 probe 視同不存在）
    if args.only:
        chain = [p.strip() for p in args.only.split(',') if p.strip()]
        closed = [p for p in full_chain if p not in chain]
    elif args.skip:
        drop = {p.strip() for p in args.skip.split(',') if p.strip()}
        chain = [p for p in full_chain if p not in drop]
        closed = [p for p in full_chain if p in drop]
    else:
        chain = full_chain

    print(f'■ probe 派工層偵測  verb={args.verb}  codex_model={spec.codex_model}  '
          f'effort={spec.codex_effort or "-"}  timeout={args.timeout}s')
    print(f'■ chain（真實 failover 順序）= {" → ".join(chain)}')
    if os.environ.get('BOOK_PIPELINE_PROVIDER_CHAIN'):
        print(f'  ↑ 來自 env BOOK_PIPELINE_PROVIDER_CHAIN 覆寫')
    if closed:
        print(f'  ✂ 已關掉（本次 probe 視同不存在）：{", ".join(closed)}')
    unknown = [c for c in chain if c not in KNOWN_PROVIDERS]
    if unknown:
        print(f'  ⚠ chain 含未知 provider {unknown}（合法：{KNOWN_PROVIDERS}）')
    print()

    landed = None
    results: list[tuple[str, dict]] = []
    for provider in chain:
        print(f'→ 探 {provider} …', flush=True)
        r = probe_one(provider, spec, PROBE_PROMPT, args.timeout, args.verbose)
        results.append((provider, r))
        ic = ICON[r['status']]
        detail = f"rc={r['rc']} events={r['n_events']} {r['ms']}ms"
        print(f'  {ic} {provider:11s} {WHY[r["status"]]:30s} [{detail}]')
        if r['status'] in ('limit', 'outage', 'task-fail') and r['err']:
            for ln in r['err'].splitlines()[-3:]:
                print(f'       │ {ln[:140]}')
        if landed is not None:
            print(f'  ➖ dispatch 在前面已停、reach 不到這層（僅 --all 健康檢查）')
        elif r['status'] in LANDS_HERE:
            landed = provider
            print(f'  ⏹ dispatch 會停在這層（不再 failover）→ 這就是現在會用的')
            if not args.all:
                break
        else:
            print(f'  ⏭ dispatch 會跳過此層、failover 到下一個')
        print()

    print('\n' + '═' * 64)
    if landed:
        st = next(r['status'] for p, r in results if p == landed)
        note = '' if st == 'available' else f'（狀態 {ICON[st]} {WHY[st]}：接上了故不 failover，但這次任務本身失敗）'
        print(f'結論：現在跑 agent 會用 →  **{landed}**  {note}')
        idx = chain.index(landed)
        if idx > 0:
            skipped = ', '.join(f'{p}({results[i][1]["status"]})' for i, p in enumerate(chain[:idx]))
            print(f'      （前面跳過：{skipped}）')
    else:
        print('結論：整條 chain 都接不上（全 limit/outage/timeout）→ dispatch 會 -2 defer，下個 cycle 重試')
        print('      逐層：' + ', '.join(f'{p}={r["status"]}' for p, r in results))
    print('═' * 64)
    return 0 if landed else 2


if __name__ == '__main__':
    sys.exit(main())
