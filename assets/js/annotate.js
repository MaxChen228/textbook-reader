/**
 * 畫線標註的 DOM 層：從選取產生標註、把標註貼回正文。
 *
 * 定位策略是「認原文、不認座標」——見 store.js 的 ANNOTATIONS_KEY 段：
 * data/ 每天由 daemon 重生，block 索引與字元位移都會變，只認位置的標註會全部錯位。
 * 這裡的做法是存下 quote 與前後各 32 字，回貼時在該章文字裡回找：
 *   ① 先在 anchor 所屬的區段裡找 quote ⊕ 前後文（最準）
 *   ② 找不到就在整章找第一個 quote（章節被重切時仍能命中）
 *   ③ 再找不到就回報 orphan，由呼叫端列在清單裡（附原文），絕不靜默丟掉
 */

const CONTEXT_CHARS = 32

/** 標註不該落在公式或已排版的數學裡（包進去會毀掉 LaTeX / MathJax 結構）。 */
const SKIP_SELECTOR = '.eq, mjx-container, script, style, .qb-inline-math'

function textNodesIn(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue) return NodeFilter.FILTER_REJECT
      const el = node.parentElement
      if (!el || el.closest(SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    },
  })
  const out = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) out.push(n)
  return out
}

/** 把整棵子樹攤平成「文字 + 每個字元屬於哪個節點的哪一格」。 */
function flatten(root) {
  const nodes = textNodesIn(root)
  let text = ''
  const index = []   // [nodeIdx, offsetInNode] per char
  nodes.forEach((n, ni) => {
    const v = n.nodeValue
    for (let i = 0; i < v.length; i++) index.push([ni, i])
    text += v
  })
  return { nodes, text, index }
}

/** 目前選取 → 標註草稿（不含 id）。選取範圍不在 root 內、或落在數學裡就回 null。 */
export function captureSelection(root) {
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null
  const range = sel.getRangeAt(0)
  if (!root.contains(range.commonAncestorContainer)) return null
  const quote = String(sel).replace(/\s+/g, ' ').trim()
  if (quote.length < 2) return null
  if (range.commonAncestorContainer.parentElement?.closest(SKIP_SELECTOR)) return null

  const { text } = flatten(root)
  const flatQuote = quote
  const at = text.replace(/\s+/g, ' ').indexOf(flatQuote)
  const flat = text.replace(/\s+/g, ' ')
  return {
    quote,
    before: at > 0 ? flat.slice(Math.max(0, at - CONTEXT_CHARS), at) : '',
    after: at >= 0 ? flat.slice(at + flatQuote.length, at + flatQuote.length + CONTEXT_CHARS) : '',
    anchor: nearestAnchor(range.startContainer),
  }
}

/** 選取起點往上找最近的節錨（h2/h3 的 id 或題目 id），供跳轉與回找定位。 */
function nearestAnchor(node) {
  let el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement
  while (el && el.classList && !el.classList.contains('article')) {
    if (el.id) return el.id
    // 同層往前找最近的標題
    let prev = el.previousElementSibling
    while (prev) {
      if (prev.id && (prev.classList.contains('sec-h') || prev.id.startsWith('prob-'))) return prev.id
      prev = prev.previousElementSibling
    }
    el = el.parentElement
  }
  return null
}

/** 在攤平文字裡找 quote：先用前後文擇一，找不到退回第一個出現處。回傳 [start,end] 或 null。 */
function locate(flatText, item) {
  const q = (item.quote || '').replace(/\s+/g, ' ').trim()
  if (!q) return null
  const withContext = `${item.before || ''}${q}${item.after || ''}`
  let at = withContext.length > q.length ? flatText.indexOf(withContext) : -1
  if (at >= 0) return [at + (item.before || '').length, at + (item.before || '').length + q.length]
  at = flatText.indexOf(q)
  return at >= 0 ? [at, at + q.length] : null
}

/**
 * 把標註貼回正文。
 * @returns {{applied: string[], orphans: string[]}} 命中與找不到原文的標註 id
 */
export function applyAnnotations(root, items) {
  const applied = []
  const orphans = []
  if (!root || !items.length) return { applied, orphans }

  for (const item of items) {
    if (!item.quote) continue                       // 書籤沒有原文可標
    // 每貼一筆就重新攤平：上一筆插入的 <mark> 會改變節點結構
    const { nodes, index } = flatten(root)
    let text = ''
    const flatIndex = []                            // 壓縮空白後的座標 → 原座標
    let prevSpace = false
    for (let i = 0; i < index.length; i++) {
      const [ni, off] = index[i]
      const ch = nodes[ni].nodeValue[off]
      const isSpace = /\s/.test(ch)
      if (isSpace) {
        if (prevSpace) continue
        text += ' '
      } else {
        text += ch
      }
      flatIndex.push(i)
      prevSpace = isSpace
    }
    flatIndex.push(index.length)   // 尾哨兵：命中一路到結尾時，區間終點才查得到（否則少標一個字）
    const found = locate(text, item)
    if (!found) { orphans.push(item.id); continue }
    const from = flatIndex[found[0]]
    const to = flatIndex[found[1]]
    if (from == null || to == null) { orphans.push(item.id); continue }
    if (wrapRange(nodes, index, from, to, item)) applied.push(item.id)
    else orphans.push(item.id)
  }
  return { applied, orphans }
}

/** 用 Range 把 [from,to) 這段字元包成 <mark class="annot">。 */
function wrapRange(nodes, index, from, to, item) {
  const start = index[from]
  const end = index[to - 1]
  if (!start || !end) return false
  const range = document.createRange()
  range.setStart(nodes[start[0]], start[1])
  range.setEnd(nodes[end[0]], end[1] + 1)
  const mark = document.createElement('mark')
  mark.className = 'annot' + (item.note ? ' has-note' : '')
  mark.dataset.annot = item.id
  if (item.note) mark.title = item.note
  try {
    range.surroundContents(mark)          // 選取跨多個節點時會丟例外
    return true
  } catch {
    // 跨節點：改成 extract + insert（會把中間的行內標籤一起搬進 mark，可接受）
    try {
      mark.appendChild(range.extractContents())
      range.insertNode(mark)
      return true
    } catch { return false }
  }
}

/** 移除正文裡的畫線（刪標註或重貼前）。 */
export function clearAnnotations(root) {
  root?.querySelectorAll('mark.annot').forEach(m => {
    const parent = m.parentNode
    while (m.firstChild) parent.insertBefore(m.firstChild, m)
    parent.removeChild(m)
    parent.normalize()
  })
}
