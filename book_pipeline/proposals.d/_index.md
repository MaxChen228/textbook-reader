# 建議佇列（proposals）— 由 JSON store 自動生成，請勿手改

正本 = `book_pipeline/proposals.d/<id>.json`（一案一檔）。新增/改狀態一律走 CLI：
`uv run python -m book_pipeline.proposals {propose|resolve|list|check|gate}`。
決策樹/閘/生命週期（owner 知識）正本：`book_pipeline/proposals.py` 模組 docstring。

## domain: math  （5 條；proposed=1）

### P-2026-06-17-collapse-mathtype-slash-phantom- — Collapse MathType slash phantom/kern residue to /
- proposed | type=normalize-rule | source=math_sweep | 偵測=\\kern,\\vphantom,\\mathord,\\left/
- 證據：cluster other occ=4 in dummit_foote_algebra plus token_signals: \\kern occ=21 / 10 books, \\vphantom occ=20 / 9 books; representative samples from dummit_foote_algebra, boas_mp, griffiths_qm3, rudin_analysis, srednicki_qft
- 提議：Replace exact MathType slash residue \\mathord{\\left/ {\\vphantom{...}} \\right. \\kern - delimiterspace} (and equivalent \\mathbin form) with literal /
- 風險：Could collapse non-slash delimiter constructs if pattern too broad; keep match exact on left/phantom/right./kern sequence and rely on full-corpus gate for collateral

### P-2026-06-17-bgroup — \bgroup / \aftergroup / \egroup 群組噪訊收斂
- accepted | type=normalize-rule | source=math_sweep | 偵測=\bgroup \egroup \aftergroup
- 決議：R5 _remove_group_noise
- 證據：\mathopen{}\mathclose\bgroup … \aftergroup\egroup 成對噪訊；alexander_circuits/axler_linalg/dummit_foote_algebra/hatcher_algebraic_topology/rudin_analysis/schwartz_qft。×19 occ。
- 提議：Layer 1 normalize 移除成對 \mathopen{}\mathclose\bgroup / \aftergroup\egroup / 殘留 \mathclose\bgroup / 裸 token。
- 風險：低；這些 token 在 MathJax 全 undefined → 凡含者本就 fail，移除只能 fail→pass，回歸閘天然安全。

### P-2026-06-17-ifmmode — \ifmmode 條件乘號展開
- accepted | type=normalize-rule | source=math_sweep | 偵測=\ifmmode
- 決議：R4 _fix_cond_times
- 證據：SU(2) \ifmmode \times \else \texttimes \fi { } …；MathJax 報 Undefined control sequence \ifmmode；出現在 schwartz_qft、srednicki_qft。×17 occ / 2 書。
- 提議：Layer 1 normalize 規則：\ifmmode \times \else \texttimes \fi → \times。
- 風險：低；reader 一律數學區 → 恆等於 \times。全 corpus 回歸確認無誤吞。

### P-2026-06-17-mua — \muA 單位巨集
- rejected | type=macro | source=math_sweep | 偵測=\muA
- 決議：already-resolved single-book
- 處置：已由 math_overrides/sedra_microe.json 5 條 override 清零（bad_occ=0），macro 冗餘
- 證據：I_B = 0.1 \, \muA；僅 sedra_microe。×7 occ / 1 書。
- 提議：原提案 Layer 0 macro \muA→\mu\text{A}。
- 風險：\muA 在禁收清單；只此 1 本無泛化價值，已由 sedra_microe.json override 清零。

### P-2026-06-17-nu — \Nu 映射
- rejected | type=macro | source=math_sweep | 偵測=\Nu
- 決議：pseudo-macro-guard semantically-ambiguous
- 處置：per-slug override（觀測語境全為大寫 N：高斯映射 N、Rudin 自然數界 N、Dummit 範數 N_{K/F}）
- 證據：\Nu \colon S \to \mathbb{R}^3（高斯映射 N）、\Nu \geq \Nu_0、\Nu_{K/F}(\alpha)（範數 N）；6 書。×20 occ。
- 提議：原提案 Layer 0 macro \Nu→\nu。
- 風險：\Nu 在 test_no_ocr_glue_pseudomacros 禁收清單；且語意非唯一——\Nu→\nu 對所有觀測樣本皆錯（實為大寫 N）。
