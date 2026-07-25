import { test, expect } from '@playwright/test'
import { watchErrors } from './helpers.js'

/** 預設視圖是 grid（存在 localStorage）；列表相關斷言先切到 list。 */
async function openList(page) {
  await page.goto('/problems.html')
  await expect(page.locator('.grid-block, .problem-card').first()).toBeVisible()
  await page.locator('#btn-view-list').click()
  await expect(page.locator('.problem-card').first()).toBeVisible()
}

test('題庫：開場只載分片索引，不再拉整包 problems.json', async ({ page }) => {
  const errors = watchErrors(page)
  const urls = []
  page.on('request', r => urls.push(r.url()))

  await page.goto('/problems.html')
  await expect(page.locator('.grid-block').first()).toBeVisible()

  // 退役的整包索引（曾經 gzip 12.7MB）不該再被請求
  expect(urls.filter(u => /\/data\/problems\.json/.test(u))).toEqual([])
  expect(urls.some(u => u.includes('/data/problems/index.json'))).toBe(true)
  expect(errors).toEqual([])
})

test('題庫：虛擬捲動只掛載視窗內的卡片', async ({ page }) => {
  await openList(page)

  const mounted = await page.locator('.problem-card').count()
  const total = await page.evaluate(() => {
    const h = parseFloat(document.getElementById('problem-list').style.height || '0')
    return Math.round(h / 150)   // ROW_H
  })
  expect(total).toBeGreaterThan(1000)      // 全站數萬題
  expect(mounted).toBeLessThan(60)         // 但同時只掛載視窗附近的少數幾張
})

test('題庫：單題詳情渲染題幹', async ({ page }) => {
  const errors = watchErrors(page)
  await openList(page)
  await page.locator('.problem-card').first().click()

  const body = page.locator('.detail-wrap .problem-body')
  await expect(body).toBeVisible()
  expect((await body.textContent()).trim().length).toBeGreaterThan(0)
  expect(errors).toEqual([])
})

test('題庫：搜尋收斂到單書時提示尚未載入的內文範圍', async ({ page }) => {
  await openList(page)

  await page.locator('#search').fill('theorem')
  const note = page.locator('#search-corpus-note')
  // 全站範圍：內文沒載完就必須誠實說出來（沒有後端，全文搜尋不是免費的）
  await expect(note).toBeVisible()
  expect(await note.textContent()).toMatch(/尚未載入|已載入/)
})
