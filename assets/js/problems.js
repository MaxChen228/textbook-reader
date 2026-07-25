/**
 * 題庫頁（problems.html）的應用層：分類樹、虛擬捲動列表、搜尋、選取匯出、單題詳情。
 * 區塊渲染與純文字轉換一律用 blocks.js（與 reader 同一份），別在這裡重寫。
 */
import S from './shared.js'
import { renderBlocks, blocksToText } from './blocks.js'

const esc = S.escapeHtml
const escAttr = S.escapeAttr

/** 題目沒有任何 block（OCR 漏抓）時的替代內容。 */
const EMPTY_BODY_HTML = '<p>OCR missing - see original PDF.</p>'
const blocksHtml = (blocks, slug) => renderBlocks(blocks, { slug, empty: EMPTY_BODY_HTML })

const RENDER_CAP = 4000   // grid 格覽一次最多渲染的格塊數（grid 無虛擬捲動）
const ROW_H = 150         // list 虛擬捲動的固定列高（卡片 138 + 間距 12）
let allProblems = []
let filtered = []
let displayed = []
let listScroll = null     // 目前綁在 #list-view 的捲動處理器（切換/重渲時解綁）
let listRange = [-1, -1]  // 上次已掛載的可見列區間，避免無變化的重繪
let activeBook = 'all'
let activeField = 'all'
let activeChapter = 'all'
let expandedFields = new Set()
let expandedBooks = new Set()
let activeSolution = 'all'
let searchQ = ''
let searchProgress = null   // 「搜尋語料載入中 n/N 本」的進度字串
let viewMode = localStorage.getItem('textbook.problems.view') || 'grid'
let selectedIds = new Set()
let suppressClickUntil = 0

S.bindSidebarDrawer()

function setError(message) {
  const el = document.getElementById('fetch-error')
  el.textContent = message || ''
  el.classList.toggle('visible', Boolean(message))
}

function label(value) {
  return value || 'Unknown'
}

function textPreview(p) {
  const t = (p.question_text || '').replace(/\s+/g, ' ').trim()
  if (t) return t
  // 分片還沒到 ≠ 這題沒內容 → 不可誤報成 OCR missing
  return p.preview_loaded ? 'OCR missing - see original PDF' : '…'
}

function chip(container, text, value, active, onClick, count = null) {
  container.appendChild(S.createChip({ text, value, active, count, onClick }))
}

function countsBy(items, fn) {
  const out = new Map()
  items.forEach(item => {
    const key = fn(item) || 'Unknown'
    out.set(key, (out.get(key) || 0) + 1)
  })
  return [...out.entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0])))
}

// 巢狀分類樹（與 library 同 SoT）：Field → sublist → book，依 frank/srank 排序。
function libraryTree() {
  const fields = new Map()
  allProblems.forEach(p => {
    let f = fields.get(p.field_id)
    if (!f) { f = { field: p.field, field_id: p.field_id, frank: p.frank, count: 0, subs: new Map() }; fields.set(p.field_id, f) }
    f.count++
    const sname = p.sublist || '其他'
    let s = f.subs.get(sname)
    if (!s) { s = { name: sname, srank: p.srank, books: new Map() }; f.subs.set(sname, s) }
    const title = p.book_title || p.book_slug || 'Unknown'
    let bk = s.books.get(title)
    if (!bk) { bk = { title, slug: p.book_slug || '', count: 0 }; s.books.set(title, bk) }
    bk.count++
  })
  const farr = [...fields.values()].sort((a, b) => a.frank - b.frank || a.field.localeCompare(b.field))
  farr.forEach(f => {
    f.sublists = [...f.subs.values()].sort((a, b) => a.srank - b.srank || a.name.localeCompare(b.name))
    f.sublists.forEach(s => { s.bookArr = [...s.books.values()].sort((a, b) => a.title.localeCompare(b.title)) })
  })
  return farr
}

