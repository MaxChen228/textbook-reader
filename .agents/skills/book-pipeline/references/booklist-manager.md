# booklist-manager — /restock 的四維查證引擎（多源親查：夠格∧連結∧版本∧解答對齊）

這是 `/restock` skill 的作業手冊。你替每一本書做**四維「合格存在」查證**，把結論落進機器域。四維綁成單一不可分判斷：

1. **維① 夠格收錄** — 大學級教科書／專著／講義／參考書（理工優先）。存量書（已在書單）信任人工夠格、不複核；**只有 discovery 找的新書**要你親判夠格。
2. **維② z-lib 連結** — 找到合法可下載 PDF 的 id+hash。
3. **維③ 版本** — 親查確認哪一版、對不對 `edition_pref`。
4. **維④ 解答對齊**（僅 `*_sol`）— 解答本與母書同版（題號才對得上）。

你**只查、只判斷、只落盤**——不下載、不選書、不碰額度、不改人工正典書單。

## 為何是你、為何多 haiku

選哪些書、下載都已確定性化；唯獨「這本在 z-lib 是哪一筆、哪一版、夠不夠格」需要判斷，且**單一來源不可靠**：z-lib 書名常不帶版次、帶了也未必實際；光看標題會把「Chemistry」配到《Food Chemistry》、把 10th 解答配到 11th 書。所以你**對每本書 fan-out 數個 haiku subagent 並行查不同來源、各自親判，你再綜合取共識**——多源交叉壓掉單點誤判，而不是賭某字串。

## 兩條鐵律（不可違反）

1. **四維一律 LLM 親查、禁硬編碼**：不准書名正則抽版次、不准字串比對當裁決。版次事實要有來源佐證（z-lib detail 的 edition/isbn、web 查到的版次史）。
2. **絕不手改書目正典**（舊 `booklists/*.json` 已退役封存 `booklists/_archive/`，universe 改 editions）。產出只落機器域：`resolve commit`（維②連結）、`editions set`（維①③④）、`discovered add`（新書候選）、`proposals propose`（owned mismatch／系統性問題）。

## 三層落盤心智模型（合格存在）

| 維 | 落哪 | 指令 |
|---|---|---|
| ② 連結 | `crawl_resolution.json`（純連結，二態 found/not_found） | `resolve commit` |
| ① 夠格 | `editions/<slug>.json` | `editions set --eligible`（新書才需） |
| ③ 版本 | `editions/<slug>.json` | `editions set --label … --matches-pref` |
| ④ 解答對齊 | `editions/<slug>.json` | `editions set --sol-aligned …` |

狀態由系統衍生：四維全過＝QUALIFIED（買書員會下載）；有連結缺維＝PENDING（待回查）；無連結＝CANDIDATE；not_found 或判不夠格＝REJECTED（無法收錄）。**你不寫狀態字串，只寫四維事實。**

## 工作清單（/restock 已在 prompt 指定批次；以下為自查備援）

```bash
uv run python -m book_pipeline.resolve queue --state candidate --limit 30  # 無連結待查（維②起手）
uv run python -m book_pipeline.resolve queue --state pending   --limit 30  # 有連結但維③④①未全過（存量回查）
```

逐本處理。單本失敗不要停，記下繼續下一本。

## 每本書：多源四維查證 workflow

```bash
# 0. 先看你在找什麼（canonical 書名/作者/edition_pref/類型；解答本附主書身份 + 已查版本）
uv run python -m book_pipeline.resolve target <slug>
uv run python -m book_pipeline.editions show <slug>     # 既有四維結論（存量回查時看缺哪維）
```

**對這本書 fan-out 數個 haiku subagent 並行查證**（Agent 工具，subagent_type=general-purpose、model=haiku；各給獨立自包含任務）：

- **haiku「z-lib 候選」**：跑 `resolve search <slug>`（只 search、不耗額度；每筆帶 metadata + advisory_conf + kind_match + `book_qc` 書況預檢）；對最像的 1–3 筆跑 `resolve inspect <id> <hash>` 深挖 edition/year/publisher/isbn/pages。回報候選清單 + 各自「是不是這本、像哪版」初判。
- **haiku「版次事實」**：WebSearch 查「<書名> <作者> editions / ISBN」，確認此書**客觀上有哪些版次**（年份、ISBN、出版社），尤其 `edition_pref` 那版的辨識特徵。這是**獨立於 z-lib 的第二來源**。
- （discovery 新書）**haiku「夠格鑑別」**：判此書是不是大學級教科書/專著/講義/參考書（維①）。
- （歧義大時）**haiku「交叉裁決」**：把上述結果擺一起，判某 z-lib 候選對應哪版、是否就是 `edition_pref`。

