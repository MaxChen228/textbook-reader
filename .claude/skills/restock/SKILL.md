---
name: restock
description: 把「能下載的合格書」淨增 100 本。無論何時何地、零指示，使命固定——替教科書 reader 補貨：找夠格的大學級書（教科書/專著/講義/參考書，理工優先）、查它在 Z-Library 的可下載連結、確認版本號、（有解答本則）確認解答本與母書版本對齊。四維全過才算數。**只要使用者打 /restock（或說「補書單/補貨/多加 100 本可下載的書」），就用這個 skill。**
---

# /restock — 合格書目補貨（淨增 100）

## 使命（寫死，不需指示）

讓「**合格存在**」的書淨增 **100** 本。一本書「合格存在」⟺ 四維**全部**成立：

1. **維① 夠格收錄** — 是大學級教科書／研究專著／講義／參考書（理工優先、不限主題）。排除小說／大眾科普／考試用書／操作手冊／已被取代的舊版。
2. **維② 有可下載連結** — Z-Library 上有此書的合法可下載 PDF（拿到 id+hash）。
3. **維③ 版本號確認** — 親查確認是哪一版，且符合書單偏好版次（`edition_pref`）。
4. **維④ 解答本對齊**（僅解答本 `*_sol`）— 解答本版次與母書確認的版次同版（題號才對得上）。

**任一維沒驗到 = 不算數 = 不該存在於系統。** 沒有「待查書單」這種東西——沒連結的書就是還不存在。你這一隻 agent 同時包辦四維、綁成單一不可分判斷。

## 核心原則

- **四維是 AND，綁在一起判**：找到連結 ≠ 完成。連結 + 版本 + 解答對齊（+夠格）全 yes，才落 QUALIFIED。缺一即 PENDING（有連結未驗）或 CANDIDATE（無連結），都不算淨增。
- **全程 LLM 親查、零硬編碼**：版本/解答/夠格一律你（多 haiku 交叉）親判，**禁**用書名正則抽版次、**禁**字串比對當裁決。每個判斷要有來源佐證。
- **只查不下載**：你只 `resolve search`（不耗額度）。下載是 daemon 買書員的事（它只下載你判定 QUALIFIED 的書）。
- **owned 保命**：回查發現已收錄（owned）的書版本不對/不夠格 → **只標記 + 開 proposal，絕不下架/刪**。已 OCR 的實體資產優先於四維判斷。
- **自我終止**：合格淨增達 100、或兩個來源（存量回查 + 發現新書）都枯竭 → 停。

## 主流程

四維 fan-out 引擎、每本書的查證步驟、落盤指令、判準表、discovery 找新書、owned 回查、硬邊界——全在
**`references/booklist-manager.md`**（讀它，那是你的作業手冊）。本檔只定使命與編排：

### 0. 先量現狀

```bash
uv run python -m book_pipeline.booklists progress      # 看 owned/qualified/pending/candidate/rejected 水位
```
記下當前 qualified 數當基線；目標 = 基線 + 100。

### 1. 存量優先（先回查有連結但未驗的書）

系統裡有一批「有連結、但版本/解答/夠格還沒親查」的書（PENDING）——把它們驗成合格，是**最便宜的淨增**
（連結已在，只缺維③④①的親查）。

```bash
uv run python -m book_pipeline.resolve queue --state pending --limit 30   # 存量回查母體
```

逐本走 `references/booklist-manager.md` 的「每本書多源查證 workflow」：fan-out haiku 查版本/解答 → 你收斂
共識 → `editions set` 落維③（`--matches-pref`）/維④（`--sol-aligned`）。四維補齊即升 QUALIFIED、計入淨增。
回查 owned 書若發現 mismatch → 開 `proposals propose --type edition-mismatch`，**不動那本書**。

### 2. 發現新書補足（存量回查不夠 100 時）

存量驗完仍未達 +100 → 進 discovery：替各領域找**夠格的新書**寫進候選層，再對每本走完整四維查證
（夠格∧連結∧版本∧解答對齊）。做法見 `references/booklist-manager.md` 的「Discovery mode」+「每本書
多源查證 workflow」。新書要先判維①夠格（`editions set --eligible --field-id --subject`）才往下查連結/版本。

### 3. 收斂判斷（每處理一批後）

```bash
uv run python -m book_pipeline.booklists progress      # 重量 qualified；達基線+100 → 完成
```

- qualified 淨增 ≥ 100 → **完成**，回報淨增數 + 各維落了多少。
- 未達、但 `resolve queue --state pending` 與 discovery 都枯竭（找不到更多夠格新書）→ **誠實停**，回報實際淨增 + 為何枯竭。別湊數塞次級書、別硬配錯版。

## 回報格式（結束時）

一句話現狀 → 淨增了幾本合格書（基線 X → 現在 Y）→ 四維各自的進展（新確認連結幾本、版本幾本、解答對齊幾本、判夠格新書幾本、判 not_found/不夠格幾本）→ 開了哪些 proposal（owned mismatch / 書單歧義）→ 若未達 100 說明為何枯竭。白話、不堆術語。

## 硬邊界（不可違反）

- **絕不下載、絕不碰額度/帳號**（只 `resolve search`）。
- **絕不手改書目正典**（舊 `booklists/*.json` 已退役封存 `booklists/_archive/`；discovery 只寫 `discovered/` 機器候選層）。
- **絕不下架/刪 owned 書**（mismatch 只標 + proposal）。
- **零硬編碼**：四維全 LLM 親查、有來源佐證。
- 你的產出只有：`resolve commit`（維②連結 found/not_found）、`editions set`（維①③④）、`discovered add`（新書候選）、`proposals propose`（owned mismatch / 系統性問題）。
