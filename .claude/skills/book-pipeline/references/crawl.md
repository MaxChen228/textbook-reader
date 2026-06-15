# crawl — z-library 自動爬書（pipeline 最上游）

被 `pipeline_tick` 的 wishlist 階段或使用者直接派來。職責：依主題挑一本最該補的書、選對版次、下載成 `raw_pdfs/<slug>.pdf`。**選書判斷是你的工作；下載/登錄由 `crawl_zlib.py` 確定性處理。**

## 工具

```bash
uv run --with requests python -m book_pipeline.crawl_zlib limits        # 今日下載額度（免費 10/日）
uv run --with requests python -m book_pipeline.crawl_zlib inventory     # 現有全部書 slug + 已爬清單（避免重複）
uv run --with requests python -m book_pipeline.crawl_zlib search "<q>" --lang english [--json]
uv run --with requests python -m book_pipeline.crawl_zlib fetch <id> <hash> --slug <slug>
```

## 流程

1. **查額度**：`limits`。`remaining<=0` → 立即停（今日爬滿，明日再來）。
2. **讀意圖**：`book_pipeline/crawl_wishlist.json` 的 `topics`（使用者想補的主題/書名/科目）。
3. **查現況**：`inventory`。已在 `known_slugs` 的書**不重複爬**；對照 topics 找真正的缺口。
4. **搜尋選版**：對目標 `search`。輸出已依堪用度排序，欄位判讀：
   - `kind=SOL` = 解答本（只在使用者要解答時抓，slug 用 `<main>_sol`）。
   - 偏好：版次新、`mb` 落在 3–80（過小常殘缺、過大常高解析掃描）、`pages` 合理、有 `publisher`、`have=✓` 代表已爬過。
   - **同書多版時挑一本**最堪用的；拿不準寧可選正式出版年份明確者。
5. **下載**：`fetch <id> <hash> --slug <slug>`。slug 命名沿用既有慣例（作者姓_主題，kebab/底線小寫，如 `axler_linalg`、`griffiths_ed4`）；查 `slug_map.json` 風格保持一致。冪等：slug 已存在會自動跳過。
6. **每 tick 一本為度**：受 10/日額度限，挑當下最該補的一本下載即可，其餘留待後續 tick。

## 鐵則

- **絕不**把帳密/userkey echo 出來（憑證在 `~/.secrets/`，工具自理）。
- 下載後**不要**自己跑 ingest——daemon 下個 tick 會依 `pdf_triage` 判定自動接手（born_digital/good 直接 ingest；可疑先 qc）。
- 下載回 HTML（額度耗盡/需驗證）會報錯中止，不要硬試。
- 拿不準某主題該抓哪本、或 topics 模糊 → 在回覆說明候選與理由，**寧缺勿濫**抓錯書。
