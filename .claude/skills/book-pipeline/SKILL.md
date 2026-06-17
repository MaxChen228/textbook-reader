---
name: book-pipeline
description: 教科書生成管理的單一入口。掃描每本參考書在 pipeline 的真實階段（raw PDF → MinerU ingest → audit 切章節題目 → 解答 merge）、規劃並建議下一步，再分派到對應子流程執行。**只要使用者提到教科書/參考書的 ingest、上傳 PDF 給 MinerU、解析章節題目、產 extract_rules.yaml、merge 解答書、查書本 pipeline 進度/狀態、處理 raw_pdfs/、或單純打 /book-pipeline，就用這個 skill。** 翻譯走獨立的 /translate-book。
---

# Book Pipeline — 教科書生成管理

參考書從 raw PDF 到可瀏覽題庫的**唯一管理入口**。核心職責：**掃描現狀 → 規劃 → 建議下一步 → 分派**。
不要憑記憶臆測哪本書到哪一步——一律先跑掃描，讓資料說話。

**部署前提（已定案）：全部在本機完成。** 無雲端 worker、無 Drive 佇列、無多機接力。每本書一條龍跑完。

## Pipeline 全景（stage 依序）

```
raw_pdfs/<slug>.pdf
  │  ingest（本機一條龍：切片→PUT→poll→assemble，一個指令跑完）   → references/ingest.md
  ▼
unified/content_list.json
  │  audit-book（LLM 產 extract_rules.yaml → parser.py 切章節題目）→ references/audit-book.md
  ▼
parsed/{book.json, chNN.json}
  │  audit-sol（可選，有 <slug>_sol 解答本時：merge solution）   → references/audit-sol.md
  │  translate（可選，獨立 skill）                              → /translate-book
  ▼
/browse/textbook 可瀏覽
```

**設計前提**：ingest 是單一本機一條龍流程（`mineru_ingest <pdf> --slug`，submit+poll+assemble 全包）；跨日卡 quota 時靠引擎冪等（done chunk 在 `raw/` 自動跳過）重跑同指令補完。audit 兩版是同一範式（LLM 產 yaml → 確定性引擎 → metric → iterate）的不同對象（主書/解答書）。共用鐵則見本檔末，細節進各 reference。

## 主流程

### 1. 掃描現狀（一律先做）

```bash
uv run python -m book_pipeline.status
```

這是 pipeline 的**單一真相**（讀實際資料、非檔名臆測）。輸出每本書的階段 + 中性動詞待辦：

| 階段 | 意義 | 待辦動詞 |
|---|---|---|
| `0 待ingest` | raw_pdfs 有 PDF、unified 未產 | `ingest` |
| `0.5 ingest中斷` | 已 PUT、unified 未組（`_pending_batches.json` 有）→ 重跑同指令冪等補完 | `ingest` |
| `X 未ingest` | 該 slug 完全沒進 pipeline、本機也無 PDF（異常，補 PDF 才能跑） | `ingest` |
| `1 待audit` | unified 已產、無 extract_rules.yaml | `audit` |
| `2 待parse` | yaml 在但 parser 沒跑過（罕見） | `parse` |
| `3 parsed` | 已切章節題目，可瀏覽 | `sol_extract(<sol>)` / `translate(可選)` / — |
| `4 sol已merge` | 解答已併入 | `translate(可選)` / — |

**補掃一件 status 看不到的**：
- **raw_pdfs 新書未登錄 slug_map**：`ls raw_pdfs/*.pdf`，比對 `book_pipeline/slug_map.json` 的 map。有 PDF 但無對應條目 → 提示「先在 slug_map.json 補一行 `檔名: slug`」（slug 無法純機械推，見 ingest.md）。

### 2. 規劃 + 建議

把 dashboard 的「非可選待辦」按 pipeline 順序歸類，呈現精簡規劃，例如：

```
待辦規劃（依 pipeline 順序）：
  ① <新書>.pdf     → 補 slug_map → ingest
  ② boas_mp        → sol_extract(boas_mp_sol)   解答 merge
可選：arfken_mp7 / goldstein_cm3 / sedra_microe / thomas_calculus 可 translate
```

**自主原則**（遵 CLAUDE.md：能定就定，不要一直問）：
- 只有**單一**非可選待辦 → 直接提議「我來做 X」並開始（用戶喊停才停）。
- 多個待辦 → 按 pipeline 上游優先排序，建議最該先做的，列出其餘讓用戶挑。
- `translate(可選)` 不主動做，除非用戶明說。

### 3. 分派執行

依待辦動詞讀對應 reference、照它執行：

