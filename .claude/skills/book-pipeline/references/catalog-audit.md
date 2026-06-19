# Catalog Audit — 修一本書的 catalog 殘留

吃 `book_pipeline/catalog_audit.audit_catalog(slug)` 的 critical findings，產出 reviewable、可重播的 `book_pipeline/catalog_overrides/<slug>.json`，把殘留壓到 0（或記成 accepted 收工）。

**核心契約**：你是 deterministic catalog repair 的執行者。catalog 是 parser 從 `parsed/*.json` 機器產的（`build_catalogs`），**機器產物全 ignore、不入 git**；你**唯一**的可追蹤產物是 `catalog_overrides/<slug>.json`（疊加 spec）。每筆 override 在 parser 重建後可 replay → 絕不手改 `parsed/*.json`，一切走 override。

**範式定位**：book-pipeline「audit → 找殘留 → LLM/agent 產配置 → 確定性引擎執行 → 重 audit 驗證 → iterate」範式的 **catalog 修復版**。對象=已 parse+audit 完的主書，引擎=`apply_catalog_overrides.py`，metric=`audit_catalog(slug)['critical']`。前置版見 `audit-book.md`（產 extract_rules.yaml）。

## 目錄
- §1 環境前提與工作流程
- §2 critical 類別總表（C1–C7）
- §3 override spec 格式（action / selector / 白名單）
- §4 逐類別修復策略
- §5 pdf_crop_insert clip 座標決定法
- §6 終局原則：accepted 殘留
- §7 硬規則
- §8 派發流程

## 1. 環境前提與工作流程

前提：`book_pipeline/mineru_data/<slug>/parsed/` 已有 `book.json` + `catalogs.json` + `chNN.json`（audit-book 已跑完、smoke 綠）。本機才有 `raw_pdfs/<檔>.pdf`（pdf_crop / pdf_contactsheet 必需）。

**工作流程（一律 `uv run`，pyyaml/pymupdf 等已在 pyproject 依賴，勿加 `--with`：那會每次冷解析 ephemeral env、拖慢）**：

```bash
SLUG=<slug>
# (1) 看殘留
uv run python -c "from book_pipeline.catalog_audit import audit_catalog as a; r=a('$SLUG',write_report=False); print({k:v for k,v in r.items() if k not in('findings','classified_refs')}); [print(f.code,f.message,'||',(f.context or {}).get('snippet') or (f.context or {}).get('where')) for f in r['findings']]"
# (2) 逐 finding 決策（§4），把 override 寫進 catalog_overrides/$SLUG.json（§3）
# (3) apply：覆寫 parsed/*.json（先自動備份到 parsed/_override_backups/<ts>/）+ rebuild catalogs
uv run python -m book_pipeline.apply_catalog_overrides $SLUG
# (4) 重 audit 驗證 critical 是否下降；非 0 回 (2) iterate
uv run python -m book_pipeline.catalog_audit $SLUG
```

`audit_catalog` 同時把人讀報告寫到 `parsed/_catalog_audit.md`（gitignore）。`apply_overrides` 對每個被改的 chunk **先 `_backup_once` 到 `parsed/_override_backups/<timestamp>/`** 再寫，replay 安全。

**收斂目標**：`critical == 0`。每輪 apply 後重跑 (1)/(4) 比對 critical 數，**只准下降**；若某筆 override 沒讓對應 finding消失，是你 selector/欄位錯了，回去修，別疊新 override 蓋。至多 3 輪實質 iterate；剩餘真源頭缺失者走 §6 accepted。

## 2. critical 類別總表（C1–C7）

`audit_catalog` 的 finding 帶 `code`。對照 `summary` 計數欄與修法：

