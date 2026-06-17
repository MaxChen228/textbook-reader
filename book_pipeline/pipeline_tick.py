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
from datetime import datetime, timezone

from book_pipeline import pipeline_queue as q
from book_pipeline import mineru_budget as mb
from book_pipeline import worker_registry as wr
from book_pipeline import agent_history as hist
from book_pipeline import leases
from book_pipeline import extract_cover
from book_pipeline import booklists

ROOT = q.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
LOCK = os.path.join(BP, '.tick.lock')
LOG = os.path.join(BP, 'reports', 'daemon.log')
READER_ROOT = q.READER_ROOT
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
# codex 派工後端（BOOK_PIPELINE_PROVIDER=codex）：headless `codex exec --json`。認證走
# ~/.codex/auth.json（codex login，ChatGPT 訂閱）。模型預設 gpt-5.4，env 可覆寫。
CODEX_BIN = os.environ.get('CODEX_BIN', 'codex')
CODEX_MODEL = os.environ.get('BOOK_PIPELINE_CODEX_MODEL', 'gpt-5.4')
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
# 並行 crawl 下載度：refill 選好 K 本後，K 個確定性 `crawl_zlib fetch` 並行下載（IO bound，
# 40MB/本）。帳號由 daemon 依各帳號餘額預先指派（--account），不靠 agent 自選 → 零碰撞。
CRAWL_PARALLEL = int(os.environ.get('BOOK_PIPELINE_CRAWL_PARALLEL', '6'))
# 爬書低水位補貨（watermark）：購物清單 backlog < LOW 才補到 HIGH（確定性、無 LLM——從書單 SoT 拉
# 已解析 ready 的書）。把爬速綁定消化速（producer/consumer），取代舊「梭哈到額度見底→永久 latch」。
CRAWL_LOW = int(os.environ.get('BOOK_PIPELINE_CRAWL_LOW', '5'))    # 清單低水位：購物清單剩餘 < 此才補貨
CRAWL_HIGH = int(os.environ.get('BOOK_PIPELINE_CRAWL_HIGH', '20')) # 補到此清單水位（單批要補 = HIGH - 清單長度）
CRAWL_RETRY_S = int(os.environ.get('BOOK_PIPELINE_CRAWL_RETRY_S', '300'))  # crawl 重試最小間隔（防額度0時緊湊空轉）
# refill 時若書單 ready 不足，單批跑確定性 resolver（書名→z-lib id/hash，無 LLM、只 search）解析幾本。
CRAWL_RESOLVE_BATCH = int(os.environ.get('BOOK_PIPELINE_CRAWL_RESOLVE_BATCH', '20'))
# 購物清單（持久 buffer）：planner 選好的「待抓書」存這（slug/id/hash/title/fails）。drain 抓到即劃掉、
# refill 低於水位才補。檔在 BP 根（同 pipeline_state.json，per-machine runtime state、gitignore）。
CRAWL_QUEUE = os.path.join(BP, 'crawl_queue.json')
DATA_DIR = os.path.join(BP, 'mineru_data')
MAX_FETCH_FAILS = int(os.environ.get('BOOK_PIPELINE_MAX_FETCH_FAILS', '3'))  # 同本連續 fetch 失敗達此 → 移出清單
# refill 冷卻：書單補不滿清單（正典都 owned/queued/absent、暫無 ready target）→ 進冷卻、停重補，避免
# 「清單永遠 < 水位 → 每輪重跑 resolver 卻補不到」的無收斂 churn（同 math_sweep GROWTH 的收斂哲學）。
# drain 改變清單或冷卻到期才重試。
CRAWL_REFILL_COOLDOWN_S = int(os.environ.get('BOOK_PIPELINE_CRAWL_REFILL_COOLDOWN_S', '21600'))  # 6h
# 手動「強制補貨」請求 marker：外部（CLI `devctl crawl-refill`，未來亦可換 UI 按鈕）丟一張即可，
# daemon 下個 observe cycle 認它 → **無視水位/冷卻/退避**立刻派 crawl 小弟補到 HIGH，消費即刪（恰跑一次）。
# 外部只丟 marker、**不自己 refill** → 不與 daemon 搶寫 crawl_queue.json（單一寫手鐵律不破；UI 化也只是換個丟 marker 的前端）。
# 檔在 BP 根（per-machine runtime state、gitignore）。
CRAWL_REFILL_FORCE = os.path.join(BP, 'crawl_refill_request')
# harvest poll 上限（秒）：OCR 全好的書第一次 poll 就秒收；沒好的書等到此上限就放棄、留
# in-flight 下個 tick 再收。**短**值＝非阻塞（不等 OCR 跑完凍住 tick）。OCR 是 async、
# 在 MinerU 雲端並行跑，daemon 只負責「收已就緒的」，不該在此空等。
HARVEST_MAX_WAIT = int(os.environ.get('BOOK_PIPELINE_HARVEST_MAX_WAIT', '90'))

