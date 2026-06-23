#!/usr/bin/env python3
"""book_pipeline.pipeline_tick — pipeline 自動化派工驅動。

術語（全 repo 統一，改碼前先認準 → tick ≠ cycle）：
  • tick  = 一次 controller invocation（粗粒度外圈）：launchd 觸發 → 跑到 walltime(≤50min)/
    idle 收斂/reload 才退 → launchd 重拉。`.tick.lock` 序列化單例、flock 防重入、起頭
    `wr.reset()`/清額度旗標、跨 tick 存活（in-flight OCR/manifest/lease）、snapshot
    `last_tick_*`/`next_tick_eta_s`/`in_tick_now` 皆以此「進程界」為單位。
  • cycle = reactive controller 內 observe→非阻塞派工→reap→sleep 一輪（細粒度內圈）：
    間隔 LOOP_POLL(75s) 或 worker 完成即時喚醒，一個 tick 內跑成百上千 cycle。
    **「下個 cycle 重試/再收/重派」＝下次 observe（≤75s）**，非等 controller 退出重拉。
  模型：reactive（生產預設 REACTIVE=1，長命 controller 跑 cycles）｜ one-shot
  （tick_once，dry-run/REACTIVE=0，一個 tick＝一趟線性掃完即退）。

職責：讀 pipeline_queue 全 stage 真相 → 推進。確定性階段 daemon 直跑，需判斷的
階段（crawl / qc / audit）派 headless `claude -p` 跑對應 skill/reference。

一個 tick（one-shot）／一輪 cycle（reactive）的骨架：
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
import contextlib
import fcntl
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from book_pipeline import pipeline_queue as q
from book_pipeline import status as st
from book_pipeline import mineru_budget as mb
from book_pipeline import worker_registry as wr
from book_pipeline import agent_history as hist
from book_pipeline import leases
from book_pipeline import extract_cover
from book_pipeline import booklists
from book_pipeline import scope_guard
from book_pipeline import pipeline_gates as pg
from book_pipeline.llm_policy import DispatchSpec, resolve_dispatch, math_sweep_model

ROOT = q.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
LOCK = os.path.join(BP, '.tick.lock')

# 閘門快照：reactive loop 每 cycle observe 起頭更新一次（單寫手=主執行緒），各 dispatch 點 + worker thread
# 的 advance_book 讀同一份（GIL 下 dict 參照讀寫原子）。tick_once 起頭亦設一次。其餘路徑（手動呼叫）
# fallback 即時 load。讀 holder 比每 verb 讀磁碟 cheap，又比「進 advance 凍結整本」新鮮（≤上 cycle）。
_GATES_HOLDER = {'g': None}


def _active_gates() -> dict:
    """當前閘門快照：reactive/tick_once 已設 holder → 回快照；否則 fallback 即時 load（fail-safe hold）。"""
    g = _GATES_HOLDER['g']
    return g if g is not None else pg.load_gates()
LOG = os.path.join(BP, 'reports', 'daemon.log')
CRAWL_LIVE_PATH = os.path.join(ROOT, 'dev', 'crawl_live.json')  # live 下載快訊（買書員逐本 下載中→✓/✗，繞 status.json）
READER_ROOT = q.READER_ROOT
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
# codex 派工後端：headless `codex exec --json`。兩條 codex provider：
#   codex      = 原生 OAuth（~/.codex/auth.json，codex login ChatGPT 訂閱）
#   codex-pool = ccNexus 池子（codex -p nexus + CCNEXUS_API_KEY；maxn970228 token 輪換、
#                與原生 codex 不同帳號＝獨立額度）。profile 在兩機 ~/.codex/nexus.config.toml。
# 模型/effort/chain/timeout 全收斂進「派工配置層」book_pipeline.llm_policy
# （DispatchSpec + DEFAULT_DISPATCH/STAGE_DISPATCH + resolve_dispatch），非散落於此。
CODEX_BIN = os.environ.get('CODEX_BIN', 'codex')
# headless LLM 派工的 wall-clock 上限（秒）。**預設 0 = 無限**（agent 跑多久就跑多久）。
# 當年設此上限是因主力曾是 kimi+claude-cli，會卡死自我空轉（重讀 content_list 卡 6.5h、燒
# token）；改用 codex 為主力後該病理消失，硬切上限只會誤殺真複雜的書。env 可重設一個正整數
# 臨時重新加上限（運維拉桿）；0/未設＝無限（→ timeout=None，p.wait 等到自然結束）。
LLM_TIMEOUT = int(os.environ.get('BOOK_PIPELINE_LLM_TIMEOUT', '0'))
# ingest async upload 的並行度：upload 是 IO bound（切片+PUT MinerU，~8min/本），多本
# 並行打滿上傳頻寬。manifest RMW 由 mineru_ingest 的 fcntl 鎖保護，並行安全。
INGEST_PARALLEL = int(os.environ.get('BOOK_PIPELINE_INGEST_PARALLEL', '4'))
# LLM 階段（audit/qc/sol_extract）並行度：各書 LLM 任務獨立無依賴 → 全並行。預設 0=不限
# （= 本輪待推進書數，全部同時跑）。經驗上 Claude 並發數 + 本機 RAM 都撞不到瓶頸
# （Max 限制是 token rolling window 非並發數）；真要壓低（debug）才設 env >0。
LLM_PARALLEL = int(os.environ.get('BOOK_PIPELINE_LLM_PARALLEL', '0'))
# 並行 crawl 下載度：買書員 select_next 取 K 本後，K 個確定性 `crawl_zlib fetch` 並行下載（IO bound，
# 40MB/本）。帳號由 daemon 依各帳號餘額預先指派（--account），不靠 agent 自選 → 零碰撞。
CRAWL_PARALLEL = int(os.environ.get('BOOK_PIPELINE_CRAWL_PARALLEL', '6'))
# pipeline 在飛上限（backpressure）：pipeline 內待消化（in-flight OCR + 未上站待辦）≥ 此就不再下載新書，
# 把爬速綁定消化速。2026-06 簡化後**唯一**爬書水位——買書員每 cycle 直接 select_next 取解析池待下載書、
# 並行抓，無購物清單 buffer（buffer 唯一不可推導的下載失敗計數已移 pipeline_state.json：見 q.crawl_fail_*）。
CRAWL_INFLIGHT_CAP = int(os.environ.get('BOOK_PIPELINE_CRAWL_INFLIGHT_CAP',
                                        os.environ.get('BOOK_PIPELINE_CRAWL_HIGH', '30')))
# 數學 sweep 每 cycle 上限 + 輪數 + 並發 worker：do_math_sweep 跑 `math_sweep batch --limit L --workers W
# --rounds 1`。8 worker 並發各打一批 LLM（每批 ≈3-5 分），limit=workers×n 餵滿全部 worker → 一 cycle
# 牆鐘 ≈ 單批時間就清掉 ~W×n 條（過去序列要 W 倍時間）。**完成即記 last_batch、occ 階梯下降、上站**。
# rounds=1 不在 cycle 內重試——失敗條下個 cycle re-list 自然重試。walltime 安全（並發不拉長單 cycle 牆鐘）。
MATH_BATCH_WORKERS = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_WORKERS', '8'))
MATH_BATCH_N = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_N', '40'))
MATH_BATCH_LIMIT = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_LIMIT',
                                      str(MATH_BATCH_WORKERS * MATH_BATCH_N)))  # 餵滿 8 worker
MATH_BATCH_ROUNDS = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_ROUNDS', '1'))
DATA_DIR = os.path.join(BP, 'mineru_data')
MAX_FETCH_FAILS = int(os.environ.get('BOOK_PIPELINE_MAX_FETCH_FAILS', '3'))  # 同本連續 fetch 失敗達此 → 排除出下載候選
# harvest poll 上限（秒）：OCR 全好的書第一次 poll 就秒收；沒好的書等到此上限就放棄、留
# in-flight 下個 cycle 再收。**短**值＝非阻塞（不等 OCR 跑完凍住 cycle）。OCR 是 async、
# 在 MinerU 雲端並行跑，daemon 只負責「收已就緒的」，不該在此空等。
HARVEST_MAX_WAIT = int(os.environ.get('BOOK_PIPELINE_HARVEST_MAX_WAIT', '90'))

# 數學式 corpus-level sweep（逐條 override 待辦，track-only，不 gate deploy）：收斂目標 = **真 0**。
# 棄舊棘輪門檻（max(100, 上次殘餘+GROWTH)——殘餘降到門檻下就永久停派、卡在非零）。新模型：只要
# **residual_unaccepted = corpus 殘餘 − 已 accept（不可渲染）> 0** 就持續派 sweep。agent 走 math_sweep
# 三工具（list 讀待辦 / batch 批量改寫 / fix 單條），每條單式 render 驗證即落地 override，人退出迴圈，
# 直到真 0 或全數 accept。**棄**舊「寫泛化規則→全 corpus gate（序列重渲染 30min+）」閉環——實測該 gate
# 塞不進 50min walltime 必 timeout、殘餘永卡非零；且 95% 殘餘是單發式、泛化規則零槓桿（規則路徑降為稀有手段）。
# 防 busy-loop 改靠 **fixpoint 偵測**（見 _sweep_decision）：上次 sweep 在相同 macros 下既沒降殘餘
# 也沒改書、且此後 corpus 殘餘未變 → 原地踏步 → 不派，等外部變化（新書部署 / agent 改碼換 macros）。

# === 反應式控制迴圈（BOOK_PIPELINE_REACTIVE=1 啟用；預設 0 = 現行單次 tick，行為一字不差）===
# 第一性原理：單一 controller 進程內跑「有界 observe→非阻塞派工→reap→harvest→sleep」迴圈，
# 把「三條件齊備（上游產物就緒 ∧ 資源可用 ∧ 無人在做）」的 transition 立刻派成 thread worker；
# worker 跑完釋放→下個 cycle 自動發現新開門工作。worker 仍是本進程子執行緒/子進程 →
# registry/exhaustion 沿用 in-memory 共享（零 refactor）；leases 只防「跨 invocation/crash 的
# orphan LLM 子進程」+ 統一 timeout-kill。達牆鐘上限即退出讓 launchd 重拉（crash-safe 邊界）。
REACTIVE = os.environ.get('BOOK_PIPELINE_REACTIVE', '0') == '1'
LOOP_WALLTIME = int(os.environ.get('BOOK_PIPELINE_LOOP_WALLTIME', '3000'))      # 50min 後退出重拉
LOOP_POLL = int(os.environ.get('BOOK_PIPELINE_LOOP_POLL', '75'))               # cycle 間隔（秒）
LOOP_IDLE_ROUNDS = int(os.environ.get('BOOK_PIPELINE_LOOP_IDLE_ROUNDS', '3'))  # 連續幾輪全無工作即收工退出
LOOP_CONCURRENCY = int(os.environ.get('BOOK_PIPELINE_LOOP_CONCURRENCY', '32')) # controller 內並行 worker 上限
DRAIN_BOUND = int(os.environ.get('BOOK_PIPELINE_DRAIN_BOUND', '600'))           # **只對純 thread worker（math sweep/det subprocess）**的排空上限秒，逾時 os._exit 逃生（防純 API thread 凍結/孤兒鎖）。可殺的子進程 agent 不受此限、無限等其自然收尾
# live reactive controller 的 statefile（JSON {pid, sha, started}）：loop 起頭寫、退出即刪。
#   pid → 外部送 SIGUSR1 喚醒（reload）；sha → 此 controller 載入的 git 版本，供
#   「daemon 跑的是哪版碼、離 HEAD 多遠」即時觀測（免上線後做 forensics）。per-machine、gitignore。
CONTROLLER_STATE = os.path.join(BP, '.controller.json')
# reload 請求 marker：`devctl reload` 丟它 + SIGUSR1 → loop **排空在飛 worker 後優雅退出**、launchd 載
# 入新碼（零浪費；對比 kick -k 硬殺跳過 finally 會棄工作）。SIGUSR1 語意＝「醒來看 reload marker / 重觀測」。
RELOAD_REQUEST = os.path.join(BP, 'reload_request')

# LLM 階段 → headless claude 任務描述（指向既有 skill/reference）。
# 註：crawl **不再派 LLM**——daemon 已降級為純收錄引擎（買書員 select_next 只取 QUALIFIED 確定性下載 →
# ingest→deploy→owned）。「填書單」（discovery + 四維查證 + 找連結 + 寫 QUALIFIED）改由使用者親打 /restock
# 在互動 session 親自 fan-out 驅動，daemon 不自主 resolve。故本表只剩 qc/audit/sol_extract 三個判斷階段。
LLM_PROMPTS = {
    'qc': (
        "對 slug={slug} 跑 `pdf_contactsheet {slug}`，看產出的 PNG，判斷書是否正確/清晰/完整/"
        "可供 MinerU OCR。結論呼叫 `python -m book_pipeline.pipeline_queue` 的 set_qc："
        "通過用 pass、不可用 reject。遵 .claude/skills/book-pipeline/references/qc.md。"),
    'audit': (
        "對 slug={slug} 執行 /book-pipeline 的 audit-book 流程"
        "（.claude/skills/book-pipeline/references/audit-book.md）：產 "
        "extract_rules.yaml → parser → smoke iterate。"),
    'sol_extract': (
        "對主書 slug={slug} 執行 audit-sol 流程"
        "（.claude/skills/book-pipeline/references/audit-sol.md）merge 解答書。"),
    'catalog_audit': (
        "對 slug={slug} 執行 catalog 修復流程，"
        "**嚴格遵照 .claude/skills/book-pipeline/references/catalog-audit.md** "
        "（含各 critical 類別的查證與修法、override action 語意、陷阱）：跑 audit_catalog 看殘留 "
        "→ 產 book_pipeline/catalog_overrides/{slug}.json → apply_catalog_overrides → 重審，"
        "把 critical 降到最低（多數可全清零）。真不可修者（源頭缺）列入 _catalog_audit.md 即可收工。"),
    # 註：math_sweep 已**不派 agent**（do_math_sweep 直跑 math_sweep batch 純 API）→ 此處無 math_sweep prompt。
}


_last_snap = 0.0
_snap_lock = threading.Lock()


def _refresh_snapshot() -> None:
    """事件驅動刷新 dev 監控快照：每個 log 事件順手重生 dev/status.json，節流 ~1s。
    **絕不在 _log_lock 內呼叫**：build_snapshot 重（評估全書 + 讀 pending/state）且會碰
    其他鎖 → 若持 _log_lock 跑它，會與『持他鎖又要 log』的 thread 反轉死鎖（並行下必現）。
    自帶 non-blocking _snap_lock：已有 thread 在刷就跳過，避免 N thread 同時 build_snapshot。
    節流 8s→1s：status.json 拆分後核已輕（逐出 per-book timeline/sessions + system 欄，
    write_snapshot 端到端 ~0.17s）→ 1s 重寫 duty ~17% 單核可接受，且核 1s 直驅看板（取代已退役
    的 stages.json fast-lane）→ 階段轉換 ≤1s 反映、不再需要繞道小檔。"""
    global _last_snap
    import time
    now = time.monotonic()
    if now - _last_snap < 1:
        return
    if not _snap_lock.acquire(blocking=False):
        return  # 別的 thread 正在刷 → 跳過本次（best-effort）
    try:
        _last_snap = now
        from book_pipeline.devctl import write_snapshot
        write_snapshot()  # write_timeline=False（預設）：controller 記憶體碼可能舊，只刷 live status.json、
                          # 不碰歷史時間軸；時間軸由 60s devsnapshot 單一寫手寫（防版本歪斜 churn）
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


# ── live 下載快訊（dev/crawl_live.json）──────────────────────────────────────────
# 買書員是同步 burst（一批並行 subprocess 下載 5–120s），刻意不註冊 worker_registry（非 LLM agent），
# 故 status.json 的 workers[] 全程空、crawl.queue 只是「下輪要抓的」→ /dev 完全看不出「正在下載」。
# 此檔補上唯一缺口：本批每本 下載中→✓/✗ 的逐本 live 狀態，前端以 ~2s cadence 直撿（繞 status.json 8s）。
# controller 是唯一寫手；前端＋devctl crawl_status 用 updated_at 守新鮮（dead tick 的殘檔自動視為過期）。
_crawl_live: dict = {}
_crawl_live_lock = threading.Lock()


def _write_crawl_live() -> None:
    """把 in-memory live 下載狀態原子寫出（持鎖內組 snapshot、鎖外寫檔，前端永不讀到半截）。"""
    with _crawl_live_lock:
        if not _crawl_live:
            return
        snap = dict(_crawl_live)
        snap['updated_at'] = time.time()
        snap['books'] = [dict(b) for b in _crawl_live.get('books', [])]
        snap['active'] = any(b.get('state') == 'downloading' for b in snap['books'])
    try:
        os.makedirs(os.path.dirname(CRAWL_LIVE_PATH), exist_ok=True)
        tmp = CRAWL_LIVE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, CRAWL_LIVE_PATH)
    except Exception:
        pass


def publish_crawl_live(batch: list[dict]) -> None:
    """買書員開抓一批時發佈：全本標 downloading，title/cover 由 resolution sidecar enrich。"""
    try:
        res = booklists.load_resolution()
    except Exception:
        res = {}
    with _crawl_live_lock:
        _crawl_live.clear()
        _crawl_live.update({
            'started_at': time.time(),
            'accounts': sorted({b.get('account') for b in batch if b.get('account') is not None}),
            'books': [{
                'slug': b['slug'],
                'title': res.get(b['slug'], {}).get('title') or b.get('title') or b['slug'],
                'cover': res.get(b['slug'], {}).get('cover', ''),
                'is_sol': b['slug'].endswith('_sol'),
                'account': b.get('account'),
                'state': 'downloading',
                'mb': None,
            } for b in batch],
        })
    _write_crawl_live()


def update_crawl_live(slug: str, state: str, mb: float | None = None) -> None:
    """單本下載落地：標 done/failed（+MB），原子重寫。前端 ≤2s 撿出 → 卡牌脈動轉 ✓/✗。"""
    with _crawl_live_lock:
        for b in _crawl_live.get('books', []):
            if b['slug'] == slug:
                b['state'] = state
                if mb is not None:
                    b['mb'] = round(mb, 1)
                break
        else:
            return
    _write_crawl_live()


def end_crawl_live() -> None:
    """整批收尾：標 ended_at（active 轉 false）。read_crawl_live 用它做 tail 寬限後自動隱藏。"""
    with _crawl_live_lock:
        if not _crawl_live:
            return
        _crawl_live['ended_at'] = time.time()
    _write_crawl_live()


def read_crawl_live() -> dict | None:
    """讀 dev/crawl_live.json（devctl snapshot 用，跨進程）。dead tick 殘檔（updated_at > 10min）視為過期回 None。"""
    try:
        d = json.load(open(CRAWL_LIVE_PATH, encoding='utf-8'))
    except Exception:
        return None
    if time.time() - (d.get('updated_at') or 0) > 600:
        return None
    return d


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
        log(f'⏱ TIMEOUT {timeout}s → 殺子工 process group（pid={p.pid}）；下個 cycle 將自動重派')
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            time.sleep(5)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return -1


def _is_codex(provider: str) -> bool:
    """codex 家族（原生 OAuth + 池子）：共用 codex CLI 命令骨架、event schema、限額 markers。"""
    return provider in ('codex', 'codex-pool')


# ── 派工配置層（單一真相源）────────────────────────────────────────────────
# 配置定義（DispatchSpec / DEFAULT_DISPATCH / STAGE_DISPATCH / 三層合併）已抽至
# book_pipeline.llm_policy（單一真相源，跨 pipeline_tick 與 math_sweep 共用）。此處只留
# 本地包裝 _resolve_dispatch（注入本模組 log 當警告通道），及下游 CLI-cmd 消費（_build_llm_cmd）。
def _resolve_dispatch(verb: str) -> DispatchSpec:
    """llm_policy.resolve_dispatch 的本地包裝：注入 pipeline_tick 的 log 當未知 provider 警告通道。"""
    return resolve_dispatch(verb, log=log)


def _llm_env(provider: str) -> dict | None:
    """指定 provider 的派工環境。codex-pool → 注入 dummy CCNEXUS_API_KEY（codex 要求 env_key
    非空才肯走 nexus profile）。claude/codex → None（claude 沿用 Claude Max 訂閱；codex 用
    自己的 ~/.codex/auth.json）。"""
    if provider == 'codex-pool':
        env = dict(os.environ)
        env.setdefault('CCNEXUS_API_KEY', 'unused')
        return env
    return None


# 撞額度的 provider（本 tick 內標記，跨並行子工共享 → 不再重複撞同一耗盡的 provider）。
# 每 tick 開頭清空重試。
_exhausted_providers: set[str] = set()
_exhausted_lock = threading.Lock()
# claude 撞 5h 滾動窗會吐這些字串、秒退；codex 撞 ChatGPT 訂閱限額吐 rate/quota 類訊息。
SESSION_LIMIT_MARKERS = ('session limit', 'hit your session', 'usage limit')
CODEX_LIMIT_MARKERS = ('rate limit', 'usage limit', 'quota', 'too many requests',
                       'insufficient_quota', 'exceeded your current',
                       'status":429', 'status": 429', 'resource_exhausted')


def _hit_limit(provider: str, out: str) -> bool:
    """從合併輸出判斷是否撞該 provider 的額度限制。"""
    markers = CODEX_LIMIT_MARKERS if _is_codex(provider) else SESSION_LIMIT_MARKERS
    return any(m in out for m in markers)


# 服務中斷標記（provider/基礎設施掛了，非任務失敗、非額度）。失敗且零事件已是最強中斷訊號
# （見 _run_one）；這些標記補「漏了幾個事件才斷線」的情況。5xx/連線/串流斷皆屬之。
OUTAGE_MARKERS = ('service unavailable', 'status":503', 'status": 503', 'status":502',
                  'status": 502', 'status":500', 'status": 500', 'bad gateway', 'gateway timeout',
                  'connection refused', 'connection reset', 'connection error', 'econnrefused',
                  'stream disconnected', 'stream error', 'network error', 'failed to connect',
                  'temporarily unavailable', 'upstream connect error')


def _hit_outage(out: str) -> bool:
    """從合併輸出判斷是否為 provider/基礎設施服務中斷（非任務失敗、非額度）→ 該 failover。"""
    return any(m in out for m in OUTAGE_MARKERS)


def _event_error_text(provider: str, ev: dict) -> str:
    """只從『終端錯誤事件』抽出可供限額判定的文字；正常 agent 訊息/工具指令一律回 ''。
    （絕不能掃整段 transcript：被 audit 的書內容與工人指令本身常含 quota/429/rate limit，
    會把成功的派工誤判成撞額度。歷史 bug：codex 額度滿卻連環「撞額度」failover。）"""
    t = ev.get('type') or ''
    if _is_codex(provider):
        # 三條錯誤通道（實測 codex exec --json）：頂層 error 事件、turn.failed、error item。
        # 真 429 的 message 內嵌 JSON：{"type":"error","status":429,"error":{...}}
        if t == 'error':
            return str(ev.get('message') or json.dumps(ev, ensure_ascii=False))
        if t == 'turn.failed':
            err = ev.get('error')
            return json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(err or '')
        if t == 'item.completed':
            item = ev.get('item') or {}
            if item.get('type') == 'error':
                return str(item.get('message') or item.get('text') or json.dumps(item, ensure_ascii=False))
        return ''
    # claude stream-json：終端 result 事件帶 is_error / 非 success subtype 才算錯誤面
    if t == 'result' and (ev.get('is_error') or ev.get('subtype') not in (None, 'success')):
        return str(ev.get('result') or ev.get('error') or json.dumps(ev, ensure_ascii=False))
    return ''


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


def _codex_model(spec: DispatchSpec) -> str:
    """codex 家族（含 codex-pool）模型：統一取 spec.codex_model（resolved spec 恒有值；
    防禦性 fallback gpt-5.4）。pool 與原生 codex 後端白名單相同，不分開持有模型。"""
    return spec.codex_model or 'gpt-5.4'


def _build_llm_cmd(provider: str, prompt: str, spec: DispatchSpec) -> list[str]:
    """依 provider + 解析後的 DispatchSpec 組 headless 派工命令。
    claude：claude CLI 走 stream-json，可由 spec.claude_model 帶 --model（Claude Max 訂閱）。
    codex/codex-pool：`codex exec --json`，沙箱 danger-full-access 對齊 claude -p 的全權（daemon
    信任環境，audit/repair 要寫 mineru_data、跑 uv），--model 取 spec.codex_model；spec.codex_effort
    非空帶 -c model_reasoning_effort；codex-pool 額外帶 `-p nexus`（走 ccNexus 池子 profile）。
    ⚠ 全權沙箱為【已審視的接受風險】：daemon 本質需 fs-write+exec 才能產書，收緊即失能。
    對應緩解——注入面 slug 已白名單化（_fetch_book / crawl_zlib，[a-z0-9_]{1,64}）、
    不可信的 OCR 產物在 bake 邊界消毒（nh3 表格 + marked raw-HTML 轉義）。勿擅自收緊。"""
    if _is_codex(provider):
        cmd = [CODEX_BIN, 'exec', '--json', '--skip-git-repo-check',
               '-C', ROOT, '--sandbox', 'danger-full-access',
               '--model', _codex_model(spec)]
        if spec.codex_effort:
            # codex 的 TOML config override（值需引號才當字串）；只 codex 家族有此旋鈕
            cmd += ['-c', f'model_reasoning_effort="{spec.codex_effort}"']
        if provider == 'codex-pool':
            cmd += ['-p', 'nexus']
        cmd.append(prompt)
        return cmd
    cmd = [CLAUDE_BIN, '-p', prompt, '--add-dir', ROOT,
           '--output-format', 'stream-json', '--verbose']
    if provider == 'claude' and spec.claude_model:
        cmd += ['--model', spec.claude_model]
    return cmd


def _emit(wkey: str, kind: str, label: str, tag: str) -> None:
    """單一事件 → live 面板（wr，截字/節流）+ 完整歷程（hist，原文不截）+ stdout 回顯。
    新增事件來源只改這一處，免「wr/hist 雙呼叫漏改」。kind='tool'|'text'。"""
    wr.event(wkey, kind, label)
    hist.event(wkey, kind, label)
    sys.stdout.write(f'[{tag}] {"🔧" if kind == "tool" else "💬"} {label[:160]}\n')


def _pump_event(provider: str, ev: dict, wkey: str, tag: str) -> None:
    """單一 JSONL 事件 → 事件匯流（_emit）。claude 與 codex schema 不同，各自解。"""
    if _is_codex(provider):
        t = ev.get('type')
        item = ev.get('item') or {}
        it = item.get('type')
        # 工具調用：item.started 時記一次（避免 started+completed 重複計數）
        if t == 'item.started' and it and it != 'agent_message':
            cmd = item.get('command') or item.get('path') or item.get('name') or it
            lbl = f'{it}: {cmd}' if it != 'command_execution' else f'shell: {cmd}'
            _emit(wkey, 'tool', lbl, tag)
        elif t == 'item.completed' and it == 'agent_message':
            txt = (item.get('text') or '').strip()
            if txt:
                _emit(wkey, 'text', txt, tag)
        return
    # claude stream-json
    if ev.get('type') != 'assistant':
        return
    for blk in (ev.get('message', {}).get('content') or []):
        bt = blk.get('type')
        if bt == 'tool_use':
            _emit(wkey, 'tool', _tool_label(blk), tag)
        elif bt == 'text':
            txt = (blk.get('text') or '').strip()
            if txt:
                _emit(wkey, 'text', txt, tag)


# ── 終止安全：在飛 LLM 子進程登記表（pid → pgid）。SIGTERM/SIGINT（部署 kickstart -k / Ctrl-C）
# 時主動快殺整組 → _run_one 的 finally（hist.finish/leases.release/wr.unregister）秒級跑完 →
# 不留「未收尾」幽靈、不丟記錄、退出遠早於 launchd ExitTimeOut 升級 SIGKILL。子進程
# start_new_session ⇒ 自成 process group，pgid==pid。
_inflight_children: dict[int, int] = {}
_inflight_lock = threading.Lock()


def _register_child(pid: int, pgid: int) -> None:
    with _inflight_lock:
        _inflight_children[pid] = pgid


def _unregister_child(pid: int) -> None:
    with _inflight_lock:
        _inflight_children.pop(pid, None)


def _kill_inflight_children() -> int:
    """快殺所有在飛 LLM 子進程組（SIGKILL）。signal-handler 安全：只做 GIL 原子快照讀（不取
    _inflight_lock，免與持鎖的 worker thread 死鎖）+ os.killpg 系統呼叫。回殺掉的組數。"""
    import signal as _sig
    pgids = list(_inflight_children.values())  # GIL 下 list(dict.values()) 原子快照
    for pgid in pgids:
        try:
            os.killpg(pgid, _sig.SIGKILL)
        except OSError:
            pass
    return len(pgids)


def _install_term_handlers(wake, terminating) -> bool:
    """安裝 SIGTERM/SIGINT handler（部署 kickstart -k 的 SIGTERM、Ctrl-C）。收訊號即：① 主動快殺在飛
    LLM 子工 → 其 _run_one finally（hist.finish/leases.release）秒級跑完、不留未收尾幽靈；② 設 terminating
    旗標 + wake 喚醒 → loop 跳出、finally 的 ex.shutdown(wait=True) 立即完成、優雅退出（launchd 重拉新碼）。
    即使訊號在已進入 ex.shutdown 死等時才到，先殺子工亦能解開 worker 的 p.wait（CPython 主執行緒的
    join/lock 等待可被訊號中斷以執行本 handler，返回後 join 即完成）。須在主執行緒裝；非主執行緒/平台
    不支援 → 回 False（呼叫端降級，不報錯）。"""
    import signal

    def _handle_term(signum, frame):
        terminating.set()
        n = _kill_inflight_children()
        wake.set()
        log(f'🛑 收到 signal {signum} → 快殺 {n} 個在飛 LLM 子工、排空退出（launchd 重拉新碼）')
    try:
        signal.signal(signal.SIGTERM, _handle_term)
        signal.signal(signal.SIGINT, _handle_term)
        return True
    except (ValueError, OSError):
        return False


def _display_model(provider: str, spec: DispatchSpec) -> str:
    """歷程/面板顯示的模型 label：codex 家族＝實際模型（附 effort），claude＝自訂模型，
    其餘＝provider 名（防禦性 fallthrough）。"""
    if _is_codex(provider):
        m = _codex_model(spec)
        return f'{m}/{spec.codex_effort}' if spec.codex_effort else m
    if provider == 'claude' and spec.claude_model:
        return spec.claude_model
    return provider


def _run_one(provider: str, todo_verb: str, slug: str | None,
             prompt: str, spec: DispatchSpec) -> tuple[int, str | None]:
    """用單一 provider + 解析後 spec 跑一次派工。回 (rc, failover_reason)。reason ∈ {None, 'limit',
    'outage'}：None=成功或「agent 真跑了卻任務失敗」（不換 provider）；'limit'=撞額度；'outage'=服務中斷
    （零事件 / 5xx / 連線錯——外部掛了）。後二者呼叫端換鏈上下一 provider 重跑同一任務。timeout→(-1, None)
    （逾時自有 kill+下個 cycle 重派處理，不在此 failover）。slug 此處即 dispatch_llm 傳入的識別/lease 鍵。"""
    import signal
    import time
    cmd = _build_llm_cmd(provider, prompt, spec)
    log(f'RUN llm {todo_verb} {slug or ""}（{provider}/JSONL）')
    # 引擎源碼面守衛 bracket：spawn 前拍受保護檔指紋（含架構師既有未提交改動）→ finally 收尾比對，
    # 只有「此 worker 存活期間新變動的受保護檔」歸給它（捕 engine/patch 提案 [+enforce 還原]）。
    sg_pre = scope_guard.snapshot()
    p = subprocess.Popen(cmd, cwd=ROOT, env=_llm_env(provider), start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    err_parts: list[str] = []  # 只裝『終端錯誤事件 + 非 JSON(CLI/stderr) 行』供限額/中斷判定
    n_events = [0]             # 本次 provider 產出的 JSONL 事件數；rc≠0 且 ==0 = provider 從未接上 = 中斷
    tag = slug or todo_verb
    wkey = f'{slug or todo_verb}:{p.pid}'
    wr.register(wkey, slug, todo_verb, p.pid, provider)
    hist.start(wkey, slug, todo_verb, p.pid, provider,
               _display_model(provider, spec))
    result_rc = -1  # finally 用：timeout 路徑直接 return -1 不設 rc，故先給地板值
    timeout = spec.timeout or LLM_TIMEOUT or None  # None ⇒ p.wait 無限等、不殺（預設）
    # 租約包住實際 LLM 子進程：reactive loop 用它防「跨 controller crash 的 orphan 子進程」
    # 被重派/續殺（pid=真子進程、killable）。one-shot 模式下亦無害（tick 內 acquire→release）。
    leases.acquire(todo_verb, slug, p.pid, timeout)
    _register_child(p.pid, p.pid)  # start_new_session ⇒ pgid==pid；SIGTERM 時整組可被快殺

    def _pump():
        for line in p.stdout:  # type: ignore[union-attr]
            s = line.strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except Exception:
                # 非 JSON＝CLI/stderr 原生錯誤（額度/認證/crash）→ 納入錯誤判定面
                err_parts.append(s)
                continue
            n_events[0] += 1  # 成功 parse 的事件 → provider 確實接上並產出（zero=從未接上=中斷）
            _pump_event(provider, ev, wkey, tag)
            et = _event_error_text(provider, ev)
            if et:
                err_parts.append(et)
    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    try:
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f'⏱ TIMEOUT {timeout}s → 殺子工 process group（pid={p.pid}）；下個 cycle 重派')
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                time.sleep(5)
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            t.join(timeout=5)  # 子進程已死 → stdout 關閉、_pump 收尾；先排空再讓 finally finish，
            return -1, None    # 否則被 kill session 的尾段事件（hist.event）會 pop 後遭丟棄
        t.join(timeout=5)
        err = '\n'.join(err_parts).lower()
        result_rc = rc
        # rc==0（成功跑完）→ 不 failover。失敗才分類：額度 > 中斷（零事件=provider 從未接上，或 5xx/連線
        # 錯）> 任務失敗（有事件、agent 真跑了卻 rc≠0 → 換 provider 無益，回 None 交呼叫端）。
        if rc == 0:
            return rc, None
        if _hit_limit(provider, err):
            return rc, 'limit'
        if n_events[0] == 0 or _hit_outage(err):
            return rc, 'outage'
        return rc, None
    finally:
        _unregister_child(p.pid)  # 先撤登記，免 handler 對已收尾 pid 空殺（無害但乾淨）
        hist.finish(wkey, result_rc)  # 失敗/timeout/撞額度/被終止的 session 也記（rc!=0, ok=False）
        leases.release(todo_verb, slug)
        wr.unregister(wkey)
        try:  # 守衛收尾：此 worker 是否擅改了受保護程式碼面（見 scope_guard）
            scope_guard.check_worker(sg_pre, verb=todo_verb, slug=slug, session=wkey, log=log)
        except Exception as e:
            log(f'scope_guard 異常（不影響派工）：{e}')


def dispatch_llm(todo_verb: str, slug: str | None, dry: bool) -> int:
    """派 headless LLM 跑階段，沿 provider 鏈 failover。回 rc；-2 = 全鏈不可用 → 呼叫端 defer。
    派工策略（chain/model/effort/timeout）由 _resolve_dispatch(verb) 解析（DEFAULT←STAGE←env）。
    **failover 觸發二類**（_run_one 回 reason）：① 撞額度（limit）② 服務中斷（outage：provider 零事件
    /5xx/連線錯——外部掛了，非任務失敗）。兩者皆標記 provider 本 tick exhausted（並行子工不再重撞死池）、
    換下一個 provider **重跑同一任務**（不浪費派工）；全鏈耗盡才 -2。**「有事件但 rc≠0」= agent 真跑了卻
    任務失敗 → 不 failover**（換 provider 無益且恐雙寫），rc 交回呼叫端。
    （本函式服務 qc/audit/sol_extract；crawl 不再派 LLM——買書員確定性下載 + 填書單改人工 /restock。）"""
    prompt = LLM_PROMPTS[todo_verb].format(slug=slug or '')
    key = slug  # lease/registry/hist 身分鍵 = slug
    spec = _resolve_dispatch(todo_verb)
    chain = list(spec.chain or ())
    if dry:
        log('DRY ' + ' '.join(shlex.quote(c) for c in _build_llm_cmd(chain[0], prompt, spec)))
        return 0
    tried = []
    for provider in chain:
        with _exhausted_lock:
            if provider in _exhausted_providers:
                continue
        tried.append(provider)
        rc, reason = _run_one(provider, todo_verb, key, prompt, spec)
        if not reason:
            return rc  # 成功，或 agent 真跑了卻任務失敗 → 交回呼叫端，不換 provider
        with _exhausted_lock:
            _exhausted_providers.add(provider)  # 額度/中斷皆標死本 tick：免其他子工重撞同一掛掉的 provider
        nxt = next((q for q in chain if q != provider
                    and q not in _exhausted_providers), None)
        why = '撞額度' if reason == 'limit' else '服務中斷'
        log(f'⚠ {provider} {why}（{todo_verb} {key or ""}）→ '
            + (f'串接 {nxt} 重跑' if nxt else '鏈上無可用 provider'))
    log(f'❌ 全 provider 不可用 {chain}（試過 {tried}）→ defer {todo_verb} {key or ""}，下個 cycle 重試')
    return -2


def probe_provider(provider: str, spec: DispatchSpec, timeout: int = 90) -> dict:
    """主動探一個 provider 能否跑：用**真實派工命令**（_build_llm_cmd + _llm_env，同沙箱/profile）發一個
    trivial 任務，回 {provider, up, latency_s, detail}。鏡像 _run_one 的成功/失敗分類（rc==0 且有 agent
    訊息=up；limit/outage/零事件/timeout=down），但**不碰 worker_registry/leases/scope_guard/hist**
    （純探針、零副作用、不寫任何狀態），亦**忽略 _exhausted_providers**（探真實當下，非 tick-local 標記）。
    供 devctl probe 按需診斷 provider 健康，取代「派真工看它 defer」的事後被動驗證（G1）。
    **有成本**（一次 trivial LLM 調用，~10–40s），勿放 status 熱路徑。"""
    import time
    prompt = '回覆一個字：pong。不要使用任何工具。'
    cmd = _build_llm_cmd(provider, prompt, spec)
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=_llm_env(provider), text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'provider': provider, 'up': False,
                'latency_s': round(time.monotonic() - t0, 1), 'detail': f'timeout >{timeout}s'}
    el = round(time.monotonic() - t0, 1)
    n_events, got_msg, err_parts = 0, False, []  # type: ignore[var-annotated]
    for line in (p.stdout or '').splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
        except Exception:
            err_parts.append(s)            # 非 JSON = CLI/stderr 原生錯誤（額度/認證/crash）
            continue
        n_events += 1
        if et := _event_error_text(provider, ev):
            err_parts.append(et)
        if _is_codex(provider):
            if ev.get('type') == 'item.completed' and (ev.get('item') or {}).get('type') == 'agent_message':
                got_msg = True
        elif ev.get('type') == 'assistant':
            got_msg = True
    err = '\n'.join(err_parts).lower()
    if p.returncode == 0 and got_msg:
        return {'provider': provider, 'up': True, 'latency_s': el, 'detail': 'ok'}
    if _hit_limit(provider, err):
        return {'provider': provider, 'up': False, 'latency_s': el, 'detail': '撞額度（limit）'}
    if n_events == 0 or _hit_outage(err):
        tail = err.replace('\n', ' ')[-120:] or '零事件（provider 從未接上）'
        return {'provider': provider, 'up': False, 'latency_s': el, 'detail': f'服務中斷（outage）：{tail}'}
    return {'provider': provider, 'up': False, 'latency_s': el,
            'detail': f'rc={p.returncode}（有事件卻失敗）'}


def probe_chain(timeout: int = 90) -> list[dict]:
    """對**生效 chain**（resolve_dispatch 解出，含 runtime override / env）每個 provider 依序探針。
    回 [{provider, up, latency_s, detail}]，順序＝failover 順序 → 一眼看出「派工會先打誰、它活著嗎」。"""
    spec = _resolve_dispatch('_probe')
    return [probe_provider(pv, spec, timeout) for pv in (spec.chain or ())]


def _zlib_accounts_remaining() -> list[dict] | None:
    """各帳號今日剩餘額度 [{account, remaining}]；查不到回 None。**權威 live 查**（繞快取）。
    查到即回寫 dev/zlib_quota.json → gate 與 /dev 顯示即時反映恢復（免等 300s TTL）；確認 0 時
    寫 fresh-0 也順手 throttle 下次 re-probe（_zlib_remaining_cached 信任 fresh-0 達 _ZLIB_GATE_ZERO_TTL）。"""
    try:
        out = subprocess.run(
            ['uv', 'run', '--with', 'requests', 'python', '-m',
             'book_pipeline.crawl_zlib', 'limits'],
            cwd=ROOT, capture_output=True, text=True, timeout=90)
        accts = (json.loads(out.stdout or '{}').get('accounts')) or None
        if accts:
            try:
                from book_pipeline import devctl
                devctl.write_zlib_cache(accts)  # 權威結果回寫共用快取（顯示同步＋自動 throttle re-probe）
            except Exception:
                pass
        return accts
    except Exception as e:
        log(f'crawl：查額度失敗 {e}')
        return None


def _code_version() -> str:
    """本 controller 載入的 git 版本（short SHA）；git 不可用 → '?'。"""
    try:
        r = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or '?'
    except Exception:
        return '?'


def _write_controller_state() -> None:
    try:
        with open(CONTROLLER_STATE, 'w') as f:
            json.dump({'pid': os.getpid(), 'sha': _code_version(), 'started': time.time()}, f)
    except OSError:
        pass


def _clear_controller_state() -> None:
    try:
        os.remove(CONTROLLER_STATE)
    except OSError:
        pass


def controller_info() -> dict | None:
    """live reactive controller 狀態 {pid, sha, started}（statefile + 探活）；無檔/進程已死 → None。"""
    try:
        st = json.load(open(CONTROLLER_STATE))
        pid = int(st['pid'])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # 活著但無權 signal（同 user 不會發生）→ 仍視為活
    return st


def controller_pid() -> int | None:
    """live controller 的 pid（給外部 signal 定址）；無/已死 → None。"""
    info = controller_info()
    return info.get('pid') if info else None


def request_reload() -> None:
    """丟『優雅 reload』請求（`devctl reload` 用）：loop 排空在飛 worker 後退出 → launchd 載入新碼（零浪費）。
    與 kick -k 的差別 = 不殺在飛工作。寫 marker 是唯一副作用，實際 drain/exit 由 controller 自己做。"""
    try:
        with open(RELOAD_REQUEST, 'w') as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _reload_pending() -> bool:
    return os.path.exists(RELOAD_REQUEST)


def _clear_reload() -> None:
    try:
        os.remove(RELOAD_REQUEST)
    except OSError:
        pass


PLIST_LABEL = 'com.textbookreader.bookpipeline'  # 與 plist Label / devctl 一致


def _schedule_respawn() -> None:
    """reload 專用：丟一個 detached 小弟，**等本 controller 退出（.tick.lock 釋放）後**才
    `launchctl kickstart` 拉起新碼 → 退出即刻 respawn、零空檔。為何要等死：鎖是 LOCK_EX|LOCK_NB，
    舊實例還活著時 kickstart 的新實例會搶不到鎖而「跳過本次」→ 必須等舊的死透。只在 reload 走
    （idle/walltime 自然退出**不**呼叫）→ 維持 idle 收斂、不變 crash 行為。若與 launchd StartInterval
    fire 撞期 → NB 鎖天然序列化（一個拿到跑、另一個跳過），不雙跑。"""
    pid = os.getpid()
    uid = os.getuid()
    # 等本進程死透（鎖釋放）→ 立即 kickstart。detached（new session）→ 不隨本進程退出被收掉。
    script = (f'while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; '
              f'exec /bin/launchctl kickstart gui/{uid}/{PLIST_LABEL}')
    try:
        subprocess.Popen(['/bin/sh', '-c', script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log('reload：已排程 detached respawn（本進程退出即 kickstart 拉新碼，零空檔）')
    except Exception as e:
        log(f'reload respawn 排程失敗（退回 launchd StartInterval ≤15min）：{e}')


def wake_controller() -> bool:
    """送 SIGUSR1 喚醒 live controller 立即 re-observe（撿 marker 立刻派工、**不中斷在飛 worker**）。
    回是否真的送出（無 live controller → False，呼叫端改 kick 起一個）。"""
    import signal
    pid = controller_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except OSError:
        return False


def _have_slugs() -> set:
    """已存在、不該再爬的 slug：mineru_data/* 任何書（含 _sol、含 in-flight）∪ raw_pdfs/*.pdf。
    daemon 端去重的權威來源——擋 refill 誤選既有書（曾見 james_islr 等已 parsed 書被重列）。"""
    have = set()
    try:
        for p in glob.glob(os.path.join(DATA_DIR, '*')):
            if os.path.isdir(p):
                have.add(os.path.basename(p))
    except Exception:
        pass
    try:
        for p in glob.glob(os.path.join(ROOT, 'raw_pdfs', '*.pdf')):
            have.add(os.path.basename(p)[:-4])
    except Exception:
        pass
    return have


# 快取「0」當硬閘門的可信時效（秒）。額度 0→正（每日 reset／回補）是『解除阻擋買書員』方向、
# 且**無失效事件**（invalidate_zlib_cache 只在花額度時觸發、恢復時不觸發）。若信任 stale-0 會卡住
# 買書員整個 ZLIB_TTL（~5min）→「額度好了卻遲遲不拉書」。故 0 僅 fresh 時可信，更舊 → 樂觀重查。
_ZLIB_GATE_ZERO_TTL = 90


def _zlib_remaining_cached():
    """廉價讀 zlib 今日餘額快取（dev/zlib_quota.json）→ reactive due 判斷用，不打網路。
    回 int 餘額 / None=未知（樂觀視為可能有額度，drain 階段再做權威查詢並回寫快取）。
    關鍵：正餘額永遠可信（正值不會假阻擋，drain 自我校正）；但「0」只有 fresh(<_ZLIB_GATE_ZERO_TTL)
    才當硬閘門信，stale-0 → 回 None（樂觀）→ drain 走權威 live 查偵測恢復（查後回寫，自動 throttle
    下次 re-probe）。否則 stale-0 會卡買書員整個 TTL。"""
    try:
        c = json.load(open(os.path.join(ROOT, 'dev', 'zlib_quota.json')))
        r = c.get('total_remaining')
        if r is None:
            return None
        r = int(r)
        if r == 0 and (time.time() - (c.get('fetched_at') or 0)) > _ZLIB_GATE_ZERO_TTL:
            return None  # stale-0：不信任當阻擋 → 樂觀，交 drain 權威查偵測恢復
        return r
    except Exception:
        return None


def _fetch_book(b: dict) -> str | None:
    """確定性下載單本（resolver 已解好 id/hash、select_next 已選好 slug，daemon 已指派 account）。
    回 slug=成功 / None=失敗。rc 0 且 raw_pdfs/<slug>.pdf 存在才算成功（不信 rc）。"""
    slug, bid, bhash = b.get('slug'), str(b.get('id', '')), str(b.get('hash', ''))
    if not (slug and bid and bhash):
        log(f'❌ crawl plan 條目缺欄位：{b}')
        return None
    if not re.fullmatch(r'[a-z0-9_]{1,64}', slug):  # LLM 產出，須擋路徑穿越/任意檔名
        log(f'❌ crawl plan slug 不合法（須 [a-z0-9_]{{1,64}}）：{slug!r} → 拒絕')
        return None
    cmd = ['uv', 'run', '--with', 'requests', '--with', 'playwright', 'python', '-m',
           'book_pipeline.crawl_zlib', 'fetch', bid, bhash, '--slug', slug]
    if b.get('account') is not None:
        cmd += ['--account', str(b['account'])]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    rc = proc.returncode
    if rc == 0 and os.path.isfile(os.path.join(ROOT, 'raw_pdfs', f'{slug}.pdf')):
        m = re.search(r'完成 ([\d.]+) MB', proc.stdout or '')  # crawl_zlib cmd_fetch 印「完成 X.X MB」
        if m:
            try:
                b['_mb'] = float(m.group(1))
            except ValueError:
                pass
        log(f'crawl ok：已補書 slug={slug}（acct {b.get("account")}）')
        return slug
    log(f'❌ crawl fetch 失敗 slug={slug} rc={rc}')
    return None


def _crawl_backlog_books(rows: list[dict]) -> list[dict]:
    """pipeline 內待消化的書清單（爬書水位的 consumer 集合）：in-flight OCR、或仍有強制待辦
    （非 — 非 可選）的非 deployed 書。deployed 書 todo=— 且不在 OCR → 自然排除。每本恰一次。"""
    ifl = mb.in_flight()
    return [r for r in rows
            if not r.get('deployed')
            and (r['slug'] in ifl
                 or [t for t in r['todo'].split() if t and t != '—' and not t.endswith('(可選)')])]


def _crawl_backlog(rows: list[dict]) -> int:
    """backlog 書數（爬速綁此值：≥CAP 就停買、讓 pipeline 消化）。= 在飛/待辦書數，單一真相源。"""
    return len(_crawl_backlog_books(rows))


def _drain_due(rows: list[dict]) -> bool:
    """**買書員**是否該跑（cheap、免 subprocess、保 idle 收斂）：解析池有 ready 可下載 ∧ pipeline
    有空間 ∧ 額度快取≠0。額度快取 None（未知）→ 樂觀（drain 內權威查）。無 ready/額度0/pipeline 滿 → 不跑。"""
    if _crawl_backlog(rows) >= CRAWL_INFLIGHT_CAP or _zlib_remaining_cached() == 0:
        return False
    blocked = q.crawl_blocked_slugs(MAX_FETCH_FAILS)
    return bool(booklists.select_next(1, exclude=blocked))


def drain_crawl_queue(rows: list[dict], dry: bool = False) -> list[str]:
    """**買書員（確定性，非 agent）**：每 cycle **直接 select_next 取解析池 ready 書**並行抓、抓到落
    raw_pdfs（隨即成 owned，下個 observe 自然接手 ingest）。無購物清單 buffer——下載候選即時從
    (解析池 ∖ owned ∖ 失敗達上限) 推導。**額度只在這裡咬**，與 LLM 無關、無退避。連 MAX_FETCH_FAILS
    次失敗的書記進 pipeline_state（q.bump_crawl_fail），select_next 以 exclude 排除、不卡隊頭。
    回本輪抓到的 slug。"""
    # 閘門：crawl lane 全域 held（不派新書）→ 本輪不抓（涵蓋 reactive/tick_once/dry；reactive 另有
    # pre-check 省 _start，此處為 tick_once 路徑 + 硬兜底）。per-book crawl 閘在 batch 選出後再過濾。
    _g = _active_gates()
    if not pg.gate_allows(None, 'crawl', _g):
        if dry:
            log('crawl 買書員：gate hold（不派新書）→ 本輪不抓')
        return []

    backlog = _crawl_backlog(rows)
    room = max(0, CRAWL_INFLIGHT_CAP - backlog)  # pipeline 還能容納幾本在飛（backpressure：滿了不抓）
    blocked = q.crawl_blocked_slugs(MAX_FETCH_FAILS)

    if dry:
        rem = _zlib_remaining_cached()
        cand = len(booklists.select_next(room or 1, exclude=blocked))
        tail = '' if (cand and room > 0) else ' → 本輪不抓'
        log(f'crawl 買書員：解析池可下載 {cand}+ 本 · pipeline 餘裕 {room} · 額度(快取) {rem}{tail}')
        return []

    if room <= 0:
        log(f'crawl 買書員 hold：pipeline 已滿（backlog {backlog} ≥ {CRAWL_INFLIGHT_CAP}）→ 待消化')
        return []

    accts = _zlib_accounts_remaining()
    if accts is None:
        log('crawl 買書員 skip：查額度失敗，本輪不抓')
        return []
    slots = [a['account'] for a in accts for _ in range(max(0, a.get('remaining') or 0))]
    if not slots:
        log('crawl 買書員 defer：今日額度耗盡 → 待明日（不 latch，下輪自動重探）')
        return []

    # 本輪要抓的本數 = min(pipeline 餘裕, 額度槽)；確定性取解析池前 n 本（排除已失敗達上限者）
    n = min(len(slots), room)
    batch = booklists.select_next(n, exclude=blocked)
    batch = [b for b in batch if pg.gate_allows(b.get('slug'), 'crawl', _g)]  # per-book crawl 閘
    if not batch:
        log('crawl 買書員：合格池暫無可下載（全 owned/pending/candidate/rejected/gate-held 或已失敗達上限）')
        return []
    for i, b in enumerate(batch):
        b['account'] = slots[i]
    log(f'crawl 買書員：解析池取 {len(batch)} 本下載（額度槽 {len(slots)}、pipeline 餘裕 {room}）')
    publish_crawl_live(batch)                            # /dev 即時看板：全本標下載中（前端 ~2s 撿）
    ok, crawled = set(), []
    with ThreadPoolExecutor(max_workers=min(CRAWL_PARALLEL, len(batch))) as ex:
        futs = {ex.submit(_fetch_book, b): b for b in batch}
        for f in cf.as_completed(futs):
            b = futs[f]
            try:
                s = f.result()
            except Exception as e:
                s = None
                log(f'❌ crawl fetch {b.get("slug")} 異常：{e}')
            if s:
                ok.add(b['slug']); crawled.append(s)
                q.clear_crawl_fail(b['slug'])           # 抓成功 → 清失敗計數
                update_crawl_live(b['slug'], 'done', b.get('_mb'))
            else:
                update_crawl_live(b['slug'], 'failed')
                fails = q.bump_crawl_fail(b['slug'])     # 失敗 +1，達上限後 select_next 自動排除
                if fails >= MAX_FETCH_FAILS:
                    log(f'crawl drop：{b["slug"]} 連 {fails} 次 fetch 失敗 → 排除出下載候選（架構師可重解後重試）')
    end_crawl_live()                                     # 整批收尾 → 看板進「剛完成」tail 寬限後自動隱藏
    log(f'crawl 買書員 done：抓到 {len(ok)}/{len(batch)}')
    if crawled:
        hist.set_touched('crawl_plan', crawled)  # 帶進的書 → 各書抽屜查得此爬書歷程
        try:
            from book_pipeline import devctl
            devctl.invalidate_zlib_cache()  # 剛花額度 → 失效快取，下個 snapshot 反映 live 餘額
        except Exception:
            pass
    return crawled


def do_crawl_tick(dry: bool, rows: list[dict]) -> list[str]:
    """oneshot tick 的 crawl 編排：**買書員 drain**（確定性，直接從解析池取 QUALIFIED 下載）。
    無 buffer、無 refill——下載候選即時從合格池推導。reactive loop **不**走此函數——它把買書員
    drain 當獨立 due-gated 步驟分派（C1，每 cycle）。daemon 不再自主填書單（resolver 已移除，改人工
    /restock）。回 drain 抓到的 slug。"""
    return drain_crawl_queue(rows, dry)


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
    """poll 收割 in-flight 書的 OCR → download+assemble→unified。**非阻塞**：用短 max_wait
    （HARVEST_MAX_WAIT），OCR 全好的書第一次 poll 就秒收，沒好的書幾十秒就放棄、留 in-flight
    下個 cycle 再收——**絕不等 OCR 跑完而凍住整 cycle**（曾因預設 30min 等待把整條鏈卡死，
    害下游 audit/crawl 全停）。rc 0=組好可 parse / 2=poll 逾時(OCR 未完) / 3=部分 chunk 缺
    （2、3 皆留 in-flight 重收，非錯誤）/ 1=無 manifest。"""
    if dry:
        log(f'DRY ingest harvest {slug}（poll OCR → 收割）')
        return 0
    log(f'ingest harvest {slug}：poll OCR batch（max_wait={HARVEST_MAX_WAIT}s）→ 收就緒 chunk')
    rc = mb.harvest_ingest(slug, max_wait=HARVEST_MAX_WAIT)
    if rc == 0:
        log(f'ingest harvest {slug} ✓：unified 組好，可 parse')
    elif rc in (2, 3):
        log(f'ingest harvest {slug}：OCR 尚未全完成（rc={rc}）→ 留 in-flight，下個 cycle 再收')
    else:
        log(f'❌ ingest harvest {slug}：rc={rc}')
    return rc


@contextlib.contextmanager
def _live_det_worker(verb: str, slug: str | None):
    """確定性 advance 步驟（parse / deploy build / catalog repair）的 live-worker 登記。
    這些步驟跑在 controller 進程內（非 LLM 子進程），過去**不註冊 worker_registry** → /dev 面板
    只看得到 LLM agent + math_sweep，正在 build/repair 的書顯示「待 X（暫無工人）」誤判成卡關
    （實則 build_all 的 cwebp 轉圖、catalog repair 三件套正跑得火熱）。此 CM 讓它們現形為
    「🔧 verb 處理中」。pid=controller 自身（活著、不被 reap）；provider='det'（非 LLM，無 model）。
    fail-open：登記失敗絕不擋實際工作。"""
    wkey = f'{verb}:{slug or "-"}:det:{os.getpid()}'
    try:
        wr.register(wkey, slug, verb, os.getpid(), 'det')
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            wr.unregister(wkey)
        except Exception:
            pass


def do_parse(slug: str, dry: bool) -> int:
    if dry:
        return _run(['uv', 'run', '--with', 'pyyaml', 'python', '-m',
                     'book_pipeline.parser', slug], dry=dry)
    with _live_det_worker('parse', slug):
        return _run(['uv', 'run', '--with', 'pyyaml', 'python', '-m',
                     'book_pipeline.parser', slug], dry=dry)


def _book_qc_block(slug: str) -> list[str]:
    """部署前書況 gate：parse 後驗「書對不對/完不完整」的硬缺陷（confusion/殘卷）。
    回 blocking reasons（空=通過）。fail-open：gate 自身出錯絕不擋好書（零誤判優先），
    僅 log。confusion 類缺陷源頭在 crawl，下游 stage 無從補 → 標 review 待架構師裁決。"""
    try:
        from textbooks import corpus
        from book_pipeline import booklists as bl
        from book_pipeline import book_qc
        book = corpus.load_book(slug)
        if not book:
            return []
        sot = next((t for t in bl.targets() if t.get('slug') == slug), None)
        flags = book_qc.detect(book, (sot or {}).get('title', ''))
        return book_qc.blocking_reasons(flags)
    except Exception as e:
        log(f'book_qc gate {slug} 異常（fail-open，照常部署）：{e}')
        return []


# ── 貴重成果 auto-commit ──────────────────────────────────────────────────────
# daemon 24hr 產書會吐 git 追蹤的貴重成果（math/catalog override、parsed/*.zh.json、
# extract_rules、cover、索引）。它們本就該被 commit（test_artifacts_committed 這道 deploy
# 前契約閘掃的就是 committed 成果），但 pipeline 過去從不 commit → 堆在工作區直到人工巨型
# commit，使 `git status` 失效、真問題被噪音蓋住、無 history 兜底。controller 退出時做一次
# curated flush 補上這環。commit-only 不 push（跨機同步仍手動）。BOOK_PIPELINE_AUTOCOMMIT=0 關。
AUTOCOMMIT = os.environ.get('BOOK_PIPELINE_AUTOCOMMIT', '1') != '0'
_ARTIFACT_PATHS = [
    'book_pipeline/math_overrides', 'book_pipeline/catalog_overrides',
    'book_pipeline/booklists', 'book_pipeline/proposals.d',
    'book_pipeline/slug_map.json', 'book_pipeline/metadata_schema.yaml',
    'book_pipeline/mineru_data',  # gitignore 已只放行 *.zh.json + extract_rules.yaml + cover.jpg
]


def _artifact_slugs(paths: list[str]) -> list[str]:
    """從 staged 檔路徑萃取書 slug（去重排序），供 commit 訊息語意化。純函式可測。
    math_overrides/<slug>.json · catalog_overrides/<slug>.json · mineru_data/<slug>/…；
    共用索引（slug_map/proposals/metadata/booklists）歸不到單一 slug → 不列。"""
    slugs = set()
    for p in paths:
        parts = p.split('/')
        if len(parts) >= 3 and parts[0] == 'book_pipeline':
            if parts[1] in ('math_overrides', 'catalog_overrides') and parts[2].endswith('.json'):
                slugs.add(parts[2][:-5])
            elif parts[1] == 'mineru_data':
                slugs.add(parts[2])
    return sorted(slugs)


def _artifact_commit_msg(files: list[str]) -> tuple[str, str]:
    """staged 檔清單 → (subject, body)。純函式可測。"""
    slugs = _artifact_slugs(files)
    head = ', '.join(slugs[:5]) + (f' (+{len(slugs) - 5})' if len(slugs) > 5 else '')
    subject = f'data(pipeline): daemon 產書成果 {head or "索引"}（{len(files)} 檔）'
    body = ('自動提交層（commit_artifacts）：math/catalog override、zh overlay、extract_rules、'
            'cover、索引。curated 白名單，機器產物由 gitignore 排除。')
    return subject, body


def commit_artifacts() -> None:
    """貴重成果 curated auto-commit（controller 退出時呼叫，main thread → 無 git index race）。
    鐵律：**只 stage 白名單路徑、絕不 `git add -A`**——盲加會把 gitignore 的漏（status.json /
    隔離書 unified symlink 之類）每次靜默 commit 進去。fail-open：git 任何錯都不擋 pipeline。
    commit-only 不 push。無 staged 變更 → 靜默 no-op。身份用 global config（Max0228）。"""
    if not AUTOCOMMIT:
        return
    try:
        # 只 add 實際存在的白名單路徑：`git add -- a b` 任一 pathspec 不存在會整批 fatal、零 staged，
        # 故某 override 目錄尚未生成時須先濾掉，否則整次 commit 被一條缺路徑拖垮。
        present = [p for p in _ARTIFACT_PATHS if os.path.exists(os.path.join(ROOT, p))]
        if not present:
            return
        subprocess.run(['git', 'add', '--', *present], cwd=ROOT,
                       check=False, capture_output=True, timeout=120)
        staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=ROOT,
                                capture_output=True, text=True, timeout=30).stdout.strip()
        if not staged:
            return
        files = staged.splitlines()
        subject, body = _artifact_commit_msg(files)
        r = subprocess.run(['git', 'commit', '-q', '-m', subject, '-m', body], cwd=ROOT,
                           check=False, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            log(f'auto-commit ✓：{len(files)} 檔成果落盤（{subject}）')
        else:
            log(f'auto-commit 失敗（不影響 pipeline）：{(r.stderr or r.stdout).strip()[:200]}')
    except Exception as e:
        log(f'auto-commit 異常（不影響 pipeline）：{e}')


def do_deploy(slug: str, dry: bool, no_deploy: bool) -> int:
    if no_deploy:
        log(f'deploy skip {slug}（--no-deploy）')
        return 0
    if not os.path.isdir(READER_ROOT):
        log(f'deploy skip {slug}：找不到 textbook-reader ({READER_ROOT})')
        return 0
    # 書況 gate：硬缺陷（書錯/殘卷）→ 標 review 不上站；通過則清除舊標記（書已修/重 parse）
    block = _book_qc_block(slug)
    if block and not dry:
        q.mark_book_qc(slug, block)
        log(f'deploy BLOCK {slug}：書況不合格 {block} → 標 review，不上站（源頭缺，待架構師裁決）')
        return 0
    if not dry:
        q.clear_book_qc(slug)
    # build-only：烤出本地 data/<slug> + img/<slug>，nginx 直讀工作目錄即時上站（無 git/push）
    build = ['uv', 'run', 'python', '-m', 'build.build_all', slug]
    log(('DRY ' if dry else 'RUN ') + 'build_all ' + slug)
    if dry:
        return 0
    with _live_det_worker('deploy', slug):  # build_all 上百張圖 cwebp 轉檔 → 數分鐘，面板顯示「🔧 deploy 處理中」
        rc = subprocess.run(build, cwd=READER_ROOT).returncode
    # 只在 build 成功且 book.json 真的烤出才標已部署；否則留待下個 cycle 重試（不誤標 done）。
    book_json = os.path.join(READER_ROOT, 'data', slug, 'book.json')
    if rc == 0 and os.path.isfile(book_json):
        q.mark_deployed(slug)
        log(f'deploy {slug} ✓：book.json 已烤出，上站')
        do_math_track(slug)  # 上站即量數學殘餘（track-only，best-effort，不影響 deploy rc）
    else:
        log(f'❌ deploy {slug}：build rc={rc}，book.json={"有" if os.path.isfile(book_json) else "無"} → 不標 deployed，下個 cycle 重試')
    return rc


def do_math_track(slug: str) -> int:
    """post-deploy 量該書數學式渲染殘餘，寫 _math_report.json + state（do_math_sweep 門檻判據）。
    best-effort：缺 node_modules → graceful skip（bad_occ=0）；任何例外吞掉、絕不影響 deploy。
    回殘餘 bad_occ（-1=出錯）。"""
    try:
        from book_pipeline import math_validate as mv
        rep = mv.validate_book(slug)
        mv.write_report(slug, rep)
        bad = int(rep.get('stats', {}).get('bad_occ') or 0)
        q.mark_math_validated(slug, bad, rep.get('macros_version', 'none'))
        if rep.get('status') == 'fail':
            log(f'math track {slug}：殘餘 {bad} occ（by_cat {rep.get("by_category")}）')
        return bad
    except Exception as e:
        log(f'math track {slug} 異常（不影響 deploy）：{e}')
        return -1


def _sweep_decision(node_available: bool, total: int, accepted: int,
                    last_sweep: dict | None, cur_macros: str) -> tuple[bool, str]:
    """純收斂判定（不碰磁碟/node，可單測）。回 (該不該 sweep, reason)。
      no-node    缺 node_modules → 無從驗證，永不派。
      converged  residual_unaccepted = total − accepted ≤ 0 → 真 0（或剩餘全已 accept）。
      fixpoint   上次 sweep 在相同 macros 下沒降殘餘也沒改書、且 corpus 殘餘此後未變 → 原地踏步，
                 不派（等外部變化：新書部署改 total、或 agent 改 macros）。
      due        其餘（residual_unaccepted>0 且非 fixpoint）→ 派，繼續朝真 0 收斂。"""
    if not node_available:
        return False, "no-node"
    if total - accepted <= 0:
        return False, "converged"
    ls = last_sweep or {}
    if ls.get("macros_version") == cur_macros and ls.get("residual_after") == total:
        progressed = ((ls.get("residual_before") is not None
                       and ls["residual_after"] < ls["residual_before"])
                      or bool(ls.get("touched")))
        if not progressed:
            return False, "fixpoint"
    return True, "due"


def _math_sweep_due(state: dict | None = None) -> tuple[bool, int]:
    """廉價判定（讀 state，不重跑 node）。回 (該不該 sweep, 當前 corpus 殘餘 occ)。
    判定邏輯見 _sweep_decision（residual_unaccepted>0 且非 fixpoint）。"""
    from book_pipeline import math_validate as mv
    s = state if state is not None else q._load_state()
    total = q.corpus_math_residual(s)
    due, _reason = _sweep_decision(mv.node_available(), total, q.math_accepted_total(s),
                                   q.math_sweep_state(s), mv.macros_version())
    return due, total


def do_math_sweep(dry: bool) -> int:
    """corpus-level 數學 sweep（track-only，不綁單本 advance 關鍵路徑）：residual_unaccepted>0 且非
    fixpoint → daemon **直接 det-step 跑 `math_sweep batch`**（純自架 LLM API：list→分批 LLM→render
    守門→per-book apply+重驗；**無無頭 agent 層**——邏輯就是「有多少壞式、batch 打 API 解掉」，非以書為
    單位派 agent）→ daemon 把殘餘變動的書重烤上站 → 重量殘餘、記錄 sweep/batch 狀態（供 fixpoint 判定）。
    回 0=完成/跳過、1=batch 基礎設施失敗（node/ccnexus，不記狀態 → 下個 cycle 重試）。"""
    from book_pipeline import math_validate as mv
    # 閘門：math_sweep lane held → 跳過（涵蓋 tick_once/dry + 硬兜底；reactive 另有 pre-check 省 _start）。
    if not pg.gate_allows(None, 'math_sweep', _active_gates()):
        if dry:
            log('math sweep：gate hold → 跳過')
        return 0
    due, total = _math_sweep_due()
    if not due:
        return 0
    cur = mv.macros_version()
    log(f'math sweep：corpus 殘餘 {total} occ（unaccepted>0、非 fixpoint）→ 直跑 math_sweep batch --limit {MATH_BATCH_LIMIT} --workers {MATH_BATCH_WORKERS} --n {MATH_BATCH_N} --rounds {MATH_BATCH_ROUNDS}（純 API，{MATH_BATCH_WORKERS} worker 並發，macros={cur}）')
    if dry:
        log(f'DRY uv run python -m book_pipeline.math_sweep batch --limit {MATH_BATCH_LIMIT} --workers {MATH_BATCH_WORKERS} --n {MATH_BATCH_N} --rounds {MATH_BATCH_ROUNDS}')
        return 0
    before_by_book = mv.residual_by_book()  # 派工前快照：normalize 規則/macro 修的書未必有 override，靠殘餘降偵測
    t0 = time.time()
    q.set_math_batch_running(total)         # 持久 flag → /dev 顯「batch 處理中」（獨立 devsnapshot 進程讀得到）
    # math sweep 走 ccNexus HTTP batch（執行路徑非 CLI），但仍納入統一觀測層：controller 端註冊單例
    # worker（'__math_sweep__'）+ agent_history corpus session，讓 /dev「工人 N」數得到、trace session
    # 看得到其歷程。⚠ 在 reactive loop（daemon 預設路徑）do_math_sweep 被提交進 LOOP_CONCURRENCY
    # 執行緒池，**與 advance:*/harvest:*/__crawl_drain__ 等 worker 並發**（非序列）；
    # controller 端同進程註冊仍安全——worker_registry 與 agent_history 全程 threading.Lock 保護，且
    # '__math_sweep__' 為唯一 math_sweep key（_last_by_verb 以 verb 為鍵不互踩）→ 無資料競爭。若日後
    # 要在此加共享狀態，務必沿用既有 Lock，勿假設序列執行。細粒度每批進度另存 math_live/math_history
    # （並存，互補）。stderr tee 回本進程 → launchd.err.log 即時可見不被吞。
    wkey = '__math_sweep__'
    model = math_sweep_model()
    out_parts: list[str] = []
    rc = -1
    proc = None
    try:
        proc = subprocess.Popen(['uv', 'run', 'python', '-m', 'book_pipeline.math_sweep', 'batch',
                                 '--limit', str(MATH_BATCH_LIMIT), '--workers', str(MATH_BATCH_WORKERS),
                                 '--n', str(MATH_BATCH_N), '--rounds', str(MATH_BATCH_ROUNDS), '--verbose'],
                                cwd=READER_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wr.register(wkey, None, 'math_sweep', proc.pid, 'ccnexus')
        hist.start(wkey, None, 'math_sweep', proc.pid, 'ccnexus', model)

        def _pump_out():
            for line in proc.stdout:  # type: ignore[union-attr]
                out_parts.append(line)  # stdout 全部 = 末尾 batch JSON 結果（indent=2 多行）

        def _pump_err():
            for line in proc.stderr:  # type: ignore[union-attr]
                sys.stderr.write(line)  # tee → launchd.err.log 即時可見（不被吞）
                sys.stderr.flush()
                s = line.strip()
                if s:
                    wr.event(wkey, 'text', s)    # /dev 工人面板即時進度
                    hist.event(wkey, 'text', s)  # trace session 完整歷程
        to = threading.Thread(target=_pump_out, daemon=True)
        te = threading.Thread(target=_pump_err, daemon=True)
        to.start()
        te.start()
        rc = proc.wait()
        to.join(timeout=5)
        te.join(timeout=5)
    finally:
        if proc is not None:
            hist.finish(wkey, rc)
            wr.unregister(wkey)
        q.clear_math_batch_running()
    res = {}
    out = ''.join(out_parts).strip()
    try:
        if out:
            res = json.loads(out)  # stdout 全部 = batch 的 JSON 結果（進度走 stderr）
    except Exception:
        pass
    if rc != 0 or not res.get('ok'):
        err = res.get('error') or f'rc={rc}（進度/錯誤詳見 launchd.err.log）'
        log(f'❌ math sweep batch 失敗（{err}）→ 不記狀態，下個 cycle 重試')
        return 1
    log(f"math sweep batch：解 {res.get('accepted', 0)} 條 · 觸 {res.get('books_touched', 0)} 書 · "
        f"剩 {res.get('still_failing', 0)} 條硬殘")
    # corpus session 回填實際觸及的書 → 各書抽屜「slug ∈ touched」查得此 sweep 歷程（比照 crawl_plan）。
    hist.set_touched('math_sweep', list((res.get('remaining_by_book') or {}).keys()))
    # daemon 確定性收尾（apply 從 agent 收回 daemon，比照 catalog；消除「agent apply 的書必須恰等於
    # daemon 重烤的書」的脆弱耦合）：對每本有 override 的書 idempotent re-apply；凡本輪真有改動（apply
    # 產生 applied，含重 parse 沖掉後重套）或 override 本輪新寫（mtime>t0）→ 重烤上站（live 讀 data/，
    # 不重烤看不到修復）+ 重量殘餘。apply 失配自動 skip-drift，全程不 raise。
    from book_pipeline import apply_math_overrides as amo
    od = os.path.join(BP, 'math_overrides')
    rebake: set[str] = set()
    for fn in sorted(os.listdir(od) if os.path.isdir(od) else []):
        if not fn.endswith('.json') or fn.startswith('_'):
            continue
        slug = fn[:-5]
        fresh = os.path.getmtime(os.path.join(od, fn)) > t0
        try:
            stats = amo.apply_overrides(slug)
        except Exception as e:
            log(f'❌ math sweep apply {slug}：{e}')
            continue
        if fresh or any(k.endswith(':applied') and v for k, v in stats.items()):
            rebake.add(slug)
            log(f'math sweep：{slug} {stats} → 重烤上站')
    # 稀有規則路徑：normalize 規則/macro 修的書沒有 override → 靠「殘餘下降」補進 rebake（該路徑會自跑
    # backfill 重 parse + 重驗，reports 已最新；不重烤這些書 reader 看不到規則修復）。逐條 override 主路徑
    # 由上面 mtime/applied 偵測涵蓋，math_sweep fix/batch 已逐書 validate_book 寫最新 report。
    for slug, after in mv.residual_by_book().items():
        if after < before_by_book.get(slug, after) and slug not in rebake:
            rebake.add(slug)
            log(f'math sweep：{slug} 殘餘 {before_by_book.get(slug)}→{after}（規則修，無 override）→ 重烤上站')
    for slug in sorted(rebake):
        brc = subprocess.run(['uv', 'run', 'python', '-m', 'build.build_all', slug], cwd=READER_ROOT).returncode
        if brc != 0:
            log(f'❌ math sweep 重烤 {slug} build rc={brc}（parsed 已修、data 未更新，下個 sweep 重試）')
        do_math_track(slug)
    residual_after = q.corpus_math_residual()
    q.mark_math_swept(cur, total, residual_after, sorted(rebake))  # total = 本輪 before（供 fixpoint 判定）
    q.record_math_batch({'accepted': res.get('accepted', 0), 'still_failing': res.get('still_failing', 0),
                         'books_touched': res.get('books_touched', 0),
                         'before': total, 'after': residual_after, 'rebaked': sorted(rebake)})
    hist.set_touched('math_sweep', sorted(rebake))  # corpus session 回填改動書清單 → 各書抽屜查得此場歷程
    log(f'math sweep ✓：batch 後改 {len(rebake)} 書，corpus 殘餘 {total}→{residual_after} occ')
    return 0


def do_catalog_repair(slug: str, dry: bool) -> int:
    """catalog_audit 殘漏的確定性修復閉環（無 LLM）：repair 三件套 → re-audit。
    三個 repair 各自確定性、自帶 _manual_repair_backups（可回滾）；metadata 補 caption/id、
    from_unified 從 MinerU 原文找回缺塊、aliases 連結 ref→既有塊。critical 清零 → catalog
    過關，下個 cycle 自然推進（sol/deploy）；殘餘 critical → surface ❌（少數書需 LLM/人工）。
    repair 改的是 parsed/*.json，status 判定『有 book.json 即不重 parse』故結果持久。"""
    from book_pipeline.catalog_audit import audit_catalog
    before = audit_catalog(slug, write_report=False).get('critical') or 0
    if before == 0:
        return 0
    log(f'catalog_repair {slug}：critical={before} → 跑確定性 repair 三件套')
    if dry:
        return 0
    with _live_det_worker('catalog_audit', slug):  # 三件套 repair 數分鐘 → 面板顯示「🔧 catalog_audit 處理中」
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
    回 0=已收斂（過關或 accept，可續 deploy）/ -2=LLM 撞 session 限額（defer 下個 cycle）。"""
    residual = do_catalog_repair(slug, dry)
    if residual == 0:
        return 0
    if dry:
        log(f'DRY catalog {slug}：殘留 {residual} → 真跑時派 LLM / accept')
        return 0
    if q.catalog_llm_done(slug):
        # 上個 cycle 已派過 LLM、本 cycle 確定性後仍殘留 → 終局 accept（不可修者）
        q.mark_catalog_accepted(slug, residual)
        log(f'catalog {slug}：LLM 修復後仍殘 {residual}（源頭缺不可修）→ accept，照常 deploy')
        return 0
    log(f'catalog {slug} → LLM 修復殘留 {residual}（產 overrides：pdf_crop/alias/修 ref）')
    rc = dispatch_llm('catalog_audit', slug, dry)
    if rc != 0:
        # session 限額(-2) 或 claude 出錯/timeout → defer 重試，不 mark_llm_done、不誤 accept
        log(f'catalog {slug}：LLM rc={rc} → defer，下個 cycle 重派（不誤 accept）')
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


def _escalate_sol(slug: str) -> None:
    """sol_extract 跑完未達終態（agent 紀律異常）→ 一次即升級架構師、停止再派（非靜默放棄）：
    ① 標 state.sol_escalated → status._sol_escalated 令 todo 消除、收斂；
    ② 開 sol/unresolved proposal 攤進申訴佇列（與 agent 主動申訴同管道）。proposal 開立失敗
    絕不影響收斂（已標旗標）。架構師修 skill/源頭後 clear state[slug].sol_escalated 重試。"""
    q.mark_sol_escalated(slug, 'agent 跑完未給結論（未 merge 亦未標 _pending）')
    try:
        from book_pipeline import proposals
        proposals.propose(
            domain='sol', type_='unresolved', slug=f'{slug}_sol', source='daemon',
            title=f'sol_extract {slug} 跑完未收斂',
            evidence='dispatch rc=0 但 sol==0 且 _sol_pending=False（agent 未達終態）',
            proposal='查 audit-sol agent 為何未達終態（merge 或 _pending 二擇一）；'
                     '修源頭/skill 後 clear state[slug].sol_escalated 重試')
        log(f'sol_extract {slug}：⚠ agent 未收斂 → 標 escalated + 開 sol proposal 升級架構師')
    except Exception as e:
        log(f'sol_extract {slug}：標 escalated（proposal 開立失敗、不影響收斂：{e}）')


def _has_open_engine_proposal(slug: str) -> bool:
    """該 slug 是否有「未終態」的 engine 提案（proposed=待裁 或 parked=等外部；兩者皆仍是 blocker）。
    id 內嵌 _slugify(slug)；截斷碰撞極罕見且此處僅用於「是否標 review」（可逆、低風險），故簡單前綴比對即可。"""
    try:
        from book_pipeline import proposals as pr
        key = pr._slugify(slug)
        for d in pr.load_all():
            if d.get('status') not in pr.UNRESOLVED or d.get('domain') != 'engine':
                continue
            body = re.sub(r'^P-\d{4}-\d\d-\d\d-', '', d.get('id', ''))
            # 只認 exact 或同書第 N 案 `-<digits>`；拒 `-sol`（避免母書因解答本的提案被誤標 blocked）。
            if body == key or re.fullmatch(re.escape(key) + r'-\d+', body):
                return True
    except Exception:
        return False
    return False


def advance_book(slug: str, dry: bool, no_deploy: bool, max_steps: int = 15) -> None:
    """縱向推進**一本書**：沿自己的 pipeline 盡可能往下跑（triage→qc→ingest→parse→
    audit→catalog→sol→deploy），**不等其他書**。每步後重新 assess（磁碟狀態會變）。

    ingest 是 async 斷點：走到 ingest 只 submit（不等 OCR），書進 in-flight 後停；OCR
    並行於雲端排隊跑，由每 cycle 的 harvest 步驟統一收割 → 組好 unified 後（同 cycle 收割
    後的 advance，或下個 cycle）才續 parse→…→deploy。其餘停點：deploy=終點；done／可選
    translate／triage·qc 拒（R/X）→ 收工；同一 stage 連兩步沒前進 → 停（防失敗空轉）。
    """
    last_key = None
    for _ in range(max_steps):
        row = q.assess_one(slug)
        stage = row.get('stage', '') or ''
        todo = row.get('todo', '—')
        # todo 可能多項空白分隔，含 (可選) 非阻塞項（已部署/已 accept 的 catalog、已部署的 sol、translate）。
        # 取第一個「非可選」項當下一步動作；全可選/無 → 本書收工。**不可**用 todo.split('(')[0]：
        # 多項時會抓到可選前綴項（如 catalog_audit(可選)）→ 對已 accept 的 catalog 每輪重跑空轉。
        actionable = [t for t in todo.split() if t not in ('—', '') and not t.endswith('(可選)')]
        if not actionable or stage.startswith(('R', 'X')):
            return
        verb = actionable[0].split('(')[0]
        # 閘門：held → 正常停在此閘（非停滯、非拒絕）→ 直接 return，**不碰 last_key、不標 blocked/review**。
        # 放在 last_key 更新前 → 不污染停滯偵測；dry 亦套用 → --dry-run 如實顯示書停在哪個閘。
        if not pg.gate_allows(slug, verb, _active_gates()):
            log(f'advance {slug}：停在閘「{verb}」（gate hold）→ 待放行')
            return
        # 停滯鍵用 (stage, verb)：todo 在同 stage 內推進（如 catalog_audit→deploy 皆在 3）
        # 算前進、不誤判停滯；唯有同階段同動作連兩步沒變（修復沒清掉）才真停。
        key = (stage, verb)
        if key == last_key:
            log(f'advance {slug} 停滯於「{stage}/{verb}」（未前進）→ 停，待下個 cycle 重試')
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
        if verb == 'sol_ingest':
            # 解答本綁母書：母書驅動把自己的解答本 PDF 送 MinerU，**走與母書完全相同的 ingest 管線**
            # （do_submit/harvest 全 slug-agnostic，經 _raw_slug_map 解析 <slug>_sol.pdf）。async 斷點同
            # ingest：submit 後本書停，harvest 階段收割所有在飛 batch（含此解答本）→ 組好 unified 後
            # 下個 cycle assess 見 has_sol_book 轉出 sol_extract。do_submit 自帶 occupied 去重，冪等。
            do_submit(f'{slug}_sol', dry)
            return
        if verb == 'deploy':
            do_deploy(slug, dry, no_deploy)
            return  # pipeline 終點
        if verb == 'parse':
            do_parse(slug, dry)
        elif verb == 'sol_extract':
            # 解答本已 ingest → LLM 對齊 merge 進母書 problems[].solution。**一次定生死**：agent 在
            # 單次 dispatch 內就迭代收斂（audit-sol.md Step 6「至多 3 輪」），終態二擇一——merge 或
            # _pending+proposal。**不跨 cycle 重派同一本**（那只是賭 LLM 隨機性、正是 LLM 該消滅的脆弱）。
            rc = dispatch_llm('sol_extract', slug, dry)
            if rc != 0:
                # rc≠0 ＝ LLM 根本沒跑成（-2 session 限額／-1 timeout／其他任務失敗）＝基礎設施層暫時
                # 不可用，**非結論**：defer 下個 cycle 重跑（provider 多會恢復）。這與「一次定生死」治的是
                # 兩種病——後者治「rc==0 跑完卻沒結論」（agent 紀律，見下 _escalate_sol）；rc≠0 的暫時失敗
                # defer 重跑是對的。**已知邊界**：若某 provider chain 對某本書「穩定」rc≠0（四層 failover
                # 全持續掛），會每 cycle 重派——但這對齊既有全 stage 行為（catalog_audit/audit 同樣只特判
                # -2、其餘 rc 無限 defer），非本機制回歸；真解＝stage-agnostic circuit breaker（連續失敗計數
                # 才升級，與「賭 LLM 隨機」的 retry 語意不同），屬跨 stage 層級、不在此單 stage 打補丁。
                return
            sol_after = st.sol_stats(slug)[1]
            if sol_after > 0:
                log(f'sol_extract {slug} ✓：merge {sol_after} 題 → 重烤上站')
                do_deploy(slug, dry, no_deploy)  # 母書多已 deployed、assess 不再出 deploy todo → 比照 math sweep 自 rebake
            elif st._sol_pending(slug):
                log(f'sol_extract {slug}：agent 判源頭不可 merge（_pending）→ 已申訴、收斂')
            else:
                # 異常：agent 跑完（rc=0）卻沒給結論（既沒 merge 也沒標 _pending）= skill 違規。
                # **一次即升級，不賭骰子重試**：標 escalated 停再派 + 開 sol/unresolved proposal 攤給架構師。
                _escalate_sol(slug)
            return
        elif verb == 'catalog_audit':
            rc = do_catalog_resolve(slug, dry)
            if rc == -2:  # LLM 撞 session 限額 → defer 本書，下個 cycle 重試
                return
        elif row.get('llm') or verb in LLM_PROMPTS:
            log(f'advance {slug} → LLM {verb}')
            rc = dispatch_llm(verb if verb in LLM_PROMPTS else 'audit', slug, dry)
            if rc == -2:  # Claude session 限額 → defer 本書，等下個 cycle reset（非「停滯」）
                return
            # audit 跑完(rc==0)卻仍無 extract_rules.yaml 且已開 engine 提案 → 結構性卡關（schema 表達
            # 不了，如 aitchison combined 2-volume）。標記 review、終止跨 cycle 重派空轉（曾空轉 8 次重推
            # 同一 blocker）；有 yaml 產出則清舊標記（恢復可推進）。一次定生死，不賭 LLM 隨機重試。
            if verb == 'audit' and rc == 0:
                if not st._exists(slug, 'extract_rules.yaml') and _has_open_engine_proposal(slug):
                    q.mark_audit_blocked(slug, ['agent 跑完未產 extract_rules.yaml 且已開 engine 提案（schema 表達不了）'])
                    log(f'advance {slug}：audit 結構性卡關（已開提案、無 yaml）→ 標記 review、停止重派空轉')
                    return
                q.clear_audit_blocked(slug)
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
    q.ensure_first_seen([r.get('slug', '') for r in rows if r.get('slug')])  # 每 observe 補新書入庫戳（idempotent，零缺口）
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


# ── post-deploy 自動 GC（讓可重生中間產物自己排水，免人工定期 prune）────────────────
# deployed_at 後須過穩定期才 GC：避免剛 deploy 又被 sol/catalog/math 重烤時誤刪中途產物。
GC_STABILITY_MIN = int(os.environ.get('BOOK_PIPELINE_GC_STABILITY_MIN', '120'))
# GC 全書掃描節流（per-controller monotonic）：免每 observe cycle 掃 320 書讀 book.json。
GC_INTERVAL_SEC = int(os.environ.get('BOOK_PIPELINE_GC_INTERVAL_SEC', '1800'))
_gc_throttle = {'last': 0.0}  # 本 controller 上次 GC 掃描戳；loop 起頭重置 → 首掃於 ~INTERVAL 後


def _gc_due() -> bool:
    return (time.monotonic() - _gc_throttle['last']) >= GC_INTERVAL_SEC


def _gc_candidates(state: dict) -> list[str]:
    """可自動 GC 的書：確有 🟡 可重生產物 ∧ 已上站(完整 book.json) ∧ 非在飛 ∧ deployed_at
    距今 ≥ GC_STABILITY_MIN 分（無戳＝歷史書，必非剛 deploy → eligible）。先 cheap listdir
    篩有東西可清的（多數書已清→跳過），才對少數讀 book.json，避免每次掃讀 320 個大檔。

    ⚠ 不額外排除 leased_slugs（正被 advance worker 跑 sol/catalog/audit 的已上站書）：那些
    post-deploy 階段只讀 parsed/+unified/、**永不碰 raw/chunk_*/或 chunks/**（GC 的刪除集），
    目錄不相交 → 並行零競爭，故容忍重疊。唯一讀 raw/chunks 的 ingest/harvest/assemble 必在
    in_flight/occupied → 已排除。**此不變量是 GC 安全的根基**：日後若新增「已上站書回讀 raw」
    的階段，必須同步在此排除其 slug，否則靜默破壞此前提。"""
    from book_pipeline import storage_gc as sgc
    flying = mb.in_flight() | mb.occupied()
    out = []
    for slug in sgc._slugs():
        if not sgc._book_prune_targets(slug):       # cheap：無可清即跳
            continue
        if not sgc._deployed(slug):                 # 只對有東西可清的讀 book.json 驗完整
            continue
        if slug in flying or f'{slug}_sol' in flying:
            continue
        dat = state.get(slug, {}).get('deployed_at')
        if dat:
            try:
                age_min = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(dat)).total_seconds() / 60
                if age_min < GC_STABILITY_MIN:
                    continue                        # 剛 deploy，未過穩定期 → 暫不清
            except Exception:
                pass                                # 壞戳 → 視為老（保守不擋，書已上站久）
        out.append(slug)
    return out


def do_post_deploy_gc(dry: bool) -> int:
    """post-deploy 自動 GC：清『已穩定上站 ∧ 非在飛』書的 🟡 可免費重生產物（raw/chunk_*/ +
    chunks/），讓中間產物自己排水、免人工定期 prune。呼叫處（tick_once 序列尾 / reactive det
    worker）皆在 controller 進程內、已持 .tick.lock → 直呼 storage_gc.gc_book 不重取鎖（與手動
    prune 共用同一刪除核心）。安全閘全在 _gc_candidates（鏡像 prune：只動已上站、跳在飛）。"""
    from book_pipeline import storage_gc as sgc
    # 閘門：gc lane held → 跳過（涵蓋 tick_once/dry + 硬兜底；reactive 另有 pre-check 省 _start）。
    if not pg.gate_allows(None, 'gc', _active_gates()):
        if dry:
            log('post-deploy GC：gate hold → 跳過')
        return 0
    _gc_throttle['last'] = time.monotonic()  # 不論有無候選都重置節流戳（下次 ~INTERVAL 後再掃）
    cands = _gc_candidates(q._load_state())
    if not cands:
        return 0
    if dry:
        log(f'post-deploy GC（DRY）：{len(cands)} 本可清可重生產物 → '
            f'{", ".join(cands[:8])}' + (' …' if len(cands) > 8 else ''))
        return 0
    freed = 0
    for slug in cands:
        with _live_det_worker('gc', slug):  # /dev 面板顯「🔧 gc 處理中」
            f, warns = sgc.gc_book(slug)
        freed += f
        for w in warns:
            log(f'  ⚠ gc {slug}: {w}')
    log(f'post-deploy GC：回收 {freed / 1e9:.2f}GB（清 {len(cands)} 本已穩定上站書的可重生產物）')
    return 0


def tick(dry: bool, max_llm: int, no_deploy: bool) -> int:
    """派工入口：REACTIVE=1 且真跑 → 反應式控制迴圈；否則 → 現行單次 tick（行為不變）。"""
    if REACTIVE and not dry:
        return tick_reactive(no_deploy)
    return tick_once(dry, max_llm, no_deploy)


def tick_once(dry: bool, max_llm: int, no_deploy: bool) -> int:
    # 閘門快照：本 tick 各 dispatch 點 + advance_book 共享。**不再整 tick early-return**——改由各點
    # gate_allows 過濾（default=hold + allow 例外時，例外書/lane 仍須跑過 dispatch 才放行）；全 held
    # 時各點自然全 no-op（等價舊「暫停不做事」）。dry-run 一律印計畫，gate 如實反映在各點顯示誰被擋。
    _GATES_HOLDER['g'] = pg.load_gates()
    log(f'=== tick start (dry={dry}) ===')
    log('budget: ' + str(mb.status_report()))
    with _exhausted_lock:  # 每 tick 重置 provider 額度旗標（上個 tick 撞光的可能已 reset）
        _exhausted_providers.clear()
    if not dry:
        wr.reset()  # 清空 worker 註冊表（含上次崩潰殘留），本 tick 新工人重新登記

    # A. 收割已就緒 in-flight（uploading=False）：OCR 早已並行於雲端，並行 poll 組 unified。
    _harvest_parallel([s for s in sorted(mb.harvestable())
                       if pg.gate_allows(s, 'ingest', _active_gates())], dry)

    # B. 不同資源並行，不互堵：待ingest書 → detached 背景 upload（fire-and-forget，立刻返回、
    #    不被慢上傳堵住整 tick）；其餘書 → 主線程同時並行縱向 advance（LLM 與 upload 真並行）。
    rows = _sorted_rows()
    try:
        extract_cover.ensure_covers([r['slug'] for r in rows])
    except Exception as e:
        log(f'封面補抽異常（不影響派工）：{e}')
    occ = mb.occupied()
    ingest_slugs = [r['slug'] for r in rows
                    if r['todo'].split('(')[0] == 'ingest' and r['slug'] not in occ
                    and pg.gate_allows(r['slug'], 'ingest', _active_gates())]
    skip = set(ingest_slugs)
    for s in ingest_slugs:
        do_submit(s, dry)  # detached upload，立刻返回；早寫 manifest 防跨 tick 重提

    # B+C 並發：advance 既有書（LLM/audit/qc/sol_extract）與買書員下載合格書（zlib 下載，確定性）
    # 是**獨立資源池**，不該互等 → 同時跑。否則買書員卡在 audit barrier 後面 = 額度遲遲不消耗、
    # audit 慢就整批新書都不下載。各書 LLM 任務獨立無依賴 → advance 內部本就全並行。
    adv_slugs = [r['slug'] for r in rows if r['slug'] not in skip]
    if dry:
        _advance_parallel(adv_slugs, dry, no_deploy)
        crawled = do_crawl_tick(dry, rows)  # 買書員 drain（確定性、零 LLM，只下載既有 QUALIFIED）
    else:
        with ThreadPoolExecutor(max_workers=2) as bc:
            fb = bc.submit(_advance_parallel, adv_slugs, dry, no_deploy)
            fc = bc.submit(do_crawl_tick, dry, rows)
            cf.wait([fb, fc])
            if fb.exception():
                log(f'❌ phase B advance 異常：{fb.exception()}')
            crawled = [] if fc.exception() else (fc.result() or [])
            if fc.exception():
                log(f'❌ phase C crawl 異常：{fc.exception()}')
    # crawl 補到的新書並行 advance（triage→qc→ingest async submit）。
    if crawled and not dry:
        _advance_parallel(crawled, dry, no_deploy)

    # D. 再收割一輪已就緒書（剛 crawl→submit 的多半還在上傳/OCR，主要收 A 階段後翻 ready 的）。
    if not dry:
        _harvest_parallel([s for s in sorted(mb.harvestable())
                           if pg.gate_allows(s, 'ingest', _active_gates())], dry)
        # E. 對收割到 unified 的書再並行縱向推進（parse→audit→catalog→sol→deploy 一條龍）。
        _advance_parallel([r['slug'] for r in _sorted_rows()], dry, no_deploy)

    # F. corpus-level 數學 sweep（track-only reduce job，殘餘過門檻才派一隻跨書 agent）。
    do_math_sweep(dry)

    # F2. post-deploy 自動 GC：清已穩定上站書的 🟡 可重生中間產物（raw 解壓檔+chunks），
    #     免人工定期 prune。tick_once 已持 .tick.lock → gc_book 直跑不重取鎖。
    do_post_deploy_gc(dry)

    log('=== tick end ===')
    return 0


def tick_reactive(no_deploy: bool) -> int:
    """反應式控制迴圈（見頂部 REACTIVE 註解）。單一 controller 進程：每 cycle observe→把三條件
    齊備的 transition 派成 thread worker（不阻塞）→reap→sleep；牆鐘上限或排空即退出讓 launchd
    重拉。worker 同進程 → registry/exhaustion in-memory 共享；leases 防跨 crash orphan 子進程。"""
    log(f'=== reactive loop start (code={_code_version()} walltime={LOOP_WALLTIME}s poll={LOOP_POLL}s) ===')
    log('budget: ' + str(mb.status_report()))
    wr.reset()
    with _exhausted_lock:  # 本 controller 起頭重置額度旗標（下個 invocation 再重探恢復）
        _exhausted_providers.clear()
    _gc_throttle['last'] = time.monotonic()  # GC 節流戳重置 → 首掃於 ~GC_INTERVAL 後（系統穩定才掃）

    inflight: set[str] = set()
    ifl_lock = threading.Lock()
    wake = threading.Event()  # 工人完成即 set → 控制迴圈立刻重觀測，免等滿一個 LOOP_POLL
    ex = ThreadPoolExecutor(max_workers=LOOP_CONCURRENCY)

    def _start(key: str, fn) -> bool:
        """key 不在 inflight 才提交 worker；結束自動移出。回是否真的派了（同進程去重）。"""
        with ifl_lock:
            if key in inflight:
                return False
            inflight.add(key)

        def _run():
            try:
                fn()
            except Exception as e:  # 一本炸不連坐
                log(f'❌ worker {key} 異常：{e}')
            finally:
                with ifl_lock:
                    inflight.discard(key)
                wake.set()  # 某書某階段做完 → 其下游/同站別書可能即刻可派，喚醒迴圈
        ex.submit(_run)
        return True

    # 外部喚醒：SIGUSR1 → wake.set() → loop 立即 re-observe（devctl reload 手動觸發用，
    # 不殺在飛 worker）。signal.signal 須在主執行緒（tick_reactive 由 main 直呼，成立）。寫 pidfile
    # 供外部找到本 controller；非主執行緒/平台不支援則略過 → 退回 LOOP_POLL 節奏（功能降級不報錯）。
    import signal
    _terminating = threading.Event()  # SIGTERM/SIGINT → 快殺在飛子工後優雅排空退出
    try:
        signal.signal(signal.SIGUSR1, lambda *a: wake.set())
        _write_controller_state()
    except (ValueError, OSError):
        pass
    _install_term_handlers(wake, _terminating)  # SIGTERM/SIGINT：快殺在飛 LLM 子工 → 排空優雅退出

    deadline = time.monotonic() + LOOP_WALLTIME
    idle = 0
    paused_logged = False  # 暫停閘只 log 一次（per controller instance），避免每 poll 刷 log
    try:
        while time.monotonic() < deadline:
            # 終止信號（SIGTERM/SIGINT）：handler 已快殺在飛子工 → 直接排空退出。不 _schedule_respawn
            # （kickstart -k 由 launchd 自帶重拉；純 kill 由 StartInterval 兜底），避免雙重重拉。
            if _terminating.is_set():
                log('reactive loop：終止信號 → 在飛 LLM 子工已快殺、排空退出')
                break
            # 優雅 reload：收到請求即停派新工、跳出迴圈 → finally 的 ex.shutdown(wait=True) 排空在飛
            # worker（audit/advance 跑完才退）→ 進程退出，launchd 載入新碼。零浪費（對比 kick -k 硬殺）。
            if _reload_pending():
                _clear_reload()
                log('reactive loop：收到 reload → 停派新工、排空在飛 worker 後優雅退出（launchd 載新碼）')
                _schedule_respawn()  # 排程 detached re-kick：本進程排空退出即刻拉新碼（零空檔）
                break
            # 閘門快照：每 cycle observe 起頭讀一次（單寫手=本執行緒），各 dispatch 點 + worker 的
            # advance_book 共享同一份 → 一個 cycle 內判定一致、且 advance_book per-verb 讀到 ≤上 cycle 新鮮度。
            _g = pg.load_gates()
            _GATES_HOLDER['g'] = _g
            # **不在此 skip dispatch**：default==hold 時也要照跑 observe/dispatch，否則 allow 例外規則
            # （如「只推這 6 本」）永遠觸發不到。各 dispatch 點的 gate_allows 自會擋下未放行者 → 全 held
            # 時 dispatched==0、由下方 idle-exit 段的 gates_active 判保活（default hold=常態、不收斂退出，
            # gate 編輯 ≤LOOP_POLL 生效；default allow=正常運轉、沒派工就 idle-exit 靠 SIGUSR1/launchd 響應）。
            if pg.gates_active(_g):
                if not paused_logged:
                    log('reactive loop：gates default=hold（僅放行 allow 例外；無例外＝全 held + 保活輪詢）')
                    paused_logged = True
            elif paused_logged:
                log('reactive loop：default=allow → 全面恢復派工')
                paused_logged = False
            # observe：reap 租約（含 orphan kill）→ leased_slugs = 此刻有活 LLM 子進程的書
            leased_slugs = {r.get('slug') for r in leases.active(log=log)}
            dispatched = 0

            # A. 收割已就緒 in-flight OCR（IO poll，非阻塞 worker）。harvest 折進 ingest 同一閘鍵。
            for slug in sorted(mb.harvestable()):
                if not pg.gate_allows(slug, 'ingest', _g):
                    continue  # 該書 ingest 閘 held → 暫不收割（OCR 留雲端，放行後再收）
                if _start(f'harvest:{slug}', lambda s=slug: do_harvest(s, False)):
                    dispatched += 1

            # B. async 提交待 ingest（detached upload，立即返回）/ 縱向推進其餘書
            occ = mb.occupied()
            rows = _sorted_rows()
            # 封面冪等補抽：raw PDF 一落地即可生 cover.jpg（不必等 OCR），/dev 產線即時有封面。
            try:
                if extract_cover.ensure_covers([r['slug'] for r in rows]):
                    log('封面：補抽新書 cover.jpg')
            except Exception as e:
                log(f'封面補抽異常（不影響派工）：{e}')
            for r in rows:
                slug = r['slug']
                verb = r['todo'].split('(')[0]
                if verb == 'ingest':
                    if slug not in occ and pg.gate_allows(slug, 'ingest', _g):
                        do_submit(slug, False)
                    continue
                if slug in leased_slugs:
                    continue  # 前一 controller crash 的 orphan 子進程還在做這本 → 等 reap/kill
                # 只剩可選(translate)／無工作(—) → 不派 worker：advance_book 本就 early-return，
                # 省一條空轉 thread + 一次 assess。有任一必做 token（audit/parse/catalog_audit/
                # sol_extract/deploy）才推進 → live 工人數＝真有事做的書數，不再一書一空轉 worker。
                acts = [t for t in r['todo'].split() if t and t != '—' and not t.endswith('(可選)')]
                if not acts:
                    continue
                # 下一閘 held → 不派 thread（省一條空轉 worker，免 dispatched++ 卡 idle 收斂）。
                # 真 enforcement 仍在 advance_book 內逐 verb 兜底（多步推進中途被擋亦停）。
                if not pg.gate_allows(slug, acts[0].split('(')[0], _g):
                    continue
                if _start(f'advance:{slug}', lambda s=slug: advance_book(s, False, no_deploy)):
                    dispatched += 1

            # C1. 買書員（確定性、非 agent、無退避）：解析池有 ready ∧ 額度 ∧ pipeline 有空間 → 派確定性
            # drain worker 直接 select_next 取 ready 抓+落 raw_pdfs（無 LLM、不註冊 worker_registry → 不顯示
            # 為 crawl agent；__crawl_drain__ 序列化）。無 buffer/refill：下載候選即時從解析池推導。額度0/無
            # ready/pipeline 滿 → _drain_due False → 不派 → loop 能 idle 收斂（額度經快取判斷，免 subprocess）。
            if pg.gate_allows(None, 'crawl', _g) and _drain_due(rows) \
                    and _start('__crawl_drain__', lambda r=rows: drain_crawl_queue(r)):
                dispatched += 1

            # C3. corpus-level 數學 sweep（track-only）：residual_unaccepted>0 且非 fixpoint 才跑。**無無頭
            # agent**——do_math_sweep 直接 det-step 跑 math_sweep batch（純自架 API：有多少壞式就 batch 解
            # 多少，非以書為單位）。__math_sweep__ key 自動序列化（在跑時不疊派）；fixpoint/converged → due
            # False → loop 能收斂 idle。
            due, _resid = _math_sweep_due()
            if due and pg.gate_allows(None, 'math_sweep', _g) \
                    and _start('__math_sweep__', lambda: do_math_sweep(False)):
                dispatched += 1

            # C3.5 post-deploy 自動 GC（det-step）：節流到位才掃（免每 cycle 掃全書）。清已穩定
            # 上站書的 🟡 可重生中間產物，讓中間產物自己排水。__post_deploy_gc__ key 序列化、
            # gc_book 在 controller 進程內已持 .tick.lock。candidates 空 → 一次掃即收斂、不空轉。
            if pg.gate_allows(None, 'gc', _g) and _gc_due() \
                    and _start('__post_deploy_gc__', lambda: do_post_deploy_gc(False)):
                dispatched += 1

            # 排空收斂：連續 LOOP_IDLE_ROUNDS 輪「無新派工 ∧ 無在跑 ∧ 無 in-flight OCR」才收工。
            # occ 非空＝有書 OCR 在雲端排隊，harvestable() 隨時會翻就緒 → 不可進 idle 提早退出
            # （否則只剩待收 OCR 時提早收工，正是本架構要消除的 work-conservation 違反）。
            with ifl_lock:
                busy = len(inflight)
            if dispatched == 0 and busy == 0 and not occ:
                # gates default=hold（gates_active）= 常態暫停、非「無事做」→ **保活輪詢、不累計 idle**，
                # 讓 gate 編輯 ≤LOOP_POLL 生效（subsume 舊 pause 的「暫停不收斂退出」語意）。default=allow
                # 才正常 idle-exit（沒派工＝真沒事）、靠 SIGUSR1/launchd 響應後續 gate 編輯。
                if pg.gates_active(_g):
                    idle = 0
                else:
                    idle += 1
                    if idle >= LOOP_IDLE_ROUNDS:
                        log(f'reactive loop：連 {idle} 輪無工作且無 in-flight OCR → 排空收工（launchd 下次重拉）')
                        break
            else:
                idle = 0
            # 事件驅動：工人一完成即被喚醒重觀測（transition 延遲從「滿一個 poll」塌到一次
            # observe，~0.04s；observe 自 sol_stats 快取後已廉價）；無事件則睡滿 LOOP_POLL
            # 當 OCR 輪詢/外部變更的上限節奏。
            wake.wait(LOOP_POLL)
            wake.clear()
    finally:
        _clear_controller_state()  # 退出即撤 statefile → 外部改走 kick 起新 controller
        # 分流排空（取代舊「一律 120s 上限、逾時快殺」——那正是 rc=-9 集體死亡的源頭：reload 時
        # 把跑了 10–40min 的 audit 在 120s 攔腰 SIGKILL）：
        #   ① 可殺的子進程 agent（_inflight_children 非空）→ **無限等其自然收尾、永不砍**。codex 主力
        #      無「自我空轉迴圈」病理、必然收斂；reload/walltime 退出對真 agent 完全無害。
        #   ② 無任何子進程、只剩純 thread worker（math sweep HTTP / det subprocess，killpg 殺不掉）→
        #      套 DRAIN_BOUND 逃生，逾時 os._exit。純 API thread 會凍結 controller + 續持 .tick.lock
        #      成孤兒鎖（見 orphan-lock memory），故唯此情形需強退。被棄 thread 產物可 disk 重導、
        #      下個 controller 冪等重派，強退安全。
        log(f'reactive loop：排空在飛 worker（子進程 agent 無限等、純 thread 上限 {DRAIN_BOUND}s）…')
        ex.shutdown(wait=False)  # 不再接新、不阻塞
        _bound_started = None  # 只在「無子進程、只剩純 thread」期間計時；有子進程即 reset
        while True:
            with ifl_lock:
                n_ifl = len(inflight)
            if n_ifl == 0:
                break
            with _inflight_lock:
                n_child = len(_inflight_children)
            if n_child > 0:
                _bound_started = None  # 有可殺子工在跑 → 無限等
                time.sleep(0.5)
                continue
            now_m = time.monotonic()  # 只剩純 thread → 起算 DRAIN_BOUND
            if _bound_started is None:
                _bound_started = now_m
            if now_m - _bound_started >= DRAIN_BOUND:
                break
            time.sleep(0.5)
        with ifl_lock:
            _stuck = len(inflight)
        if _stuck == 0:
            log('reactive loop：在飛 worker 已排空，優雅退出')
        else:
            # 走到這 = 只剩純 thread worker 卡 DRAIN_BOUND（子進程 agent 已全部自然收尾）→ os._exit 逃生
            _killed = _kill_inflight_children()  # 通常 0（純 thread 無子進程可殺）；保險一擊
            log(f'reactive loop：純 thread worker 排空逾時 {DRAIN_BOUND}s → 棄置 {_stuck} 個（殺 {_killed} 子工）'
                '，os._exit 強退（產物 disk 重導、下個 controller 重派）')
            sys.stdout.flush()
            os._exit(0)  # 唯一能停掉卡死純 API thread 的手段；flock 隨進程死釋放、respawn 小弟接手
    # 在飛 worker 已排空（上面 drain 完成）→ 此處 main thread 獨佔，安全做貴重成果 auto-commit。
    # 唯 os._exit 硬退路徑跳過（卡死 worker 可能正寫 override → 不冒半寫風險，下個 controller 退出時補）。
    if not no_deploy:
        commit_artifacts()
    log('=== reactive loop end ===')
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
