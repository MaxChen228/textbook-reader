"""MinerU 雲端 API ingest pipeline。

兩個角色，可分可合：

  [Submitter] PDF 來源機（你的筆電）跑
    slice → PUT MinerU → 寫 batch_id 到 _pending_batches.json → 退出（10s-2min）
    指令：python -m book_pipeline.mineru_ingest --upload PDF [--slug N]

  [Receiver] 任何地方都能跑（本機 / 雲端 agent）
    讀 manifest → poll → 下載 → assemble unified/ → 從 manifest 移除
    指令：python -m book_pipeline.mineru_ingest --resume BATCH --slug N

  橋樑：book_pipeline/_pending_batches.json（commit 進 git）

  All-in-one：本機網路穩、有耐心：直接 `python -m book_pipeline.mineru_ingest PDF`

PDF 永遠不需要進 git（gitignore raw_pdfs/）。它只在「submitter 上傳給 MinerU 的瞬間」
有用，之後系統的狀態完全由 manifest + unified/ 表達。

raw PDF → 機械切 ≤180 頁（1 頁 overlap）→ batch 上傳 → 輪詢 → 下載解壓 → 組裝
unified content_list（global page_idx、images/ 合併）。

不依賴任何 book.yaml 或預先知識。輸入只有 PDF。

產物（每本書一個目錄）：
  book_pipeline/mineru_data/<slug>/
    chunks/p0001-0180.pdf, p0180-0359.pdf, ...      # 切好的 PDF
    raw/chunk_<n>/                                  # MinerU zip 解壓（原樣）
      *_content_list.json, images/, full.md, layout.json, *_origin.pdf, ...
    unified/
      content_list.json     # 組裝後扁平 block list（page_idx 已轉 global 0-based）
      chunks.json           # 切點與每 chunk 元資料
      images/               # 所有 chunk 的圖合併
      full.md               # 各 chunk markdown 串接（除錯用）
    _run.json               # batch_id、timings

用法：
  uv run --with requests --with pymupdf python -m book_pipeline.mineru_ingest \\
      raw_pdfs/sakurai_mqm3.pdf

  選項：
    --slug NAME        強制指定 slug（預設取 PDF 檔名）
    --chunk-size N     每 chunk 頁數上限，預設 180（≤200）
    --overlap N        每 chunk 重疊頁數，預設 1
    --language L       MinerU language，預設 en（教科書）
    --skip-upload      已有 raw/，只重做組裝
    --resume BATCH_ID  撿已上傳的 batch（poll → download → assemble），不需要本機 PDF
    --account 1|2|N    指定 MinerU 帳號（對應 .env 的 MINERU_API_TOKEN[N]）

  Resume 模式：
    uv run --with requests --with pymupdf python -m book_pipeline.mineru_ingest \\
        --resume <batch_id> --slug <slug>
    從 poll 回應的 file_name 推 ranges（p{SSSS}-{EEEE}.pdf），不需 PDF。
    Resume 時不需傳 --account（從 manifest 自動讀，舊 entry 視為 1）。

  Multi-account：兩個帳號的 token 分別放 .env 的 MINERU_API_TOKEN / MINERU_API_TOKEN2。
  Submit 時用 --account 指定，manifest 會記錄該 batch 屬於哪個帳號；receiver 自動切。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

import fitz
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'book_pipeline' / 'mineru_data'
BASE = 'https://mineru.net/api/v4'


# ── token ────────────────────────────────────────────────────────────────────

DEFAULT_ACCOUNT_ENV = 'MINERU_API_TOKEN'


def normalize_account(value: str | None) -> str:
    """正規化 --account 參數為 env var name。
    - None / '' / '1' → 'MINERU_API_TOKEN'
    - 純數字 N (N≥2)  → 'MINERU_API_TOKEN<N>'
    - 已是 'MINERU_API_TOKEN[*]' 形式 → 原樣
    其他輸入 → ValueError"""
    if not value:
        return DEFAULT_ACCOUNT_ENV
    v = value.strip()
    if v.isdigit():
        n = int(v)
        return DEFAULT_ACCOUNT_ENV if n <= 1 else f'{DEFAULT_ACCOUNT_ENV}{n}'
    if not v.startswith(DEFAULT_ACCOUNT_ENV):
        raise ValueError(f'--account 必須是 1/2/N 或 MINERU_API_TOKEN[N]，得到：{value!r}')
    return v


def load_token(account: str | None = None) -> str:
    env_name = normalize_account(account)
    tok = os.environ.get(env_name, '').strip()
    if tok:
        return tok
    env_path = ROOT / '.env'
    if env_path.exists():
        prefix = f'{env_name}='
        for line in env_path.read_text().splitlines():
            if line.startswith(prefix):
                return line.split('=', 1)[1].strip()
    sys.exit(f'{env_name} 未設定（--account 指向的 env var 找不到）')


# ── 切片計畫 ──────────────────────────────────────────────────────────────────

def plan_chunks(total_pages: int, chunk_size: int = 180, overlap: int = 1) -> list[tuple[int, int]]:
    """產生 (start, end) 1-based inclusive PDF page ranges。每 chunk ≤chunk_size 頁，
    相鄰 chunk 重疊 overlap 頁（後 chunk 起點 = 前 chunk 終點 - overlap + 1）。"""
    if chunk_size > 200:
        raise ValueError('chunk_size 不可超過 200（MinerU 硬限制）')
    out: list[tuple[int, int]] = []
    s = 1
    while True:
        e = min(s + chunk_size - 1, total_pages)
        out.append((s, e))
        if e == total_pages:
            return out
        s = e - overlap + 1


def slice_pdf(src_pdf: Path, ranges: list[tuple[int, int]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = fitz.open(src_pdf)
    out_paths: list[Path] = []
    for s, e in ranges:
        dst = fitz.open()
        dst.insert_pdf(src, from_page=s - 1, to_page=e - 1)
        p = out_dir / f'p{s:04d}-{e:04d}.pdf'
        dst.save(p, deflate=True)
        dst.close()
        out_paths.append(p)
    src.close()
    return out_paths


# ── MinerU API ───────────────────────────────────────────────────────────────

def request_upload_urls(token: str, items: list[dict], language: str) -> dict:
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        'files': [{'name': it['name'], 'data_id': it['data_id']} for it in items],
        'model_version': 'vlm',
        'language': language,
        'enable_formula': True,
        'enable_table': True,
    }
    r = requests.post(f'{BASE}/file-urls/batch', headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get('code') != 0:
        raise RuntimeError(f'申請 upload URL 失敗：{body}')
    return body['data']


def put_file(url: str, path: Path, max_retries: int = 5, timeout: int = 1200) -> int:
    """PUT 大 chunk PDF 到 OSS。每次 retry 重開 file handle（避免 stream 已消耗）。
    timeout=1200s 應付 ~150MB 慢上行（sedra 場景）。500 級錯誤也算可重試。"""
    backoff = 10
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with open(path, 'rb') as f:
                r = requests.put(url, data=f, timeout=timeout)
            if r.status_code >= 500:
                last_err = RuntimeError(f'PUT HTTP {r.status_code}')
                raise last_err
            return r.status_code
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                RuntimeError) as e:
            last_err = e
            if attempt == max_retries:
                raise
            sleep_for = backoff * attempt
            print(f'  [put-retry] {path.name} attempt {attempt} failed '
                  f'({type(e).__name__}); sleeping {sleep_for}s', flush=True)
            time.sleep(sleep_for)
    raise last_err  # pragma: no cover


def _request_with_retry(method: str, url: str, max_retries: int = 5, **kw):
    """requests.{get,put,post} 包 retry：對 Timeout / ConnectionError / 5xx 退避重試。"""
    backoff = 5
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code >= 500:
                last_err = RuntimeError(f'HTTP {r.status_code}')
                raise last_err
            return r
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                RuntimeError) as e:
            last_err = e
            if attempt == max_retries:
                raise
            sleep_for = backoff * attempt
            print(f'  [retry] {method} {url.split("?")[0][-60:]} attempt {attempt} failed ({type(e).__name__}); sleeping {sleep_for}s', flush=True)
            time.sleep(sleep_for)
    raise last_err  # pragma: no cover


def poll_batch(token: str, batch_id: str, interval: int = 10, timeout: int = 3600,
               polls_path: Path | None = None) -> dict:
    headers = {'Authorization': f'Bearer {token}'}
    start = time.time()
    last_line = None
    while True:
        r = _request_with_retry('GET', f'{BASE}/extract-results/batch/{batch_id}',
                                headers=headers, timeout=60)
        r.raise_for_status()
        body = r.json()
        elapsed = round(time.time() - start, 1)
        if polls_path:
            with polls_path.open('a') as f:
                f.write(json.dumps({'t': elapsed, 'response': body}, ensure_ascii=False) + '\n')
        results = body.get('data', {}).get('extract_result', [])
        parts = []
        for rr in results:
            name = rr.get('data_id') or rr.get('file_name', '?')
            state = rr.get('state', '?')
            prog = rr.get('extract_progress') or {}
            extra = f"({prog.get('extracted_pages')}/{prog.get('total_pages')})" if prog else ''
            parts.append(f'{name}:{state}{extra}')
        line = '  '.join(parts)
        if line != last_line:
            print(f'[{elapsed:6.1f}s] {line}')
            last_line = line
        if results and all(r['state'] in ('done', 'failed') for r in results):
            return body
        if time.time() - start > timeout:
            raise TimeoutError(f'輪詢逾時 {timeout}s')
        time.sleep(interval)


def download_zip(url: str, dst: Path) -> None:
    r = _request_with_retry('GET', url, timeout=600)
    r.raise_for_status()
    dst.write_bytes(r.content)


# ── Resume：從既有 batch_id 撿回，不需要本機 PDF ─────────────────────────────

_FNAME_RE = re.compile(r'^p(\d{4})-(\d{4})\.pdf$')


def ranges_from_results(results: list[dict]) -> list[tuple[int, int]]:
    """從 extract_result[].file_name 推回原始 chunk ranges（依 data_id 中的 c<N> 排序）。"""
    paired: list[tuple[int, int, int]] = []  # (chunk_idx, start, end)
    for r in results:
        fname = r.get('file_name', '')
        did = r.get('data_id', '')
        m = _FNAME_RE.match(fname)
        if not m:
            raise RuntimeError(f'無法從 file_name 解析 range：{fname}')
        cidx_m = re.search(r'_c(\d+)$', did)
        if not cidx_m:
            raise RuntimeError(f'無法從 data_id 解析 chunk index：{did}')
        paired.append((int(cidx_m.group(1)), int(m.group(1)), int(m.group(2))))
    paired.sort()
    return [(s, e) for _, s, e in paired]


def resume(batch_id: str, slug: str, overlap: int = 1,
           max_wait: int = 1800, account: str | None = None,
           max_retries: int = 2, auto_resubmit: bool = True,
           resubmit_wait: int = 60) -> int:
    """從 manifest（或傳入 batch_id）撿回，chunk 級冪等 + failed 自動補傳重試。

    真相來源：raw/chunk_i/ 是否含 content_list。每輪 poll 所有待收 batch、冪等
    download done chunk、對 failed/missing chunk 用本機切片補傳成新 batch，最多重試
    max_retries 輪，全齊才 assemble。MinerU 的 'parsing failed, please try again
    later' 是暫時性失敗，重提即過——這正是 auto_resubmit 要消化的。

    account=None 時從 manifest 讀（舊 entry 無 account → 預設 MINERU_API_TOKEN）。

    回傳：0=成功 assemble；2=還在 pending（poll timeout，乾淨退出讓下次再來）；
         3=部分完成（仍缺 chunk，manifest 保留，可下次再撿 / 換有切片的機器補傳）。
    """
    book_dir = DATA / slug
    book_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = book_dir / 'raw'
    raw_dir.mkdir(exist_ok=True)
    unified_dir = book_dir / 'unified'
    polls_path = book_dir / '_polls.jsonl'

    entry = next((it for it in _read_pending() if it.get('slug') == slug), None)
    if entry is None:
        # manifest 無此 slug（已移除 / 舊式直接 resume）→ 合成單 batch、ranges 待推導
        entry = {'slug': slug, 'batch_id': batch_id, 'ranges': None,
                 'overlap': overlap, 'language': 'en',
                 'account': account or DEFAULT_ACCOUNT_ENV}
    account = account or entry.get('account', DEFAULT_ACCOUNT_ENV)
    language = entry.get('language', 'en')
    overlap = entry.get('overlap', overlap)
    ranges = [tuple(r) for r in entry['ranges']] if entry.get('ranges') else None
    token = load_token(account)

    print(f'[receive] slug={slug} account={account} max_wait={max_wait}s '
          f'max_retries={max_retries} auto_resubmit={auto_resubmit}', flush=True)

    pending = _entry_batches(entry)        # 本輪待 poll 的 batch
    all_batches = list(pending)            # 審計用：累積所有用過的 batch
    t0 = time.time()

    for attempt in range(max_retries + 1):
        # 1) poll 本輪待收 batch，收集 done chunk
        done_results: dict[int, dict] = {}
        for b in pending:
            print(f'[poll] batch {b["batch_id"]}', flush=True)
            try:
                final = poll_batch(token, b['batch_id'], interval=8,
                                   timeout=max_wait, polls_path=polls_path)
            except TimeoutError as e:
                print(f'[poll-timeout] {e} — 還在 pending，乾淨退出', flush=True)
                return 2
            results = final['data']['extract_result']
            if ranges is None:               # 舊式：從含完整 chunk 的 batch 推 ranges
                ranges = ranges_from_results(results)
                print(f'[ranges] 推出 {len(ranges)} chunks: {ranges}', flush=True)
            for r in results:
                m = re.search(r'_c(\d+)$', r.get('data_id', ''))
                if m and r.get('state') == 'done':
                    done_results[int(m.group(1))] = r

        all_idxs = list(range(len(ranges)))

        # 2) 冪等 download：已撈回的 chunk 跳過
        for idx, r in sorted(done_results.items()):
            if _chunk_done(raw_dir, idx):
                continue
            zip_path = raw_dir / f'chunk_{idx}.zip'
            download_zip(r['full_zip_url'], zip_path)
            extract_zip(zip_path, raw_dir / f'chunk_{idx}')
            print(f'  chunk_{idx}: 下載解壓完', flush=True)

        # 3) 算 missing
        missing = [i for i in all_idxs if not _chunk_done(raw_dir, i)]
        print(f'[status] {len(all_idxs) - len(missing)}/{len(all_idxs)} chunk 就緒'
              + (f'；缺 {missing}' if missing else ''), flush=True)
        if not missing:
            break

        # 4) 補傳 failed/missing chunk
        if not auto_resubmit or attempt == max_retries:
            print('[stop] 仍缺 chunk，'
                  + ('關閉自動補傳' if not auto_resubmit else '重試額度用盡'), flush=True)
            break
        try:
            new_batch = submit_chunks(slug, missing, ranges, language, account)
        except FileNotFoundError as e:
            print(f'[resubmit-skip] {e}', flush=True)
            print('  → 在本機（submitter，有切片）跑 --resume 才能補傳', flush=True)
            break
        pending_add_batch(slug, new_batch, missing)
        pending = [{'batch_id': new_batch, 'chunk_idxs': missing}]
        all_batches.append(pending[0])
        print(f'[resubmit] chunk {missing} → 新 batch {new_batch}；'
              f'等 {resubmit_wait}s 再 poll（attempt {attempt + 1}/{max_retries}）', flush=True)
        time.sleep(resubmit_wait)

    # 收尾
    poll_secs = round(time.time() - t0, 1)
    missing = [i for i in range(len(ranges)) if not _chunk_done(raw_dir, i)]
    if missing:
        miss_pages = [list(ranges[i]) for i in missing]
        print(f'[partial] 仍缺 chunk {missing}（PDF 頁 {miss_pages}）— manifest 保留，'
              f'可換有切片的機器或稍後重跑 --resume', flush=True)
        return 3

    print('[assemble]', flush=True)
    summary = assemble(raw_dir, ranges, overlap, unified_dir)
    run_meta = {
        'slug': slug, 'resumed': True, 'batch_id': entry.get('batch_id'),
        'batches': all_batches, 'ranges': ranges, 'overlap': overlap,
        'poll_seconds': poll_secs, 'unified_summary': summary,
    }
    (book_dir / '_run.json').write_text(json.dumps(run_meta, ensure_ascii=False, indent=2))
    pending_remove(slug)
    print(f'  unified blocks: {summary["total_blocks"]}', flush=True)
    print(f'  images merged:  {summary["images_merged"]}', flush=True)
    print(f'[manifest] 已從 _pending_batches.json 移除 {slug}', flush=True)
    print(f'\n完成 → {unified_dir}', flush=True)
    return 0


def extract_zip(zip_path: Path, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)


# ── 組裝 ──────────────────────────────────────────────────────────────────────

def find_content_list(chunk_dir: Path) -> Path:
    files = list(chunk_dir.glob('*_content_list.json'))
    if not files:
        raise FileNotFoundError(f'找不到 *_content_list.json in {chunk_dir}')
    return files[0]


def _chunk_done(raw_dir: Path, i: int) -> bool:
    """chunk 完成的唯一真相：raw/chunk_i/ 已解壓且含 content_list。"""
    cd = raw_dir / f'chunk_{i}'
    return cd.is_dir() and bool(list(cd.glob('*_content_list.json')))


def assemble(raw_root: Path, ranges: list[tuple[int, int]], overlap: int,
             out_dir: Path) -> dict:
    """讀所有 chunk 的 content_list.json，平移 page_idx 為 global 0-based，
    去除重疊頁（後 chunk 的前 overlap 頁），合併 images/，產 unified content_list.json。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / 'images'
    img_dir.mkdir(exist_ok=True)

    unified_blocks: list[dict] = []
    md_parts: list[str] = []
    chunks_meta = []

    for i, (s, e) in enumerate(ranges):
        chunk_dir = raw_root / f'chunk_{i}'
        cl_path = find_content_list(chunk_dir)
        blocks = json.loads(cl_path.read_text())
        # MinerU page_idx 是 chunk-local 0-based。轉 global 0-based PDF page。
        # chunk i 起點 PDF 1-based = s；global_page_idx = page_idx + (s - 1)
        offset = s - 1

        # 重疊處理：i > 0 時，前 overlap 頁與上一 chunk 末尾重疊 → 丟掉本 chunk 的前 overlap 頁
        drop_local_pages = set(range(overlap)) if i > 0 else set()

        kept = 0
        for b in blocks:
            local_p = b.get('page_idx', 0)
            if local_p in drop_local_pages:
                continue
            nb = dict(b)
            nb['page_idx'] = local_p + offset
            nb['chunk_idx'] = i
            unified_blocks.append(nb)
            kept += 1

        # 合併 images/
        src_img = chunk_dir / 'images'
        if src_img.is_dir():
            for f in src_img.iterdir():
                dst = img_dir / f.name
                if not dst.exists():
                    shutil.copy2(f, dst)

        # 合併 markdown（純串接，除錯用）
        md_file = chunk_dir / 'full.md'
        if md_file.exists():
            md_parts.append(f'\n\n<!-- chunk {i}: PDF p.{s}-{e} -->\n\n' + md_file.read_text())

        chunks_meta.append({
            'chunk_idx': i,
            'pdf_pages': [s, e],
            'page_count': e - s + 1,
            'blocks_total': len(blocks),
            'blocks_kept': kept,
            'blocks_dropped_overlap': len(blocks) - kept,
        })

    # 寫出
    (out_dir / 'content_list.json').write_text(
        json.dumps(unified_blocks, ensure_ascii=False, indent=2))
    (out_dir / 'chunks.json').write_text(
        json.dumps({'ranges': ranges, 'overlap': overlap, 'chunks': chunks_meta},
                   ensure_ascii=False, indent=2))
    (out_dir / 'full.md').write_text(''.join(md_parts))

    summary = {
        'total_chunks': len(ranges),
        'total_blocks': len(unified_blocks),
        'images_merged': len(list(img_dir.iterdir())),
        'chunks': chunks_meta,
    }
    return summary


