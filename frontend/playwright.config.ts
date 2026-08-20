import { defineConfig, devices } from '@playwright/test'

/**
 * 无后端渲染验证(Wave 4B)——
 * 全部用例 route-mock `/api/**`,不连任何真后端;只验证「前端在各 API 形态下正确渲染」。
 * 真后端走查走 PR draft 人工 checklist(daily-server 未升级)。
 *
 * webServer 起 `vite preview`(已 build 的 dist),贴近生产产物。
 */
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:4173',
    trace: process.env.CI ? 'retain-on-failure' : 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  // 默认起 vite preview;沙箱里 4173 可能被禁,可用 E2E_BASE_URL 指向已起的 preview
  // 并设 E2E_NO_WEBSERVER=1 跳过自启。
  webServer: process.env.E2E_NO_WEBSERVER
    ? undefined
    : {
        command: 'npm run preview -- --port 4173 --strictPort',
        url: 'http://localhost:4173',
        reuseExistingServer: !process.env.CI,
        timeout: 120000,
      },
})
