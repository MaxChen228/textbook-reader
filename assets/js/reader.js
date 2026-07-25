/**
 * 閱讀器（index.html）的應用層：載書、目錄、章節渲染、路由、鍵盤、浮層。
 *
 * 可重用、可獨立測試的部分已抽成模組，本檔只留「這個 app 怎麼把它們串起來」：
 *   blocks.js — 區塊 → HTML/純文字（與題庫頁共用同一份）
 *   store.js  — 設置與閱讀進度的持久化
 *   router.js — hash 路由文法
 *   shared.js — 三頁共用的 QBankShared（傳統全域腳本的 ESM 轉接層）
 */
import QBankShared, { safeHtml as esc, escapeAttr as escAttr, renderMarkdown } from './shared.js'
import { renderBlocks, biReveal, blocksToText, problemsToText } from './blocks.js'
import {
  FS_VALUES, LH_VALUES, WIDTH_VALUES, DEFAULT_SETTINGS, clampStep,
  loadSettings, saveSettings as persistSettings,
  loadProgress, recordProgress, chunkProgress, lastProgressFor,
} from './store.js'
import { buildHash, parseHash as parseRoute, go as routerGo, replace as replaceHash } from './router.js'
import { loadIndex, queryTerms, candidateChunks, searchChunk, highlightInDom } from './search.js'

let books = []         // 已收錄可讀書 metadata（data/books.json）
let catalog = null     // 完整收錄表（data/catalog.json）：書單 SoT × 三態，library 渲染來源
let bookBySlug = {}    // slug → 已收錄書 metadata（catalog owned 卡片 join 封面/章數/中譯）
let book = null        // currently loaded book.json
let slug = null
let currentKind = null // 'ch' | 'app'
let currentKey = null  // chapter number or appendix id
let pendingAnchor = null // section anchor to scroll to after next showChunk
let libraryField = 'all'
let sectionObserver = null
let sectionScrollHandler = null
let mathScrollHandler = null // 視窗化增量 MathJax：只保留視窗附近排版
let mathResizeHandler = null
let mathVisibilityHandler = null
let activeSectionAnchor = null
let readerDrawer = null
const bookCache = {}
const chunkCache = {}
const catalogCache = {}
let sidebarMode = 'toc'
let catalogType = 'figures'
let catalogQuery = ''
let catalogLimit = 80
let catalogSearchTimer = null
let catalogDetailTarget = null
let pendingProblemNum = null

// 設置與進度的 schema／持久化在 store.js；這裡只留 app 狀態與 DOM 套用。
let settings = loadSettings()
const darkMQ = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null
let progressSaveTimer = null

function saveSettings() {
  persistSettings(settings)
}

/** 量測目前捲動位置並存檔。位置怎麼量是 DOM 的事，存去哪是 store 的事。 */
function persistCurrentProgress() {
  if (!slug || !currentKind || currentKey == null) return
  const content = document.getElementById('content')
  const maxScroll = Math.max(0, content.scrollHeight - content.clientHeight)
  recordProgress({
    slug,
    kind: currentKind,
    key: String(currentKey),
    anchor: activeSectionAnchor || headingAnchorForScroll(content) || null,
    scrollTop: Math.max(0, Math.round(content.scrollTop || 0)),
    scrollRatio: maxScroll ? Math.min(1, Math.max(0, content.scrollTop / maxScroll)) : 0,
    updatedAt: Date.now(),
  })
}

function scheduleProgressSave() {
  if (progressSaveTimer) clearTimeout(progressSaveTimer)
  progressSaveTimer = setTimeout(() => {
    progressSaveTimer = null
    persistCurrentProgress()
  }, 250)
}

function restoreProgressPosition(saved) {
  if (!saved) return false
  const content = document.getElementById('content')
  const maxScroll = Math.max(0, content.scrollHeight - content.clientHeight)
  if (Number.isFinite(saved.scrollRatio) && maxScroll > 0) {
    content.scrollTop = Math.round(maxScroll * saved.scrollRatio)
    updateActiveSectionFromScroll()
    return true
  }
  if (Number.isFinite(saved.scrollTop)) {
    content.scrollTop = Math.max(0, saved.scrollTop)
    updateActiveSectionFromScroll()
    return true
  }
  const byAnchor = saved.anchor ? document.getElementById(saved.anchor) : null
  if (byAnchor) {
    byAnchor.scrollIntoView({ behavior: 'auto', block: 'start' })
    setActiveSection(saved.anchor)
    return true
  }
  return false
}

function resolvedTheme() {
  if (settings.theme === 'dark' || settings.theme === 'light') return settings.theme
  return (darkMQ && darkMQ.matches) ? 'dark' : 'light'
}

function applySettings() {
  document.body.dataset.lang = settings.lang
  document.body.dataset.theme = resolvedTheme()
  document.body.dataset.skin = settings.skin
  const fs = FS_VALUES[settings.fsStep - 1]
  const lh = LH_VALUES[settings.lhStep - 1]
  const width = WIDTH_VALUES[settings.widthStep - 1]
  document.documentElement.style.setProperty('--article-fs', fs + 'px')
  document.documentElement.style.setProperty('--article-lh', lh)
  document.documentElement.style.setProperty('--article-max-width', width + 'px')
  document.querySelectorAll('#seg-lang button').forEach(b =>
    b.classList.toggle('active', b.dataset.lang === settings.lang))
  document.getElementById('slider-fs').value = String(settings.fsStep)
  document.getElementById('slider-lh').value = String(settings.lhStep)
  document.getElementById('slider-width').value = String(settings.widthStep)
  document.getElementById('fs-value').textContent = fs + 'px'
  document.getElementById('lh-value').textContent = lh.toFixed(2)
  document.getElementById('width-value').textContent = width + 'px'
  document.querySelectorAll('#seg-theme button').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === settings.theme))
  document.querySelectorAll('#seg-skin button').forEach(b =>
    b.classList.toggle('active', b.dataset.skin === settings.skin))
}

// 靜態化：URL query 改成檔名 suffix。無 zh overlay 的書一律回 '' → 顯英文、零 404。
let currentBookHasZh = false

function langSuffix() {
  if (!currentBookHasZh) return ''
  if (settings.lang === 'zh') return '.zh'
  if (settings.lang === 'bi') return '.bi'
  return ''
}

function bookLangKey() {
  return (currentBookHasZh && settings.lang === 'zh') ? 'zh' : 'en'
}

function bookLangSuffix() {
  return (currentBookHasZh && settings.lang === 'zh') ? '.zh' : ''
}

function setupSettingsUI() {
  const btn = document.getElementById('btn-settings')
  const pop = document.getElementById('settings-pop')
  btn.onclick = (e) => {
    e.stopPropagation()
    const open = !pop.classList.contains('open')
    pop.classList.toggle('open', open)
    btn.classList.toggle('open', open)
    btn.setAttribute('aria-expanded', String(open))
  }
  document.addEventListener('click', (e) => {
    if (!pop.contains(e.target) && e.target !== btn) {
      pop.classList.remove('open'); btn.classList.remove('open')
      btn.setAttribute('aria-expanded', 'false')
    }
  })
  document.getElementById('seg-lang').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-lang]')
    if (!b || b.dataset.lang === settings.lang) return
    settings.lang = b.dataset.lang
    persistCurrentProgress()
    saveSettings(); applySettings()
    if (slug) {
      await loadBook(slug, { reloadForLanguage: true })
      if (currentKind) await showChunk(currentKind, currentKey, { preserveScroll: true })
      else showBookOverview()
    }
  })
  document.getElementById('slider-fs').addEventListener('input', (e) => {
    settings.fsStep = clampStep(e.target.value, DEFAULT_SETTINGS.fsStep)
    saveSettings(); applySettings()
  })
  document.getElementById('slider-lh').addEventListener('input', (e) => {
    settings.lhStep = clampStep(e.target.value, DEFAULT_SETTINGS.lhStep)
    saveSettings(); applySettings()
  })
  document.getElementById('slider-width').addEventListener('input', (e) => {
    settings.widthStep = clampStep(e.target.value, DEFAULT_SETTINGS.widthStep)
    saveSettings(); applySettings()
  })
  document.getElementById('seg-theme').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-theme]')
    if (!b) return
    settings.theme = b.dataset.theme
    saveSettings(); applySettings()
  })
  document.getElementById('btn-panel-copy').addEventListener('click', copyCurrentChunk)
  document.getElementById('btn-panel-copy-text').addEventListener('click', copyCurrentChunk)
  // 系統 prefers-color-scheme 變動 → auto 模式跟著切
  if (darkMQ) {
    const onChange = () => { if (settings.theme === 'auto') applySettings() }
    if (darkMQ.addEventListener) darkMQ.addEventListener('change', onChange)
    else if (darkMQ.addListener) darkMQ.addListener(onChange)
  }
}

