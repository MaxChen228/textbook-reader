# Math sweep — 泛化提案佇列（人工 review → 升級 Layer 0/1）

sweep agent（含 daemon autonomous track）把「可跨書泛化」的修法**只記在這裡**，**不**自行改
`math_macros.json`（Layer 0）/ `math_normalize.py`（Layer 1）—— 那是核心碼，daemon 不 commit，
誤改會掩蓋真錯。owner review 後才動手升級 + 補測試 + 跑全 corpus 回歸（殘餘不得上升）。

per-slug 的一次性亂碼/截斷修復 **不**寫這裡，直接寫 `math_overrides/<slug>.json`（apply_math_overrides 套用）。

格式（每筆一節）：

```
## <YYYY-MM-DD> \macro 或 規則名 — ×N occ / M 書
- 類型：macro | normalize-rule
- 證據：哪些 tex 樣本壞、render err、跨哪些書
- 提議：新增 macros 條目 `"\\x": "..."` / 新增 normalize 規則 RX（附 before→after）
- 風險：是否可能誤改合法式子（要全 corpus 回歸）
- 狀態：proposed | accepted(<commit>) | rejected(理由)
```

---

<!-- proposals 由此往下 append -->
## 2026-06-17 \ifmmode 條件乘號展開 — ×17 occ / 2 書
- 類型：macro
- 證據：`$\mathrm { S U } ( 2 ) \ifmmode \times \else \texttimes \fi { } \mathrm { S U } ( 2 )$`、`$\mathrm { S U } ( 2 ) \ifmmode \times \else \texttimes \fi { } \mathrm { U } ( 1 )$`；MathJax 報 `Undefined control sequence \ifmmode`；出現在 `schwartz_qft`、`srednicki_qft`。
- 提議：Layer 1 normalize 規則，把 `\ifmmode \times \else \texttimes \fi` 收斂成 `\times`。
- 風險：低；這段條件式在數學區通常只是 TeX/文字雙態兼容寫法，但仍需全 corpus 回歸確認沒誤吞非乘號分支。
- 狀態：proposed

## 2026-06-17 \Nu 映射到 \nu — ×20 occ / 6 書
- 類型：macro
- 證據：`$\Nu \colon S \to \mathbb { R } ^ { 3 }$`、`$\Nu \geq \Nu _ { 0 }$`、`$\Nu > 2$`；MathJax 報 `Undefined control sequence \Nu`；出現在 `do_carmo_dg`、`rudin_analysis`、`goldstein_cm3`、`lindner_theoretical_physics`、`thijssen_computational_physics`、`dummit_foote_algebra`。
- 提議：Layer 0 macros 新增 `"\\Nu": "\\nu"`。
- 風險：中低；`\Nu` 不是標準 LaTeX 巨集，現有樣本都像 OCR/作者自定義把希臘小寫 nu 誤寫成大寫名，仍需回歸確認沒有少數書把它當自定義符號。
- 狀態：proposed

## 2026-06-17 \bgroup / \aftergroup / \egroup 噪訊收斂 — ×19 occ / 6 書
- 類型：normalize-rule
- 證據：`$\mathbf { Z } _ { \mathrm { T h } } = 4 . 4 7 3 \mathopen { } \mathclose \bgroup / - 7 . 6 4 ^ { \circ } ~ \Omega$`、`$\mathrm { d } _ { X } \mathopen { } \mathclose \bgroup \left( x , \mathfrak { p } \aftergroup \egroup \right) < \delta$` 等；MathJax 報 `Undefined control sequence \bgroup`；出現在 `alexander_circuits`、`axler_linalg`、`dummit_foote_algebra`、`hatcher_algebraic_topology`、`rudin_analysis`、`schwartz_qft`。
- 提議：Layer 1 normalize 規則移除 `\mathopen { } \mathclose \bgroup` / `\aftergroup \egroup` 這類成對噪訊，保留真正的定界符內容。
- 風險：中；若規則寫太寬，可能誤刪合法 grouping token，需只針對 OCR 固定片段做最小替換並跑全 corpus 回歸。
- 狀態：proposed

## 2026-06-17 \muA 單位巨集展開 — ×7 occ / 1 書
- 類型：macro
- 證據：`$I_{B} = 0.1 \, \muA$`、`$25 \, \muA$`、`$k_{n}^{\prime} = 100 \, \muA/V^{2}$`；MathJax 報 `Undefined control sequence \muA`；目前集中於 `sedra_microe`。
- 提議：Layer 0 macros 新增 `"\\muA": "\\mu\\text{A}"`，避免各書再用 override 重覆修單位。
- 風險：低；用途明確是微安培單位，但需確認沒有書把它當作者自定 shorthand 並期待不同排版。
- 狀態：proposed
