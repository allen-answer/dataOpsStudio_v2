import { test, expect } from '@playwright/test'
import { json, seedLocale, trackConsoleErrors, ADMIN_JWT } from './helpers'

/**
 * 登录第二因子(两态):
 *   1) 密码通过但 401 mfa_required → 切到第二步(6 位 TOTP 输入 + 改用恢复码切换)
 *   2) invalid_mfa_code → 明确报错;正确码 → 拿 token 进 app
 * 非 MFA 用户:第一步直接成功(此处由 mfa-not-required 用例覆盖)。
 */

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedLocale(page)
})

test('MFA user: 401 mfa_required switches to second step, invalid then valid', async ({ page }) => {
  let attempt = 0
  await page.route('**/api/auth/login', async (route) => {
    attempt += 1
    const body = JSON.parse(route.request().postData() ?? '{}')
    if (!body.mfa_code) {
      return json(route, 401, { error: 'mfa_required', message: 'MFA verification required' })
    }
    if (body.mfa_code !== '123456') {
      return json(route, 401, { error: 'invalid_mfa_code', message: 'Invalid MFA code' })
    }
    return json(route, 200, { access_token: ADMIN_JWT, token_type: 'bearer' })
  })
  // 登录后会跳 /projects,mock 之以免崩。
  await page.route('**/api/projects', (r) => json(r, 200, []))
  await page.route('**/api/license/status', (r) =>
    json(r, 200, { mode: 'valid', edition: null, customer: null, expires_at: null, limits: {}, features: [], repair_reason: null, trial_days_remaining: null }),
  )

  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Sign in to DataOps Studio' })).toBeVisible()

  // step 1: 填密码提交 → mfa_required → 进第二步
  await page.locator('input[autocomplete="username"]').fill('alice')
  await page.locator('input[autocomplete="current-password"]').fill('pw')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page.getByRole('heading', { name: 'Two-factor authentication' })).toBeVisible()
  await expect(page.getByTestId('mfa-code-input')).toBeVisible()
  await expect(page.getByTestId('mfa-toggle-recovery')).toHaveText('Use a recovery code')

  // 切恢复码 → label 变;再切回
  await page.getByTestId('mfa-toggle-recovery').click()
  await expect(page.getByText('Recovery code', { exact: true })).toBeVisible()
  await page.getByTestId('mfa-toggle-recovery').click()

  // step 2: 错误码 → 明确报错
  await page.getByTestId('mfa-code-input').fill('000000')
  await page.getByRole('button', { name: 'Verify and sign in' }).click()
  await expect(page.getByRole('alert')).toHaveText('Invalid code, please try again')

  // 正确码 → 进 app
  await page.getByTestId('mfa-code-input').fill('123456')
  await page.getByRole('button', { name: 'Verify and sign in' }).click()
  await expect(page).toHaveURL(/\/projects/)

  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('non-MFA user: single step login succeeds', async ({ page }) => {
  await page.route('**/api/auth/login', (r) =>
    json(r, 200, { access_token: ADMIN_JWT, token_type: 'bearer' }),
  )
  await page.route('**/api/projects', (r) => json(r, 200, []))
  await page.route('**/api/license/status', (r) =>
    json(r, 200, { mode: 'valid', edition: null, customer: null, expires_at: null, limits: {}, features: [], repair_reason: null, trial_days_remaining: null }),
  )

  await page.goto('/login')
  await page.locator('input[autocomplete="username"]').fill('bob')
  await page.locator('input[autocomplete="current-password"]').fill('pw')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/\/projects/)
  // 从未出现第二步
  await expect(page.getByTestId('mfa-code-input')).toHaveCount(0)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})
