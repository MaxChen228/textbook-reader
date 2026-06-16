# Audit Report — burden_numerical

## 書籍資訊
- **Slug**: burden_numerical
- **Title**: Numerical Analysis
- **Author**: Richard L. Burden; J. Douglas Faires; Annette M. Burden
- **Edition**: 10th
- **Subject**: Mathematical Methods
- **Publisher**: Cengage Learning
- **Language**: en

## 結構摘要
- **章節數**: 12
- **Appendix 數**: 0
- **heading_text_level**: 2
- **inline_problems**: true
- **problem_num_namespace_by_section**: true

## Regex 設定
- **section_re**: `'^(\d+\.\d+)\s+(.+)$'`
- **subsection_re**: `'^(\d+\.\d+\.\d+)\s+(.+)$'` (無實際匹配)
- **problem_start_re**: `'^(\d+)\.\s+'`
- **problem_chapter_must_match**: false
- **equation_label_re**: `'\\tag\{([0-9]+\.[0-9]+[a-z]?)\}'`
- **example_start_re**: `'^Example\s+(\d+)'`

## Smoke 最終結果
- **critical**: 14
- **warning**: 0

### Critical 明細

#### H2 — Problem num 重複 (12 項，每章 1 項)
**原因**: 本書每節末尾有 `E X E R C I S E S E T X.Y` 習題集，題號依節重置（1, 2, 3...）。但每節最後還有 `DISCUSSION QUESTION(S)`，同樣以 `1. `、`2. ` 編號，導致同一節 namespace 內出現重複題號（如 `1.1.1` 同時對應習題 1 與討論題 1）。

**已設**: `problem_num_namespace_by_section: true`，但討論題與習題共用同一節編號空間，parser 無法區分。此為書籍結構特性，無法僅靠 yaml 修正。

#### H6 — Catalog unresolved refs=12 (Table=12)
**原因**: 正文引用 12 個表格（如 Table 2.6、Table 3.17、Table 4.9 等），但這些表格在 MinerU 輸出中無 caption 或 ID，未被編入 catalog。多為 Example 內的輔助表格，MinerU 未正確提取標題。

#### H7 — Catalog empty captions=274
**原因**: 274 個 figure/table 區塊的 caption 為空字串。MinerU 未成功提取圖表標題文字，導致 catalog 無法建立可索引條目。需手動補 caption 或設 catalog_exclude_reason。

## OCR 空洞列表
- **無空洞**。未發現 `type=='list'` 且 `text==''` 且 `list_items` 全空的區塊。

## 不確定決策

### ⚠ Ch 7 / Ch 8 章節標題層級
Ch 7 (`Iterative Techniques in Matrix Algebra`) 與 Ch 8 (`Approximation Theory`) 的章節標題在 MinerU 輸出中被標為 `text_level=2`（而非其他章的 `text_level=1`）。已設 `chapter_title_block_idx` 為實際標題區塊，並對這兩章加設 `chapter_title_block_idx_secondary` 指向 `Introduction` 小標，確保解析正確。

### ⚠ 最後一章邊界
Ch 12 原設 `next_chapter_block_idx: 21990`（檔案末尾），導致 Bibliography、Answers for Selected Exercises、Index 被誤入 Ch 12。已修正為 `next_chapter_block_idx: 17666`（Bibliography 起始區塊），使 Ch 12 正確止於 page 797。

### ⚠ 無 Appendix
本書無 Appendix 章節。`appendices_start_page: 798`（= Bibliography 起始頁），`appendices: []`。

### ⚠ Bibliography 與 Answers 區段
- Bibliography 起於 page 798（idx 17666）
- Answers for Selected Exercises 起於 page 804（idx 17690）
- Index 起於 page 906（idx 20450）
這些後設資料區段已正確排除於章節範圍外。