# ── Pending manifest ────────────────────────────────────────────────────────
# book_pipeline/_pending_batches.json 是 submitter ↔ receiver 跨機橋。
# Schema：[{slug, batch_id, submitted_at, ranges, overlap, language, account, batches}]
#   batch_id  — 最初 batch（保留供向後相容；新欄位是 batches）
#   ranges    — 全書 chunk 的 (start,end) PDF 頁範圍，chunk_idx = list 位置
#   batches   — [{batch_id, chunk_idxs}]：哪個 batch 涵蓋哪些 chunk_idx。
#               補傳 failed chunk 會 append 新 batch（同 chunk_idx 以最後者為準）。
#   account   — env var name（'MINERU_API_TOKEN' / 'MINERU_API_TOKEN2'）。
# 向後相容：舊 entry 無 batches → 視為單 batch 涵蓋全部 chunk（_entry_batches）；
#           無 account → 'MINERU_API_TOKEN'。

PENDING_PATH = ROOT / 'book_pipeline' / '_pending_batches.json'


def _read_pending() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    return json.loads(PENDING_PATH.read_text() or '[]')


def _write_pending(items: list[dict]) -> None:
    """Atomic write：先寫 .tmp 再 rename，避免 Ctrl-C / 併發寫一半。"""
    tmp = PENDING_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(PENDING_PATH)


