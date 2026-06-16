# Audit Book — 為主書產 extract_rules.yaml

吃 `book_pipeline/mineru_data/<slug>/unified/content_list.json`，產出 parser.py 能直接吃的 `extract_rules.yaml`。

**核心契約**：派一個 general-purpose sub-agent 跑 deterministic 流程，**不靠範例對照、不靠 LLM 直覺**。所有規則來自下方 §2 schema 與 §3 方法。完成後主對話按 §5 流程 validate。

**範式定位**：這是 book-pipeline「LLM 產配置 → 確定性引擎執行 → metric → iterate」範式的主書版（對象=主書，產物=`extract_rules.yaml`，引擎=`parser.py`）。解答書版見 `audit-sol.md`。

## 目錄
- §1 環境前提
- §2 extract_rules.yaml 完整 Schema
- §3 方法（Deterministic 8 步）
- §4 產出與路徑
- §5 主對話 Validate（commit 前必做）
- §6 硬規則
- §7 派發流程

## 1. 環境前提

- `book_pipeline/mineru_data/<slug>/unified/content_list.json` 已存在（ingest 完成，見 `ingest.md`）
- `book_pipeline/parser.py` 與 `book_pipeline/mineru_ingest.py` **不要動**
- 派出的 sub-agent 用 `general-purpose`，給它 §2 + §3 + §4 完整指引

## 2. extract_rules.yaml 完整 Schema