# 數學式 corpus-level sweep（Phase 2，track-only，不 gate deploy）：書先上站，殘餘累積到門檻
# 才派**一隻**跨書 sweep agent 一次清。THRESHOLD = 觸發水位（全 corpus 壞式 occ 總和）；
# 數學 sweep 派工模型：a=100, b≈0。THRESHOLD(a)=殘餘累積到此才派——調高=批量攢夠才一次
# 結構化大掃，而非低門檻一輪一輪 nibble；agent 之 mandate 是把可清的收斂到趨近零（見 prompt）。
# GROWTH 為**防 busy-loop 安全閥**：autonomous 無法自改核心碼，可泛化殘餘只能提 proposals 交人工，
# 故單次 sweep 後殘餘未必 <a；已 sweep 後須再長 GROWTH 才重派（max(a, 殘餘+GROWTH)），避免同態反覆喚醒。
MATH_SWEEP_THRESHOLD = int(os.environ.get('BOOK_PIPELINE_MATH_SWEEP_THRESHOLD', '100'))
MATH_SWEEP_GROWTH = int(os.environ.get('BOOK_PIPELINE_MATH_SWEEP_GROWTH', '25'))

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
# live reactive controller 的 statefile（JSON {pid, sha, started}）：loop 起頭寫、退出即刪。
#   pid → 外部送 SIGUSR1 喚醒（crawl-refill / reload）；sha → 此 controller 載入的 git 版本，供
#   「daemon 跑的是哪版碼、離 HEAD 多遠」即時觀測（免上線後做 forensics）。per-machine、gitignore。
CONTROLLER_STATE = os.path.join(BP, '.controller.json')
# reload 請求 marker：`devctl reload` 丟它 + SIGUSR1 → loop **排空在飛 worker 後優雅退出**、launchd 載
# 入新碼（零浪費；對比 kick -k 硬殺跳過 finally 會棄工作）。SIGUSR1 統一語意＝「醒來看控制 marker」（refill/reload 共用）。
RELOAD_REQUEST = os.path.join(BP, 'reload_request')

# LLM 階段 → headless claude 任務描述（指向既有 skill/reference）。
# 註：crawl 選書已不在此——書單 SoT（booklists/）+ 確定性 resolver 取代了舊「派 LLM 從零重推書單」
# 的土炮做法（見 refill_crawl_queue）。本表只剩真正需 LLM 判斷的下游階段。
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
    'math_sweep': (
        "你是 book_pipeline 的 **corpus-level 數學式 sweep agent**（跨全書，非單本）。"
        "**嚴格遵照 .claude/skills/book-pipeline/references/math-sweep.md**（parsed 檔路徑慣例："
        "book_pipeline/mineru_data/<slug>/parsed/<chunk>.json；eq 修法的 expect 可直接抄 finding 的 tex）。"
        "**目標：把 corpus 數學渲染殘餘結構化收斂到趨近零**——不是跑一輪就交差。**反覆**："
        "aggregate → 分類修 → 重 validate，直到殘餘**停止下降**（只剩你權限外或源頭不可逆的硬 edge）才收工。"
        "**autonomous 模式硬規則**："
        "(1) 跑 `uv run python -m book_pipeline.math_validate --aggregate --json` 取跨書聚合殘餘，由高頻往下做。"
        "(2) **結構化優先**：可跨書泛化者（巨集缺定義 / 機械 OCR pattern）**用建議系統 CLI 提案**（交人工升級核心碼）："
        "`uv run python -m book_pipeline.proposals propose --domain math --type <macro|normalize-rule> "
        "--source math_sweep --title '<簡述>' --detect '<\\token…>' --evidence '<壞樣本+err+哪些書>' "
        "--proposal '<建議規則/巨集>' --risk '<誤改風險>'`（自動 id、schema 強制、並行安全）。"
        "**絕不**自行改 math_macros.json / math_normalize.py。先提案再做 one-off，避免重工。"
        "(3) 其餘逐書 one-off：寫 book_pipeline/math_overrides/<slug>.json（action fix_eq_tex/fix_inline_math，"
        "targets 直接抄 finding 的 chunk/selector/field，eq 用 expect、inline 用 anchor guard；同欄重複式用 all、"
        "重複 problem num 用 selector 的 #OCC，見 SOP §4）。寫完自跑 `apply_math_overrides <slug>` + "
        "`math_validate <slug>` 確認該書殘餘下降。**收工前再跑一次 aggregate 確認總殘餘已收斂**。"
        "(4) 源頭 OCR 亂碼/截斷、無 PDF 可重建的少數硬 edge：**留著即可**（§8 accept），"
        "**不要為了清零硬塞語意錯的式子、也不要寫死過擬合邊界的演算法**。"
        "卡關或發現系統性問題 → 走建議系統 `proposals propose` 記下交人工。**絕不手改 parsed/*.json**。"),
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


def _llm_env(provider: str) -> dict | None:
    """指定 provider 的派工環境。kimi → 把同一個 claude CLI（harness 不變）導到 Kimi Code
    端點（key 讀 ~/.secrets/kimi.env，不進全域 env）。claude/codex → None（claude 沿用 Claude
    Max 訂閱；codex 用自己的 ~/.codex/auth.json）。"""
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


