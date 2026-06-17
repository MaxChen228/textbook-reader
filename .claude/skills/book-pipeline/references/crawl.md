# crawl — z-library 自動爬書**選書**（pipeline 最上游）

被 `pipeline_tick` 的 crawl 階段或使用者直接派來。職責**兩條**：(A) 依 wishlist 主題挑出最該補的經典缺口主書；(B) **為 inventory 既有主書補對應的解答本（solution manual）**——只要該主書還沒有 `<slug>_sol` 同伴，就盡量找到官方解答/instructor solutions 一併排進計畫。兩類缺口合計 **N 本互異**（N＝daemon 寫進你派工 prompt 的目標數量，見下），逐本選對版次、取得 id/hash，把計畫寫成 `crawl_plan.json`。**你只選書、不下載**——下載由 daemon 讀計畫後並行確定性執行（`crawl_zlib fetch`，帳號由 daemon 依各帳號餘額預先指派）。

> **選書與額度徹底解耦（核心原則，務必內化）**：你的任務只是「書單缺什麼、補哪 N 本」——純粹的書單問題，**與下載額度完全無關**。今日 z-library 還能下載幾本是 **daemon 與下載步驟**的事，**不是你的事**。所以：**絕不跑 `crawl_zlib limits`、絕不查詢或推算額度、絕不自己決定要找幾本**。N 由 daemon 給你，你找滿 N 即可。額度不足時 daemon 的下載步驟會自動只下載得了的本數、其餘留待後續 cycle——完全不需你介入。

> 解答本是常駐 duty（非「只在使用者要時才抓」）：解答本經 `sol_extract` merge 進主書，直接讓既有書更完整、CP 值極高。故每輪都該掃 inventory 找缺解答的主書補上。

> 為何拆「選書／下載」：只有「選哪本」需判斷（讀共享 inventory → 須單一決策避免多 agent 撞同一本）；「下載」是確定性的、可並行。故一隻 planner 選 N 本 → daemon 並行下載，比 N 隻序列 agent 快數倍、省 LLM、零選書碰撞。

## 工具

```bash
uv run --with requests python -m book_pipeline.crawl_zlib inventory     # 現有全部書 slug + 已爬清單（避免重複）
uv run --with requests python -m book_pipeline.crawl_zlib search "<q>" --lang english [--json]
# 注意：你**不要**跑 fetch（下載是 daemon 的事），也**不要**跑 limits（額度與你的選書無關）。
```

## 流程

1. **收到本批目標 N**：daemon 已把「本批要找幾本」寫進你的派工 prompt（純書單水位缺口，與額度無關）。你的目標就是找滿 N 本互異缺口。
2. **讀意圖**：`book_pipeline/crawl_wishlist.json` 的 `topics`（使用者想補的主題/書名/科目）+ `_rules` 選書鐵則。
3. **查現況**：`inventory`。已在 `known_slugs` 的書**不重複**；對照 topics 找主書缺口。**同時**掃一遍 inventory 的主書（非 `_sol`、非 `is_solution`），凡 `<slug>_sol` 不在 `known_slugs` 者 = 缺解答本的候選。
4. **挑 N 本互異**：兩類缺口合計 N 本。**解答本優先**（補既有書 CP 值最高）：先為缺解答的高價值主書各跑 `search "<書名> solutions manual" --lang english`，找到 `kind=SOL` 且確屬該書的官方解答 → 列為 `{"slug":"<main>_sol", ...}`；N 的其餘部分再填 wishlist 主書缺口。逐本 `search` 選版次，欄位判讀：
   - `kind=SOL` = 解答本（slug 必須是對應主書的 `<main>_sol`，main 須是 inventory 既有 slug）。**只收確屬該主書的官方/instructor 解答**，版次盡量對齊主書；查無正牌解答就略過該本。
   - 偏好：版次新、`mb` 落在 3–80（過小常殘缺、過大常高解析掃描）、`pages` 合理、有 `publisher`、`have=✓` 代表已爬過（跳過）。
   - **同書多版挑一本**最堪用的；拿不準寧選正式出版年份明確者。
   - 取得每本的 `id` 與 `hash`（`--json` 輸出含 hash）。
5. **slug 命名**：沿用既有慣例（作者姓_主題，kebab/底線小寫，如 `axler_linalg`、`griffiths_ed4`）；查 `slug_map.json` 風格保持一致。**計畫內 slug 不得重複、不得與 inventory 既有者重複。**

## 鐵則

- **絕不跑 `crawl_zlib limits`、絕不查詢或推算下載額度、絕不自己決定要找幾本**——N 由 daemon 給定，額度是 daemon 與下載步驟的事，與你選書無關。
- **絕不執行 `fetch`**——你只產計畫，下載是 daemon 的事。
- **絕不**把帳密/userkey echo 出來（憑證在 `~/.secrets/`，工具自理）。
- 寧缺勿濫：拿不準某主題該抓哪本、或 topics 模糊 → 該本就不列入計畫，在 reason 說明，**不要**抓錯書。
- N 是 daemon 給定的目標數量；唯有合格缺口湊不滿 N 本才可列實際本數（少於 N）並在 reason 說明；全無缺口就 `books:[]`。

## 收尾產出（強制，daemon 靠它並行下載）

結束前**務必**寫 `book_pipeline/reports/crawl_plan.json`（單一 JSON object）。daemon 讀它去重、依各帳號餘額指派 account、並行下載（實際下載得了幾本由額度決定，與你無關）：

```jsonc
{
  "books": [
    {"slug": "ashcroft_ssp",   "id": "1234567", "hash": "ab12cd", "title": "Solid State Physics (Ashcroft & Mermin)"},
    {"slug": "axler_linalg_sol", "id": "5551234", "hash": "cd56ef", "title": "Linear Algebra Done Right — Solutions (Axler)"}
  ],
  "reason": "補主書缺口（固態物理）+ 為既有 axler_linalg 補官方解答本"
}
```

無合格缺口時：

```jsonc
{"books": [], "reason": "wishlist 經典缺口都已有，今日無合格新書"}
```

鐵則：
- **計畫內每本必須 `slug`/`id`/`hash` 三欄齊全**，缺欄該本作廢（daemon 會跳過並 surface ❌）。
- slug 已存在（inventory 既有）的書不要列——daemon 的 fetch 會冪等跳過（列了是浪費）。
- 主動判斷「沒合格書可補」→ `books:[]` + reason 說明（正常、非錯誤）。