```yaml
# ── 書本 metadata ──
# **必須 cite `book_pipeline/metadata_schema.yaml` 為 SoT**：
#   - subject 必須 ∈ schema.subjects（新主題先補 schema、不要瞎發明字串）
#   - edition 必須符 schema.edition_format.pattern（"4th"、"7th International"、null）
#     不知道版本 → 寫 null，**不要瞎填、不要空字串**
#   - author 多人用 "; " 分隔，不要 " and " 不要 ", "
# parser 完成後跑 `python -m book_pipeline.normalize_metadata` 自動驗，違規會 exit 1
slug: <str, 等同目錄名>                 # required
title: <str>                            # required
author: <str | null>                    # optional；format: "First M. Last; First M. Last"
edition: <str | null>                   # optional；format: "<N>(st|nd|rd|th)( <variant>)?"
subject: <str | null>                   # optional；必須 ∈ metadata_schema.yaml subjects
publisher: <str | null>                 # optional
language: <"en" | "ch">                 # optional, default "en"

# ── block 過濾 ──
filter_types: [<str>, ...]              # optional, default []
                                        # 允許值：page_number, header, footer,
                                        # page_footnote, aside_text, ref_text
ignore_image_content: <bool>            # optional, default false
ignore_chart_content: <bool>            # optional, default false
heading_text_level: <int>               # optional, default 1
                                        # MinerU 把 section/subsection/Example heading 標在哪個 text_level。
                                        # 多數書 =1；但約 1/3 書（strogatz/axler/munkres/cormen/boyd/peskin/
                                        # ashcroft/dummit/feynman_em2/lemons/lindner/oppenheim/reif…）MinerU 把
                                        # §-heading 全標成 lvl2 → **必須設 2**，否則 parser detect_heading 認不到
                                        # 任何 heading，body 變 flat（章內無 section/subsection/Example 結構）。
                                        # 判定見 §3 Step 3。預設 1，既有 lvl1 書省略即可（向後相容）。

# ── 內容範圍（global 0-based PDF page_idx） ──
body_start_page: <int>                  # required
appendices_start_page: <int>            # required
bibliography_start_page: <int | null>   # optional
index_start_page: <int | null>          # optional

# ── inline-problem 模式 ──
# Griffiths 風格：題目散落 section 中、無獨立 Problems heading。
# 兩種設法：
#   (a) 全本 inline → 此欄填 true，所有 chapter[].problems_block_idx 填 null
#   (b) 混合（如 griffiths_qm3 ch1-11 有 Further Problems heading、ch12 是 inline）
#       → 此欄填 true，pbi=null 的章走 inline，pbi=int 的章走二分
#   (c) 全本二分 → 此欄填 false 或省略，所有 pbi 必須非 null
# parser 對 `inline_problems=true 且該章 pbi=null` 跑單流水：整章 [cti+1, nci-1]
# 遇 problem_start_re 切題、遇 section/subsection heading 結束題目；題目跟正文交錯。
inline_problems: <bool>                 # optional, default false

# ── 章節 ──
chapters:                               # required, non-empty list
  - num: <int>                          # 章號（流水正整數）
    title: <str>                        # 章名（不含編號前綴）
    page_start: <int>                   # 該章 page_idx 起點
    page_end: <int>                     # 該章 page_idx 終點（含）
    chapter_title_block_idx: <int>      # primary anchor：章首 text_level=1 block 的 idx
    chapter_title_block_idx_secondary: <int | omit>  # 可選：章首 TOC 預覽末尾或拆兩段標題的次塊
    problems_block_idx: <int | null>    # "Problems" heading idx；null 表 inline（需 top-level inline_problems=true）或該章無題
    next_chapter_block_idx: <int>       # 下一章 chapter_title_block_idx（最後一章填 appendix A 的）

# ── 附錄 ──
appendices:                             # optional, list 可空
  - id: <str>                           # "A", "B", ...
    title: <str>
    page_start: <int>
    chapter_title_block_idx: <int>

# ── heading regex ──
# 兩條 regex 必須各有恰好 2 個 capture group: (id, title)
# 若該書無編號（純標題型），id group 用 '()' 空 group
section_re: <regex str>                 # required
subsection_re: <regex str>             # required
heading_priority: [subsection_re, section_re]  # required, 固定這個順序
heading_text_level: <int>               # optional, default 1
                                        # MinerU 把 section heading 標成哪個 text_level。
                                        # 多數書 section=lvl1；但少數書（Munkres：chapter title=lvl1、
                                        # §-section heading=lvl2）section 在 lvl2 → 設 2，否則 detect_heading
                                        # 偵測不到 section（整本無 heading 結構 + inline namespace 失效）。
                                        # 判定：跑 Step 3 看 text_level==1 是否只有 chapter title、
                                        # 真正的 section heading 落在 text_level==2 → 設 2。

# ── 題號 ──
problem_start_re: <regex str>           # required, 必須有 1 個 capture group（題號）
problem_chapter_must_match: <bool>      # required；題號首段是否須等於當章 num
                                        # 「N.M」型 → true；「N」純流水 → false
problem_num_namespace_by_section: <bool>  # optional, default false
                                        # 每節 Problem Set 題號從 1 重置（Strang 風格）→ true
                                        # walker 把當前 section_id 串到 num 前：§1.2 題 1 → num='1.2.1'
                                        # 只在 inline mode 生效（rules.inline_problems=true 且 ch.pbi=null）
problems_end_re: <regex str | null>     # optional, default null
                                        # problems 區內遇 text_level=1 text 命中即提早結束、丟棄其後 block。
                                        # 救「章末習題後緊接同 N. 編號的非習題段」(Brookshear: CHAPTER REVIEW
                                        # PROBLEMS 後接 SOCIAL ISSUES/ADDITIONAL READING)。二分與 inline 皆適用。
                                        # e.g. '^(SOCIAL ISSUES|ADDITIONAL READING)\s*$'
solution_start_re: <regex str | null>   # optional, default null
                                        # 題身後緊接解答的「題本」(Tamvakis: Problem N.M … 後接 lvl=1
                                        # 'Solution' heading + 解答)。題目開啟中遇 lvl=1 text 命中 →
                                        # 其後 block 收進 problem.solution（同 sol_extract merge 欄位、
                                        # 不混進 problem.body），至下一題/heading。二分與 inline 皆適用。
                                        # e.g. '^Solution\s*$'

# ── equation ──
equation_strip_dollar: <bool>           # required, 99% 為 true
equation_label_re: <regex str>          # required, 1 個 capture group（label 字串）

# ── example（內文題） ──
example_start_re: <regex str | null>    # 該書內文無 Example 段落填 null
                                        # **若非 null，必須恰好 1 個 capture group**（example 編號）

# ── figure ──
figure_caption_merge: <bool>            # default false
figure_caption_main_re: <regex str | null>

# ── OCR 漏題（可空） ──
known_missing_problems:                 # optional, default []
  - chapter: <int>
    nums: ["<num str>", ...]
```

