# Math Sweep — 全 LLM 閉環，把 corpus 數學渲染殘餘收斂到真 0

修「reader 的 MathJax 引擎渲不出來」的式子（黃底 parse error / 紅字未定義巨集）。**跨全書的 reduce
job**：數學壞法跨書成 pattern，最高槓桿（一條 normalize 規則 / 一個 macro 清掉全 corpus 同類）只有
跨書視野做得出來。書已先上站，本流程是上站後的批次收尾。

> **本檔讀者 = math sweep agent。你是全 LLM 閉環裡唯一的決策者——人不在迴圈裡。** 你自己分類、
> 寫 normalize 規則/macro/override、過真實數據閘、self-resolve、self-commit。安全靠**真實數據閘 +
> git 可回退**取代人審：錯了只會被閘擋下或被 owner 一個 `git revert` 推平，所以**放手做、做到真 0**。
> proposals 系統機制（schema/CLI）正本在 `proposals.py` docstring；你只需照本檔操作。

## 目錄
1. 環境前提與閉環工作流程
2. 眼睛：`--cluster`（結構簇 + 診斷 token）
3. 選修法決策樹（規則 / macro / override / accept）
4. 寫 Layer 1 normalize 規則 / Layer 0 macro
5. 真實數據閘（採用準則，取代人審）
6. override spec 格式（含 make-override 自動填欄）
7. 逐類別修復策略
8. 對照 PDF 重建
9. §8 accept（源文已毀，極少用）
10. 決策日誌 self-resolve + self-commit
11. 硬規則
12. 收斂任務（真 0）

---

## 1. 環境前提與閉環工作流程

驗證器是 ground-truth：`render_check.js`（mathjax-full，移除 noerrors/noundefined 讓錯誤現形）+
共享巨集表 `book_pipeline/math_macros.json`。需 `node_modules`（首次 `npm --prefix book_pipeline install`）。

閉環一圈（反覆做到 residual_unaccepted=0）：
```bash
# (1) 眼睛：看結構簇 + 診斷 token（§2）
uv run python -m book_pipeline.math_validate --cluster --json
# (2) 選修法（§3）：跨書同病灶 → 寫規則/macro（§4）；單書/edge/源毀 → override（§6）
# (3) 真實數據閘（§5）：全 corpus 重 parse/套 override/重渲染，判嚴格淨降且無書上升
uv run python -m book_pipeline.proposals gate            # 不帶 slug = 全 corpus（規則是全域的）
# (4) self-resolve 記決策日誌（§10）→ commit + 重烤上站
```

## 2. 眼睛：`--cluster`（結構簇 + 診斷 token）

`--cluster` 給你兩種跨書視野（比舊 `--aggregate` 的「完全相同 tex」強——那會把同病灶不同內文打散成
occ≈1，泛化視野消失）：

- **`clusters`**：依「結構骨架」(`skeleton`：留控制序列 + `^_{}&`，內容塌縮成 `·`) 跨書聚類。
  `a^{x}^{y}` 與 `p^{m}^{n}` 同簇。每簇附 `total_occ`/`book_count`/`uniques`/3 條真實 `examples`（含
  `targets`，直接寫 override）。高 `total_occ`＋多 `book_count` = 最該寫一條規則一次清。
- **`token_signals`**：壞式中每個控制序列的 `book_count`/`occ` 直方圖。`\kern`/`\vphantom`/`\bgroup`
  跨多書 = 高槓桿規則標的；`\frac`/`\left` 普遍高頻是雜訊（你自己判，工具不臆測）。簇會碎成多形狀，
  唯 token 視圖量得出「一條規則涵蓋幾本」。

由高 occ／多書往下做。`--aggregate`（exact-tex）仍在，但 `--cluster` 是你的主視野。

## 3. 選修法決策樹（你是決策者）

對每個高 occ 簇／token 問「最小、最安全、能淨降最多的修法是哪個」：

1. **真符號缺定義**（某符號全 corpus 都缺、單一無歧義展開、跨多書）→ **Layer 0 macro**（§4.2）。
2. **機械 OCR pattern**（確定性字串轉換、語意保持，如 `\ifmmode\times\else…\fi`→`\times`、MathType
   `\mathord{\left/\vphantom..}\right.\kern-\delimiterspace`→`/`）→ **Layer 1 normalize 規則**（§4.1）。
3. **OCR 黏字偽巨集**（`\Nu`/`\muA`/`\cdotE`…，token 歧義或上下文相依）→ **per-book override** 改回正確
   token（§6）；**絕不**收進 macros（會掩蓋真錯，且 `test_no_ocr_glue_pseudomacros` 會擋）。
4. **單書特異 / 規則的 edge case / 源文已毀** → per-book override（§6）；連 override 成可渲染都做不到的
   極少數 → §9 accept。

