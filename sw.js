/**
 * Service Worker：只為了「離線也讀得到剛才讀過的東西」，不為了加速。
 *
 * **一律 network-first**。這是刻意的取捨：這個站的 data/ 是 daemon 每天重生的產物，
 * 而且已經被 Cloudflare 邊緣快取咬過一次（.js/.css 的 no-cache 被改寫成 4 小時，
 * 見 deploy/nginx.conf）。再疊一層會提前回應的快取，等於再造一個「明明改了卻看不到」
 * 的失效來源。所以：有網路時永遠走網路、順手更新快取；只有在網路真的失敗時才回落快取。
 * 代價是沒有速度紅利，換來的是零陳舊風險——這個交換在這個站上是划算的。
 *
 * 不碰 /dev/（即時儀表板，看到舊資料等於看錯）與任何非 GET 請求。
 */
const VERSION = 'tbr-v1'
const SHELL_CACHE = `${VERSION}-shell`
const RUNTIME_CACHE = `${VERSION}-runtime`

/** 首次安裝就抓下來的殼；缺任何一個都不該讓安裝失敗（某支檔案改名不能連帶弄壞離線）。 */
const SHELL = [
  '/',
  '/index.html',
  '/problems.html',
  '/assets/qbank-shared.js',
  '/assets/qbank-shared.css',
  '/assets/js/shared.js',
  '/assets/js/blocks.js',
  '/assets/js/store.js',
  '/assets/js/router.js',
  '/assets/js/search.js',
  '/assets/js/annotate.js',
  '/assets/js/reader.js',
  '/assets/js/problems.js',
  '/design/tokens.css',
  '/assets/fonts/fonts.css',
]

/** runtime 快取上限（筆數）。一章 JSON 可達數 MB，不設限會把使用者的磁碟配額吃光。 */
const RUNTIME_MAX = 240

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE)
    await Promise.all(SHELL.map(u => cache.add(u).catch(() => null)))
    await self.skipWaiting()
  })())
})

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys()
    await Promise.all(keys.filter(k => !k.startsWith(VERSION)).map(k => caches.delete(k)))
    await self.clients.claim()
  })())
})

function cacheable(url) {
  if (url.origin !== self.location.origin) return false
  if (url.pathname.startsWith('/dev/')) return false        // 即時儀表板：舊資料＝看錯
  if (url.pathname === '/sw.js') return false
  return true
}

async function trim(cacheName, max) {
  const cache = await caches.open(cacheName)
  const keys = await cache.keys()
  if (keys.length <= max) return
  // keys() 是插入序 → 先進先出，砍掉最舊的那批
  await Promise.all(keys.slice(0, keys.length - max).map(k => cache.delete(k)))
}

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (!cacheable(url)) return

  event.respondWith((async () => {
    try {
      const res = await fetch(req)
      // 只快取成功的完整回應（opaque/206 存了也用不了）
      if (res && res.status === 200 && res.type === 'basic') {
        const cache = await caches.open(RUNTIME_CACHE)
        cache.put(req, res.clone()).then(() => trim(RUNTIME_CACHE, RUNTIME_MAX)).catch(() => {})
      }
      return res
    } catch (err) {
      const hit = await caches.match(req, { ignoreSearch: true })
      if (hit) return hit
      if (req.mode === 'navigate') {
        const shell = await caches.match('/index.html') || await caches.match('/')
        if (shell) return shell
      }
      throw err
    }
  })())
})