## 3. 方法（Deterministic 8 步）

派 sub-agent 嚴格按下列流程。每步**輸入是什麼、輸出是什麼**寫死，不要自由發揮。

### Step 1 — 載入並建索引

```python
import json
from pathlib import Path
blocks = json.loads(Path(f'book_pipeline/mineru_data/{slug}/unified/content_list.json').read_text())
N = len(blocks)
```

每個 block 形如 `{type, text, text_level?, page_idx, bbox, ...}`。idx = list index。

### Step 2 — block type 統計與 filter_types 決定

```python
from collections import Counter
type_count = Counter(b['type'] for b in blocks)
```

**規則**（不協商）：
- `header` → 一律進 filter_types
- `page_number` → 一律進 filter_types
- `footer` → 一律進 filter_types
- `aside_text` → 一律進 filter_types
- `page_footnote` → 一律進 filter_types
- `ref_text` 若 count > 5 → 進 filter_types，否則不進
- 其他 type **不進** filter_types

`ignore_image_content` 與 `ignore_chart_content` 一律設 **true**（MinerU 的 image/chart content 是偽 mermaid 雜訊）。

### Step 3 — 偵測 heading_text_level 並列出 heading-level block

**先判斷 section heading 落在哪個 text_level**（MinerU 各書不一，約 1/3 書標在 lvl2，認錯 → body 全 flat）：

```python
from collections import Counter
import re
def secish(b):  # 像 'N.M ...' / 'NA ...'（Axler）section heading 的 block
    return b['type'] == 'text' and bool(re.match(r'^\d+[.\dA-Z]', (b.get('text') or '').strip()))
lvl_secish = Counter(b.get('text_level') for b in blocks if secish(b))
HEADING_LVL = lvl_secish.most_common(1)[0][0] if lvl_secish else 1
print('section-like heading 的 text_level 分布:', dict(lvl_secish), '→ HEADING_LVL =', HEADING_LVL)
```

- `HEADING_LVL == 1`（多數書）→ yaml 省略 `heading_text_level`（用預設 1）
- `HEADING_LVL == 2`（strogatz/axler/munkres 等）→ yaml **必須**設 `heading_text_level: 2`，否則 parser 認不到 heading、body 全 flat
- 注意：章標題（chapter title）可能落在**不同** level（Munkres/strogatz：章名 lvl1、§-section lvl2）。`heading_text_level` 只管 section/subsection/Example；chapter anchor（Step 4-5）仍由 `chapter_title_block_idx` 手動指定，不受此欄影響。

然後用該 level 建後續工作面：

```python
H = [(i, b['page_idx'], b.get('text', '').strip()) for i, b in enumerate(blocks)
     if b.get('text_level') == HEADING_LVL and b['type'] == 'text']
```

把 `(idx, page_idx, text)` 印出來檢視。這份 list 是後續 step 4-7 的工作面。

### Step 4 — 偵測 body / appendix / index 起點

從 `H` 由前往後掃：

1. 第一個 text 形似章節 title（不含 "Contents" / "Preface" / "Foreword" / "Introduction" / "Acknowledg" / "About the" / 純人名）→ 該 block 的 `page_idx` = **body_start_page**
2. 從後往前掃，第一個 text 開頭為 "Appendix" 或 "APPENDIX"（不論大小寫）→ 該 block 的 `page_idx` = **appendices_start_page**
   - 若全本無 Appendix → appendices_start_page = 該書最後一個正文 chapter 的 page_end + 1
