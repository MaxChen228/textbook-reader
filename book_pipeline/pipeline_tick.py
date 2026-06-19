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
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from book_pipeline import pipeline_queue as q
from book_pipeline import mineru_budget as mb
from book_pipeline import worker_registry as wr
from book_pipeline import agent_history as hist
from book_pipeline import leases
from book_pipeline import extract_cover
from book_pipeline import booklists
from book_pipeline import scope_guard

ROOT = q.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
LOCK = os.path.join(BP, '.tick.lock')
LOG = os.path.join(BP, 'reports', 'daemon.log')
STAGES_PATH = os.path.join(ROOT, 'dev', 'stages.json')  # live 階段快訊（單卡即時，繞 status.json 8s 節流）
READER_ROOT = q.READER_ROOT
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
# codex 派工後端：headless `codex exec --json`。兩條 codex provider：
#   codex      = 原生 OAuth（~/.codex/auth.json，codex login ChatGPT 訂閱）
#   codex-pool = ccNexus 池子（codex -p nexus + CCNEXUS_API_KEY；maxn970228 token 輪換、
#                與原生 codex 不同帳號＝獨立額度）。profile 在兩機 ~/.codex/nexus.config.toml。
# 模型/effort/chain/timeout 全收斂進「派工配置層」（DispatchSpec + DEFAULT_DISPATCH/
# STAGE_DISPATCH + _resolve_dispatch，見下），非散落於此。
CODEX_BIN = os.environ.get('CODEX_BIN', 'codex')
# headless LLM 派工的 wall-clock 上限（秒）。逾時殺整個子工 process group，避免單一
# audit 的子 agent 陷入迴圈時拖死整個 daemon（曾見 kimi audit 重讀 content_list 卡 6.5h）。
# 正常 audit ~25min；1h 留足餘裕（重書 smoke 迭代偶逼近 40min），只在真卡死時觸發。env 可覆寫。
LLM_TIMEOUT = int(os.environ.get('BOOK_PIPELINE_LLM_TIMEOUT', '3600'))
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
# 把爬速綁定消化速。2026-06 簡化後**唯一**爬書水位——買書員每 tick 直接 select_next 取解析池待下載書、
# 並行抓，無購物清單 buffer（buffer 唯一不可推導的下載失敗計數已移 pipeline_state.json：見 q.crawl_fail_*）。
CRAWL_INFLIGHT_CAP = int(os.environ.get('BOOK_PIPELINE_CRAWL_INFLIGHT_CAP',
                                        os.environ.get('BOOK_PIPELINE_CRAWL_HIGH', '20')))
# 解析池水位（已確認 z-lib 連結、未 owned = READY）：低於此就派 crawl agent 解析更多 unresolved，
# 讓「已確認連結可抽」的書常住 ≥ 此數，買書員永遠有貨。解析由 LLM agent 判斷（規則會假陽性）。
CRAWL_POOL_LOW = int(os.environ.get('BOOK_PIPELINE_CRAWL_POOL_LOW', '100'))
CRAWL_RESOLVE_BATCH = int(os.environ.get('BOOK_PIPELINE_CRAWL_RESOLVE_BATCH', '20'))  # 每隻 crawl agent 單批解析本數
# 數學 sweep 每 tick 上限 + 輪數 + 並發 worker：do_math_sweep 跑 `math_sweep batch --limit L --workers W
# --rounds 1`。8 worker 並發各打一批 LLM（每批 ≈3-5 分），limit=workers×n 餵滿全部 worker → 一 tick
# 牆鐘 ≈ 單批時間就清掉 ~W×n 條（過去序列要 W 倍時間）。**完成即記 last_batch、occ 階梯下降、上站**。
# rounds=1 不在 tick 內重試——失敗條下個 tick re-list 自然重試。walltime 安全（並發不拉長單 tick 牆鐘）。
MATH_BATCH_WORKERS = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_WORKERS', '8'))
MATH_BATCH_N = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_N', '40'))
MATH_BATCH_LIMIT = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_LIMIT',
                                      str(MATH_BATCH_WORKERS * MATH_BATCH_N)))  # 餵滿 8 worker
