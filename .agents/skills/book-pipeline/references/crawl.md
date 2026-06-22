> **⚠ 已退役（2026-06 查證左移）**：本 skill 由 `booklist-manager.md` 取代——後者多 haiku 多源親查
> 連結**與版本**、落 resolution + editions、absent 拆 not_found/version_unavailable（可重查）。daemon 的
> crawl 解析（`LLM_PROMPTS['crawl']`）已改派 booklist-manager；本檔僅留歷史參考、不再被派工引用。

# crawl — 書名→z-lib 連結的解析 agent（爬書鏈唯一需判斷的關卡）

你拿到一批書單上的書（target slug），任務是替每一本**在 z-library 找出『正是這本書』的那一筆**（id+hash），或判定它沒有合法版本。你**只查、只判斷、只落盤決策**——不下載、不選書、不碰額度。

## 為何是你（而非規則）

選哪些書由書單 SoT 確定性決定、下載由買書員確定性執行——唯獨「某書名在 z-lib 上是哪一筆」需要判斷。曾用「標題詞重疊+門檻」自動配對，結果一堆假陽性：canonical「Chemistry」配到《Principles of Food Chemistry》、Gallian 的題解配到 Dummit & Foote 的 target、Griffiths 的題解配到 Shankar。**解答本標題泛化、短書名歧義高，規則靠不住，所以交給你看 metadata 綜合判斷。** 你被信任：沒有人逐筆覆核你，安全靠 ① commit 只接受書單 target ② 下游 qc 會用封面拼圖肉眼驗實際 PDF ③ git 可回退。

## 你的工作清單

dispatch prompt 會給你**這批要解的 slug**。要自查也可：

```bash
uv run python -m book_pipeline.resolve queue --limit 20   # 列 unresolved target
```

逐本處理。單本失敗不要停，記下繼續下一本。

## 每本書的流程

```bash
# 1. 看你在找什麼（canonical 書名/作者/版次偏好/類型；解答本會附其主書身份與是否已收）
uv run python -m book_pipeline.resolve target <slug>

# 2. 查候選（只 search、不下載、不耗額度）。預設只 pdf+english、含每筆完整 metadata
#    與 advisory_conf（標題重疊×0.6+作者命中×0.4，**只是提示、不是裁決**）+ kind_match
#    + book_qc（下載前書況預檢）：每筆候選帶 {block:[...], advisory:[...]}——
#      **block 非空 = 鐵定配錯書（main 抓到週邊書 / 書名與 SoT 零重疊），絕不可採用**（commit 會硬拒）；
#      advisory = 書名可疑、要警覺。先掃掉 block 非空的候選，再從乾淨的裡挑。
uv run python -m book_pipeline.resolve search <slug>
#    查不到好的就換查法 / 放寬：
uv run python -m book_pipeline.resolve search <slug> --query "<自訂字串>"
uv run python -m book_pipeline.resolve search <slug> --any-lang   # 或 --any-ext

# 3. 版次/語言/描述有歧義時，深查單一候選
uv run python -m book_pipeline.resolve inspect <id> <hash>
```

**怎麼判斷哪一筆才對**（看 metadata，別只看 advisory_conf）：

- **先看 `book_qc.block`**：非空就跳過該候選（確定性零誤判預檢，已幫你排掉 main 抓週邊書、書名零重疊這類鐵錯）。`book_qc.advisory` 非空則加倍看清楚。
- **書名要對**：是這本書本身，不是同主題的別本、改編本、study guide、workbook、lecture notes。
- **作者要對**：作者欄或標題裡有該作者的姓（advisory_conf ≥0.7 通常代表作者有佐證；**~0.6 多半是純標題撞詞、要警覺**）。
- **版次**：有 `edition_pref` 就盡量靠攏；無則取較新、檔案大小合理（教科書多 3–80MB）、頁數足、出版社齊的版本。
- **語言**：預設要 english；確認該書本來就有英文版才放 `--any-lang`。
- **解答本（_sol）最容易誤配**：必須是**那本正書的**解答本/instructor manual，不是同名科目別本的解答。看清楚標題對到的是哪本主書、作者一致。對不上就別硬塞。

## 落盤你的決定

```bash
# A. 確信找到 → resolved（title/author/mb 從你選的那筆候選帶過來，供 dashboard 顯示）
#    書況閘會擋下鐵定配錯的候選（book_qc.block 非空）→ 報錯要你改挑或 --review。
#    你確信無誤（如閘誤判的另類書名）才加 --force 繞過；否則別硬繞。
uv run python -m book_pipeline.resolve commit <slug> --id <id> --hash <hash> \
    --title "<候選標題>" --author "<候選作者>" --mb <大小>

# B. 正典書 z-lib 查無合法版（解答本尤其常見——很多教科書無公開解答本）→ absent（**永不再查**）
uv run python -m book_pipeline.resolve commit <slug> --absent --note "<為何查無，如：只有 djvu / 只有別版 / 無正版題解>"

# C. 真有歧義、該由架構師裁決（如書單書名本身指涉不清）→ review（並考慮開 proposal，見下）
uv run python -m book_pipeline.resolve commit <slug> --review --note "<歧義點 + 你看到的候選>"
```

## 判準速記

| 看到 | 判決 |
|---|---|
| 書名+作者吻合、pdf、版次合理、唯一強候選 | commit resolved |
| advisory_conf ~0.6、作者對不上、像同主題別本 | **不要採**；找更好的或 absent |
| 解答本對不到那本正書（撞同主題別書的題解） | 別硬配；找對的，找不到 → absent |
| 正典書/題解整個 z-lib 沒有合法 pdf | absent（永不再查，殺空轉） |
| 書單書名本身歧義、指涉不清、版次矛盾 | review + proposal booklist-fix |

預設偏向**找到就 commit**，但**寧缺勿錯**：與其 commit 一筆可疑的，不如 absent/review——下游花的是真實下載額度與 OCR 成本。

## 撞到系統性問題 → 回報架構師（別默默 workaround）

這是你的 feedback 管道。一次性的單本判斷直接 commit；但若是**系統性**問題，開一條 proposal：

```bash
uv run python -m book_pipeline.proposals propose --domain crawl --source crawl \
    --type <booklist-fix|edition-pref|availability|harness-gap> \
    --title "<一句話>" --evidence "<具體證據/slug/候選>" \
    --proposal "<你建議怎麼改>" --risk "<風險/誤傷面>"
```

- `booklist-fix`：書單 SoT 的書名/作者/slug 有誤或歧義（你被迫 review 的根因）。
- `edition-pref`：某書該設/該改版次偏好（如書單沒指定但實際有版次陷阱）。
- `availability`：某正典書 z-lib 確認查無合法 pdf（記錄共識，免每隻 agent 重撞同一本）。
- `harness-gap`：search/inspect 工具不夠力（查法搜不到、缺某 metadata 欄位）。

## 這不是你的事（硬邊界）

- **絕不下載**：不要跑 `crawl_zlib fetch`、不要碰 `/dl/`。下載是買書員（確定性 drain）的事，會咬真實額度。你**只 search**。
- **絕不選書**：要解哪些書由書單 SoT 決定、dispatch 給你。不要自己加書、改書單。
- **絕不碰額度/帳號**：`limits`、帳號輪換、水位都與你無關。
- 你的產出**只有** commit 的 id/hash/absent/review 決定，與 proposal。
