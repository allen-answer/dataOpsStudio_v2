import { expect, test, type Page } from '@playwright/test'
import en from '../src/i18n/en'
import zhCN from '../src/i18n/zh-CN'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

function leafKeys(messages: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(messages).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      return leafKeys(value as Record<string, unknown>, path)
    }
    return [path]
  })
}

function trackMissingMessageWarnings(page: Page): string[] {
  const warnings: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'warning' && message.text().includes("Not found '")) {
      warnings.push(message.text())
    }
  })
  return warnings
}

async function mockSystemSettings(page: Page): Promise<void> {
  await page.route('**/api/admin/system-settings', (route) =>
    json(route, 200, {
      access_token_ttl_seconds: 3600,
      license_enforcement_enabled: true,
    }),
  )
}

async function expectLocalizedAdminSettings(
  page: Page,
  expected: {
    locale: 'zh-CN' | 'en'
    title: string
    loginSession: string
    refresh: string
  },
): Promise<void> {
  const consoleErrors = trackConsoleErrors(page)
  const missingMessageWarnings = trackMissingMessageWarnings(page)

  await seedAdminAuth(page, expected.locale)
  await mockLicense(page)
  await mockSystemSettings(page)

  await page.goto('/admin/settings')
  await expect(page.getByRole('heading', { level: 1, name: expected.title })).toBeVisible()
  await expect(page.getByRole('heading', { name: expected.loginSession })).toBeVisible()
  await expect(page.getByRole('button', { name: expected.refresh })).toBeVisible()
  await expect.soft(page.locator('body')).not.toContainText(/(?:admin\.settings|common\.refresh)/)
  expect(missingMessageWarnings, missingMessageWarnings.join('\n')).toEqual([])
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

test('English and Chinese locale dictionaries expose identical leaf-key sets', () => {
  const enKeys = new Set(leafKeys(en))
  const zhKeys = new Set(leafKeys(zhCN))

  expect({
    missingInZhCN: [...enKeys].filter((key) => !zhKeys.has(key)).sort(),
    missingInEn: [...zhKeys].filter((key) => !enKeys.has(key)).sort(),
  }).toEqual({ missingInZhCN: [], missingInEn: [] })
})

test('default Chinese admin settings renders localized text without missing-message warnings', async ({
  page,
}) => {
  await expectLocalizedAdminSettings(page, {
    locale: 'zh-CN',
    title: '系统设置',
    loginSession: '登录会话',
    refresh: '刷新',
  })
})

test('English admin settings remains localized without console errors', async ({ page }) => {
  await expectLocalizedAdminSettings(page, {
    locale: 'en',
    title: 'System settings',
    loginSession: 'Login session',
    refresh: 'Refresh',
  })
})
