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
import json
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
# headless LLM 派工的 wall-clock 上限（秒）。逾時殺整個子工 process group，避免單一
# audit 的子 agent 陷入迴圈時拖死整個 daemon（曾見 kimi audit 重讀 content_list 卡 6.5h）。
# 正常 audit ~25min；40min 留足餘裕，只在真卡死時觸發。可用 env 覆寫。
LLM_TIMEOUT = int(os.environ.get('BOOK_PIPELINE_LLM_TIMEOUT', '2400'))

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


_last_snap = 0.0


def _refresh_snapshot() -> None:
    """事件驅動刷新 dev 監控快照：每個 log 事件順手重生 dev/status.json，
    節流 ~8s 避免一個 tick 內暴衝（per-book audit 有成本）。best-effort，絕不拖垮 tick。"""
    global _last_snap
    import time
    now = time.monotonic()
    if now - _last_snap < 8:
        return
    _last_snap = now
    try:
        from book_pipeline.devctl import write_snapshot
        write_snapshot()
    except Exception:
        pass


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')
    _refresh_snapshot()


def _run(cmd: list[str], cwd: str = ROOT, dry: bool = False,
         env: dict | None = None, timeout: int | None = None) -> int:
    log(('DRY ' if dry else 'RUN ') + ' '.join(shlex.quote(c) for c in cmd))
    if dry:
        return 0
    if timeout is None:
        return subprocess.run(cmd, cwd=cwd, env=env).returncode
    # 有 timeout（LLM 派工）：start_new_session 讓子工自成 process group，逾時殺整組。
    # claude -p 會 spawn 子 agent（孫程序），單殺父程序會留孤兒繼續空轉，故用 killpg。
    import signal
    import time
    p = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True)
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f'⏱ TIMEOUT {timeout}s → 殺子工 process group（pid={p.pid}）；下個 tick 將自動重派')
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            time.sleep(5)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return -1


def _llm_env() -> dict | None:
    """LLM 派工的環境。BOOK_PIPELINE_PROVIDER=kimi → 把同一個 claude CLI（harness 不變）
    導到 Kimi Code 端點（key 讀 ~/.secrets/kimi.env，不進全域 env）。其餘情況回 None
    （沿用現有環境＝Claude Max 訂閱）。"""
    if os.environ.get('BOOK_PIPELINE_PROVIDER', '').lower() != 'kimi':
        return None
    key_path = os.path.expanduser('~/.secrets/kimi.env')
    try:
        with open(key_path) as f:
            key = f.read().strip()
    except OSError:
        log(f'⚠ provider=kimi 但讀不到 {key_path} → 退回預設供應商')
        return None
    if not key:
        log(f'⚠ {key_path} 為空 → 退回預設供應商')
        return None
    env = dict(os.environ)
    env.update({
        'ANTHROPIC_BASE_URL': 'https://api.kimi.com/coding',
        'ANTHROPIC_AUTH_TOKEN': key,
        'ANTHROPIC_MODEL': 'kimi-for-coding',
        'ANTHROPIC_DEFAULT_OPUS_MODEL': 'kimi-for-coding',
        'ANTHROPIC_DEFAULT_SONNET_MODEL': 'kimi-for-coding',
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'kimi-for-coding',
        'ANTHROPIC_SMALL_FAST_MODEL': 'kimi-for-coding',
    })
    return env


def dispatch_llm(todo_verb: str, slug: str | None, dry: bool) -> int:
    prompt = LLM_PROMPTS[todo_verb].format(slug=slug or '')
    cmd = [CLAUDE_BIN, '-p', prompt, '--add-dir', ROOT]
    return _run(cmd, cwd=ROOT, dry=dry, env=_llm_env(), timeout=LLM_TIMEOUT)


def _wishlist_pending() -> list:
    """crawl_wishlist.json 的未滿足主題。格式：{"topics": [...]}（agent 自行判斷選書）。"""
    import json
    try:
        w = json.load(open(WISHLIST)) or {}
        return w.get('topics', []) if isinstance(w, dict) else (w or [])
    except Exception:
        return []


CRAWL_SENTINEL = os.path.join(BP, 'reports', 'crawl_last.json')


def _zlib_remaining() -> int | None:
    """查 z-library 今日剩餘下載額度；查不到回 None（不當成 0，避免誤判耗盡）。"""
    try:
        out = subprocess.run(
            ['uv', 'run', '--with', 'requests', 'python', '-m',
             'book_pipeline.crawl_zlib', 'limits'],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or '{}').get('remaining')
    except Exception as e:
        log(f'crawl：查額度失敗 {e}')
        return None


def _read_crawl_sentinel() -> dict | None:
    """crawl agent 收尾寫的結構化結果（reports/crawl_last.json）。讀不到回 None。"""
    try:
        return json.load(open(CRAWL_SENTINEL))
    except Exception:
        return None