function buildBookTree(bookEl) {
  bookEl.innerHTML = ''
  const allRow = document.createElement('button')
  allRow.type = 'button'
  allRow.className = 'lib-tree-all' + (activeBook === 'all' && activeField === 'all' ? ' active' : '')
  allRow.innerHTML = `<span class="lib-field-name">All Books</span><span class="lib-tree-count">${allProblems.length}</span>`
  allRow.onclick = () => { activeBook = 'all'; activeField = 'all'; activeChapter = 'all'; render() }
  bookEl.appendChild(allRow)

  libraryTree().forEach(f => {
    const hasActiveBook = f.sublists.some(s => s.bookArr.some(b => b.title === activeBook))
    const open = expandedFields.has(f.field_id) || (activeField === f.field_id && activeBook === 'all') || hasActiveBook
    const head = document.createElement('button')
    head.type = 'button'
    head.className = 'lib-field' + (activeField === f.field_id && activeBook === 'all' ? ' active' : '') + (open ? ' open' : '')
    head.innerHTML = `<span class="lib-field-chev">▸</span><span class="lib-field-name">${esc(f.field)}</span><span class="lib-tree-count">${f.count}</span>`
    head.onclick = () => {
      if (expandedFields.has(f.field_id)) expandedFields.delete(f.field_id)
      else expandedFields.add(f.field_id)
      activeField = f.field_id; activeBook = 'all'; activeChapter = 'all'
      render()
    }
    bookEl.appendChild(head)
    if (!open) return
    const body = document.createElement('div')
    body.className = 'lib-field-body'
    f.sublists.forEach(s => {
      const sl = document.createElement('div')
      sl.className = 'lib-sub-label'
      sl.textContent = s.name
      body.appendChild(sl)
      s.bookArr.forEach(b => {
        const open = expandedBooks.has(b.title) || activeBook === b.title
        body.appendChild(bookPick(b, activeBook === b.title, () => {
          if (expandedBooks.has(b.title)) expandedBooks.delete(b.title)
          else expandedBooks.add(b.title)
          activeBook = b.title; activeChapter = 'all'; render()
        }))
        if (!open) return
        const chBox = document.createElement('div')
        chBox.className = 'lib-ch-body'
        chBox.appendChild(chapterRow('All chapters', b.count, activeBook === b.title && activeChapter === 'all',
          () => { activeBook = b.title; activeChapter = 'all'; render() }))
        bookChapters(b.title).forEach(c => {
          const key = `Ch ${c.chapter}`
          chBox.appendChild(chapterRow(`Ch ${c.chapter}${c.title ? ' · ' + c.title : ''}`, c.count,
            activeBook === b.title && activeChapter === key,
            () => { activeBook = b.title; activeChapter = key; render() }))
        })
        body.appendChild(chBox)
      })
    })
    bookEl.appendChild(body)
  })
}

function bookChapters(title) {
  const m = new Map()  // chapter -> {chapter, title, count}
  allProblems.forEach(p => {
    if ((p.book_title || p.book_slug) !== title) return
    const e = m.get(p.chapter) || { chapter: p.chapter, title: p.chapter_title || '', count: 0 }
    e.count++
    m.set(p.chapter, e)
  })
  return [...m.values()].sort((a, b) => (a.chapter || 0) - (b.chapter || 0))
}

function chapterRow(text, count, active, onClick) {
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'lib-ch' + (active ? ' active' : '')
  btn.innerHTML = `<span class="lib-ch-name">${esc(text)}</span><span class="lib-tree-count">${count}</span>`
  btn.addEventListener('click', onClick)
  return btn
}

function bookPick({ title, slug, count, glyph }, active, onClick) {
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'book-pick' + (active ? ' active' : '')
  const cover = slug
    ? `<img src="img/${escAttr(slug)}/cover.webp" alt="" loading="lazy" onerror="this.remove()">`
    : (glyph || '')
  btn.innerHTML = `<span class="book-pick-cover">${cover}</span>` +
    `<span class="book-pick-title">${esc(title)}</span>` +
    `<span class="book-pick-count">${count}</span>`
  btn.addEventListener('click', onClick)
  return btn
}

function buildFilters() {
  const bookEl = document.getElementById('filter-book')
  const solEl = document.getElementById('filter-solution')
  solEl.innerHTML = ''

  buildBookTree(bookEl)

  chip(solEl, 'All', 'all', activeSolution === 'all', () => { activeSolution = 'all'; render() }, allProblems.length)
  chip(solEl, 'With solution', 'yes', activeSolution === 'yes', () => { activeSolution = 'yes'; render() },
    allProblems.filter(p => p.has_solution).length)
  chip(solEl, 'No solution', 'no', activeSolution === 'no', () => { activeSolution = 'no'; render() },
    allProblems.filter(p => !p.has_solution).length)
}

function matches(p) {
  if (activeBook !== 'all' && p.book_title !== activeBook) return false
  if (activeBook === 'all' && activeField !== 'all' && p.field_id !== activeField) return false
  if (activeChapter !== 'all' && `Ch ${p.chapter}` !== activeChapter) return false
  if (activeSolution === 'yes' && !p.has_solution) return false
  if (activeSolution === 'no' && p.has_solution) return false
  if (searchQ) {
    const hay = [
      p.id, p.num, p.book_title, p.chapter_title, p.question_text, p.solution_text, p.subject,
    ].join(' ').toLowerCase()
    if (!hay.includes(searchQ)) return false
  }
  return true
}