規則 vs override 的界線：**能跨書、確定性、語意保持** → 規則（一次清全 corpus）；否則 override。不確定就
先寫規則跑閘看淨降——閘會告訴你它誤傷了誰（§5），對誤傷補 override 即可。

## 4. 寫 Layer 1 normalize 規則 / Layer 0 macro

**你被授權改核心碼**（`math_normalize.py` / `math_macros.json`）。每個變更**強制**過下列閘，缺一不可：

### 4.1 Layer 1 規則（`book_pipeline/math_normalize.py`）
- 加一個純函式進 `_TEX_RULES`（有序套用）。規則必須**冪等** `f(f(x))==f(x)`、**對正確式 no-op**
  （只動 OCR 壞 token；正確式不含那些 token，自然不碰）。保守：delete-only / 精確 pattern，寧可漏修不可誤改。
- before/after fixture 寫進 `test_math_normalize.py`（`TEX_CASES` 真實壞樣本→期望、`NOOP_CASES` 正確式不動）。
- 跑 `uv run python -m book_pipeline.test_math_normalize`（綠）。

### 4.2 Layer 0 macro（`book_pipeline/math_macros.json`）
- 加一條 `\macro: 展開`。fixture 進 `test_math_macros.py`；對照 `test_no_ocr_glue_pseudomacros` 偽巨集禁收清單
  （`\Nu`/`\muA`… 不准進）。改完**必跑** `uv run python -m build.gen_macros`（同步 reader 端）。
- 跑 `uv run python -m book_pipeline.test_math_macros`（綠）。

兩者寫完都必過 §5 真實數據閘。**閘沒過就 `git checkout -- <檔>` 退掉該變更**，改走 override 或換做法。

## 5. 真實數據閘（採用準則，取代人審）

**只看真實數據**：把變更套到全 corpus 真的重 parse + 重渲染，看結果——不靠「應該安全」的推論。
```bash
uv run python -m book_pipeline.proposals gate     # 不帶 slug = 全 corpus（規則是全域，務必全跑才驗得出他書誤傷）
```
**採用準則（鐵律）**：corpus 殘餘**嚴格淨降（Δ<0）且無任一書殘餘上升**才算採用。

- **任何規則必有 edge case**：閘會列出「好→壞 collateral」（規則誤傷的、本來正確的式子）。
  **不要丟規則**——對那幾條 collateral 補 per-book override（§6）把它們蓋回正確，再重跑閘，直到淨降且無上升。
- Δ≥0（沒淨改善）或補完仍有書上升 → 此變更不採用，`git checkout` 退掉，換做法。
- 子集 slug 只判該範圍 → 規則/macro 一律**不帶 slug** 跑全 corpus。

閘内部：`backfill_math` 全 corpus 重 parse（折進 Layer 1）+ 重套 override + 重渲染，做位置級 before/after
diff（fixed 壞→好、collateral 好→壞、still_bad）。這就是「對正確式 no-op」的機器強制：誤傷正確式 = 該書
殘餘上升 = 閘擋。

## 6. override spec 格式（`book_pipeline/math_overrides/<slug>.json`）

**自動填欄**（推薦）：從一條 finding 產 override，只剩 `new` 要你判：
```bash
uv run python -m book_pipeline.apply_math_overrides make-override \
  --slug <s> --index <i> --new '<正確 tex（inline 給裸 inner、eq 給裸 tex）>'
# 印出 override JSON 條目 → 併進 math_overrides/<slug>.json 的 "overrides" 陣列
```
`--index` = `math_validate <slug> --json` 的 `findings[]` 索引。它自動填 id/action/chunk/selector/field/
expect/anchor/old（讀 live 欄位精確定位）。**eq 的 `expect`、inline 的 `old`+`anchor` 都不必手填**。

手寫格式（make-override 不適用時）：`{"overrides": [ … ]}`，chunk/selector/field 抄 finding 的 `targets[]`。
- **`fix_eq_tex`**（field=`tex`）：`{id, action, chunk, selector, expect:<舊 tex 精確 guard>, new}`。
- **`fix_inline_math`**（field=`md`/`caption`/`footnote`/`title`）：`{id, action, chunk, selector, field,
  anchor:<overlay_anchor({field:該欄全文}) 8-hex>, old:<含 $ 的精確子字串>, new}`。`field` 抄 `targets[].field`
  別自己猜；`old` 從當前 parsed 實抄（anchor 容忍空白、old 不容忍）。

套用＋重驗：
```bash
uv run python -m book_pipeline.apply_math_overrides <slug>    # 自動備份 parsed/_override_backups/
uv run python -m book_pipeline.math_validate <slug>          # 確認該書殘餘下降
```

**guard 哲學**：失配一律 skip（不 raise）→ 源頭漂移舊修復自動停用、其餘照套；冪等可重播。

