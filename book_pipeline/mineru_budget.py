#!/usr/bin/env python3
"""book_pipeline.mineru_budget — MinerU ingest 的 async submit / harvest 排程（daemon 用）。

第一性原理（官方 limit 文檔 mineru.net/doc/docs/limit_en）：MinerU **每帳號每日無頁數
硬上限**——2000 頁/天是「最高優先級」配額，超過僅**降低解析優先級（排隊變慢），非拒絕**。
故 daemon 24h 運作下「慢沒關係」：所有書一有就全 async 提交、並行 OCR、之後統一收割，
而非序列同步等每本 OCR。

  - submit_ingest：切+傳+寫 manifest，**不 poll**（雲端排隊跑）→ 書進 in-flight
  - harvest_ingest：poll in-flight 的 batch → download+assemble→unified（收割就緒的）
  - pick_account：挑今日 used 最少帳號做負載均衡（**不闸门**，一律返回一個帳號全提交）
  - PRIORITY_PAGES 僅供 dashboard 顯示「今日高優先級餘量」，不作硬性阻擋

真相仍在引擎：in-flight（_pending_batches.json 有）每 tick 無條件 harvest，chunk 級
冪等 + failed 自動補傳會自癒。不修改 mineru_ingest.py；透過 subprocess 呼叫它的
--upload（submit）/ --resume（harvest）模式。
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

# MinerU 高優先級配額（頁/帳號/日）：超過僅降解析優先級排隊、非硬拒（limit 文檔）。
# 僅供 dashboard 顯示「今日高優先級餘量」進度條 + pick_account 負載均衡，**不作閘門**。
PRIORITY_PAGES = int(os.environ.get('MINERU_PRIORITY_PAGES', '2000'))
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
    """今日高優先級餘量（dashboard 用，非硬上限）。超過 0 不代表停，只代表降優先排隊。"""
    return max(0, PRIORITY_PAGES - account_used(account))


def pick_account(pages: int = 0) -> str:
    """挑今日 used 最少的帳號做負載均衡。MinerU 無每日硬上限（超高優先配額僅降速、
    不拒絕），故一律返回一個帳號全提交、不再因『預算不足』回 None。"""
    return min(ACCOUNTS, key=account_used)


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


def _pending_entries() -> list:
    try:
        return json.load(open(PENDING_PATH)) or []
    except Exception:
        return []


def in_flight() -> set:
    return {e['slug'] for e in _pending_entries() if e.get('slug')}


def _pending_entry(slug: str) -> dict | None:
    for e in _pending_entries():
        if e.get('slug') == slug:
            return e
    return None


def _raw_pdf(slug: str) -> str | None:
    from book_pipeline import status as st
    fn = st._raw_slug_map().get(slug)
    if not fn:
        return None
    pdf = os.path.join(ROOT, 'raw_pdfs', fn)
    return pdf if os.path.isfile(pdf) else None


def submit_ingest(slug: str, account: str) -> int:
    """async submit-only：切片+上傳 MinerU+寫 manifest，**不 poll OCR**（雲端排隊跑）。
    對應 mineru_ingest --upload。提交後該書進 in-flight，由 harvest_ingest 收割。
    回 rc（0=已提交）。"""
    pdf = _raw_pdf(slug)
    if not pdf:
        return 1
    cmd = ['uv', 'run', '--with', 'requests', '--with', 'pymupdf',
           'python', '-m', 'book_pipeline.mineru_ingest', pdf,
           '--slug', slug, '--account', _account_num(account), '--upload']
    return subprocess.run(cmd, cwd=ROOT).returncode


def harvest_ingest(slug: str, max_wait: int = 1800) -> int:
    """poll 收割 in-flight 書的 OCR batch → download+assemble→unified。對應 mineru_ingest
    --resume（從 manifest 撈該 slug 全部 batch，chunk 級冪等 + failed 自動補傳）。
    回 rc（0=完成組好 unified / 3=部分仍缺，下個 tick 再收 / 1=無 manifest）。"""
    entry = _pending_entry(slug)
    if not entry:
        return 1
    batch_id = entry.get('batch_id') or next(
        (b.get('batch_id') for b in (entry.get('batches') or []) if b.get('batch_id')), None)
    if not batch_id:
        return 1
    cmd = ['uv', 'run', '--with', 'requests', '--with', 'pymupdf',
           'python', '-m', 'book_pipeline.mineru_ingest',
           '--resume', batch_id, '--slug', slug, '--max-wait', str(max_wait)]
    return subprocess.run(cmd, cwd=ROOT).returncode


def status_report() -> dict:
    return {'date': _today(), 'priority_pages': PRIORITY_PAGES,
            'accounts': {a: {'used': account_used(a), 'remaining': account_remaining(a)}
                         for a in ACCOUNTS},
            'in_flight': sorted(in_flight())}


if __name__ == '__main__':
    print(json.dumps(status_report(), ensure_ascii=False, indent=2))
