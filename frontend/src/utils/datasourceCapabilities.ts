import type { DatasourceCapabilities } from '../api/types'

/**
 * 数据源能力判断:优先读后端下发的 `capabilities`(app/api/datasource_capabilities.py),
 * 不再在各页面硬编码 db_type 白名单。
 *
 * 老后端(响应里没有 capabilities)时回落到收敛前的白名单;灰度期保留一版后删除。
 * 回落值必须与后端规则对同一类型给出相同结果,改动任一侧都要同步。
 */
const FALLBACK_EXECUTION = new Set(['mysql', 'dm', 'postgresql', 'db2'])
const FALLBACK_SQL_TOOLS = new Set(['mysql', 'dm', 'postgresql'])
const FALLBACK_METADATA = new Set(['mysql', 'dm', 'postgresql'])
const FALLBACK_SESSION_CONSOLE = new Set(['mysql', 'dm'])

export interface CapabilitySource {
  db_type: string
  capabilities?: DatasourceCapabilities
}

type Source = CapabilitySource | null | undefined

export function canExecuteSql(ds: Source): boolean {
  if (!ds) return false
  return ds.capabilities?.execute ?? FALLBACK_EXECUTION.has(ds.db_type)
}

/** EXPLAIN / 展开 * / 格式化等依赖 sqlglot 方言与元数据缓存的工具。 */
export function supportsSqlTools(ds: Source): boolean {
  if (!ds) return false
  return ds.capabilities?.sql_tools ?? FALLBACK_SQL_TOOLS.has(ds.db_type)
}

/** EXPLAIN 按钮:工具可用且数据源策略允许(后端已合并 allow_explain)。 */
export function supportsExplain(
  ds: (CapabilitySource & { operation_policy?: { allow_explain: boolean } }) | null | undefined,
): boolean {
  if (!ds) return false
  if (ds.capabilities) return ds.capabilities.explain
  return supportsSqlTools(ds) && Boolean(ds.operation_policy?.allow_explain)
}

/** 编辑器里按元数据缓存补全表名 / 列名。 */
export function supportsMetadataCompletion(ds: Source): boolean {
  if (!ds) return false
  return ds.capabilities?.metadata ?? FALLBACK_METADATA.has(ds.db_type)
}

/** 会话式 SQL 控制台(owner 连接常驻、可硬取消);其余类型回退 job 路径。 */
export function supportsSessionConsole(ds: Source): boolean {
  if (!ds) return false
  return ds.capabilities?.session_console ?? FALLBACK_SESSION_CONSOLE.has(ds.db_type)
}

/** 只有 db_type 字符串、拿不到数据源对象时的回落判断(会话路由早期入口用)。 */
export function sessionConsoleFallback(dbType: string): boolean {
  return FALLBACK_SESSION_CONSOLE.has(dbType)
}

export function metadataCompletionFallback(dbType: string): boolean {
  return FALLBACK_METADATA.has(dbType)
}
