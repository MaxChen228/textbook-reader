/**
 * localStorage 持久層：顯示設置 + 閱讀進度。
 *
 * 只管「schema、驗證、讀寫」，不碰 DOM——捲動位置怎麼量、怎麼還原是 reader 的事，
 * 這裡只負責存下來的東西一定合法。壞掉/被人手改過的 localStorage 一律靜默退回預設值，
 * 絕不讓一筆髒資料把整個 app 弄崩（無痕模式寫入丟例外也一併吞掉）。
 */

// ── 低階：容錯的 localStorage 存取 ─────────────────────────────────
function readJson(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key) || 'null')
    return v && typeof v === 'object' ? v : fallback
  } catch { return fallback }
}

function writeJson(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true } catch { return false }
}

// ── 顯示設置 ───────────────────────────────────────────────────────
export const SETTINGS_KEY = 'textbook.settings.v1'
export const DEFAULT_SETTINGS = { lang: 'en', fsStep: 4, lhStep: 5, widthStep: 5, theme: 'auto', skin: 'cohere' }
export const VALID_LANGS = ['en', 'zh', 'bi']
export const VALID_THEMES = ['auto', 'light', 'dark']
export const VALID_SKINS = ['cohere']   // 與 design/tokens.css + QBankShared.theme.SKINS 對齊
/** 三條滑桿的實際值表；UI 存的是 1–10 的 step，換算才得到 px / 倍數。 */
export const FS_VALUES = [10, 12, 14, 16, 18, 22, 26, 30, 35, 40]
export const LH_VALUES = [1.00, 1.15, 1.30, 1.50, 1.70, 1.95, 2.25, 2.55, 2.85, 3.20]
export const WIDTH_VALUES = [640, 700, 760, 820, 880, 940, 1000, 1060, 1120, 1180]

/** 滑桿值一律夾在 1–10 的整數；非法就退回 fallback。UI 事件與 localStorage 共用同一道驗證。 */
export function clampStep(value, fallback) {
  const n = Number(value)
  return (Number.isInteger(n) && n >= 1 && n <= 10) ? n : fallback
}

/** 舊版存的是實際值（fs: 16px）而非 step，遷移時挑最接近的一格。 */
function nearestStep(values, value, fallback) {
  const n = Number(value)
  if (!Number.isFinite(n)) return fallback
  let best = 0
  values.forEach((v, i) => { if (Math.abs(v - n) < Math.abs(values[best] - n)) best = i })
  return best + 1
}

export function loadSettings() {
  const raw = readJson(SETTINGS_KEY, {})
  const merged = {
    lang: VALID_LANGS.includes(raw.lang) ? raw.lang : DEFAULT_SETTINGS.lang,
    fsStep: clampStep(raw.fsStep, nearestStep(FS_VALUES, raw.fs, DEFAULT_SETTINGS.fsStep)),
    // 舊版的列高/版寬是三段離散值（compact/normal/loose、focus/standard/wide），對到現行 10 段。
    lhStep: clampStep(raw.lhStep, { compact: 2, normal: 6, loose: 9 }[raw.lh] ?? DEFAULT_SETTINGS.lhStep),
    widthStep: clampStep(raw.widthStep, { focus: 2, standard: 5, wide: 8 }[raw.width] ?? DEFAULT_SETTINGS.widthStep),
    theme: VALID_THEMES.includes(raw.theme) ? raw.theme : DEFAULT_SETTINGS.theme,
    skin: VALID_SKINS.includes(raw.skin) ? raw.skin : DEFAULT_SETTINGS.skin,
  }
  // 正規化後與原值不同（首次遷移／髒資料）→ 立刻寫回，之後就不必每次重算。
  if (JSON.stringify(merged) !== JSON.stringify(raw)) writeJson(SETTINGS_KEY, merged)
  return merged
}

export function saveSettings(settings) {
  return writeJson(SETTINGS_KEY, settings)
}

// ── 閱讀進度 ───────────────────────────────────────────────────────
// schema：{ last: item, chunks: { '<slug>/<kind>/<key>': item } }
// item：{ slug, kind, key, anchor, scrollTop, scrollRatio, maxRatio, updatedAt }
//
// **兩個比例各司其職，別合併**：
//   scrollRatio = 離開時的位置 → 下次回到這一章要捲到哪（resume）
//   maxRatio    = 這一章曾到達的最遠處 → 進度顯示（單調不減）
// 合成一個會壞在這個真實情境：開站自動回到上次位置，內容因為數學排版而長高，
// 同一個 scrollTop 換算出來的比例變小，一離開就把 40% 覆寫成 4%。進度只該往前。
export const PROGRESS_KEY = 'textbook.readerProgress.v1'