| code | summary 欄 | 觸發條件 | 本質 | 主修法 |
|---|---|---|---|---|
| **C1** | `fallback_ids` | id 命中 `^(fig\|tbl)-(ch\d{2}\|app…)`（純位置 fallback、無語意編號） | parser 沒從 caption 解出 N.M 編號 | `set_fields` 給真 `id`+`caption`，或 `catalog_exclude_reason` |
| **C2** | `empty_captions` | entry `caption` 空白 **且** 無 `catalog_exclude_reason` | 圖/表無說明文字 | `replace_text`/`set_fields` 補 caption，或 set `catalog_exclude_reason` |
| **C3** | `missing_images` | `type==figure`、`kind!='text'`、`src` 指的檔不存在（四處 candidate 都找不到） | 圖檔遺失 | 修 `src`、或 `kind:'text'`、或 `pdf_crop_insert` 重裁 |
| **C4** | `missing_figure_refs` | 正文出現 `Fig(ure) X.Y` 但 catalog 無 `fig-X.Y`（且 parent `X.Y`→`X` 也不覆蓋） | 正文 ref 找不到對應 figure | **見 §4.C4 決策樹**：別名 / pdf_crop / replace_text / ref_classification |
| **C5** | `missing_table_refs` | 同 C4 但 `Table X.Y` / `tbl-X.Y` | 正文 ref 找不到對應 table | 同 C4 套到 table |
| **C6** | `broken_anchors` | 可見 catalog entry 缺 anchor metadata，或 anchor id 不在 reader chunk 的 block id 集合 | catalog 指向的 block 已不存在/換 id | `set_fields` 對齊 anchor 對應 block 的真實 `id` |
| **C7** | `unresolved_visuals` | entry 有 caption 但 `id` 空 **且** 無 `catalog_exclude_reason` | 圖有說明卻無語意 id | `set_fields` 給 `id`，或 set `catalog_exclude_reason` |

> C4/C5 是 ref ↔ catalog **交叉檢查**（正文 demand vs catalog supply），跟其他類別（檢查 catalog entry 自身完整性）正交。`_canonical_num` 把 `2-11-2`/`2–11–2` 一律正規成 `2.11.2`，`X.Yz`（小寫尾字母）會 fallback 到 parent `X.Y`。所以 ref `9.16.1` 需要 catalog 端存在語意 id `fig-9.16.1`（或別名）才算覆蓋。

## 3. override spec 格式

`catalog_overrides/<slug>.json` 頂層兩個 key：

```json
{
  "ref_classifications": [ ... ],   // C4/C5 專用：把 ref 歸類為非內部/來源不符 → 從 critical 移除（不改資料）
  "overrides": [ ... ]              // set_fields / replace_text / pdf_crop_insert / copy_solution_images
}
```

### 3.1 selector 格式（精確）

`_select_list_and_index` 只認兩種：

- `body[N]` — chunk 頂層 `data['body']` 的第 N 個 block（0-based）。
- `problem:NUM:field[N]` — `data['problems']` 中 `num==NUM`（字串比對）的題，其 `field` list（`body` 或 `solution`）第 N 個 block。例：`problem:14.2.2:body[22]`、`problem:2:solution[2]`。

selector index **取自 `audit_catalog` finding 的 `context.path`**（`_block_path` 產生，格式完全一致）。**不要自己數** block index — apply 時 `expect`（§3.2）會擋住對錯 block。

### 3.2 action: `set_fields`

改 catalog entry 對應 block 的後設欄位。**白名單只有 8 個**（其他 key 直接 raise）：
`id`、`caption`、`src`、`kind`、`aspect`、`catalog_exclude_reason`、`catalog_repair_source`、`catalog_aliases`。

```json
{
  "id": "<override 唯一識別字串，純標籤>",
  "action": "set_fields",
  "chunk": "ch09",
  "selector": "body[88]",
  "expect": { "id": "fig-9.16" },              // 可選但強烈建議：apply 前驗現值，不符即 raise（防 selector 飄移）
  "set": {
    "id": "fig-9.16.1",
    "catalog_aliases": ["9.16.1"],
    "catalog_repair_source": "agent-id-fix"
  },
  "unset": ["catalog_exclude_reason"],          // 可選：移除欄位（等同 set 該欄為 null）
  "reason": "<人讀理由，不影響執行>"
}
```

- `set` 內某 key 值為 `null` → 等同從 block 刪該 key。
- 冪等：apply 前若 block 現值已滿足 `set`+`unset`，直接 skip（不重寫、不再驗 expect）。
- `expect` 是防呆：parser 重建後 block index 可能位移，`expect` 對不上會 raise → 你即時發現 selector 過期，而非默默改錯 block。

### 3.3 action: `replace_text`

對 block 的單一文字欄位做**第一次出現**的字串替換。`field` ∈ `{md, caption, tex}`（default `md`）。

