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
import concurrent.futures as cf
import fcntl
import json
import os
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from book_pipeline import pipeline_queue as q
from book_pipeline import mineru_budget as mb
from book_pipeline import worker_registry as wr

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
# ingest async upload 的並行度：upload 是 IO bound（切片+PUT MinerU，~8min/本），多本
# 並行打滿上傳頻寬。manifest RMW 由 mineru_ingest 的 fcntl 鎖保護，並行安全。
INGEST_PARALLEL = int(os.environ.get('BOOK_PIPELINE_INGEST_PARALLEL', '4'))
# LLM 階段（audit/qc/sol_extract）並行度：各書 LLM 任務獨立無依賴 → 全並行。預設 0=不限
# （= 本輪待推進書數，全部同時跑）。經驗上 Claude 並發數 + 本機 RAM 都撞不到瓶頸
# （Max 限制是 token rolling window 非並發數）；真要壓低（debug）才設 env >0。
LLM_PARALLEL = int(os.environ.get('BOOK_PIPELINE_LLM_PARALLEL', '0'))
# 並行 crawl 下載度：planner 選好 K 本後，K 個確定性 `crawl_zlib fetch` 並行下載（IO bound，
# 40MB/本）。帳號由 daemon 依各帳號餘額預先指派（--account），不靠 agent 自選 → 零碰撞。
CRAWL_PARALLEL = int(os.environ.get('BOOK_PIPELINE_CRAWL_PARALLEL', '6'))

# LLM 階段 → headless claude 任務描述（指向既有 skill/reference）
LLM_PROMPTS = {
    'crawl_plan': (
        "你是 book_pipeline 自動爬書**規劃** agent（只選書、**不下載**）。步驟："
        "(1) 跑 `uv run --with requests python -m book_pipeline.crawl_zlib limits` 看今日總剩餘額度 R。"
        "(2) 讀 book_pipeline/crawl_wishlist.json 主題 + `crawl_zlib inventory` 現有書況。"
        "(3) 挑最多 min(R, 12) 本**互異**、最該補的經典缺口；逐本 `crawl_zlib search` 選最佳版次"
        "（最新、OCR 友善），取得每本 id 與 hash。**互異鐵則**：同一本（含不同版次）只列一次、"
        "且不得與 inventory 已有者重複。(4) 把計畫寫成 JSON 到 book_pipeline/reports/crawl_plan.json："
        '{{"books":[{{"slug":"..","id":"..","hash":"..","title":".."}}],"reason":".."}}，'
        "無合格缺口則 books 空陣列、reason 說明。**絕不執行 fetch**。遵 references/crawl.md 選書規則。"),
    'qc': (
        "對 slug={slug} 跑 `pdf_contactsheet {slug}`，看產出的 PNG，判斷書是否正確/清晰/完整/"
        "可供 MinerU OCR。結論呼叫 `python -m book_pipeline.pipeline_queue` 的 set_qc："
        "通過用 pass、不可用 reject。遵 references/qc.md。"),
    'audit': (
        "對 slug={slug} 執行 /book-pipeline 的 audit-book 流程（references/audit-book.md）：產 "
        "extract_rules.yaml → parser → smoke iterate。"),
    'sol_extract': (
        "對主書 slug={slug} 執行 audit-sol 流程（references/audit-sol.md）merge 解答書。"),
    'catalog_audit': (
        "對 slug={slug} 執行 catalog 修復流程，**嚴格遵照 references/catalog-audit.md** "
        "（含各 critical 類別的查證與修法、override action 語意、陷阱）：跑 audit_catalog 看殘留 "
        "→ 產 book_pipeline/catalog_overrides/{slug}.json → apply_catalog_overrides → 重審，"
        "把 critical 降到最低（多數可全清零）。真不可修者（源頭缺）列入 _catalog_audit.md 即可收工。"),
}


_last_snap = 0.0
_snap_lock = threading.Lock()


def _refresh_snapshot() -> None:
    """事件驅動刷新 dev 監控快照：每個 log 事件順手重生 dev/status.json，節流 ~8s。
    **絕不在 _log_lock 內呼叫**：build_snapshot 重（評估全書 + 讀 pending/state）且會碰
    其他鎖 → 若持 _log_lock 跑它，會與『持他鎖又要 log』的 thread 反轉死鎖（並行下必現）。
    自帶 non-blocking _snap_lock：已有 thread 在刷就跳過，避免 N thread 同時 build_snapshot。"""
    global _last_snap
    import time
    now = time.monotonic()
    if now - _last_snap < 8:
        return
    if not _snap_lock.acquire(blocking=False):
        return  # 別的 thread 正在刷 → 跳過本次（best-effort）
    try:
        _last_snap = now
        from book_pipeline.devctl import write_snapshot
        write_snapshot()
    except Exception:
        pass
    finally:
        _snap_lock.release()