function render() {
  buildFilters()
  filtered = allProblems.filter(matches)
  updateSearchCorpusNote()   // 必須在「無結果就 return」之前：沒結果往往正是內文還沒載入
  const isGrid = viewMode === 'grid'
  // list 走虛擬捲動 → 無上限；grid 無虛擬化 → 仍設上限防 6 萬格塊凍結
  const capped = isGrid && filtered.length > RENDER_CAP
  displayed = capped ? filtered.slice(0, RENDER_CAP) : filtered
  const countText = capped ? `${RENDER_CAP}/${filtered.length} 顯示` : `${filtered.length}/${allProblems.length}`
  document.getElementById('result-count').textContent =
    countText + (searchProgress ? ` · 語料 ${searchProgress}` : '')
  const list = document.getElementById('problem-list')
  const grid = document.getElementById('problem-grid')
  document.getElementById('loading').style.display = 'none'
  document.getElementById('empty').style.display = filtered.length ? 'none' : ''
  list.style.display = isGrid ? 'none' : 'block'
  grid.style.display = isGrid ? 'block' : 'none'
  if (!filtered.length) {
    list.innerHTML = ''
    list.style.height = '0px'
    grid.innerHTML = ''
    return
  }
  if (isGrid) {
    renderGrid()
    if (capped) {
      const note = document.createElement('div')
      note.style.cssText = 'padding:14px;color:var(--sub);font-family:var(--mono);font-size:11px;text-align:center'
      note.textContent = `顯示前 ${RENDER_CAP} / 共 ${filtered.length} 題 — 用左側篩選或搜尋收斂`
      grid.prepend(note)
    }
  } else {
    renderList()
  }
  syncRenderedSelection()
}

// 索引不含題目內文（preview 在各書分片）。使用者打字搜尋時：
//  · 範圍已收斂到某一本 → 已自動載入，不打擾
//  · 範圍是領域/全部 → 顯示「要不要載入內文語料（約 N MB）」，避免無預警吃掉幾十 MB
function pendingSearchScope() {
  const scope = allProblems.filter((p) => {
    if (activeBook !== 'all') return p.book_title === activeBook
    if (activeField !== 'all') return p.field_id === activeField
    return true
  })
  const slugs = new Set()
  let problems = 0
  scope.forEach((p) => { if (!p.preview_loaded) { slugs.add(p.book_slug); problems += 1 } })
  return { books: slugs.size, mb: Math.max(1, Math.round(problems * 170 / 1e6)) }
}

function updateSearchCorpusNote() {
  const note = document.getElementById('search-corpus-note')
  if (!searchQ || searchPriming) { note.hidden = true; return }
  const { books, mb } = pendingSearchScope()
  if (!books) { note.hidden = true; return }
  // 範圍已收斂到單一本書（不論是打字時就選好、還是查詢中途才收斂）→ 直接載，不打擾
  if (activeBook !== 'all') { note.hidden = true; primeSearchCorpus(); return }
  note.hidden = false
  note.innerHTML = ''
  const msg = document.createElement('span')
  msg.textContent = `目前只搜到書名／章名／題號。還有 ${books} 本書的題目內文尚未載入（約 ${mb} MB）。`
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.textContent = '載入內文一起搜'
  btn.onclick = () => { note.hidden = true; primeSearchCorpus() }
  note.append(msg, btn)
}

function openProblem(id) {
  location.hash = encodeURIComponent(id)
}

// 虛擬捲動清單：畫布高 = 列數×ROW_H，只掛載視窗 ±BUFFER 列的卡片並對其 typeset LaTeX。
// DOM 與 MathJax 工作量恆定（~視窗列數），與總題數無關 → 6 萬題也不卡。
const LIST_BUFFER = 5
function renderList() {
  const scroller = document.getElementById('list-view')
  const list = document.getElementById('problem-list')
  const canvasH = displayed.length * ROW_H + 14
  list.style.height = `${canvasH}px`
  const maxScroll = Math.max(0, canvasH - (scroller.clientHeight || 600))
  if (scroller.scrollTop > maxScroll) scroller.scrollTop = maxScroll  // 篩選變短時別捲到空白
  listRange = [-1, -1]
  if (!listScroll) {
    let ticking = false
    listScroll = () => {
      if (ticking) return
      ticking = true
      requestAnimationFrame(() => { ticking = false; paintList() })
    }
    scroller.addEventListener('scroll', listScroll, { passive: true })
  }
  paintList()
}