```json
{
  "id": "fix-misocr-ref",
  "action": "replace_text",
  "chunk": "ch08",
  "selector": "body[12]",
  "field": "md",
  "old": "Fig. 8.6.2 by",
  "new": "Fig. 8.6.1 by",
  "reason": "正文 misOCR：原文是 8.6.1，OCR 成 8.6.2"
}
```

- `old` 找不到但 `new` 已在 → skip（冪等）；都沒有 → raise（你 old 抄錯）。
- 只替換**一次**（`replace(old,new,1)`），所以 `old` 要夠長到唯一定位。

### 3.4 action: `pdf_crop_insert`

從 raw PDF 指定頁裁一塊圖、插入（或更新同 id 的）block。MinerU 真的漏抓圖時用。詳見 §5。

```json
{
  "id": "<slug>-fig-14.15-pdf-crop",
  "action": "pdf_crop_insert",
  "chunk": "ch14",
  "selector": "problem:14.2.2:body[22]",
  "position": "before",                 // before | after：相對 selector 插入點（default before）
  "page": 820,                          // 1-based PDF 實體頁（注意非 page_idx）
  "clip": [0.035, 0.405, 0.315, 0.94],  // §5：全為 0–1 → normalized；否則絕對 points
  "zoom": 2.5,                          // 解析度倍率，default 2.5
  "image_id": "<可選，覆寫輸出檔名 stem；default 取 block.id>",
  "src": "<可選，明指輸出檔名；default manual_<id>.png>",
  "block": {
    "t": "fig",
    "id": "fig-14.15",
    "kind": "line",
    "caption": "Figure 14.15: ..."
  },
  "reason": "MinerU 漏抓此圖；raw PDF 該頁左欄確有"
}
```

- 裁出的 PNG 寫到 `mineru_data/<slug>/unified/images/<src>`（build 階段 cwebp 轉 webp）。`aspect` 由 clip 比例自動算並覆寫進 block。
- 若 `block.id` 已存在於該 list → **更新**那個既有 block（換 src/aspect、補 `catalog_repair_source='agent-pdf-crop'`），不重複插入。否則依 `position` 插新 block。

### 3.5 action: `copy_solution_images`

把解答書（`from_slug`）的解答 figure 圖檔，依本書 `problems[].solution[]` 引用，補進本書 images 目錄。sol_extract merge 後若解答圖沒跟著搬才需要。少見。

```json
{ "action": "copy_solution_images", "from_slug": "<解答書 slug>" }
```

### 3.6 ref_classifications（C4/C5 的「非資料修復」出口）

當正文 ref 的目標**本就不在本書**（跨冊引用、來源編號不符、misOCR 跨書雜訊），不該硬塞圖。把它登記成分類，`audit_catalog` 即**從 critical 扣除**（移進報告的 "Classified Noninternal/Source Refs" 段，不再算 critical）：

```json
{
  "type": "figure",                  // figure | table
  "ref": "36.5",                     // canonical num（與 finding 訊息一致；hyphen 會被正規成 dot）
  "classification": "external",      // 自由字串標籤，慣用：external / source_mismatch / mis_ocr
  "reason": "正文明寫 Chapter 36, Vol. I — 跨冊引用，非本書 figure",
  "where": "ch13.json body[119]",
  "evidence": "Text says: Chapter 36, Vol. I; see Fig. 36-5."
}
```

慣用分類：
- `external` — 明確跨冊/跨書引用（"Vol. I"、"Chapter 36" 在他冊）。
- `source_mismatch` — raw PDF 鄰頁有相鄰編號圖，但**這個編號的目標圖在原書就不存在**（原書排版/編號跳號）。
- `mis_ocr` — ref 本身是 OCR 雜訊（若能修正文則優先 `replace_text`，不能才分類）。

> ref_classifications **不改任何 parsed 資料**，只影響 audit 計分。務必填 `evidence`（你查 raw PDF 看到什麼）——這是 reviewer 信任此分類的唯一依據。**不要**拿它當逃避：能用別名/pdf_crop 真修的，先真修。

## 4. 逐類別修復策略

### C4 / C5 — missing figure/table ref（最常見，含 reif 教學案例）