3. 第一個 text 完全等於 "Index" / "INDEX"（不含 page 數尾巴）→ **index_start_page**
4. 第一個 text 完全等於 "Bibliography" / "References" / "BIBLIOGRAPHY" → **bibliography_start_page**；找不到填 null

### Step 5 — 章節 anchor 偵測

對 `H` 應用兩種 pattern（依書本實際擇一）：

**Pattern A — 編號章**：text 匹配 `^(Chapter\s+)?(\d+)([\s.:].*)?$` 或 `^(\d+)\s+[A-Z]`
- group 出來的數字遞增、唯一、無跳號 → 採用此 pattern
- 每個匹配 block：`chapter_title_block_idx = idx`，`title = text 去除編號前綴`

**Pattern B — 純標題章**：text 是 Title Case 短句（≤80 字元），且該頁 `page_idx` 上第一個 `text_level=1` block
- 配合「下一頁起的 body」邏輯，可能需要 `chapter_title_block_idx_secondary`（同章首頁的次塊）

`page_start` = `blocks[chapter_title_block_idx]['page_idx']`
`page_end` = 下一章 `page_start - 1`，最後一章用 `appendices_start_page - 1`
`next_chapter_block_idx` = 下一章 `chapter_title_block_idx`；最後一章用第一個 appendix 的 `chapter_title_block_idx`，無 appendix 用 `N`（content_list 長度）

### Step 6 — Problems 區偵測（per-chapter，3 種結局）

對每章區間 `(cti, next_chapter_block_idx)` 內，**依下列優先序判定**：

**Priority 1 — 章末單一 heading**：text_level=1 block 且 text 完全等於下列任一（去頭尾空白後）：
`"Problems"`、`"PROBLEMS"`、`"Exercises"`、`"EXERCISES"`、`"Problem Set"`、`"PROBLEM SET"`、`"問題"`、`"練習題"`
→ `problems_block_idx: <idx>`（**二分模式**：body=[cti+1, pbi-1], problems=[pbi+1, nci-1]）

**Priority 2 — 章末 heading 帶 chapter 尾巴**：text_level=1 block 且 text 匹配 regex
```
^(Further\s+Problems|End[-\s]of[-\s]Chapter\s+Problems|Chapter\s+\d+\s+Problems|Problems\s+for\s+Chapter\s+\d+)(\s+on\s+Chapter\s+\d+)?\s*$
```
→ `problems_block_idx: <idx>`（**二分模式**）

**Priority 3 — 無 Problems heading 但章內可見 problem_start_re 命中**（題目散落 section 中）
→ `problems_block_idx: null` + **設 top-level `inline_problems: true`**
→ parser 走 inline walker：整章 [cti+1, nci-1] 單流水，遇 problem_start_re 切題、遇 section/subsection heading 結束題目；題目跟正文交錯
→ **絕對不要**把第一個 problem 的 idx 當 pbi（這會把正文 §X.2~§X.5 誤切進 problems 區，造成 body=20、problems body 吞正文）

**Priority 4 — 該章真的無題**：章內全範圍掃 `problem_start_re` 零命中、且無 Problems heading
→ `problems_block_idx: null`（其他章如有 inline，本章保持 null 不影響）

> **判定順序**：P1 → P2 → P3/P4 依序試。P1/P2 命中 → 二分；P1/P2 都不中 → 看 problem_start_re 在章內是否命中：有 → P3（inline）、沒有 → P4（無題）。
> **單一章 inline 即足以觸發** top-level `inline_problems: true`；其他章可繼續走二分。
> **判斷 inline 的另一線索**：手動掃章節前段，若看到 `Problem N.M Lorem ipsum...` 直接接 `N.M.N Subsection Title`（沒有獨立 Problems heading 切開），即 inline。

### Step 6.5 — namespace_by_section 偵測（每節題號重置）