# Provider failover 串接：撞額度的 provider 不再讓整輪停擺，而是換鏈上下一個 provider 重跑
# 同一任務。鏈預設 kimi→codex→claude（env BOOK_PIPELINE_PROVIDER_CHAIN 覆寫，逗號分隔）；
# 未設則退回單一 BOOK_PIPELINE_PROVIDER（向後相容）。全鏈撞光才 defer 到下個 tick。
def _provider_chain() -> list[str]:
    raw = os.environ.get('BOOK_PIPELINE_PROVIDER_CHAIN', '').strip()
    if raw:
        chain = [p.strip().lower() for p in raw.split(',') if p.strip()]
        if chain:
            return chain
    return [os.environ.get('BOOK_PIPELINE_PROVIDER', 'claude').lower()]


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
    markers = CODEX_LIMIT_MARKERS if provider == 'codex' else SESSION_LIMIT_MARKERS
    return any(m in out for m in markers)


def _event_error_text(provider: str, ev: dict) -> str:
    """只從『終端錯誤事件』抽出可供限額判定的文字；正常 agent 訊息/工具指令一律回 ''。
    （絕不能掃整段 transcript：被 audit 的書內容與工人指令本身常含 quota/429/rate limit，
    會把成功的派工誤判成撞額度。歷史 bug：codex 額度滿卻連環「撞額度」failover。）"""
    t = ev.get('type') or ''
    if provider == 'codex':
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


def _build_llm_cmd(provider: str, prompt: str) -> list[str]:
    """依 provider 組 headless 派工命令。
    claude/kimi：同一個 claude CLI（kimi 僅由 _llm_env 換後端），走 stream-json。
    codex：`codex exec --json`，沙箱 danger-full-access 對齊 claude -p 的全權（daemon 信任
    環境，audit/repair 要寫 mineru_data、跑 uv），--model 預設 gpt-5.4。
    ⚠ 全權沙箱為【已審視的接受風險】：daemon 本質需 fs-write+exec 才能產書，收緊即失能。
    對應緩解——注入面 slug 已白名單化（_fetch_book / crawl_zlib，[a-z0-9_]{1,64}）、
    不可信的 OCR 產物在 bake 邊界消毒（nh3 表格 + marked raw-HTML 轉義）。勿擅自收緊。"""
    if provider == 'codex':
        return [CODEX_BIN, 'exec', '--json', '--skip-git-repo-check',
                '-C', ROOT, '--sandbox', 'danger-full-access',
                '--model', CODEX_MODEL, prompt]
    return [CLAUDE_BIN, '-p', prompt, '--add-dir', ROOT,
            '--output-format', 'stream-json', '--verbose']


def _emit(wkey: str, kind: str, label: str, tag: str) -> None:
    """單一事件 → live 面板（wr，截字/節流）+ 完整歷程（hist，原文不截）+ stdout 回顯。
    新增事件來源只改這一處，免「wr/hist 雙呼叫漏改」。kind='tool'|'text'。"""
    wr.event(wkey, kind, label)
    hist.event(wkey, kind, label)
    sys.stdout.write(f'[{tag}] {"🔧" if kind == "tool" else "💬"} {label[:160]}\n')


def _pump_event(provider: str, ev: dict, wkey: str, tag: str) -> None:
    """單一 JSONL 事件 → 事件匯流（_emit）。claude 與 codex schema 不同，各自解。"""
    if provider == 'codex':
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


