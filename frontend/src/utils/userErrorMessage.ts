import { ApiError } from '../api/types'

export type ErrorTranslator = (key: string) => string
export type ApiErrorMessageOverride = (error: ApiError) => string | undefined

const LICENSE_WRITE_BLOCK_CODES = new Set(['license_repair_mode', 'license_in_grace'])

export function createUserErrorMessage(t: ErrorTranslator) {
  return (error: unknown, override?: ApiErrorMessageOverride): string => {
    if (!(error instanceof ApiError)) return t('common.error_unknown')
    if (error.status === 0) return t('common.error_network')
    if (LICENSE_WRITE_BLOCK_CODES.has(error.code ?? '')) return t('license.writes_blocked')

    const specificMessage = override?.(error)
    if (specificMessage) return specificMessage
    if (error.status === 403) return t('common.error_forbidden')

    return error.message.trim() ? error.message : t('common.error_unknown')
  }
}
