import { test, expect } from '@playwright/test'

/** 等 SW 接管本頁（register 掛在 load 之後，接管還要一輪）。 */
async function waitForController(page) {
  await page.waitForFunction(() => navigator.serviceWorker?.controller != null, null, { timeout: 20_000 })
}

test.describe.configure({ mode: 'serial' })   // 共用同一組 SW 註冊，別互相搶

test('Service Worker：註冊並接管頁面', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.lib-card').first()).toBeVisible()
  await page.reload()                       // 第一次載入時 SW 還沒接管，重載一次才有 controller
  await waitForController(page)
  expect(await page.evaluate(() => navigator.serviceWorker.controller.scriptURL)).toContain('/sw.js')
})

test('Service Worker：讀過的章離線後仍打得開', async ({ page, context }) => {
  await page.goto('/')
  await expect(page.locator('.lib-card[data-slug]').first()).toBeVisible()
  const slug = await page.locator('.lib-card[data-slug]').first().getAttribute('data-slug')

  // 首次載入時 SW 還沒接管，那一輪的請求不會經過它 → 先重載讓它接管，
  // 之後讀到的東西才會進 runtime 快取（回訪者本來就是一開始就被接管的狀態）。
  await page.reload()
  await waitForController(page)
  await page.goto(`/#${slug}`)
  await page.locator('#toc .toc-item').first().click()
  await expect(page.locator('.article')).toBeVisible()
  await waitForController(page)
  // 讓 SW 有機會把這一輪的資源寫進 runtime 快取
  await page.waitForTimeout(1200)
  const hash = await page.evaluate(() => location.hash)
  const title = await page.locator('.article h1').textContent()

  await context.setOffline(true)
  await page.reload()
  await expect(page.locator('.article h1')).toHaveText(title, { timeout: 20_000 })
  expect(await page.evaluate(() => location.hash)).toBe(hash)
  await context.setOffline(false)
})

test('Service Worker：/dev 不進快取（即時儀表板看到舊資料等於看錯）', async ({ page }) => {
  await page.goto('/')
  await page.reload()
  await waitForController(page)
  const cached = await page.evaluate(async () => {
    const names = await caches.keys()
    const urls = []
    for (const n of names) {
      const c = await caches.open(n)
      for (const r of await c.keys()) urls.push(new URL(r.url).pathname)
    }
    return urls
  })
  expect(cached.filter(u => u.startsWith('/dev/'))).toEqual([])
  expect(cached).toContain('/index.html')
})

test('Service Worker：?sw=off 可以整個關掉並清空快取', async ({ page }) => {
  await page.goto('/')
  await page.reload()
  await waitForController(page)

  // 這個逃生口內部會自我重載一次（脫離舊 controller 才清得掉快取）→ 等它安定再驗，
  // 中途 evaluate 會撞上正在銷毀的執行環境。
  await page.goto('/?sw=off')
  await page.waitForFunction(() => navigator.serviceWorker.controller == null, null, { timeout: 20_000 })
  await expect.poll(
    () => page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length),
    { timeout: 20_000 },
  ).toBe(0)
  await expect.poll(
    () => page.evaluate(async () => (await caches.keys()).length),
    { timeout: 20_000 },
  ).toBe(0)
})