_log_lock = threading.Lock()


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    # _log_lock 只守 print + 寫檔（真正的共享 mutation，廉價、不碰他鎖）。snapshot 刷新
    # 移到鎖外：它重且碰他鎖，留在鎖內會死鎖（見 _refresh_snapshot docstring）。
    with _log_lock:
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


# Claude Max 5 小時滾動窗（非週額度）撞頂時 claude -p 會吐這些字串、秒退。撞到後本 tick
# 內其餘 LLM 派工一律 defer（不再空轉 25s/本 + 假「停滯」log）；下個 tick clear 重試。
_llm_exhausted = threading.Event()
SESSION_LIMIT_MARKERS = ('session limit', 'hit your session', 'usage limit')


def _tool_label(blk: dict) -> str:
    """tool_use block → 「工具名: 代表性參數」精簡標籤（給 /dev 工人面板）。"""
    name = blk.get('name', 'tool')
    inp = blk.get('input') or {}
    for k in ('command', 'file_path', 'path', 'pattern', 'query',
              'description', 'url', 'prompt', 'slug'):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return f'{name}: {v.strip()}'
    return name


def dispatch_llm(todo_verb: str, slug: str | None, dry: bool) -> int:
    """派 headless claude 跑 LLM 階段。回 rc；-2 = 撞 Claude 5h session 限額 → 呼叫端 defer。
    輸出走 stream-json：逐步 tool_use/text 事件即時餵 worker_registry（/dev 工人面板看
    最近 5 條工具調用/發言 + 總調用數）；合併文字仍掃 SESSION_LIMIT_MARKERS 設 _llm_exhausted。"""
    prompt = LLM_PROMPTS[todo_verb].format(slug=slug or '')
    cmd = [CLAUDE_BIN, '-p', prompt, '--add-dir', ROOT,
           '--output-format', 'stream-json', '--verbose']
    if dry:
        log('DRY ' + ' '.join(shlex.quote(c) for c in cmd))
        return 0
    if _llm_exhausted.is_set():
        log(f'defer LLM {todo_verb} {slug or ""}：本 tick 已撞 Claude session 限額，等下個 tick reset')
        return -2
    log(f'RUN llm {todo_verb} {slug or ""}（stream-json）')
    import signal
    import time
    provider = os.environ.get('BOOK_PIPELINE_PROVIDER', 'claude').lower()
    p = subprocess.Popen(cmd, cwd=ROOT, env=_llm_env(), start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    captured: list[str] = []
    wkey = f'{slug or todo_verb}:{p.pid}'
    wr.register(wkey, slug, todo_verb, p.pid, provider)

    def _pump():
        for line in p.stdout:  # type: ignore[union-attr]
            captured.append(line)
            s = line.strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except Exception:
                continue
            if ev.get('type') != 'assistant':
                continue
            for blk in (ev.get('message', {}).get('content') or []):
                bt = blk.get('type')
                if bt == 'tool_use':
                    lbl = _tool_label(blk)
                    wr.event(wkey, 'tool', lbl)
                    sys.stdout.write(f'[{slug or todo_verb}] 🔧 {lbl[:160]}\n')
                elif bt == 'text':
                    txt = (blk.get('text') or '').strip()
                    if txt:
                        wr.event(wkey, 'text', txt)
                        sys.stdout.write(f'[{slug or todo_verb}] 💬 {txt[:160]}\n')
    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    try:
        try:
            rc = p.wait(timeout=LLM_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f'⏱ TIMEOUT {LLM_TIMEOUT}s → 殺子工 process group（pid={p.pid}）；下個 tick 重派')
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                time.sleep(5)
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return -1
        t.join(timeout=5)
        out = ''.join(captured).lower()
        if any(m in out for m in SESSION_LIMIT_MARKERS):
            _llm_exhausted.set()
            log(f'❌ Claude 5h session 限額已滿（{todo_verb} {slug or ""}）→ 本 tick 餘 LLM 全 defer，下個 tick reset 後重試')
            return -2
        return rc
    finally:
        wr.unregister(wkey)


def _wishlist_pending() -> list:
    """crawl_wishlist.json 的未滿足主題。格式：{"topics": [...]}（agent 自行判斷選書）。"""
    import json
    try:
        w = json.load(open(WISHLIST)) or {}
        return w.get('topics', []) if isinstance(w, dict) else (w or [])
    except Exception:
        return []


CRAWL_PLAN = os.path.join(BP, 'reports', 'crawl_plan.json')


def _zlib_accounts_remaining() -> list[dict] | None:
    """各帳號今日剩餘額度 [{account, remaining}]；查不到回 None。"""
    try:
        out = subprocess.run(
            ['uv', 'run', '--with', 'requests', 'python', '-m',
             'book_pipeline.crawl_zlib', 'limits'],
            cwd=ROOT, capture_output=True, text=True, timeout=90)
        return (json.loads(out.stdout or '{}').get('accounts')) or None
    except Exception as e:
        log(f'crawl：查額度失敗 {e}')
        return None


def _read_crawl_plan() -> dict | None:
    try:
        return json.load(open(CRAWL_PLAN))
    except Exception:
        return None


def _fetch_book(b: dict) -> str | None:
    """確定性下載單本（planner 已選好 id/hash/slug，daemon 已指派 account）。
    回 slug=成功 / None=失敗。rc 0 且 raw_pdfs/<slug>.pdf 存在才算成功（不信 rc）。"""
    slug, bid, bhash = b.get('slug'), str(b.get('id', '')), str(b.get('hash', ''))
    if not (slug and bid and bhash):
        log(f'❌ crawl plan 條目缺欄位：{b}')
        return None
    cmd = ['uv', 'run', '--with', 'requests', '--with', 'playwright', 'python', '-m',
           'book_pipeline.crawl_zlib', 'fetch', bid, bhash, '--slug', slug]
    if b.get('account') is not None:
        cmd += ['--account', str(b['account'])]
    rc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).returncode
    if rc == 0 and os.path.isfile(os.path.join(ROOT, 'raw_pdfs', f'{slug}.pdf')):
        log(f'crawl ok：已補書 slug={slug}（acct {b.get("account")}）')
        return slug
    log(f'❌ crawl fetch 失敗 slug={slug} rc={rc}')
    return None


