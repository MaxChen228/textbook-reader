import { test, expect } from '@playwright/test'
import { openLibrary } from './helpers.js'

/** 直接寫一筆進度到 localStorage，再開站看 UI 有沒有據此顯示。 */
async function seedProgress(page, slug, entries) {
  await page.goto('/')
  await page.evaluate(({ slug, entries }) => {
    const chunks = {}
    for (const e of entries) {
      chunks[`${slug}/ch/${e.key}`] = {
        slug, kind: 'ch', key: String(e.key), anchor: null,
        scrollTop: 100, scrollRatio: e.ratio, maxRatio: e.ratio,
        updatedAt: Date.now() - (e.ago || 0),
      }
    }
    localStorage.setItem('textbook.readerProgress.v1', JSON.stringify({ chunks }))
  }, { slug, entries })
}

test('store：進度只前進不後退（maxRatio 與 scrollRatio 各司其職）', async ({ page }) => {
  await page.goto('/')
  const out = await page.evaluate(async () => {
    localStorage.removeItem('textbook.readerProgress.v1')
    const { recordProgress, chunkProgress, furthest } = await import('/assets/js/store.js')
    const base = { slug: 'demo', kind: 'ch', key: '1', anchor: null, scrollTop: 0, updatedAt: 1 }
    recordProgress({ ...base, scrollRatio: 0.4 })
    recordProgress({ ...base, scrollRatio: 0.04 })   // 排版後高度長高 → 同一位置比例變小
    const rec = chunkProgress('demo', 'ch', '1')
    return { resume: rec.scrollRatio, progress: furthest(rec) }
  })
  expect(out.resume).toBe(0.04)     // 續讀回到「實際離開的位置」
  expect(out.progress).toBe(0.4)    // 進度顯示用「曾讀到的最遠處」
})

test('繼續閱讀：讀過的書出現在書庫最上方並帶進度', async ({ page }) => {
  const slug = await openLibrary(page)
  await seedProgress(page, slug, [{ key: '1', ratio: 0.95 }, { key: '2', ratio: 0.5 }])
  await page.goto('/')

  const resume = page.locator('.lib-resume').first()
  await expect(resume).toBeVisible()
  await expect(resume).toHaveAttribute('href', new RegExp(`^#${slug}/ch/`))
  await expect(page.locator('.lib-resume-where').first()).toContainText('%')

  // 對應書卡也要有進度（讀完 1 章）
  const card = page.locator(`.lib-card[data-slug="${slug}"] .lib-card-progress`)
  await expect(card).toContainText('已讀 1/')
})

test('繼續閱讀：只看一眼的書不算在讀，不污染書牆', async ({ page }) => {
  const slug = await openLibrary(page)
  await seedProgress(page, slug, [{ key: '1', ratio: 0.001 }])
  await page.goto('/')

  await expect(page.locator('#lib-continue')).toBeHidden()
  await expect(page.locator(`.lib-card[data-slug="${slug}"] .lib-card-progress`)).toHaveCount(0)
})

test('目錄：讀過的章帶已讀徽章', async ({ page }) => {
  const slug = await openLibrary(page)
  await seedProgress(page, slug, [{ key: '1', ratio: 0.95 }])
  await page.goto(`/#${slug}`)

  const firstRow = page.locator('#toc .toc-row').first()
  await expect(firstRow).toHaveClass(/read/)
  await expect(firstRow.locator('.toc-read')).toHaveText('✓')
})

test('書總覽：有進度時給續讀入口', async ({ page }) => {
  const slug = await openLibrary(page)
  await seedProgress(page, slug, [{ key: '2', ratio: 0.5 }])
  await page.goto(`/#${slug}`)
  await page.evaluate(() => { location.hash = location.hash.split('/')[0] })

  const cta = page.locator('.overview-cta')
  await expect(cta).toBeVisible()
  await expect(cta).toContainText('繼續讀')
  await expect(page.locator('.overview-progress')).toContainText('已讀')
})
