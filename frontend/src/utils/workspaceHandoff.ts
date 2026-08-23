const STORAGE_PREFIX = 'dataops:workspace-handoff:'
const VERSION = 1
const TTL_MS = 10 * 60 * 1000
const MAX_SQL_LENGTH = 1_000_000
const MAX_CONSOLE_NAME_LENGTH = 128

interface HandoffBase {
  version: typeof VERSION
  projectId: string
  datasourceId: string
  sql: string
  createdAt: number
}

export interface SqlToCompareHandoff extends HandoffBase {
  kind: 'sql_to_compare'
  side: 'source'
}

export interface CompareToSqlHandoff extends HandoffBase {
  kind: 'compare_to_sql'
  consoleName: string
}

export type WorkspaceHandoff = SqlToCompareHandoff | CompareToSqlHandoff
export type WorkspaceHandoffDraft =
  | Omit<SqlToCompareHandoff, 'version' | 'createdAt'>
  | Omit<CompareToSqlHandoff, 'version' | 'createdAt'>

export function createWorkspaceHandoff(draft: WorkspaceHandoffDraft): string {
  if (!validCommon(draft)) throw new Error('invalid_workspace_handoff')
  if (draft.kind === 'sql_to_compare' && draft.side !== 'source') {
    throw new Error('invalid_workspace_handoff')
  }
  if (
    draft.kind === 'compare_to_sql' &&
    (!draft.consoleName.trim() || draft.consoleName.length > MAX_CONSOLE_NAME_LENGTH)
  ) {
    throw new Error('invalid_workspace_handoff')
  }
  pruneExpiredHandoffs()
  const token = globalThis.crypto.randomUUID()
  const payload: WorkspaceHandoff = {
    ...draft,
    version: VERSION,
    createdAt: Date.now(),
  } as WorkspaceHandoff
  globalThis.sessionStorage.setItem(`${STORAGE_PREFIX}${token}`, JSON.stringify(payload))
  return token
}

export function consumeWorkspaceHandoff(
  token: string,
  expectedProjectId: string,
): WorkspaceHandoff | null {
  if (!token || !expectedProjectId) return null
  const key = `${STORAGE_PREFIX}${token}`
  const raw = globalThis.sessionStorage.getItem(key)
  // One-shot even when the payload is malformed or belongs to another project.
  globalThis.sessionStorage.removeItem(key)
  if (!raw) return null
  try {
    const payload: unknown = JSON.parse(raw)
    if (!validPayload(payload, expectedProjectId)) return null
    return payload
  } catch {
    return null
  }
}

function validCommon(
  value: object,
): value is Record<string, unknown> & {
  projectId: string
  datasourceId: string
  sql: string
} {
  if (!isRecord(value)) return false
  return (
    typeof value.projectId === 'string' &&
    value.projectId.length > 0 &&
    typeof value.datasourceId === 'string' &&
    value.datasourceId.length > 0 &&
    typeof value.sql === 'string' &&
    value.sql.trim().length > 0 &&
    value.sql.length <= MAX_SQL_LENGTH
  )
}

function validPayload(value: unknown, expectedProjectId: string): value is WorkspaceHandoff {
  if (!isRecord(value) || value.version !== VERSION || !validCommon(value)) return false
  if (value.projectId !== expectedProjectId) return false
  if (typeof value.createdAt !== 'number' || Date.now() - value.createdAt > TTL_MS) return false
  if (value.createdAt > Date.now() + 60_000) return false
  if (value.kind === 'sql_to_compare') return value.side === 'source'
  return (
    value.kind === 'compare_to_sql' &&
    typeof value.consoleName === 'string' &&
    value.consoleName.trim().length > 0 &&
    value.consoleName.length <= MAX_CONSOLE_NAME_LENGTH
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function pruneExpiredHandoffs(): void {
  const now = Date.now()
  for (let index = globalThis.sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = globalThis.sessionStorage.key(index)
    if (!key?.startsWith(STORAGE_PREFIX)) continue
    const raw = globalThis.sessionStorage.getItem(key)
    try {
      const payload: unknown = raw ? JSON.parse(raw) : null
      if (
        !isRecord(payload) ||
        typeof payload.createdAt !== 'number' ||
        now - payload.createdAt > TTL_MS ||
        payload.createdAt > now + 60_000
      ) {
        globalThis.sessionStorage.removeItem(key)
      }
    } catch {
      globalThis.sessionStorage.removeItem(key)
    }
  }
}