MATH_BATCH_ROUNDS = int(os.environ.get('BOOK_PIPELINE_MATH_BATCH_ROUNDS', '1'))
DATA_DIR = os.path.join(BP, 'mineru_data')
MAX_FETCH_FAILS = int(os.environ.get('BOOK_PIPELINE_MAX_FETCH_FAILS', '3'))  # 同本連續 fetch 失敗達此 → 排除出下載候選
# harvest poll 上限（秒）：OCR 全好的書第一次 poll 就秒收；沒好的書等到此上限就放棄、留
# in-flight 下個 tick 再收。**短**值＝非阻塞（不等 OCR 跑完凍住 tick）。OCR 是 async、
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
DRAIN_BOUND = int(os.environ.get('BOOK_PIPELINE_DRAIN_BOUND', '120'))           # 退出排空在飛 worker 的上限秒數，逾時快殺+強退（防無上限 drain 凍結/孤兒鎖）
# live reactive controller 的 statefile（JSON {pid, sha, started}）：loop 起頭寫、退出即刪。
#   pid → 外部送 SIGUSR1 喚醒（reload）；sha → 此 controller 載入的 git 版本，供
#   「daemon 跑的是哪版碼、離 HEAD 多遠」即時觀測（免上線後做 forensics）。per-machine、gitignore。
CONTROLLER_STATE = os.path.join(BP, '.controller.json')
# reload 請求 marker：`devctl reload` 丟它 + SIGUSR1 → loop **排空在飛 worker 後優雅退出**、launchd 載
# 入新碼（零浪費；對比 kick -k 硬殺跳過 finally 會棄工作）。SIGUSR1 語意＝「醒來看 reload marker / 重觀測」。
RELOAD_REQUEST = os.path.join(BP, 'reload_request')