def _run_one(provider: str, todo_verb: str, slug: str | None,
             prompt: str) -> tuple[int, bool]:
    """用單一 provider 跑一次派工。回 (rc, hit_limit)。hit_limit=True 代表撞該 provider 額度，
    呼叫端據此換鏈上下一個 provider 重跑同一任務。timeout→(-1, False)（逾時非額度）。"""
    import signal
    import time
    cmd = _build_llm_cmd(provider, prompt)
    log(f'RUN llm {todo_verb} {slug or ""}（{provider}/JSONL）')
    p = subprocess.Popen(cmd, cwd=ROOT, env=_llm_env(provider), start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    err_parts: list[str] = []  # 只裝『終端錯誤事件 + 非 JSON(CLI/stderr) 行』供限額判定
    tag = slug or todo_verb
    wkey = f'{slug or todo_verb}:{p.pid}'
    wr.register(wkey, slug, todo_verb, p.pid, provider)
    hist.start(wkey, slug, todo_verb, p.pid, provider,
               CODEX_MODEL if provider == 'codex' else provider)
    result_rc = -1  # finally 用：timeout 路徑直接 return -1 不設 rc，故先給地板值
    # 租約包住實際 LLM 子進程：reactive loop 用它防「跨 controller crash 的 orphan 子進程」
    # 被重派/續殺（pid=真子進程、killable）。one-shot 模式下亦無害（tick 內 acquire→release）。
    leases.acquire(todo_verb, slug, p.pid, LLM_TIMEOUT)

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
            _pump_event(provider, ev, wkey, tag)
            et = _event_error_text(provider, ev)
            if et:
                err_parts.append(et)
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
            t.join(timeout=5)  # 子進程已死 → stdout 關閉、_pump 收尾；先排空再讓 finally finish，
            return -1, False   # 否則被 kill session 的尾段事件（hist.event）會 pop 後遭丟棄
        t.join(timeout=5)
        # rc==0（成功跑完）絕不可能是撞額度；只在失敗且錯誤面含限額字才判 hit
        err = '\n'.join(err_parts).lower()
        result_rc = rc
        return rc, (rc != 0 and _hit_limit(provider, err))
    finally:
        hist.finish(wkey, result_rc)  # 失敗/timeout/撞額度的 session 也記（rc!=0, ok=False）
        leases.release(todo_verb, slug)
        wr.unregister(wkey)


def dispatch_llm(todo_verb: str, slug: str | None, dry: bool) -> int:
    """派 headless LLM 跑階段，沿 provider 鏈 failover。回 rc；-2 = 全鏈撞額度 → 呼叫端 defer。
    鏈預設 kimi→codex→claude（_provider_chain）。某 provider 撞額度即標記（本 tick 內共享，
    並行子工不再重複撞）、換下一個 provider **重跑同一任務**（不浪費派工）；全鏈撞光才 -2。
    （crawl 選書已不派 LLM——改書單 SoT + 確定性 resolver；本函式只服務 qc/audit/sol_extract 等下游階段。）"""
    prompt = LLM_PROMPTS[todo_verb].format(slug=slug or '')
    chain = _provider_chain()
    if dry:
        log('DRY ' + ' '.join(shlex.quote(c) for c in _build_llm_cmd(chain[0], prompt)))
        return 0
    tried = []
    for provider in chain:
        with _exhausted_lock:
            if provider in _exhausted_providers:
                continue
        tried.append(provider)
        rc, hit = _run_one(provider, todo_verb, slug, prompt)
        if not hit:
            return rc  # 成功或非額度失敗 → 交回呼叫端
        with _exhausted_lock:
            _exhausted_providers.add(provider)
        nxt = next((q for q in chain if q != provider
                    and q not in _exhausted_providers), None)
        log(f'⚠ {provider} 撞額度（{todo_verb} {slug or ""}）→ '
            + (f'串接 {nxt} 重跑' if nxt else '鏈上無可用 provider'))
    log(f'❌ 全 provider 撞額度 {chain}（試過 {tried}）→ defer {todo_verb} {slug or ""}，下個 tick 重試')
    return -2


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


# ── 購物清單（持久 buffer）：producer-buffer-consumer 解耦的核心資料結構 ──────────────
#   refill（producer）低水位時自書單 SoT 確定性補貨進清單；daemon 買書員（consumer）有額度即從
#   清單頭抓、抓到劃掉。清單與額度徹底解耦 → /dev 爬書欄永遠有貨可顯示（額度0只是抓不動，清單照在）。
def _load_queue_full() -> dict:
    """讀購物清單完整 payload（books + meta）。檔缺 → 空清單（refill 會自書單 SoT 補回）。"""
    try:
        return json.load(open(CRAWL_QUEUE)) or {}
    except Exception:
        return {'books': []}


def _load_queue() -> list[dict]:
    """購物清單 books（薄包裝；due 判斷 / devctl 用）。"""
    return [b for b in (_load_queue_full().get('books') or []) if b.get('slug')]


def _save_queue(books: list[dict], reason: str = '', exhausted_at=None) -> None:
    """原子寫購物清單（tmp+replace）。單寫手：只在 tick 的 crawl worker 內呼叫（__crawl__ 序列化）。
    refill_exhausted_at：書單補不滿時的冷卻時戳（_in_cooldown 用），None=未枯竭。"""
    tmp = CRAWL_QUEUE + f'.tmp{os.getpid()}'
    payload = {'books': books, 'count': len(books), 'reason': reason,
               'refill_exhausted_at': exhausted_at}
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CRAWL_QUEUE)


def _in_cooldown(exhausted_at) -> bool:
    """refill 是否在冷卻中（書單 ready target 暫枯竭、停重補直到冷卻到期）。"""
    try:
        return exhausted_at is not None and (time.time() - float(exhausted_at)) < CRAWL_REFILL_COOLDOWN_S
    except Exception:
        return False


# ── 手動「強制補貨」請求（CLI/未來 UI → daemon 的單向 signal）───────────────────────
def request_refill() -> None:
    """外部（`devctl crawl-refill`，未來亦可 UI 按鈕）丟一張『強制補貨』請求。寫 marker 是唯一副作用，
    **不自己 dispatch LLM、不碰 crawl_queue.json** → 不與 daemon 搶寫（單一寫手不變）。daemon 下個
    observe cycle 認它，無視水位/冷卻/退避立刻派 crawl 小弟補到 HIGH。"""
    try:
        with open(CRAWL_REFILL_FORCE, 'w') as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _refill_force_pending() -> bool:
    """是否有未消費的強制補貨請求（daemon 兩條路徑都 peek 它，無視一切 gate 觸發 refill）。"""
    return os.path.exists(CRAWL_REFILL_FORCE)


