/**
 * 區塊資料模型 → HTML / 純文字 的**單一真相**。
 *
 * 資料模型定義在 textbooks/corpus.py：chunk.body 是 block 陣列，block 以 `t` 分型
 * （section/subsection/p/eq/fig/table/example）。reader（index.html）與題庫
 * （problems.html）都要把同一組 block 畫出來，過去兩邊各養一份實作，已經漂移出
 * 實質差異：題庫版少了圖片 w/h 版位預留（→ 有 CLS）、表格轉純文字時整個表身被丟掉、
 * eq 轉文字少了 $$ 包裹。此模組把兩份合併成一份（取語意較完整的那一版），
 * 兩頁一律 import，不再各自維護。
 *
 * 純函式、不碰全域狀態：所有情境（slug、雙語、章節前綴）都由 opts 傳入。
 * 唯一的環境依賴是 tableToText 用 DOM 解析表格 HTML（僅在瀏覽器裡呼叫）。
 */
import { safeHtml as esc, escapeAttr as escAttr, renderMarkdown } from './shared.js'

/** 版心寬度上限（px）：圖片預留版位時的內聯寬度封頂，與 .article figure img 的 max-width 對齊。 */
const IMG_MAX_W = 480

/** eq 的 \tag：資料有 label 且 tex 本身沒寫 \tag 時才補。 */
function eqTag(b) {
  return b.label && !/\\tag\s*\{/.test(b.tex || '') ? ` \\tag{${b.label}}` : ''
}

function attrId(b) {
  return b.id ? ` id="${escAttr(b.id)}"` : ''
}

// ── 雙語（lang='bi'）：英文為主、中文摺疊在 <details> 裡 ──────────────
function biPanel(html) {
  return `<div class="bi-panel">${html}</div>`
}

/** 標題/圖說類：中文單獨一顆可展開的小面板（英文本體在外面另外畫）。 */
export function biReveal(text) {
  if (!text) return ''
  return `<details class="bi-reveal"><summary></summary>${biPanel(renderMarkdown(text))}</details>`
}

/** 段落類：英文本體當 summary，展開才看到中文。 */
function biBlock(enHtml, zhText) {
  if (!zhText) return enHtml
  return `<details class="bi-block"><summary>${enHtml}</summary>${biPanel(renderMarkdown(zhText))}</details>`
}

/**
 * 單一 block → HTML。
 * @param {object} b     block
 * @param {object} opts  { slug, bi }
 *   slug — 圖片路徑 `img/<slug>/…` 的來源；未給則圖片不畫（避免畫出壞連結）
 *   bi   — 雙語模式，附上中文摺疊面板
 */
export function renderBlock(b, opts = {}) {
  const { slug = '', bi = false } = opts
  switch (b.t) {
    case 'example':
      return `<div class="example-marker">Example ${esc(b.id)}</div>`
    case 'p': {
      const en = `<p${attrId(b)}>${renderMarkdown(b.md || '')}</p>`
      return bi ? biBlock(en, b.md_zh) : en
    }
    case 'eq':
      return `<div class="eq"${attrId(b)} data-tex="${escAttr(`$$${b.tex || ''}$$`)}" title="點擊複製 LaTeX">$$${b.tex || ''}${eqTag(b)}$$</div>`
    case 'fig': {
      if (!b.src) return ''
      const cap = b.caption ? `<figcaption>${esc(b.caption)}</figcaption>` : ''
      const kind = b.kind ? ` data-kind="${escAttr(b.kind)}"` : ''
      // w/h 由 bake 從 webp 檔頭讀進來：有了它瀏覽器排版時就先留好版位，圖片載入
      // 不再把下方整段文字往下推（CLS）。內聯 width（不超過版心上限）+ aspect-ratio
      // 缺一不可：只給 width/height 屬性而 CSS 是 width:auto 時，未載入的圖不佔任何空間。
      // 舊資料沒這兩欄 → 退回原行為（會有 CLS，但不會壞）。
      const dim = (b.w && b.h)
        ? ` width="${escAttr(b.w)}" height="${escAttr(b.h)}" style="width:min(100%,${Math.min(b.w, IMG_MAX_W)}px);aspect-ratio:${b.w}/${b.h}"`
        : ''
      const fig = `<figure${attrId(b)}${kind}><img src="img/${escAttr(slug)}/${escAttr(b.src)}"${dim} loading="lazy" decoding="async" alt="${escAttr(b.caption || '')}">${cap}</figure>`
      return fig + (bi ? biReveal(b.caption_zh) : '')
    }
    case 'table': {
      const cap = b.caption ? `<div class="tbl-cap">${esc(b.caption)}</div>` : ''
      const fn = b.footnote ? `<div class="tbl-fn">${esc(b.footnote)}</div>` : ''
      // 表格 HTML 在 bake 時已用 nh3 消毒過；此處只把相對圖片路徑改寫到本書圖庫。
      const html = String(b.html || '').replace(/src="images\//g, `src="img/${escAttr(slug)}/`)
      const zh = [b.caption_zh, b.footnote_zh].filter(Boolean).join('\n\n')
      return `<div class="tbl"${attrId(b)}>${cap}${html}${fn}</div>${bi ? biReveal(zh) : ''}`
    }
  }
  return ''
}

/**
 * block 陣列 → HTML。
 * @param {object} opts { slug, bi, secPrefix, headings, empty }
 *   secPrefix — 沒有 id 的節標題用來合成錨點：`sec-<secPrefix>-<第幾個>`
 *   headings  — false 時 section/subsection 不畫成 h2/h3（題目內文用不到章節標題階層）
 *   empty     — 陣列為空時的替代 HTML
 */
export function renderBlocks(blocks, opts = {}) {
  const { secPrefix = '', headings = true, empty = '' } = opts
  const list = blocks || []
  if (!list.length) return empty
  let counter = 0
  return list.map((b) => {
    if (b.t === 'section' || b.t === 'subsection') {
      if (!headings) return ''
      counter += 1
      const bid = (b.id || '').trim()
      const anchor = bid ? `sec-${bid}` : `sec-${secPrefix}-${counter}`
      const tag = b.t === 'section' ? 'h2' : 'h3'
      const num = bid ? `<span class="sec-num">${esc(bid)}</span> ` : ''
      const zh = opts.bi ? biReveal(b.title_zh) : ''
      return `<${tag} id="${escAttr(anchor)}" class="sec-h">${num}${esc(b.title)}</${tag}>${zh}`
    }
    return renderBlock(b, opts)
  }).join('')
}

// ── block → 純文字（供複製 / Markdown 匯出 / 列印）─────────────────────
// 鏡像 build/bake_json._block_text 的語意；表格連表身一起轉，複製出去才是完整內容。

export function tableToText(html) {
  if (typeof document === 'undefined') return ''
  const tmp = document.createElement('div')
  tmp.innerHTML = html || ''
  const rows = [...tmp.querySelectorAll('tr')]
  if (!rows.length) return tmp.textContent.trim()
  return rows.map(r => [...r.children].map(c => c.textContent.trim()).join('\t')).join('\n')
}

export function blockToText(b) {
  switch (b.t) {
    case 'section': case 'subsection': {
      const id = (b.id || '').trim()
      return (id ? id + ' ' : '') + (b.title || '')
    }
    case 'example': return `Example ${b.id || ''}`.trim()
    case 'p': return b.md || ''
    case 'eq': return `$$${b.tex || ''}${eqTag(b)}$$`
    case 'fig': return b.caption ? `[Figure] ${b.caption}` : '[Figure]'
    case 'table': return [b.caption || '', tableToText(b.html), b.footnote || ''].filter(Boolean).join('\n')
  }
  return ''
}

export function blocksToText(blocks) {
  return (blocks || []).map(blockToText).filter(Boolean).join('\n\n')
}

export function problemsToText(probs) {
  if (!probs || !probs.length) return ''
  const out = ['Problems']
  probs.forEach(p => {
    let s = `Problem ${p.num}`
    const body = blocksToText(p.body)
    if (body) s += '\n' + body
    const sol = blocksToText(p.solution)
    if (sol) s += '\nSolution\n' + sol
    out.push(s)
  })
  return out.join('\n\n')
}