# LLM 階段 → headless claude 任務描述（指向既有 skill/reference）。
# 註：crawl **選書**仍確定性（書單 SoT + select_next），但 crawl **解析**（書名→z-lib id/hash）需判斷
# → 改回 LLM agent（'crawl' 條目，見 do_crawl_resolve / references/crawl.md）；買書員 drain 仍確定性。
# 曾試圖把解析也確定性化（信心門檻自動配），假陽性太多（Chemistry→Food Chemistry、Gallian 題解→Dummit）。
LLM_PROMPTS = {
    'crawl': (
        "你是 book_pipeline 的 **crawl 解析 agent**：替書單 target 在 z-library 找出『正是這本書』的 "
        "id/hash，或判定 absent/review。**嚴格遵照 .claude/skills/book-pipeline/references/crawl.md**"
        "（工具、判準、proposal 管道、硬邊界全在那）。**只 search、不下載、不選書、不碰額度。** "
        "本批要解的 target slug（逗號分隔）：{slug}。逐本：target → search →（歧義時 inspect）→ 判斷 → commit。"
        "advisory_conf 只是提示、非裁決；寧缺勿錯——可疑就 absent/review，別 commit 錯的。"),
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


# ── live 階段快訊（dev/stages.json）────────────────────────────────────────────
# 書一轉換階段就把 {slug: stage} 寫進極小檔（不走 status.json 的 8s 全量節流），供 /dev 以
# ~1.5s cadence 即時撿出單卡階段變化 + 閃示。**只寫 live 階段、不碰 timeline 歷史**（歷史單一
# 寫手仍是 60s devsnapshot，見 devctl）→ controller 舊碼寫此檔不引入版本歪斜（與它本就在寫的
# status.json live books[].stage 同性質）。controller 是此檔唯一寫手；前端用 generated_at_utc 守新舊。
_stage_map: dict[str, str] = {}
_stage_lock = threading.Lock()


def _publish_stages(pairs) -> None:
    """更新 in-memory 階段表，有變動才原子寫出 dev/stages.json。pairs = [(slug, stage), …]，冪等。"""
    with _stage_lock:
        changed = False
        for slug, stage in pairs:
            if stage and _stage_map.get(slug) != stage:
                _stage_map[slug] = stage
                changed = True
        if not changed:
            return
        snap = {'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'stages': dict(_stage_map)}
    try:
        os.makedirs(os.path.dirname(STAGES_PATH), exist_ok=True)
        tmp = STAGES_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, STAGES_PATH)  # 原子替換，前端永不讀到半截
    except Exception:
        pass


def emit_stage(slug: str, stage: str) -> None:
    """單書階段轉換的最早可知點即時發佈（advance_book 每步呼叫；同階段不重寫）。"""
    _publish_stages([(slug, stage)])


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


def _is_codex(provider: str) -> bool:
    """codex 家族（原生 OAuth + 池子）：共用 codex CLI 命令骨架、event schema、限額 markers。"""
    return provider in ('codex', 'codex-pool')


# ── 派工配置層（單一真相源）────────────────────────────────────────────────
# 每個 LLM stage「怎麼派」收斂成一個 DispatchSpec：provider failover 優先序、各家模型、
# codex reasoning effort、timeout。三層合併（_resolve_dispatch）：
#   DEFAULT_DISPATCH ← STAGE_DISPATCH[verb]（per-stage 覆寫）← env（運維臨時拉桿，最高）。
# 新增可操縱維度＝擴 DispatchSpec 一個欄 + _build_llm_cmd 消費它，無第三處散落。
KNOWN_PROVIDERS = ('codex-pool', 'codex', 'kimi', 'claude')


@dataclass(frozen=True)
class DispatchSpec:
    """單一 stage 的派工配置。欄位 None＝繼承上層 / 不帶該旗標。"""
    chain: tuple[str, ...] | None = None   # provider failover 優先序
    codex_model: str | None = None         # codex 家族模型（gpt-5.x；池子白名單見下）
    codex_effort: str | None = None        # codex reasoning effort（low/medium/high）
    claude_model: str | None = None        # claude 模型（kimi 由 _llm_env 寫死，不適用）
    timeout: int | None = None             # 派工 wall-clock 上限（秒）


# 全域底：chain＝優先榨池子（maxn970228 獨立額度）→ 原生 codex → kimi → Claude Max 保底。
# codex 家族預設 gpt-5.4（池子白名單 gpt-5.5/5.4/5.4-mini/5.3-codex-spark 內，需 ccNexus
# fork 透傳修復在線才切得動非 5.5）。timeout 1h：正常 audit ~25min，留卡死護欄餘裕。
DEFAULT_DISPATCH = DispatchSpec(
    chain=('codex-pool', 'codex', 'kimi', 'claude'),
    codex_model='gpt-5.4',
    timeout=3600,
)
# per-stage 覆寫（只列偏離預設者；未列 stage 全走 DEFAULT_DISPATCH）。reasoning effort 分層：
# 重判斷（audit/catalog_audit/sol_extract）high、解析（crawl）medium、視覺 qc low。
# 註：math_sweep 已純 API 化（do_math_sweep 直跑 batch、不派 LLM）→ 不入此表。
STAGE_DISPATCH: dict[str, DispatchSpec] = {
    'audit':         DispatchSpec(codex_effort='high'),
    'catalog_audit': DispatchSpec(codex_effort='high'),
    'sol_extract':   DispatchSpec(codex_effort='high'),
    'crawl':         DispatchSpec(codex_effort='medium'),
    'qc':            DispatchSpec(codex_effort='low'),
}


def _merge(base: DispatchSpec, over: DispatchSpec | None) -> DispatchSpec:
    """over 的非 None 欄覆寫 base。"""
    if over is None:
        return base
    return replace(base, **{k: v for k, v in vars(over).items() if v is not None})


def _env_override(spec: DispatchSpec) -> DispatchSpec:
    """env 運維臨時拉桿凌駕（最高優先）。未設的 env 不動該欄。"""
    ch = os.environ.get('BOOK_PIPELINE_PROVIDER_CHAIN', '').strip()
    if ch:
        parsed = tuple(p.strip().lower() for p in ch.split(',') if p.strip())
        if parsed:
            spec = replace(spec, chain=parsed)
    for env_key, field in (('BOOK_PIPELINE_CODEX_MODEL', 'codex_model'),
                           ('BOOK_PIPELINE_CODEX_EFFORT', 'codex_effort'),
                           ('BOOK_PIPELINE_CLAUDE_MODEL', 'claude_model')):
        v = os.environ.get(env_key)
        if v:
            spec = replace(spec, **{field: v})
    to = os.environ.get('BOOK_PIPELINE_LLM_TIMEOUT')
    if to:
        spec = replace(spec, timeout=int(to))
    return spec


def _resolve_dispatch(verb: str) -> DispatchSpec:
    """三層合併 → fully-resolved spec：DEFAULT ← STAGE_DISPATCH[verb] ← env。"""
    spec = _env_override(_merge(DEFAULT_DISPATCH, STAGE_DISPATCH.get(verb)))
    unknown = [p for p in (spec.chain or ()) if p not in KNOWN_PROVIDERS]
    if unknown:
        log(f'⚠ provider chain 含未知 provider {unknown}（合法：{KNOWN_PROVIDERS}）'
            f' → 將走 claude CLI 預設分支，恐非預期')
    return spec


def _llm_env(provider: str) -> dict | None:
    """指定 provider 的派工環境。kimi → 把同一個 claude CLI（harness 不變）導到 Kimi Code
    端點（key 讀 ~/.secrets/kimi.env，不進全域 env）。codex-pool → 注入 dummy CCNEXUS_API_KEY
    （codex 要求 env_key 非空才肯走 nexus profile）。claude/codex → None（claude 沿用 Claude
    Max 訂閱；codex 用自己的 ~/.codex/auth.json）。"""
    if provider == 'codex-pool':
        env = dict(os.environ)
        env.setdefault('CCNEXUS_API_KEY', 'unused')
        return env
    if provider != 'kimi':
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


# 撞額度的 provider（本 tick 內標記，跨並行子工共享 → 不再重複撞同一耗盡的 provider）。
# 每 tick 開頭清空重試。
_exhausted_providers: set[str] = set()
_exhausted_lock = threading.Lock()
# claude/kimi 撞 5h 滾動窗會吐這些字串、秒退；codex 撞 ChatGPT 訂閱限額吐 rate/quota 類訊息。
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
    # claude/kimi stream-json：終端 result 事件帶 is_error / 非 success subtype 才算錯誤面
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
    claude/kimi：同一個 claude CLI（kimi 僅由 _llm_env 換後端），走 stream-json；claude 可由
    spec.claude_model 帶 --model（kimi 不帶，靠 _llm_env 的 ANTHROPIC_MODEL）。
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
    # claude/kimi stream-json
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
    kimi＝provider 名（後端固定 kimi-for-coding）。"""
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
    （逾時自有 kill+下個 tick 重派處理，不在此 failover）。slug 此處即 dispatch_llm 傳入的識別/lease 鍵。"""
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
    timeout = spec.timeout or LLM_TIMEOUT
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
            log(f'⏱ TIMEOUT {timeout}s → 殺子工 process group（pid={p.pid}）；下個 tick 重派')
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


def dispatch_llm(todo_verb: str, slug: str | None, dry: bool, label: str | None = None) -> int:
    """派 headless LLM 跑階段，沿 provider 鏈 failover。回 rc；-2 = 全鏈不可用 → 呼叫端 defer。
    派工策略（chain/model/effort/timeout）由 _resolve_dispatch(verb) 解析（DEFAULT←STAGE←env）。
    **failover 觸發二類**（_run_one 回 reason）：① 撞額度（limit）② 服務中斷（outage：provider 零事件
    /5xx/連線錯——外部掛了，非任務失敗）。兩者皆標記 provider 本 tick exhausted（並行子工不再重撞死池）、
    換下一個 provider **重跑同一任務**（不浪費派工）；全鏈耗盡才 -2。**「有事件但 rc≠0」= agent 真跑了卻
    任務失敗 → 不 failover**（換 provider 無益且恐雙寫），rc 交回呼叫端。
    label：lease/registry/hist 的顯示鍵（預設 = slug）；crawl 解析傳 '__crawl_resolve__' 當穩定 singleton
    鍵，真正 batch slug 只進 prompt（見 do_crawl_resolve）。
    （crawl 選書已不派 LLM——改書單 SoT + 確定性 resolver；本函式服務 qc/audit/sol_extract/crawl 解析。）"""
    prompt = LLM_PROMPTS[todo_verb].format(slug=slug or '')
    key = label or slug  # lease/registry/hist 身分鍵；prompt 已由真 slug 建好，key 只管識別/序列化
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
    log(f'❌ 全 provider 不可用 {chain}（試過 {tried}）→ defer {todo_verb} {key or ""}，下個 tick 重試')
    return -2


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
    rc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).returncode
    if rc == 0 and os.path.isfile(os.path.join(ROOT, 'raw_pdfs', f'{slug}.pdf')):
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
    """**買書員（確定性，非 agent）**：每 tick **直接 select_next 取解析池 ready 書**並行抓、抓到落
    raw_pdfs（隨即成 owned，下個 observe 自然接手 ingest）。無購物清單 buffer——下載候選即時從
    (解析池 ∖ owned ∖ 失敗達上限) 推導。**額度只在這裡咬**，與 LLM 無關、無退避。連 MAX_FETCH_FAILS
    次失敗的書記進 pipeline_state（q.bump_crawl_fail），select_next 以 exclude 排除、不卡隊頭。
    回本輪抓到的 slug。"""
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
    if not batch:
        log('crawl 買書員：解析池暫無 ready 可下載（全 owned/unresolved/review/absent 或已失敗達上限）')
        return []
    for i, b in enumerate(batch):
        b['account'] = slots[i]
    log(f'crawl 買書員：解析池取 {len(batch)} 本下載（額度槽 {len(slots)}、pipeline 餘裕 {room}）')
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
            else:
                fails = q.bump_crawl_fail(b['slug'])     # 失敗 +1，達上限後 select_next 自動排除
                if fails >= MAX_FETCH_FAILS:
                    log(f'crawl drop：{b["slug"]} 連 {fails} 次 fetch 失敗 → 排除出下載候選（架構師可重解後重試）')
    log(f'crawl 買書員 done：抓到 {len(ok)}/{len(batch)}')
    if crawled:
        hist.set_touched('crawl_plan', crawled)  # 帶進的書 → 各書抽屜查得此爬書歷程
        try:
            from book_pipeline import devctl
            devctl.invalidate_zlib_cache()  # 剛花額度 → 失效快取，下個 snapshot 反映 live 餘額
        except Exception:
            pass
    return crawled


