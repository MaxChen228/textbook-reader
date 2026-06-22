# Ingest — raw PDF → MinerU 雲端 → unified content_list（本機一條龍）

把 PDF 經 MinerU v4 batch API 轉成 unified `content_list.json`（下游 audit/parse 的輸入）。
**全部在本機完成、單一流程**：一個指令切片 → PUT → poll → 補傳 → assemble 跑到底。無雲端 worker、無 Drive 佇列、無多機接力。

跨日卡 quota 不是分支模式，而是**同一指令重跑**：引擎冪等，已 done 的 chunk 在 `raw/chunk_N/` 自動跳過，只補未完成的。

---

## 共用前提

- **token**：`MINERU_API_TOKEN`（帳號1）+ `MINERU_API_TOKEN2`（帳號2），設於 `.env` 或環境變數。
  - 一條龍模式用 `--account <1|2>` 指定該本走哪個帳號。**絕不 echo token 到 log。**
- **slug 推導**：raw PDF 檔名 → slug 查 `book_pipeline/slug_map.json`（中文/作者/簡寫無法純機械推，顯式記錄）。解答本 slug 以 `_sol` 結尾。新書先在 slug_map 補一行。
- **指令一律 uv**：`uv run python -m book_pipeline.mineru_ingest ...`
- **manifest 只透過 `mineru_ingest.py` 改**（`_pending_batches.json` 是 read-modify-write），**不手編 JSON**。
- **不要動** `mineru_ingest.py`、`parser.py`、`slug_map.json` 結構、`.env`。
- **不 commit** `chunks/`（gitignored，submit 後留本機作審計）。新書完成後若要 commit unified，記得 `git add -f` images。

### MinerU quota（多帳號分流）

- 每帳號 **1000 頁/天**高優先，自然日重置（推測北京 0:00）。**無 quota 查詢 API** → 自己按 PDF 頁數估。
- 超量**降優先（非拒絕）**：poll 等更久，且 VLM 在低優先隊列 `parsing failed, please try again later` 失敗率飆高（伺服器忙，非檔案壞，自動補傳會消化）。
- **PUT 不受 quota 限**（但受對方網域上傳頻寬限速，大檔可能慢），卡的是 poll/解析階段。

**分流策略**：submit 前估頁數，按累計 ≤1000 頁分 account 1、其餘 account 2。超過合計 2000 頁照送（降優先，補傳兜底，跨日重跑續完）。`_sol` 本通常頁少，優先穿插。

```bash
for pdf in raw_pdfs/*.pdf; do
  uv run python -c "import fitz,sys; d=fitz.open(sys.argv[1]); print(f'{d.page_count:5d}  {sys.argv[1]}')" "$pdf"
done | sort -rn
```

---

## 本機一條龍流程

對每本 status 顯示待 `ingest` 的書（`0 待ingest` / `0.5 ingest中斷` / `X 未ingest`），逐本順序跑——**不並行**（manifest 是 read-modify-write，並行會 race）。

1. **列待 ingest + 估頁數分流**：status dashboard 的 `ingest` 待辦 slug，配上頁數（上方指令）按帳號分流。跳過條件：`mineru_data/<slug>/unified/content_list.json` 已存在。
2. **逐本一條龍**：
   ```bash
   uv run python -m book_pipeline.mineru_ingest \
     "raw_pdfs/<file>.pdf" --slug <slug> --account <1|2> \
     --max-wait 2400 --max-retries 4 --resubmit-wait 60
   echo "rc=$?"
   ```
   引擎內部：切片 ≤180 頁（1 頁 overlap）→ PUT 各塊 → 輪詢 poll → VLM 失敗自動補傳 → 下載解壓 `raw/chunk_N/` → 組裝 `unified/{content_list.json, chunks.json, images/, full.md}`。
   退出碼：
   - `0`＝完成（unified/ 已產、manifest 已自動移除）→ 下一本
   - `2`＝poll timeout 仍 pending（罕見）→ 隔次重跑同指令撿
   - `3`＝部分完成（補傳用盡仍缺 chunk，多半當日 quota 卡死）→ **留 manifest**，記缺哪些 chunk，續下一本；隔日 quota 重置**重跑同指令**會自動續（done chunk 在 `raw/` 冪等跳過）
   - 其他＝失敗 → 記訊息、**續下一本，不要停**
3. **跨日續跑**：當日 quota 卡（rc=3 或 rc=2）的書留著，隔自然日（quota 重置後）重開 `/book-pipeline`，dashboard 會把它標 `0.5 ingest中斷`，待辦仍是 `ingest`，重跑同指令補完。
4. **新書 commit（可選）**：unified 產出後若要入 git，
   ```bash
   git add book_pipeline/mineru_data/<slug>/unified/content_list.json \
           book_pipeline/mineru_data/<slug>/unified/chunks.json \
           book_pipeline/mineru_data/<slug>/_run.json
   git add -f book_pipeline/mineru_data/<slug>/unified/images/
   ```

報告表（slug / 頁 / account / rc / unified blocks / 動作）+ 剩餘待續（含 rc=3 跨日）+ 是否需隔日再跑。

---

## 鐵則

- 順序跑、**不並行**（manifest race）。
- 待處理只認 repo `unified/content_list.json`，不憑檔名臆測。
- 遇單本錯誤**不停**，記訊息續下一本，最後彙整。
- **絕不 echo** token。