正文提 `Fig X.Y` 但 catalog 沒 `fig-X.Y`。**決策樹**（先查證、再選 action）：

**Step 0 — 看 catalog 端鄰近 id**。先 dump 該章 figure ids 與 caption，比對 ref：

```bash
uv run python -c "
import json; from pathlib import Path
cat=json.loads(Path('book_pipeline/mineru_data/$SLUG/parsed/catalogs.json').read_text())
for e in cat.get('figures') or []:
    fid=str(e.get('id') or '')
    if fid.startswith('fig-9.16') or fid.startswith('fig-9.16'):   # 換成你的 ref 前綴
        print(fid,'|',repr(e.get('caption') or '')[:70],'| anchor',e.get('anchor'),'chunk',e.get('chunk_kind'),e.get('chunk_key'))
"
```

依四種情境分流：

**(A) 圖在，但 catalog id 跟正文 ref 差一格（caption 被 MinerU 接到鄰圖）** ← reif 主病灶
- 症狀：catalog 有 `fig-9.16`（caption 實為 "Fig. 9·16·1"）和 `fig-9.16--1`（caption "Fig. 9·16·2"），ref `9.16.1`/`9.16.2` 卻判 missing；或 `fig-8.6.2` 的 caption 是 "Fig. 8·6·3"。MinerU 把每張圖的 caption 往下/往上錯位一格。
- 修：對那個 block `set_fields`，把 `id` 改成 caption 真正對應的語意編號（`expect` 鎖現 id 防呆）。若不想動 id（怕牽動 anchor）或一塊要兼容多個 ref，用 `catalog_aliases` 補上 ref 字串（`_catalog_covers_ref` 只查 `id` 解出的 num，但別名是給 reader/未來檢索的權威標記；要讓 C4 真消失，**id 端的 canonical num 必須等於 ref**，故此情境通常得改 `id`）。
  - 鐵律：改 id 前確認**沒有別的 entry 已佔用目標 id**，否則造成重複。reif 的 `fig-9.16` 該改成 `fig-9.16.1`、`fig-9.16--1` 改 `fig-9.16.2`、`fig-9.16.3` 已正確 → 連鎖修，逐塊 `expect` 鎖原值。

**(B) 圖真存在於 raw PDF，但 MinerU 整塊漏抓**
- 查證：`pdf_contactsheet`（看縮圖頁）或直接 `Read` raw PDF 該頁確認圖在。
- 修：`pdf_crop_insert`（§5），插一個帶正確 `id`/`caption` 的 fig block 到正文 ref 鄰近的 selector。thomas_calculus.json 即此例（Figure 14.15 在左欄、MinerU 沒 emit）。

**(C) 正文 ref 是 misOCR**（`Fig. 8.6.2` 實為 `8.6.1`，或編號被 OCR 打錯）
- 查證：對照 raw PDF 該句原文。
- 修：`replace_text` 把正文裡錯的 ref 字串改對。改完該 ref 不再 demand 缺的編號，C4 自然消。

**(D) ref 目標本就不在本書**（跨冊 / 原書跳號 / 跨書雜訊）
- 查證：raw PDF 全文搜不到該編號的 figure caption；鄰近編號都在、就缺這個。
- 修：登 `ref_classifications`（§3.6），`source_mismatch` 或 `external`，附 evidence。reif 不屬此類（reif 8 個都是 (A) 錯位，全可真修）；feynman/kittel 的 .json 有 (D) 實例。

> **reif_statmech 實況（教學案例）**：8 個 C4 全部是情境 (A)。實跑 `audit_catalog('reif_statmech')` 得 `missing_figure_refs=8`，refs = `15.4, 2.11.2, 2.8.1, 3.3.3, 3.9.1, 5.11.4, 8.6.1, 9.16.1`。逐一 dump catalog 鄰近 id 會看到：每個缺的編號其實都有對應圖塊，只是 caption 與 id 被 MinerU 錯位一格（如 `fig-9.16` 的 caption 是 "Fig. 9·16·1"、`fig-8.6.2` 的 caption 是 "Fig. 8·6·3"、`fig-3.3.2--1` caption 才是 "Fig. 3·3·2"）。修法：逐塊 `set_fields` 校正 `id` 使其 canonical num == ref，每筆用 `expect` 鎖原 id。**沒有任何一個需要 pdf_crop 或 ref_classification**——這證明「先查 catalog 鄰近 id」是 C4 的第一動作，多數 C4 是錯位、非真缺。

