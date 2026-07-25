import { expect } from '@playwright/test'

/**
 * 收集本頁的 console error 與未捕捉例外。
 * 用法：`const errors = watchErrors(page)` … 測試結尾 `expect(errors).toEqual([])`。
 * 圖片 404 之類的網路錯誤不算（資料是 build 產物，缺圖不該讓 smoke 紅）。
 */
export function watchErrors(page) {
  const errors = []
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', (e) => errors.push(String(e)))
  return errors
}

/** 進到書庫並等第一批書卡出現；回傳第一本「已收錄」書的 slug。 */
export async function openLibrary(page) {
  await page.goto('/')
  const card = page.locator('.lib-card[data-slug]').first()
  await expect(card).toBeVisible()
  return card.getAttribute('data-slug')
}

/** 進到某本書的第一章（走 UI：書卡 → 目錄第一項），回傳章節 hash。 */
export async function openFirstChapter(page, slug) {
  await page.goto(`/#${slug}`)
  const first = page.locator('#toc .toc-item').first()
  await expect(first).toBeVisible()
  await first.click()
  await expect(page.locator('.article')).toBeVisible()
  return page.evaluate(() => location.hash)
}
