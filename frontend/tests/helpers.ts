import type { ConsoleMessage, Page, Route } from '@playwright/test'

/**
 * 收集「真正的 JS / render 红错」,过滤掉浏览器对 mock 4xx/5xx 应答的
 * 「Failed to load resource: the server responded with a status of N」噪音
 * —— 那是网络层的预期错误响应(我们就是在测 4xx/5xx 渲染),不是前端崩。
 */
export function trackConsoleErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (m: ConsoleMessage) => {
    if (m.type() !== 'error') return
    const text = m.text()
    if (text.includes('Failed to load resource:')) return
    errors.push(text)
  })
  return errors
}

/**
 * 测试用假 JWT(前端只解 sub/role,不验签)。decodeJwt 只读 payload。
 */
export const ADMIN_JWT =
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbi11c2VyLTAwMDEiLCJyb2xlIjoiYWRtaW4ifQ.sig'

/** 在页面加载前注入 token + 锁定语言为 en(断言文案稳定),并清状态。 */
export async function seedAdminAuth(page: Page): Promise<void> {
  await page.route('**/api/version', (route) =>
    json(route, 200, {
      version: '2.0.1-test',
      commit: 'abcdef0123456789',
      image_version: 'test',
    }),
  )
  await page.addInitScript(
    ([token]) => {
      localStorage.setItem('dataops-token', token)
      localStorage.setItem('dataops-locale', 'en')
    },
    [ADMIN_JWT],
  )
}

export async function seedLocale(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('dataops-locale', 'en')
  })
}

/** 统一 JSON 应答。 */
export function json(route: Route, status: number, body: unknown): Promise<void> {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

/** license 状态(LicenseBanner 会拉,给个 valid 免横条干扰)。 */
export async function mockLicense(page: Page): Promise<void> {
  await page.route('**/api/license/status', (r) =>
    json(r, 200, {
      mode: 'valid',
      edition: 'enterprise',
      customer: 'test',
      expires_at: null,
      limits: {},
      features: [],
      repair_reason: null,
      trial_days_remaining: null,
    }),
  )
}