### 6.1 一欄多處 / 重複 problem num（清不掉的兩個坑）
- **同欄同式多次**（occ>1）：一筆 `fix_inline_math` 加 `"all": true` 一次換光（預設只換首處、其餘清不掉）。
- **重複 problem num**（同章兩題同 num）：第二題 selector 用 `problem:NUM#1:...`（`#OCC`，從 0 起算）。
- 這兩類**不是** §9 不可修——對症下藥即可。

## 7. 逐類別修復策略（finding.category）

| category | 意義 | 修法 |
|---|---|---|
| `undefined_macro` | 未定義控制序列（detail=巨集名） | 真符號缺定義→§4.2 macro；偽巨集（`\Nu`→`N`/`\nu`、`\muA`→`\mu A`）→ override |
| `double_script` | `a^{x}^{y}` 雙上下標 | 多數已被 R2/R3 清；殘餘巢狀形 → 規則放寬（§4.1）或 override 手合 |
| `math_mode` | `^`/`_`/巨集在文字模式、`$` 未閉合 | override 包 `$...$` / 把上下標移出 `\text` |
| `missing_brace` | 缺 `{`/`}`、引數不足 | override 補括號 |
| `left_right` | `\left`/`\right` 不配對、相角 `\left/` | 跨書同形（如 MathType 相量）→ 規則；單書 → override 配對或改 `\angle` |
| `alignment` | `&` 對齊環境錯位 | override 修對齊或拆環境 |
| `other` | 截斷/雜訊 | §8 翻 PDF / §9 accept |

任何「看不懂原意」的 → §8 翻 PDF；救不了 → §9 accept。**絕不亂猜塞一個能渲染但語意錯的式子**。

## 8. 對照 PDF 重建

殘餘看不懂原意 → 開 `raw_pdfs/<file>.pdf`（`slug_map.json` 查檔名）對照章節，照原書重打正確 LaTeX 寫
override。雲端機缺 PDF → 不重建，§9 accept。

## 9. §8 accept（源文已毀，極少用）

收斂目標是**真 0**：連源文已毀的也盡量 override 成「至少渲染得出來」。唯有**連 override 成可渲染都做不到**
（OCR 讀成不可逆亂碼、無 PDF 可重建）的極少數，才 accept：
```bash
uv run python -m book_pipeline.devctl math-accept --slug <s> --occ <n> --reason '<為何連可渲染都做不到>'
```
accept 的 occ 不計入 `residual_unaccepted`（收斂終態判據）。**要留證**（reason 供 owner 稽核）；別為清零硬塞語意錯的式子。

## 10. 決策日誌 self-resolve + self-commit

proposals 是你的**決策日誌**（不是等人核准的佇列）：
```bash
# 泛化修法（規則/macro）開一筆
uv run python -m book_pipeline.proposals propose --domain math --type <macro|normalize-rule> \
  --source math_sweep --title '<簡述>' --detect '\token' --evidence '<簇證據：occ/書數/樣本>' \
  --proposal '<規則/macro>' --risk '<誤改風險>'
# 閘過後自記決議（稽核軌跡）
uv run python -m book_pipeline.proposals resolve <id> --status accepted --resolution '<規則名> 淨降 N occ'
```
閘綠的變更**自己 commit + 重烤受影響書上站**：
```bash
uv run python -m build.build_all <受影響 slug…>     # 規則影響多書 → 全部重烤
git add -A && git commit -m 'fix(math): <規則名> — 淨降 N occ / M 書'
```
（daemon 的 `do_math_sweep` 也會確定性收尾重烤殘餘下降的書，但你自己 commit 才留下決策軌跡。）

## 11. 硬規則

- **絕不手改 `parsed/*.json`**：一律經 override（git 追蹤、可 review、可重播）。手改下次 parse 即丟。
- 改核心碼（`math_normalize.py`/`math_macros.json`）**必過 §4 測試 + §5 真實數據閘**；沒過就 `git checkout` 退掉。
- **絕不** echo MinerU/zlib token。
- override 失配是**正常**（源頭漂移）→ skip，不是錯誤；不要為了讓它套用而放寬 guard。
- 一本錯不要停，記下續下一本，最後彙整。

## 12. 收斂任務（真 0）

**目標：residual_unaccepted = 0。** 反覆 `--cluster` → 選修法 → 真實數據閘 → self-commit，直到 0 或
剩餘全是 §9 accept（源文已毀的極少數）。每圈都讓 corpus 殘餘**嚴格下降**（新規則一定要比舊的好）。
- 跨書同病灶 → 規則/macro（§4）一次清；edge case 誤傷 → override 補；源文已毀 → §9 accept。
- 收工前 `--cluster` 確認 residual_unaccepted 已到 0（或只剩已 accept）。
