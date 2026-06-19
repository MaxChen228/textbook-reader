# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 本質

**自包含**的教科書 pipeline + 靜態 reader 站。一條鏈 crawl→ingest(MinerU 雲端 OCR)→qc→parse→audit→catalog→sol→bake→serve 全住本 repo。原本依賴外部 `qbank` repo,已整條搬進來(`book_pipeline/` + `textbooks/`)。常駐主機 standby(`100.118.39.104`)24hr 跑 daemon 全自動產書,經 Cloudflare `kg-standby` tunnel 自託管於 **books.wordnexus.lol**。**不再用 GitHub Pages**(remote 僅留作 git 跨機同步)。

前端是單頁 reader(`index.html`,全 JS inline)。`data/`(JSON)+ `img/`(WebP)是 build 從 `book_pipeline/mineru_data/` 烤出的**本地產物**,**不入 git**,standby 上由 daemon 自動重生。

## 指令

```bash
uv run python -m build.build_all [slug ...]              # 烤靜態站（不帶 slug = 全部書）
uv run python -m book_pipeline.status                    # 全書 stage 儀表板（當下 frontier）
uv run python -m book_pipeline.book_audit [slug ...]     # 新進書唯讀體檢（書本身對不對/完不完整）
uv run python -m book_pipeline.trace cohort --since 12h  # 某時間段入庫 cohort 溯源漏斗（每本為何沒上架）
uv run python -m book_pipeline.trace book <slug>         # 單書時間線 ⊕ 每階段 LLM session（→ trace session <id> 看全對話）
uv run python -m book_pipeline.pipeline_tick --dry-run   # daemon 單 tick 計畫（不執行）
uv run python -m book_pipeline.pipeline_queue            # 跨書全 stage work-queue
uv run python -m http.server 8001                        # 本機預覽
```

一律 `uv run`(專案 python 規範禁裸 `python3`)。`pyproject.toml` 宣告依賴,免 `--with` sprawl。

### 觀測面四分（關注點不重疊、各一入口，改前先認準）

- **`status`** — 當下階段 frontier 儀表板（現在每本卡哪）。
- **`trace`**(`book_pipeline/trace.py`) — 回溯「一本/一批書發生什麼」的統一 forensic 入口：`book`(階段⊕session 時間線)/`session <id>`(全對話)/`cohort --since`(批次溯源漏斗,✅上站+⏳處理中+⚠卡關==入庫,零缺口)/`stuck`(待人工裁決)。**只組合既有資料 API**(`book_timeline`/`agent_history`/`status.assess`/`pipeline_state`),不持新真相;`devctl history/--session` delegate 至此。
- **`devctl`** — daemon 即時控制/健康(kick/reload/incident/snapshot,見下「監控」)。
- **`pipeline_queue`** — work-queue 機制 + `first_seen_at` 入庫戳資料層(`--backfill-first-seen` 補登歷史)。入庫時間單一真相 = `pipeline_state.json` 的 `first_seen_at`,每 observe idempotent 蓋、零缺口。

## Pipeline 架構（book_pipeline/）

`pipeline_tick.py` 是 launchd 每 ~45min 觸發的 daemon 單 tick,推進整條鏈:
- **crawl**(z-library 爬書,**全確定性、零 LLM**:見下「書單 SoT」)→ **triage**(pdf_triage)→ **qc**(視覺驗證,LLM)→ **ingest**(`mineru_ingest`+`mineru_budget` 多帳號預算,MinerU 雲端 API)→ **parse**(`parser.py`→`mineru_data/<slug>/parsed/*.json`)→ **audit**(LLM,產 extract_rules.yaml)→ **catalog**(`build_catalogs`)→ **sol_extract**(LLM 合併解答書)→ **deploy**。
- 確定性階段 daemon 直跑;需判斷的階段(qc/audit/sol_extract)派 headless LLM 跑 `.claude/skills/book-pipeline/references/*.md`。**派工策略單一真相源 = `book_pipeline/llm_policy.py`**(`DispatchSpec` + `DEFAULT_DISPATCH`/`STAGE_DISPATCH` + `resolve_dispatch`/`math_sweep_model`,三層合併 DEFAULT←per-stage←env):per-stage 可宣告 provider chain/model/reasoning effort/timeout。四 provider failover **codex-pool**(codex CLI 走 ccNexus 池子)/**codex**(原生 OAuth)/**kimi**/**claude**(Max),預設 `codex-pool→codex→kimi→claude`;effort 分層(重判斷 high/解析 medium/qc low,僅 codex 家族)。math sweep 走 ccNexus HTTP batch(執行路徑非 CLI)但**模型同源於此配置層**(`math_sweep_model`)。env(`BOOK_PIPELINE_PROVIDER_CHAIN`/`_CODEX_MODEL`/`_CODEX_EFFORT`/`_CLAUDE_MODEL`/`_LLM_TIMEOUT`/`_MATH_MODEL`)僅運維臨時凌駕(`_MATH_MODEL` 為 math sweep 專屬覆寫;未設時 `_CODEX_MODEL` 亦連動 math sweep)。

