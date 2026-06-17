# Math Sweep — 跨書清數學式渲染殘餘（corpus-level）

修「reader 的 MathJax 引擎渲不出來」的式子（黃底 parse error / 紅字未定義巨集）。**這是跨全書
的 reduce job**，不是單本流程：數學壞法跨書成 pattern，最高槓桿（加一個巨集 / 一條 normalize 規則
清掉全 corpus 同類）**只有跨書視野做得出來**。書已先上站，本流程是上站後的批次收尾。

與 `catalog-audit.md` 的關係：catalog 管「圖表編號/ref 結構」；本流程管「公式 LaTeX 渲染正確性」。
兩者各自獨立的 override 機制（catalog_overrides / math_overrides），同一套 selector 文法、同一套
「失配即 skip 不 raise」replay 哲學。

> **本檔讀者 = autonomous math sweep agent**。只講你的職責：怎麼把式子修對、可泛化者怎麼交出去。
> 派發門檻、規則升級、採納決策等 **owner 機制不在此檔**（正本在 `proposals.py` docstring 與
> `pipeline_tick.py` 註解）。你不必、也不該去讀那些。

## 目錄
1. 環境前提與工作流程
2. 殘餘類別總表
3. **泛化 vs one-off 決策（最高槓桿，先讀）**
4. override spec 格式
5. 逐類別修復策略
6. 規則升級（owner 範圍——僅指路）
7. 對照 PDF 重建
8. accepted 殘留
9. 硬規則
10. 你的收斂任務

---

## 1. 環境前提與工作流程

驗證器是 ground-truth：`render_check.js`（mathjax-full，移除 noerrors/noundefined 讓錯誤現形）+
共享巨集表 `book_pipeline/math_macros.json`。需 `node_modules`（首次 `npm --prefix book_pipeline install`）。

```bash
# (1) 取跨書聚合殘餘（不重跑 render，讀既有 _math_report.json）→ pattern-mining 入口
uv run python -m book_pipeline.math_validate --aggregate --json

# (2) 高頻可泛化者 → §3 決策：用 `proposals propose` 交出去（你不升級核心碼，那是 owner 的事）
# (3) one-off → 寫 book_pipeline/math_overrides/<slug>.json（§4）
uv run python -m book_pipeline.apply_math_overrides <slug>     # 套用，自動備份到 parsed/_override_backups/
uv run python -m book_pipeline.math_validate <slug>           # 重驗，確認該書殘餘下降
```

`--aggregate` 的 `groups` 已按「完全相同 (tex,display) 跨書合併、總 occ 排序」：**高頻在前 = 先泛化**，
尾端低頻交 one-off override。每組附各書 `slug`/`occ`/`targets`（`{chunk,selector}`，直接抄進 override）。

工作流順序鐵則：泛化（清大宗）→ 再 one-off（清長尾）。先做泛化才不會手寫一堆本可一條規則解決的 override。

## 2. 殘餘類別總表

`math_validate.categorize` 的分類（finding.category）：

| category | 意義 | 典型修法 |
|---|---|---|
| `undefined_macro` | 未定義控制序列（detail = 巨集名，如 `\bgroup`） | 真巨集 → 提案加 math_macros.json；OCR 黏字偽巨集（`\Nu`/`\muA`）→ override 改回正確 token |
| `double_script` | `a^{x}^{y}` / `a_{x}_{y}` 雙上下標硬錯 | 多數已被 Layer 1 R2/R3 清；殘餘是巢狀形 → override 手合或提案放寬規則 |
| `math_mode` | `^`/`_`/巨集出現在文字模式、`$` 未正確閉合 | override 包 `$...$` 切回數學 / 補閉合 |
| `missing_brace` | 缺 `{`/`}`、引數不足 | override 補括號 |
| `left_right` | `\left`/`\right` 不配對、相角 `\left/` 亂碼 | override 配對或改 `\angle` |
| `alignment` | `&` 對齊環境錯位 | override 修對齊或拆環境 |
| `other` | 其餘（截斷、雜訊） | §7 對照 PDF 重建 / §8 accept |

## 3. 泛化優先決策（最高槓桿，先讀）

