import type {
  NotifyTargetInSpec,
  WorkflowEdge,
  WorkflowNode,
  WorkflowNodeKind,
  WorkflowSpec,
  WorkflowVariableValue,
} from '../../api/workflow'

export interface WorkflowEditorValue {
  name: string
  enabled: boolean
  spec: WorkflowSpec
}

export interface VariableRow {
  name: string
  mode: 'scalar' | 'list'
  value: string
  values: string[]
}

const NODE_KINDS = new Set<WorkflowNodeKind>([
  'sql_query',
  'sql_explain',
  'compare_run',
  'lineage_analyze',
  'export_excel',
  'notify',
  'sleep',
  'branch',
])

export function cloneValue<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

export function defaultPayload(kind: WorkflowNodeKind): Record<string, unknown> {
  if (kind === 'sleep') return { duration_seconds: 60 }
  if (kind === 'notify') return { target_ids: [], message: '' }
  if (kind === 'branch') return {}
  if (kind === 'export_excel') return { source_result_set_id: '', filename: 'result.xlsx' }
  if (kind === 'compare_run') return { task_id: '' }
  if (kind === 'lineage_analyze') return { datasource_id: '', sql_text: '' }
  return { datasource_id: '', sql: '' }
}

export function defaultNode(index: number): WorkflowNode {
  return {
    id: `node_${index + 1}`,
    job_kind: 'sql_query',
    payload: defaultPayload('sql_query'),
    retry_policy: null,
    timeout_seconds: 60,
    on_failure: 'abort',
    when: null,
  }
}

export function emptyWorkflowSpec(): WorkflowSpec {
  return {
    nodes: [defaultNode(0)],
    edges: [],
    schedule: null,
    sensor: null,
    notifications: [],
    variables: {},
  }
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function normalizeNode(value: unknown, index: number): WorkflowNode {
  const raw = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  const rawKind = typeof raw.job_kind === 'string' ? raw.job_kind : 'sql_query'
  const jobKind = NODE_KINDS.has(rawKind as WorkflowNodeKind)
    ? (rawKind as WorkflowNodeKind)
    : 'sql_query'
  const payload =
    raw.payload && typeof raw.payload === 'object' && !Array.isArray(raw.payload)
      ? cloneValue(raw.payload as Record<string, unknown>)
      : defaultPayload(jobKind)
  return {
    id: typeof raw.id === 'string' ? raw.id : `node_${index + 1}`,
    job_kind: jobKind,
    payload: jobKind === 'branch' ? {} : payload,
    retry_policy:
      raw.retry_policy && typeof raw.retry_policy === 'object'
        ? {
            max_retries: numberValue(
              (raw.retry_policy as Record<string, unknown>).max_retries,
              0,
            ),
            backoff_seconds: numberValue(
              (raw.retry_policy as Record<string, unknown>).backoff_seconds,
              0,
            ),
          }
        : null,
    timeout_seconds: numberValue(raw.timeout_seconds, 60),
    on_failure:
      raw.on_failure === 'continue' || raw.on_failure === 'branch'
        ? raw.on_failure
        : 'abort',
    when: typeof raw.when === 'string' ? raw.when : null,
  }
}

function normalizeEdge(value: unknown): WorkflowEdge {
  const raw = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  return {
    source: typeof raw.source === 'string' ? raw.source : '',
    target: typeof raw.target === 'string' ? raw.target : '',
    trigger: raw.trigger === 'failure' ? 'failure' : 'success',
    when: typeof raw.when === 'string' ? raw.when : null,
    is_default: raw.is_default === true,
  }
}

export function normalizeWorkflowSpec(
  value: unknown,
  notifications: NotifyTargetInSpec[] = [],
): WorkflowSpec {
  const raw = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  const rawSchedule =
    raw.schedule && typeof raw.schedule === 'object'
      ? (raw.schedule as Record<string, unknown>)
      : null
  const rawSensor =
    raw.sensor && typeof raw.sensor === 'object'
      ? (raw.sensor as Record<string, unknown>)
      : null
  const rawVariables =
    raw.variables && typeof raw.variables === 'object' && !Array.isArray(raw.variables)
      ? (raw.variables as Record<string, unknown>)
      : {}
  const variables: Record<string, WorkflowVariableValue> = {}
  for (const [name, variable] of Object.entries(rawVariables)) {
    if (typeof variable === 'string') variables[name] = variable
    if (Array.isArray(variable) && variable.every((item) => typeof item === 'string')) {
      variables[name] = [...variable]
    }
  }
  const embeddedNotifications = Array.isArray(raw.notifications)
    ? (cloneValue(raw.notifications) as NotifyTargetInSpec[])
    : cloneValue(notifications)
  return {
    nodes: Array.isArray(raw.nodes)
      ? raw.nodes.map(normalizeNode)
      : [defaultNode(0)],
    edges: Array.isArray(raw.edges) ? raw.edges.map(normalizeEdge) : [],
    schedule: rawSchedule
      ? {
          cron: typeof rawSchedule.cron === 'string' ? rawSchedule.cron : '',
          enabled: rawSchedule.enabled !== false,
        }
      : null,
    sensor: rawSensor
      ? {
          sql: typeof rawSensor.sql === 'string' ? rawSensor.sql : '',
          datasource_id:
            typeof rawSensor.datasource_id === 'string' ? rawSensor.datasource_id : '',
          check_interval_seconds: numberValue(rawSensor.check_interval_seconds, 60),
          cooldown_seconds: numberValue(rawSensor.cooldown_seconds, 300),
          enabled: rawSensor.enabled !== false,
        }
      : null,
    notifications: embeddedNotifications,
    variables,
  }
}

export function specForAdvancedJson(spec: WorkflowSpec): Record<string, unknown> {
  return {
    nodes: cloneValue(spec.nodes),
    edges: cloneValue(spec.edges),
    schedule: cloneValue(spec.schedule),
    sensor: cloneValue(spec.sensor),
    variables: cloneValue(spec.variables),
  }
}

export function variableRows(variables: Record<string, WorkflowVariableValue>): VariableRow[] {
  return Object.entries(variables).map(([name, value]) => ({
    name,
    mode: Array.isArray(value) ? 'list' : 'scalar',
    value: Array.isArray(value) ? '' : value,
    values: Array.isArray(value) ? [...value] : [],
  }))
}

export function variablesFromRows(rows: VariableRow[]): Record<string, WorkflowVariableValue> {
  const variables: Record<string, WorkflowVariableValue> = {}
  for (const row of rows) {
    const name = row.name.trim()
    if (!name) continue
    variables[name] = row.mode === 'list' ? [...row.values] : row.value
  }
  return variables
}

export function ensureSuccessEdge(
  edges: WorkflowEdge[],
  source: string,
  target: string,
): WorkflowEdge[] {
  if (
    edges.some(
      (edge) =>
        edge.source === source && edge.target === target && edge.trigger === 'success',
    )
  ) {
    return edges
  }
  return [...edges, { source, target, trigger: 'success', when: null, is_default: false }]
}

export function safeOutputEntries(
  outputs: unknown,
): [string, string | number | boolean | null][] {
  if (!outputs || typeof outputs !== 'object' || Array.isArray(outputs)) return []
  return Object.entries(outputs).filter(
    (entry): entry is [string, string | number | boolean | null] =>
      entry[1] === null ||
      typeof entry[1] === 'string' ||
      typeof entry[1] === 'number' ||
      typeof entry[1] === 'boolean',
  )
}
