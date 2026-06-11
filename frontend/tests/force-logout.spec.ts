import { test, expect } from '@playwright/test'
import { json, seedAdminAuth, mockLicense, trackConsoleErrors } from './helpers'

/**
 * admin 用户页 force-logout:确认弹窗 → 成功 toast。
 */
let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
  await mockLicense(page)
  await page.route('**/api/admin/users', (r) =>
    json(r, 200, [
      { id: 'u-1111-2222', username: 'alice', role: 'editor', mfa_enabled: true, created_at: '2026-01-01T00:00:00Z' },
    ]),
  )
})

test('force-logout: confirm dialog then success toast', async ({ page }) => {
  await page.route('**/api/admin/users/*/force-logout', (r) =>
    json(r, 200, { user_id: 'u-1111-2222', revoked_after: '2026-06-11T00:00:00Z' }),
  )

  await page.goto('/admin/users')
  await expect(page.getByRole('heading', { name: 'User management' })).toBeVisible()

  // 操作按钮 opacity-0 group-hover;force 点击(Playwright 可点穿透明元素)
  await page.getByTestId('force-logout-btn').click()

  // 确认弹窗(Modal title 是 div,不是语义 heading,按文案 + 确认按钮断言)
  await expect(page.getByTestId('force-logout-confirm')).toBeVisible()
  await expect(page.getByText('All of their current sessions are invalidated immediately', { exact: false })).toBeVisible()
  await page.getByTestId('force-logout-confirm').click()

  // 成功 toast
  await expect(page.getByTestId('toast')).toBeVisible()
  await expect(page.getByTestId('toast')).toContainText('Forced alice to log out')
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})
