# Audit Sol — 為解答書產 sol_rules.yaml 並 merge 進主書

把 `<slug>_sol` 解答書的解答 merge 進主書 `parsed/chNN.json` 的 `problem.solution`。

**範式定位**：book-pipeline「LLM 產配置 → 確定性引擎執行 → metric → 語義驗證 → iterate」範式的解答書版（對象=解答書，產物=`sol_rules.yaml`，引擎=`sol_extract.py`）。主書版見 `audit-book.md`。

**與 audit-book 的關鍵差異**：多一層 **LLM 不可省的語義判斷**——配對率高 ≠ 配對對。必須抽 body 對照，才能識破「主書 body 是 OCR 垃圾」這種純 metric 看不出的情況（boas 慘案）。

## 1. 環境前提

- 主書已 audit + parse：`book_pipeline/mineru_data/<main>/parsed/chNN.json` 存在（有 `problems[].num`）
- 解答書已 ingest：`book_pipeline/mineru_data/<sol>/unified/content_list.json` 存在
- 引擎 `book_pipeline/sol_extract.py` **不要動**（只產配置 yaml）
- **parser 每次重跑會清掉 solution，sol_extract 必須在 parser 之後跑**
- 派 general-purpose sub-agent，給它本檔 §2+§3 全文 + main_slug + sol_slug

## 2. sol_rules.yaml Schema

放 `book_pipeline/mineru_data/<sol_slug>/sol_rules.yaml`（入 git，貴重配置）。無此檔時引擎用 Griffiths 預設。

```yaml
chapter_re: '^Chapter\s+(\d+)\s*$'          # required；恰好 1 capture group = 章號(int)
problem_re: '^Problem\s+(\d+\.\d+[a-z]?)'   # required；group(1) 必須等於主書 problem['num'] 字串
multi_per_block: false                       # 一個 text block 內擠多答案(Boas 風) → true，用 finditer 切
equation_label_re: '\tag\s*\{([0-9]+\.[0-9]+[a-z]?)\}'  # optional
chapter_level: null                          # optional；章錨可接受的 text_level。null(預設)=任意層級
                                             #   (解鎖章標落在 lvl2/header 的解答書)；設 int 限定該層級
_pending: true                               # 設此 → 引擎拒絕 merge（主書品質不足時用）
```

**key 對齊是核心**：`problem_re` 的 group(1) 抓出的字串，必須與主書 `parsed/chNN.json` 內 `problem['num']` 逐字相等才配得上。先讀主書 num 格式，再設計 problem_re：
- 主書 num `"N.M"`（如 griffiths/boas）→ group(1) 抓 `"N.M"`
- 主書 num 純整數、章內 reset（如 kittel）→ group(1) 抓題序整數，章靠 chapter_re 分
- sol 題號是 `"C-M."` 但主書 num 純整數（hartle）→ group(1) 抓 M（題序），丟棄章前綴

## 3. 方法（Deterministic + 語義判斷）

### Step 0 — 先跑 sol_scout（必做第一動作，決定能不能 merge）

```bash
uv run python -m book_pipeline.sol_scout <SOL>
```

它一次確定性算出**章錨可行性**：解答書的「章標」到底落在哪個 `text_level`、各有幾個帶數字章號，
附題號 prefix 樣本與**判讀**。引擎 `extract_sol_chapters` 章錨**預設認任意 `text_level` 的 text block**
（`chapter_level: null`，由 anchored `chapter_re` 當濾網）——**章標落在 lvl2/header 不再是阻塞**（2026-06
章錨層級可配後解鎖；舊 references 說「只認 lvl1」已過時）。

- scout 判 **✓ 可錨**（任一層級帶數字章標數 ≈ 主書章數）→ 照 Step 1–7 寫 rules、dry-run、merge。
  **章標在 lvl2/header（scout 顯示 lvl1≈0 但 lvl2 有數字章標）→ 照樣 merge**，sol_rules 省略 `chapter_level`
  即可（預設 null=任意層級）；只有當 chapter_re 在某層級會誤抓散文時，才設 `chapter_level: 1` 限定。
- scout 判 **⚠ 真無可用章號**（任何層級都沒有可轉 int 的數字章標——如純羅馬數字章標需 int 映射、或源頭
  根本缺 chapter heading block）→ chapter_re 救不了 → Step 6 設 `_pending` + 開 `harness-gap`（引擎缺 int
  映射）或 `source-quality`（源頭缺 anchor）proposal。先跑一次 Step 4 dry-run、看到大量章 `0/N 全空` 坐實再下。

**版本對齊閘（入口已判時自動生效）**：若書單管理 agent 在入口已親判此解答本與母書版次不對齊
（`editions/<sol>.json` 的 `sol_alignment.aligned=False`），正式 merge（Step 7）會被引擎**自動擋下
（rc=3）並開 `edition-mismatch` proposal**——這是預期的題號錯位防護，**你直接收斂、不要 iterate
regex**（regex 救不了版次錯配）。`editions show <sol>` 可先確認；未判（留空）則閘放行（fail-open）。

### Step 1 — 讀主書 num 格式（scout 已附概況；要細節才手跑）
```python
import json, glob
from pathlib import Path
for cf in sorted(glob.glob(f'book_pipeline/mineru_data/{MAIN}/parsed/ch*.json'))[:4]:
    d = json.loads(Path(cf).read_text())
    print(d['num'], [p['num'] for p in d.get('problems', [])[:6]])
```
記下主書 num 是 `N.M` / 純整數 / 其他。這決定 problem_re 的 group(1) 該抓什麼。

### Step 2 — 章 anchor 與題號格式（scout 已給；僅補細節）