### 書單 SoT 驅動的 crawl(整套爬書系統的分母,2026-06 重構)

- **`book_pipeline/booklists/*.json` = 整個 project 唯一 SoT**:兩層(領域檔 → 具名子單 → 主書`{slug,title,author}`),人工維護、git 追蹤。**owned 狀態絕不存檔**(由 mineru_data/raw_pdfs 即時推導)。題本不手列:主書 `solution!=false`(預設 true)→ 系統自衍生 `<slug>_sol` target。邏輯層 `booklists.py`(targets/status_of/select_next/catalog/progress/validate/reconcile)。
- **crawl 兩段:解析需判斷(LLM)、選書+下載全確定性**(取代舊「每次補貨把全 wishlist+inventory 丟 LLM 從零重推」的土炮):
  1. **resolver**(`resolve.py` → `crawl_resolution.json` sidecar,gitignore):書單 target(書名,作者)→ z-lib 具體 id/hash 的**唯一需判斷步驟**,交 crawl agent。信心不足:**題本標 absent(永不再查,殺空轉)、主書標 review(待架構師人工裁決,不自動重試)**。一次性 cache。**只 search 不 fetch**,不耗下載額度。解析池(status==ready)< `CRAWL_POOL_LOW`(100)且有 unresolved → 派 agent 解析一批。
     - **下載前書況閘**(`resolution_qc`,共用 `book_qc` detector,零額度純書名比對):`cmd_search` 每筆候選帶 `book_qc` 標註、`cmd_commit` 落盤 resolved 時硬擋鐵定配錯書——**companion(主書抓到 Study Guide/Manual 等週邊,只套 main、解答本豁免)** + **title_mismatch(0%)(候選書名與 SoT 零重疊)**。逼 agent 改挑或 `--review`(`--force` 可繞)。與**部署後 gate**(`book_qc` 部署前,抓 parse 後才知的 `partial_source`/殘卷)兩道分工:上游擋「配錯書」省整條鏈,下游擋「對的書但源殘缺」。校準:全現存 resolved 零假陽(test_resolve_qc 護欄)。
  2. **買書員**(`drain_crawl_queue`,確定性,唯一消費額度處):每 tick 直接 `booklists.select_next(n)` 取解析池 ready 並行下載、落 raw_pdfs(隨即成 owned)。**無購物清單 buffer**(2026-06 簡化:buffer 唯一不可推導的下載失敗計數移 `pipeline_state.json` 的 `q.crawl_fail_*`,達 `MAX_FETCH_FAILS` 即 exclude 出候選)。下載量綁 `CRAWL_INFLIGHT_CAP`(pipeline 在飛上限,backpressure)×額度,非每日固定本數。