def _clear_refill_force() -> None:
    """消費強制補貨請求（派出 refill 後即刪 → 恰跑一次；派不出去時不刪、下個 cycle 再認）。"""
    try:
        os.remove(CRAWL_REFILL_FORCE)
    except OSError:
        pass


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
    daemon 端去重的權威來源——擋 planner 誤選既有書（曾見 james_islr 等已 parsed 書被重列）。"""
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


def _zlib_remaining_cached():
    """廉價讀 zlib 今日餘額快取（dev/zlib_quota.json，devctl 60s 心跳刷新）→ reactive due 判斷用，
    不打網路。回 int 餘額 / None=未知（樂觀視為可能有額度，drain 階段再做權威查詢）。"""
    try:
        c = json.load(open(os.path.join(ROOT, 'dev', 'zlib_quota.json')))
        r = c.get('total_remaining')
        return int(r) if r is not None else None
    except Exception:
        return None


def _merge_plan_into_queue(queue: list[dict], plan: dict | None, have: set) -> int:
    """把一批選好的書（{'books':[{slug,id,hash,title}]}，來自書單 select_next）**append** 進清單；
    去重 vs 清單∪inventory。回實際新增數。daemon 端權威去重 = 既有 re-crawl bug 的修補。"""
    if not plan:
        return 0
    qslugs = {b['slug'] for b in queue}
    added = 0
    for b in (plan.get('books') or []):
        slug = b.get('slug')
        if not slug or not re.fullmatch(r'[a-z0-9_]{1,64}', slug):
            continue
        if slug in qslugs or slug in have:
            continue
        if not (b.get('id') and b.get('hash')):
            continue
        queue.append({'slug': slug, 'id': str(b['id']), 'hash': str(b['hash']),
                      'title': b.get('title', ''), 'fails': 0})
        qslugs.add(slug)
        added += 1
    return added


def _fetch_book(b: dict) -> str | None:
    """確定性下載單本（planner 已選好 id/hash/slug，daemon 已指派 account）。
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
    """backlog 書數（爬速綁此值：<LOW 才補、>=LOW 讓 pipeline 消化）。= 清單長度，單一真相源。"""
    return len(_crawl_backlog_books(rows))


def _drain_due(rows: list[dict]) -> bool:
    """**買書員**是否該跑（cheap、免 subprocess、保 idle 收斂）：清單有貨 ∧ pipeline 有空間 ∧
    額度快取≠0。額度快取 None（未知）→ 樂觀（drain 內權威查）。額度0/清單空/pipeline 滿 → 不跑。"""
    have = _have_slugs()
    queue = [b for b in _load_queue() if b['slug'] not in have]
    return bool(queue) and _crawl_backlog(rows) < CRAWL_HIGH and _zlib_remaining_cached() != 0


def _refill_due() -> bool:
    """**確定性 refill** 是否該跑：清單（去重後）< 水位 ∧ 不在冷卻。與額度、pipeline 全無關。"""
    full = _load_queue_full()
    have = _have_slugs()
    queue = [b for b in (full.get('books') or []) if b.get('slug') and b['slug'] not in have]
    return len(queue) < CRAWL_LOW and not _in_cooldown(full.get('refill_exhausted_at'))