function paintList() {
  if (viewMode !== 'list') return
  const scroller = document.getElementById('list-view')
  const list = document.getElementById('problem-list')
  const top = scroller.scrollTop
  const vh = scroller.clientHeight || 600
  const start = Math.max(0, Math.floor(top / ROW_H) - LIST_BUFFER)
  const end = Math.min(displayed.length, Math.ceil((top + vh) / ROW_H) + LIST_BUFFER)
  if (start === listRange[0] && end === listRange[1]) return  // 視窗未變 → 不重繪
  listRange = [start, end]
  let html = ''
  for (let i = start; i < end; i++) html += cardHtml(displayed[i], 14 + i * ROW_H, i)
  // 丟掉舊卡前先清其 MathJax 內部節點引用 → 長時間捲動不累積記憶體
  if (window.MathJax && MathJax.typesetClear) { try { MathJax.typesetClear([list]) } catch (e) { /* noop */ } }
  list.innerHTML = html
  list.querySelectorAll('.problem-card').forEach(card => {
    const p = displayed[+card.dataset.i]
    card.addEventListener('click', (e) => {
      if (Date.now() < suppressClickUntil) { e.preventDefault(); return }
      if (e.target.closest('input, label, button')) return
      e.preventDefault()
      if (e.metaKey || e.ctrlKey) toggleIdSelection(p.id)
      else openProblem(p.id)
    })
    const cb = card.querySelector('.problem-card-cb')
    cb.addEventListener('click', (e) => e.stopPropagation())
    cb.addEventListener('change', (e) => setIdSelection(p.id, e.target.checked))
  })
  S.renderMath(list)   // 只 typeset 已掛載（可見）的卡 → 工作量恆定
  // 可見卡的 preview 若還沒到 → 抓該書分片，回來後重畫這段視窗
  ensurePreviews(displayed.slice(start, end), () => { listRange = [-1, -1]; paintList() })
}

function cardHtml(p, top, i) {
  const sel = selectedIds.has(p.id)
  return `<a class="problem-card${sel ? ' selected' : ''}" style="top:${top}px" data-i="${i}"
      href="problems.html#${encodeURIComponent(p.id)}" data-id="${escAttr(p.id)}">
    <div class="problem-card-head">
      <span class="problem-card-main">
        <input class="problem-card-cb" type="checkbox"${sel ? ' checked' : ''} aria-label="選取 ${escAttr(p.num)}">
        <span class="problem-card-id">Problem ${esc(p.num)}</span>
      </span>
      <span>${p.has_solution ? 'solution' : 'no solution'}</span>
    </div>
    <div class="problem-card-book">${esc(p.book_title || p.book_slug)}</div>
    <div class="problem-card-preview">${S.renderMarkdown(textPreview(p))}</div>
    <div class="problem-card-meta">
      <span class="pill">${esc(label(p.subject))}</span>
      <span class="pill">Ch ${esc(p.chapter)} · ${esc(p.chapter_title || '')}</span>
      ${p.has_solution ? '<span class="pill ok">solution</span>' : ''}
    </div>
  </a>`
}

function renderGrid() {
  const grid = document.getElementById('problem-grid')
  ensurePreviews(displayed, renderGrid)
  const groups = new Map()
  displayed.forEach(p => {
    const key = p.book_title || p.book_slug || 'Unknown'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(p)
  })
  grid.innerHTML = [...groups.entries()].map(([book, items]) => {
    const first = items[0] || {}
    const byChapter = new Map()
    items.forEach(p => {
      const key = `Ch ${p.chapter}`
      if (!byChapter.has(key)) byChapter.set(key, [])
      byChapter.get(key).push(p)
    })
    const blocks = [...byChapter.entries()].map(([chapter, probs]) => `
      <div class="grid-section">
        <div class="grid-section-header">
          <span class="grid-section-title">${esc(chapter)}</span>
          <span class="grid-section-sub">${esc(probs[0]?.chapter_title || '')}</span>
          <span class="grid-section-count">${probs.length} 題</span>
        </div>
        <div class="grid-blocks">
          ${probs.map(p => `<a class="grid-block ${p.has_solution ? 'has-solution' : 'no-solution'}" href="problems.html#${encodeURIComponent(p.id)}" data-id="${escAttr(p.id)}">${esc(p.num)}</a>`).join('')}
        </div>
      </div>`).join('')
    return `<section class="grid-book">
      <div class="grid-section-header">
        <span class="grid-section-title">${esc(book)}</span>
        <span class="grid-section-sub">${esc(label(first.subject))}</span>
        <span class="grid-section-count">${items.length} 題</span>
      </div>
      ${blocks}
    </section>`
  }).join('')
  grid.querySelectorAll('.grid-block').forEach(block => {
    block.addEventListener('click', (e) => {
      if (Date.now() < suppressClickUntil) { e.preventDefault(); return }
      e.preventDefault()
      const id = block.dataset.id
      if (e.metaKey || e.ctrlKey) toggleIdSelection(id)
      else openProblem(id)
    })
  })
}