對每個高頻 group 問：**這是「定義缺失/系統性 OCR pattern」還是「一次性亂碼」？**

- **可泛化**（同一 tex 跨多書反覆出現、或同一壞法成形）：
  - 真數學符號的未定義巨集（如某符號全 corpus 都缺定義）→ **提案加 math_macros.json**（Layer 0），一次復活全 corpus。
  - 機械可判的壞 pattern（如某種 OCR 固定錯位）→ **提案加 math_normalize.py 規則**（Layer 1），parse 時自動清。
  - ⚠ **故意不收 OCR 黏字偽巨集**（`\Nu`/`\muA`/`\cdotE`…）：定義它們會掩蓋真錯。這類走 one-off override 改回正確 token。
- **一次性**（單書、源頭 OCR 把該處讀爛/截斷）→ per-slug override（§4），或 §7 重建、§8 accept。

**你（agent）的分流**：泛化者**用 CLI 提交**（不手寫檔、不改核心碼）：
```bash
uv run python -m book_pipeline.proposals propose --domain math --type <macro|normalize-rule> \
  --source math_sweep --title '<簡述>' --detect '\token' --evidence '<壞樣本+err+哪些書>' \
  --proposal '<建議規則/巨集>' --risk '<誤改風險>'
```
**絕不**自行改 math_macros.json / math_normalize.py（核心碼，交人工升級）。one-off override 照寫照套（§4）。
提案完當沒這回事繼續清下一類——升不升級、何時升，不是你的事。

## 4. override spec 格式

`book_pipeline/math_overrides/<slug>.json`：`{"overrides": [ … ]}`。chunk/selector/field **直接抄** `--aggregate`
finding 的 `targets[]`（已從 locator 橋接好）：selector = `body[N]` / `problem:NUM:field[N]` / `title`；
field = `tex`（eq block → 用 `fix_eq_tex`）或 `md`/`caption`/`footnote`/`title`（inline → 用 `fix_inline_math`）。

### 4.1 `fix_eq_tex` — 換 eq block 的整條 tex
```json
{
  "id": "<slug>-eqfix-ch03-body42",
  "action": "fix_eq_tex",
  "chunk": "ch03",
  "selector": "body[42]",
  "expect": "<目前 parsed 裡那條壞 tex（精確）>",
  "new": "<修好的 tex>",
  "reason": "…"
}
```
`expect` **必填**（舊 tex 精確 guard）；目前 tex ≠ expect → **skip-drift 不套**（源頭重 OCR 改過了）。
目前 tex 已等於 new → noop（冪等）。block 非 eq → skip。

### 4.2 `fix_inline_math` — 換 md/caption/footnote/title 內一段數學子字串
```json
{
  "id": "<slug>-inlinefix-ch02-body7",
  "action": "fix_inline_math",
  "chunk": "ch02",
  "selector": "body[7]",
  "field": "md",
  "anchor": "<overlay_anchor({field: 該欄當前全文}) 的 8-hex>",
  "old": "<該欄內要替換的精確子字串（含 $ 分隔符）>",
  "new": "<替換後>",
  "reason": "…"
}
```
`field` = 抄 `targets[].field`，**別自己猜**：填錯欄 → anchor key 就錯 → 必 skip-drift。
`old` 必須**從當前 parsed 欄位實抄**精確子字串（含 `$` 分隔符）：**anchor 容忍空白、`old` 不容忍**，用 report 的 inner
重組會對不上 → `old` 找不到 → noop 沒修到。
`anchor` = 該欄當前全文內容指紋，**key 必須等於 `field`**（title selector 時 key 用 `'title'`）：
`from book_pipeline.translate import overlay_anchor; overlay_anchor({<field>: <該欄全文>})`。
欄位漂移 → skip。`old`==`new` 或（`old` 找不到但 `new` 在）→ noop；都不在 → skip-drift。
`title` 用 `selector:"title"` + `field:"title"`。

**guard 哲學**：失配一律 skip（不 raise）→ 對齊 corpus overlay；源頭一漂移舊修復自動停用、其餘照套。
冪等可重播：parser 重建後重跑 apply，已套用/已漂移者自動跳過。