def drain_crawl_queue(rows: list[dict], dry: bool = False) -> list[str]:
    """**買書員（確定性，非 agent）**：有額度就照購物清單抓、抓到就**劃掉**。與 LLM 完全無關、
    無退避——每次有額度+有貨+pipeline 有空間就跑。**額度只在這裡咬**。回本輪抓到的 slug（已落
    raw_pdfs，reactive loop 下個 observe 自然接手 ingest，毋須在此 advance）。"""
    full = _load_queue_full()
    have = _have_slugs()
    orig = full.get('books') or []
    # 進場去重：清單裡其實已存在的（已從別路徑抓到 / 舊誤入）→ 直接劃掉（確定性、與是否抓無關）
    queue = [b for b in orig if b.get('slug') and b['slug'] not in have]
    exhausted_at = full.get('refill_exhausted_at')
    reason = full.get('reason', '') or ''
    backlog = _crawl_backlog(rows)
    room = max(0, CRAWL_HIGH - backlog)  # pipeline 還能容納幾本在飛（backpressure：滿了不抓，清單照留）
    deduped = len(queue) != len(orig)

    if dry:
        rem = _zlib_remaining_cached()
        tail = '' if (queue and room > 0) else ' → 本輪不抓'
        log(f'crawl 買書員：清單 {len(queue)} 本 · pipeline 餘裕 {room} · 額度(快取) {rem}{tail}')
        return []

    def _persist_if_deduped():
        if deduped:
            _save_queue(queue, reason=reason, exhausted_at=exhausted_at)

    if not (queue and room > 0):
        if queue and room <= 0:
            log(f'crawl 買書員 hold：pipeline 已滿（backlog {backlog} ≥ {CRAWL_HIGH}）→ 清單 {len(queue)} 本待消化')
        _persist_if_deduped()
        return []

    accts = _zlib_accounts_remaining()
    if accts is None:
        log('crawl 買書員 skip：查額度失敗，本輪不抓（清單原封保留）')
        _persist_if_deduped()
        return []
    slots = [a['account'] for a in accts for _ in range(max(0, a.get('remaining') or 0))]
    n = min(len(queue), len(slots), room)
    if n <= 0:
        log('crawl 買書員 defer：今日額度耗盡 → 清單原封保留待明日（不 latch，下輪自動重探）')
        _persist_if_deduped()
        return []

    batch = queue[:n]
    for i, b in enumerate(batch):
        b['account'] = slots[i]
    log(f'crawl 買書員：清單 {len(queue)} 本 → 本輪抓 {n}（額度槽 {len(slots)}、pipeline 餘裕 {room}）')
    ok, crawled = set(), []
    with ThreadPoolExecutor(max_workers=min(CRAWL_PARALLEL, n)) as ex:
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
            else:
                b['fails'] = int(b.get('fails', 0)) + 1
    # 結算清單：成功劃掉 / 失敗達上限丟棄 / 其餘保留重試（清掉本輪臨時 account 指派）
    newq = []
    for b in queue:
        if b['slug'] in ok:
            continue
        if int(b.get('fails', 0)) >= MAX_FETCH_FAILS:
            log(f'crawl drop：{b["slug"]} 連 {b.get("fails")} 次 fetch 失敗 → 移出清單（待 refill 補替代）')
            continue
        b.pop('account', None)
        newq.append(b)
    _save_queue(newq, reason=reason, exhausted_at=exhausted_at)
    log(f'crawl 買書員 done：抓到 {len(ok)}/{n}，清單剩 {len(newq)} 本')
    if crawled:
        hist.set_touched('crawl_plan', crawled)  # 帶進的書 → 各書抽屜查得此爬書歷程
        try:
            from book_pipeline import devctl
            devctl.invalidate_zlib_cache()  # 剛花額度 → 失效快取，下個 snapshot 反映 live 餘額
        except Exception:
            pass
    return crawled


def _run_resolver(limit: int) -> None:
    """跑確定性 canon resolver（書名→z-lib id/hash，或標 absent；**無 LLM、只 search、不耗下載額度**）
    把 unresolved 書名解析成可下載目標，補充書單的 ready 池。與買書員一樣是確定性步驟，daemon 直跑
    （非 agent，不註冊 worker_registry）。子進程跑 book_pipeline.resolve。"""
    log(f'crawl resolver：解析最多 {limit} 本 unresolved 書名 → id/hash（無 LLM、只 search）')
    try:
        out = subprocess.run(
            ['uv', 'run', '--with', 'requests', 'python', '-m', 'book_pipeline.resolve',
             '--limit', str(limit)], cwd=ROOT, capture_output=True, text=True, timeout=900)
        tail = (out.stdout or '').strip().splitlines()
        if tail:
            log(f'crawl resolver：{tail[-1]}')
        if out.returncode != 0:
            log(f'⚠ crawl resolver rc={out.returncode}：{(out.stderr or "")[:200]}')
    except Exception as e:
        log(f'⚠ crawl resolver 異常：{e}')


def refill_crawl_queue(dry: bool = False) -> int:
    """**確定性 refill（無 LLM）**：清單低於水位 → 從書單 SoT（booklists/）拉已解析(ready)的書補進
    購物清單；ready 不足且仍有 unresolved → 先跑確定性 resolver（書名→id/hash，只 search）產更多
    ready 再拉。補不出新書（剩下皆 review/absent，或 resolver 也解不出）→ 進冷卻、停 churn。回新增數。
    取代舊「派 LLM planner 讀整本 wishlist 從零重推書單」的土炮做法（零 token、零漂移、有分母）。
    買書員 drain 仍是唯一咬下載額度處；本函式與額度無關。"""
    full = _load_queue_full()
    have = booklists.have_slugs()
    queue = [b for b in (full.get('books') or []) if b.get('slug') and b['slug'] not in have]
    exhausted_at = full.get('refill_exhausted_at')
    reason = full.get('reason', '') or ''
    want = CRAWL_HIGH - len(queue)
    if want <= 0:  # 清單已滿（多半是 forced refill 在水位之上觸發）→ 無缺口可補
        log(f'crawl refill skip：清單已達水位 {len(queue)}/{CRAWL_HIGH}，無需補貨')
        return 0
    if dry:
        ready = len(booklists.select_next(want))
        log(f'crawl refill（dry）：清單 {len(queue)} < 水位 → 想補 {want}，書單 ready {ready}'
            f'（不足且有 unresolved 會先跑 resolver）')
        return 0
    picks = booklists.select_next(want)
    if len(picks) < want and booklists.has_unresolved():
        _run_resolver(CRAWL_RESOLVE_BATCH)         # ready 不夠 → 解析更多書名（確定性、無 LLM）
        picks = booklists.select_next(want)
    added = _merge_plan_into_queue(queue, {'books': picks}, have)  # 去重 vs 清單∪inventory
    if added:
        exhausted_at = None                         # 補成功 → 解除冷卻
        reason = f'書單確定性補貨 +{added}'
        log(f'crawl refill done：書單補入 {added} 本 → 清單 {len(queue)} 本（確定性、零 LLM）')
    elif len(queue) < CRAWL_LOW:
        exhausted_at = time.time()                  # 補不出 ready（剩 review/absent/解析不出）→ 冷卻、停 churn
        log(f'crawl refill 收斂：書單暫無可補的 ready（剩 review/absent 或待解析）'
            f' → 冷卻 {CRAWL_REFILL_COOLDOWN_S // 3600}h')
    _save_queue(queue, reason=reason, exhausted_at=exhausted_at)
    return added