function setViewMode(mode) {
  viewMode = mode === 'list' ? 'list' : 'grid'
  localStorage.setItem('textbook.problems.view', viewMode)
  document.body.classList.toggle('grid-mode', viewMode === 'grid')
  document.getElementById('btn-view-list').classList.toggle('active', viewMode === 'list')
  document.getElementById('btn-view-grid').classList.toggle('active', viewMode === 'grid')
}

document.getElementById('btn-view-list').addEventListener('click', () => {
  if (viewMode === 'list') return
  setViewMode('list')
  render()
})
document.getElementById('btn-view-grid').addEventListener('click', () => {
  if (viewMode === 'grid') return
  setViewMode('grid')
  render()
})

function updateSelBar() {
  const n = selectedIds.size
  document.getElementById('sel-bar-count').textContent = `已選 ${n} 題`
  document.getElementById('sel-bar').classList.toggle('visible', n > 0)
}

function syncRenderedSelection() {
  document.querySelectorAll('.problem-card').forEach(card => {
    const on = selectedIds.has(card.dataset.id)
    card.classList.toggle('selected', on)
    const cb = card.querySelector('.problem-card-cb')
    if (cb) cb.checked = on
  })
  document.querySelectorAll('.grid-block').forEach(block => {
    block.classList.toggle('selected', selectedIds.has(block.dataset.id))
  })
  updateSelBar()
}

function setIdSelection(id, selected) {
  if (selected) selectedIds.add(id)
  else selectedIds.delete(id)
  syncRenderedSelection()
}

function toggleIdSelection(id) {
  setIdSelection(id, !selectedIds.has(id))
}

function selectedProblems() {
  return allProblems.filter(p => selectedIds.has(p.id))
}

function selectedMarkdown() {
  const incQ = document.getElementById('sel-inc-q').checked
  const incSol = document.getElementById('sel-inc-sol').checked
  return selectedProblems().map(p => {
    const parts = [`## ${p.book_title} - Problem ${p.num}`]
    parts.push(`\`${[label(p.subject), `Ch ${p.chapter}`, p.id].join(' · ')}\``)
    if (incQ) parts.push('', p.question_text || '')
    if (incSol && p.solution_text) parts.push('', '### Solution', p.solution_text)
    return parts.join('\n')
  }).join('\n\n---\n\n')
}

document.getElementById('sel-clear').addEventListener('click', () => {
  selectedIds.clear()
  syncRenderedSelection()
})
document.getElementById('sel-copy').addEventListener('click', async (e) => {
  const btn = e.currentTarget
  await Promise.all(selectedProblems().map(ensureBlocks))
  copyText(selectedMarkdown(), btn)
})
document.getElementById('sel-pdf').addEventListener('click', async () => {
  const problems = selectedProblems()
  if (!problems.length) return
  await Promise.all(problems.map(ensureBlocks))
  const incQ = document.getElementById('sel-inc-q').checked
  const incSol = document.getElementById('sel-inc-sol').checked
  const pageBreak = document.getElementById('pdf-pagebreak').checked
  const body = problems.map((p, i) => {
    const breakStyle = pageBreak && i < problems.length - 1 ? ' style="page-break-after:always"' : ''
    return `<section class="q-item"${breakStyle}>
      <div class="q-id">${esc(p.book_title)} · Problem ${esc(p.num)}</div>
      ${incQ ? `<div class="sec-label">Problem</div>${blocksHtml(p.body, p.book_slug)}` : ''}
      ${incSol && p.has_solution ? `<div class="sec-label">Solution</div>${blocksHtml(p.solution, p.book_slug)}` : ''}
    </section>`
  }).join(pageBreak ? '' : '<hr class="q-sep">')
  S.openPrintWindow({
    title: 'Textbook Problems',
    bodyHtml: body,
    extraStyles: [
      S.printTypographyCss(),
      'body{font-family:var(--serif);font-size:13px;color:#1a1a18;padding:24px 32px;line-height:1.9;}',
      '.q-item{margin-bottom:32px;}.q-id{font-family:monospace;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#7a756c;margin-bottom:8px;}',
      '.sec-label{font-family:monospace;font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#7a756c;margin-top:20px;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #dbd6cd;}',
      '.q-sep{border:none;border-top:1px solid #dbd6cd;margin:24px 0;}img{max-width:100%;display:block;margin:12px auto;}.eq{overflow-x:auto;padding:10px 0;}',
      '@media print{body{padding:0;}}',
    ].join('\n'),
  })
})

