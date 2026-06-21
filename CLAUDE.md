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

上四者觀測 live pipeline;另一軸是**決策日誌**——

- **`proposals`**(`book_pipeline/proposals.py`,`proposals.d/<id>.json` 一案一檔 + `_index.md` 視圖) — 各 agent 跑到一半發現「值得跨書泛化、但 autonomous 不該擅改核心碼」就一行 `propose` 落案;**這是 provenance/稽核軌跡,不是等人核准的佇列**。架構師裁決 SOP:**先查真相層再裁,絕不照提案文字拍腦袋**——engine/patch(scope_guard 捕獲,多是 worker 越界改進已被主線收編的殘留)`grep` working-tree 對應函式是否已存在(已落地→`superseded`)、math/normalize-rule 跑 `proposals check` 看 live aggregate occ(0→`rejected/already-resolved`,逐條 override 已清則全域規則多餘)。**批次事務工具**:`resolve` 收多 id 或 `--where-domain/-type/-source` 過濾 proposed(刻意只命中 proposed)→ 全批先套用、一次 lint、全過才落盤、結尾只 render 一次(all-or-nothing,`--dry-run` 先看圈中誰);成批同去向別再外部迴圈硬湊。**caption 假缺口自動護欄**:`proposals supersede-resolved [--dry-run]`——掃 proposed 的 caption/catalog tooling-gap,凡其書 `audit_catalog` live critical=0(=已被下游 `repair_catalog_metadata` 涵蓋)即自動 superseded,用真實 critical 數當裁判、非讀提案文字(只認 caption 類關鍵字故不誤觸 conway/poole 章節題號缺口;critical>0 真殘留/-1 無法判定皆保守留 proposed;`_sol` 排除不自動裁)。把「audit agent 撞 repair 前 H6/H7 誤判成引擎缺口」這類假提案的人工裁決自動化(根因見 audit-book.md §5 smoke 表 H6/H7 列)。

## Pipeline 架構（book_pipeline/）

