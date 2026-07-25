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
// item：{ slug, kind, key, anchor, scrollTop, scrollRatio, updatedAt }
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
  data.last = item
  data.chunks = (data.chunks && typeof data.chunks === 'object') ? data.chunks : {}
  data.chunks[ck] = item
  return saveProgress(data)
}

/** 某本書上次讀到哪（跨 chunk）；不是這本書就 null。 */
export function lastProgressFor(slug) {
  const last = loadProgress().last
  if (!last || last.slug !== slug || !last.kind || last.key == null) return null
  return last
}