function setupDragSelect() {
  const overlay = document.getElementById('drag-select-box')
  const area = document.getElementById('list-view')
  const state = { active: false, moved: false, x: 0, y: 0, hits: new Set(), base: new Set(), add: false }
  const selector = () => viewMode === 'grid' ? '.grid-block' : '.problem-card'
  const intersects = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top
  const clearHits = () => document.querySelectorAll('.drag-hit').forEach(el => el.classList.remove('drag-hit'))

  area.addEventListener('mousedown', (e) => {
    if (document.getElementById('app').dataset.mode !== 'list') return
    if (e.button !== 0 || e.target.closest('input, button, label')) return
    state.active = true
    state.moved = false
    state.x = e.clientX
    state.y = e.clientY
    state.hits.clear()
    state.base = new Set(selectedIds)
    state.add = e.metaKey || e.ctrlKey
    document.body.classList.add('is-drag-selecting')
  })
  window.addEventListener('mousemove', (e) => {
    if (!state.active) return
    const dx = e.clientX - state.x
    const dy = e.clientY - state.y
    if (!state.moved && Math.hypot(dx, dy) < 6) return
    state.moved = true
    e.preventDefault()
    const left = Math.min(state.x, e.clientX)
    const top = Math.min(state.y, e.clientY)
    const rect = { left, top, right: Math.max(state.x, e.clientX), bottom: Math.max(state.y, e.clientY) }
    overlay.style.display = 'block'
    overlay.style.left = `${left}px`
    overlay.style.top = `${top}px`
    overlay.style.width = `${Math.abs(dx)}px`
    overlay.style.height = `${Math.abs(dy)}px`
    state.hits.clear()
    clearHits()
    document.querySelectorAll(selector()).forEach(el => {
      if (intersects(rect, el.getBoundingClientRect())) {
        el.classList.add('drag-hit')
        if (el.dataset.id) state.hits.add(el.dataset.id)
      }
    })
  }, { passive: false })
  window.addEventListener('mouseup', () => {
    if (!state.active) return
    overlay.style.display = 'none'
    clearHits()
    document.body.classList.remove('is-drag-selecting')
    if (state.moved) {
      suppressClickUntil = Date.now() + 180
      selectedIds = state.add ? new Set([...state.base, ...state.hits]) : new Set(state.hits)
      syncRenderedSelection()
    }
    state.active = false
  })
}

function problemMarkdown(p) {
  const parts = [
    `## ${p.book_title} - Problem ${p.num}`,
    '',
    p.question_text || '',
  ]
  if (p.solution_text) parts.push('', '### Solution', p.solution_text)
  return parts.join('\n')
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const old = btn.textContent
    btn.textContent = 'Copied'
    btn.classList.add('copied')
    setTimeout(() => { btn.textContent = old; btn.classList.remove('copied') }, 1200)
  })
}

function printProblem(p) {
  S.openPrintWindow({
    title: `${p.book_title} - Problem ${p.num}`,
    bodyHtml: `<h1>Problem ${esc(p.num)}</h1>${blocksHtml(p.body, p.book_slug)}${p.has_solution ? `<h2>Solution</h2>${blocksHtml(p.solution, p.book_slug)}` : ''}`,
    extraStyles: [
      S.printTypographyCss(),
      'body{font-family:var(--serif);font-size:14px;line-height:1.9;max-width:820px;margin:0 auto;padding:32px 40px;}',
      'h1,h2{font-family:var(--mono);font-size:13px;letter-spacing:.08em;text-transform:uppercase;}',
      'img{max-width:100%;display:block;margin:12px auto;}',
      '.eq{overflow-x:auto;padding:10px 0;}',
    ].join('\n'),
  })
}