def do_crawl_parallel(dry: bool) -> list[str]:
    """wishlist 驅動爬書：1 隻 planner agent（LLM）選 K 本互異缺口 → daemon 依各帳號餘額
    預先指派 account → **並行**確定性下載（CRAWL_PARALLEL）。回成功補到的 slug 清單。

    第一性原理：只有『選哪本』需判斷（LLM、共享 inventory→須單一決策避免碰撞）；『下載』
    是確定性的（fetch id hash --slug），可並行。故拆開：K 隻序列 LLM agent → 1 隻 planner
    + K 個並行下載。帳號由 daemon 指派（非 agent 自選）→ 多本不會搶同一帳號／超額。"""
    topics = _wishlist_pending()
    if not topics:
        return []
    if dry:
        log(f'crawl plan：wishlist 有 {len(topics)} 主題（真跑時派 planner 選書 → 並行下載）')
        dispatch_llm('crawl_plan', None, dry=True)
        return []
    accts = _zlib_accounts_remaining()
    if accts is None:
        log('crawl skip：查額度失敗，本 tick 不派 crawl')
        return []
    slots = [a['account'] for a in accts for _ in range(max(0, a.get('remaining') or 0))]
    if not slots:
        log('crawl defer：今日所有帳號額度耗盡 → 明日')
        return []
    try:
        os.remove(CRAWL_PLAN)
    except FileNotFoundError:
        pass
    log(f'crawl plan dispatch：wishlist {len(topics)} 主題，總額度剩 {len(slots)}')
    dispatch_llm('crawl_plan', None, dry=False)
    plan = _read_crawl_plan()
    if not plan:
        log('❌ crawl plan 空：planner 未產 crawl_plan.json（崩潰／逾時？）')
        return []
    books = plan.get('books') or []
    if not books:
        log(f'crawl 收斂：{plan.get("reason", "無更多合格經典缺口")}（正常）')
        return []
    # 去重（slug + id）+ 依帳號餘額容量截斷 + 預先指派 account（零碰撞）
    seen, uniq = set(), []
    for b in books:
        kdup = (b.get('slug'), str(b.get('id')))
        if not b.get('slug') or kdup in seen:
            continue
        seen.add(kdup)
        uniq.append(b)
    uniq = uniq[:len(slots)]
    for i, b in enumerate(uniq):
        b['account'] = slots[i]
    log(f'crawl fan-out：計畫 {len(books)} 本 → 去重後 {len(uniq)} 本並行下載（配額容 {len(slots)}）')
    crawled: list[str] = []
    with ThreadPoolExecutor(max_workers=min(CRAWL_PARALLEL, len(uniq))) as ex:
        futs = {ex.submit(_fetch_book, b): b for b in uniq}
        for f in cf.as_completed(futs):
            try:
                s = f.result()
                if s:
                    crawled.append(s)
            except Exception as e:
                log(f'❌ crawl fetch {futs[f].get("slug")} 異常：{e}')
    log(f'crawl done：成功下載 {len(crawled)}/{len(uniq)} 本')
    return crawled


