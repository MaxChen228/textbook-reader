# DESIGN.md — textbook-reader 設計系統（Cohere）

> 正典 spec。機器真相在 [`design/tokens.css`](./tokens.css)（所有 token）；切換邏輯在
> `assets/qbank-shared.js` 的 `QBankShared.theme`。元件只引用語意 token，**換皮 = 新增一個
> skin 區塊，元件零改動**。
>
> **來源**：本系統的視覺語彙取自 claude.ai 上的 **Cohere** design system（企業 AI 官網重建）。
> 那是上游靈感來源；本檔與 tokens.css 是**本 repo 在地落實**，與 claude.ai 無 live link、不雙向同步。

## 架構：兩軸正交

| 軸 | 屬性（掛 `<body>`） | 值 | 意義 |
|---|---|---|---|
| 風格 skin | `data-skin` | `cohere`(目前唯一) | 美感家族 |
| 模式 mode | `data-theme` | `light` / `dark`（`auto` 跟系統） | 明暗 |

token 三層：**結構**(字體/圓角/間距，skin 無關) → **語意**(每 skin × mode：`--bg`/`--text`/`--ink`/`--accent`/`--on-ink`/`--shadow-*`…) → **元件**(只用語意 token)。

---

## 1. 視覺主題與氛圍
Cohere：**白 canvas 為主**、深綠黑/海軍藍色帶作分節、**珊瑚橙(coral)** 編輯強調、action blue 連結；克制、企業、精準。色彩來自媒體與色帶、不是 UI chrome。**例外（本 repo 刻意）**：文章正文(article)保留**襯線**（CMU + Noto Serif TC），長文閱讀 + 行內 MathJax 數學和諧。

## 2. 色票與語意角色（定義於 tokens.css）
| token | 角色 | cohere light |
|---|---|---|
| `--bg` / `--surface` | 頁底 / 抬升表面 | `#ffffff` / `#ffffff`（靠髮絲邊框分層） |
| `--border` / `--border-l` | 主髮絲線 / 最淺卡片線 | `#d9d9dd` / `#f2f2f2` |
| `--text` / `--sub` | 內文 / 次要 | `#212121` / `#616161` |
| `--ink` / `--ink-light` | 近黑實心填色(CTA/active) / slate hover | `#17171c` / `#75758a` |
| `--on-ink` | 疊在 `--ink` 上的字 | `#ffffff` |
| `--accent` | coral 編輯強調 | `#ff7759` |
| `--shadow-*` / `--focus-ring` | 扁平柔影 / 藍 focus 環 | media-lift / `#4c6ee6` |
| `--status-*` | 自評三態 + danger（skin 無關） | green/orange/slate/red |
| `--info` / `--sweep` | 功能色：雲端 OCR·下載·等外部 / math sweep | action blue `#1863dc` / violet `#6a4cd6` |
| `--on-accent` / `--on-solid` | 淺填色(coral/amber)上深字 / 飽和填色(藍紫綠紅)上白字 | `#17171c` / `#ffffff` |

> **功能性領域 token**（`--info`/`--sweep`/`--on-accent`/`--on-solid`）：pipeline 狀態語意，`/dev` 儀表板消費，定義於每 skin × mode → `/dev` 亦可換皮。疊在封面圖上的浮層 backing（`.ps-badge`/`.dl-mb`/walker 陰影/drawer scrim）刻意保留固定半透明黑、不隨主題（§8 Don't 影像 backing 例外）。

dark 模式：Cohere 官方無完整深色 UI，由其深綠黑/近黑色帶**推導**（`--bg#0f1311`/`--surface#17171c`/coral 提亮）。

## 3. 字體規則
- **UI / chrome 內文**：`--sans` = **Inter**（Cohere body 替身）。
- **大標**：`--font-display` = **Space Grotesk**（Cohere display 替身，碑刻感）。
- **技術標籤 / 數值**：`--mono` = **Space Mono**。
- **文章正文 + 數學**：`--serif`/`--article-font` = **CMU Serif + Noto Serif TC**（刻意保留襯線）。
- 字檔缺失用 Google Fonts 替身（與 Cohere 文件一致：CohereText→Space Grotesk、Unica77→Inter、CohereMono→Space Mono）。

## 4. 元件樣式（`assets/qbank-shared.css`，全 token 化）
button(`.btn-primary` 膠囊 / `.qbk-control-btn` / `.qbk-icon-btn`)、chip(`.qbk-chip`，8px 圓)、輸入(`.qbk-search`/`.qbk-select`)、分段(`.qbk-segmented`)、浮層(`.qbk-popover`)、卡片(`.qbk-raised-item`，8px)、側欄/topbar 殼層。各態齊備。

## 5. 版面原則
殼層 `.qbk-app`（flex 全高）：左 `--qbk-sidebar-width` 側欄 + 右主欄；內容頁 `.qbk-page-shell`。Cohere 8px 間距基、區段大留白。

## 6. 深度與陰影
Cohere 偏扁平：深度靠表面交替 + 髮絲邊框 + 圓角。陰影僅保留卡片柔性 media-lift（`--shadow-card`/`--shadow-pop`），隨 mode 自動深淺，元件不寫死。

## 7. 圓角
`--radius-sm 4`（輸入/縮圖）/ `--radius 8`（chips/卡片）/ `--radius-md 16` / `--radius-lg 22`（招牌 media）/ `--radius-pill 32`（主 CTA 膠囊）。

## 8. Do / Don't
- ✅ 一律用語意 token；新值先進 tokens.css。
- ✅ coral 只作**編輯強調**小範圍，**不**做大面積填色（Cohere 鐵律）。
- ✅ 新增風格 = tokens.css 加 `body[data-skin="x"]`(light+dark) + `QBankShared.theme.SKINS` 加名字 + 把 `data-skin-set` 鈕加回 UI。
- ❌ 元件 CSS 寫死 hex / rgba（影像白底 backing、固定深色 lightbox overlay 例外，已註記）。
- ❌ 在 HTML 內各自重定義 dark/skin token（已全收斂到 tokens.css）。
- ❌ 註解內出現「星號緊接斜線」（會提早關閉註解、吞掉後段規則）。
- ❌ emoji 當品牌裝飾（Cohere 不用）。

## 9. RWD / Agent 提示
- 斷點 `max-width:768px`：側欄轉抽屜、topbar 收斂。
- 「加 X 元件」→ 用既有 `.qbk-*` primitives 組合，顏色只引語意 token。
- 「換 Y 風格」→ tokens.css 新增 `body[data-skin="y"]` 全集(light+dark)，勿動元件。
- 預覽：開 `design/preview.html` 對照色票 + 元件。
