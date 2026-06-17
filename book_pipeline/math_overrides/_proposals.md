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