def do_crawl(dry: bool) -> bool:
    """wishlist 驅動的爬書（LLM）。有主題且今日額度 > 0 才派 headless claude。
    回傳是否消耗了一個 LLM slot。

    關鍵：dispatch 後**客觀驗證產出**（額度是否消耗 + agent sentinel），不再相信
    agent 自述「成功」。空手/失敗一律 log 成 `❌`（被 devctl ERROR_RE 捕獲 → /dev
    頁可見），杜絕「crawl 沒補到書卻一片綠、要人追 transcript」的 silent failure。"""
    topics = _wishlist_pending()
    if not topics:
        return False
    if dry:
        log(f'crawl plan：wishlist 有 {len(topics)} 主題（真跑時查額度後派 agent 選書）')
        dispatch_llm('crawl', None, dry=True)
        return True
    rem = _zlib_remaining()
    if rem is None:
        log('crawl skip：查額度失敗，本 tick 不派 crawl')
        return False
    if rem <= 0:
        log(f'crawl defer：今日下載額度耗盡（remaining={rem}）→ 明日')
        return False
    # daily tick：把 zlib 當日額度爬滿——loop 直到額度耗盡／agent 回 no_candidate／失敗。
    # 每輪客觀驗證（額度消耗是「真下載成功」的硬訊號），非成功一律 break 避免空轉無限迴圈。
    fetched = 0
    while rem > 0:
        try:
            os.remove(CRAWL_SENTINEL)
        except FileNotFoundError:
            pass
        log(f'crawl dispatch（第 {fetched + 1} 本）：wishlist {len(topics)} 主題，額度剩 {rem}')
        dispatch_llm('crawl', None, dry=False)
        rem_after = _zlib_remaining()
        res = _read_crawl_sentinel()
        if rem_after is not None and rem_after < rem:
            fetched += 1
            log(f'crawl ok：已補書 slug={(res or {}).get("slug", "?")}（額度 {rem}→{rem_after}）')
            rem = rem_after
            continue  # 額度還有 → 繼續爬下一本，把當日額度榨乾
        if res is None:
            log(f'❌ crawl 空手：agent 未回報結果（崩潰／逾時？額度 {rem} 未動）→ 停止本輪')
        elif res.get('action') == 'no_candidate':
            log(f'crawl 收斂：{res.get("reason", "無更多合格經典缺口")}（已補 {fetched} 本，正常）')
        elif res.get('action') == 'failed':
            log(f'❌ crawl 失敗：{res.get("reason", "未知")}（額度未消耗）→ 停止本輪')
        else:  # action=fetched 卻沒扣額度 → 自以為成功但其實沒下到
            log(f'❌ crawl 異常：agent 宣稱 fetched slug={res.get("slug", "?")} 但額度未消耗，'
                f'疑 z-library /dl/ 下載端故障 → 停止本輪')
        break
    log(f'crawl 本輪結束：共補 {fetched} 本，額度剩 {rem}')
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


def do_catalog_repair(slug: str, dry: bool) -> int:
    """catalog_audit 殘漏的確定性修復閉環（無 LLM）：repair 三件套 → re-audit。
    三個 repair 各自確定性、自帶 _manual_repair_backups（可回滾）；metadata 補 caption/id、
    from_unified 從 MinerU 原文找回缺塊、aliases 連結 ref→既有塊。critical 清零 → catalog
    過關，下個 tick 自然推進（sol/deploy）；殘餘 critical → surface ❌（少數書需 LLM/人工）。
    repair 改的是 parsed/*.json，status 判定『有 book.json 即不重 parse』故結果持久。"""
    from book_pipeline.catalog_audit import audit_catalog
    before = audit_catalog(slug, write_report=False).get('critical') or 0
    if before == 0:
        return 0
    log(f'catalog_repair {slug}：critical={before} → 跑確定性 repair 三件套')
    if dry:
        return 0
    _run(['uv', 'run', 'python', '-m', 'book_pipeline.repair_catalog_metadata', '--slug', slug])
    _run(['uv', 'run', 'python', '-m', 'book_pipeline.repair_catalog_from_unified', slug])
    _run(['uv', 'run', 'python', '-m', 'book_pipeline.repair_catalog_aliases', slug])
    after = audit_catalog(slug, write_report=False).get('critical') or 0
    if after == 0:
        log(f'catalog_repair {slug} ✓：critical {before}→0，catalog 過關')
    else:
        log(f'❌ catalog_repair {slug}：critical {before}→{after}，殘餘 {after} 項需 LLM/人工修')
    return 0


def tick(dry: bool, max_llm: int, no_deploy: bool) -> int:
    log(f'=== tick start (dry={dry}, max_llm={max_llm}) ===')
    log('budget: ' + str(mb.status_report()))

    # 2) resume in-flight ingests（無視預算）
    for slug in sorted(mb.in_flight()):
        do_ingest(slug, dry)

    llm_done = 0

    # 3) actionable（先做：已在手的書 ingest/parse/deploy/catalog 全推完，先榨乾 MinerU 額度）
    #    crawl 放最後（step 4）——zlib 與 MinerU 是獨立資源池，crawl loop 不可阻塞 ingest。
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
            if max_llm and llm_done >= max_llm:
                log(f'skip {slug} {todo}（本 tick LLM 上限 {max_llm} 已滿，debug 壓低）')
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
            do_catalog_repair(slug, dry)
        else:
            log(f'skip {slug}：未知確定性 todo={todo}')

    # 4) wishlist 驅動的爬書（LLM，補新書，放最後榨乾 zlib——新書這 tick 只到 qc，下 tick 才 ingest）
    if do_crawl(dry):
        llm_done += 1

    log('=== tick end ===')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='自動化迴圈單次 tick')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', help='印計劃不執行（預設）')
    g.add_argument('--once', action='store_true', help='真正執行一次')
    # 限流交給外部額度（zlib 10/日、MinerU 預算）與 per-LLM 40min timeout，不用人為計數。
    # daily tick：LLM 可解的階段（qc/audit/sol_extract）每天一次全做完。--max-llm 0=不限
    # （預設，額度驅動）；>0 僅供 debug 壓低派工數。
    ap.add_argument('--max-llm', type=int, default=0, help='headless claude 上限，0=不限（預設）')
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
