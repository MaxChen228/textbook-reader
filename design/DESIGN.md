# DESIGN.md — textbook-reader 設計系統

> 本檔是設計系統的**正典 spec**，同時是 Claude Design (`/design-sync`) 的推送/同步 artifact。
> 機器真相在 [`design/tokens.css`](./tokens.css)（所有 token）；切換邏輯在 `assets/qbank-shared.js` 的 `QBankShared.theme`。
> 元件樣式只引用語意 token，**換皮 = 新增一個 skin 區塊，元件零改動**。

## 架構：兩軸正交

| 軸 | 屬性（掛 `<body>`） | 值 | 意義 |
|---|---|---|---|
| 風格 skin | `data-skin` | `paper`(預設) / `claude` | 整體美感家族 |
| 模式 mode | `data-theme` | `light` / `dark`（`auto` 跟系統） | 明暗 |

token 三層：**結構**(skin 無關：字體/圓角/間距/動態) → **語意**(每 skin × mode 一組：`--bg`/`--text`/`--ink`/`--accent`/`--on-ink`/`--shadow-*`…) → **元件**(只用語意 token)。

---

## 1. 視覺主題與氛圍
- **paper（預設）**：暖色紙感、襯線優先、學術閱讀。低彩度、高可讀、克制留白。density 偏緊（mono 標籤 + serif 正文）。
- **claude**：取自 Anthropic/Claude 品牌 — 暖奶油底 + 黏土橙（clay）accent，溫潤、現代、略帶手感。

## 2. 色票與語意角色（CSS 變數，定義於 tokens.css）
所有顏色走語意角色，元件**不得**寫死 hex：

| token | 角色 |
|---|---|
| `--bg` / `--surface` | 頁底 / 抬升表面（卡片、側欄、topbar） |
| `--border` / `--border-l` | 主邊線 / 更淺的分隔線 |
| `--text` / `--sub` | 正文 / 次要文字 |
| `--ink` / `--ink-light` | 強調實心填色（primary、active）/ hover 邊 |
| `--on-ink` | 疊在 `--ink` 填色上的文字色（取代寫死的 `#fff`，dark 自動翻面） |
| `--accent` | 品牌強調色（paper 近黑、claude 黏土橙） |
| `--shadow-1` / `--shadow-card` / `--shadow-pop` / `--shadow-seg` | 抬升/卡片/浮層/分段 的陰影（隨 mode 自動換深淺，免逐元件補 dark） |
| `--focus-ring` | 輸入聚焦光環 |
| `--status-known/unknown/skip/danger` | 自評三態 + 危險（skin 無關） |

## 3. 字體規則
- 正文 / UI：`--serif`（CMU Serif + Noto Serif TC fallback）。
- 標籤 / 數值 / 程式感：`--mono`（JetBrains Mono）。
- 數學：MathJax 3（`QBankShared.mathJaxConfig`，巨集由 `build/gen_macros.py` 生成）。

## 4. 元件樣式（皆在 `assets/qbank-shared.css`，全 token 化）
button(`.btn-primary`/`.qbk-control-btn`/`.qbk-icon-btn`)、chip(`.qbk-chip`)、輸入(`.qbk-search`/`.qbk-select`)、分段(`.qbk-segmented`)、浮層(`.qbk-popover`)、卡片(`.qbk-raised-item`)、側欄/topbar 殼層(`.qbk-*`)。各態(hover/active/disabled/focus/copied)齊備。

## 5. 版面原則
殼層 `.qbk-app`（flex 全高）：左 `--qbk-sidebar-width`(250/260) 側欄 + 右主欄。內容頁用 `.qbk-page-shell`(max 1100px 置中)。間距節奏以 4px 為基。

## 6. 深度與陰影
四級陰影 token（見 §2），**只在 tokens.css 定義 light/dark 兩值**，元件引用單一變數即自動隨 mode 變深淺 — 不再各元件寫 `body[data-theme="dark"]` 補丁。

## 7. Do / Don't
- ✅ 一律用語意 token；新值先進 tokens.css。
- ✅ 新增風格 = tokens.css 加 `body[data-skin="x"]` + dark 區塊，並在 `QBankShared.theme.SKINS` 加名字 → UI(`data-skin-set` 鈕)自動長出。
- ❌ 元件 CSS 寫死 hex / rgba（影像白底 backing、固定深色 lightbox overlay 例外，已註記）。
- ❌ 在 HTML 內各自重定義 dark/skin token（已全數收斂到 tokens.css）。

## 8. RWD
斷點 `max-width:768px`：側欄轉抽屜（`.qbk-drawer-btn` + overlay）、topbar 收斂、list padding 縮。

## 9. Agent 提示指南
- 「替 reader 加一個 X 元件」→ 用既有 `.qbk-*` primitives 組合，顏色只引語意 token。
- 「換成 Y 風格」→ 在 tokens.css 新增 `body[data-skin="y"]` 全集（light+dark），勿動元件。
- 「校準 claude 皮」→ 比對 `DESIGN-claude.md` 把 `body[data-skin="claude"]` 各 token 逐值對齊（目前為品牌色 first-cut）。

---

## Claude Design 同步（`/design-sync`）
此 `design/` 目錄即同步目標。雙向：`/design-sync` 可把本系統推上 claude.ai design 專案、或把 canvas 改動拉回。一個 repo 可掛多套設計系統；新增風格走 §7 流程。