// ── init ──
async function init() {
  applySettings()
  setupSettingsUI()
  readerDrawer = QBankShared.bindSidebarDrawer({
    overlayId: 'reader-sidebar-overlay',
    buttonId: 'btn-reader-sidebar',
  })
  document.getElementById('sb-back').addEventListener('click', (e) => {
    e.preventDefault(); location.hash = ''
  })
  document.getElementById('lib-search').addEventListener('input', (e) => {
    renderLibrary(e.target.value)
  })
  setupSidebarModes()
  setupCatalogDetail()
  setupLightbox()
  setupMathCopy()
  setupKeyboard()
  try {
    books = await QBankShared.fetchJson('data/books.json')
    bookBySlug = Object.fromEntries(books.map(b => [b.slug, b]))
    // 收錄表 = 書單 SoT × 三態（data/catalog.json）。缺檔（舊 build）→ 退回用已收錄書合成（只顯示已收錄）。
    try { catalog = await QBankShared.fetchJson('data/catalog.json') }
    catch (e) { catalog = synthCatalogFromBooks() }
    if (!catalog || !catalog.fields || !catalog.fields.length) catalog = synthCatalogFromBooks()
    if (!books.length && !catalog.fields.some(f => f.sublists.some(s => s.books.length))) {
      showLibrary()
      document.getElementById('lib-groups').innerHTML =
        '<div class="lib-empty">無資料 — 先跑 <code>mineru_ingest</code> 把書送進來</div>'
      return
    }
    renderLibraryRail()
    applyRoute({ restoreSaved: true })
  } catch (e) {
    document.getElementById('loading').textContent = '載入失敗：' + e.message
  }
}

// catalog.json 缺檔時的退路：用已收錄書按 subject 合成單一領域收錄表（全 owned）。
function synthCatalogFromBooks() {
  const bySub = {}
  books.forEach(b => { (bySub[b.subject || '其他'] ||= []).push(
    { slug: b.slug, title: b.title, author: b.author || '', status: 'owned' }) })
  return { fields: [{ field: '已收錄', field_id: 'owned',
    sublists: Object.keys(bySub).sort().map(n => ({ name: n, books: bySub[n] })) }] }
}

// 六態 → 使用者三態：已收錄 / 待收錄（排隊/待解析/待裁）/ 無法收錄。
function shelfState(status) {
  if (status === 'owned') return 'owned'
  if (status === 'absent') return 'absent'
  return 'pending'  // queued | ready | unresolved | review（review=架構師待裁，公開 UI 同視「待收錄」）
}
const SHELF_LABEL = { owned: '已收錄', pending: '待收錄', absent: '無法收錄', processing: '處理中' }

function closeReaderSidebar() {
  if (readerDrawer) readerDrawer.close()
}

function setupSidebarModes() {
  document.getElementById('reader-tabs').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-sidebar-mode]')
    if (!b) return
    await setSidebarMode(b.dataset.sidebarMode)
  })
  document.getElementById('catalog-types').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-catalog-type]')
    if (!b) return
    catalogType = b.dataset.catalogType
    catalogLimit = 80
    renderCatalogPanel()
  })
  const box = document.getElementById('book-search')
  box.addEventListener('input', (e) => {
    if (searchTimer) clearTimeout(searchTimer)
    const q = e.target.value || ''
    searchTimer = setTimeout(() => runBookSearch(q), 160)
  })
  box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runBookSearch(box.value || '') }
  })
  document.getElementById('search-results').addEventListener('click', (e) => {
    const more = e.target.closest('#search-more')
    if (more) { searchChunkCap += SEARCH_CHUNK_CAP; runBookSearch(box.value || '', { keepCap: true }); return }
    const row = e.target.closest('.search-hit-row')
    if (row) gotoSearchHit(Number(row.dataset.hit))
  })
  document.getElementById('catalog-search').addEventListener('input', (e) => {
    if (catalogSearchTimer) clearTimeout(catalogSearchTimer)
    catalogSearchTimer = setTimeout(() => {
      catalogQuery = e.target.value || ''
      catalogLimit = 80
      renderCatalogPanel()
    }, 120)
  })
}

// ── 書內全文搜尋 ───────────────────────────────────────────────────────
// 索引只給候選章節（search.js），真正的命中與摘要靠抓那幾章的 JSON 在本機比對。
// 每章 JSON 動輒數百 KB，所以：一次最多掃 SEARCH_CHUNK_CAP 章、並行 4、邊掃邊出結果，
// 掃不完的部分明說「還有 N 章」讓使用者自己決定要不要繼續——沒有後端就別假裝免費。
const SEARCH_CHUNK_CAP = 12
const SEARCH_CONCURRENCY = 4
let searchTerms = []          // 目前查詢的詞條（已 fold），也用來標記正文
let searchHits = []           // [{ ci, kind, key, chapterTitle, anchor, where, snippet }]
let searchRun = 0             // 取消舊查詢用（輸入變更即作廢在飛的掃描）
let searchTimer = null
let searchChunkCap = SEARCH_CHUNK_CAP
let activeSearchHit = -1
let pendingSearchScroll = false   // 下一次 showChunk 後要不要跳到命中處

function setSearchStatus(html) {
  document.getElementById('search-status').innerHTML = html
}

async function runBookSearch(query, { keepCap = false } = {}) {
  const run = ++searchRun
  if (!keepCap) searchChunkCap = SEARCH_CHUNK_CAP
  searchTerms = queryTerms(query)
  searchHits = []
  activeSearchHit = -1
  renderSearchResults()
  if (!searchTerms.length) { setSearchStatus(''); clearSearchHighlight(); return }

  const index = await loadIndex(slug)
  if (run !== searchRun) return
  if (!index) {
    setSearchStatus('這本書還沒有搜尋索引（重新 build 後就會有）')
    return
  }
  const cands = candidateChunks(index, searchTerms)
  if (!cands.length) { setSearchStatus('沒有找到'); return }

  const scan = cands.slice(0, searchChunkCap)
  const rest = cands.length - scan.length
  let done = 0
  const tick = () => {
    setSearchStatus(`掃描 ${done}/${scan.length} 章 · 命中 ${searchHits.length}`
      + (rest > 0 ? ` · 還有 ${rest} 章未掃` : ''))
  }
  tick()

  let cursor = 0
  const worker = async () => {
    while (cursor < scan.length && run === searchRun) {
      const ci = scan[cursor++]
      const [kind, key, title] = index.chunks[ci]
      try {
        const data = await fetchChunk(kind, key)
        if (run !== searchRun) return
        const secPrefix = kind === 'ch' ? String(data.num) : `app${data.id}`
        for (const h of searchChunk(data, searchTerms, { secPrefix })) {
          searchHits.push({ ci, kind, key, chapterTitle: title, ...h })
        }
      } catch { /* 單章抓失敗不該讓整個搜尋停擺 */ }
      done += 1
      searchHits.sort((a, b) => a.ci - b.ci)
      tick()
      renderSearchResults(rest)
    }
  }
  await Promise.all(Array.from({ length: SEARCH_CONCURRENCY }, worker))
  if (run !== searchRun) return
  setSearchStatus(searchHits.length
    ? `命中 ${searchHits.length} 處 · 已掃 ${scan.length} 章` + (rest > 0 ? ` · 還有 ${rest} 章未掃` : '')
    : `這 ${scan.length} 章裡沒有` + (rest > 0 ? `（還有 ${rest} 章未掃）` : ''))
  renderSearchResults(rest)
}

function renderSearchResults(rest = 0) {
  const root = document.getElementById('search-results')
  if (!searchTerms.length) { root.innerHTML = ''; return }
  let html = ''
  let lastCi = null
  searchHits.forEach((h, i) => {
    if (h.ci !== lastCi) {
      lastCi = h.ci
      const label = h.kind === 'ch' ? `Ch ${h.key}` : `App ${h.key}`
      html += `<div class="search-group">${esc(label)} · ${esc(h.chapterTitle)}</div>`
    }
    html += `<button class="search-hit-row${i === activeSearchHit ? ' active' : ''}" type="button" data-hit="${i}">
      ${h.where ? `<div class="search-hit-where">${esc(h.where)}</div>` : ''}
      <div class="search-hit-text">${h.snippet}</div>
    </button>`
  })
  if (rest > 0) {
    html += `<button class="qbk-control-btn compact search-more" id="search-more" type="button">再掃 ${Math.min(rest, SEARCH_CHUNK_CAP)} 章</button>`
  }
  root.innerHTML = html
}

async function gotoSearchHit(i) {
  const h = searchHits[i]
  if (!h) return
  activeSearchHit = i
  renderSearchResults(0)
  document.querySelectorAll('.search-hit-row').forEach((el, idx) => el.classList.toggle('active', idx === i))
  if (readerDrawer && window.matchMedia('(max-width: 768px)').matches) readerDrawer.close()
  pendingSearchScroll = true
  if (String(currentKind) === h.kind && String(currentKey) === String(h.key)) {
    // 已經在這一章 → 不重建 DOM，就地補標記再跳過去
    const content = document.getElementById('content')
    applySearchHighlight(content)
    if (h.anchor) { scrollToAnchor(h.anchor); setActiveSection(h.anchor) }
    focusSearchHit(content, h.anchor)
    pendingSearchScroll = false
    return
  }
  navigate(slug, h.kind, h.key, h.anchor || undefined)
}

/** 在正文標出所有命中；被 showChunk 呼叫（必須在 setupIncrementalMath 之前，
 *  否則 _mathRaw 快照裡不含這些 <mark>，捲出去再回來就掉了）。 */