`pipeline_tick.py` 是 launchd 每 ~45min 觸發的 daemon 單 tick,推進整條鏈:
- **crawl**(z-library 爬書:選書/下載確定性、四維查證走 LLM,見下「合格存在四維模型」)→ **triage**(pdf_triage)→ **qc**(視覺驗證,LLM)→ **ingest**(`mineru_ingest`+`mineru_budget` 多帳號預算,MinerU 雲端 API)→ **parse**(`parser.py`→`mineru_data/<slug>/parsed/*.json`)→ **audit**(LLM,產 extract_rules.yaml)→ **catalog**(`build_catalogs`)→ **sol_extract**(LLM 合併解答書)→ **deploy**。
- 確定性階段 daemon 直跑;需判斷的階段(qc/audit/sol_extract)派 headless LLM 跑 `.claude/skills/book-pipeline/references/*.md`。**派工策略單一真相源 = `book_pipeline/llm_policy.py`**(`DispatchSpec` + `DEFAULT_DISPATCH`/`STAGE_DISPATCH` + `resolve_dispatch`/`math_sweep_model`,三層合併 DEFAULT←per-stage←env):per-stage 可宣告 provider chain/model/reasoning effort/timeout。三 provider failover **codex-pool**(codex CLI 走 ccNexus 池子)/**codex**(原生 OAuth)/**claude**(Max 保底),預設 `codex-pool→codex→claude`(kimi 已於 2026-06-20 下架:斷線窗 fallback 品質不可靠,寧落 Claude Max);effort 分層(重判斷 high/qc low,僅 codex 家族;crawl 改 claude-only 多源查證、無 effort)。math sweep 走 ccNexus HTTP batch(執行路徑非 CLI)但**模型同源於此配置層**(`math_sweep_model`)。env(`BOOK_PIPELINE_PROVIDER_CHAIN`/`_CODEX_MODEL`/`_CODEX_EFFORT`/`_CLAUDE_MODEL`/`_LLM_TIMEOUT`/`_MATH_MODEL`)僅運維臨時凌駕(`_MATH_MODEL` 為 math sweep 專屬覆寫;未設時 `_CODEX_MODEL` 亦連動 math sweep)。

### 合格存在四維模型驅動的 crawl(整套爬書系統的分母,2026-06 重構)

**核心原則(使用者拍板)**:一本書「**合格存在**」⟺ 四維全過——① 夠格收錄(大學級教科書/專著/講義/參考書,理工優先) ② z-lib 有可下載連結 ③ 版本號確認(符合 edition_pref) ④(有解答本則)解答本與母書版本對齊。**任一沒驗 = 不算數 = 不該存在於系統**。沒有「待查書單/wishlist」——沒連結的書就是不存在。

- **三層資料架構**(寫入頻率 × 持久化 × 人工/機器 三正交):
  - **`book_pipeline/fields.json`**(git,人工)= 領域骨架 `[{field_id,field,order}]`(顯示名+排序)。
  - **`book_pipeline/editions/<slug>.json`**(git,LLM agent+遷移)= **universe**:每本「存在」的書(owned∪已連結∪discovery 候選)的完整記錄〔`identity` 身份 + `classification` 分類 + 四維結論 `qualification.eligible`(維①)/`version.matches_pref`(維③)/`sol_alignment.aligned`(維④)〕。**沒 editions 檔的書＝不存在**(舊無連結 wishlist 書自然消失)。
  - **`book_pipeline/crawl_resolution.json`**(gitignore,高頻)= 純連結快取 `{status: found|not_found}`(維②,可重生)。
- **`booklists.py` = 由 editions 派生的狀態 shim**(舊 `booklists/*.json` wishlist 已退役封存 `booklists/_archive/`):`targets`(由 editions 主書記錄派生 + `identity.has_solution` 衍生 `<slug>_sol`)/`status_of`(五態 OWNED/QUALIFIED/PENDING/CANDIDATE/REJECTED,**owned 保命最優先**)/`select_next`(只取 QUALIFIED)/`pending_targets`(存量回查母體,recheck cooldown 阻 busy-loop)/`catalog`/`progress`/`validate`/`reconcile_owned`。**書單自我生長**:discovery 找夠格新書寫 `discovered/` 機器候選層→`targets()` 合併→走四維查證;人 git diff 否決(`discovered remove`)或晉升;agent 絕不寫人工正典(daemon **自動 discovery 預設關閉**,只 /restock 使用者親打才 discovery)。
- **`/restock` 自包含 skill(使用者入口)**:固定使命「合格書目淨增 100」、零指示、四維綁定查證、存量優先(pending 回查)→ discovery 補足、自我終止。daemon 模式(`do_crawl_resolve`)= 同引擎但**只做庫存查證(不 discovery)**。
- **crawl 兩段:四維查證需判斷(LLM)、選書+下載全確定性**:
  1. **庫存查證 agent**(`resolve.py`+`editions.py`,skill `references/booklist-manager.md` = /restock 四維引擎):每本 fan-out 多 haiku 交叉查 z-lib+web,**維②連結落 `resolve commit`(found/not_found)、維①③④落 `editions set`**(走 claude-only 因需 subagent fan-out)。只有別版 → commit found + `--no-matches-pref`(→PENDING 可重查);歧義 → 開 proposal 留 CANDIDATE;owned mismatch → proposal(`edition-mismatch`)**絕不下架**。**只 search 不 fetch**。合格池 `qualified_ready` < `CRAWL_POOL_LOW`(100) ∧ 工作母體(candidate∪actionable pending) > 0 → 派 agent。**收斂 latch**:PENDING recheck cooldown(`RECHECK_COOLDOWN_DAYS` 30,憑 `editions.checked_at`)阻 busy-loop,母體枯竭即 due False。
     - **下載前書況閘**(`resolution_qc`,共用 `book_qc` detector,零額度純書名比對):`cmd_search` 每筆候選帶 `book_qc` 標註、`cmd_commit` 落盤 found 時硬擋鐵定配錯書(**companion** + **title_mismatch(0%)**)。逼 agent 改挑或開 proposal(`--force` 可繞)。與**部署後 gate**兩道分工:上游擋「配錯書」、下游擋「對的書但源殘缺」。
  2. **買書員**(`drain_crawl_queue`,確定性,唯一消費額度處):每 tick 直接 `booklists.select_next(n)` 取**合格池 QUALIFIED** 並行下載、落 raw_pdfs(隨即成 owned)。下載失敗計數 `pipeline_state.json` 的 `q.crawl_fail_*`,達 `MAX_FETCH_FAILS` 即 exclude。下載量綁 `CRAWL_INFLIGHT_CAP`×額度。
- **架構師職責**:歧義書(proposal) → 人工裁決重解或改 editions;owned mismatch(proposal `edition-mismatch`) → 換版重 ingest/改 pref/維持。
- **收錄表 UI**:build 烤 `data/catalog.json`(editions universe × 五態,經 `_public_status` 摺疊回前端舊三態字串 → reader 零改)→ reader library 渲染收錄表(三態:已收錄/待收錄/無法收錄)。沒 editions 的書不在收錄表(落實「沒連結＝不存在」)。`data/books.json`(已收錄可讀書)餵內容、`catalog.json` 餵收錄表,並存。
- `status.py` 從**實際資料**判斷每書階段(非檔名臆測);`pipeline_queue.py` 在其前後補 crawl/qc/deploy 組成完整 queue;`pipeline_state.json` 持久化 qc verdict + deploy 狀態避免重複 LLM/部署。
- **路徑根基**:`ROOT = dirname(dirname(__file__))`(`status.py`/`corpus.py`/`pipeline_queue.ROOT`),所有狀態檔/`mineru_data`/`raw_pdfs` 都 repo-root 相對 → 模組原名擺 repo 根下即全自動正確。
- **deploy 已本地化**:`do_deploy()` 只跑 `uv run python -m build.build_all <slug>` 烤出 data/img(nginx 直讀工作目錄即時上站),**無 git push**。
- **部署前書況 gate**(`book_qc.py`,parse 後/build 前):確定性零 LLM 驗「書對不對/完不完整」,攔 crawl 配錯書(`companion`/`title_mismatch`)與殘卷(`partial_source`/`chapter_gap`)——這類源頭缺陷下游 stage 無從補。命中硬缺陷 → `q.mark_book_qc` 標 review、**不上站**,`assess_full` 後續 tick 見標記回 `R 書況` 終止排程(不再耗 build/LLM)。**fail-open**(gate 自身出錯絕不擋好書)。**架構師職責**:`book_audit` 看旗標 → 修 booklists 重解/找完整版 → 重 parse 後 `do_deploy` 自動 `clear_book_qc` 放行。worker 無此職責(屬 crawl/架構師域,故無 skill reference)。
- **audit 結構性卡關終態 `R audit-blocked`**(與 `R 書況` 平行的「需人工裁決」終態):audit agent 跑完(rc==0)卻產不出 `extract_rules.yaml` 且已開 engine 提案(=schema 表達不了,如 aitchison combined 2-volume 非連續多區附錄)→ `advance_book` 一次 `q.mark_audit_blocked` 標 review、**停止跨 tick 重派空轉**(此前曾空轉 8 次重推同一 blocker);`assess` 見標記回 `R audit-blocked`、進 `trace stuck`/cohort ⚠。**架構師職責**:改 booklists/手寫 yaml/降規格繞過(如末章 `next_chapter_block_idx` 留 gap 跳過中段附錄);產出 yaml 後 advance 自動 `clear_audit_blocked` 放行。一次定生死、不賭 LLM 隨機重試。

## Build self-contained（build/）

- `bake_json.py`:`from textbooks import corpus`(本地),corpus 即時轉換 dump 成 `data/<slug>/*.json`,順手把 fig/table/catalog 內 `.jpg` 引用改寫 `.webp`(只改字串,轉檔是 convert_images 的事)。
- `convert_images.py`:`book_pipeline/mineru_data/<slug>/unified/images/*.jpg` + cover → `img/<slug>/*.webp`(cwebp q80,mtime 冪等增量,ProcessPoolExecutor)。
- **必須 `-m build.build_all` 從 repo 根跑**(裸跑 script 會斷 `book_pipeline` import)。
- 主力機開發:`book_pipeline/mineru_data` 是 symlink → qbank(省 8.9G);standby 是 rsync 的獨立副本。

## 資料模型

`textbooks/corpus.py` 是 `mineru_data/<slug>/parsed/` 的唯讀層。書 = chapters(`ch`)+ appendices(`app`)。chunk 有 `body`(block 陣列)+ 可選 `problems`。block 用 `t`:`section`/`subsection`、`p`(markdown)、`eq`(LaTeX)、`fig`、`table`、`example`。語言三態 en(預設)/zh/bi(雙語),由 `parsed/*.zh.json` overlay 稀疏合併(防漂移用 anchor hash)。git 追蹤的「貴重成果/候選」:`book_pipeline/editions/`(**書目 universe**:身份+分類+四維結論,LLM 親查) + `book_pipeline/fields.json`(領域骨架) + `discovered/`(discovery 機器候選層,人可否決/晉升) + `*.zh.json` + `extract_rules.yaml` + `catalog_overrides/`;`booklists/_archive/`(舊 wishlist 已退役封存、僅供遷移腳本讀)。機器產物 `crawl_resolution.json`〔純連結快取〕/`data/`/`img/` 全 ignore。

## 前端 reader（index.html）

- Hash 路由:`#`→library,`#slug`→書總覽,`#slug/kind/key`→chunk。
- **Math 視窗化 + derender**(`setupIncrementalMath`,最核心架構決策):整章 typeset 上千公式會阻塞數秒、吃數百 MB。只 typeset 視窗 ±900px 的 unit,捲出 3000px 還原成 `_mathRaw` 原始碼以 placeholder 占位 → 記憶體封頂。改公式渲染前必懂。
- 共用層 `assets/qbank-shared.js`(`QBankShared`)。CDN:MathJax 3、marked 9。

## 部署（standby）

- **靜態托管**:docker compose nginx:alpine,mount repo 根 `:ro`,`restart: always`,bind `127.0.0.1:8001:80`。nginx 直讀磁碟,daemon 烤新檔即時生效(免 reload)。
- **對外**:CF `kg-standby` tunnel(id `03ad6631-…`,zone `wordnexus.lol`)加 ingress `books.wordnexus.lol→localhost:8001`(整包覆寫須含 kg `→8000` 規則 + 末尾 404)+ proxied CNAME。SOP:`~/butler/docs/cloudflare-tunnel-hosting.md`。
- **daemon**:`book_pipeline/daemon_run.sh` + plist(`com.textbookreader.bookpipeline`,反應式:`BOOK_PIPELINE_REACTIVE=1`,launchd StartInterval 15min 重拉、controller walltime ≤50min 自退讓重拉,flock `.tick.lock` 序列化單例)。MinerU token 由 wrapper `source ~/.secrets/mineru.env`(`export MINERU_API_TOKEN[2]=`)注入,勿入 plist/git。
- **post-deploy 自動 GC**(`pipeline_tick.do_post_deploy_gc`→`storage_gc.gc_book`,見記憶 [[storage-gc-tiering-system]]):每 tick 末清「已穩定上站(`deployed_at`≥`GC_STABILITY_MIN` 120min)∧非在飛(`mb.in_flight`/`occupied`,含 `_sol`)」書的 🟡 可重生中間產物(raw/chunk_*/ 解壓檔+chunks/ 切割 PDF),**人工免再定期 prune**。reactive 經 `_gc_due` 節流(`GC_INTERVAL_SEC` 1800s/controller,免每 cycle 掃全書)+ `__post_deploy_gc__` det-worker(/dev 顯 🧹)。**安全根基**:所有 post-deploy 階段(sol/catalog/math/build/serving)只讀 parsed/+unified/、**永不碰 raw/chunk_*/或 chunks/** → GC 刪除集與並行 worker 目錄不相交、零競爭(日後新增「已上站書回讀 raw」階段必須在 `_gc_candidates` 同步排除其 slug)。手動治理同工具:`storage_gc report|prune|archive|restore|reassemble|migrate`(預設 dry-run)。**冷藏目的地 ARCHIVE_ROOT=felix 常駐外接碟 `/Volumes/TOSHIBA EXT/textbook-reader-cold`(2TB exFAT,sidecar `.storage_gc.json` 指定);archive=事務式 move(copy→校驗→刪源)釋放工作碟⇒冷藏卷即唯一副本、restore=copy 拷回。手動 archive/restore/report 須先 oscar 打 `s` 進 felix cmux(launchd tmux server 才有 FDA),經普通 ssh/Terminal 碰碟 `Operation not permitted`(TCC,非權限 bug)。碟機制/查閱總覽見 `~/project/AGENTS.md` 外接冷藏碟段。**
- **改演算法上線(別焦慮,三檔路徑)**:reactive loop **自然退出本就優雅排空**——跑滿 walltime(≤50min)或 idle 收斂後 `finally` 排空在飛 worker 才退,launchd 再載新碼。**排空分流(2026-06,取代舊一律 120s 上限——那正是 rc=-9 集體死亡源頭):可殺的子進程 agent(_inflight_children 非空,codex/claude)無限等其自然收尾、永不砍**(codex 主力無自我空轉迴圈病理,必收斂);**只有殺不掉的純 thread worker(math sweep HTTP)** 才套 `DRAIN_BOUND`(600s)逾時 `os._exit` 逃生(防純 API thread 凍結 controller + 持 .tick.lock 成孤兒鎖)。配套:per-agent `LLM_TIMEOUT` 與 lease `DEFAULT_TTL` **預設 0=無限**(同因 codex 主力後卡死病理消失;env 設正整數可臨時重新加上限)。**唯一會棄在飛工作的是 `kick -k`(硬殺跳過 finally)**。故:① **預設 = commit 後啥都不做**,daemon ≤50min 自然滾到新碼、零浪費;② **`devctl reload`** = 丟 `reload_request` + SIGUSR1 令 loop 停派新工、排空後優雅退出 + 排一個 detached 小弟在本進程死後立即 `launchctl kickstart` → **零浪費、零空檔**(無在飛工作秒級換碼;有 audit 在飛則排空後才換、不棄工作)。為何要 detached 等死後才 kick:`.tick.lock` 是 NB 鎖,舊實例沒死透時新實例搶不到鎖會「跳過本次」;只在 reload 走、idle/walltime 自然退出不觸發 → 維持收斂;與 StartInterval 撞期由 NB 鎖序列化、不雙跑;③ **`devctl kick`** = 硬殺重啟,**只在現役碼壞了/卡死**才用(接受棄工作)。跑哪版碼不必再做 forensics:controller 起頭把 `git short SHA` 寫進 `.controller.json`,**`devctl status` 直接顯示 `code=<sha> vs HEAD · 落後 N commit`**。
- **監控**:`/dev` 頁(`dev/index.html`,復用 reader 元件)即時看 daemon/budget/錯誤/書本階段。單一真相源 `book_pipeline/devctl.py`——網頁與 CLI 共用:`devctl status|snapshot|errors|incident|kick|reload|pause|resume`(`pause`/`resume`=系統暫停/啟動〔見下「系統暫停/啟動」〕;`reload`=優雅載新碼〔見上「改演算法上線」〕:丟 `reload_request` marker + SIGUSR1 令 reactive loop 馬上 re-observe〔**不殺在飛 worker**;閒置才 kick〕;controller 狀態 `.controller.json`{pid,sha} 供 signal 定址 + 版本觀測)。snapshot 由 `pipeline_tick.log()` 事件驅動刷新(節流 1s——核 status.json 已拆小〔per-book timeline/sessions→`dev/detail/<slug>.json` on-demand、errors/log/corpus→`dev/system.json`〕故可 1s 直驅看板、stages.json 繞道已退役)+ `com.textbookreader.devsnapshot` plist 60s 心跳;寫 `dev/status.json`(純 live 核,gitignore)。**出事除錯入口 = `uv run python -m book_pipeline.devctl incident`**。
- **`/dev` 存取**:CF Access 信箱閘(app `textbook-dev`,只給 max970228,session 1 月)+ nginx `Cf-Access-Authenticated-User-Email` header 把關。設定/改 policy 全 CLI,SOP `~/butler/docs/cloudflare-tunnel-hosting.md` §9。
- **`/dev` 寫回控制平面(devcontrol sidecar)**:面板互動寫回(① zlib 帳號停用 toggle/流量控制 `POST /account`;② 系統暫停/啟動 `POST /pause`)走 docker compose **第二服務 `devcontrol`**(`book_pipeline/dev_control.py`,python:3.13-alpine,純 stdlib 零 requests、**零憑證**)——**不 publish port**(只 compose 內網 `devcontrol:8002`),唯一 rw 掛載 `book_pipeline/.control/`(停用態 + 運行/暫停態狀態檔,窄掛碰不到 *.py)。nginx `location ^~ /dev/api/` proxy_pass 進去,同 CF Access header 把關 + 轉發 header 供服務復驗 max970228(縱深三層:CF Access→nginx 空 header 403→服務復驗精確 email)。**部署須一併 `docker compose up -d devcontrol`**(漏起 → 面板 toggle 502,但純讀觀測不受影響);改 `dev_control.py` 碼後 `docker compose restart devcontrol`。流量控制 SoT = `crawl_zlib`/`zlib_control_state`(等效 CLI `crawl_zlib disable/enable/accounts`,架構師面、不洩漏給 worker skill)。
- **系統暫停/啟動**:暫停 = daemon reactive loop **停派一切新工**(crawl + 全階段),在飛 worker 自然收尾(同 reload 不殺);loop 保活輪詢 `pipeline_run_state`(`.control/pipeline_run_state.json`),resume ≤`LOOP_POLL`(~20s)生效(`devctl resume` 另送 SIGUSR1 秒級喚醒)。**預設暫停**(fail-safe,與 zlib 停用態 fail-open 刻意相反):狀態檔不存在/壞/`running!=true` → 暫停 → **部署/首啟即暫停,待人工按啟動**。控制三路等效:面板 topbar ⏸/▶ 鈕(`POST /dev/api/pause`)、CLI `devctl pause`/`resume`、直寫狀態檔;狀態經 `devctl snapshot` 的 `paused` 欄反映上 /dev。SoT = `pipeline_run_state`(dep-light,host pipeline_tick/devctl 與 sidecar 共用)。
- 憑證:`~/.secrets/{mineru.env,zlib.env,zlib_session.json}`、`cloudflare_token`(含 Access scope)、rclone(Drive 備份)。各機獨立,絕不入 git。

## Gotchas

- CF ingress PUT 全覆寫,漏 kg 規則會打掛正式站 wordnexus.lol。
- `cwebp` 須裝(`brew install webp`),否則 convert 失敗。
- `lint_latex.py` 有壞 import(`from config import ...`,函式不存在)但不在 tick 路徑,照留;**不**拖 qbank 的 `config.py` 進來。
- MinerU/zlib 憑證各機獨立,絕不入 git。