僅當 **章本身判定為 P3 inline** 時才做。判定流程：

1. 對該章 `[cti+1, nci-1]` 範圍掃 `problem_start_re` 命中的前 8-15 個 text block，依出現順序記錄 capture group 的 raw_num
2. 判斷序列性質：
   - **嚴格遞增整數**（`1, 2, 3, ..., N` 或 `N.1, N.2, ..., N.k`）→ namespace 不需要
   - **出現重複、或從某個位置突然往回 reset（如 `1, 2, 3, 1, 2, ...` 或 `5, 6, 7, 1, 2`）** → 每節題號重置 → 設 `problem_num_namespace_by_section: true`
   - **跨多個 section 但全部單調**（如 `1, 2, 3, ..., 50` 不 reset）→ 不需要 namespace（Griffiths-style 連續編號）

3. 若整本至少一章需要 namespace → top-level `problem_num_namespace_by_section: true`；只在 inline 模式 + 純流水題號（`^(\d+)\.\s+`）下生效

> **範例**：Strang 每節「Problem Set N.M」內題號從 1 重編 → 跨節掃會看到 `1, 2, 3, 4, 5, 1, 2, 3, ...` → 必須 namespace=true
> Griffiths inline 但 `Problem 1.1, 1.2, ..., 1.55` 連續編號 → 不需要 namespace

### Step 7 — heading / problem / equation regex 推斷

**heading regex**：先觀察 body 內（chapter 1 中段任一頁）的 `text_level=1` block text：

- 若多數匹配 `^\d+\.\d+\s+\S` → 編號型 N.M
  - `section_re: '^(\d+\.\d+)\s+(.+)$'`
  - `subsection_re: '^(\d+\.\d+\.\d+)\s+(.+)$'`
- 若多數全大寫（≥3 字元 ALL CAPS）→ 純文字 ALL CAPS 型
  - `section_re: '^()([A-Z][A-Z][A-Z\- ’‘'',.&/:]*[A-Z.)])$'`
  - `subsection_re: '^()([A-Z][a-z]*(?:[:\s\-’‘'',][A-Za-z][A-Za-z]*)+)$'`

**硬規則 — section_re 不准允許空 title**（thomas_calculus 慘案教訓）：
N.M 型 alternation 後段**必須**強制至少 1 個 non-whitespace title 字元（用 `\s+(.+)$` 或 lookahead `(?=[\s ]+\S)`），**不可**寫成 `\s*(.*)$`。
MinerU 會把每頁 header/footer 的 section 編號（如 `'4.1 '`、`'4.2 '`）獨立輸出為 `text_level=1` block；若允許空 title，這些頁眉編號會被當合法 section heading，UI 上每節重複出現一次空標題項。

若需多 alternation（per-section Problem Set 邊界、Chapter Practice/Review 等）：
- N.M alternation **仍須**強制 title 非空：`(?:\d+\.\d+(?=[\s ]+\S))`
- 其他 alternation（identifier 即整字串、無後續 title）才可允許 group 2 為空：
  ```
  ^((?:\d+\.\d+(?=[\s ]+\S))|(?:Exercises?\s+\d+\.\d+)|(?:Chapter\s*\d*\s*(?:Practice|...)))[\s ]*(.*)$
  ```

**problem regex**：必須恰好 **1 個 capture group**（題號）。在第一個 `problems_block_idx` 後找 3-5 個 `type=text` block，看題號實際 prefix：

- `"1.1 ..."` / `"1.10 ..."` → `'^(\d+\.\d+)\s+'`、`problem_chapter_must_match: true`
- `"1. ..."` / `"10. ..."`（純流水）→ `'^(\d+)\.\s+'`、`problem_chapter_must_match: false`
- `"Problem 1.1 ..."` → `'^Problem\s+(\d+\.\d+)\s+'`、`problem_chapter_must_match: true`
- `"P.2-1 ..."` / `"2-1. ..."`（**hyphen 分隔的 N-M**）→ `'^P\.(\d+-\d+)\s+'`、`problem_chapter_must_match: false`
- `"Exercise X.Y ..."` → 類似改寫，1 capture group