function applySearchHighlight(content) {
  if (!searchTerms.length || sidebarMode !== 'search') return
  const article = content.querySelector('.article')
  if (!article) return
  clearSearchHighlight()   // 冪等：同一章重複套用（換查詢詞、點另一筆命中）不會疊出巢狀 <mark>
  highlightInDom(article, searchTerms)
}

/** 清掉正文裡的命中標記（查詢清空時）。就地還原成文字節點，不重建整章。 */
function clearSearchHighlight() {
  const content = document.getElementById('content')
  content.querySelectorAll('mark.search-hit').forEach(m => {
    const parent = m.parentNode
    parent.replaceChild(document.createTextNode(m.textContent), m)
    parent.normalize()
  })
}

/** 把 anchor 之後的第一顆命中標成 current 並捲到畫面中央。 */
function focusSearchHit(content, anchor) {
  content.querySelectorAll('mark.search-hit.current').forEach(m => m.classList.remove('current'))
  const marks = [...content.querySelectorAll('mark.search-hit')]
  if (!marks.length) return
  const from = anchor ? document.getElementById(anchor) : null
  const target = from
    ? marks.find(m => from.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING) || marks[0]
    : marks[0]
  target.classList.add('current')
  target.scrollIntoView({ block: 'center', behavior: 'auto' })
}

const SIDEBAR_MODES = ['toc', 'search', 'catalog']

async function setSidebarMode(mode) {
  sidebarMode = SIDEBAR_MODES.includes(mode) ? mode : 'toc'
  document.querySelectorAll('.reader-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.sidebarMode === sidebarMode))
  document.getElementById('toc').style.display = sidebarMode === 'toc' ? 'block' : 'none'
  document.getElementById('search-panel').style.display = sidebarMode === 'search' ? 'flex' : 'none'
  document.getElementById('catalog-panel').style.display = sidebarMode === 'catalog' ? 'flex' : 'none'
  if (sidebarMode === 'catalog') {
    await loadCatalog(slug)
    renderCatalogPanel()
  }
}

function currentReaderLabel() {
  if (!slug || !book) return 'Library'
  if (!currentKind) return book.title || slug
  const items = currentKind === 'ch' ? book.chapters : book.appendices
  const idField = currentKind === 'ch' ? 'num' : 'id'
  const item = (items || []).find(it => String(it[idField]) === String(currentKey))
  const prefix = currentKind === 'ch' ? `Ch ${currentKey}` : `App ${currentKey}`
  return [prefix, item?.title].filter(Boolean).join(' · ')
}

function setButtonAction(id, enabled, handler) {
  const btn = document.getElementById(id)
  if (!btn) return
  btn.disabled = !enabled
  btn.onclick = enabled ? handler : null
}

function updateReaderPanel({ prev = null, next = null } = {}) {
  const isChunk = Boolean(currentKind && currentKey)
  const state = document.getElementById('reader-panel-state')
  if (state) state.textContent = currentReaderLabel()
  setButtonAction('btn-panel-prev', Boolean(prev), () => navigate(slug, currentKind, prev))
  setButtonAction('btn-panel-next', Boolean(next), () => navigate(slug, currentKind, next))
  document.querySelectorAll('#btn-panel-copy, #btn-panel-copy-text').forEach(btn => {
    btn.disabled = !isChunk
  })
}

async function copyCurrentChunk(e) {
  if (e) e.stopPropagation()
  if (!currentKind || !currentKey) return
  await copyChapter(currentKind, currentKey, e?.currentTarget || null)
}

const SKELETON_HTML = `<div class="skeleton" aria-hidden="true">
  <div class="sk-bar title"></div>
  ${'<div class="sk-bar"></div>'.repeat(3)}<div class="sk-bar short"></div>
  ${'<div class="sk-bar"></div>'.repeat(4)}<div class="sk-bar short"></div>
  ${'<div class="sk-bar"></div>'.repeat(3)}
</div>`

// ── overlay 管理：焦點鎖 + 捲動鎖 + 焦點回復 ─────────────────────────
// 兩個舊 bug 一起修：① 只鎖 body 沒用（真正的捲動容器是 #content，背景照捲）
// ② 開了對話框仍能 Tab 到背景的目錄/按鈕（螢幕閱讀器也讀得到）→ 用 inert + Tab 迴圈雙保險。
const overlayStack = []

function lockScroll(on) {
  document.body.style.overflow = on ? 'hidden' : ''
  const c = document.getElementById('content')
  if (c) c.style.overflow = on ? 'hidden' : ''   // scrollTop 保留，關閉後回原位
}

function focusablesIn(root) {
  return [...root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter(el => el.offsetWidth || el.offsetHeight || el === document.activeElement)
}

function openOverlay(el, { initialFocus = null } = {}) {
  if (overlayStack.some(o => o.el === el)) return
  overlayStack.push({ el, restore: document.activeElement })
  document.getElementById('app').inert = true
  lockScroll(true)
  // 同步聚焦：呼叫端已先把浮層設為顯示。（別排進 rAF——分頁在背景時 rAF 不跑，焦點會留在背景內容上）
  const target = initialFocus || focusablesIn(el)[0] || el
  target.focus?.({ preventScroll: true })
}

function closeOverlay(el) {
  const i = overlayStack.findIndex(o => o.el === el)
  if (i === -1) return
  const [entry] = overlayStack.splice(i, 1)
  if (!overlayStack.length) {
    document.getElementById('app').inert = false
    lockScroll(false)
  }
  entry.restore?.focus?.({ preventScroll: true })
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Tab' || !overlayStack.length) return
  const el = overlayStack[overlayStack.length - 1].el
  const f = focusablesIn(el)
  if (!f.length) return
  const first = f[0], last = f[f.length - 1]
  if (!el.contains(document.activeElement)) { e.preventDefault(); first.focus() }
  else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
})

// ── 鍵盤操作 ───────────────────────────────────────────────────────
const SHORTCUTS = [
  ['←  /  [', '上一章'],
  ['→  /  ]', '下一章'],
  ['j  /  k', '向下 / 向上捲動'],
  ['g', '回書庫'],
  ['t', '跳到目錄（手機自動開側欄）'],
  ['/', '搜尋（書庫：書名；閱讀中：本書全文）'],
  ['d', '切換明暗'],
  ['?', '本說明'],
  ['Esc', '關閉浮層 / 離開輸入框'],
]

function isTypingTarget(el) {
  return Boolean(el) && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}

function clickIfEnabled(id) {
  const b = document.getElementById(id)
  if (b && !b.disabled) b.click()
}

function focusTOC() {
  if (document.getElementById('app').dataset.view !== 'reader') return
  if (readerDrawer && window.matchMedia('(max-width: 768px)').matches) readerDrawer.open()
  setSidebarMode('toc')
  const target = document.querySelector('.toc-row.active .toc-item') || document.querySelector('.toc-item')
  target?.focus({ preventScroll: true })
  target?.scrollIntoView({ block: 'nearest' })
}

function focusSearch() {
  const isReader = document.getElementById('app').dataset.view === 'reader'
  if (!isReader) { document.getElementById('lib-search').focus(); return }
  if (readerDrawer && window.matchMedia('(max-width: 768px)').matches) readerDrawer.open()
  setSidebarMode('search').then(() => document.getElementById('book-search').focus())
}

function toggleShortcuts(force) {
  const el = document.getElementById('shortcuts-modal')
  const open = force != null ? force : !el.classList.contains('open')
  el.classList.toggle('open', open)
  if (open) openOverlay(el)
  else closeOverlay(el)
}

function setupKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    if (isTypingTarget(document.activeElement)) {
      if (e.key === 'Escape') document.activeElement.blur()
      return
    }
    if (e.key === 'Escape') {
      if (document.getElementById('shortcuts-modal').classList.contains('open')) toggleShortcuts(false)
      return
    }
    if (overlayStack.length) return   // 浮層開著時，其餘快捷鍵讓位
    const content = document.getElementById('content')
    const isReader = document.getElementById('app').dataset.view === 'reader'
    switch (e.key) {
      case 'ArrowLeft': case '[':
        if (isReader) { e.preventDefault(); clickIfEnabled('btn-prev') } break
      case 'ArrowRight': case ']':
        if (isReader) { e.preventDefault(); clickIfEnabled('btn-next') } break
      case 'j':
        if (isReader) { e.preventDefault(); content.scrollBy({ top: Math.round(content.clientHeight * 0.12) }) } break
      case 'k':
        if (isReader) { e.preventDefault(); content.scrollBy({ top: -Math.round(content.clientHeight * 0.12) }) } break
      case 'g': e.preventDefault(); location.hash = ''; break
      case 't': e.preventDefault(); focusTOC(); break
      case '/': e.preventDefault(); focusSearch(); break
      case 'd':
        e.preventDefault()
        settings.theme = resolvedTheme() === 'dark' ? 'light' : 'dark'
        saveSettings(); applySettings()
        break
      case '?': e.preventDefault(); toggleShortcuts(); break
    }
  })
  document.getElementById('shortcuts-list').innerHTML = SHORTCUTS
    .map(([k, d]) => `<div class="sc-key"><kbd>${esc(k)}</kbd></div><div class="sc-desc">${esc(d)}</div>`).join('')
  document.getElementById('shortcuts-close').addEventListener('click', () => toggleShortcuts(false))
  document.getElementById('shortcuts-modal').addEventListener('click', (e) => {
    if (e.target.id === 'shortcuts-modal') toggleShortcuts(false)
  })
  document.getElementById('btn-shortcuts').addEventListener('click', () => toggleShortcuts(true))
}

