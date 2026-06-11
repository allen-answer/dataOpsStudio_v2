import { test, expect, type Page } from '@playwright/test'
import { json, seedAdminAuth, mockLicense, trackConsoleErrors } from './helpers'

/**
 * §6 账户安全三卡各态:
 *   - MFA 未开:enroll → QR 渲染(SVG)+ secret 文本 → 验证 → 恢复码一次性展示
 *   - MFA 已开:状态标 + disable 入口 + 恢复码计数(已用 N / total)
 */

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
  await mockLicense(page)
})

function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

async function gotoSecurity(page: Page): Promise<void> {
  await page.goto('/account/security')
  await expect(page.getByRole('heading', { name: 'Account security' })).toBeVisible()
}

test('MFA off: enroll renders QR + secret, verify shows recovery codes', async ({ page }) => {
  await page.route('**/api/account/security', (r) =>
    json(r, 200, { mfa_enabled: false, recovery_codes_total: 0, recovery_codes_used: 0 }),
  )
  await page.route('**/api/account/mfa/enroll', (r) =>
    json(r, 200, {
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/DataOps:admin?secret=JBSWY3DPEHPK3PXP&issuer=DataOps',
    }),
  )
  await page.route('**/api/account/mfa/verify', (r) =>
    json(r, 200, {
      enabled: true,
      recovery_codes: ['aaaa-1111', 'bbbb-2222', 'cccc-3333', 'dddd-4444', 'eeee-5555', 'ffff-6666', 'gggg-7777', 'hhhh-8888'],
    }),
  )

  await gotoSecurity(page)
  await expect(page.getByTestId('mfa-status-off')).toBeVisible()

  await page.getByTestId('mfa-enroll-btn').click()
  // QR SVG 渲染
  await expect(page.getByTestId('mfa-qr')).toBeVisible()
  await expect(page.locator('[data-testid="mfa-qr"] svg')).toBeVisible()
  // secret 文本备份
  await expect(page.getByTestId('enroll-secret')).toHaveText('JBSWY3DPEHPK3PXP')

  // 输入 6 位 → 验证 → 恢复码一次性展示
  await page.getByTestId('enroll-code-input').fill('123456')
  await page.getByRole('button', { name: 'Verify and enable' }).click()
  await expect(page.getByTestId('enroll-recovery-codes')).toBeVisible()
  await expect(page.getByText('aaaa-1111')).toBeVisible()
  await expect(page.getByText('hhhh-8888')).toBeVisible()
  expectNoConsoleErrors()
})

test('MFA off: invalid verify code shows error', async ({ page }) => {
  await page.route('**/api/account/security', (r) =>
    json(r, 200, { mfa_enabled: false, recovery_codes_total: 0, recovery_codes_used: 0 }),
  )
  await page.route('**/api/account/mfa/enroll', (r) =>
    json(r, 200, { secret: 'JBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/DataOps:admin?secret=X' }),
  )
  await page.route('**/api/account/mfa/verify', (r) =>
    json(r, 401, { error: 'invalid_mfa_code', message: 'Invalid MFA code' }),
  )

  await gotoSecurity(page)
  await page.getByTestId('mfa-enroll-btn').click()
  await page.getByTestId('enroll-code-input').fill('000000')
  await page.getByRole('button', { name: 'Verify and enable' }).click()
  await expect(page.getByText('Invalid code, please try again')).toBeVisible()
  // 未被踢回登录页(skipAuthRedirect 生效)
  await expect(page).toHaveURL(/\/account\/security/)
  expectNoConsoleErrors()
})

test('MFA on: status badge + recovery count + disable entry', async ({ page }) => {
  await page.route('**/api/account/security', (r) =>
    json(r, 200, { mfa_enabled: true, recovery_codes_total: 8, recovery_codes_used: 3 }),
  )
  await gotoSecurity(page)
  await expect(page.getByTestId('mfa-status-on')).toBeVisible()
  await expect(page.getByTestId('recovery-count')).toHaveText('5 / 8')
  await expect(page.getByTestId('mfa-disable-btn')).toBeVisible()
  await expect(page.getByTestId('recovery-regen-btn')).toBeVisible()

  // regenerate modal: 二次确认 → 提交需当前 TOTP
  await page.getByTestId('recovery-regen-btn').click()
  await expect(page.getByTestId('regen-code-input')).toBeVisible()
  expectNoConsoleErrors()
})

test('password change: mismatch shows error, success shows confirmation', async ({ page }) => {
  await page.route('**/api/account/security', (r) =>
    json(r, 200, { mfa_enabled: false, recovery_codes_total: 0, recovery_codes_used: 0 }),
  )
  await page.route('**/api/account/password', (r) => json(r, 200, { changed: true }))

  await gotoSecurity(page)
  const card = page.locator('section', { hasText: 'Change password' })
  await card.locator('input[autocomplete="current-password"]').fill('old')
  await card.locator('input[autocomplete="new-password"]').first().fill('new1')
  await card.locator('input[autocomplete="new-password"]').last().fill('new2')
  await card.getByRole('button', { name: 'Change password' }).click()
  await expect(page.getByText('The new passwords do not match')).toBeVisible()

  // 修正确认 → 成功
  await card.locator('input[autocomplete="new-password"]').last().fill('new1')
  await card.getByRole('button', { name: 'Change password' }).click()
  await expect(page.getByText('Password changed')).toBeVisible()
  expectNoConsoleErrors()
})
