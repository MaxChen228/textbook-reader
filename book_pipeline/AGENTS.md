# book_pipeline — AGENTS.md

教科書 ingest 與下游解析的單一權威指引。給 Claude Code agent 讀。

## 1. 系統定位

raw PDF → MinerU 雲端 OCR → unified content_list（扁平 block list，global page_idx）→ per-book `extract_rules.yaml` → `parsed/chNN.json`。
**這個目錄負責「PDF → 結構化 blocks → 章節 / 題目 / 附錄 JSON」**；詳解配對由 `sol_extract.py` 補上、翻譯走 `translate.py` overlay。

舊「人工抄寫 sec_*.json + book.yaml + prepare/extract_figures/extract_solutions/assemble」pipeline 已**完全廢棄並刪除**。看到任何文件提到那些檔名，就是過時資訊。

## 2. 入口指令

```
# 1. ingest（PDF → unified content_list）
uv run --with requests --with pymupdf python -m book_pipeline.mineru_ingest \
    raw_pdfs/<slug>.pdf [--language en|ch] [--chunk-size 180] [--overlap 1] [--skip-upload]

# 2. parse（unified + extract_rules.yaml → parsed/chNN.json）
uv run --with pyyaml python -m book_pipeline.parser <slug>

# 3. smoke（parser 結果語義檢測；audit-book skill 內部呼叫）
uv run --with pyyaml python -m book_pipeline.smoke <slug>

# 4. sol_extract（解答書 merge 到主書 problem.solution；可選）
#    新書建議走 /book-pipeline（audit-sol 流程，references/audit-sol.md）：派 agent 自動產 sol_rules.yaml + dry-run 校準 + 語義驗證
uv run --with pyyaml python -m book_pipeline.sol_extract <main_slug> <sol_slug> [--dry-run]
```

- 解答書結構因書而異，per-sol 配置在 `mineru_data/<sol_slug>/sol_rules.yaml`（入 git；無檔則 Griffiths 預設）。
  schema：`chapter_re`(1 group=章號) / `problem_re`(group(1)=對主書 num) / `multi_per_block` / `_pending`。
- `--dry-run`：不寫主書，印 per-chapter 配對率，校準用。`_pending: true` → 引擎拒絕 merge（主書品質不足）。

- 教科書英文 → `--language en`；中文用 `ch`（預設 `ch`）。
- `--skip-upload`：已有 `raw/`，只重跑 unified 組裝。
- 需要 `.env` 的 `MINERU_API_TOKEN`。
- **sol_extract 必須在 parser 後跑**：parser 重跑會清掉 problem.solution 欄位。

**chunk 級冪等 + 自動補傳（2026-05-29 重構）**：ingest 的真相來源是 `raw/chunk_i/` 是否含
content_list，非「整本一個 batch」。`--resume` 會 poll 所有 batch、冪等下載 done chunk、對
failed/missing chunk **用本機切片自動補傳成新 batch**，重試到全齊才 assemble。
- `--max-retries N`（預設 2）：failed chunk 補傳輪數（需本機有 `chunks/` 切片）
- `--no-resubmit`：關閉補傳，只撿現有 done chunk，缺的回 rc=3
- `--resubmit-wait S`（預設 60）：補傳後等多久再 poll
- 退出碼：0=完成 / 2=poll timeout 仍 pending / 3=部分完成（缺 chunk，manifest 保留可續）
- manifest entry 新增 `batches: [{batch_id, chunk_idxs}]`（補傳追加），向後相容舊單 `batch_id`。
- MinerU `parsing failed, please try again later` 是**暫時性**失敗（超 quota 降優先 + VLM 負載），
  非檔案壞；auto 補傳就是來消化它的。

## 3. 資料路徑