**`problem_chapter_must_match` 判定規則**（不協商）：
- 題號 capture 內含 `.` 且 `split('.')[0]` 可 `int()` → `true`
- 題號 capture 內含 `-` 或為純流水數字 → `false`（parser 用 `int(num.split('.')[0])` 對 hyphen/單一數字會 ValueError，需跳過驗證）

**equation regex**：掃 `type == 'equation'` block 中的 `text`，看 `\tag{...}` 內容：

- 整數 `\tag{40}` → `'\\tag\s*\{([0-9]+[a-z]?)\}'`
- `\tag{1.2}` 或 `\tag{1.2a}` → `'\\tag\{([0-9]+\.[0-9]+[a-z]?)\}'`
- 沒 `\tag` 全本 → 保留前者作為 default（不誤匹配無害）

**equation_strip_dollar**：掃前 5 個 equation block，若 text 以 `$$\n` 開頭、`\n$$` 結尾 → true。所有 MinerU v4 輸出皆 true，幾乎不會是 false。

### Step 7.5 — Smoke feedback loop（必跑，至多 3 次 iterate）

完成 yaml 與 §5 schema validator 後，**必須**跑 parser + smoke 並依結果回頭修：

```bash
SLUG=<slug>
uv run --with pyyaml python -m book_pipeline.parser $SLUG  # 產出 parsed/chNN.json
uv run --with pyyaml python -m book_pipeline.smoke $SLUG   # 啟發式 anomaly 檢測
```

`smoke` 退出碼：`0` 全綠或僅 warning、`1` 有 critical anomaly。對應修法：

| 啟發式 | 含意 | 修 yaml |
|---|---|---|
| **H1** ch body < 20 + problems > 30 | inline 模式漏設 | 該章 `problems_block_idx: null`、top-level `inline_problems: true` |
| **H2** ch 內 problem.num 重複 | 每節題號 reset | top-level `problem_num_namespace_by_section: true`（僅 inline + 純流水） |
| **H3** ch 全部 problem body=[] | OCR 大量漏 / parser 沒讀 list_items | 通常非 audit 可修；列進 `_audit.md` |
| **H4** appendix body > 1500 | 吞 Index/Bib | 補 `index_start_page` 或 `bibliography_start_page` |
| **H5** 鄰章 body 比 > 5x | anchor 飄移可疑 | 檢查可疑章的 `chapter_title_block_idx` |

**iterate 規則**：
1. 第 1 次跑 smoke：若 critical=0 → 進 Step 8、結束
2. critical > 0：依表修 yaml → 重跑 §5 schema validator → 重跑 parser → 重跑 smoke
3. 至多 3 輪。第 3 輪仍有 critical 但你已嘗試合理修法 → 接受殘留，把每個未修 critical 列進 `_audit.md` 並加 ⚠ 說明原因（OCR 邊界、跨書罕見 case 等）

**禁止 cheat**：不要為了讓 smoke 綠而把可疑章設成 P4 無題或把 problem_start_re 改鬆。修法必須對應真實成因。

### Step 8 — OCR 空 list 偵測（**只報告，不塞 known_missing_problems**）

對每章 problems 區掃 `type == 'list'` 且 **`text == ''` 且 `list_items` 為空（或全為空字串）** 的 block — 這是 MinerU OCR 把子題內容真正吃掉的特徵。

> 注意：`text == ''` 但 `list_items` 非空的 block 是**正常的** `(a)(b)(c)` 子題（parser 會讀 `list_items`），不該報。

**正確處理**：
- **寫進 `_audit.md`** 的「OCR 空洞」段：每個空 list block 一行，含 `(chapter, idx, page, 前一塊文字摘要)`。給人工後續判斷
- **不寫進 `known_missing_problems`**：該欄位是「人工確認的漏題編號」（schema 為 `[{chapter:int, nums:[str]}]`），自動偵測拿不到可靠題號不該塞
- **`known_missing_problems: []`** 是預設正確值（直到有人工驗證過後才填）

