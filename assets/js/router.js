/**
 * Hash 路由的單一真相。
 *
 * 文法：`#<slug>[/<kind>/<key>[/<anchor>]][?query]`
 *   #                                  → 書庫
 *   #stewart_calculus                  → 書總覽
 *   #stewart_calculus/ch/3             → 第 3 章
 *   #stewart_calculus/ch/3/sec-3.2     → 第 3 章並捲到 3.2 節（可分享、可收藏）
 *   #…/ch/3?problem=17                 → 進章後跳到第 17 題
 *
 * 建網址與解網址過去分散在兩處且編碼規則不一致（目錄連結有 encodeURIComponent、
 * 程式導航沒有），統一收在這裡：**只有本模組知道分隔符與編碼規則**。
 */

/** route 物件 → hash 字串（含前綴 #）。 */
export function buildHash({ slug, kind, key, anchor, params } = {}) {
  if (!slug) return '#'
  let h = '#' + encodeURIComponent(slug)
  if (kind && key != null) {
    h += `/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`
    if (anchor) h += `/${encodeURIComponent(anchor)}`
  }
  const q = params instanceof URLSearchParams ? params.toString() : new URLSearchParams(params || {}).toString()
  return q ? `${h}?${q}` : h
}

/** hash 字串 → route 物件。空 hash 回 { slug: null }（＝書庫）。 */
export function parseHash(hash = location.hash) {
  const raw = String(hash || '').replace(/^#/, '')
  if (!raw) return { slug: null, kind: null, key: null, anchor: null, params: new URLSearchParams() }
  const [pathPart, queryPart = ''] = raw.split('?')
  const parts = pathPart.split('/')
  const dec = (v) => { try { return decodeURIComponent(v) } catch { return v } }
  return {
    slug: dec(parts[0]),
    kind: parts.length >= 3 ? dec(parts[1]) : null,
    key: parts.length >= 3 ? dec(parts[2]) : null,
    anchor: parts[3] ? dec(parts[3]) : null,
    params: new URLSearchParams(queryPart),
  }
}

/** 導航（寫入 history，上一頁可回）。hash 沒變時瀏覽器不發 hashchange，呼叫端不必自行防抖。 */
export function go(route) {
  location.hash = buildHash(route)
}

/**
 * 只換網址、不寫 history。捲動造成的「目前所在節」變動用這個——
 * 每捲過一節就 push 一筆的話，上一頁會被逐節塞爆。
 */
export function replace(hash) {
  try { history.replaceState(null, '', hash) } catch { /* file:// 等環境 → 放棄同步網址 */ }
}

/** 訂閱 hashchange；回傳解除訂閱函式。 */
export function onRouteChange(cb) {
  const handler = () => cb(parseHash())
  window.addEventListener('hashchange', handler)
  return () => window.removeEventListener('hashchange', handler)
}
