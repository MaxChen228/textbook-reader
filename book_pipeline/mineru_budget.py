#!/usr/bin/env python3
"""book_pipeline.mineru_budget — MinerU ingest 的 async submit / harvest 排程（daemon 用）。

第一性原理（官方 limit 文檔 mineru.net/doc/docs/limit_en）：MinerU **每帳號每日無頁數
硬上限**——2000 頁/天是「最高優先級」配額，超過僅**降低解析優先級（排隊變慢），非拒絕**。
故 daemon 24h 運作下「慢沒關係」：所有書一有就全 async 提交、並行 OCR、之後統一收割，
而非序列同步等每本 OCR。

  - submit_ingest：切+傳+寫 manifest，**不 poll**（雲端排隊跑）→ 書進 in-flight
  - harvest_ingest：poll in-flight 的 batch → download+assemble→unified（收割就緒的）
  - pick_account：挑今日 used 最少帳號做負載均衡（**不闸门**，一律返回一個帳號全提交）
  - account_used 僅供負載均衡 + dashboard 顯示「今日已送頁數」（無 cap 概念，避免誤導）

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

# MinerU 無每日硬上限：>1000 頁僅降解析優先級（排隊變慢）、非拒絕（limit 文檔）。
# 故無 cap/remaining 概念——僅記今日 used 頁數供負載均衡與 dashboard 資訊顯示。
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


# 上傳中（uploading=True）entry 超過此秒數仍未翻 ready → 視為崩潰殘留，可重新提交（自癒）。
STALE_UPLOAD_SECS = int(os.environ.get('MINERU_STALE_UPLOAD_SECS', '7200'))
UPLOAD_LOG = os.path.join(BP, 'reports', 'uploads.log')


def _pending_entries() -> list:
    try:
        return json.load(open(PENDING_PATH)) or []
    except Exception:
        return []


def _age_secs(e: dict) -> float:
    """entry submitted_at 距今秒數；無時戳回 inf（保守視為老舊、不擋重提）。"""
    ts = e.get('submitted_at')
    if not ts:
        return float('inf')
    try:
        dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float('inf')


def in_flight() -> set:
    """manifest 內全部 slug（含上傳中與就緒）。dashboard / 一般「mineru 處理中」判斷用。"""
    return {e['slug'] for e in _pending_entries() if e.get('slug')}


def occupied() -> set:
    """正被 MinerU 佔用、不該重新提交的 slug：就緒（uploading falsy）或上傳中且未 stale。
    上傳中但超過 STALE_UPLOAD_SECS（崩潰殘留）→ 排除 → tick 會重新提交覆寫（自癒）。"""
    out = set()
    for e in _pending_entries():
        s = e.get('slug')
        if not s:
            continue
        if e.get('uploading'):
            if _age_secs(e) < STALE_UPLOAD_SECS:
                out.add(s)  # 上傳中且新鮮 → 佔用
            # else stale → 不佔用，可重提
        else:
            out.add(s)  # 就緒 → 佔用（等 harvest）
    return out


def harvestable() -> set:
    """可收割的 slug：已完成上傳（uploading falsy）。上傳中的書 OCR 尚不完整、跳過。"""
    return {e['slug'] for e in _pending_entries()
            if e.get('slug') and not e.get('uploading')}


def mark_submitting(slug: str, account: str, pages: int) -> None:
    """spawn detached upload 前先寫佔位 entry（batch_id=None, uploading=True），關閉
    『Popen 返回』到『子程序早寫 manifest』之間的空窗，防同窗內重複提交。子程序的
    pending_add 會以真 batch_id 覆寫本佔位。"""
    from book_pipeline import mineru_ingest as mi
    n = max(1, (pages + 179) // 180)
    mi.pending_add(slug, None, [[0, 0]] * n, 1, 'en', account=account, uploading=True)


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
    """async submit：**detached 背景**切片+上傳 MinerU（不等 PUT 完成、不 poll OCR）→
    立刻返回，tick 不被慢上傳堵住。對應 mineru_ingest --upload；子程序自寫 manifest
    （早寫 uploading=True，PUT 完翻 False），start_new_session 使其 outlive 本 tick。
    輸出導 uploads.log。回 0=已 spawn / 1=無 PDF。呼叫端須先 mark_submitting 佔位。"""
    pdf = _raw_pdf(slug)
    if not pdf:
        return 1
    cmd = ['uv', 'run', '--with', 'requests', '--with', 'pymupdf',
           'python', '-m', 'book_pipeline.mineru_ingest', pdf,
           '--slug', slug, '--account', _account_num(account), '--upload']
    os.makedirs(os.path.dirname(UPLOAD_LOG), exist_ok=True)
    logf = open(UPLOAD_LOG, 'a')
    logf.write(f'\n===== {datetime.now(timezone.utc).isoformat(timespec="seconds")} '
               f'submit {slug} acct={_account_num(account)} =====\n')
    logf.flush()
    subprocess.Popen(cmd, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return 0


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
    up = sorted(in_flight() - harvestable())
    return {'date': _today(),
            'accounts': {a: {'used': account_used(a)} for a in ACCOUNTS},
            'uploading': up, 'harvestable': sorted(harvestable()),
            'in_flight': sorted(in_flight())}


if __name__ == '__main__':
    print(json.dumps(status_report(), ensure_ascii=False, indent=2))