async function showDetail(p) {
  document.getElementById('app').dataset.mode = 'detail'
  document.getElementById('sel-bar').classList.remove('visible')
  const root = document.getElementById('detail-view')
  if (!p.body) {
    root.innerHTML = '<div class="detail-wrap" style="color:var(--sub);font-family:var(--mono);font-size:12px">loading...</div>'
    await ensureBlocks(p)
    if (decodeURIComponent(location.hash.slice(1)) !== p.id) return  // 載入中已切走
  }
  root.innerHTML = `<div class="detail-wrap">
    <a class="detail-back" href="problems.html" id="back-list">← Problems</a>
    <div class="detail-head">
      <div class="detail-id">Problem ${esc(p.num)}</div>
      <div class="detail-title">${esc(p.book_title || p.book_slug)}</div>
      <div class="detail-meta">
        <span class="pill">${esc(label(p.subject))}</span>
        <span class="pill">Chapter ${esc(p.chapter)} · ${esc(p.chapter_title || '')}</span>
        ${p.has_solution ? '<span class="pill ok">solution</span>' : '<span class="pill">no solution</span>'}
      </div>
      <div class="detail-actions">
        <a class="qbk-control-btn compact" href="${escAttr(p.href_reader || 'index.html')}">Open in Reader</a>
        <button class="qbk-control-btn compact" id="copy-md" type="button">Copy Markdown</button>
        <button class="qbk-control-btn compact" id="copy-json" type="button">Copy JSON</button>
        <button class="qbk-control-btn compact" id="print-pdf" type="button">PDF</button>
      </div>
    </div>
    <section class="section">
      <div class="section-label">Problem</div>
      <div class="problem-body">${blocksHtml(p.body, p.book_slug)}</div>
    </section>
    ${p.has_solution ? `<section class="section">
      <div class="section-label">Solution</div>
      <div class="solution-body">${blocksHtml(p.solution, p.book_slug)}</div>
    </section>` : ''}
  </div>`
  document.getElementById('back-list').onclick = (e) => { e.preventDefault(); history.pushState('', document.title, 'problems.html'); showList() }
  document.getElementById('copy-md').onclick = (e) => copyText(problemMarkdown(p), e.currentTarget)
  document.getElementById('copy-json').onclick = (e) => copyText(JSON.stringify(p, null, 2), e.currentTarget)
  document.getElementById('print-pdf').onclick = () => printProblem(p)
  root.querySelectorAll('.eq').forEach(eq => {
    eq.addEventListener('click', () => {
      if (eq.dataset.tex) navigator.clipboard.writeText(eq.dataset.tex)
    })
  })
  S.renderMath(root)
}

function showList() {
  document.getElementById('app').dataset.mode = 'list'
  document.getElementById('detail-view').innerHTML = ''
  render()
}

function route() {
  const raw = location.hash.slice(1)
  if (!raw) {
    showList()
    return
  }
  const id = decodeURIComponent(raw)
  const p = allProblems.find(item => item.id === id)
  if (!p) {
    setError(`problem not found: ${id}`)
    showList()
    return
  }
  setError('')
  showDetail(p)
}

document.getElementById('search').addEventListener('input', (e) => {
  searchQ = (e.target.value || '').trim().toLowerCase()
  // 單本書 → 分片小，直接載；範圍更大 → 由 #search-corpus-note 讓使用者決定（見 render）
  if (searchQ && activeBook !== 'all') primeSearchCorpus()
  if (document.getElementById('app').dataset.mode === 'list') render()
})
document.addEventListener('keydown', (e) => {
  const target = e.target
  const editable = target && (
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.isContentEditable
  )
  if (editable) return
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') {
    e.preventDefault()
    selectedIds = new Set(filtered.map(p => p.id))
    syncRenderedSelection()
  }
})
window.addEventListener('hashchange', route)

// 索引 v3 = data/problems/index.json：books 去重表 + rows [bookIdx, chapter, [num…]]
// （num 前綴 '*' = 有解答）。**preview 不在索引裡**——它佔了舊單檔 45MB / 12.7MB gz 的絕大
// 部分，現在按書放在 data/problems/book/<slug>.json，等到真的要顯示/搜尋該書時才抓。
// body/solution 一如既往不入索引，detail/匯出時由 ensureBlocks 抓 data/<slug>/ch/<n>.json。
function hydrate(data) {
  const books = data.books || []
  const out = []
  ;(data.rows || []).forEach(([bi, ch, nums]) => {
    const b = books[bi] || {}
    const slug = b.slug || ''
    const key = String(ch)
    const chapterTitle = (b.chapters || {})[key] || ''
    nums.forEach((raw) => {
      const hasSol = raw.charCodeAt(0) === 42   // '*'
      const num = hasSol ? raw.slice(1) : raw
      out.push({
        id: `tb:${slug}:ch:${ch}:p:${num}`,
        book_slug: slug,
        book_title: b.title || slug,
        author: b.author || null,
        subject: b.subject || null,
        kind: 'ch',
        key,
        chapter: ch,
        chapter_title: chapterTitle,
        num,
        has_solution: hasSol,
        question_text: '',                 // 由 preview 分片補；ensureBlocks 後換成全文
        preview_loaded: false,
        href_reader: `index.html#${slug}/ch/${ch}?problem=${encodeURIComponent(num)}`,
        field: b.field || '其他',
        field_id: b.field_id || 'other',
        sublist: b.sublist || '',
        frank: b.frank ?? 999,
        srank: b.srank ?? 999,
      })
    })
  })
  return out
}