def _entry_batches(entry: dict) -> list[dict]:
    """回傳 [{batch_id, chunk_idxs}]，相容舊式（無 batches 欄位 → 單 batch 涵蓋全部）。"""
    if entry.get('batches'):
        return [{'batch_id': b['batch_id'], 'chunk_idxs': list(b['chunk_idxs'])}
                for b in entry['batches']]
    n = len(entry.get('ranges') or [])
    return [{'batch_id': entry['batch_id'], 'chunk_idxs': list(range(n))}]


def pending_add(slug: str, batch_id: str, ranges: list[tuple[int, int]],
                overlap: int, language: str,
                account: str = DEFAULT_ACCOUNT_ENV) -> None:
    items = _read_pending()
    # 同 slug 已在 list → 覆寫（重新 submit 的情況）
    items = [it for it in items if it.get('slug') != slug]
    items.append({
        'slug': slug,
        'batch_id': batch_id,
        'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'ranges': ranges,
        'overlap': overlap,
        'language': language,
        'account': account,
        'batches': [{'batch_id': batch_id, 'chunk_idxs': list(range(len(ranges)))}],
    })
    _write_pending(items)


def pending_add_batch(slug: str, batch_id: str, chunk_idxs: list[int]) -> None:
    """補傳 failed chunk 後，把新 batch 追加進該 slug 的 batches。
    若 manifest 無此 slug（已被移除）→ no-op（receiver 用 in-memory 狀態續跑）。"""
    items = _read_pending()
    for it in items:
        if it.get('slug') == slug:
            bs = it.get('batches') or _entry_batches(it)
            bs.append({'batch_id': batch_id, 'chunk_idxs': list(chunk_idxs)})
            it['batches'] = bs
            _write_pending(items)
            return


