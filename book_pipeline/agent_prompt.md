# Translate-book sub-agent SOP

呼叫者在 prompt 給 `SLUG` 與 `BATCH`（例：`SLUG=kittel_thermal BATCH=ch07_00`）。

1. Read `book_pipeline/mineru_data/<SLUG>/batches/<BATCH>_in.json`
2. 翻譯 `units` 陣列中每個單元的 `text`，譯名與規範見該檔的 `guide` 欄位
3. Write `book_pipeline/mineru_data/<SLUG>/batches/<BATCH>_out.txt`（**純文字檔，非 JSON**）：每個單元先一行 `<<<n>>>`（n 為該單元的 n 值），下一行起寫譯文
4. 回覆 `done`

**你只做翻譯**：複製 `<<<n>>>` 標記、寫譯文。**不要寫 JSON、不要碰 i/prob/field、不要數數量、不要自檢計數**——程式會按標記核對缺漏並只補缺的。LaTeX 原樣照打、不要跳脫反斜線。規範全在 in.json 的 `guide`，不需 Read 其他檔。
