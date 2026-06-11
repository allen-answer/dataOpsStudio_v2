/**
 * Job 错误码 → i18n key + 配色 tone 的精确映射。
 *
 * 后端有两层来源(JobsView 同时兼容):
 *  1. 结构化 `error_code`(JobErrorCode,7 值,新 job)—— 精确映射,优先用。
 *  2. `error_code` 为 null 的旧 job —— 回落 JobListItem.error 里的粗分类字符串
 *     (`cancelled` / `query_timeout` / `datasource_connection_failed` /
 *      `sql_execution_failed` / `job_failed`,PRD §5 字典)。
 *
 * 配色 tone 复用状态语义色(见 §10.3 / JobStatusBadge):
 *  - red    连接 / SQL / 权限 / internal 等"硬失败"
 *  - amber  timeout(可重试,缩小范围)
 *  - slate  cancelled(用户主动,中性)
 *
 * i18n key 落在 `jobs.error.<slug>`,zh-CN + en 两份都有。
 */
import type { JobErrorCode } from './types'

export type JobErrorTone = 'red' | 'amber' | 'slate'

export interface JobErrorDisplay {
  /** i18n key,如 'jobs.error.connection_failed' */
  i18nKey: string
  tone: JobErrorTone
}

/** 结构化 error_code(7 值)→ 显示。 */
const STRUCTURED: Record<JobErrorCode, JobErrorDisplay> = {
  connection_failed: { i18nKey: 'jobs.error.connection_failed', tone: 'red' },
  sql_failed: { i18nKey: 'jobs.error.sql_failed', tone: 'red' },
  timeout: { i18nKey: 'jobs.error.timeout', tone: 'amber' },
  cancelled: { i18nKey: 'jobs.error.cancelled', tone: 'slate' },
  permission_denied: { i18nKey: 'jobs.error.permission_denied', tone: 'red' },
  unsupported_db_type: { i18nKey: 'jobs.error.unsupported_db_type', tone: 'red' },
  internal: { i18nKey: 'jobs.error.internal', tone: 'red' },
}

/**
 * 旧 job 粗分类字符串(后端 _safe_job_error 在 error_code 为 null 时产出)→ 显示。
 * 这些值来自 PRD §5 错误码字典 + core.py。未命中则归 internal。
 */
const LEGACY: Record<string, JobErrorDisplay> = {
  cancelled: STRUCTURED.cancelled,
  query_timeout: STRUCTURED.timeout,
  datasource_connection_failed: STRUCTURED.connection_failed,
  sql_execution_failed: STRUCTURED.sql_failed,
  job_failed: STRUCTURED.internal,
}

/**
 * 解析一个 job 的错误显示。
 * @param errorCode 结构化码(优先);null 时回落 legacyError。
 * @param legacyError JobListItem.error 字符串(旧 job 粗分类)。
 * @returns 命中则 {i18nKey,tone};无错误信息则 null(调用方不渲染 badge)。
 */
export function resolveJobError(
  errorCode: JobErrorCode | null | undefined,
  legacyError: string | null | undefined,
): JobErrorDisplay | null {
  if (errorCode) return STRUCTURED[errorCode]
  if (legacyError) return LEGACY[legacyError] ?? STRUCTURED.internal
  return null
}

/** tone → Tailwind class(浅/深双色,与 JobStatusBadge 同源)。 */
export const JOB_ERROR_TONE_CLASS: Record<JobErrorTone, string> = {
  red: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300 border-red-200/60 dark:border-red-500/30',
  amber:
    'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 border-amber-200/60 dark:border-amber-500/30',
  slate:
    'bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300 border-slate-200/60 dark:border-slate-500/30',
}