def do_submit(slug: str, dry: bool) -> int:
    """async 提交一本待 ingest 書到 MinerU（切+傳，**不等 OCR**）。進 in-flight，OCR
    並行於雲端排隊跑，由 do_harvest 收割。MinerU 無每日硬上限（超高優先配額僅降速），
    故不闸门、一律提交（pick_account 只做帳號負載均衡）。"""
    if slug in mb.occupied():
        return 0  # 上傳中/就緒，等 harvest，不重提
    pages = mb.estimate_pages(slug) or 200
    acct = mb.pick_account(pages)
    log(f'ingest submit {slug}：{pages} 頁 → 帳號{mb._account_num(acct)}（detached upload，不等）')
    if dry:
        return 0
    mb.record_start(slug, acct, pages)
    mb.mark_submitting(slug, acct, pages)  # 先佔位防空窗重提
    return mb.submit_ingest(slug, acct)


def do_harvest(slug: str, dry: bool) -> int:
    """poll 收割 in-flight 書的 OCR → download+assemble→unified。OCR 早已並行於雲端，
    這裡只把就緒 chunk 收回組裝。rc 0=組好可 parse / 3=部分仍 OCR 中（留 in-flight 下個
    tick 再收）/ 1=無 manifest。"""
    if dry:
        log(f'DRY ingest harvest {slug}（poll OCR → 收割）')
        return 0
    log(f'ingest harvest {slug}：poll OCR batch → 收割就緒 chunk')
    rc = mb.harvest_ingest(slug)
    if rc == 0:
        log(f'ingest harvest {slug} ✓：unified 組好，可 parse')
    elif rc == 3:
        log(f'ingest harvest {slug}：部分 chunk 仍 OCR 中 → 留 in-flight，下個 tick 再收')
    elif rc != 0:
        log(f'❌ ingest harvest {slug}：rc={rc}')
    return rc


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
    if dry:
        return 0
    rc = subprocess.run(build, cwd=READER_ROOT).returncode
    # 只在 build 成功且 book.json 真的烤出才標已部署；否則留待下個 tick 重試（不誤標 done）。
    book_json = os.path.join(READER_ROOT, 'data', slug, 'book.json')
    if rc == 0 and os.path.isfile(book_json):
        q.mark_deployed(slug)
        log(f'deploy {slug} ✓：book.json 已烤出，上站')
    else:
        log(f'❌ deploy {slug}：build rc={rc}，book.json={"有" if os.path.isfile(book_json) else "無"} → 不標 deployed，下個 tick 重試')
    return rc


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
        log(f'catalog_repair {slug}：critical {before}→{after}（確定性已盡，殘餘交 LLM）')
    return after  # 回殘留 critical 數（0=過關）


def _catalog_critical(slug: str) -> int:
    from book_pipeline.catalog_audit import audit_catalog
    return audit_catalog(slug, write_report=False).get('critical') or 0