def _crawl_resolve_due() -> tuple[bool, dict]:
    """**crawl 解析 agent** 是否該派：解析池 confirmed（= READY = 已確認連結、未 owned）< CRAWL_POOL_LOW
    ∧ 仍有 unresolved target 可解。回 (due, pool_counts)。池夠滿或無 unresolved → 不派 → loop 能 idle
    收斂（不 busy-loop）。**provenance 不在此判**：`status_of` 已把 legacy 無 by 解析降級成 UNRESOLVED →
    confirmed 天然只含現役演算法解出者、被降級的 legacy 自動進 unresolved 重解（stale cache 自動失效）。"""
    pc = booklists.pool_counts()
    return (pc['confirmed'] < CRAWL_POOL_LOW and pc['unresolved'] > 0), pc


def do_crawl_resolve(dry: bool) -> int:
    """解析池低於水位 → 派一隻 **crawl agent** 解析一批 unresolved target（書名→z-lib id/hash 判斷）。
    agent 用 resolve.py 的 target/search/inspect/commit 工具親自查/挑（確定性配對會假陽性：Chemistry→
    Food Chemistry、Gallian 題解→Dummit，故交 LLM 判斷）。單隻在飛——reactive loop 的 __crawl_resolve__
    key 自動序列化：本批 commit 後、下個 cycle 池仍低才派下一批，不並發撞同批。回 dispatch_llm rc（dry→0）。"""
    due, pc = _crawl_resolve_due()
    if not due:
        return 0
    batch = [t['slug'] for t in booklists.unresolved_targets()[:CRAWL_RESOLVE_BATCH]]
    if not batch:
        return 0
    log(f'crawl 解析：解析池 {pc["confirmed"]}/{CRAWL_POOL_LOW}'
        f'（unresolved {pc["unresolved"]}，含被降級的 legacy）→ 派 crawl agent 解析 {len(batch)} 本')
    # label 固定 '__crawl_resolve__'：當 lease/registry/hist 的穩定 singleton key（真正的 batch slug
    # 列表只進 prompt）→ lease 不再用整批 slug 串成超長檔名、/dev 不顯示怪 slug、跨 crash 同一穩定鍵。
    return dispatch_llm('crawl', ','.join(batch), dry, label='__crawl_resolve__')