| 路徑 | 內容 |
|---|---|
| `raw_pdfs/<slug>.pdf` | 輸入 PDF（不入 git） |
| `book_pipeline/mineru_data/<slug>/chunks/` | 切片 PDF（≤180 頁 + 1 頁 overlap） |
| `book_pipeline/mineru_data/<slug>/raw/chunk_<n>/` | MinerU zip 解壓原樣 |
| `book_pipeline/mineru_data/<slug>/unified/content_list.json` | 組裝後扁平 blocks（**下游入口**） |
| `book_pipeline/mineru_data/<slug>/unified/chunks.json` | 切點與每 chunk 元資料 |
| `book_pipeline/mineru_data/<slug>/unified/images/` | 合併後圖檔（hash 命名） |
| `book_pipeline/mineru_data/<slug>/unified/full.md` | 串接 markdown（除錯用） |
| `book_pipeline/mineru_data/<slug>/extract_rules.yaml` | 由 `/book-pipeline`（audit-book 流程）產生的 per-book 規則 |
| `book_pipeline/mineru_data/<slug>/parsed/book.json` | 書本 metadata + 章節 / 附錄索引 |
| `book_pipeline/mineru_data/<slug>/parsed/ch{NN}.json` | 章節 body + problems（含 `problem.solution`，sol_extract 後） |
| `book_pipeline/mineru_data/<slug>/parsed/app{X}.json` | 附錄 body |
| `book_pipeline/mineru_data/<slug>/parsed/_gaps.md` | 缺題報告 |
| `book_pipeline/mineru_data/<slug>/parsed/_smoke.md` | smoke heuristic 結果 |
| `book_pipeline/mineru_data/<slug>/parsed/<stem>.zh.json` | translate skill 的稀疏 overlay（可選） |
| `book_pipeline/mineru_data/<slug>/_run.json` | batch_id、timings |
| `book_pipeline/mineru_data/<slug>/_polls.jsonl` | 輪詢紀錄 |

完整跑通的 reference：`sakurai_mqm3`、`griffiths_ed4`、`griffiths_qm3`、`kittel_thermal`、`cheng_em`、`blundell_thermal`、`alexander_circuits`、`strang_linalg`、`sedra_microe`（共 9 本，皆有 unified/ + extract_rules.yaml + parsed/）。

## 4. MinerU Cloud API 速查

| 項目 | 值 |
|---|---|
| Base | `https://mineru.net/api/v4` |
| Auth | `Authorization: Bearer $MINERU_API_TOKEN` |
| 申請上傳 URL | `POST /file-urls/batch` |
| 上傳檔 | `PUT <url>`（**不要帶 Content-Type**） |
| 輪詢結果 | `GET /extract-results/batch/{batch_id}` |
| 單檔上限 | 200MB / 200 頁 |
| Batch 上限 | 50 檔 |
| 每日配額 | 1000 頁（高優先） |
| model_version | `vlm`（PDF/Doc/PPT/image）、`MinerU-HTML`（HTML） |

回傳 zip 內容：`*_content_list.json`（扁平，最常用）、`*_content_list_v2.json`（巢狀 by page）、`full.md`、`layout.json`（= middle.json）、`*_model.json`、`*_origin.pdf`（標註版）、`images/`。

## 5. Block 格式

每個 block：`{type, text, text_level, bbox, page_idx, img_path?, content?, sub_type?}`。
組裝後額外加：`chunk_idx`（來源 chunk），`page_idx` 已轉 **global 0-based PDF page**。
bbox 為 chunk-local 頁面座標，0–1000 正規化，乘 `page_w/h` 還原 PDF point。

### type 處理規則

| type | 下游處理 |
|---|---|
| `text` | 主要內容；可能含 inline LaTeX、可能黏題號 |
| `equation` | 公式 block；LaTeX 偶有空格錯位需收斂 |
| `image` | 用 `img_path` + 鄰近 `image_caption`；**忽略 `content` 與 `sub_type`**（常為偽 mermaid 垃圾） |
| `chart` | 同 image |
| `table` | 保留 |
| `list` | 保留；內容在 `list_items` 陣列、`text` 常為空，**必須讀 `list_items`** 否則 (a)(b)(c) 子題會漏 |
| `header` | 書眉，**過濾** |
| `page_number` | **過濾** |
| `page_footnote` | **過濾**（或視書另議） |
| `footer` | **過濾** |
| `aside_text` | 旁註，**通常過濾** |
| `code` | 保留 |

過濾雜訊約佔 12% blocks（4 本實測）。

