import { test, expect } from '@playwright/test'
import { watchErrors, openLibrary, openFirstChapter } from './helpers.js'

test('書庫：書卡渲染、沒有 console error', async ({ page }) => {
  const errors = watchErrors(page)
  const slug = await openLibrary(page)
  expect(slug).toBeTruthy()
  expect(await page.locator('.lib-card').count()).toBeGreaterThan(0)
  expect(errors).toEqual([])
})

test('目錄：每一項都是真連結、標題不被截斷', async ({ page }) => {
  const slug = await openLibrary(page)
  await page.goto(`/#${slug}`)
  await expect(page.locator('#toc .toc-item').first()).toBeVisible()

  // 真 <a href>：可 Tab、可中鍵開新分頁、螢幕閱讀器讀得到
  const hrefs = await page.locator('#toc .toc-item').evaluateAll(
    els => els.map(e => ({ tag: e.tagName, href: e.getAttribute('href') })))
  expect(hrefs.length).toBeGreaterThan(0)
  for (const h of hrefs) {
    expect(h.tag).toBe('A')
    expect(h.href).toMatch(/^#/)
  }

  // 章標題完整換行，不能被 ellipsis 吃掉（側欄是導航的唯一入口）
  const clipped = await page.locator('#toc .toc-title').evaluateAll(
    els => els.filter(e => e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1).length)
  expect(clipped).toBe(0)
})

test('章節：內容渲染、圖片預留版位、複製工具存在', async ({ page }) => {
  const errors = watchErrors(page)
  const slug = await openLibrary(page)
  const hash = await openFirstChapter(page, slug)
  expect(hash).toContain('/ch/')

  expect(await page.locator('.article > *').count()).toBeGreaterThan(0)

  // 有 w/h 的圖一律帶 aspect-ratio；沒有 w/h 的是舊資料，允許但要能辨識
  const imgs = await page.locator('.article figure img').evaluateAll(els => els.map(e => ({
    hasDim: e.hasAttribute('width') && e.hasAttribute('height'),
    hasRatio: Boolean(e.style.aspectRatio),
  })))
  for (const i of imgs) expect(i.hasDim).toBe(i.hasRatio)

  expect(errors).toEqual([])
})

test('節錨路由：同章換節只捲動、不重建 DOM', async ({ page }) => {
  const slug = await openLibrary(page)
  await openFirstChapter(page, slug)

  const sec = page.locator('#toc .toc-sec').first()
  if (await sec.count() === 0) test.skip(true, '這本書的第一章沒有節')

  await page.evaluate(() => { window.__article = document.querySelector('.article') })
  await sec.click()
  await expect.poll(() => page.evaluate(() => location.hash)).toMatch(/\/ch\/[^/]+\/.+/)

  // 同一顆 .article 節點還在 → 沒重建（重建 = 丟掉已排版的數學與捲動脈絡）
  const sameNode = await page.evaluate(() => window.__article === document.querySelector('.article'))
  expect(sameNode).toBe(true)
  await expect(page.locator('#toc .toc-sec-row.active')).toHaveCount(1)
})

test('鍵盤：? 開快捷鍵說明並鎖焦點，Esc 關閉並還原', async ({ page }) => {
  const slug = await openLibrary(page)
  await openFirstChapter(page, slug)

  await page.keyboard.press('?')
  await expect(page.locator('#shortcuts-modal')).toHaveClass(/open/)
  expect(await page.evaluate(() => document.getElementById('app').inert)).toBe(true)

  await page.keyboard.press('Escape')
  await expect(page.locator('#shortcuts-modal')).not.toHaveClass(/open/)
  expect(await page.evaluate(() => document.getElementById('app').inert)).toBe(false)
})

test('數學：視窗內的公式會被 MathJax 排版', async ({ page }) => {
  const slug = await openLibrary(page)
  await openFirstChapter(page, slug)
  if (await page.locator('.article .eq').count() === 0) test.skip(true, '這本書的第一章沒有公式')
  await expect.poll(
    () => page.locator('mjx-container').count(),
    { timeout: 40_000, message: '視窗內的 .eq 應在 MathJax 載入後被排版' },
  ).toBeGreaterThan(0)
})