def do_crawl_tick(dry: bool, rows: list[dict]) -> list[str]:
    """oneshot tick 的 crawl 編排：**買書員 drain**（確定性，直接從解析池取 ready 下載）。
    無 buffer、無 refill 步驟——下載候選即時從解析池推導。reactive loop **不**走此函數——它把
    drain / crawl 解析當獨立 due-gated 步驟分派（C1 買書員每 cycle、C4 解析 agent）。回 drain 抓到的 slug。"""
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
    下個 tick 再收——**絕不等 OCR 跑完而凍住整 tick**（曾因預設 30min 等待把整條鏈卡死，
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
        log(f'ingest harvest {slug}：OCR 尚未全完成（rc={rc}）→ 留 in-flight，下個 tick 再收')
    else:
        log(f'❌ ingest harvest {slug}：rc={rc}')
    return rc


def do_parse(slug: str, dry: bool) -> int:
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
    鐵律：**只 stage 白名單路徑、絕不 `git add -A`**——盲加會把 gitignore 的漏（stages.json /
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
    rc = subprocess.run(build, cwd=READER_ROOT).returncode
    # 只在 build 成功且 book.json 真的烤出才標已部署；否則留待下個 tick 重試（不誤標 done）。
    book_json = os.path.join(READER_ROOT, 'data', slug, 'book.json')
    if rc == 0 and os.path.isfile(book_json):
        q.mark_deployed(slug)
        log(f'deploy {slug} ✓：book.json 已烤出，上站')
        do_math_track(slug)  # 上站即量數學殘餘（track-only，best-effort，不影響 deploy rc）
    else:
        log(f'❌ deploy {slug}：build rc={rc}，book.json={"有" if os.path.isfile(book_json) else "無"} → 不標 deployed，下個 tick 重試')
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
    回 0=完成/跳過、1=batch 基礎設施失敗（node/ccnexus，不記狀態 → 下個 tick 重試）。"""
    from book_pipeline import math_validate as mv
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
    try:
        # stdout=PIPE 取 JSON 結果；stderr 直通（_log 進度走 stderr）→ launchd.err.log 即時可見，不被吞。
        proc = subprocess.run(['uv', 'run', 'python', '-m', 'book_pipeline.math_sweep', 'batch',
                               '--limit', str(MATH_BATCH_LIMIT), '--workers', str(MATH_BATCH_WORKERS),
                               '--n', str(MATH_BATCH_N), '--rounds', str(MATH_BATCH_ROUNDS), '--verbose'],
                              cwd=READER_ROOT, stdout=subprocess.PIPE, stderr=None, text=True)
    finally:
        q.clear_math_batch_running()
    res = {}
    try:
        if proc.stdout.strip():
            res = json.loads(proc.stdout.strip())  # stdout 全部 = batch 的 JSON 結果（indent=2 多行，進度走 stderr）
    except Exception:
        pass
    if proc.returncode != 0 or not res.get('ok'):
        err = res.get('error') or f'rc={proc.returncode}（進度/錯誤詳見 launchd.err.log）'
        log(f'❌ math sweep batch 失敗（{err}）→ 不記狀態，下個 tick 重試')
        return 1
    log(f"math sweep batch：解 {res.get('accepted', 0)} 條 · 觸 {res.get('books_touched', 0)} 書 · "
        f"剩 {res.get('still_failing', 0)} 條硬殘")
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
        emit_stage(slug, stage)  # live 階段快訊：轉換最早可知點即時發佈（冪等）
        todo = row.get('todo', '—')
        # todo 可能多項空白分隔，含 (可選) 非阻塞項（已部署/已 accept 的 catalog、已部署的 sol、translate）。
        # 取第一個「非可選」項當下一步動作；全可選/無 → 本書收工。**不可**用 todo.split('(')[0]：
        # 多項時會抓到可選前綴項（如 catalog_audit(可選)）→ 對已 accept 的 catalog 每輪重跑空轉。
        actionable = [t for t in todo.split() if t not in ('—', '') and not t.endswith('(可選)')]
        if not actionable or stage.startswith(('R', 'X')):
            return
        verb = actionable[0].split('(')[0]
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
    _publish_stages([(r.get('slug', ''), r.get('stage', '') or '') for r in rows])  # 每 observe 全書階段快訊
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
    """派工入口：REACTIVE=1 且真跑 → 反應式控制迴圈；否則 → 現行單次 tick（行為不變）。"""
    if REACTIVE and not dry:
        return tick_reactive(no_deploy)
    return tick_once(dry, max_llm, no_deploy)


def tick_once(dry: bool, max_llm: int, no_deploy: bool) -> int:
    log(f'=== tick start (dry={dry}) ===')
    log('budget: ' + str(mb.status_report()))
    with _exhausted_lock:  # 每 tick 重置 provider 額度旗標（上個 tick 撞光的可能已 reset）
        _exhausted_providers.clear()
    if not dry:
        wr.reset()  # 清空 worker 註冊表（含上次崩潰殘留），本 tick 新工人重新登記

    # A. 收割已就緒 in-flight（uploading=False）：OCR 早已並行於雲端，並行 poll 組 unified。
    _harvest_parallel(sorted(mb.harvestable()), dry)

    # B. 不同資源並行，不互堵：待ingest書 → detached 背景 upload（fire-and-forget，立刻返回、
    #    不被慢上傳堵住整 tick）；其餘書 → 主線程同時並行縱向 advance（LLM 與 upload 真並行）。
    rows = _sorted_rows()
    try:
        extract_cover.ensure_covers([r['slug'] for r in rows])
    except Exception as e:
        log(f'封面補抽異常（不影響派工）：{e}')
    occ = mb.occupied()
    ingest_slugs = [r['slug'] for r in rows
                    if r['todo'].split('(')[0] == 'ingest' and r['slug'] not in occ]
    skip = set(ingest_slugs)
    for s in ingest_slugs:
        do_submit(s, dry)  # detached upload，立刻返回；早寫 manifest 防跨 tick 重提

    # B+C 並發：advance 既有書（LLM/audit/qc/sol_extract）與 crawl 補新書（zlib 下載 + 確定性 refill）
    # 是**獨立資源池**，不該互等 → 同時跑。否則 crawl 卡在 audit barrier 後面 = 額度遲遲不消耗、
    # audit 慢就整批新書都不爬。各書 LLM 任務獨立無依賴 → advance 內部本就全並行。
    adv_slugs = [r['slug'] for r in rows if r['slug'] not in skip]
    if dry:
        _advance_parallel(adv_slugs, dry, no_deploy)
        crawled = do_crawl_tick(dry, rows)  # 買書員 drain（劃掉）+ 低水位才 refill（確定性、零 LLM）
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
        _harvest_parallel(sorted(mb.harvestable()), dry)
        # E. 對收割到 unified 的書再並行縱向推進（parse→audit→catalog→sol→deploy 一條龍）。
        _advance_parallel([r['slug'] for r in _sorted_rows()], dry, no_deploy)

    # F. corpus-level 數學 sweep（track-only reduce job，殘餘過門檻才派一隻跨書 agent）。
    do_math_sweep(dry)

    # G. crawl 解析 agent（解析池 < 水位 ∧ 有 unresolved → 派一隻解析一批書名→id/hash）。
    do_crawl_resolve(dry)

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
            # observe：reap 租約（含 orphan kill）→ leased_slugs = 此刻有活 LLM 子進程的書
            leased_slugs = {r.get('slug') for r in leases.active(log=log)}
            dispatched = 0

            # A. 收割已就緒 in-flight OCR（IO poll，非阻塞 worker）
            for slug in sorted(mb.harvestable()):
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
                    if slug not in occ:
                        do_submit(slug, False)
                    continue
                if slug in leased_slugs:
                    continue  # 前一 controller crash 的 orphan 子進程還在做這本 → 等 reap/kill
                # 只剩可選(translate)／無工作(—) → 不派 worker：advance_book 本就 early-return，
                # 省一條空轉 thread + 一次 assess。有任一必做 token（audit/parse/catalog_audit/
                # sol_extract/deploy）才推進 → live 工人數＝真有事做的書數，不再一書一空轉 worker。
                if not [t for t in r['todo'].split() if t and t != '—' and not t.endswith('(可選)')]:
                    continue
                if _start(f'advance:{slug}', lambda s=slug: advance_book(s, False, no_deploy)):
                    dispatched += 1

            # C1. 買書員（確定性、非 agent、無退避）：解析池有 ready ∧ 額度 ∧ pipeline 有空間 → 派確定性
            # drain worker 直接 select_next 取 ready 抓+落 raw_pdfs（無 LLM、不註冊 worker_registry → 不顯示
            # 為 crawl agent；__crawl_drain__ 序列化）。無 buffer/refill：下載候選即時從解析池推導。額度0/無
            # ready/pipeline 滿 → _drain_due False → 不派 → loop 能 idle 收斂（額度經快取判斷，免 subprocess）。
            if _drain_due(rows) and _start('__crawl_drain__', lambda r=rows: drain_crawl_queue(r)):
                dispatched += 1

            # C3. corpus-level 數學 sweep（track-only）：residual_unaccepted>0 且非 fixpoint 才跑。**無無頭
            # agent**——do_math_sweep 直接 det-step 跑 math_sweep batch（純自架 API：有多少壞式就 batch 解
            # 多少，非以書為單位）。__math_sweep__ key 自動序列化（在跑時不疊派）；fixpoint/converged → due
            # False → loop 能收斂 idle。
            due, _resid = _math_sweep_due()
            if due and _start('__math_sweep__', lambda: do_math_sweep(False)):
                dispatched += 1

            # C4. crawl 解析 agent：解析池（已確認連結未 owned）< 水位 ∧ 有 unresolved → 派一隻 crawl
            # agent 解析一批（書名→id/hash 判斷）。__crawl_resolve__ key 自動序列化（單隻在飛，本批 commit
            # 後下個 cycle 池仍低才派下一批，不並發撞同批）。池夠滿/無 unresolved → due False → loop 收斂。
            cr_due, _pc = _crawl_resolve_due()
            if cr_due and _start('__crawl_resolve__', lambda: do_crawl_resolve(False)):
                dispatched += 1

            # 排空收斂：連續 LOOP_IDLE_ROUNDS 輪「無新派工 ∧ 無在跑 ∧ 無 in-flight OCR」才收工。
            # occ 非空＝有書 OCR 在雲端排隊，harvestable() 隨時會翻就緒 → 不可進 idle 提早退出
            # （否則只剩待收 OCR 時提早收工，正是本架構要消除的 work-conservation 違反）。
            with ifl_lock:
                busy = len(inflight)
            if dispatched == 0 and busy == 0 and not occ:
                idle += 1
                if idle >= LOOP_IDLE_ROUNDS:
                    log(f'reactive loop：連 {idle} 輪無工作且無 in-flight OCR → 排空收工（launchd 下次重拉）')
                    break
            else:
                idle = 0
            # 事件驅動：工人一完成即被喚醒重觀測（transition 延遲從「滿一個 poll」塌到一次
            # observe，~16s）；無事件則睡滿 LOOP_POLL 當 OCR 輪詢/外部變更的上限節奏。
            wake.wait(LOOP_POLL)
            wake.clear()
    finally:
        _clear_controller_state()  # 退出即撤 statefile → 外部改走 kick 起新 controller
        # bounded drain：給在飛 worker 有限時間（DRAIN_BOUND）自然收尾，逾時升級「快殺子工 + 強制
        # 退出」。取代舊 ex.shutdown(wait=True) 的無上限等待——它會卡在長在飛批次（math sweep 是純
        # API thread，連 _kill_inflight_children 都殺不掉）→ reload/walltime 退出時 24min 凍結 +
        # 舊實例不死續持 .tick.lock 的孤兒鎖（見 orphan-lock memory）。被棄 worker 的產物全可從 disk
        # 重導、下個 controller 冪等重派，故強退安全（符合「狀態皆 disk 真相重導」架構）。
        log(f'reactive loop：排空在飛 worker（上限 {DRAIN_BOUND}s）…')
        ex.shutdown(wait=False)  # 不再接新、不阻塞
        _drain_deadline = time.monotonic() + DRAIN_BOUND
        while time.monotonic() < _drain_deadline:
            with ifl_lock:
                if not inflight:
                    break
            time.sleep(0.5)
        with ifl_lock:
            _stuck = len(inflight)
        if _stuck == 0:
            log('reactive loop：在飛 worker 已排空，優雅退出')
        else:
            _killed = _kill_inflight_children()  # 快殺可殺的 LLM 子工 → 解開卡在 p.wait 的 worker thread
            log(f'reactive loop：drain 逾時 {DRAIN_BOUND}s → 快殺 {_killed} 在飛子工、棄置 {_stuck} worker'
                '（產物 disk 重導、下個 controller 重派），強制退出')
            _grace = time.monotonic() + 5  # 極短 grace 讓被快殺的 worker 收尾（hist.finish/leases.release）
            while time.monotonic() < _grace:
                with ifl_lock:
                    if not inflight:
                        break
                time.sleep(0.2)
            with ifl_lock:
                _residual = len(inflight)
            if _residual:
                log(f'reactive loop：仍有 {_residual} 個非子進程型卡死 worker（純 API）→ os._exit 強退（respawn/launchd 重拉）')
                sys.stdout.flush()
                os._exit(0)  # 唯一能停掉卡死 thread 的手段；flock 隨進程死釋放、respawn 小弟接手
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