### C1 — fallback id

id 形如 `fig-ch09-…`（純位置、無語意編號）= parser 沒從 caption 解出 N.M。
- caption 看得出編號 → `set_fields` 給語意 `id`（+ 必要時補 `caption`）。
- 確實是無編號的內文裝飾圖（不該進 catalog）→ `set_fields` 設 `catalog_exclude_reason`（如 `"unnumbered_body_fig"`），該 entry 即從多數檢查豁免。

### C2 — empty caption

caption 空且無 exclude reason。
- 圖/表真有標題（raw PDF 看得到）→ `replace_text`（若原 caption 是某段殘字）或 `set_fields` 設 `caption`。
- 真無標題的裝飾圖 → `set_fields` 設 `catalog_exclude_reason`。

### C3 — missing image

`src` 指的檔四處 candidate（`unified/`, `unified/images/`, `parsed/`, `parsed/images/`）都不存在。
- `src` 檔名打錯/路徑漂 → `set_fields` 改 `src` 成真實存在檔名（先 `ls unified/images/` 找）。
- 該「圖」其實是純文字（公式/表格被誤判 fig）→ `set_fields` 設 `kind:'text'`（`kind=='text'` 的 figure 不檢查圖檔）。
- 圖檔真遺失但 raw PDF 有 → `pdf_crop_insert` 用同 `block.id` 重裁（會更新既有 block 的 src）。

### C6 — broken anchor

可見 catalog entry 的 `anchor` 不在 reader chunk 的 block id 集合（或缺 anchor metadata）。多半是 anchor 對應的 block `id` 被改過/重 parse 後變了。
- 找出 anchor 該指的 block，`set_fields` 把該 block 的 `id` 設成 catalog 期望的 anchor 值（或反過來，若 catalog 由 block id 重建，對齊 block id 即可）。核心是讓 **block id == catalog anchor** 兩端一致。

### C7 — unresolved visual

有 caption 但 `id` 空、無 exclude reason。
- caption 解得出編號 → `set_fields` 給 `id`。
- 無編號裝飾圖 → `set_fields` 設 `catalog_exclude_reason`。

## 5. pdf_crop_insert clip 座標決定法

`_crop_pdf_image` 規則（精確）：

1. **page**：1-based 實體頁，`page = doc[page-1]`。**注意這是 PDF 物理頁碼，不是 extract_rules 的 0-based `page_idx`**。先用 contactsheet 或 PDF reader 找到圖所在物理頁。
2. **clip 四值 `[x0, y0, x1, y1]`**，左上原點、y 向下：
   - **全部落在 0–1（含）** → 視為 **normalized**，乘頁面 `rect.width/height` 換算：`x = rect.x0 + rect.width*x0` …。**推薦用 normalized**（跟頁尺寸無關、好估）。
   - **任一值 > 1** → 視為**絕對 points**（72 pt/inch），直接 `fitz.Rect(*clip)`。
3. **zoom**：`matrix=Matrix(zoom,zoom)`，default 2.5 → 約 180 dpi。圖小/要清晰可拉到 3–4。
4. **aspect** 自動 = `clip.width/clip.height`，覆寫進 block，不用自己填。

**決定 normalized clip 的方法**：
- 先拿頁尺寸：`uv run python -c "import fitz; d=fitz.open('raw_pdfs/<f>.pdf'); r=d[<page-1>].rect; print(r.width,r.height)"`。
- 估圖在頁面的相對位置：左欄上半 ≈ `[0.05, 0.05, 0.48, 0.5]`，整頁中段 ≈ `[0.1, 0.35, 0.9, 0.7]`。
- 裁完先 apply、開 `unified/images/<src>` 目視確認框對；偏了就調 clip 重 apply（同 id 會更新、不重插）。寧可框略大含留白，別切到圖內容。
- thomas 實例 `[0.035, 0.405, 0.315, 0.94]` = 左欄、縱跨頁面 40%–94%（細長直圖）。

## 6. 終局原則：accepted 殘留（不要無限糾纏）

