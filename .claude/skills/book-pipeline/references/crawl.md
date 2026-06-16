# crawl — z-library 自動爬書**規劃**（pipeline 最上游）

被 `pipeline_tick` 的 crawl 階段或使用者直接派來。職責：依 wishlist 主題挑出**最多 K 本互異**、最該補的經典缺口，逐本選對版次、取得 id/hash，把計畫寫成 `crawl_plan.json`。**你只選書、不下載**——下載由 daemon 讀計畫後並行確定性執行（`crawl_zlib fetch`，帳號由 daemon 依各帳號餘額預先指派）。

> 為何拆「規劃／下載」：只有「選哪本」需判斷（讀共享 inventory → 須單一決策避免多 agent 撞同一本）；「下載」是確定性的、可並行。故一隻 planner 選 K 本 → daemon 並行下載 K 本，比 K 隻序列 agent 快數倍、省 LLM、零選書碰撞。

## 工具

```bash
uv run --with requests python -m book_pipeline.crawl_zlib limits        # 今日各帳號 + 總剩餘額度（多帳號輪換，3 帳號=30/日）
uv run --with requests python -m book_pipeline.crawl_zlib inventory     # 現有全部書 slug + 已爬清單（避免重複）
uv run --with requests python -m book_pipeline.crawl_zlib search "<q>" --lang english [--json]
# 注意：你**不要**跑 fetch。下載是 daemon 的事。
```

## 流程

1. **查額度**：`limits` 取 `total_remaining` = R。`R<=0` → 寫空計畫（`books:[]`, reason 說明今日爬滿）即收工。
2. **讀意圖**：`book_pipeline/crawl_wishlist.json` 的 `topics`（使用者想補的主題/書名/科目）。
3. **查現況**：`inventory`。已在 `known_slugs` 的書**不重複**；對照 topics 找真正的缺口。
4. **挑 K 本互異**：K = min(R, 12)。挑當下最該補的 K 本**不同**經典（同一本含不同版次只算一本）。逐本 `search` 選版次，欄位判讀：
   - `kind=SOL` = 解答本（只在使用者要解答時抓，slug 用 `<main>_sol`）。
   - 偏好：版次新、`mb` 落在 3–80（過小常殘缺、過大常高解析掃描）、`pages` 合理、有 `publisher`、`have=✓` 代表已爬過（跳過）。
   - **同書多版挑一本**最堪用的；拿不準寧選正式出版年份明確者。
   - 取得每本的 `id` 與 `hash`（`--json` 輸出含 hash）。
5. **slug 命名**：沿用既有慣例（作者姓_主題，kebab/底線小寫，如 `axler_linalg`、`griffiths_ed4`）；查 `slug_map.json` 風格保持一致。**計畫內 slug 不得重複、不得與 inventory 既有者重複。**

## 鐵則

- **絕不執行 `fetch`**——你只產計畫，下載是 daemon 的事。
- **絕不**把帳密/userkey echo 出來（憑證在 `~/.secrets/`，工具自理）。
- 寧缺勿濫：拿不準某主題該抓哪本、或 topics 模糊 → 該本就不列入計畫，在 reason 說明，**不要**抓錯書。
- K 是上限不是配額：合格缺口不足 K 本就列實際本數；全無缺口就 `books:[]`。

## 收尾產出（強制，daemon 靠它並行下載）

結束前**務必**寫 `book_pipeline/reports/crawl_plan.json`（單一 JSON object）。daemon 讀它去重、依各帳號餘額指派 account、並行下載：

```jsonc
{
  "books": [
    {"slug": "ashcroft_ssp",   "id": "1234567", "hash": "ab12cd", "title": "Solid State Physics (Ashcroft & Mermin)"},
    {"slug": "peskin_qft",      "id": "7654321", "hash": "ef34gh", "title": "An Introduction to QFT (Peskin & Schroeder)"}
  ],
  "reason": "填 wishlist 物理缺口：固態物理 + 量子場論"
}
```

無合格缺口時：

```jsonc
{"books": [], "reason": "wishlist 經典缺口都已有，今日無合格新書"}
```

鐵則：
- **計畫內每本必須 `slug`/`id`/`hash` 三欄齊全**，缺欄該本作廢（daemon 會跳過並 surface ❌）。
- slug 已存在（inventory 既有）的書不要列——daemon 的 fetch 會冪等跳過，但列了浪費計畫名額。
- 主動判斷「沒合格書可補」→ `books:[]` + reason 說明（正常、非錯誤）。