scout 的「章錨候選分布」已列出各 `text_level` 帶數字章標數與樣本、題號 prefix 樣本——**讀它即可定
chapter_re/problem_re**，不必再開 content_list 生肉手翻。章錨**預設認任意層級**（chapter_re 當濾網）；
章標落在 lvl2/header 照樣可錨（sol_rules 省略 `chapter_level` 即 null=任意層級，無須特別處理）。寫
chapter_re 時注意大小寫（kittel `CHAPTER` 全大寫）、hyphen 題號（hartle `2-1.`）、一行多答案
（boas `1.1 .. 1.2 ..` → multi_per_block）。

### Step 3 — 寫 sol_rules.yaml
依 §2 schema。chapter_re 恰好 1 group，problem_re group(1) 對齊主書 num。

### Step 4 — dry-run 量配對率
```bash
uv run python -m book_pipeline.sol_extract <MAIN> <SOL> --dry-run
```
看總配對率 + per-chapter。某章 `0/N 全空` → 該章 anchor 沒命中或原書缺該章解答（Step 5 判別）。

### Step 5 — 語義抽樣對照（**LLM 必做，不可省**）
對至少 **3 章 × 各前 3 題**，並排主書題幹與配到的 sol body：
```python
from book_pipeline import sol_extract as S
rules = S.load_sol_rules(SOL); sol = S.extract_sol_chapters(SOL, rules)
# 對每章每題印 主書 p['body'] 摘要 vs sol[ch][p['num']] 摘要
```
判定：
- **同題**（題幹與解答講同一主題/同一問題）→ 對齊正確
- **錯位**（系統性配到不同題）→ 回 Step 3 修 regex/anchor（可能章偏移、題號 group 抓錯）
- **主書 body 本身不是題幹**（如 OCR 把答案數字串當題目）→ 主書品質不足

### Step 6 — 決策門檻（自主定，不要問）

**一次定生死**：你這一趟 dispatch 內就要收斂到**終態**，二擇一——**merge** 或 **`_pending` ＋ 開 proposal 申訴**。
別把問題留給「下次重跑」（同一本書、同樣輸入重跑只是賭隨機性）。系統性錯位就在本次內 iterate（至多 3 輪）修到好。

| 情況 | 動作 |
|---|---|
| 語義抽樣多數對齊（≥~80%）、配對率合理 | 正式 merge（去 --dry-run） |
| 對齊正確但配對率低（OCR 漏題 / 原書缺某章解答） | merge，接受殘缺，_audit 記原因 |
| 系統性錯位 | 回 Step 3 iterate（**本次 dispatch 內**，至多 3 輪） |
| 主書 body 是 OCR 垃圾 / 解答本缺章 anchor，**無法產出品質 merge** | 設 `_pending: true` + 註解原因，**不 merge**，並**開 proposal 申訴**（見下） |
| 解答本根本是別本書 / 版次不符 | **不 merge**，開 proposal（`edition-mismatch`），設 `_pending` |
| 對齊需要 sol_extract 引擎沒有的能力 | 盡力 merge 能 merge 的，開 proposal（`harness-gap`）記缺口 |

**禁 cheat**：不准為衝高配對率放鬆 problem_re 而塞錯解答。配對率是參考，語義對齊才是真相。

**申訴管道（拒絕 merge 時必開，別默默 `_pending` 埋掉）**：你判「這本現在產不出品質 merge」是對的決定（爛 merge 比沒答案更糟），但**源頭問題只有架構師能修**（換更完整的解答本/母書、修 parser）——所以要攤給他，而非靜默放棄：
```bash
uv run python -m book_pipeline.proposals propose --domain sol \
  --type source-quality \   # 或 edition-mismatch / harness-gap
  --slug <main>_sol --source sol_extract \
  --title "<main> 解答本無法 merge：<一句話原因>" \
  --evidence "配對率 X%；語義抽樣：第N章題M 主書 body 是『…』非題幹（OCR 垃圾）" \
  --proposal "換更完整的解答本/母書版次，或修 parser 章邊界後重 ingest"
```
type 選擇：`source-quality`（OCR 垃圾/缺 anchor）｜`edition-mismatch`（配錯書/版次）｜`harness-gap`（引擎能力不足）。

### Step 7 — 正式 merge（非 pending 時）
```bash
uv run python -m book_pipeline.sol_extract <MAIN> <SOL>
```
寫進 gitignored `parsed/`，靠 sol_rules.yaml + sol unified 重跑重現（parser 重跑會清 solution，sol_extract 必須在 parser 之後）。

## 4. 產出（終態二擇一，不留待重跑）
1. `book_pipeline/mineru_data/<sol>/sol_rules.yaml`（入 git）
2. **merge 結果**（parsed/，不入 git）**或** `_pending` 標記 ＋ **sol proposal**（拒絕時必開，見 Step 6）
3. 回報：配對率、語義抽樣對齊判定、merge / `_pending`+proposal 決策與理由

## 5. 主對話收尾
- agent 回報後跑 `uv run python -m book_pipeline.status <main>` 確認階段（`4 sol已merge` 或 pending 不誤報）
- `git add` sol_rules.yaml + commit（solution 在 parsed/ 不入 git）
- 重跑前提：sol 本 unified 須在本機（剛 ingest 即在；換機後可 `drive_queue.py restore <sol>` 或重跑 ingest 重生）才能重跑 sol_extract

## 6. 派發流程
由 `/book-pipeline` dashboard 對 `待辦=sol_extract(<sol>)` 的書派發：主對話派 general-purpose agent（prompt 含本檔 §2+§3 + main_slug + sol_slug），agent **本次 dispatch 內收斂到終態**（merge 或 `_pending`+proposal），回報；主對話 §5 收尾。**不跨 tick 重派同一本**——daemon 只在「merge 成功重烤」或「升級」後就不再出此 todo。
