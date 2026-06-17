# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 本質

**自包含**的教科書 pipeline + 靜態 reader 站。一條鏈 crawl→ingest(MinerU 雲端 OCR)→qc→parse→audit→catalog→sol→bake→serve 全住本 repo。原本依賴外部 `qbank` repo,已整條搬進來(`book_pipeline/` + `textbooks/`)。常駐主機 standby(`100.118.39.104`)24hr 跑 daemon 全自動產書,經 Cloudflare `kg-standby` tunnel 自託管於 **books.wordnexus.lol**。**不再用 GitHub Pages**(remote 僅留作 git 跨機同步)。

前端是單頁 reader(`index.html`,全 JS inline)。`data/`(JSON)+ `img/`(WebP)是 build 從 `book_pipeline/mineru_data/` 烤出的**本地產物**,**不入 git**,standby 上由 daemon 自動重生。

## 指令

```bash
uv run python -m build.build_all [slug ...]              # 烤靜態站（不帶 slug = 全部書）
uv run python -m book_pipeline.status                    # 全書 stage 儀表板
uv run python -m book_pipeline.pipeline_tick --dry-run   # daemon 單 tick 計畫（不執行）
uv run python -m book_pipeline.pipeline_queue            # 跨書全 stage work-queue
uv run python -m http.server 8001                        # 本機預覽
```

一律 `uv run`(專案 python 規範禁裸 `python3`)。`pyproject.toml` 宣告依賴,免 `--with` sprawl。

## Pipeline 架構（book_pipeline/）

`pipeline_tick.py` 是 launchd 每 ~45min 觸發的 daemon 單 tick,推進整條鏈:
- **crawl**(z-library 爬書,LLM 派 headless `claude -p`)→ **triage**(pdf_triage)→ **qc**(視覺驗證,LLM)→ **ingest**(`mineru_ingest`+`mineru_budget` 多帳號預算,MinerU 雲端 API)→ **parse**(`parser.py`→`mineru_data/<slug>/parsed/*.json`)→ **audit**(LLM,產 extract_rules.yaml)→ **catalog**(`build_catalogs`)→ **sol_extract**(LLM 合併解答書)→ **deploy**。
- 確定性階段 daemon 直跑;需判斷的階段派 headless claude 跑 `.claude/skills/book-pipeline/references/*.md`。
- `status.py` 從**實際資料**判斷每書階段(非檔名臆測);`pipeline_queue.py` 在其前後補 crawl/qc/deploy 組成完整 queue;`pipeline_state.json` 持久化 qc verdict + deploy 狀態避免重複 LLM/部署。
- **路徑根基**:`ROOT = dirname(dirname(__file__))`(`status.py`/`corpus.py`/`pipeline_queue.ROOT`),所有狀態檔/`mineru_data`/`raw_pdfs` 都 repo-root 相對 → 模組原名擺 repo 根下即全自動正確。
- **deploy 已本地化**:`do_deploy()` 只跑 `uv run python -m build.build_all <slug>` 烤出 data/img(nginx 直讀工作目錄即時上站),**無 git push**。

## Build self-contained（build/）

- `bake_json.py`:`from textbooks import corpus`(本地),corpus 即時轉換 dump 成 `data/<slug>/*.json`,順手把 fig/table/catalog 內 `.jpg` 引用改寫 `.webp`(只改字串,轉檔是 convert_images 的事)。
- `convert_images.py`:`book_pipeline/mineru_data/<slug>/unified/images/*.jpg` + cover → `img/<slug>/*.webp`(cwebp q80,mtime 冪等增量,ProcessPoolExecutor)。
- **必須 `-m build.build_all` 從 repo 根跑**(裸跑 script 會斷 `book_pipeline` import)。
- 主力機開發:`book_pipeline/mineru_data` 是 symlink → qbank(省 8.9G);standby 是 rsync 的獨立副本。

## 資料模型

`textbooks/corpus.py` 是 `mineru_data/<slug>/parsed/` 的唯讀層。書 = chapters(`ch`)+ appendices(`app`)。chunk 有 `body`(block 陣列)+ 可選 `problems`。block 用 `t`:`section`/`subsection`、`p`(markdown)、`eq`(LaTeX)、`fig`、`table`、`example`。語言三態 en(預設)/zh/bi(雙語),由 `parsed/*.zh.json` overlay 稀疏合併(防漂移用 anchor hash)。`*.zh.json` + `extract_rules.yaml` + `catalog_overrides/` 是 git 唯一追蹤的「貴重成果」(機器產物全 ignore)。

## 前端 reader（index.html）

