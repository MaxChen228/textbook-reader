#!/usr/bin/env python3
"""book_pipeline.mineru_budget — MinerU 每日頁數預算的輕量排程（daemon 用）。

設計前提（見 AGENTS §2/§4）：MinerU 超每日配額（1000 頁/帳號）**不硬拒**，只降
優先 + 暫時性 `parsing failed`，mineru_ingest 的 chunk 級冪等 auto-resubmit 會自
癒。故本模組不做硬性閘門，只做「禮貌排程」：

  - 估每本書頁數（pymupdf）
  - 記錄每帳號每日「已開新書」頁數（best-effort，UTC 日重置）
  - 為新書挑剩餘預算最多的帳號；都滿則回 None（今天不開新書，但 in-flight 照 resume）

真相仍在引擎：in-flight（_pending_batches.json 有）的書每 tick 無條件 resume，
與預算無關。預算只決定「今天要不要再開一本新的」。

不修改 mineru_ingest.py；透過 subprocess 呼叫它。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BP = os.path.join(ROOT, 'book_pipeline')
BUDGET_PATH = os.path.join(BP, 'mineru_budget.json')
PENDING_PATH = os.path.join(BP, '_pending_batches.json')

DAILY_PAGES = int(os.environ.get('MINERU_DAILY_PAGES', '1000'))
ACCOUNTS = ['MINERU_API_TOKEN', 'MINERU_API_TOKEN2']  # 對應 --account 1 / 2


def _today() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _load() -> dict:
    try:
        return json.load(open(BUDGET_PATH)) or {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    json.dump(d, open(BUDGET_PATH, 'w'), ensure_ascii=False, indent=2)


def estimate_pages(slug: str) -> int | None:
    """raw PDF 頁數（pymupdf）。找不到回 None。"""
    from book_pipeline import status as st
    raw = st._raw_slug_map()
    fn = raw.get(slug)
    if not fn:
        return None
    path = os.path.join(ROOT, 'raw_pdfs', fn)
    if not os.path.isfile(path):
        return None
    try:
        import fitz
        d = fitz.open(path)
        n = d.page_count
        d.close()
        return n
    except Exception:
        return None


def account_used(account: str) -> int:
    return int(_load().get(_today(), {}).get(account, {}).get('pages', 0))


def account_remaining(account: str) -> int:
    return max(0, DAILY_PAGES - account_used(account))


def pick_account(pages: int) -> str | None:
    """挑剩餘預算 ≥ pages 且最多的帳號；都不足回 None。"""
    cand = sorted(ACCOUNTS, key=account_remaining, reverse=True)
    for a in cand:
        if account_remaining(a) >= pages:
            return a
    return None


def record_start(slug: str, account: str, pages: int) -> None:
    d = _load()
    day = d.setdefault(_today(), {})
    ent = day.setdefault(account, {'pages': 0, 'books': []})
    if slug not in ent['books']:
        ent['books'].append(slug)
        ent['pages'] += pages
    _save(d)


def _account_num(account: str) -> str:
    return '1' if account == 'MINERU_API_TOKEN' else account.replace('MINERU_API_TOKEN', '')


def in_flight() -> set:
    try:
        return {e['slug'] for e in (json.load(open(PENDING_PATH)) or [])}
    except Exception:
        return set()


def run_ingest(slug: str, account: str, max_wait: int = 600) -> int:
    """subprocess 呼叫 mineru_ingest（提交+輪詢一段時間）。回傳 rc（0完成/2,3未完）。
    in-flight 重跑同指令冪等續完；新書首次提交。"""
    from book_pipeline import status as st
    raw = st._raw_slug_map()
    fn = raw.get(slug)
    if not fn:
        return 1
    pdf = os.path.join(ROOT, 'raw_pdfs', fn)
    cmd = ['uv', 'run', '--with', 'requests', '--with', 'pymupdf',
           'python', '-m', 'book_pipeline.mineru_ingest', pdf,
           '--slug', slug, '--account', _account_num(account),
           '--max-wait', str(max_wait)]
    r = subprocess.run(cmd, cwd=ROOT)
    return r.returncode


def status_report() -> dict:
    return {'date': _today(),
            'accounts': {a: {'used': account_used(a), 'remaining': account_remaining(a)}
                         for a in ACCOUNTS},
            'in_flight': sorted(in_flight())}


if __name__ == '__main__':
    print(json.dumps(status_report(), ensure_ascii=False, indent=2))