**你（主 agent）收斂共識**：綜合各 haiku，**親自**下最終判斷——這筆候選是不是這本書、哪一版、對不對 `edition_pref`、（新書）夠不夠格。多源一致才有信心，分歧就再查或留空。**先排除 `book_qc.block` 非空的候選**（鐵定配錯書，commit 會硬拒）。

## 落盤四維結論

```bash
# 維②：找到對的書與版的可下載連結 → found
uv run python -m book_pipeline.resolve commit <slug> --id <id> --hash <hash> \
    --title "<候選標題>" --author "<候選作者>" --mb <大小> --by restock

# 維①（discovery 新書才需；存量書已 eligible，跳過）：判夠格 + 歸類
uv run python -m book_pipeline.editions set <slug> --eligible \
    --field-id <field_id> --subject "<科目>" --by restock

# 維③：版本親查結論（--matches-pref = 此版符合 edition_pref）
uv run python -m book_pipeline.editions set <slug> \
    --label "3rd" --year 2018 --publisher "Cambridge" --isbn 9781107189638 --matches-pref \
    --confidence high \
    --evidence "z-lib detail edition=3 + web 確認 3rd 2018 Cambridge ISBN 對上 + 多源一致" \
    --source "zlib_detail:<id>/<hash>" --source "web:editions 查證" --by restock
```

維②+①+③（解答本再加④）全落齊，這本就升 QUALIFIED、計入 /restock 淨增。

### 只有別版（edition_pref 那版 z-lib 查無）

別降版硬塞、也別當 not_found。**commit 你找到的那個連結為 found**，但維③記 `--no-matches-pref` → 系統判 PENDING（有連結未驗對版、日後可重查）：

```bash
uv run python -m book_pipeline.resolve commit <slug> --id <id> --hash <hash> --title "<別版候選>" --by restock
uv run python -m book_pipeline.editions set <slug> --no-matches-pref --confidence high \
    --label "2nd" --evidence "z-lib 僅 2nd/4th、web 確認 3rd 存在但 z-lib 無此版" --by restock
```
若連任何合法連結都沒有 → 不 commit，留 CANDIDATE（下輪重查）。

### z-lib 真無此書（多源確認、非版次問題）

```bash
uv run python -m book_pipeline.resolve commit <slug> --status not_found \
    --note "<為何確認真無：無任何合法 pdf／該書無公開解答本，多源一致>" --by restock
```

### 書單書名歧義／版次矛盾／卷次不清 → 開 proposal、留空（不 commit）

歧義書不 commit（留 CANDIDATE），改開 proposal 交架構師裁決——proposal 就是架構師佇列：

```bash
uv run python -m book_pipeline.proposals propose --domain crawl --source restock \
    --type booklist-fix --title "<一句話歧義點>" --evidence "<slug/候選/查證證據>" \
    --proposal "<建議怎麼改>" --risk "<誤傷面>"
```

### discovery 新書判不夠格 → 落 REJECTED

```bash
uv run python -m book_pipeline.editions set <slug> --no-eligible \
    --evidence "<為何不夠格：大眾科普/考試書/已被取代舊版…>" --by restock
```

## 解答本（_sol）：維④版次對齊母書（題號防護）

解答本題號**綁母書版次**——11th 解答配 10th 母書，題號全錯位、答非所問。查解答本（slug 結尾 `_sol`）時，除了找對的解答本（維②③），還要**親判它與母書確認的版次是否同版**（維④）：

```bash
uv run python -m book_pipeline.editions show <main_slug>     # 先看母書已查版次

# A. 找到與母書同版的解答本 → found + editions 記對齊
uv run python -m book_pipeline.resolve commit <sol_slug> --id <id> --hash <hash> --title "<候選>" --by restock
uv run python -m book_pipeline.editions set <sol_slug> --label "11th" --matches-pref --confidence high \
    --sol-aligned --parent-version "11th" --sol-version "11th" \
    --basis "解答本書名標 11th、與母書 11th 同版" --by restock

# B. 只找得到別版解答 → commit 該連結 found，維④記 --no-sol-aligned（別硬塞、PENDING 可重查）
uv run python -m book_pipeline.resolve commit <sol_slug> --id <id> --hash <hash> --title "<10th 解答>" --by restock
uv run python -m book_pipeline.editions set <sol_slug> --no-sol-aligned \
    --parent-version "11th" --sol-version "10th" --basis "只有 10th 解答、母書 11th" --by restock
```

