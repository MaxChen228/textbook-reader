# Math Sweep — 逐條 override 待辦，把 corpus 數學渲染殘餘收斂到真 0

修「reader 的 MathJax 引擎渲不出來」的式子（黃底 parse error / 紅字未定義巨集）。書已先上站，本流程是
上站後的批次收尾：**跨全書殘餘是一份可列舉待辦清單**，每條一個壞式，你只做不可化約判斷「這條壞 tex 該長
什麼樣」，逐條改寫成正確 LaTeX，用 override 落地（不污染原始 parsed）。驗證是 **O(單式)<1ms 的單條
render**，不跑全 corpus gate。

> **本檔讀者 = math sweep agent。你是唯一決策者，人不在迴圈裡。** 安全靠**單式 render 驗證 + git 可
> 回退**：每條改寫都先單條渲染過才落地，錯了 owner 一個 `git revert` 推平。放手做、做到真 0。

## 工作流程（反覆做到 residual_unaccepted=0）

驗證器是 ground-truth：`render_check.js`（mathjax-full，移除 noerrors/noundefined 讓錯誤現形）+ 共享
巨集表 `math_macros.json`。需 `node_modules`（首次 `npm --prefix book_pipeline install`）。

```bash
# (1) 讀待辦：全 corpus render 殘餘，每條含 gid / tex（壞式）/ err（編譯錯誤）/ occ
uv run python -m book_pipeline.math_sweep list --json

# (2) 改寫——主力＝批量打自架 LLM 逐條改寫（render 守門 + retry，最省、最快清空大量單發式）
uv run python -m book_pipeline.math_sweep batch          # 預設全 corpus；--book/--category/--limit 可窄化
#     個別難條、batch 留下的零頭 → 單條手改（render 驗證過才落地，沒過印 err 讓你重寫）
uv run python -m book_pipeline.math_sweep fix --gid <gid> --new '<正確 tex>'

# (3) 重烤受影響書上站（live 讀 data/，不重烤 reader 看不到修復）
uv run python -m build.build_all <受影響 slug…>
```

`batch`/`fix` 都自動：反查 finding → 單條 render 驗證 `new` → 產 override（id/action/chunk/selector/
field/expect/anchor/old 全自動，只剩 `new` 是你的判斷）→ 併入 `math_overrides/<slug>.json` → apply 到
parsed → 單書重 validate 寫最新 report。你**不必**手寫 override JSON，也**不必**回讀 parsed 定位。

## 你給的 `new`（唯一要判斷的）

- **eq 區塊**：給裸 tex（不含 `$`）。**inline**（md/caption/footnote/title）：給裸 inner（不含 `$`）。
- 看 `err` 與 `tex` 判斷壞在哪，改成**語意正確**的 LaTeX。常見壞法 → 修法：

| 病灶 | 例 | 修法 |
|---|---|---|
| 雙上下標 | `a^{x}^{y}` | 合併 `a^{xy}` 或照原意分組 |
| 文字模式跑出上下標 | `\text{a_b}` | 上下標移出 `\text{}` |
| `\left`/`\right` 不配對 | `\left( x` | 補對應 `\right)`，或用 `\left.`/`\right.` |
| 偽巨集（OCR 黏字） | `\Nu`→`N`、`\muA`→`\mu A` | 改回正確 token |
| 缺括號 | `\frac{a}` | 補足引數 |
| 模式 delimiter 殘體 | `0\]\[x` | 刪 `\[ \]`、`\(`→`(`（多已被 R8 自動清） |

- **絕不亂猜塞一個能渲染但語意錯的式子**。看不懂原意 → 翻 PDF（下節）；連 override 成可渲染都做不到 →
  accept（下下節）。

## 看不懂原意 → 對照 PDF 重建

開 `raw_pdfs/<file>.pdf`（`slug_map.json` 查檔名）對照章節，照原書重打正確 LaTeX 當 `new`。雲端機缺 PDF
→ 不硬重建，走 accept。

## accept（源文已毀，極少用）

收斂目標是**真 0**：連源文已毀的也盡量 override 成「至少渲染得出來」。唯有**連可渲染都做不到**（OCR 讀成
不可逆亂碼、無 PDF 可重建）的極少數，才 accept（不計入 `residual_unaccepted`、要留證供稽核）：
```bash
uv run python -m book_pipeline.devctl math-accept --slug <s> --occ <n> --reason '<為何連可渲染都做不到>'
```

## 稀有手段：泛化規則（非預設）

殘餘 ~95% 是 occ==1 的單發式，泛化規則零槓桿——**預設一律逐條 override**。**唯有**某 OCR token 跨**極多
書**同病灶、確定性字串轉換、語意保持，逐條才不划算時，才考慮寫 Layer 1 規則（`math_normalize.py` 加冪等
純函式進 `_TEX_RULES` + before/after fixture 進 `test_math_normalize.py`，跑綠）。這條路罕用，別當常態。

## 硬規則

- **絕不手改 `parsed/*.json`**：一律經 override（`batch`/`fix` 自動產，git 追蹤、可 review、可重播）。
- override 失配是**正常**（源頭漂移）→ skip，不是錯誤；不要為了讓它套用而放寬 guard。
- **絕不** echo MinerU/zlib token。
- 一本錯不要停，記下續下一本，最後彙整。

## 收斂任務（真 0）

**目標：residual_unaccepted = 0。** 反覆 `list` → `batch`（清大量）→ `fix`（零頭）→ `build`，直到 0 或
剩餘全是 accept（源文已毀的極少數）。收工前 `list` 確認待辦已到 0（或只剩已 accept）。