| 待辦動詞 | 讀 | 備註 |
|---|---|---|
| `ingest` | `references/ingest.md` | 本機一條龍。多本依 quota 分流 account，順序跑（不並行）。中斷重跑同指令冪等續完 |
| `audit` | `references/audit-book.md` | 派 general-purpose sub-agent，主對話跑 §5 validate |
| `parse` | — | 直接 `uv run --with pyyaml python -m book_pipeline.parser <slug>`（yaml 已在，只是沒跑過） |
| `sol_extract(<sol>)` | `references/audit-sol.md` | 派 sub-agent，含語義抽樣驗證 |
| `translate(可選)` | 獨立 `/translate-book` | 不在本 skill 範圍 |

## 自動化迴圈（unattended daemon，crawl → deploy 全自動）

互動式 `/book-pipeline` 之外，有一套 launchd 驅動的無人值守迴圈，把整條 pipeline 串成自動化。元件：

| 元件 | 角色 |
|---|---|
| `crawl_zlib.py` | z-library eapi client（確定性）：`limits`/`search`/`fetch`/`inventory`。憑證 `~/.secrets/zlib.env`。下到 `raw_pdfs/`，登錄 `crawl_manifest.json`。 |
| `pdf_triage.py` | PDF type 分類（確定性 pymupdf，零 token）：born_digital/scanned/ocr_sandwich + 品質 + verdict（proceed/review）。`--all` 體檢全 raw_pdfs。 |
| `pdf_contactsheet.py` | 抽樣頁拼單張 PNG，供 qc 一次 vision 驗證。 |
| `pipeline_queue.py` | **跨書全 stage 單一真相**（status.py 超集）：crawl→triage→qc→ingest→audit→parse→sol→deploy。`--next` 給下一可動項，每項標 `[LLM]`/`[det]`。 |
| `mineru_budget.py` | MinerU 每日頁數預算輕量排程（per-account，UTC 重置）。超 quota 不硬拒、靠引擎 resubmit 自癒，故只決定「今天開不開新書」。 |
| `pipeline_tick.py` | 單次 tick：resume in-flight → 自書單 SoT 確定性補爬 → 走 actionable。det 直跑，LLM（**crawl 解析**/qc/audit/sol）派 headless `claude -p`（每 tick `--max-llm` 上限）。crawl 選書/買書員確定性，但**解析（書名→id/hash）是 LLM agent**。**`--dry-run` 印計劃**。 |
| `daemon_run.sh` + `com.textbookreader.bookpipeline.plist` | launchd 每 45 min（`StartInterval` 2700s）觸發 wrapper，standby 24hr 常駐。安裝需使用者授權（建立持久背景排程）。MinerU token 由 wrapper `source ~/.secrets/mineru.env` 注入，不入 plist/git。 |
| `booklists/*.json` | **書單 SoT**（整個 project 唯一真相）：領域檔內含具名子單 → 主書（書名+作者）。refill 自此確定性選書，零 LLM。題本不手列（主書 `solution!=false` → 系統自衍生 `<slug>_sol` target）。 |
| `resolve.py` + `crawl_resolution.json` | **crawl agent 的 harness**：queue/target/search（候選+advisory 信心分，只 search）/inspect/commit（resolved\|absent\|review）。解析交 LLM agent 判斷（規則會假陽性）；`auto` 只自動採用零歧義 exact 主書。決定寫 sidecar 永久 cache。 |
| `math_validate.py` + `render_check.js` | 數學式 MathJax ground-truth 驗證（移除 noerrors/noundefined 讓壞式現形）。`<slug>`/`--all`/`--aggregate`/`--cluster`（結構骨架＋診斷 token 聚類，sweep 的眼睛）。post-deploy 由 daemon track，殘餘記入 state。 |
| `apply_math_overrides.py` + `math_overrides/` | corpus 數學 sweep 的 reviewable 修復（比照 catalog_overrides）：`fix_eq_tex`/`fix_inline_math`，git 追蹤、可重播。 |
| `references/{crawl,qc,math-sweep}.md` | headless agent 流程指引：**crawl 解析**（書名→z-lib id/hash 判斷）/ 視覺 QC / 跨書數學 sweep。 |

**新 stage**（pipeline_queue 在 status 前後補的）：`0.2 待qc`（triage 判 needs_llm）、`0.3 待ingest`（triage 過）、`deploy`（parse 完 → 本地 `build.build_all` 烤 data/img，nginx 直讀即時上站，**無 git push**）。`R *拒` = triage/qc 判不可用。

互動排查迴圈時：`pipeline_queue` 看全景、`pipeline_tick --dry-run` 看下個 tick 會做什麼、`tail reports/daemon.log` 看歷史。

## 共用鐵則（跨所有 stage）

- **絕不 echo** `MINERU_API_TOKEN` / `MINERU_API_TOKEN2` 到 log。
- **不要動**：`.env`、`mineru_ingest.py`、`parser.py`、`sol_extract.py`、`slug_map.json` 結構。
- `_pending_batches.json` **只透過 `mineru_ingest.py` 改**，不手編 JSON。
- 遇單本錯誤**不要停**，記訊息、繼續下一本，最後彙整報告。
- 待辦判斷只認**實際資料**（unified/content_list.json、parsed/），不憑檔名臆測。