def pending_remove(slug: str) -> None:
    items = _read_pending()
    items = [it for it in items if it.get('slug') != slug]
    _write_pending(items)


# ── Submit：切 + 上傳 + 寫 manifest，不 poll ─────────────────────────────────

def submit(pdf_path: Path, slug: str, chunk_size: int, overlap: int,
           language: str, account: str = DEFAULT_ACCOUNT_ENV) -> str:
    """slice + PUT 上傳 + 寫 _pending_batches.json + 退出。回傳 batch_id。
    這是 submitter 角色：PDF 來源機（你的筆電）跑這個。
    account：env var name（'MINERU_API_TOKEN' / 'MINERU_API_TOKEN2'）。"""
    book_dir = DATA / slug
    book_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = book_dir / 'chunks'

    src = fitz.open(pdf_path)
    total = src.page_count
    src.close()
    ranges = plan_chunks(total, chunk_size, overlap)
    print(f'[plan] {slug}: {total} 頁 → {len(ranges)} chunks  '
          f'(size≤{chunk_size}, overlap={overlap})', flush=True)
    for i, (s, e) in enumerate(ranges):
        print(f'  chunk_{i}: p.{s}-{e}  ({e-s+1} 頁)', flush=True)

    print('\n[slice] 切 chunk PDF', flush=True)
    chunk_pdfs = slice_pdf(pdf_path, ranges, chunks_dir)
    for p in chunk_pdfs:
        print(f'  {p.name}: {p.stat().st_size/1024:.1f} KB', flush=True)

    print('\n[upload] 申請 URL', flush=True)
    print(f'  account={account}', flush=True)
    token = load_token(account)
    items = [{'name': p.name, 'data_id': f'{slug}_c{i}'} for i, p in enumerate(chunk_pdfs)]
    upload = request_upload_urls(token, items, language)
    batch_id = upload['batch_id']
    urls = upload['file_urls']
    print(f'  batch_id={batch_id}', flush=True)

    print('\n[upload] PUT 上傳', flush=True)
    for p, url in zip(chunk_pdfs, urls):
        t0 = time.time()
        code = put_file(url, p)
        print(f'  {p.name}: HTTP {code}  {time.time()-t0:.1f}s', flush=True)

    pending_add(slug, batch_id, ranges, overlap, language, account=account)
    print(f'\n[manifest] 已加入 {PENDING_PATH}', flush=True)
    print(f'\n完成 submit → batch_id={batch_id}', flush=True)
    print(f'下一步：git add {PENDING_PATH.relative_to(ROOT)} && git commit && push', flush=True)
    print(f'        然後在任何地方跑 receive（雲端 agent / 本機）', flush=True)
    return batch_id


