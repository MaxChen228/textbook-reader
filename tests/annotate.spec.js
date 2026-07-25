import { test, expect } from '@playwright/test'
import { watchErrors, openLibrary, openFirstChapter } from './helpers.js'

/** 用程式選取正文裡第一段夠長的文字（Playwright 沒有「拖曳選字」的原生 API）。 */
async function selectSomeText(page, from = 5, to = 45) {
  return page.evaluate(({ from, to }) => {
    const p = [...document.querySelectorAll('.article p')].find(el => el.textContent.trim().length > 80)
    if (!p) return null
    const tn = [...p.childNodes].find(n => n.nodeType === 3 && n.nodeValue.trim().length > to)
    if (!tn) return null
    const r = document.createRange()
    r.setStart(tn, from); r.setEnd(tn, to)
    const sel = window.getSelection()
    sel.removeAllRanges(); sel.addRange(r)
    document.getElementById('content').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    return String(sel)
  }, { from, to })
}

test('標註：選取正文 → 畫線 → 重新載入後仍在（認原文不認座標）', async ({ page }) => {
  const errors = watchErrors(page)
  const slug = await openLibrary(page)
  const hash = await openFirstChapter(page, slug)

  const quote = await selectSomeText(page)
  test.skip(!quote, '本章沒有夠長的段落')

  await expect(page.locator('#sel-toolbar')).toHaveClass(/open/)
  await page.locator('#sel-annotate').click()
  await expect(page.locator('mark.annot')).toHaveCount(1)

  // 重新載入：標註靠 quote 在該章文字裡回找，不依賴任何 DOM 座標。
  // 一定要用 reload——goto 到同一個 URL 是 same-document 導航，不會重跑渲染，測不到東西。
  await page.reload()
  await expect(page.locator('.article')).toBeVisible()
  await expect(page.locator('mark.annot')).toHaveCount(1)
  expect(await page.locator('mark.annot').textContent()).toBe(quote)
  expect(errors).toEqual([])
})

test('標註：資料變動導致找不到原文時列為 orphan，不靜默丟掉', async ({ page }) => {
  const slug = await openLibrary(page)
  const hash = await openFirstChapter(page, slug)
  const kind = hash.split('/')[1]
  const key = decodeURIComponent(hash.split('/')[2])

  await page.evaluate(({ slug, kind, key }) => {
    localStorage.setItem('textbook.annotations.v1', JSON.stringify({
      items: [{
        id: 'an_ghost', createdAt: Date.now(), slug, kind, key, anchor: null,
        quote: '這段文字在原書裡絕對不存在因為它是測試用的中文句子', before: '', after: '', note: '我的筆記',
      }],
    }))
  }, { slug, kind, key })

  await page.reload()
  await expect(page.locator('.article')).toBeVisible()
  await page.keyboard.press('n')

  const row = page.locator('#notes-list [data-annot="an_ghost"]')
  await expect(row).toBeVisible()
  await expect(row).toContainText('我的筆記')
  await expect(row.locator('.annot-orphan')).toBeVisible()
  await expect(page.locator('mark.annot')).toHaveCount(0)
})

test('書籤：b 切換、以章為單位、列在標註面板', async ({ page }) => {
  const slug = await openLibrary(page)
  await openFirstChapter(page, slug)

  await page.keyboard.press('b')
  await expect(page.locator('#btn-bookmark')).toHaveAttribute('aria-pressed', 'true')

  // 捲動改變目前小節，星號狀態不該跟著閃
  await page.evaluate(() => { document.getElementById('content').scrollTop = 1500 })
  await page.waitForTimeout(400)
  await expect(page.locator('#btn-bookmark')).toHaveAttribute('aria-pressed', 'true')

  await page.keyboard.press('n')
  await expect(page.locator('#notes-list .annot-bookmark')).toHaveCount(1)

  await page.keyboard.press('b')
  await expect(page.locator('#btn-bookmark')).toHaveAttribute('aria-pressed', 'false')
  await expect(page.locator('#notes-list .annot-bookmark')).toHaveCount(0)
})

test('標註：不會落在公式裡（選到公式時不給畫線）', async ({ page }) => {
  await page.goto('/')
  const out = await page.evaluate(async () => {
    const { applyAnnotations } = await import('/assets/js/annotate.js')
    const root = document.createElement('div')
    root.innerHTML = '<p>before text</p><div class="eq">E = mc^2 special</div><p>after text</p>'
    document.body.appendChild(root)
    const r = applyAnnotations(root, [{ id: 'a1', quote: 'special' }, { id: 'a2', quote: 'after text' }])
    const html = root.innerHTML
    root.remove()
    return { ...r, html }
  })
  expect(out.orphans).toEqual(['a1'])     // 公式裡的字視同找不到 → 不會插進 LaTeX
  expect(out.applied).toEqual(['a2'])
  expect(out.html).toContain('<mark class="annot" data-annot="a2">after text</mark>')
})

test('標註：換書不會看到別本書的標註', async ({ page }) => {
  await page.goto('/')
  const slugs = await page.locator('.lib-card[data-slug]').evaluateAll(els => els.slice(0, 2).map(e => e.dataset.slug))
  test.skip(slugs.length < 2, '站上不足兩本書')
  const [a, b] = slugs

  await page.evaluate(({ a }) => {
    localStorage.setItem('textbook.annotations.v1', JSON.stringify({
      items: [{ id: 'an_a', createdAt: 1, slug: a, kind: 'ch', key: '1', quote: 'x', note: 'A 書的筆記' }],
    }))
  }, { a })

  await page.goto(`/#${b}`)
  await page.keyboard.press('n')
  await expect(page.locator('#notes-list')).toContainText('還沒有任何標註')
  await expect(page.locator('#notes-list [data-annot="an_a"]')).toHaveCount(0)
})