export function progressKey(slug, kind, key) {
  if (!slug || !kind || key == null) return null
  return `${slug}/${kind}/${key}`
}

export function loadProgress() {
  return readJson(PROGRESS_KEY, {})
}

export function saveProgress(data) {
  return writeJson(PROGRESS_KEY, data)
}

/** 讀某個 chunk 上次離開的位置；沒有就 null。 */
export function chunkProgress(slug, kind, key) {
  const ck = progressKey(slug, kind, key)
  if (!ck) return null
  return loadProgress().chunks?.[ck] || null
}

/** 記錄一筆位置：同時更新「最後閱讀」與該 chunk 自己的紀錄。 */
export function recordProgress(item) {
  const ck = progressKey(item.slug, item.kind, item.key)
  if (!ck) return false
  const data = loadProgress()
  data.chunks = (data.chunks && typeof data.chunks === 'object') ? data.chunks : {}
  const merged = { ...item, maxRatio: Math.max(furthest(data.chunks[ck]), Number(item.scrollRatio) || 0) }
  data.last = merged
  data.chunks[ck] = merged
  return saveProgress(data)
}

/** 一筆紀錄的「最遠讀到哪」。舊資料只有 scrollRatio → 拿它當初始值。 */
export function furthest(item) {
  if (!item) return 0
  const v = Number(item.maxRatio)
  return Number.isFinite(v) ? v : (Number(item.scrollRatio) || 0)
}

/** 某本書上次讀到哪（跨 chunk）；不是這本書就 null。 */
export function lastProgressFor(slug) {
  const last = loadProgress().last
  if (!last || last.slug !== slug || !last.kind || last.key == null) return null
  return last
}

/** 捲到這個比例就算「讀完這一章」。不用 1.0：最後一段常留在視窗中段，永遠碰不到底。 */
export const CHUNK_DONE_RATIO = 0.9
/**
 * 低於這個比例視同「只是點進去看了一眼」，不算讀過。
 * 沒有這道門檻的話，隨手翻過的書會全部掛上「已讀 0/9 章 0%」，書牆變成一片雜訊。
 */
export const CHUNK_STARTED_RATIO = 0.02

function chunkItems(data, slug) {
  return Object.entries(data.chunks || {})
    .filter(([ck]) => ck.startsWith(`${slug}/`))
    .map(([, item]) => item)
    .filter(it => it && typeof it === 'object')
}

/**
 * 單書統計：碰過幾章、讀完幾章、最後閱讀時間與位置。
 * total（總章數）由呼叫端從 books.json 給——store 不該知道書的結構。
 */
export function bookStats(slug, total = 0) {
  const data = loadProgress()
  const items = chunkItems(data, slug)
  if (!items.length) return { touched: 0, started: 0, read: 0, total, ratio: 0, lastAt: 0, last: null }
  const read = items.filter(it => furthest(it) >= CHUNK_DONE_RATIO).length
  const started = items.filter(it => furthest(it) >= CHUNK_STARTED_RATIO).length
  const newest = items.reduce((a, b) => (Number(b.updatedAt) > Number(a.updatedAt) ? b : a))
  return {
    touched: items.length,
    started,
    read,
    total,
    ratio: total ? Math.min(1, read / total) : 0,
    lastAt: Number(newest.updatedAt) || 0,
    last: newest,
  }
}

/** 最近讀過的書（新到舊）。回傳 [{slug, lastAt, last}]，供「繼續閱讀」用。 */
export function recentBooks(limit = 4) {
  const data = loadProgress()
  const bySlug = new Map()
  for (const [ck, item] of Object.entries(data.chunks || {})) {
    if (!item || typeof item !== 'object') continue
    if (furthest(item) < CHUNK_STARTED_RATIO) continue   // 點進去看一眼不算「在讀」
    const slug = item.slug || ck.split('/')[0]
    const at = Number(item.updatedAt) || 0
    const cur = bySlug.get(slug)
    if (!cur || at > cur.lastAt) bySlug.set(slug, { slug, lastAt: at, last: item })
  }
  return [...bySlug.values()].sort((a, b) => b.lastAt - a.lastAt).slice(0, limit)
}

/** 忘掉某本書的所有進度（書卡上的「清除進度」）。 */
export function forgetBook(slug) {
  const data = loadProgress()
  for (const ck of Object.keys(data.chunks || {})) {
    if (ck.startsWith(`${slug}/`)) delete data.chunks[ck]
  }
  if (data.last?.slug === slug) delete data.last
  return saveProgress(data)
}
