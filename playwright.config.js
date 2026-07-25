import { defineConfig, devices } from '@playwright/test'

/**
 * 前端 smoke 測試。跑法：`npm run test:e2e`（先 `npm install && npx playwright install chromium`）。
 *
 * webServer 預設 reuseExistingServer → 常駐主機上 nginx 已在 8001 就直接沿用；
 * 沒有的機器才自己起一個 http.server（兩者都是「直送工作目錄」，內容一致）。
 * data/ 與 img/ 是 build 產物、不入 git，所以測試預設跑在有烤過站的機器上；
 * 資料不存在時測試會清楚地失敗在「書庫沒有書」而不是靜默通過。
 */
export default defineConfig({
  testDir: './tests',
  timeout: 45_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.READER_BASE_URL || 'http://127.0.0.1:8001',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'uv run python -m http.server 8001',
    url: 'http://127.0.0.1:8001/',
    reuseExistingServer: true,
    timeout: 20_000,
  },
})