少數 critical 若**源頭真缺失**——MinerU 無法 OCR 出該塊、raw PDF 本身就沒那張圖/那段 caption、且 `pdf_crop_insert` 也無從裁（圖根本不在任何頁）——**停手**，不要為了清零而瞎塞假圖或亂改編號。

收工程序：
1. 對「真缺」的 C4/C5：盡量走 `ref_classifications`（§3.6）標 `source_mismatch`/`external` 附 evidence → 它們從 critical 計分移除，是**正當歸零**而非掩蓋。
2. 對無法分類也無法修的其他類別（C1/C2/C3/C6/C7 的源頭缺失）：保留 finding，在 `parsed/_catalog_audit.md`（audit 自動產）之外，於你的 handoff 回報明列「accepted 殘留」清單，每條含 `code / entry label / 為何無法修（你查 raw PDF 看到什麼）`。
3. **判準**：已查 raw PDF 確認源頭缺、已試過對應 action（或確認 action 不適用）、再投入也不會改變結果 → accepted。**禁止**為綠而把可疑塊一律設 `catalog_exclude_reason` 或亂改 id 製造假覆蓋。
4. 回報殘留數與每條理由，收工。daemon 端把「critical 全 accepted、有理由」視為此 stage 完成。

## 7. 硬規則

- **絕不手改 `parsed/*.json`**——一切走 `catalog_overrides/<slug>.json` + `apply_catalog_overrides`。手改會在下次 parser 重建時蒸發。
- **你的合法產物只有 `catalog_overrides/<slug>.json`**（+ pdf_crop 寫的 `unified/images/*.png`）。**禁止改任何
  `book_pipeline/*.py`**（含 `build_catalogs.py`/`catalog_audit.py`）。`build_catalogs` 抓不到你這本的圖說
  形態（如離散 caption block）時：**不要改 build_catalogs 讓自己過**（污染所有書、且被 scope_guard 自動還原成
  提案）。先試 override（set_fields/aliases/pdf_crop_insert 多半能逐本解）；override 真涵蓋不了才提案：
  ```bash
  uv run python -m book_pipeline.proposals propose --domain engine --type tooling-gap \
    --slug <slug> --title "<build_catalogs 缺哪種 caption 偵測能力>" \
    --evidence "<具體形態 + 為何 override 解不了>" --proposal "<建議引擎怎麼補>"
  ```
- override 是**疊加、可重播、冪等**：每筆給唯一 `id` 標籤、`reason`，能加 `expect` 就加（防 selector 飄移）。
- **不破壞既有正確 catalog**：改 id 前先確認目標 id 沒被別的 entry 佔用；replace_text 的 `old` 要長到唯一。
- `ref_classifications` 必填 `evidence`，且只用於**真不在本書**的 ref；能真修的先真修。
- selector index 一律取自 finding 的 `context.path`（格式與 `_block_path` 一致），不要自己數。
- 白名單外的 set 欄位 / 非 `{md,caption,tex}` 的 replace field / 非 `body[N]`/`problem:NUM:field[N]` 的 selector → apply 直接 raise。
- raw PDF 與 MinerU/zlib 憑證各機獨立、不入 git；pdf_crop 只在有 `raw_pdfs/<f>.pdf` 的機器（本機/standby）可跑。
- 每輪 apply 前自動備份到 `parsed/_override_backups/<ts>/`，但那是安全網不是版本控制——你的真相是 override json。

## 8. 派發流程

daemon / `/book-pipeline` dashboard 對「parsed 完成但 `audit_catalog` critical>0」的書派發：

1. 派 general-purpose sub-agent，prompt 含本檔 §2–§6 全文 + slug + 該書 raw PDF 路徑。
2. agent 跑 §1 工作流程：audit → 逐 finding 決策（§4，C4 先查 catalog 鄰近 id）→ 寫 override → apply → 重 audit，iterate 至 critical=0 或 §6 accepted。
3. agent 回報：起始/最終 critical 數、各 override 一行摘要、accepted 殘留清單（含理由）。
4. 主對話/daemon 驗 `critical == 0`（或殘留全 accepted 且有 evidence）→ `git add book_pipeline/catalog_overrides/<slug>.json` + commit（**只 commit override json，不 commit 任何 parsed/img 機器產物**）。