這樣後續 parser 跑出 body 為空的 Problem，能對照 `_audit.md` 的 OCR 空洞表確認是 OCR 漏抓而非 parser bug。

## 4. 產出與路徑

Sub-agent 必須產出兩個檔案：

1. `book_pipeline/mineru_data/<slug>/extract_rules.yaml` — §2 schema 嚴格符合
2. `book_pipeline/mineru_data/<slug>/_audit.md` — 過程記錄：
   - block 統計
   - 章節 anchor 偵測表（章號、title、anchor idx、page 範圍）
   - heading / problem / equation regex 的依據（觀察到的 sample）
   - 不確定的決策（標 ⚠）

## 5. 主對話 Validate（commit 前必做）

sub-agent 回報後，主對話跑下列 self-check（schema 規則收攏於 `book_pipeline/validate_rules.py`，改規則改該 module，本檔不內聯）：

```bash
SLUG=<slug>
uv run --with pyyaml python -m book_pipeline.validate_rules $SLUG
# 退出 0=合規、1=不合規（逐條列違規）。不合規不准 commit。
```

驗證通過後跑 parser + smoke + metadata normalize（按 §3 Step 7.5 規則 iterate）：

```bash
uv run --with pyyaml python -m book_pipeline.parser $SLUG
uv run --with pyyaml python -m book_pipeline.smoke $SLUG
uv run --with pyyaml python -m book_pipeline.normalize_metadata --fix   # 自動修 edition/author 格式 + 驗 subject ∈ schema
```

成功會在 `book_pipeline/mineru_data/$SLUG/parsed/` 產出 `book.json` + `chNN.json` + `_smoke.md`。
- smoke critical=0 才算完成；critical>0 須回 §3 Step 7.5 對應修法 iterate
- normalize_metadata 退出 0 才算完成；若報「subject 不在 schema」**先補 `book_pipeline/metadata_schema.yaml` subjects list 再重跑**，不准瞎寫

**抽封面（必跑，本機才有 raw PDF）**：

```bash
uv run --with pymupdf python -m book_pipeline.extract_cover $SLUG
```

`extract_cover.py` 會 auto-detect `raw_pdfs/` 內對應 PDF（stem 規範化比對 + 模糊匹配），新書一般免改 map。若報「找不到對應 PDF」→ 補 `SLUG_TO_PDF` alias map 再重跑；或直接 `… extract_cover $SLUG raw_pdfs/<檔名>.pdf` 明確指定。

封面存 `book_pipeline/mineru_data/$SLUG/cover.jpg`（入 git）。**`cover.jpg` 不存在 = audit 未完成、不准 commit。**

## 6. 硬規則

- **不要動** `.env`、`book_pipeline/mineru_ingest.py`、`book_pipeline/parser.py`
- Sub-agent **不可**讀別本書（如 `sakurai_mqm3` / `kittel_thermal`）的 `extract_rules.yaml` 作參考 — 全部依本檔 §2 schema 與 §3 方法產出
- 主對話收回報後**必跑** §5 validate，不合規不准 commit
- `_audit.md` 是過程紀錄，**不入 git**（gitignore 已含 `parsed/_*.md`，不過 `_audit.md` 在上層；視情況加白名單或讓他留本機）

## 7. 派發流程

主對話（由 `/book-pipeline` dashboard 對 `1 待audit` 的書派發）：
1. 派 general-purpose sub-agent，prompt 包含本檔 §2 + §3 + §4 全文 + slug
2. agent 完成 → 主對話跑 §5 validate
3. 驗證通過 → smoke test parser（§5）
4. 全綠 + cover.jpg 存在 → `git add extract_rules.yaml cover.jpg` + `git commit -m "audit-book: <slug>"` + push
