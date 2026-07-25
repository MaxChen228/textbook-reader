/**
 * assets/qbank-shared.js（傳統全域腳本）→ ESM 具名匯出的轉接層。
 *
 * 為什麼不把 qbank-shared.js 本身改成 module：
 *  1. dev/index.html 的 inline 傳統腳本同步依賴 `window.QBankShared`。module 一律 deferred，
 *     改過去會讓它在 inline 腳本之後才執行 → /dev 直接白畫面。
 *  2. build/gen_macros.py 以 MACROS:BEGIN/END 字串區塊寫入該檔的 mathJaxConfig，
 *     檔案結構是 build 的契約。
 * 所以共用層維持「傳統腳本 + window 全域」，新模組經由本檔取用，避免第二份實作。
 * 三頁的 <head> 都必須先以傳統 <script> 載入 qbank-shared.js，再載 module。
 */
const S = globalThis.QBankShared
if (!S) throw new Error('[shared] qbank-shared.js 必須在 module 之前以傳統 <script> 載入')

export const {
  bindHistoryBackLinks,
  bindSidebarDrawer,
  countUp,
  createChip,
  escapeAttr,
  escapeHtml,
  errorMessage,
  fetchJson,
  mathJaxConfig,
  openPrintWindow,
  printTypographyCss,
  relTime,
  renderMarkdown,
  renderMath,
  safeHtml,
  theme,
} = S

export default S