def submit_chunks(slug: str, idxs: list[int], ranges: list[tuple[int, int]],
                  language: str, account: str) -> str:
    """補傳指定 chunk_idx：用本機已存切片 chunks/p{s}-{e}.pdf 重新 PUT，回新 batch_id。
    只在有切片的機器（submitter / 本機一條龍）能跑；receiver 端無切片會 raise。
    data_id 仍用 {slug}_c{i} 保留全域 chunk index，receiver download 時據此歸位。"""
    chunks_dir = DATA / slug / 'chunks'
    items, paths = [], []
    for i in idxs:
        s, e = ranges[i]
        p = chunks_dir / f'p{s:04d}-{e:04d}.pdf'
        if not p.exists():
            raise FileNotFoundError(
                f'本機缺切片 {p}（補傳 failed chunk 需在 submitter 機器，receiver 無切片）')
        items.append({'name': p.name, 'data_id': f'{slug}_c{i}'})
        paths.append(p)
    token = load_token(account)
    upload = request_upload_urls(token, items, language)
    batch_id = upload['batch_id']
    for p, url in zip(paths, upload['file_urls']):
        put_file(url, p)
    return batch_id


# ── 主流程：一條龍（本機完整跑） ────────────────────────────────────────────

def ingest(pdf_path: Path, slug: str, chunk_size: int, overlap: int,
           language: str, skip_upload: bool,
           account: str = DEFAULT_ACCOUNT_ENV) -> None:
    """一條龍模式：submit + resume 合一。本機網路穩、有耐心的可用。
    若中途崩潰，batch_id 已寫進 _pending_batches.json，可改用 --resume 撿。"""
    if not skip_upload:
        batch_id = submit(pdf_path, slug, chunk_size, overlap, language, account=account)
    else:
        # skip-upload：用既有 raw/ 重做組裝（沒 manifest 也要能跑）
        pend = next((it for it in _read_pending() if it.get('slug') == slug), None)
        if pend is None:
            sys.exit(f'--skip-upload 需要 _pending_batches.json 有 {slug} 的紀錄')
        batch_id = pend['batch_id']
        account = pend.get('account', DEFAULT_ACCOUNT_ENV)

    # 接 receive（poll + download + assemble）
    rc = resume(batch_id, slug, overlap, max_wait=3600, account=account)
    if rc == 0:
        pending_remove(slug)
        print(f'[manifest] 已從 _pending_batches.json 移除 {slug}', flush=True)
    sys.exit(rc)