def do_catalog_resolve(slug: str, dry: bool) -> int:
    """catalog 三層收斂，保證**不 forever-stall**：
      1) 確定性 repair 三件套（清掉大宗）。殘留 0 → 過關。
      2) 殘留 >0 且未派過 LLM → 派 LLM catalog-audit（產 catalog_overrides：pdf_crop 救圖
         / alias / 修 ref），apply 後重審。清 0 → 過關。
      3) LLM 已派過仍殘留（多為 MinerU 源頭缺、無法憑空生）→ mark_catalog_accepted →
         assess 不再 gate，書照常 deploy（殘留 surface 不阻塞）。
    回 0=已收斂（過關或 accept，可續 deploy）/ -2=LLM 撞 session 限額（defer 下個 tick）。"""
    residual = do_catalog_repair(slug, dry)
    if residual == 0:
        return 0
    if dry:
        log(f'DRY catalog {slug}：殘留 {residual} → 真跑時派 LLM / accept')
        return 0
    if q.catalog_llm_done(slug):
        # 上個 tick 已派過 LLM、本 tick 確定性後仍殘留 → 終局 accept（不可修者）
        q.mark_catalog_accepted(slug, residual)
        log(f'catalog {slug}：LLM 修復後仍殘 {residual}（源頭缺不可修）→ accept，照常 deploy')
        return 0
    log(f'catalog {slug} → LLM 修復殘留 {residual}（產 overrides：pdf_crop/alias/修 ref）')
    rc = dispatch_llm('catalog_audit', slug, dry)
    if rc != 0:
        # session 限額(-2) 或 claude 出錯/timeout → defer 重試，不 mark_llm_done、不誤 accept
        log(f'catalog {slug}：LLM rc={rc} → defer，下個 tick 重派（不誤 accept）')
        return -2
    q.mark_catalog_llm_done(slug)
    after = _catalog_critical(slug)
    if after == 0:
        log(f'catalog {slug} ✓：LLM 修復後 critical→0，過關')
        return 0
    # LLM 已盡力仍殘留 → accept（保證 deploy，不卡）
    q.mark_catalog_accepted(slug, after)
    log(f'catalog {slug}：LLM 後殘 {after}（不可修）→ accept，照常 deploy')
    return 0


def advance_book(slug: str, dry: bool, no_deploy: bool, max_steps: int = 15) -> None:
    """縱向推進**一本書**：沿自己的 pipeline 盡可能往下跑（triage→qc→ingest→parse→
    audit→catalog→sol→deploy），**不等其他書**。每步後重新 assess（磁碟狀態會變）。

    ingest 是 async 斷點：走到 ingest 只 submit（不等 OCR），書進 in-flight 後停；OCR
    並行於雲端排隊跑，由 tick 的 harvest 階段統一收割 → 組好 unified 後（同 tick 收割階段
    後的 advance，或下個 tick）才續 parse→…→deploy。其餘停點：deploy=終點；done／可選
    translate／triage·qc 拒（R/X）→ 收工；同一 stage 連兩步沒前進 → 停（防失敗空轉）。
    """
    last_key = None
    for _ in range(max_steps):
        row = q.assess_one(slug)
        stage = row.get('stage', '') or ''
        todo = row.get('todo', '—')
        verb = todo.split('(')[0]
        # 無必要 work：done／可選 translate／triage·qc 拒（R）／無源（X）
        if todo in ('—', '') or todo.endswith('(可選)') or stage.startswith(('R', 'X')):
            return
        # 停滯鍵用 (stage, verb)：todo 在同 stage 內推進（如 catalog_audit→deploy 皆在 3）
        # 算前進、不誤判停滯；唯有同階段同動作連兩步沒變（修復沒清掉）才真停。
        key = (stage, verb)
        if key == last_key:
            log(f'advance {slug} 停滯於「{stage}/{verb}」（未前進）→ 停，待下個 tick 重試')
            return
        last_key = key

        if dry:
            log(f'DRY advance {slug}：下一步 {verb}（stage={stage}）')
            return
        if verb == 'ingest':
            # async 斷點：已 occupied（上傳中/就緒）→ 等 harvest，不重 submit；否則 detached 提交後停。
            if slug in mb.occupied():
                return
            do_submit(slug, dry)
            return
        if verb == 'deploy':
            do_deploy(slug, dry, no_deploy)
            return  # pipeline 終點
        if verb == 'parse':
            do_parse(slug, dry)
        elif verb == 'catalog_audit':
            rc = do_catalog_resolve(slug, dry)
            if rc == -2:  # LLM 撞 session 限額 → defer 本書，下個 tick 重試
                return
        elif row.get('llm') or verb in LLM_PROMPTS:
            log(f'advance {slug} → LLM {verb}')
            rc = dispatch_llm(verb if verb in LLM_PROMPTS else 'audit', slug, dry)
            if rc == -2:  # Claude session 限額 → defer 本書，等下個 tick reset（非「停滯」）
                return
        else:
            log(f'advance {slug} skip：未知確定性 todo={todo}')
            return
    log(f'advance {slug}：達 max_steps={max_steps} → 停（防失控）')