### 4.3 一欄多處 / 重複 problem num（殘餘清不掉的兩個坑，必讀）
- **同欄同式多次**（occ>1，一欄裡同一壞式 ≥2 次）：預設只換首處、其餘清不掉，且換完 anchor 變、第二筆同 anchor
  override 必 skip。→ 一筆 `fix_inline_math` 加 `"all": true` 一次換光該欄全部同式。
- **重複 problem num**（同章兩題同 num）：`targets[].selector` 只給 `problem:NUM:...`（指第一個）。第二題改用
  `problem:NUM#1:...`（`#OCC`，OCC 從 0 起算，回讀 parsed 數第幾個）。
- 這兩類**不是** §8「不可修」——是機制要對症下藥，別誤判成 accept。

## 5. 逐類別修復策略

- **undefined_macro（偽巨集）**：`\muA`→`\mu A`、`\cdotE`→`\cdot E`、`\Nu`→`N`/`\nu`（看上下文）。`fix_eq_tex`/`fix_inline_math`。
- **undefined_macro（真符號缺定義）**：`proposals propose`（§3），不自己改 macros。
- **double_script 巢狀**：`a^{x^{y}}^{z}` 之類 Layer 1 不敢碰的 → 手判正確結構，`fix_eq_tex`。
- **math_mode**：文字模式漏 `$` → 包回；或 `\text{…^…}` → 把上下標移出 `\text`。
- **missing_brace / left_right / alignment**：補括號、配 `\left\right`、修 `&`。逐條對照原意。
- 任何「看不懂原意」的 → §7 翻 PDF，不是 §7 能救的 → §8 accept。**絕不亂猜塞一個能渲染但語意錯的式子**。

## 6. 規則升級（owner 範圍——僅指路）

把提案升進 Layer 0/1（macros/normalize）、過 fixture/回歸閘、記狀態，**是 owner 的事，不在你職責**。
你只要 `proposals propose` 把可泛化者交出去（§3）即可，**不要**自己改核心碼或追蹤升級。
（採納決策樹/強制閘正本在 `proposals.py` docstring，那是 owner 看的。）

## 7. 對照 PDF 重建

殘餘看不懂原意 → 開 `raw_pdfs/<file>.pdf`（`slug_map.json` 查檔名）對照該章節，照原書重打正確 LaTeX 寫 override。
source 缺 PDF（雲端機）→ 不重建，§8 accept。

## 8. accepted 殘留（不要無限糾纏）

源頭 OCR 把式子讀成不可逆亂碼/截斷、無 PDF 可重建 → **留著即可**。daemon `do_math_sweep` 會把殘餘記進
state（track-only，**不 gate deploy**，書照常在站）。`/dev` 數學殘餘區會顯示「哪些書殘多、是否已過 sweep」。
不要為了清零硬塞錯式子（§5 末）。

## 9. 硬規則

- **絕不手改 `parsed/*.json`**：一律經 override（git 追蹤、可 review、parser 重建後可重播）。手改下次 parse 即丟。
- **絕不**改 `math_macros.json` / `math_normalize.py`（autonomous 模式）；互動模式照 §6。
- **絕不** echo MinerU/zlib token。
- override 失配是**正常**（源頭漂移）→ skip，不是錯誤；不要為了讓它套用而放寬 guard。
- 一本錯不要停，記下續下一本，最後彙整。

## 10. 你的收斂任務

**目標：把 corpus 數學殘餘結構化收斂到趨近零**——不是跑一輪就交差。**反覆** aggregate → 分類修
→ 重 validate，直到殘餘**停止下降**（只剩你權限外的泛化案、或源頭不可逆的硬 edge §8）才收工。
- 可泛化 → `proposals propose`（§3）交出去；其餘 → one-off override（§4）；硬 edge → §8 留著。
- 寫完 override 自跑 `apply_math_overrides <slug>` + `math_validate <slug>` 確認該書下降；收工前再
  `--aggregate` 確認總殘餘已收斂。
- **部署不用你管**：daemon 會收尾確定性 re-apply 全部 override + 重烤改動的書上站。
