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

/**
 * 註冊 Service Worker（只為離線，見 sw.js 檔頭；一律 network-first、零陳舊風險）。
 * `?sw=off` 是逃生口：萬一某天 SW 出事，帶這個 query 開一次就會登出並清空它的快取。
 * 失敗一律靜默——離線能力是加分項，絕不能因為它讓正常載入出問題。
 */
export function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return
  if (new URLSearchParams(location.search).get('sw') === 'off') {
    (async () => {
      const regs = await navigator.serviceWorker.getRegistrations()
      await Promise.all(regs.map(r => r.unregister()))
      // unregister 不會停掉「目前這一頁」的 controller：這一輪的資源請求仍走 SW，
      // 清了快取也會被它馬上填回去。所以先重載一次脫離控管（用 sessionStorage 擋迴圈），
      // 下一輪 controller 為 null 時才真的清得乾淨。
      if (navigator.serviceWorker.controller && !sessionStorage.getItem('sw-off')) {
        sessionStorage.setItem('sw-off', '1')
        location.reload()
        return
      }
      sessionStorage.removeItem('sw-off')
      const ks = await caches.keys()
      await Promise.all(ks.map(k => caches.delete(k)))
    })().catch(() => {})
    return
  }
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