// ── lightbox ──────────────────────────────────────────────────────
function setupLightbox() {
  const lb = document.getElementById('lightbox')
  const lbImg = document.getElementById('lightbox-img')
  const lbCap = document.getElementById('lightbox-caption')
  // delegate：content 區所有 figure img 點擊 → 開 lightbox
  document.getElementById('content').addEventListener('click', (e) => {
    const img = e.target.closest('figure img')
    if (!img) return
    e.preventDefault()
    lbImg.src = img.src
    lbImg.alt = img.alt || ''
    const cap = img.parentElement?.querySelector('figcaption')?.textContent || ''
    lbCap.textContent = cap
    lbCap.style.display = cap ? 'block' : 'none'
    lb.classList.add('open')
    openOverlay(lb, { initialFocus: document.getElementById('lightbox-close') })
  })
  const close = () => {
    if (!lb.classList.contains('open')) return
    lb.classList.remove('open')
    lbImg.src = ''
    closeOverlay(lb)
  }
  lb.addEventListener('click', close)
  document.getElementById('lightbox-close').addEventListener('click', (e) => {
    e.stopPropagation(); close()
  })
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lb.classList.contains('open')) close()
  })
}

// ── library view ──────────────────────────────────────────────────
function showLibrary() {
  persistCurrentProgress()
  document.getElementById('app').dataset.view = 'library'
  closeReaderSidebar()
  cleanupSectionSpy()
  slug = null; book = null; currentKind = null; currentKey = null
  updateReaderPanel()
  renderLibrary(document.getElementById('lib-search').value || '')
  document.title = '教科書 Reader'
}

// 收錄表統計：跨指定 fields 數主書三態（已收錄/待收錄/無法收錄）。
function catalogTally(fields) {
  let owned = 0, total = 0, pending = 0, absent = 0
  fields.forEach(f => f.sublists.forEach(sl => sl.books.forEach(b => {
    total++
    const st = shelfState(b.status)
    if (st === 'owned') owned++
    else if (st === 'absent') absent++
    else pending++
  })))
  return { owned, total, pending, absent }
}

function renderLibraryRail() {
  const root = document.getElementById('library-subjects')
  root.innerHTML = ''
  const addChip = (field, label, owned, total) => {
    root.appendChild(QBankShared.createChip({
      text: label + ' ',
      count: `${owned}/${total}`,
      active: libraryField === field,
      dataset: { field },
      onClick: () => {
        libraryField = field
        renderLibraryRail()
        renderLibrary(document.getElementById('lib-search').value || '')
      },
    }))
  }
  const all = catalogTally(catalog.fields)
  addChip('all', '全部', all.owned, all.total)
  catalog.fields.forEach(f => {
    const t = catalogTally([f])
    addChip(f.field_id, f.field, t.owned, t.total)
  })
  document.getElementById('library-stat-books').textContent = all.owned
  document.getElementById('library-stat-chapters').textContent = all.total
  document.getElementById('library-stat-translated').textContent = all.pending + all.absent
}

// 收錄表渲染：catalog.fields（書單 SoT × 三態）→ field 群組 → 具名子單 → 三態卡片。
// 搜尋對 title/author 做子字串過濾；libraryField 選領域。只有「已收錄」卡可點進 reader。
function renderLibrary(filter) {
  const wrap = document.getElementById('lib-groups')
  const count = document.getElementById('library-count')
  const q = (filter || '').trim().toLowerCase()
  const matchBook = b => !q
    || (b.title || '').toLowerCase().includes(q)
    || (b.author || '').toLowerCase().includes(q)
  let totalAll = 0
  catalog.fields.forEach(f => f.sublists.forEach(sl => { totalAll += sl.books.length }))

  const fields = catalog.fields.filter(f => libraryField === 'all' || f.field_id === libraryField)
  let shown = 0
  const html = fields.map(f => {
    const sublists = f.sublists
      .map(sl => ({ name: sl.name, books: sl.books.filter(matchBook) }))
      .filter(sl => sl.books.length)
    if (!sublists.length) return ''
    const t = catalogTally([{ ...f, sublists }])
    shown += t.total
    const subHtml = sublists.map(sl => `<div class="lib-sublist">
      <div class="lib-sublist-label">${esc(sl.name)}</div>
      <div class="lib-grid">${sl.books.map(catalogCardHtml).join('')}</div>
    </div>`).join('')
    return `<div class="lib-group">
      <div class="lib-group-label">${esc(f.field)}
        <span class="lib-group-stat">${t.owned}/${t.total} 收錄</span></div>
      ${subHtml}
    </div>`
  }).join('')

  count.textContent = `${shown}/${totalAll}`
  wrap.innerHTML = html || '<div class="lib-empty">沒有相符的書</div>'
  wrap.querySelectorAll('.lib-card[data-slug]').forEach(card => {
    card.addEventListener('click', (e) => {
      e.preventDefault()
      navigate(card.dataset.slug)
    })
  })
}

// 收錄表卡片：已收錄(且已部署)→join bookBySlug 取封面/版次/中譯/章數，可點；處理中(owned 目錄存在
// 但未 bake 進 books.json)→ghost 卡（不可點）；待收錄/無法收錄→ghost 卡。皆掛 shelf-pill 標狀態。
function catalogCardHtml(b) {
  const st = shelfState(b.status)
  const m = bookBySlug[b.slug]
  // owned 但不在 books.json（已部署集）= 管線未跑完（crawl+OCR 了、尚未 parse/audit/deploy）→ 處理中：
  // 不可點（點了會導去不存在的 data/<slug> 空白頁）、不顯封面/章數（那些 deploy 時才 bake）。
  if (st === 'owned' && m) {
    const meta = [
      m.edition ? `<span class="edition">${esc(m.edition)}</span>` : '',
      m.has_zh ? `<span class="zh-badge" title="已有中文翻譯">中譯</span>` : '',
      m.chapter_count != null ? `<span>${m.chapter_count} 章</span>` : '',
      b.sol_status === 'owned' ? `<span class="sol-badge" title="已收錄解答本">解答</span>` : '',
    ].filter(Boolean).join('')
    // onerror：封面 webp 萬一 404（如 deploy 空窗或 immutable 快取黏死）→ 收掉封面框、退化成無封面卡，
    // 不顯瀏覽器破圖 icon。build_all 已 convert 先於 bake 杜絕產生空窗，此為前端防線。
    const cover = m.has_cover
      ? `<div class="lib-card-cover"><img src="img/${escAttr(b.slug)}/cover.webp" alt="" loading="lazy" onerror="this.closest('.lib-card-cover').style.display='none'"></div>`
      : ''
    return `<a class="lib-card qbk-raised-item" href="#${escAttr(b.slug)}" data-slug="${escAttr(b.slug)}">
      ${cover}
      <div class="lib-card-body">
        <div class="lib-card-title">${esc(b.title || m.title || b.slug)}</div>
        <div class="lib-card-author">${esc(b.author || m.author || '')}</div>
        <div class="lib-card-meta">${meta}</div>
      </div>
    </a>`
  }
  const ghost = (st === 'owned') ? 'processing' : st   // owned 但未部署 → 處理中 ghost
  return `<div class="lib-card lib-card-ghost shelf-${ghost}">
    <div class="lib-card-body">
      <div class="lib-card-title">${esc(b.title || b.slug)}</div>
      <div class="lib-card-author">${esc(b.author || '')}</div>
      <div class="lib-card-meta"><span class="shelf-pill shelf-${ghost}">${SHELF_LABEL[ghost]}</span></div>
    </div>
  </div>`
}

// ── reader：sidebar header + TOC ──────────────────────────────────
function updateSidebarHeader() {
  document.getElementById('sb-bookname').textContent = book?.title || '—'
  const meta = [book?.author, book?.edition].filter(Boolean).join(' · ')
  document.getElementById('sb-bookmeta').textContent = meta
}

async function loadBook(s, { reloadForLanguage = false } = {}) {
  currentBookHasZh = !!(books.find(b => b.slug === s) || {}).has_zh
  const key = `${s}/${bookLangKey()}`
  if (!reloadForLanguage && slug === s && book && bookCache[key]) {
    if (sidebarMode === 'catalog') {
      await loadCatalog(s)
      renderCatalogPanel()
    }
    return book
  }
  slug = s
  if (!bookCache[key]) {
    bookCache[key] = await QBankShared.fetchJson(`data/${s}/book${bookLangSuffix()}.json`)
  }
  book = bookCache[key]
  updateSidebarHeader()
  renderTOC()
  if (sidebarMode === 'catalog') {
    await loadCatalog(s)
    renderCatalogPanel()
  }
  document.title = `${book.title} — 教科書`
  return book
}

