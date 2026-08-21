import { expect, test } from '@playwright/test'

import { ApiError } from '../src/api/types'
import en from '../src/i18n/en'
import zhCN from '../src/i18n/zh-CN'
import { createUserErrorMessage, type ErrorTranslator } from '../src/utils/userErrorMessage'

function translator(messages: Record<string, unknown>): ErrorTranslator {
  return (key) => {
    const value = key.split('.').reduce<unknown>((current, part) => {
      if (typeof current !== 'object' || current === null) return undefined
      return (current as Record<string, unknown>)[part]
    }, messages)
    return typeof value === 'string' ? value : key
  }
}

for (const [locale, messages, expected] of [
  [
    'en',
    en,
    {
      network: 'Network error, please retry',
      license: 'Write actions are disabled in the current license state (view / license update only)',
      forbidden: 'You do not have permission to perform this action',
      unknown: 'Unknown error',
    },
  ],
  [
    'zh-CN',
    zhCN,
    {
      network: '网络异常,请稍后重试',
      license: '当前 license 状态下写操作已禁用(仅允许查看 / 更新 license)',
      forbidden: '你没有执行此操作的权限',
      unknown: '未知错误',
    },
  ],
] as const) {
  test(`${locale}: shared user error messages cover network, license, forbidden, and unknown errors`, () => {
    const message = createUserErrorMessage(translator(messages))

    expect(message(new ApiError(0, 'browser-specific network text', 'network_error'))).toBe(
      expected.network,
    )
    expect(message(new ApiError(403, 'backend repair text', 'license_repair_mode'))).toBe(
      expected.license,
    )
    expect(message(new ApiError(403, 'backend grace text', 'license_in_grace'))).toBe(
      expected.license,
    )
    expect(message(new ApiError(403, 'backend permission text', 'forbidden'))).toBe(
      expected.forbidden,
    )
    expect(message(new Error('internal implementation detail'))).toBe(expected.unknown)
    expect(message(new ApiError(500, '', 'internal_error'))).toBe(expected.unknown)
  })
}

test('page-specific code messages override the generic 403 fallback without hiding safe API messages', () => {
  const message = createUserErrorMessage(translator(en))

  expect(
    message(
      new ApiError(403, 'Datasource operation policy denies SELECT', 'select_not_allowed'),
      (error) => error.code === 'select_not_allowed' ? 'SELECT is disabled for this source' : undefined,
    ),
  ).toBe('SELECT is disabled for this source')
  expect(
    message(new ApiError(409, 'The saved compare aliases are stale', 'compare_sql_aliases_stale')),
  ).toBe('The saved compare aliases are stale')
})