- **架構師職責**:resolver 標 review 的主書 → 看 `crawl_resolution.json` 候選、`resolve --force --slug <x>` 重解或人工改 booklists。**重解 review 標記書**是架構師任務(非 worker);worker 的 crawl skill(`references/crawl.md`)只負責解析「未決 target」,且須尊重 `resolution_qc` 的 `book_qc.block`(下載前書況閘,見下「下載前書況閘」)。
- **收錄表 UI**:build 烤 `data/catalog.json`(書單 SoT × 五態:owned/ready/absent/review/unresolved)→ reader library 渲染完整收錄表(三態:已收錄/待收錄/無法收錄)。`data/books.json`(已收錄可讀書)餵內容、`catalog.json` 餵收錄表,並存。
- `status.py` 從**實際資料**判斷每書階段(非檔名臆測);`pipeline_queue.py` 在其前後補 crawl/qc/deploy 組成完整 queue;`pipeline_state.json` 持久化 qc verdict + deploy 狀態避免重複 LLM/部署。
- **路徑根基**:`ROOT = dirname(dirname(__file__))`(`status.py`/`corpus.py`/`pipeline_queue.ROOT`),所有狀態檔/`mineru_data`/`raw_pdfs` 都 repo-root 相對 → 模組原名擺 repo 根下即全自動正確。
- **deploy 已本地化**:`do_deploy()` 只跑 `uv run python -m build.build_all <slug>` 烤出 data/img(nginx 直讀工作目錄即時上站),**無 git push**。
- **部署前書況 gate**(`book_qc.py`,parse 後/build 前):確定性零 LLM 驗「書對不對/完不完整」,攔 crawl 配錯書(`companion`/`title_mismatch`)與殘卷(`partial_source`/`chapter_gap`)——這類源頭缺陷下游 stage 無從補。命中硬缺陷 → `q.mark_book_qc` 標 review、**不上站**,`assess_full` 後續 tick 見標記回 `R 書況` 終止排程(不再耗 build/LLM)。**fail-open**(gate 自身出錯絕不擋好書)。**架構師職責**:`book_audit` 看旗標 → 修 booklists 重解/找完整版 → 重 parse 後 `do_deploy` 自動 `clear_book_qc` 放行。worker 無此職責(屬 crawl/架構師域,故無 skill reference)。

## Build self-contained（build/）

- `bake_json.py`:`from textbooks import corpus`(本地),corpus 即時轉換 dump 成 `data/<slug>/*.json`,順手把 fig/table/catalog 內 `.jpg` 引用改寫 `.webp`(只改字串,轉檔是 convert_images 的事)。
- `convert_images.py`:`book_pipeline/mineru_data/<slug>/unified/images/*.jpg` + cover → `img/<slug>/*.webp`(cwebp q80,mtime 冪等增量,ProcessPoolExecutor)。
- **必須 `-m build.build_all` 從 repo 根跑**(裸跑 script 會斷 `book_pipeline` import)。
- 主力機開發:`book_pipeline/mineru_data` 是 symlink → qbank(省 8.9G);standby 是 rsync 的獨立副本。

## 資料模型

`textbooks/corpus.py` 是 `mineru_data/<slug>/parsed/` 的唯讀層。書 = chapters(`ch`)+ appendices(`app`)。chunk 有 `body`(block 陣列)+ 可選 `problems`。block 用 `t`:`section`/`subsection`、`p`(markdown)、`eq`(LaTeX)、`fig`、`table`、`example`。語言三態 en(預設)/zh/bi(雙語),由 `parsed/*.zh.json` overlay 稀疏合併(防漂移用 anchor hash)。`book_pipeline/booklists/*.json`(書單 SoT)+ `*.zh.json` + `extract_rules.yaml` + `catalog_overrides/` 是 git 唯一追蹤的「貴重成果」(機器產物如 `crawl_resolution.json`/`data/`/`img/` 全 ignore)。

## 前端 reader（index.html）

- Hash 路由:`#`→library,`#slug`→書總覽,`#slug/kind/key`→chunk。
- **Math 視窗化 + derender**(`setupIncrementalMath`,最核心架構決策):整章 typeset 上千公式會阻塞數秒、吃數百 MB。只 typeset 視窗 ±900px 的 unit,捲出 3000px 還原成 `_mathRaw` 原始碼以 placeholder 占位 → 記憶體封頂。改公式渲染前必懂。
- 共用層 `assets/qbank-shared.js`(`QBankShared`)。CDN:MathJax 3、marked 9。

## 部署（standby）