## 6. 已知陷阱

- **`text_level=1` 過於鬆散**：Sakurai 全本 335 個 level-1，含封面 / TOC 條目 / Problems 章標。**不能單靠 text_level 切章節**，要結合 regex + 頁碼。
- **題號黏內文**：`"Problem 1.2Is the cross product..."`、`"1.1 A beam of silver..."`。沒有獨立題號 block，需 regex 切。
- **題號 pattern 因書而異**：Griffiths 多用 `Problem N.M`、Sakurai 多用 `N.M ` 行首、部分書用 `Exercise X` / `Example X.X`。
- **LaTeX 空格錯位**：`'1 0 0 0'` 應為 `'1000'`。需在後處理時做空格收斂。
- **空白頁**：出版社章節分隔頁全空，MinerU 跳過導致 `page_idx` 不連續（**正確行為，不要補**）。
- **接縫**：1 頁 overlap，組裝時丟掉後 chunk 的第一頁 blocks。4 本實測零異常，**不要改動 overlap 邏輯**。
- **偽 mermaid**：`type ∈ {image, chart}` 偶帶 `content` 為空節點 mermaid flowchart、`sub_type=flowchart`。永遠忽略。
- **inline 題目章節**：題目散落 section 中（Griffiths 風格），沒有「章末 Problems 區塊」可錨。yaml 該章 `pbi=null` + top-level `inline_problems=true`，parser 走 `walk_inline_chapter`。
- **per-section 題號重置**：部分書（Strang）每個 section 的 Problem Set 都從 1 重新編號。yaml 加 top-level `problem_num_namespace_by_section=true`（必須與 inline mode 同用），parser 會把 `section_id` 串到 num 前避免撞號。
- **附錄與 Index / Bibliography 區邊界**：appendix 末尾用 `index_start_page` 或 `bibliography_start_page` 切，否則 Index 區會被吞進 `appendix.body`。

## 7. 跨書策略：規則化 per-book

| 面向 | 跨書共通 | per-book 變異 |
|---|---|---|
| 欄位 schema / type enum / bbox 座標系 / 圖片命名 | ✅ | — |
| 題號 regex | — | ✅ |
| 章節 regex | — | ✅ |
| 題目布局（穿插 vs 章末聚集） | — | ✅ |
| 過濾名單（例如 Sakurai 每頁重複 `Problems` header） | — | ✅ |
| 是否信任 `image.content` | 一律不信 | — |

**設計方針**：每本書一份 `extract_rules.yaml`，由 `/book-pipeline`（audit-book 流程）一次性吃 unified content_list 樣本產出草案，smoke feedback loop（max 3 iterate）修正語義錯誤，之後 runtime 純規則跑（零 LLM 成本、可重現）。yaml schema 含 top-level `inline_problems` / `problem_num_namespace_by_section` / `bibliography_start_page` / `index_start_page` 等 flag 控制 parser 分支。

設計哲學：**LLM 只做索引歸位，不產文字；文字逐字取自 OCR，程式端做確定性清洗**（沿用自已廢棄的 sourcebank LLM extract pipeline 留下的經驗）。

## 8. 廢棄清單（看到請忽略，不要嘗試恢復）

| 已刪除 | 原職責（無需理解） |
|---|---|
| `books/<slug>/sec_*.json` | 人工抄寫題目 JSON |
| `books/<slug>/book.yaml` | 書本 metadata |
| `book_pipeline/prepare.py` | 預備 sec_*.json |
| `book_pipeline/extract_figures.py` | 人工抽圖 |
| `book_pipeline/extract_solutions.py` | 人工抽詳解 |
| `book_pipeline/assemble.py` | 組 ch{NN}.json |
| `book_pipeline/config.py` | 舊路徑設定 |
| `book_pipeline/default_*_instructions.md` | 抄寫指引 |
| `sync_books.sh` | 同步舊產物到 static/books/ |
| `.claude/commands/{extract-chapter,extract-solutions,init-book,index-problems,find-problems}.md` | 舊 skills |
| `books/` `static/books/` 目錄內容 | 舊產物 |

## 9. 已實作元件

