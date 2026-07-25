/**
 * 書內全文搜尋。
 *
 * 兩段式，因為沒有後端：
 *   ① 候選過濾 — 載 `data/<slug>/search.json`（倒排索引，一本書約 40KB gz），
 *      算出「哪些章可能有」。索引只存 token → 章序號，不存全文。
 *   ② 精確比對 — 只抓那幾章的 JSON（讀書本來就要抓、且會進 chunkCache），
 *      在瀏覽器裡對純文字做子字串比對，產生真正的命中位置與摘要。
 * 索引因此只需要「不漏」，不需要「精準」：多給的候選在第二段被淘汰，
 * 使用者永遠不會看到假命中。
 *
 * **分詞必須與 build/bake_json.py 的 _fold/_tokenize 逐條等價**，否則索引查不到。
 */
import { escapeHtml } from './shared.js'
import { blockToText } from './blocks.js'

const TOKEN_RE = /[\p{L}\p{N}]+/gu
const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿]/

/** 小寫 + 去附加符號（thévenin → thevenin）。對應 Python 的 _fold。 */
export function fold(text) {
  return String(text ?? '').toLowerCase().normalize('NFD').replace(/\p{M}/gu, '')
}

/**
 * 逐字 fold，並記下每個輸出字元來自原字串的哪一格。
 * 需要它是因為 fold 不保證長度不變（例如 'İ'.toLowerCase() 會變成兩個字元），
 * 直接拿 folded 的 index 去切原字串會讓 <mark> 標錯位置。
 */
function foldWithMap(text) {
  const src = String(text ?? '')
  let folded = ''
  const map = []
  for (let i = 0; i < src.length; i++) {
    const piece = fold(src[i])
    for (let k = 0; k < piece.length; k++) map.push(i)
    folded += piece
  }
  map.push(src.length)   // 尾哨兵：讓區間終點也查得到
  return { folded, map, src }
}

/** 在 folded 字串裡找出所有詞條的區間，換算回原字串座標並合併重疊。 */
function markRanges({ folded, map }, terms) {
  const spans = []
  for (const t of terms) {
    if (!t) continue
    let i = folded.indexOf(t)
    while (i >= 0) { spans.push([map[i], map[i + t.length]]); i = folded.indexOf(t, i + t.length) }
  }
  spans.sort((a, b) => a[0] - b[0])
  const merged = []
  for (const s of spans) {
    const last = merged[merged.length - 1]
    if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1])
    else merged.push([...s])
  }
  return merged
}

/** 對應 Python 的 _tokenize：CJK 取 2-gram，其餘取長度 2–30 的字母數字段。 */
export function tokenize(text) {
  const out = new Set()
  for (const raw of fold(text).matchAll(TOKEN_RE)) {
    const w = raw[0]
    if (CJK_RE.test(w)) {
      if (w.length === 1) out.add(w)
      else for (let i = 0; i < w.length - 1; i++) out.add(w.slice(i, i + 2))
    } else if (w.length >= 2 && w.length <= 30) {
      out.add(w)
    }
  }
  return out
}

/** 查詢字串 → 詞條陣列（已 fold）。空白分詞，單字元也保留（第二段是子字串比對）。 */
export function queryTerms(q) {
  return fold(q).split(/[^\p{L}\p{N}]+/u).filter(Boolean)
}

const indexCache = new Map()

/** 載某本書的索引（含快取）。找不到（舊 build 沒烤）回 null，呼叫端據此顯示「此書尚無索引」。 */
export async function loadIndex(slug) {
  if (!indexCache.has(slug)) {
    indexCache.set(slug, fetch(`data/${encodeURIComponent(slug)}/search.json`)
      .then(r => (r.ok ? r.json() : null))
      .catch(() => null))
  }
  return indexCache.get(slug)
}

/**
 * 候選章節（依命中詞數排序，多的在前）。
 * 詞條一律用**前綴比對**：使用者打到一半的 "capacit" 要能撈到 capacitor/capacitance——
 * 反正第二段會用子字串重驗，寧可寬。索引裡沒有的詞（不是 common）代表整本書都沒有。
 */