function renderTOC() {
  const el = document.getElementById('toc')
  el.innerHTML = ''
  const isZh = settings.lang === 'zh'
  const copyBtn = (title, onClick) => {
    const b = document.createElement('button')
    b.className = 'toc-copy qbk-icon-btn compact'; b.type = 'button'
    b.title = title; b.setAttribute('aria-label', title)
    b.textContent = '⧉'
    b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); onClick(b) })
    return b
  }
  const buildItem = (kind, key, num, title, meta, sections) => {
    const hasSecs = (sections || []).length > 0
    const row = document.createElement('div')
    row.className = 'toc-row'
    row.dataset.kind = kind; row.dataset.key = String(key)
    const it = document.createElement('a')
    it.className = 'toc-item' + (hasSecs ? ' has-children' : '')
    it.href = chunkHash(kind, key)
    it.innerHTML =
      (hasSecs ? `<span class="toc-caret">▸</span>` : `<span class="toc-caret"></span>`) +
      `<span class="toc-num">${num}</span>` +
      `<span class="toc-title">${esc(title)}</span>` +
      (meta ? `<span class="toc-meta">${meta}</span>` : '')
    row.appendChild(it)
    row.appendChild(copyBtn('複製本章純文字', (b) => copyChapter(kind, key, b)))
    el.appendChild(row)
    if (hasSecs) {
      const list = document.createElement('div')
      list.className = 'toc-section-list'
      list.dataset.parentKind = kind; list.dataset.parentKey = String(key)
      sections.forEach(s => {
        const secRow = document.createElement('div')
        secRow.className = 'toc-sec-row'
        const si = document.createElement('a')
        si.className = 'toc-sec'
        si.href = chunkHash(kind, key, s.anchor)
        si.dataset.kind = kind; si.dataset.key = String(key); si.dataset.anchor = s.anchor
        si.dataset.level = String(s.level)
        si.innerHTML =
          `<span class="toc-sec-num">${s.id ? esc(s.id) : ''}</span>` +
          `<span class="toc-sec-title">${esc(s.title)}</span>`
        secRow.appendChild(si)
        secRow.appendChild(copyBtn('複製本節純文字', (b) => copySection(kind, key, s.anchor, b)))
        list.appendChild(secRow)
      })
      el.appendChild(list)
    }
  }
  if (book.chapters?.length) {
    el.insertAdjacentHTML('beforeend',
      `<div class="toc-group-label">${isZh ? '章節' : 'Chapters'}</div>`)
    book.chapters.forEach(c => buildItem('ch', c.num, `Ch ${c.num}`, c.title,
      `${c.problem_count}${isZh ? '題' : ' pb'}`, c.sections))
  }
  if (book.appendices?.length) {
    el.insertAdjacentHTML('beforeend',
      `<div class="toc-group-label">${isZh ? '附錄' : 'Appendices'}</div>`)
    book.appendices.forEach(a => buildItem('app', a.id, `App ${a.id}`, a.title, '', a.sections))
  }
  highlightTOC()
}

async function loadCatalog(s) {
  if (!s) return null
  if (!catalogCache[s]) {
    catalogCache[s] = await QBankShared.fetchJson(`data/${s}/catalogs.json`)
  }
  return catalogCache[s]
}

function catalogCounts() {
  const data = catalogCache[slug] || {}
  return {
    figures: (data.figures || []).filter(e => e.id).length,
    tables: (data.tables || []).filter(e => e.id).length,
    equations: (data.equations || []).filter(e => e.label).length,
  }
}

function catalogSearchText(e) {
  return [
    e.id, e.caption, e.section, e.problem, e.label, e.tex_preview,
    e.chunk_kind === 'ch' ? `ch ${e.chunk_key}` : `app ${e.chunk_key}`,
  ].filter(Boolean).join(' ').toLowerCase()
}

function filteredCatalogItems() {
  const data = catalogCache[slug] || {}
  const all = data[catalogType] || []
  const semantic = all.filter(e => catalogType === 'equations' ? e.label : e.id)
  const q = catalogQuery.trim().toLowerCase()
  if (!q) return semantic
  return semantic.filter(e => catalogSearchText(e).includes(q))
}

function catalogTypeLabel(type) {
  return { figures: 'Figure', tables: 'Table', equations: 'Equation' }[type] || 'Item'
}

function catalogDisplayId(e) {
  if (catalogType === 'equations') return e.label ? `(${e.label})` : 'Equation'
  return e.id || catalogTypeLabel(catalogType)
}

function catalogItemText(e) {
  if (catalogType === 'equations') {
    const tex = e.tex || e.tex_preview || ''
    return tex ? `$$${esc(tex)}$$` : ''
  }
  return e.caption || (e.problem ? `Problem ${e.problem}` : '')
}

function catalogMeta(e) {
  const loc = e.chunk_kind === 'ch' ? `Ch ${e.chunk_key}` : `App ${e.chunk_key}`
  const sec = e.section ? ` §${e.section}` : ''
  const prob = e.problem ? ` · P ${e.problem}` : ''
  return `${loc}${sec}${prob}`
}

function catalogItemHtml(e) {
  const active = e.chunk_kind === currentKind && String(e.chunk_key) === String(currentKey)
    && e.anchor === activeSectionAnchor
  const thumb = catalogType === 'figures' && e.src
    ? `<img class="catalog-thumb" data-kind="${escAttr(e.kind || '')}" src="img/${escAttr(slug)}/${escAttr(e.src)}" alt="">`
    : ''
  return `<div class="catalog-item${active ? ' active' : ''}" data-kind="${escAttr(e.chunk_kind)}" data-key="${escAttr(e.chunk_key)}" data-anchor="${escAttr(e.anchor)}">
    ${thumb}
    <div class="catalog-body">
      <div class="catalog-id">${esc(catalogDisplayId(e))}</div>
      <div class="catalog-meta">${esc(catalogMeta(e))}</div>
      <div class="catalog-text">${catalogType === 'equations' ? catalogItemText(e) : esc(catalogItemText(e))}</div>
    </div>
  </div>`
}

function renderCatalogPanel() {
  const list = document.getElementById('catalog-list')
  document.querySelectorAll('.catalog-type').forEach(b =>
    b.classList.toggle('active', b.dataset.catalogType === catalogType))
  const counts = catalogCounts()
  document.querySelectorAll('.catalog-type').forEach(b => {
    const n = counts[b.dataset.catalogType] || 0
    b.textContent = `${b.dataset.catalogType[0].toUpperCase()}${b.dataset.catalogType.slice(1)} ${n}`
  })
  if (!slug || !catalogCache[slug]) {
    list.innerHTML = '<div class="catalog-empty">No catalog</div>'
    return
  }
  const items = filteredCatalogItems()
  if (!items.length) {
    list.innerHTML = '<div class="catalog-empty">No matches</div>'
    return
  }
  const shown = items.slice(0, catalogLimit)
  list.innerHTML = shown.map(catalogItemHtml).join('')
  if (catalogType === 'equations') QBankShared.renderMath(list)
  list.querySelectorAll('.catalog-item').forEach((el, i) => {
    el.addEventListener('click', () => openCatalogDetail(shown[i], catalogType))
  })
  if (items.length > catalogLimit) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'qbk-control-btn compact catalog-load-more'
    btn.textContent = `More ${items.length - catalogLimit}`
    btn.onclick = () => { catalogLimit += 80; renderCatalogPanel() }
    list.appendChild(btn)
  }
}

function setupCatalogDetail() {
  document.getElementById('catalog-detail-close').addEventListener('click', closeCatalogDetail)
  document.getElementById('catalog-detail-cancel').addEventListener('click', closeCatalogDetail)
  document.getElementById('catalog-detail-modal').addEventListener('click', (e) => {
    if (e.target.id === 'catalog-detail-modal') closeCatalogDetail()
  })
  document.getElementById('catalog-detail-navigate').addEventListener('click', () => {
    const target = catalogDetailTarget
    closeCatalogDetail()
    if (target?.entry) jumpToCatalogEntry(target.entry)
  })
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.getElementById('catalog-detail-modal').classList.contains('open')) {
      closeCatalogDetail()
    }
  })
}

function closeCatalogDetail() {
  const modal = document.getElementById('catalog-detail-modal')
  if (!modal.classList.contains('open')) return
  modal.classList.remove('open')
  document.getElementById('catalog-detail-body').innerHTML = ''
  closeOverlay(modal)
  catalogDetailTarget = null
}

async function openCatalogDetail(entry, type) {
  if (!entry) return
  catalogDetailTarget = { entry, type }
  document.getElementById('catalog-detail-title').textContent = catalogDisplayIdForType(entry, type)
  document.getElementById('catalog-detail-meta').textContent = catalogMeta(entry)
  const body = document.getElementById('catalog-detail-body')
  body.innerHTML = '<div class="catalog-detail-fallback">Loading…</div>'
  const modal = document.getElementById('catalog-detail-modal')
  modal.classList.add('open')
  openOverlay(modal, { initialFocus: document.getElementById('catalog-detail-close') })
  const html = await catalogDetailHtml(entry, type)
  if (!catalogDetailTarget || catalogDetailTarget.entry !== entry) return
  body.innerHTML = html
  wrapInlineMath(body)
  QBankShared.renderMath(body)
}

function catalogDisplayIdForType(e, type) {
  if (type === 'equations') return e.label ? `(${e.label})` : 'Equation'
  return e.id || catalogTypeLabel(type)
}