def do_crawl_tick(dry: bool, rows: list[dict]) -> list[str]:
    """oneshot tick 的 crawl 編排：先**買書員 drain**（確定性、劃掉），再（清單低於水位才）**確定性
    refill**（從書單 SoT 拉 ready；ready 不足跑 resolver 解析更多——皆無 LLM）。reactive loop **不**走
    此函數——它把 drain/refill 當兩個獨立 due-gated 步驟分派（C1 買書員每 cycle、C2 退避補貨）。回 drain 抓到的 slug。"""
    crawled = drain_crawl_queue(rows, dry)
    forced = _refill_force_pending()  # 手動強制補貨請求：無視水位/冷卻直接補
    if forced or _refill_due():
        refill_crawl_queue(dry)
        if forced and not dry:
            _clear_refill_force()  # 消費請求（恰一次）
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


def _math_refire_threshold(state: dict | None = None) -> int:
    """corpus 殘餘要達到多少 occ 才會（再次）派 sweep —— _math_sweep_due 的唯一門檻真相源。
    冷啟/換 macros → 靜態地板 THRESHOLD（a=100，攢夠才結構化大掃）；已在同 macros sweep 過 →
    動態升級為 max(地板, 上次收斂殘餘 + GROWTH)（防 busy-loop：autonomous 清不掉的泛化殘餘只能提
    proposals 交人工，單次 sweep 後殘餘未必 <a）。看板/CLI 一律顯示這個值，穩定 = N < 此動態門檻。"""
    from book_pipeline import math_validate as mv
    s = state if state is not None else q._load_state()
    ls = q.math_sweep_state(s)
    if ls and ls.get('macros_version') == mv.macros_version() and ls.get('residual_after') is not None:
        return max(MATH_SWEEP_THRESHOLD, int(ls['residual_after']) + MATH_SWEEP_GROWTH)
    return MATH_SWEEP_THRESHOLD


def _math_sweep_due(state: dict | None = None) -> tuple[bool, int]:
    """廉價門檻判定（讀 state，不重跑 node）。回 (該不該 sweep, 當前 corpus 殘餘 occ)。
    缺 node → 永不 due（無從驗證）；殘餘 ≥ 動態重派門檻（見 _math_refire_threshold）→ due。
    保證 reactive loop 能收斂 idle：sweep 後門檻升到「上次收斂 + GROWTH」，殘餘沒再長就不重派。"""
    from book_pipeline import math_validate as mv
    if not mv.node_available():
        return False, 0
    s = state if state is not None else q._load_state()
    total = q.corpus_math_residual(s)
    return total >= _math_refire_threshold(s), total


