# qc — PDF 視覺驗證（爬取與 ingest 之間的 LLM 關卡）

被 `pipeline_tick` 對 `pdf_triage` 判 `needs_llm=True` 的書派來（掃描檔、ocr_sandwich、低 DPI、字少等邊界情況）。職責：用**一張 contact sheet 一次 vision 呼叫**判斷此書可否進 MinerU，結論寫回 pipeline state。

## 為何需要

`pdf_triage`（確定性 pymupdf）已分類 type/quality，但測不出「是不是正確的書/版次、實際清不清晰、有沒有缺章、掃描歪斜污損」。這些只能用眼睛看——所以才有這關，且只對可疑書跑（born_digital/good 直接 proceed，不進此關）。

## 流程

```bash
# 1. 產抽樣拼圖（均勻取 6 頁，跳過封面/索引，拼成單張 PNG）
uv run python -m book_pipeline.pdf_contactsheet <slug> --pages 6
#    → book_pipeline/reports/contactsheets/<stem>.png
#    （pymupdf/pillow 已在 pyproject 依賴，勿加 --with：那會每次強制 ephemeral env 冷解析、render 變慢）
```

> ⚠ **渲染已硬封頂（~90s）+ 部分容錯**。逐頁漸進渲染：個別頁病態（卡 fitz C 層）只會少那一頁，**其餘頁照常
> 拼出 sheet**（stderr 會印 `⚠ contactsheet 部分渲染：N/6 頁`）——這屬正常，**依可見頁判斷 pass/reject、不要因少
> 一兩頁就判不完整**。只有命令**非零退出且 stderr 含 `CONTACTSHEET_UNRENDERABLE`**（=連一頁都出不來、整本不可
> 渲染）才代表 PDF 損壞/超大掃描——**此時才 `reject`（reason 引用該訊息）**，**不要**自己改寫 inline
> `fitz`/`sips`/單頁渲染去跟它搏鬥（那正是過去燒滿 daemon 60min 上限、產不出 verdict 的根因）。

2. **Read 那張 PNG**（一次 vision 呼叫），判斷：
   - **書對嗎**：書名/作者/版次符合預期（版次 SoT = `book_pipeline/booklists/*.json` 該 slug，含 `edition_pref`；勿用已退役的 `crawl_manifest.json`）。
   - **清晰嗎**：文字與公式可辨識？掃描是否過淡/歪斜/污損到 OCR 會大量出錯。
   - **完整嗎**：抽樣頁是否連貫、無大量空白/缺頁跡象。
   - **MinerU 吃得下嗎**：掃描檔只要夠清晰，MinerU OCR 可處理；極糊或手寫批註滿版則否。

3. **寫回結論**（持久化，避免重複呼叫）：
```bash
uv run python -c "from book_pipeline import pipeline_queue as q; q.set_qc('<slug>', 'pass', '清晰掃描, DPI 足', 'claude')"
# verdict: pass（可進 ingest）| reject（不可用, 需重爬更好版本）
```

4. `reject` 時在回覆簡述原因，並建議是否該回 crawl 找更好的版本（不同版次/出版社）。

## 判準速記

| 看到 | 判決 |
|---|---|
| 清晰掃描、公式可讀、頁面連貫 | pass |
| born-digital 般銳利（triage 可能誤判邊界） | pass |
| 掃描極糊/嚴重歪斜/滿版手寫註記 | reject |
| 明顯缺章、大量空白、書名版次不符 | reject |

預設偏向 **pass**（MinerU OCR 比想像中強）；只有明確不可用才 reject，避免浪費爬取額度。