async function catalogDetailHtml(entry, type) {
  if (type === 'equations') {
    const tex = entry.tex || entry.tex_preview || ''
    const tag = entry.label && !/\\tag\s*\{/.test(tex) ? ` \\tag{${esc(entry.label)}}` : ''
    return tex
      ? `<div class="eq" data-tex="${escAttr(`$$${tex}$$`)}">$$${esc(tex)}${tag}$$</div>`
      : '<div class="catalog-detail-fallback">Catalog audit failed: missing equation source.</div>'
  }
  const extracted = await extractCatalogAnchorHtml(entry)
  if (extracted) return extracted
  if (type === 'figures' && entry.src) {
    const cap = entry.caption ? `<figcaption>${esc(entry.caption)}</figcaption>` : ''
    const kind = entry.kind ? ` data-kind="${escAttr(entry.kind)}"` : ''
    return `<figure${kind}><img src="img/${escAttr(slug)}/${escAttr(entry.src)}" alt="${escAttr(entry.caption || '')}">${cap}</figure>`
  }
  const label = catalogDisplayIdForType(entry, type)
  return `<div class="catalog-detail-fallback">Catalog audit failed: ${esc(label)} has no resolvable anchor.</div>`
}

async function extractCatalogAnchorHtml(entry) {
  if (!entry?.anchor || !entry.chunk_kind || entry.chunk_key == null) return ''
  try {
    const data = await fetchChunk(entry.chunk_kind, entry.chunk_key)
    const secPrefix = entry.chunk_kind === 'ch' ? String(data.num) : `app${data.id}`
    const tmp = document.createElement('div')
    tmp.innerHTML = `${renderBody(data.body || [], secPrefix)}${renderProblems(data.problems || [])}`
    const target = [...tmp.querySelectorAll('[id]')].find(el => el.id === entry.anchor)
    if (!target) return ''
    target.querySelectorAll('.edit-block-btn').forEach(btn => btn.remove())
    if (target.closest('details')) {
      let details = target.closest('details')
      while (details) {
        details.open = true
        details = details.parentElement?.closest('details')
      }
    }
    return target.outerHTML
  } catch {
    return ''
  }
}

function jumpToCatalogEntry(entry) {
  if (!entry) return
  if (entry.chunk_kind === currentKind && String(entry.chunk_key) === String(currentKey)) {
    scrollToAnchor(entry.anchor)
    setActiveSection(entry.anchor)
  } else {
    pendingAnchor = entry.anchor
    navigate(slug, entry.chunk_kind, entry.chunk_key)
  }
  closeReaderSidebar()
}

function highlightTOC() {
  document.querySelectorAll('.toc-row').forEach(row => {
    const on = row.dataset.kind === currentKind && row.dataset.key === String(currentKey)
    row.classList.toggle('active', on)
    row.classList.toggle('expanded', on && Boolean(row.querySelector('.toc-item.has-children')))
  })
}

function jumpToSection(kind, key, anchor) {
  if (kind === currentKind && String(key) === String(currentKey)) {
    scrollToAnchor(anchor)
    setActiveSection(anchor)
    replaceHash(chunkHash(kind, key, anchor))
  } else {
    pendingAnchor = anchor
    navigate(slug, kind, key, anchor)
  }
}

function scrollToAnchor(anchor) {
  const el = document.getElementById(anchor)
  if (!el) return
  let details = el.closest('details')
  while (details) {
    details.open = true
    details = details.parentElement?.closest('details')
  }
  // 即時跳（不 smooth）：lazy 模式下平滑滾過長路徑會逐段觸發 typeset，等於重排整章。
  el.scrollIntoView({ block: 'start' })
  const content = document.getElementById('content')
  if (content && content._primeVisible) content._primeVisible()
}

function setActiveSection(anchor) {
  activeSectionAnchor = anchor || null
  document.querySelectorAll('.toc-sec').forEach(s =>
    s.closest('.toc-sec-row')?.classList.toggle('active', Boolean(anchor) && s.dataset.anchor === anchor))
  revealActiveTOC(anchor)
}

function revealActiveTOC(anchor) {
  const toc = document.getElementById('toc')
  const target = anchor
    ? toc.querySelector(`.toc-sec[data-anchor="${CSS.escape(anchor)}"]`)
    : toc.querySelector(`.toc-row.active`)
  if (!target) return
  target.scrollIntoView({ block: 'nearest' })
}

function cleanupSectionSpy() {
  if (sectionObserver) {
    sectionObserver.disconnect()
    sectionObserver = null
  }
  const content = document.getElementById('content')
  if (sectionScrollHandler) {
    content.removeEventListener('scroll', sectionScrollHandler)
    sectionScrollHandler = null
  }
  activeSectionAnchor = null
  setActiveSection(null)
}

function cleanupMathObserver() {
  const content = document.getElementById('content')
  if (content && mathScrollHandler) {
    content.removeEventListener('scroll', mathScrollHandler)
    mathScrollHandler = null
  }
  if (mathResizeHandler) {
    window.removeEventListener('resize', mathResizeHandler)
    mathResizeHandler = null
  }
  if (mathVisibilityHandler) {
    document.removeEventListener('visibilitychange', mathVisibilityHandler)
    mathVisibilityHandler = null
  }
}

// 整章一次 typeset 上千條公式 → 阻塞數秒、17 萬 DOM 節點、分頁 RAM 數百 MB。
// 改為「視窗化 + derender」：只保留視窗 ± RENDER_MARGIN 的公式排版；
// 捲出 DERENDER_MARGIN 的 unit 還原成輕量原始碼（釋放 MathJax DOM），記憶體因此封頂。
// derender 時以 placeholder 高度占位 → 版面不位移、視覺無感。
const RENDER_MARGIN = 900    // 視窗上下預先排版的緩衝（px）；進視窗前就排好，正常捲動不見原始碼
const DERENDER_MARGIN = 3000 // 超出此距離才還原；必須 > RENDER_MARGIN，避免邊界反覆 render/derender 抖動

function setupIncrementalMath(content) {
  cleanupMathObserver()
  const article = content.querySelector('.article')
  if (!article) return Promise.resolve()

  // render unit：article 的頂層子節點；題目區拆成每一題各自一個 unit。
  const units = []
  for (const child of article.children) {
    if (child.classList && child.classList.contains('problems-section')) {
      child.querySelectorAll(':scope > .problem').forEach(p => units.push(p))
    } else {
      units.push(child)
    }
  }
  const rendered = new Set()   // 目前已 typeset 的 unit（derender 掃描只看這些，O(視窗數)）

  // 狀態三態：raw（原始碼）→ rendering（排版中，擋重入）→ rendered。
  // 排版失敗/MathJax 未就緒時退回 raw，讓下一次捲動 refresh 重試——若在此提前記成
  // rendered，一次冷載逾時就會讓整章數學永久停在原始碼。
  const renderUnit = async (el) => {
    if (!el || !el.isConnected) return
    if (el.dataset.mathState === 'rendered' || el.dataset.mathState === 'rendering') return
    if (el._mathRaw == null) el._mathRaw = el.innerHTML   // 首次排版前存原始碼
    el.dataset.mathState = 'rendering'
    el.style.minHeight = ''
    const ok = await QBankShared.renderMath(el)
    if (!el.isConnected) return
    if (ok) { el.dataset.mathState = 'rendered'; rendered.add(el) }
    else { delete el.dataset.mathState }
  }
  const derenderUnit = (el) => {
    if (el.dataset.mathState !== 'rendered' || el._mathRaw == null) return
    el.style.minHeight = el.getBoundingClientRect().height + 'px'  // 占位防版面跳動
    el.innerHTML = el._mathRaw
    el.dataset.mathState = 'raw'
    rendered.delete(el)
  }

  // 視窗 refresh：① derender 離開 keep 範圍的 unit；② 排版 render 範圍內未排版的 unit（逐個 await，
  // 讓高度隨排版成長後再決定下一個，杜絕「排版前量壓縮高度導致過度選取」）。re-entrancy 用 flag 防併發。
  let refreshing = false, pending = false
  const refresh = async () => {
    if (refreshing) { pending = true; return }
    refreshing = true
    try {
      const cr = content.getBoundingClientRect()
      const H = cr.height
      // ① derender 離開 keep 範圍者
      for (const el of [...rendered]) {
        const r = el.getBoundingClientRect()
        const top = r.top - cr.top, bot = r.bottom - cr.top
        if (bot < -DERENDER_MARGIN || top > H + DERENDER_MARGIN) derenderUnit(el)
      }
      // ② render 範圍：二分搜第一個 bottom 進入 render 視窗者，往下逐個排版
      let lo = 0, hi = units.length - 1, start = units.length
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (units[mid].getBoundingClientRect().bottom - cr.top >= -RENDER_MARGIN) { start = mid; hi = mid - 1 }
        else lo = mid + 1
      }
      for (let i = start; i < units.length; i++) {
        if (units[i].getBoundingClientRect().top - cr.top > H + RENDER_MARGIN) break
        await renderUnit(units[i])   // await → 排版後高度更新，下一輪 rect 反映真實位置
      }
    } finally {
      refreshing = false
      if (pending) { pending = false; refresh() }
    }
  }
  content._primeVisible = refresh
  content._typesetUnit = renderUnit

  let ticking = false
  mathScrollHandler = () => {
    if (ticking) return
    ticking = true
    requestAnimationFrame(() => { ticking = false; refresh() })
  }
  content.addEventListener('scroll', mathScrollHandler, { passive: true })
  mathResizeHandler = () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { ticking = false; refresh() }) } }
  window.addEventListener('resize', mathResizeHandler)
  // 分頁在背景時 rAF 停擺 → 捲動事件不會觸發排版（例如從別的分頁開錨點連結進來）。
  // 回到前景補跑一次，避免使用者切回來看到一整片未排版的原始碼。
  mathVisibilityHandler = () => { if (document.visibilityState === 'visible') refresh() }
  document.addEventListener('visibilitychange', mathVisibilityHandler)

  refresh()   // 初始排版視窗內 unit（同步呼叫，不依賴 rAF）
  return Promise.resolve()
}

