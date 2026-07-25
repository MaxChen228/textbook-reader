import { test, expect } from '@playwright/test'

/**
 * 純函式模組的單元測試。跑在真瀏覽器裡（blocks.js 的表格轉文字要 DOM），
 * 但不碰 app 狀態：直接動態 import 模組，餵資料、看回傳。
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')   // 需要一個已載入 qbank-shared.js 的頁面（shared.js 是它的轉接層）
})

test('router：建網址與解網址互為反函式（含編碼）', async ({ page }) => {
  const out = await page.evaluate(async () => {
    const { buildHash, parseHash } = await import('/assets/js/router.js')
    const route = { slug: 'stewart calculus', kind: 'ch', key: '3', anchor: 'sec-3.2' }
    const hash = buildHash(route)
    const back = parseHash(hash)
    return {
      hash,
      back: { slug: back.slug, kind: back.kind, key: back.key, anchor: back.anchor },
      library: buildHash({}),
      overview: buildHash({ slug: 'x' }),
      withQuery: buildHash({ slug: 'x', kind: 'ch', key: '1', params: { problem: '7' } }),
      queryBack: parseHash('#x/ch/1?problem=7').params.get('problem'),
      empty: parseHash('#').slug,
    }
  })
  expect(out.hash).toBe('#stewart%20calculus/ch/3/sec-3.2')
  expect(out.back).toEqual({ slug: 'stewart calculus', kind: 'ch', key: '3', anchor: 'sec-3.2' })
  expect(out.library).toBe('#')
  expect(out.overview).toBe('#x')
  expect(out.withQuery).toBe('#x/ch/1?problem=7')
  expect(out.queryBack).toBe('7')
  expect(out.empty).toBeNull()
})

test('blocks：圖片有 w/h 時預留版位（CLS 防線）', async ({ page }) => {
  const html = await page.evaluate(async () => {
    const { renderBlocks } = await import('/assets/js/blocks.js')
    return renderBlocks([{ t: 'fig', src: 'a.webp', w: 800, h: 400, caption: 'Fig 1' }], { slug: 'demo' })
  })
  expect(html).toContain('aspect-ratio:800/400')
  expect(html).toContain('width="800"')
  expect(html).toContain('loading="lazy"')
  expect(html).toContain('img/demo/a.webp')
})

test('blocks：表格轉純文字要帶表身（不是只有 caption）', async ({ page }) => {
  const text = await page.evaluate(async () => {
    const { blockToText } = await import('/assets/js/blocks.js')
    return blockToText({
      t: 'table',
      caption: 'Table 2.1',
      html: '<table><tr><td>alpha</td><td>1</td></tr><tr><td>beta</td><td>2</td></tr></table>',
      footnote: 'note',
    })
  })
  expect(text).toContain('Table 2.1')
  expect(text).toContain('alpha\t1')
  expect(text).toContain('beta\t2')
  expect(text).toContain('note')
})

test('blocks：eq 轉文字保留 $$ 與 \\tag', async ({ page }) => {
  const out = await page.evaluate(async () => {
    const { blockToText } = await import('/assets/js/blocks.js')
    return [
      blockToText({ t: 'eq', tex: 'x=1', label: '2.3' }),
      blockToText({ t: 'eq', tex: 'y=2 \\tag{9}', label: '2.4' }),   // tex 已自帶 tag → 不重複補
    ]
  })
  expect(out[0]).toBe('$$x=1 \\tag{2.3}$$')
  expect(out[1]).toBe('$$y=2 \\tag{9}$$')
})

test('blocks：節標題產生穩定錨點；headings:false 時不畫標題', async ({ page }) => {
  const out = await page.evaluate(async () => {
    const { renderBlocks } = await import('/assets/js/blocks.js')
    const blocks = [
      { t: 'section', id: '3.2', title: 'Limits' },
      { t: 'section', title: 'Unnumbered' },
      { t: 'p', md: 'hello' },
    ]
    return {
      withHeadings: renderBlocks(blocks, { secPrefix: '3' }),
      without: renderBlocks(blocks, { secPrefix: '3', headings: false }),
      empty: renderBlocks([], { empty: '<p>none</p>' }),
    }
  })
  expect(out.withHeadings).toContain('id="sec-3.2"')
  expect(out.withHeadings).toContain('id="sec-3-2"')   // 沒 id 的節 → sec-<前綴>-<序>
  expect(out.without).not.toContain('<h2')
  expect(out.without).toContain('hello')
  expect(out.empty).toBe('<p>none</p>')
})

test('store：壞掉的 localStorage 退回預設值而不是炸掉', async ({ page }) => {
  const out = await page.evaluate(async () => {
    localStorage.setItem('textbook.settings.v1', '{ not json')
    const { loadSettings, clampStep } = await import('/assets/js/store.js')
    const a = loadSettings()
    localStorage.setItem('textbook.settings.v1', JSON.stringify({ lang: 'klingon', fsStep: 99 }))
    const b = loadSettings()
    return { a, b, clamped: [clampStep(3, 5), clampStep('x', 5), clampStep(11, 5)] }
  })
  expect(out.a.lang).toBe('en')
  expect(out.b.lang).toBe('en')
  expect(out.b.fsStep).toBe(4)
  expect(out.clamped).toEqual([3, 5, 5])
})