- **靜態托管**:docker compose nginx:alpine,mount repo 根 `:ro`,`restart: always`,bind `127.0.0.1:8001:80`。nginx 直讀磁碟,daemon 烤新檔即時生效(免 reload)。
- **對外**:CF `kg-standby` tunnel(id `03ad6631-…`,zone `wordnexus.lol`)加 ingress `books.wordnexus.lol→localhost:8001`(整包覆寫須含 kg `→8000` 規則 + 末尾 404)+ proxied CNAME。SOP:`~/butler/docs/cloudflare-tunnel-hosting.md`。
- **daemon**:`book_pipeline/daemon_run.sh` + plist(`com.textbookreader.bookpipeline`,反應式:`BOOK_PIPELINE_REACTIVE=1`,launchd StartInterval 15min 重拉、controller walltime ≤50min 自退讓重拉,flock `.tick.lock` 序列化單例)。MinerU token 由 wrapper `source ~/.secrets/mineru.env`(`export MINERU_API_TOKEN[2]=`)注入,勿入 plist/git。
- **改演算法上線(別焦慮,三檔路徑)**:reactive loop **自然退出本就優雅排空**——跑滿 walltime(≤50min)或 idle 收斂後 `finally` 排空在飛 worker 才退,launchd 再載新碼。**排空分流(2026-06,取代舊一律 120s 上限——那正是 rc=-9 集體死亡源頭):可殺的子進程 agent(_inflight_children 非空,codex/claude/kimi)無限等其自然收尾、永不砍**(codex 主力無自我空轉迴圈病理,必收斂);**只有殺不掉的純 thread worker(math sweep HTTP)** 才套 `DRAIN_BOUND`(600s)逾時 `os._exit` 逃生(防純 API thread 凍結 controller + 持 .tick.lock 成孤兒鎖)。配套:per-agent `LLM_TIMEOUT` 與 lease `DEFAULT_TTL` **預設 0=無限**(同因 codex 主力後卡死病理消失;env 設正整數可臨時重新加上限)。**唯一會棄在飛工作的是 `kick -k`(硬殺跳過 finally)**。故:① **預設 = commit 後啥都不做**,daemon ≤50min 自然滾到新碼、零浪費;② **`devctl reload`** = 丟 `reload_request` + SIGUSR1 令 loop 停派新工、排空後優雅退出 + 排一個 detached 小弟在本進程死後立即 `launchctl kickstart` → **零浪費、零空檔**(無在飛工作秒級換碼;有 audit 在飛則排空後才換、不棄工作)。為何要 detached 等死後才 kick:`.tick.lock` 是 NB 鎖,舊實例沒死透時新實例搶不到鎖會「跳過本次」;只在 reload 走、idle/walltime 自然退出不觸發 → 維持收斂;與 StartInterval 撞期由 NB 鎖序列化、不雙跑;③ **`devctl kick`** = 硬殺重啟,**只在現役碼壞了/卡死**才用(接受棄工作)。跑哪版碼不必再做 forensics:controller 起頭把 `git short SHA` 寫進 `.controller.json`,**`devctl status` 直接顯示 `code=<sha> vs HEAD · 落後 N commit`**。
- **監控**:`/dev` 頁(`dev/index.html`,復用 reader 元件)即時看 daemon/budget/錯誤/書本階段。單一真相源 `book_pipeline/devctl.py`——網頁與 CLI 共用:`devctl status|snapshot|errors|incident|kick|reload`(`reload`=優雅載新碼〔見上「改演算法上線」〕:丟 `reload_request` marker + SIGUSR1 令 reactive loop 馬上 re-observe〔**不殺在飛 worker**;閒置才 kick〕;controller 狀態 `.controller.json`{pid,sha} 供 signal 定址 + 版本觀測)。snapshot 由 `pipeline_tick.log()` 事件驅動刷新(節流 1s——核 status.json 已拆小〔per-book timeline/sessions→`dev/detail/<slug>.json` on-demand、errors/log/corpus→`dev/system.json`〕故可 1s 直驅看板、stages.json 繞道已退役)+ `com.textbookreader.devsnapshot` plist 60s 心跳;寫 `dev/status.json`(純 live 核,gitignore)。**出事除錯入口 = `uv run python -m book_pipeline.devctl incident`**。
- **`/dev` 存取**:CF Access 信箱閘(app `textbook-dev`,只給 max970228,session 1 月)+ nginx `Cf-Access-Authenticated-User-Email` header 把關。設定/改 policy 全 CLI,SOP `~/butler/docs/cloudflare-tunnel-hosting.md` §9。
- 憑證:`~/.secrets/{mineru.env,zlib.env,zlib_session.json}`、`cloudflare_token`(含 Access scope)、rclone(Drive 備份)。各機獨立,絕不入 git。

## Gotchas

- CF ingress PUT 全覆寫,漏 kg 規則會打掛正式站 wordnexus.lol。
- `cwebp` 須裝(`brew install webp`),否則 convert 失敗。
- `lint_latex.py` 有壞 import(`from config import ...`,函式不存在)但不在 tick 路徑,照留;**不**拖 qbank 的 `config.py` 進來。
- MinerU/zlib 憑證各機獨立,絕不入 git。