**為何認真寫 `sol_alignment`**：下游 merge 引擎讀它——`--no-sol-aligned` 時引擎**自動擋下 merge**（防題號錯位污染母書）、改開申訴。你不判（留空）則引擎 fail-open 放行。**寧 `--no-sol-aligned` 擋住，也別讓錯版解答靜默 merge**。母書版次還沒查時，先查母書定版再對齊。

## 判準速記

| 多源查證結果 | 落盤 |
|---|---|
| 書名+作者吻合、找到 `edition_pref` 那版、z-lib 有 pdf、多源一致 | `commit found` + `editions --matches-pref`（→ QUALIFIED） |
| 書對、z-lib 只有別版 | `commit found`（別版連結）+ `editions --no-matches-pref`（→ PENDING 可重查；別降版硬塞、別當 not_found） |
| 多源確認 z-lib 真無此書/此解答（非版次） | `commit --status not_found`（永不再查） |
| 候選 `book_qc.block` 非空、作者對不上、像同主題別本 | 不採；換查法或上述 |
| 解答本對不到那本正書（撞同主題別書題解） | 別硬配；找對的，找不到 → not_found |
| 書名歧義/版次矛盾/卷次不清 | 開 proposal booklist-fix、留 CANDIDATE（不 commit） |
| （discovery 新書）判不夠格 | `editions set --no-eligible`（→ REJECTED） |

寧缺勿錯：與其 commit 可疑的，不如留 CANDIDATE 開 proposal——下游花的是真實下載額度與 OCR 成本。

## owned 回查（保命）：發現 mismatch 只標、絕不下架

回查時碰到**已收錄（owned）**的書（`resolve target` 顯 status=owned），若發現它版本不對/不夠格——**絕不刪、絕不下架、絕不改它的連結**。已 OCR 的實體資產優先於四維判斷。只開 proposal 交架構師：

```bash
uv run python -m book_pipeline.proposals propose --domain crawl --source restock \
    --type edition-mismatch --title "owned <slug> 版本疑慮" \
    --evidence "<owned 的是 X 版、書單要 Y 版、佐證…>" \
    --proposal "<建議：換版重 ingest / 改 edition_pref / 維持>" --risk "下架會丟已 OCR 成果"
```
可順手 `editions set <slug>` 補上你查到的真實版本事實（記錄），但**不**動 owned 狀態。

## Discovery mode：找夠格新書（書單自我生長，/restock 存量回查不足時）

替指定領域找**夠格的新書**寫進候選層，讓書單自己長。

**夠格鑑別（嚴格、寧缺勿濫）**：收＝大學級教科書／研究專著／講義／參考書（理工優先、不限主題）；排除＝小說／大眾科普／考試用書／操作手冊／已被取代舊版。

**書源（fan-out haiku 並行找）**：
- haiku「參考文獻輻射」：對已收的同領域經典，找它常被併列/引用的同級教科書。
- haiku「課程書目種子」：權威大學該領域課程的指定教科書清單。

**收斂 + 落候選**（去重交給工具，你只管夠格判斷）：

```bash
uv run python -m book_pipeline.discovered add <field_id> --field "<領域中文名>" \
    --slug <new_slug> --title "<書名>" --author "<作者>" --subject "<科目>" \
    --note "<來源：如 griffiths_qm 參考文獻 / MIT 8.04 指定書>"
```

候選**自動流入查證**（與人工正典同等走四維查證 + book_qc gate）；架構師 git diff 抽查、可 `discovered remove <field_id> <slug>` 否決或晉升進 booklists。找不到夠格的就少加，別湊數塞次級書。落候選後，對每本新書走完整四維查證（先判維①夠格 `editions set --eligible` 再往下）。

## 這不是你的事（硬邊界）

- **絕不下載**：不跑 `crawl_zlib fetch`、不碰 `/dl/`。下載是買書員的事、咬真實額度。你只 search。
- **絕不改人工正典 booklists**：discovery 只寫 `discovered/` 機器候選層，仍**絕不碰 booklists**。
- **絕不下架/刪 owned 書**：mismatch 只標 + proposal。
- **絕不碰額度/帳號**。
- 產出只有：`resolve commit`（維②）、`editions set`（維①③④）、`discovered add`（新書）、`proposals propose`。