export function candidateChunks(index, terms) {
  if (!index || !terms.length) return []
  const common = new Set(index.common || [])
  const total = (index.chunks || []).length
  const score = new Map()
  for (const term of terms) {
    let hit = null
    if (index.tokens[term]) hit = index.tokens[term]
    else if (common.has(term)) hit = null      // 停用詞：等同全部命中，不縮小範圍
    else {
      hit = []
      for (const tok in index.tokens) {        // 前綴掃描（一本書幾千個 token，夠快）
        if (tok.startsWith(term)) hit = hit.concat(index.tokens[tok])
      }
      if (!hit.length) {
        // 停用詞的前綴也算命中全部（例如打 "th" 撞到被剪掉的 "the"）
        let commonPrefix = false
        for (const tok of common) { if (tok.startsWith(term)) { commonPrefix = true; break } }
        if (!commonPrefix) return []           // 這個詞全書都沒有 → 整條查詢無解
        hit = null
      }
    }
    const set = hit === null ? null : new Set(hit)
    for (let i = 0; i < total; i++) {
      if (set === null || set.has(i)) score.set(i, (score.get(i) || 0) + 1)
    }
  }
  return [...score.entries()]
    .sort((a, b) => b[1] - a[1] || a[0] - b[0])
    .map(([i]) => i)
}

/** 命中位置前後各取 radius 字的摘要，命中詞用 <mark> 標起來。回傳已跳脫的 HTML。 */
export function snippet(text, terms, radius = 62) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim()
  const fm = foldWithMap(flat)
  const all = markRanges(fm, terms)
  const at = all.length ? all[0][0] : 0
  const start = Math.max(0, at - radius)
  const end = Math.min(flat.length, at + radius * 2)
  let out = '', cursor = start
  for (const [a, b] of all) {
    if (b <= start || a >= end) continue
    if (a < cursor) continue
    out += escapeHtml(flat.slice(cursor, a)) + '<mark>' + escapeHtml(flat.slice(a, Math.min(b, end))) + '</mark>'
    cursor = Math.min(b, end)
  }
  out += escapeHtml(flat.slice(cursor, end))
  return (start > 0 ? '…' : '') + out + (end < flat.length ? '…' : '')
}

/** 一段文字是否含有全部詞條（AND 語意，子字串比對）。 */
function matchesAll(text, terms) {
  const f = fold(text)
  return terms.every(t => f.includes(t))
}

/**
 * 在單一章節裡找出所有命中。
 * @returns [{ anchor, where, text, snippet }]  anchor 可能為 null（章首、尚未進任何節）
 */
export function searchChunk(chunk, terms, { secPrefix = '' } = {}) {
  const hits = []
  let counter = 0
  let anchor = null
  let where = ''
  for (const b of chunk.body || []) {
    if (b.t === 'section' || b.t === 'subsection') {
      counter += 1
      const bid = (b.id || '').trim()
      anchor = bid ? `sec-${bid}` : `sec-${secPrefix}-${counter}`
      where = [bid, b.title].filter(Boolean).join(' ')
    }
    const text = blockToText(b)
    if (text && matchesAll(text, terms)) {
      hits.push({ anchor, where, text, snippet: snippet(text, terms) })
    }
  }
  for (const p of chunk.problems || []) {
    const text = [p.body, p.solution].map(bs => (bs || []).map(blockToText).join('\n')).join('\n')
    if (text && matchesAll(text, terms)) {
      hits.push({
        anchor: `prob-${p.num}`,
        where: `Problem ${p.num}`,
        text,
        snippet: snippet(text, terms),
      })
    }
  }
  return hits
}

/**
 * 把 DOM 裡的命中詞包成 <mark class="search-hit">。
 * 跳過 .eq（包進去會毀掉 LaTeX）與已排版的 MathJax 節點；只動文字節點，不重建 DOM。
 * 回傳標記出來的節點數。
 */
export function highlightInDom(root, terms) {
  if (!root || !terms.length) return 0
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT
      const el = node.parentElement
      if (!el || el.closest('.eq, mjx-container, script, style, .qb-inline-math')) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    },
  })
  const targets = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (matchesAny(n.nodeValue, terms)) targets.push(n)
  }
  let count = 0
  for (const node of targets) {
    const text = node.nodeValue
    const spans = markRanges(foldWithMap(text), terms)
    if (!spans.length) continue
    const frag = document.createDocumentFragment()
    let cursor = 0
    for (const [a, b] of spans) {
      if (a < cursor) continue
      if (a > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, a)))
      const mark = document.createElement('mark')
      mark.className = 'search-hit'
      mark.textContent = text.slice(a, b)
      frag.appendChild(mark)
      cursor = b
      count += 1
    }
    if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)))
    node.parentNode.replaceChild(frag, node)
  }
  return count
}

function matchesAny(text, terms) {
  const f = fold(text)
  return terms.some(t => f.includes(t))
}
