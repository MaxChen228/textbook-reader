import { test, expect } from '@playwright/test'
import { watchErrors, openLibrary, openFirstChapter } from './helpers.js'

test('search 模組：分詞與 build 端規格一致（去附加符號、CJK 取 2-gram）', async ({ page }) => {
  await page.goto('/')
  const out = await page.evaluate(async () => {
    const { fold, tokenize, queryTerms, snippet } = await import('/assets/js/search.js')
    return {
      folded: fold('Thévenin ÅNGSTRÖM'),
      latin: [...tokenize('Thévenin equivalent a')].sort(),
      cjk: [...tokenize('電路分析')].sort(),
      terms: queryTerms('  node-voltage  method '),
      snip: snippet('The Thévenin equivalent circuit is useful', ['thevenin']),
    }
  })
  expect(out.folded).toBe('thevenin angstrom')
  expect(out.latin).toEqual(['equivalent', 'thevenin'])   // 單字元 a 被丟掉
  expect(out.cjk).toEqual(['分析', '路分', '電路'])
  expect(out.terms).toEqual(['node', 'voltage', 'method'])
  expect(out.snip).toContain('<mark>Thévenin</mark>')     // 摘要標的是原字（含附加符號）
})

test('search 模組：索引沒有的詞直接判定全書都沒有', async ({ page }) => {
  await page.goto('/')
  const out = await page.evaluate(async () => {
    const { candidateChunks } = await import('/assets/js/search.js')
    const index = {
      chunks: [['ch', '1', 'A'], ['ch', '2', 'B'], ['ch', '3', 'C']],
      tokens: { capacitor: [0, 2], voltage: [1] },
      common: ['the'],
    }
    return {
      exact: candidateChunks(index, ['capacitor']),
      prefix: candidateChunks(index, ['capacit']),      // 前綴要撈得到
      and: candidateChunks(index, ['capacitor', 'voltage']).length,
      stop: candidateChunks(index, ['the']),            // 停用詞＝全部命中
      none: candidateChunks(index, ['zzzznotexist']),
    }
  })
  expect(out.exact).toEqual([0, 2])
  expect(out.prefix).toEqual([0, 2])
  expect(out.and).toBe(3)          // AND 是加權排序、不是硬交集（第二段才精確過濾）
  expect(out.stop).toEqual([0, 1, 2])
  expect(out.none).toEqual([])
})

test('書內搜尋：打字 → 命中 → 點擊跳到該處並標記', async ({ page }) => {
  const errors = watchErrors(page)
  const slug = await openLibrary(page)
  await openFirstChapter(page, slug)

  // 取本章一個真實存在的字當查詢詞 → 同時驗證 build 端索引與前端分詞沒有漂移
  const term = await page.evaluate(() => {
    const text = document.querySelector('.article').textContent
    const words = (text.match(/[A-Za-z]{6,12}/g) || [])
    return words.find(w => words.filter(x => x === w).length >= 3) || words[0] || ''
  })
  test.skip(!term, '本章沒有可用的英文詞')

  await page.keyboard.press('/')
  await expect(page.locator('#search-panel')).toBeVisible()
  await page.locator('#book-search').fill(term)

  await expect.poll(() => page.locator('.search-hit-row').count(), { timeout: 30_000 }).toBeGreaterThan(0)
  await expect(page.locator('#search-status')).toContainText(/命中|沒有/)

  await page.locator('.search-hit-row').first().click()
  await expect(page.locator('mark.search-hit.current')).toHaveCount(1)
  expect(errors).toEqual([])
})

test('書內搜尋：清空查詢會把正文標記還原', async ({ page }) => {
  const slug = await openLibrary(page)
  await openFirstChapter(page, slug)
  const term = await page.evaluate(() =>
    (document.querySelector('.article').textContent.match(/[A-Za-z]{6,12}/g) || [])[0] || '')
  test.skip(!term, '本章沒有可用的英文詞')

  await page.keyboard.press('/')
  await page.locator('#book-search').fill(term)
  await expect.poll(() => page.locator('.search-hit-row').count(), { timeout: 30_000 }).toBeGreaterThan(0)
  await page.locator('.search-hit-row').first().click()
  await expect(page.locator('mark.search-hit').first()).toBeAttached()

  await page.locator('#book-search').fill('')
  await expect(page.locator('mark.search-hit')).toHaveCount(0)
  await expect(page.locator('.search-hit-row')).toHaveCount(0)
})