function headingAnchorForScroll(content) {
  const heads = [...content.querySelectorAll('.article > .sec-h')]
  if (!heads.length) return null
  const readingLine = content.scrollTop + content.clientHeight * 0.18
  let current = null
  for (const h of heads) {
    if (h.offsetTop <= readingLine) current = h
    else break
  }
  return current?.id || null
}

function updateActiveSectionFromScroll() {
  if (!currentKind) return
  const content = document.getElementById('content')
  const anchor = headingAnchorForScroll(content)
  if (anchor !== activeSectionAnchor) {
    setActiveSection(anchor)
    scheduleProgressSave()
  }
}

function setupSectionSpy() {
  cleanupSectionSpy()
  const content = document.getElementById('content')
  const headings = [...content.querySelectorAll('.article > .sec-h')]
  if (!headings.length) return

  if ('IntersectionObserver' in window) {
    sectionObserver = new IntersectionObserver(() => updateActiveSectionFromScroll(), {
      root: content,
      rootMargin: '-15% 0px -75% 0px',
      threshold: [0, 1],
    })
    headings.forEach(h => sectionObserver.observe(h))
  }

  let ticking = false
  sectionScrollHandler = () => {
    if (ticking) return
    ticking = true
    requestAnimationFrame(() => {
      ticking = false
      updateActiveSectionFromScroll()
      scheduleProgressSave()
    })
  }
  content.addEventListener('scroll', sectionScrollHandler, { passive: true })
  updateActiveSectionFromScroll()
}

// ── navigation ──
// 路由文法（建網址／解網址）在 router.js；這裡只做「解出來之後要對 app 做什麼」。
function chunkHash(kind, key, anchor) {
  return buildHash({ slug, kind, key, anchor })
}

function navigate(s, kind, key, anchor) {
  persistCurrentProgress()
  routerGo({ slug: s, kind, key, anchor })
}

async function applyRoute({ restoreSaved = false } = {}) {
  const route = parseRoute()
  if (!route.slug) {
    // 開站直接進上次讀到的地方（只在首次載入做；之後的空 hash＝使用者主動回書庫）
    if (restoreSaved) {
      const last = loadProgress().last
      if (last?.slug && last.kind && last.key != null && books.find(b => b.slug === last.slug)) {
        navigate(last.slug, last.kind, last.key)
        return
      }
    }
    showLibrary(); return
  }
  pendingProblemNum = route.params.get('problem')
  const s = route.slug
  if (!books.find(b => b.slug === s)) {
    showLibrary()
    document.getElementById('lib-groups').insertAdjacentHTML('afterbegin',
      `<div class="lib-empty">找不到書：${esc(s)}</div>`)
    return
  }
  document.getElementById('app').dataset.view = 'reader'
  closeReaderSidebar()
  await loadBook(s)
  if (route.kind && route.key != null) {
    const { kind, key, anchor } = route
    if (!validChunkRef(kind, key)) {
      showBookOverview()
      return
    }
    // 同一章內換錨點：只捲動，不重建整章 DOM（重建 = 丟掉已排版的數學與捲動脈絡）
    if (book && kind === currentKind && String(key) === String(currentKey)) {
      if (anchor) { scrollToAnchor(anchor); setActiveSection(anchor) }
      return
    }
    pendingAnchor = anchor || pendingAnchor
    await showChunk(kind, key)
  } else {
    const saved = lastProgressFor(s)
    if (saved) navigate(saved.slug, saved.kind, saved.key)
    else showBookOverview()
  }
}

window.addEventListener('hashchange', () => applyRoute())

function showBookOverview() {
  cleanupSectionSpy()
  currentKind = currentKey = null
  highlightTOC()
  document.getElementById('crumb').innerHTML =
    `<span class="cur">${esc(book.title)}</span> <span style="color:var(--sub)">— ${book.chapters?.length||0} 章</span>`
  document.getElementById('btn-prev').disabled = true
  document.getElementById('btn-next').disabled = true
  updateReaderPanel()
  document.getElementById('content').innerHTML = `
    <div class="article">
      <h1>${esc(book.title)}</h1>
      <p style="color:var(--sub);margin-top:6px;font-style:italic">${esc(book.author||'')}${book.edition?` · ${esc(book.edition)} edition`:''}</p>
      <p>從左側選章節開始閱讀。</p>
    </div>`
}

function validChunkRef(kind, key) {
  if (kind === 'ch') return (book.chapters || []).some(c => String(c.num) === String(key))
  if (kind === 'app') return (book.appendices || []).some(a => String(a.id) === String(key))
  return false
}

async function showChunk(kind, key, { preserveScroll = false } = {}) {
  cleanupSectionSpy()
  cleanupMathObserver()
  currentKind = kind; currentKey = key
  highlightTOC()
  const isZh = settings.lang === 'zh'
  const content = document.getElementById('content')
  const previousScrollTop = preserveScroll ? content.scrollTop : 0
  const savedPosition = preserveScroll || pendingAnchor ? null : chunkProgress(slug, kind, key)
  content.innerHTML = SKELETON_HTML
  content.scrollLeft = 0
  let data
  try {
    data = await fetchChunk(kind, key)
  } catch (e) {
    content.innerHTML = `<div class="chunk-error">
      <div>章節載入失敗：${esc(e.message || 'network error')}</div>
      <button class="qbk-control-btn" id="chunk-retry" type="button">重試</button></div>`
    document.getElementById('chunk-retry').onclick = () => showChunk(kind, key, { preserveScroll })
    return
  }

  const label = kind === 'ch' ? `Ch ${data.num}` : `App ${data.id}`
  document.getElementById('crumb').innerHTML =
    `${esc(book.title)} <span style="color:var(--ink-light);margin:0 6px">›</span> <span class="cur">${label} ${esc(data.title)}</span>`
  setupNav(kind, key)

  const heading = kind === 'ch'
    ? (isZh ? `第 ${data.num} 章` : `Chapter ${data.num}`)
    : (isZh ? `附錄 ${data.id}` : `Appendix ${data.id}`)
  const secPrefix = kind === 'ch' ? String(data.num) : `app${data.id}`
  const titleZh = biReveal(data.title_zh)
  const articleHtml = `
    <article class="article">
      <h1><span class="ch-num">${heading}</span>${esc(data.title)}</h1>
      ${titleZh}
      ${renderBody(data.body || [], secPrefix)}
      ${renderProblems(data.problems || [])}
    </article>`
  content.innerHTML = articleHtml
  groupAdjacentFigures(content)
  wrapInlineMath(content)
  applySearchHighlight(content)   // 必須早於 setupIncrementalMath：_mathRaw 快照要含這些 <mark>
  content.scrollLeft = 0

  // 先定位捲動位置（用尚未 typeset 的 DOM offset），再啟動增量排版。
  // 增量模式下高度會隨捲動成長，故以 anchor 為主、pixel 為輔，並在 typeset 後再校正一次。
  const restoreAnchor = pendingAnchor || (savedPosition && savedPosition.anchor) || null
  const problemAnchor = pendingProblemNum ? `prob-${pendingProblemNum}` : null
  if (problemAnchor && document.getElementById(problemAnchor)) {
    scrollToAnchor(problemAnchor)
    pendingProblemNum = null
  } else if (restoreAnchor && document.getElementById(restoreAnchor)) {
    scrollToAnchor(restoreAnchor)
    setActiveSection(restoreAnchor)
  } else if (savedPosition) {
    restoreProgressPosition(savedPosition)
  } else {
    content.scrollTop = previousScrollTop
  }

  // renderMath 會輪詢等 MathJax CDN ready（硬重載時 CDN 可能晚於首次 showChunk）
  setupIncrementalMath(content)
  setupSectionSpy()

  // 視窗內 unit 在下一兩個 frame 排版完，高度確定後把 anchor 重新校正到頂。
  if (restoreAnchor && document.getElementById(restoreAnchor)) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (currentKind === kind && String(currentKey) === String(key) && document.getElementById(restoreAnchor)) {
        scrollToAnchor(restoreAnchor)
        if (content._primeVisible) content._primeVisible()
      }
    }))
  }
  if (pendingSearchScroll) {
    pendingSearchScroll = false
    requestAnimationFrame(() => focusSearchHit(content, restoreAnchor))
  }
  if (pendingAnchor) {
    pendingAnchor = null
    persistCurrentProgress()
  }
  if (sidebarMode === 'catalog') renderCatalogPanel()
}