- Hash 路由:`#`→library,`#slug`→書總覽,`#slug/kind/key`→chunk。
- **Math 視窗化 + derender**(`setupIncrementalMath`,最核心架構決策):整章 typeset 上千公式會阻塞數秒、吃數百 MB。只 typeset 視窗 ±900px 的 unit,捲出 3000px 還原成 `_mathRaw` 原始碼以 placeholder 占位 → 記憶體封頂。改公式渲染前必懂。
- 共用層 `assets/qbank-shared.js`(`QBankShared`)。CDN:MathJax 3、marked 9。

## 部署（standby）

- **靜態托管**:docker compose nginx:alpine,mount repo 根 `:ro`,`restart: always`,bind `127.0.0.1:8001:80`。nginx 直讀磁碟,daemon 烤新檔即時生效(免 reload)。
- **對外**:CF `kg-standby` tunnel(id `03ad6631-…`,zone `wordnexus.lol`)加 ingress `books.wordnexus.lol→localhost:8001`(整包覆寫須含 kg `→8000` 規則 + 末尾 404)+ proxied CNAME。SOP:`~/butler/docs/cloudflare-tunnel-hosting.md`。
- **daemon**:`book_pipeline/daemon_run.sh` + plist(`com.textbookreader.bookpipeline`,45min/tick)。MinerU token 由 wrapper `source ~/.secrets/mineru.env`(`export MINERU_API_TOKEN[2]=`)注入,勿入 plist/git。
- **改演算法上線(別焦慮,三檔路徑)**:reactive loop **自然退出本就優雅排空**——跑滿 walltime(≤50min)或 idle 收斂後 `finally: ex.shutdown(wait=True)` 等在飛 audit/advance 跑完才退,launchd 再載新碼。**唯一會棄在飛工作的是 `kick -k`(硬殺跳過 finally)**。故:① **預設 = commit 後啥都不做**,daemon ≤50min 自然滾到新碼、零浪費;② **`devctl reload`** = 丟 `reload_request` + SIGUSR1 令 loop 停派新工、排空後優雅退出 → launchd ≤15min 載新碼(零浪費的「快一點」);③ **`devctl kick`** = 硬殺重啟,**只在現役碼壞了/卡死**才用(接受棄工作)。跑哪版碼不必再做 forensics:controller 起頭把 `git short SHA` 寫進 `.controller.json`,**`devctl status` 直接顯示 `code=<sha> vs HEAD · 落後 N commit`**。
- **監控**:`/dev` 頁(`dev/index.html`,復用 reader 元件)即時看 daemon/budget/錯誤/書本階段。單一真相源 `book_pipeline/devctl.py`——網頁與 CLI 共用:`devctl status|snapshot|errors|incident|kick|reload|crawl-refill`(`reload`=優雅載新碼〔見上「改演算法上線」〕;`crawl-refill`=手動丟「強制補貨」marker `crawl_refill_request`+**立即**喚醒 daemon〔運轉中送 SIGUSR1 令 reactive loop 馬上 re-observe 撿 marker、**不殺在飛 worker**;閒置才 kick〕,無視水位/冷卻派 crawl 小弟補書單;外部只丟 marker→不搶寫 crawl_queue.json,UI 化也只換前端。SIGUSR1 統一語意=「醒來看控制 marker」,refill/reload 共用;controller 狀態 `.controller.json`{pid,sha} 供 signal 定址 + 版本觀測)。snapshot 由 `pipeline_tick.log()` 事件驅動刷新(節流 8s)+ `com.textbookreader.devsnapshot` plist 60s 心跳;寫 `dev/status.json`(gitignore)。**出事除錯入口 = `uv run python -m book_pipeline.devctl incident`**。
- **`/dev` 存取**:CF Access 信箱閘(app `textbook-dev`,只給 max970228,session 1 月)+ nginx `Cf-Access-Authenticated-User-Email` header 把關。設定/改 policy 全 CLI,SOP `~/butler/docs/cloudflare-tunnel-hosting.md` §9。
- 憑證:`~/.secrets/{mineru.env,zlib.env,zlib_session.json}`、`cloudflare_token`(含 Access scope)、rclone(Drive 備份)。各機獨立,絕不入 git。

## Gotchas

- CF ingress PUT 全覆寫,漏 kg 規則會打掛正式站 wordnexus.lol。
- `cwebp` 須裝(`brew install webp`),否則 convert 失敗。
- `lint_latex.py` 有壞 import(`from config import ...`,函式不存在)但不在 tick 路徑,照留;**不**拖 qbank 的 `config.py` 進來。
- MinerU/zlib 憑證各機獨立,絕不入 git。
