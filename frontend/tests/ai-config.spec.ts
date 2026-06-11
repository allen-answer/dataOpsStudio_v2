import { test, expect, type Page } from '@playwright/test'
import { json, seedAdminAuth, mockLicense, trackConsoleErrors } from './helpers'

/**
 * §9 AI 配置各态:
 *   - L4 行永久锁定灰显(不可选)
 *   - key 已存储态(展示来源 + 清除按钮)
 *   - 测试连接三种结构化错误文案 + 成功 latency
 *   - 选未实现 provider 且启用 → 前端禁保存 + 提示
 */

function aiConfig(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    enabled: false,
    provider: 'off',
    model: null,
    base_url: null,
    max_auto_egress_level: 1,
    l4_requires_optin: true,
    enable_inference: false,
    enable_auto_translation: false,
    api_key_source: 'none',
    has_stored_api_key: false,
    updated_at: null,
    ...over,
  }
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
  await mockLicense(page)
})
function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

async function gotoAi(page: Page): Promise<void> {
  await page.goto('/admin/ai-config')
  await expect(page.getByRole('heading', { name: 'AI configuration' })).toBeVisible()
}

test('L4 row is permanently locked / greyed and not selectable', async ({ page }) => {
  await page.route('**/api/admin/ai-config', (r) => json(r, 200, aiConfig()))
  await gotoAi(page)
  const l4 = page.getByTestId('ai-egress-l4-locked')
  await expect(l4).toBeVisible()
  await expect(l4).toContainText('Locked')
  // L4 行内没有可选 radio(只有 L0-L3 是 input[type=radio])
  await expect(l4.locator('input[type="radio"]')).toHaveCount(0)
  await expect(page.locator('input[name="egress"]')).toHaveCount(4)
  expectNoConsoleErrors()
})

test('stored key state: source label + clear button', async ({ page }) => {
  await page.route('**/api/admin/ai-config', (r) =>
    json(r, 200, aiConfig({ enabled: true, provider: 'openai_compatible', base_url: 'https://x/v1', api_key_source: 'stored', has_stored_api_key: true })),
  )
  await gotoAi(page)
  await expect(page.getByTestId('ai-key-stored')).toBeVisible()
  await expect(page.getByTestId('ai-key-stored')).toContainText('Stored (source: stored)')
  await expect(page.getByTestId('ai-key-clear')).toBeVisible()
  // 已存储时仍提供替换输入框(password 类型,留空=不改)
  await expect(page.getByTestId('ai-key-input')).toBeVisible()
  await expect(page.getByTestId('ai-key-input')).toHaveAttribute('type', 'password')
  // 点清除 → 进入"将清除"态,替换输入框收起,stored 行隐藏
  await page.getByTestId('ai-key-clear').click()
  await expect(page.getByTestId('ai-key-clearing')).toBeVisible()
  await expect(page.getByTestId('ai-key-input')).toHaveCount(0)
  await expect(page.getByTestId('ai-key-stored')).toHaveCount(0)
  expectNoConsoleErrors()
})

test('test connection: three structured errors + success latency', async ({ page }) => {
  await page.route('**/api/admin/ai-config', (r) => json(r, 200, aiConfig({ enabled: true, provider: 'mock' })))

  const cases = [
    { error: 'ai_disabled', text: 'AI is not enabled' },
    { error: 'unsupported_provider', text: 'not implemented in 2.0.x and cannot be enabled' },
    { error: 'missing_provider_config', text: 'Missing required config' },
  ]
  for (const c of cases) {
    await page.route('**/api/admin/ai-config/test', (r) =>
      json(r, 200, { ok: false, provider: 'mock', model: null, latency_ms: 5, error: c.error }),
    )
    await gotoAi(page)
    await page.getByTestId('ai-test-btn').click()
    await expect(page.getByTestId('ai-test-result')).toContainText(c.text)
    await page.unroute('**/api/admin/ai-config/test')
  }

  // 成功:展示 latency
  await page.route('**/api/admin/ai-config/test', (r) =>
    json(r, 200, { ok: true, provider: 'mock', model: 'mock-1', latency_ms: 42, error: null }),
  )
  await gotoAi(page)
  await page.getByTestId('ai-test-btn').click()
  await expect(page.getByTestId('ai-test-result')).toContainText('42 ms')
  expectNoConsoleErrors()
})

test('selecting unimplemented provider while enabled blocks save', async ({ page }) => {
  await page.route('**/api/admin/ai-config', (r) => json(r, 200, aiConfig({ enabled: true, provider: 'mock' })))
  await gotoAi(page)
  await page.getByTestId('ai-provider').selectOption('anthropic')
  await expect(page.getByTestId('ai-provider-warning')).toBeVisible()
  await page.getByTestId('ai-save-btn').click()
  await expect(page.getByText('This provider is not implemented in 2.0.x and cannot be enabled.')).toBeVisible()
  expectNoConsoleErrors()
})