| 元件 | 角色 |
|---|---|
| `mineru_ingest.py` | PDF 切片 + MinerU upload/poll/assemble unified |
| `parser.py` | unified + yaml → parsed/chNN.json（含 inline 章 walker、namespace_by_section、list_items 讀取、appendix 末端 index/bib 切點） |
| `smoke.py` | parser 結果語義 heuristic 檢測（H1-H5） |
| `sol_extract.py` | 解答書 problems merge 進主書 `problem.solution` |
| `translate.py` | 整本翻譯產 `<stem>.zh.json` overlay |
| `/book-pipeline` skill | **教科書生成單一入口**：掃描 `status` → 分派。子流程在 `.claude/skills/book-pipeline/references/`：`ingest.md`（本機一條龍）、`audit-book.md`（unified 樣本 → `extract_rules.yaml`，含 smoke feedback loop）、`audit-sol.md`（解答 merge）|
| `drive_queue.py` | （可選）unified 災難備份：`backup <slug>`（unified tar → `gdrive:qbank_data_backup/`）、`restore`（拉回 + 自動 parser 重生 parsed/）。ingest 已全本機，不再用 Drive 中轉 PDF |
| `slug_map.json` | raw 檔名 → ingest slug 對照（slug 無法純機械推，顯式記錄；新書補一行） |
| `status.py` | **每本書 pipeline 階段的單一真相**（讀實際資料：unified→audit→parse→sol merge率→zh）。判斷下一步前先 `uv run python -m book_pipeline.status`，勿憑檔名臆測（parsed/ 是 gitignore；problems 是 ch.json 獨立鍵非 body block） |
| `/translate-book` skill | 派 sub-agent 走 translate.py |

Web viewer（`textbooks/corpus.py` + `routes/textbook_pages.py` + `templates/browse_textbook.html`）讀 `parsed/*.json`（parser 產）+ `unified/images/`（圖），template 支援 `<details>` 折疊式 solution 顯示與 `body=[]` 題目的 OCR 漏抓 warning。

## 10. 資料/代碼分流 + 閉環（2026-05-29）

git **只追蹤代碼 + 不可重生的貴重成果**，書籍機器產物全部走 Drive。界線：

| 類別 | 例 | git? | 真相來源 / 還原 |
|---|---|---|---|
| 代碼 / skill | `*.py *.md *.html`、`.claude/` | ✅ git | git |
| 貴重成果 | `extract_rules.yaml`、`parsed/*.zh.json`、`questions.json` | ✅ git | git |
| 機器產物（圖+OCR） | `unified/`（images + content_list + chunks）、`_run.json` | ❌ | Drive `gdrive:qbank_data_backup/<slug>.tar`（per-book） |
| 可重生半成品 | `parsed/*.json`（chNN/book/app） | ❌ | `parser` 從 unified + extract_rules 重生 |
| 中間產物 | `raw/ chunks/ batches/ _polls.jsonl` | ❌ | 不備份（重跑 ingest 再生） |
| 源頭 PDF | `raw_pdfs/` | ❌ | 本機 |

**新書全流程（本機一條龍）**：
1. PDF 放本機 `raw_pdfs/`（新檔名補 `slug_map.json`）
2. `/book-pipeline` → ingest：本機 `mineru_ingest <pdf> --slug <slug> --account <N>`（切片→PUT→poll→assemble 產 unified；跨日卡 quota 重跑同指令冪等補完）
3. `/book-pipeline`（audit-book 流程，產 `extract_rules.yaml` + parser 切 parsed/）→（可選）`/translate-book`（產 `*.zh.json`）→ git commit 這些貴重成果
4. （可選）`drive_queue.py backup <slug>` → unified tar 上 Drive 作災難備份
5. `browse.py` → viewer 顯示

**換機 / 災難還原**：`git clone` → 每本書 unified 二選一還原：`drive_queue.py restore <slug>`（若有 Drive 備份，拉 tar + 自動 parser 重生 parsed）或本機重跑 ingest 從 PDF 重生（耗 MinerU quota）→ `browse.py`。git（代碼+規則+翻譯）為唯一真相，unified 機器產物可隨時重生，無單點遺失。