def main() -> None:
    ap = argparse.ArgumentParser(description=(
        '三種模式：\n'
        '  submit:  --upload PDF [--slug N]     切+傳 MinerU+寫 manifest（PDF 來源機跑）\n'
        '  resume:  --resume BATCH --slug N     撿 batch+下載+組裝（任何地方跑）\n'
        '  all:     PDF [--slug N]              一條龍（本機網路穩用這個）'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', type=Path, nargs='?',
                    help='輸入 PDF（一條龍模式必填，--resume / --upload 自動偵測）')
    ap.add_argument('--slug', help='指定 slug（預設取 PDF 檔名）')
    ap.add_argument('--chunk-size', type=int, default=180)
    ap.add_argument('--overlap', type=int, default=1)
    ap.add_argument('--language', default='en')
    ap.add_argument('--upload', action='store_true',
                    help='只切 + 傳 + 寫 manifest，不 poll（submitter 角色）')
    ap.add_argument('--resume', metavar='BATCH_ID',
                    help='撿既有 batch（必須同時帶 --slug）')
    ap.add_argument('--max-wait', type=int, default=1800,
                    help='resume poll timeout，秒（預設 1800=30 分）')
    ap.add_argument('--max-retries', type=int, default=2,
                    help='failed chunk 自動補傳重試輪數（預設 2；需本機有切片）')
    ap.add_argument('--no-resubmit', action='store_true',
                    help='關閉自動補傳：只撿現有 done chunk，缺的回報 rc=3')
    ap.add_argument('--resubmit-wait', type=int, default=60,
                    help='補傳後等多久再 poll，秒（預設 60）')
    ap.add_argument('--skip-upload', action='store_true',
                    help='[deprecated] 用 --resume 取代')
    ap.add_argument('--account', default=None,
                    help='指定 MinerU 帳號：1/2/N 或 MINERU_API_TOKEN[N]（預設 1）。'
                         '--resume 模式不傳則從 manifest 讀。')
    args = ap.parse_args()

    # Resume 模式：account=None 觸發從 manifest 讀；明確指定才覆寫
    if args.resume:
        if not args.slug:
            sys.exit('--resume 必須同時帶 --slug')
        acc = normalize_account(args.account) if args.account else None
        rc = resume(args.resume, args.slug, args.overlap,
                    max_wait=args.max_wait, account=acc,
                    max_retries=args.max_retries,
                    auto_resubmit=not args.no_resubmit,
                    resubmit_wait=args.resubmit_wait)
        sys.exit(rc)

    # Submit-only 模式
    if args.upload:
        if not args.pdf:
            sys.exit('--upload 需要 PDF 路徑')
        pdf = args.pdf.resolve()
        if not pdf.exists():
            sys.exit(f'PDF 不存在：{pdf}')
        slug = args.slug or pdf.stem
        submit(pdf, slug, args.chunk_size, args.overlap, args.language,
               account=normalize_account(args.account))
        return

    # All-in-one 模式（舊行為）
    if not args.pdf:
        sys.exit('需要 PDF 路徑（或用 --resume / --upload）')
    pdf = args.pdf.resolve()
    if not pdf.exists():
        sys.exit(f'PDF 不存在：{pdf}')
    slug = args.slug or pdf.stem
    ingest(pdf, slug, args.chunk_size, args.overlap, args.language,
           args.skip_upload, account=normalize_account(args.account))


if __name__ == '__main__':
    main()