def do_math_sweep(dry: bool) -> int:
    """corpus-level 數學 sweep reduce job（track-only，不綁單本 advance 關鍵路徑）：殘餘累積
    過門檻 → 派**一隻**跨書 sweep agent（寫 math_overrides + `proposals propose`，autonomous 不碰核心碼）
    → daemon 確定性把被改的書重烤上站 → 重量殘餘、記錄 sweep 狀態（防重派）。
    回 0=完成/跳過、-2=LLM 撞額度（defer 下個 tick，不記狀態）。"""
    from book_pipeline import math_validate as mv
    due, total = _math_sweep_due()
    if not due:
        return 0
    cur = mv.macros_version()
    log(f'math sweep：corpus 殘餘 {total} occ ≥ 門檻 {MATH_SWEEP_THRESHOLD} → 派跨書 sweep agent（macros={cur}）')
    if dry:
        dispatch_llm('math_sweep', None, dry=True)
        return 0
    t0 = time.time()
    rc = dispatch_llm('math_sweep', None, dry=False)
    if rc == -2:
        log('math sweep：LLM 撞額度 → defer 下個 tick（不記 sweep 狀態）')
        return -2
    # daemon 確定性收尾（apply 從 agent 收回 daemon，比照 catalog；消除「agent apply 的書必須恰等於
    # daemon 重烤的書」的脆弱耦合）：對每本有 override 的書 idempotent re-apply；凡本輪真有改動（apply
    # 產生 applied，含重 parse 沖掉後重套）或 override 本輪新寫（mtime>t0）→ 重烤上站（live 讀 data/，
    # 不重烤看不到修復）+ 重量殘餘。apply 失配自動 skip-drift，全程不 raise。
    from book_pipeline import apply_math_overrides as amo
    od = os.path.join(BP, 'math_overrides')
    rebake: list[str] = []
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
            rebake.append(slug)
            log(f'math sweep：{slug} {stats} → 重烤上站')
    for slug in rebake:
        brc = subprocess.run(['uv', 'run', 'python', '-m', 'build.build_all', slug], cwd=READER_ROOT).returncode
        if brc != 0:
            log(f'❌ math sweep 重烤 {slug} build rc={brc}（parsed 已修、data 未更新，下個 sweep 重試）')
        do_math_track(slug)
    residual_after = q.corpus_math_residual()
    q.mark_math_swept(cur, residual_after, rebake)
    hist.set_touched('math_sweep', rebake)  # corpus session 回填改動書清單 → 各書抽屜查得此場歷程
    log(f'math sweep ✓：本輪改 {len(rebake)} 書，corpus 殘餘 {total}→{residual_after} occ')
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

    # B+C 並發：advance 既有書（LLM/audit/qc/sol_extract）與 crawl 補新書（zlib 下載 + planner）
    # 是**獨立資源池**，不該互等 → 同時跑。否則 crawl 卡在 audit barrier 後面 = 額度遲遲不消耗、
    # audit 慢就整批新書都不爬。各書 LLM 任務獨立無依賴 → advance 內部本就全並行。
    adv_slugs = [r['slug'] for r in rows if r['slug'] not in skip]
    if dry:
        _advance_parallel(adv_slugs, dry, no_deploy)
        crawled = do_crawl_tick(dry, rows)  # 買書員 drain（劃掉）+ 低水位才 refill（agent）
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

    # 外部喚醒：SIGUSR1 → wake.set() → loop 立即 re-observe（devctl crawl-refill 等手動觸發用，
    # 不殺在飛 worker）。signal.signal 須在主執行緒（tick_reactive 由 main 直呼，成立）。寫 pidfile
    # 供外部找到本 controller；非主執行緒/平台不支援則略過 → 退回 LOOP_POLL 節奏（功能降級不報錯）。
    import signal
    try:
        signal.signal(signal.SIGUSR1, lambda *a: wake.set())
        _write_controller_state()
    except (ValueError, OSError):
        pass

    deadline = time.monotonic() + LOOP_WALLTIME
    idle = 0
    last_refill_attempt = -1e9  # 退避只套「叫 crawl agent 補貨」（補不到時別狂叫）；買書員 drain 無退避
    try:
        while time.monotonic() < deadline:
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

            # C1. 買書員（確定性、非 agent、無退避）：清單有貨 ∧ 額度 ∧ pipeline 有空間 → 派確定性 drain
            # worker 抓+劃掉（無 LLM、不註冊 worker_registry → 不顯示為 crawl agent；__crawl_drain__ 序列化）。
            # 每 cycle 可派 → 有額度就持續買、抓到就劃掉、與 agent 節奏無關。額度0/清單空/pipeline 滿 →
            # _drain_due False → 不派 → loop 能 idle 收斂（額度經快取判斷，免 subprocess）。
            if _drain_due(rows) and _start('__crawl_drain__', lambda r=rows: drain_crawl_queue(r)):
                dispatched += 1

            # C2. 確定性 refill（**無 LLM**）：清單 < 水位 ∧ 不在冷卻 → 從書單 SoT 拉 ready 補貨；ready 不足
            # 跑 resolver 解析更多（只 search、不耗額度）。退避（CRAWL_RETRY_S）只套這裡——補不出（剩 review/
            # absent）時別狂跑 resolver；__crawl_refill__ 序列化。冷卻 + 退避雙保險 → 補不滿時 loop 仍能 idle 收斂。
            # forced（手動 `devctl crawl-refill` 丟的 marker）= **無視水位/冷卻/退避**立刻補：消費綁「真派出去」
            # （_start True 才 clear）→ 已在補時不清、下個 cycle 再認 → 恰跑一次，不雙派。
            forced = _refill_force_pending()
            if forced or (_refill_due() and (time.monotonic() - last_refill_attempt) >= CRAWL_RETRY_S):
                if _start('__crawl_refill__', lambda: refill_crawl_queue(False)):
                    dispatched += 1
                    last_refill_attempt = time.monotonic()
                    if forced:
                        _clear_refill_force()

            # C3. corpus-level 數學 sweep（track-only）：殘餘過門檻才派一隻跨書 agent。__math_sweep__
            # key 自動序列化（在跑時不疊派）；_math_sweep_due 在已 sweep 狀態回 False → loop 能收斂 idle。
            due, _resid = _math_sweep_due()
            if due and _start('__math_sweep__', lambda: do_math_sweep(False)):
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
        log('reactive loop：等待在跑 worker 收尾…')
        ex.shutdown(wait=True)
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