def _sorted_rows() -> list:
    """全書 queue，上游優先排序（先推接近 ingest 的書讓新書早上站；純體感不影響正確性）。"""
    rows = q.build_queue()
    order = {'0.2': 0, '0.3': 1, '0.5': 1, '1': 2, '2': 3, '3': 4, '4': 4}

    def _ord(r):
        parts = (r.get('stage') or '').split()
        return order.get(parts[0] if parts else '', 9)
    rows.sort(key=_ord)
    return rows


def _advance_parallel(slugs: list[str], dry: bool, no_deploy: bool) -> None:
    """並行縱向推進多本書（LLM 階段獨立無依賴 → 不設人為上限：0=並發到書數，全部同時）。
    一本炸不連坐（exception 收進 future，log ❌ 不傳播）。"""
    if not slugs:
        return
    if dry:
        for s in slugs:
            advance_book(s, dry, no_deploy)
        return
    with ThreadPoolExecutor(max_workers=LLM_PARALLEL or len(slugs)) as aex:
        futs = {aex.submit(advance_book, s, dry, no_deploy): s for s in slugs}
        for f in cf.as_completed(futs):
            if f.exception():
                log(f'❌ advance {futs[f]} 異常：{f.exception()}')


def _harvest_parallel(slugs: list[str], dry: bool) -> None:
    """並行收割多本 in-flight（poll OCR 是 IO bound → 並發到 INGEST_PARALLEL）。"""
    if dry or not slugs:
        return
    with ThreadPoolExecutor(max_workers=INGEST_PARALLEL) as hex_:
        futs = {hex_.submit(do_harvest, s, dry): s for s in slugs}
        for f in cf.as_completed(futs):
            if f.exception():
                log(f'❌ harvest {futs[f]} 異常：{f.exception()}')


def tick(dry: bool, max_llm: int, no_deploy: bool) -> int:
    log(f'=== tick start (dry={dry}) ===')
    log('budget: ' + str(mb.status_report()))
    _llm_exhausted.clear()  # 每 tick 重試一次 LLM（上個 tick 撞額度後可能已 reset）
    if not dry:
        wr.reset()  # 清空 worker 註冊表（含上次崩潰殘留），本 tick 新工人重新登記

    # A. 收割已就緒 in-flight（uploading=False）：OCR 早已並行於雲端，並行 poll 組 unified。
    _harvest_parallel(sorted(mb.harvestable()), dry)

    # B. 不同資源並行，不互堵：待ingest書 → detached 背景 upload（fire-and-forget，立刻返回、
    #    不被慢上傳堵住整 tick）；其餘書 → 主線程同時並行縱向 advance（LLM 與 upload 真並行）。
    rows = _sorted_rows()
    occ = mb.occupied()
    ingest_slugs = [r['slug'] for r in rows
                    if r['todo'].split('(')[0] == 'ingest' and r['slug'] not in occ]
    skip = set(ingest_slugs)
    for s in ingest_slugs:
        do_submit(s, dry)  # detached upload，立刻返回；早寫 manifest 防跨 tick 重提

    # advance 非待ingest書：LLM 階段（audit/qc/sol_extract）各書獨立無依賴 → 並行。
    _advance_parallel([r['slug'] for r in rows if r['slug'] not in skip], dry, no_deploy)

    # C. crawl 補新書：1 隻 planner 選 K 本互異缺口 → 並行下載當日全額度（3 帳號=30 本）→
    #    補到的書並行 advance（triage→qc→ingest async submit）。zlib 與 MinerU 獨立資源池、互不阻塞。
    crawled = do_crawl_parallel(dry)
    if crawled and not dry:
        _advance_parallel(crawled, dry, no_deploy)

    # D. 再收割一輪已就緒書（剛 crawl→submit 的多半還在上傳/OCR，主要收 A 階段後翻 ready 的）。
    if not dry:
        _harvest_parallel(sorted(mb.harvestable()), dry)
        # E. 對收割到 unified 的書再並行縱向推進（parse→audit→catalog→sol→deploy 一條龍）。
        _advance_parallel([r['slug'] for r in _sorted_rows()], dry, no_deploy)

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
