import { test, expect } from '@playwright/test'
import { watchErrors } from './helpers.js'

/**
 * 換書一律走 SPA 內部導航（改 hash，不重載頁面）——這才是會累積污染的路徑。
 * 用 window.__spa 當哨兵：整頁重載會把它沖掉，測試就會失敗，避免測到假的「乾淨」。
 */
async function switchBook(page, slug) {
  await page.evaluate(s => { window.__spa = true; location.hash = s }, slug)
  await page.waitForFunction(() => window.__spa === true)
  await expect(page.locator('#toc .toc-item').first()).toBeVisible()
}

/** 取兩本不同的已收錄書。 */
async function twoBooks(page) {
  await page.goto('/')
  await expect(page.locator('.lib-card[data-slug]').first()).toBeVisible()
  const slugs = await page.locator('.lib-card[data-slug]').evaluateAll(
    els => els.slice(0, 2).map(e => e.dataset.slug))
  test.skip(slugs.length < 2, '站上不足兩本書')
  return slugs
}

test('換書：上一本的搜尋結果與查詢字不會留到下一本', async ({ page }) => {
  const errors = watchErrors(page)
  const [a, b] = await twoBooks(page)

  await switchBook(page, a)
  await page.locator('#toc .toc-item').first().click()
  await expect(page.locator('.article')).toBeVisible()

  const term = await page.evaluate(() =>
    (document.querySelector('.article').textContent.match(/[A-Za-z]{6,12}/g) || [])[0] || '')
  test.skip(!term, '第一本書的第一章沒有可用的英文詞')

  await page.keyboard.press('/')
  await page.locator('#book-search').fill(term)
  await expect.poll(() => page.locator('.search-hit-row').count(), { timeout: 30_000 }).toBeGreaterThan(0)

  // 換到另一本書 → 搜尋面板必須整個歸零（留著會讓命中列點下去導到新書不存在的章）
  await switchBook(page, b)
  expect(await page.evaluate(() => window.__spa)).toBe(true)   // 確認沒有整頁重載
  await expect(page.locator('.search-hit-row')).toHaveCount(0)
  expect(await page.locator('#book-search').inputValue()).toBe('')
  expect(await page.locator('#search-status').textContent()).toBe('')
  expect(errors).toEqual([])
})

test('換書：目錄與麵包屑換成新書的，不殘留舊書節點', async ({ page }) => {
  const [a, b] = await twoBooks(page)

  await switchBook(page, a)
  const titleA = await page.locator('#toc .toc-title').first().textContent()

  await switchBook(page, b)
  const titleB = await page.locator('#toc .toc-title').first().textContent()
  expect(titleB).not.toBe(titleA)

  // 側欄標頭是新書；目錄連結全部指向新書
  const hrefs = await page.locator('#toc .toc-item').evaluateAll(els => els.map(e => e.getAttribute('href')))
  for (const h of hrefs) expect(decodeURIComponent(h)).toContain(`#${b}/`)
})

test('換書：只保留當前這本的 chunk 快取（跨書累積會吃掉幾百 MB）', async ({ page }) => {
  const [a, b] = await twoBooks(page)

  await switchBook(page, a)
  await page.locator('#toc .toc-item').first().click()
  await expect(page.locator('.article')).toBeVisible()

  const reqs = []
  page.on('request', r => { if (r.url().includes('/data/')) reqs.push(r.url()) })

  await switchBook(page, b)
  await page.locator('#toc .toc-item').first().click()
  await expect(page.locator('.article')).toBeVisible()
  // 回到第一本的同一章：快取已被清掉 → 必須重新抓（證明沒有跨書堆積）
  reqs.length = 0
  await switchBook(page, a)
  await page.locator('#toc .toc-item').first().click()
  await expect(page.locator('.article')).toBeVisible()
  await expect.poll(() => reqs.filter(u => u.includes(`/data/${a}/`)).length).toBeGreaterThan(0)
})