let searchPriming = false
function primeSearchCorpus() {
  if (searchPriming) return
  searchPriming = true
  ensureSearchCorpus((done, total) => {
    searchProgress = done < total ? `${done}/${total} 本` : null
    render()
  }).finally(() => {
    searchPriming = false
    searchProgress = null
    render()
  })
}

// ── preview 分片：按書載入，載到就回填 question_text ────────────────────────
const previewShards = new Map()     // slug → Promise
const bySlug = new Map()            // slug → 該書的題目物件（回填用）

function indexBySlug() {
  bySlug.clear()
  allProblems.forEach((p) => {
    if (!bySlug.has(p.book_slug)) bySlug.set(p.book_slug, [])
    bySlug.get(p.book_slug).push(p)
  })
}

function loadPreviews(slug) {
  if (!slug) return Promise.resolve()
  if (!previewShards.has(slug)) {
    previewShards.set(slug, S.fetchJson(`data/problems/book/${slug}.json`).then((shard) => {
      const text = new Map()
      ;(shard.rows || []).forEach(([ch, num, , preview]) => text.set(`${ch}/${num}`, preview || ''))
      ;(bySlug.get(slug) || []).forEach((p) => {
        if (!p.preview_loaded) {
          p.question_text = text.get(`${p.chapter}/${p.num}`) || ''
          p.preview_loaded = true
        }
      })
    }).catch(() => {
      ;(bySlug.get(slug) || []).forEach((p) => { p.preview_loaded = true })
    }))
  }
  return previewShards.get(slug)
}

// 目前畫面上這批題目缺哪些書的 preview → 抓回來後重畫（只重畫一次，避免每片一次）
let previewRepaintTimer = null
function ensurePreviews(items, onDone) {
  const want = new Set()
  items.forEach((p) => { if (!p.preview_loaded) want.add(p.book_slug) })
  if (!want.size) return
  Promise.all([...want].map(loadPreviews)).then(() => {
    if (previewRepaintTimer) clearTimeout(previewRepaintTimer)
    previewRepaintTimer = setTimeout(() => { previewRepaintTimer = null; onDone() }, 0)
  })
}

// 全站文字搜尋：索引沒有 preview，故搜尋前要把「搜尋範圍」的分片備齊。
// 範圍已收斂到某書/某領域 → 一兩個請求即可；範圍是全部書 → 邊下載邊出結果並顯示進度。
let searchLoadToken = 0
async function ensureSearchCorpus(onProgress) {
  const scope = allProblems.filter((p) => {
    if (activeBook !== 'all') return p.book_title === activeBook
    if (activeField !== 'all') return p.field_id === activeField
    return true
  })
  const slugs = [...new Set(scope.filter((p) => !p.preview_loaded).map((p) => p.book_slug))]
  if (!slugs.length) return
  const token = ++searchLoadToken
  const CONCURRENCY = 8
  let done = 0
  let cursor = 0
  const worker = async () => {
    while (cursor < slugs.length) {
      if (token !== searchLoadToken) return          // 使用者改了範圍 → 放棄這一輪
      const slug = slugs[cursor++]
      await loadPreviews(slug)
      done += 1
      if (done % CONCURRENCY === 0 || done === slugs.length) onProgress(done, slugs.length)
    }
  }
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, slugs.length) }, worker))
}

const chapterCache = new Map()
function loadChapter(slug, ch) {
  const k = `${slug}/${ch}`
  if (!chapterCache.has(k)) {
    chapterCache.set(k, S.fetchJson(`data/${slug}/ch/${ch}.json`).catch(() => null))
  }
  return chapterCache.get(k)
}

// 按需把完整 body/solution 區塊掛上 problem 物件（冪等，抓過即快取）。
async function ensureBlocks(p) {
  if (p.body) return p
  const chunk = await loadChapter(p.book_slug, p.chapter)
  const match = (chunk && chunk.problems || []).find(x => String(x.num).trim() === p.num)
  p.body = (match && match.body) || []
  p.solution = (match && match.solution) || []
  if (match) {
    p.question_text = blocksToText(p.body)
    p.solution_text = blocksToText(p.solution)
  } else {
    p.solution_text = p.solution_text || ''
  }
  return p
}

async function init() {
  S.theme.init()   // 主題+換皮：套用全站設定並綁切換鈕（補回本頁原缺的 dark/skin）
  try {
    const data = await S.fetchJson('data/problems/index.json', { cache: 'no-cache' })
    allProblems = hydrate(data)
    indexBySlug()
    setError('')
    setViewMode(viewMode)
    setupDragSelect()
    route()
  } catch (err) {
    document.getElementById('loading').textContent = 'data/problems/index.json not found - run build first.'
    setError(S.errorMessage(err, 'failed to load problems'))
  }
}

init()