// MinerU 常把 (a)(b) 子圖拆成獨立 figure block；把相鄰 figure 包進 .fig-row 並排顯示
function groupAdjacentFigures(root) {
  root.querySelectorAll('article.article, .problem-body, .solution-body').forEach(scope => {
    const children = [...scope.children]
    let i = 0
    while (i < children.length) {
      if (children[i].tagName !== 'FIGURE') { i++; continue }
      let j = i + 1
      while (j < children.length && children[j].tagName === 'FIGURE') j++
      if (j - i >= 2) {
        const row = document.createElement('div')
        row.className = 'fig-row'
        scope.insertBefore(row, children[i])
        for (let k = i; k < j; k++) row.appendChild(children[k])
      }
      i = j
    }
  })
}

// ── 數學式複製 ─────────────────────────────────────────────────────
// block：.eq 由 data-tex 承載完整 LaTeX（見 renderBlock）。
// inline：typeset 前把段落文字裡的 $...$ 包成 .qb-inline-math 並記原始碼，
//         MathJax 隨後在 span 內 typeset；原始碼留在 span 上供複製。
function wrapInlineMath(root) {
  const blocked = (node) => {
    let p = node.parentElement
    while (p && p !== root) {
      if (p.classList && (p.classList.contains('eq') || p.classList.contains('qb-inline-math'))) return true
      if (p.tagName === 'SCRIPT' || p.tagName === 'STYLE') return true
      p = p.parentElement
    }
    return false
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.nodeValue.indexOf('$') === -1) return NodeFilter.FILTER_REJECT
      return blocked(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    },
  })
  const re = /\$\$[\s\S]*?\$\$|\$[^$\n]+?\$/g
  const targets = []
  let tn
  while ((tn = walker.nextNode())) targets.push(tn)
  targets.forEach((node) => {
    const text = node.nodeValue
    re.lastIndex = 0
    if (!re.test(text)) return
    re.lastIndex = 0
    const frag = document.createDocumentFragment()
    let last = 0, m
    while ((m = re.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)))
      const span = document.createElement('span')
      span.className = 'qb-inline-math'
      span.dataset.tex = m[0]
      span.textContent = m[0]
      frag.appendChild(span)
      last = m.index + m[0].length
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)))
    node.parentNode.replaceChild(frag, node)
  })
}

let copyToastTimer = null
function showCopyToast(msg) {
  let t = document.getElementById('copy-toast')
  if (!t) { t = document.createElement('div'); t.id = 'copy-toast'; document.body.appendChild(t) }
  t.textContent = msg
  t.classList.add('show')
  if (copyToastTimer) clearTimeout(copyToastTimer)
  copyToastTimer = setTimeout(() => t.classList.remove('show'), 1300)
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true } catch {}
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}

// 選取範圍 → 純文字：每個數學元素換成它的完整 LaTeX 原始碼（含 $ 分隔符）。
// 無數學則回傳 null（交還瀏覽器預設複製，不破壞一般文字選取）。
function selectionToLatexText(range) {
  const host = document.createElement('div')
  host.appendChild(range.cloneContents())
  if (!host.querySelector('[data-tex]')) return null
  host.querySelectorAll('[data-tex]').forEach((el) => {
    const tex = el.getAttribute('data-tex')
    const block = el.classList.contains('eq')
    el.replaceWith(document.createTextNode(block ? '\n' + tex + '\n' : tex))
  })
  host.querySelectorAll('p, h1, h2, h3, li, figcaption').forEach((el) => el.append('\n'))
  return host.textContent.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim()
}

function setupMathCopy() {
  const content = document.getElementById('content')
  // block：點擊整塊複製（拖曳選取中則不觸發，留給 copy 事件）
  content.addEventListener('click', (e) => {
    const eq = e.target.closest('.eq')
    if (!eq || !eq.dataset.tex) return
    const sel = window.getSelection()
    if (sel && !sel.isCollapsed && content.contains(sel.anchorNode)) return
    copyText(eq.dataset.tex).then((ok) => {
      if (!ok) return
      eq.classList.add('copied')
      setTimeout(() => eq.classList.remove('copied'), 1300)
      showCopyToast('已複製 LaTeX')
    })
  })
  // inline / 混合：Cmd-C 時選到任何數學式 → 整段 LaTeX 進剪貼簿
  document.addEventListener('copy', (e) => {
    const sel = window.getSelection()
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return
    if (!content.contains(sel.anchorNode) && !content.contains(sel.focusNode)) return
    const text = selectionToLatexText(sel.getRangeAt(0))
    if (text == null) return
    e.clipboardData.setData('text/plain', text)
    e.preventDefault()
    showCopyToast('已複製（含 LaTeX）')
  })
}

function setupNav(kind, key) {
  const items = kind === 'ch' ? book.chapters : book.appendices
  const idField = kind === 'ch' ? 'num' : 'id'
  const idx = items.findIndex(it => String(it[idField]) === String(key))
  const prev = idx > 0 ? items[idx-1] : null
  const next = idx >= 0 && idx < items.length-1 ? items[idx+1] : null
  const prevBtn = document.getElementById('btn-prev')
  const nextBtn = document.getElementById('btn-next')
  prevBtn.disabled = !prev
  nextBtn.disabled = !next
  prevBtn.onclick = prev ? () => navigate(slug, kind, prev[idField]) : null
  nextBtn.onclick = next ? () => navigate(slug, kind, next[idField]) : null
  updateReaderPanel({
    prev: prev ? prev[idField] : null,
    next: next ? next[idField] : null,
  })
}

// ── render body / problems ──
// 區塊 → HTML 在 blocks.js（與題庫頁共用）；這裡只補上 reader 的情境：目前這本書的
// slug（圖片路徑）、雙語模式、以及沒有 id 的節標題要用什麼前綴合成錨點。
function renderBody(blocks, secPrefix) {
  return renderBlocks(blocks, { slug, bi: settings.lang === 'bi', secPrefix })
}

// 題目區是 reader 專屬的版面（題號、解答摺疊、跳到題庫頁），不進 blocks.js。
function renderProblems(probs) {
  if (!probs.length) return ''
  const isZh = settings.lang === 'zh'
  const heading = isZh ? '題目' : 'Problems'
  const probLabel = isZh ? '題' : 'Problem'
  const solLabel = isZh ? '展開解答' : 'Show solution'
  const emptyLabel = isZh ? '⚠ 此題 OCR 漏抓，請查原書' : '⚠ OCR missing — see original PDF'
  return `<section class="problems-section">
    <h2>${heading}</h2>
    ${probs.map(p => `
      <div class="problem" id="prob-${escAttr(p.num)}">
        <div class="problem-tools">
          <a class="problem-open" href="problems.html#${encodeURIComponent(`tb:${slug}:${currentKind}:${currentKey}:p:${p.num}`)}">Open in Problems</a>
        </div>
        <div class="problem-num">${probLabel} ${esc(p.num)}</div>
        <div class="problem-body">${(p.body && p.body.length)
          ? renderBody(p.body, `p${p.num}`)
          : `<div class="problem-body-empty">${emptyLabel}</div>`}</div>
        ${p.solution && p.solution.length ? `
        <details class="problem-solution">
          <summary>${solLabel}</summary>
          <div class="solution-body">${renderBody(p.solution, `s${p.num}`)}</div>
        </details>` : ''}
      </div>`).join('')}
  </section>`
}

async function fetchChunk(kind, key) {
  const ck = `${slug}/${kind}/${key}/${settings.lang}`
  if (chunkCache[ck]) return chunkCache[ck]
  const base = kind === 'ch' ? `data/${slug}/ch/${key}` : `data/${slug}/app/${key}`
  const data = await QBankShared.fetchJson(`${base}${langSuffix()}.json`)
  chunkCache[ck] = data
  return data
}

function chunkToText(kind, data) {
  const head = kind === 'ch'
    ? `Chapter ${data.num} — ${data.title}`
    : `Appendix ${data.id} — ${data.title}`
  return [head, blocksToText(data.body), problemsToText(data.problems)]
    .filter(Boolean).join('\n\n').trim()
}

function sectionToText(kind, data, anchor) {
  const body = data.body || []
  const secPrefix = kind === 'ch' ? String(data.num) : `app${data.id}`
  let counter = 0
  const idxOf = {}
  body.forEach((b, i) => {
    if (b.t === 'section' || b.t === 'subsection') {
      counter += 1
      const id = (b.id || '').trim()
      idxOf[id ? `sec-${id}` : `sec-${secPrefix}-${counter}`] = i
    }
  })
  const start = idxOf[anchor]
  if (start == null) return chunkToText(kind, data)
  const startIsSection = body[start].t === 'section'
  let end = body.length
  for (let i = start + 1; i < body.length; i++) {
    const t = body[i].t
    if (t === 'section' || (!startIsSection && t === 'subsection')) { end = i; break }
  }
  return blocksToText(body.slice(start, end)).trim()
}

function flashCopy(ok, btn) {
  showCopyToast(ok ? '已複製純文字' : '複製失敗')
  if (ok && btn) { btn.classList.add('copied'); setTimeout(() => btn.classList.remove('copied'), 900) }
}

async function copyChapter(kind, key, btn) {
  try {
    const data = await fetchChunk(kind, key)
    flashCopy(await copyText(chunkToText(kind, data)), btn)
  } catch { showCopyToast('載入失敗') }
}

async function copySection(kind, key, anchor, btn) {
  try {
    const data = await fetchChunk(kind, key)
    flashCopy(await copyText(sectionToText(kind, data, anchor)), btn)
  } catch { showCopyToast('載入失敗') }
}

init()
