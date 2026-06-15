#!/usr/bin/env python3
"""book_pipeline.pipeline_tick — 自動化迴圈的單次 tick（launchd 每 30–60min 觸發）。

職責：讀 pipeline_queue 全 stage 真相 → 推進。確定性階段 daemon 直跑，需判斷的
階段（crawl / qc / audit）派 headless `claude -p` 跑對應 skill/reference。

一個 tick：
  1. flock 防重入（launchd 可能在前一 tick 未結束時又觸發）
  2. resume 所有 in-flight ingest（_pending_batches.json 有）— 無視預算，冪等續完
  3. 依 pipeline 上游優先走 actionable：
       det:  ingest（預算挑帳號）/ parse / deploy
       LLM:  crawl / qc / audit / sol_extract → headless claude（每 tick 上限 --max-llm）
  4. log 到 reports/daemon.log

安全鐵則：
  - dry-run（預設需顯式 --once 才真跑）印計劃不執行，供驗證。
  - LLM 派工與 ingest 是對外/計費動作；deploy 改為純本地 build（nginx 直讀，無 push）；launchd 啟用由使用者明確授權。
  - 單項錯不停，記錄續下一項。

用法：
  uv run --with pymupdf --with requests python -m book_pipeline.pipeline_tick --dry-run
  uv run ... python -m book_pipeline.pipeline_tick --once [--max-llm 1] [--no-deploy]
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from book_pipeline import pipeline_queue as q
from book_pipeline import mineru_budget as mb

ROOT = q.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
LOCK = os.path.join(BP, '.tick.lock')
LOG = os.path.join(BP, 'reports', 'daemon.log')
WISHLIST = os.path.join(BP, 'crawl_wishlist.json')
READER_ROOT = q.READER_ROOT
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')

# LLM 階段 → headless claude 任務描述（指向既有 skill/reference）
LLM_PROMPTS = {
    'crawl': (
        "你是 book_pipeline 自動爬書 agent。讀 book_pipeline/crawl_wishlist.json 的主題與 "
        "`crawl_zlib inventory` 的現有書況，決定一本最該補的書，`crawl_zlib search` 選版次後 "
        "`crawl_zlib fetch` 下載。遵 .claude/skills/book-pipeline/references/crawl.md。"),
    'qc': (
        "對 slug={slug} 跑 `pdf_contactsheet {slug}`，看產出的 PNG，判斷書是否正確/清晰/完整/"
        "可供 MinerU OCR。結論呼叫 `python -m book_pipeline.pipeline_queue` 的 set_qc："
        "通過用 pass、不可用 reject。遵 references/qc.md。"),
    'audit': (
        "對 slug={slug} 執行 /book-pipeline 的 audit-book 流程（references/audit-book.md）：產 "
        "extract_rules.yaml → parser → smoke iterate。"),
    'sol_extract': (
        "對主書 slug={slug} 執行 audit-sol 流程（references/audit-sol.md）merge 解答書。"),
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def _run(cmd: list[str], cwd: str = ROOT, dry: bool = False) -> int:
    log(('DRY ' if dry else 'RUN ') + ' '.join(shlex.quote(c) for c in cmd))
    if dry:
        return 0
    return subprocess.run(cmd, cwd=cwd).returncode


def dispatch_llm(todo_verb: str, slug: str | None, dry: bool) -> int:
    prompt = LLM_PROMPTS[todo_verb].format(slug=slug or '')
    cmd = [CLAUDE_BIN, '-p', prompt, '--add-dir', ROOT]
    return _run(cmd, cwd=ROOT, dry=dry)


def _wishlist_pending() -> list:
    """crawl_wishlist.json 的未滿足主題。格式：{"topics": [...]}（agent 自行判斷選書）。"""
    import json
    try:
        w = json.load(open(WISHLIST)) or {}
        return w.get('topics', []) if isinstance(w, dict) else (w or [])
    except Exception:
        return []


def do_crawl(dry: bool) -> bool:
    """wishlist 驅動的爬書（LLM）。有主題且今日下載額度 > 0 才派 headless claude。
    回傳是否消耗了一個 LLM slot。"""
    topics = _wishlist_pending()
    if not topics:
        return False
    if dry:
        log(f'crawl plan：wishlist 有 {len(topics)} 主題（真跑時查額度後派 agent 選書）')
        dispatch_llm('crawl', None, dry=True)
        return True
    # 真跑：查下載額度
    try:
        out = subprocess.run(
            ['uv', 'run', '--with', 'requests', 'python', '-m',
             'book_pipeline.crawl_zlib', 'limits'],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        import json
        rem = json.loads(out.stdout or '{}').get('remaining')
    except Exception as e:
        log(f'crawl skip：查額度失敗 {e}')
        return False
    if not rem or rem <= 0:
        log(f'crawl defer：今日下載額度耗盡（remaining={rem}）→ 明日')
        return False
    log(f'crawl dispatch：wishlist {len(topics)} 主題，額度剩 {rem}')
    dispatch_llm('crawl', None, dry=False)
    return True


def do_ingest(slug: str, dry: bool) -> int:
    pages = mb.estimate_pages(slug) or 200
    if slug in mb.in_flight():
        log(f'ingest resume {slug}（in-flight，無視預算）')
        return 0 if dry else mb.run_ingest(slug, mb.ACCOUNTS[0])
    acct = mb.pick_account(pages)
    if not acct:
        log(f'ingest defer {slug}：{pages} 頁，今日各帳號預算不足 → 明日')
        return 0
    log(f'ingest start {slug}：{pages} 頁 → {acct}')
    if dry:
        return 0
    mb.record_start(slug, acct, pages)
    return mb.run_ingest(slug, acct)


def do_parse(slug: str, dry: bool) -> int:
    return _run(['uv', 'run', '--with', 'pyyaml', 'python', '-m',
                 'book_pipeline.parser', slug], dry=dry)


def do_deploy(slug: str, dry: bool, no_deploy: bool) -> int:
    if no_deploy:
        log(f'deploy skip {slug}（--no-deploy）')
        return 0
    if not os.path.isdir(READER_ROOT):
        log(f'deploy skip {slug}：找不到 textbook-reader ({READER_ROOT})')
        return 0
    # build-only：烤出本地 data/<slug> + img/<slug>，nginx 直讀工作目錄即時上站（無 git/push）
    build = ['uv', 'run', 'python', '-m', 'build.build_all', slug]
    log(('DRY ' if dry else 'RUN ') + 'build_all ' + slug)
    if not dry:
        subprocess.run(build, cwd=READER_ROOT)
        q.mark_deployed(slug)
    return 0


def tick(dry: bool, max_llm: int, no_deploy: bool) -> int:
    log(f'=== tick start (dry={dry}, max_llm={max_llm}) ===')
    log('budget: ' + str(mb.status_report()))

    # 2) resume in-flight ingests（無視預算）
    for slug in sorted(mb.in_flight()):
        do_ingest(slug, dry)

    llm_done = 0

    # 3) wishlist 驅動的爬書（LLM，最上游）
    if do_crawl(dry):
        llm_done += 1

    # 4) actionable（上游優先）
    rows = q.build_queue()
    actionable = [r for r in rows
                  if r['todo'] not in ('—', '', 'translate(可選)')
                  and not r['stage'].startswith('R')]
    order = {'0.2': 1, '0.3': 2, '0.5': 2, '1': 3, '2': 4, '3': 5, '4': 5}
    actionable.sort(key=lambda r: order.get(r['stage'].split()[0], 9))

    for r in actionable:
        slug, todo = r['slug'], r['todo']
        verb = todo.split('(')[0]
        if r['llm'] or verb in LLM_PROMPTS:
            if llm_done >= max_llm:
                log(f'skip {slug} {todo}（本 tick LLM 上限 {max_llm} 已滿）')
                continue
            log(f'LLM dispatch {slug} → {verb}')
            dispatch_llm(verb if verb in LLM_PROMPTS else 'audit', slug, dry)
            llm_done += 1
            continue
        # 確定性
        if verb == 'ingest':
            do_ingest(slug, dry)
        elif verb == 'parse':
            do_parse(slug, dry)
        elif verb == 'deploy':
            do_deploy(slug, dry, no_deploy)
        elif verb == 'catalog_audit':
            log(f'defer {slug}：catalog_audit 需修復流程（catalog_audit.py / repair_*），surface 不自動跑')
        else:
            log(f'skip {slug}：未知確定性 todo={todo}')

    log('=== tick end ===')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='自動化迴圈單次 tick')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', help='印計劃不執行（預設）')
    g.add_argument('--once', action='store_true', help='真正執行一次')
    ap.add_argument('--max-llm', type=int, default=1, help='本 tick headless claude 上限')
    ap.add_argument('--no-deploy', action='store_true', help='跳過 build+push')
    args = ap.parse_args()
    dry = not args.once

    lf = open(LOCK, 'w')
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('上一個 tick 仍在執行，跳過本次')
        return 0
    try:
        return tick(dry, args.max_llm, args.no_deploy)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


if __name__ == '__main__':
    sys.exit(main())
