/**
 * useConsoleSessions —— SQL 工作台的 console→会话绑定态(Session Broker 设计 §3.3)。
 *
 * 只管**会话**这一层:懒 attach、epoch 持有、接管/丢失判定、回退路由。
 * 语句执行、轮询与结果读取仍在 `SqlWorkspaceView` 里,按 statement 维度复用
 * 既有 job 轮询机器(字段镜像见 `api/sessions.ts` toJobProgress)。
 *
 * 两个不变量:
 *  1. **懒 attach** —— 打开工作台不占连接,只有 `ensureSession` 被首次执行调用
 *     时才建会话;切 console 只 `observe` 一次展示状态。
 *  2. **回退不是错误** —— `console_session_disabled` / `console_session_unsupported`
 *     两个 409 使 `ensureSession` 返回 null,调用方整体走 job 路径。前者是部署级
 *     开关(记在 `routing` 上,全局只撞一次),后者是方言级(记在该 console 上)。
 */
import { reactive, ref, type Ref } from 'vue'
import {
  ACTIVE_SESSION_STATES,
  attachSession,
  closeSession,
  observeSession,
  SESSION_FALLBACK_CODES,
  type ServerCancelState,
  type SessionResponse,
  type SessionState,
  type StatementSessionBlock,
} from '../api/sessions'
import { ApiError } from '../api/types'

/** 与后端 `app/broker/wiring.py:SESSION_CAPABLE_DB_TYPES` 同口径。 */
export const SESSION_CAPABLE_DB_TYPES: ReadonlySet<string> = new Set(['mysql', 'dm'])

export interface ConsoleSessionState {
  sessionId: string | null
  /** 本 tab 持有的 epoch;`< currentEpoch` 即已被接管。 */
  epoch: number
  currentEpoch: number
  state: SessionState | null
  dbType: string | null
  serverCancel: ServerCancelState
  idleDeadline: string | null
  closeReason: string | null
  errorCode: string | null
  attaching: boolean
  /** 会话层错误(attach 失败 / 观察失败),与语句错误分开渲染。 */
  error: string | null
  /** 该 console 的方言没有会话实现 → 永久走 job 路径。 */
  unsupported: boolean
}

/** 部署级回退开关的已知状态。`disabled` 后不再对任何 console 试 attach。 */
export type SessionRouting = 'unknown' | 'enabled' | 'disabled'

function blankSession(): ConsoleSessionState {
  return {
    sessionId: null,
    epoch: 0,
    currentEpoch: 0,
    state: null,
    dbType: null,
    serverCancel: 'unknown',
    idleDeadline: null,
    closeReason: null,
    errorCode: null,
    attaching: false,
    error: null,
    unsupported: false,
  }
}

export interface UseConsoleSessions {
  routing: Ref<SessionRouting>
  sessions: Record<string, ConsoleSessionState>
  sessionFor: (consoleId: string | null) => ConsoleSessionState | null
  /** 已被另一窗口接管(M1):本 tab 的 epoch 落后于会话当前 epoch。 */
  isTakenOver: (consoleId: string | null) => boolean
  /** 会话已不可续用:session_lost / connect_failed / closed。 */
  isLost: (consoleId: string | null) => boolean
  ensureSession: (consoleId: string, dbType: string | null) => Promise<ConsoleSessionState | null>
  reattach: (consoleId: string, dbType: string | null) => Promise<ConsoleSessionState | null>
  observe: (consoleId: string) => Promise<void>
  applySessionBlock: (consoleId: string, block: StatementSessionBlock) => void
  applyStaleEpoch: (consoleId: string, currentEpoch: number | undefined) => void
  markLost: (consoleId: string, code: string) => void
  close: (consoleId: string) => Promise<void>
  forget: (consoleId: string) => void
}

export function useConsoleSessions(): UseConsoleSessions {
  const routing = ref<SessionRouting>('unknown')
  const sessions = reactive<Record<string, ConsoleSessionState>>({})

  function slot(consoleId: string): ConsoleSessionState {
    if (!sessions[consoleId]) sessions[consoleId] = blankSession()
    return sessions[consoleId]
  }

  function sessionFor(consoleId: string | null): ConsoleSessionState | null {
    return consoleId ? (sessions[consoleId] ?? null) : null
  }

  function isTakenOver(consoleId: string | null): boolean {
    const current = sessionFor(consoleId)
    return Boolean(current?.sessionId && current.currentEpoch > current.epoch)
  }

  function isLost(consoleId: string | null): boolean {
    const current = sessionFor(consoleId)
    if (!current?.state) return false
    return !ACTIVE_SESSION_STATES.has(current.state)
  }

  function apply(consoleId: string, response: SessionResponse): ConsoleSessionState {
    const entry = slot(consoleId)
    entry.sessionId = response.session_id
    entry.epoch = response.epoch
    entry.currentEpoch = response.current_epoch
    entry.state = response.state
    entry.dbType = response.db_type
    entry.serverCancel = response.server_cancel
    entry.idleDeadline = response.idle_deadline
    entry.closeReason = response.close_reason
    entry.errorCode = response.error_code
    entry.error = null
    return entry
  }

  /**
   * 把 409 归类:回退码 → 记路由后返回 null(调用方走 job 路径);
   * 其余抛回给调用方按会话冲突处理。
   */
  function classifyAttachError(consoleId: string, error: unknown): 'fallback' | 'error' {
    if (!(error instanceof ApiError)) return 'error'
    // 滚动升级兜底:端点不存在(路由级 404,没有业务 code)= 这套部署还没有
    // 会话能力,整体走 job 路径。与 `SqlWorkspaceView.loadJobProgress` 对
    // `/progress` 的同款兜底一致;业务 404(not_found)不在此列。
    if (error.status === 404 && error.code !== 'not_found') {
      routing.value = 'disabled'
      return 'fallback'
    }
    if (error.status !== 409) return 'error'
    const code = error.code ?? ''
    if (!SESSION_FALLBACK_CODES.has(code)) return 'error'
    if (code === 'console_session_disabled') routing.value = 'disabled'
    else slot(consoleId).unsupported = true
    return 'fallback'
  }

  async function doAttach(
    consoleId: string,
    dbType: string | null,
  ): Promise<ConsoleSessionState | null> {
    const entry = slot(consoleId)
    entry.attaching = true
    entry.error = null
    try {
      const response = await attachSession(consoleId)
      routing.value = 'enabled'
      return apply(consoleId, response)
    } catch (e) {
      if (classifyAttachError(consoleId, e) === 'fallback') return null
      throw e
    } finally {
      entry.attaching = false
      if (dbType) entry.dbType = entry.dbType ?? dbType
    }
  }

  /**
   * 首次执行时懒 attach;已有活会话且本 tab 仍是持有者时**直接复用**,不重
   * 建连接也不 bump epoch —— 免握手延迟正是只读片的可感知收益(设计 §6)。
   */
  async function ensureSession(
    consoleId: string,
    dbType: string | null,
  ): Promise<ConsoleSessionState | null> {
    if (routing.value === 'disabled') return null
    if (dbType && !SESSION_CAPABLE_DB_TYPES.has(dbType)) {
      slot(consoleId).unsupported = true
      return null
    }
    const entry = slot(consoleId)
    if (entry.unsupported) return null
    if (entry.sessionId && !isLost(consoleId) && !isTakenOver(consoleId)) return entry
    return doAttach(consoleId, dbType)
  }

  /** 「重新连接」按钮:无论当前是丢失还是被接管,都重新 attach 抢回持有权。 */
  async function reattach(
    consoleId: string,
    dbType: string | null,
  ): Promise<ConsoleSessionState | null> {
    if (routing.value === 'disabled') return null
    const entry = slot(consoleId)
    if (entry.unsupported) return null
    entry.state = null
    entry.sessionId = null
    return doAttach(consoleId, dbType)
  }

  /** 切 console 时的单次 observe(不轮询,设计 §3.3)。 */
  async function observe(consoleId: string): Promise<void> {
    const entry = sessions[consoleId]
    if (!entry?.sessionId) return
    try {
      apply(consoleId, await observeSession(entry.sessionId))
    } catch (e) {
      // 会话没了就是没了 —— 如实标记,不静默保留一个假的 idle 状态。
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
        entry.state = 'session_lost'
        entry.errorCode = e.code ?? entry.errorCode
      }
    }
  }

  /** progress 内嵌会话块 ⇒ 执行期间无需第二条轮询(设计 §3.2)。 */
  function applySessionBlock(consoleId: string, block: StatementSessionBlock): void {
    const entry = slot(consoleId)
    if (entry.sessionId && entry.sessionId !== block.session_id) return
    entry.sessionId = block.session_id
    entry.state = block.state
    entry.currentEpoch = block.current_epoch
  }

  /** 409 stale_session_epoch:响应体带 current_epoch,直接落成接管提示。 */
  function applyStaleEpoch(consoleId: string, currentEpoch: number | undefined): void {
    const entry = slot(consoleId)
    entry.currentEpoch =
      typeof currentEpoch === 'number' ? currentEpoch : Math.max(entry.currentEpoch, entry.epoch + 1)
  }

  function markLost(consoleId: string, code: string): void {
    const entry = slot(consoleId)
    entry.state = 'session_lost'
    entry.errorCode = code
  }

  async function close(consoleId: string): Promise<void> {
    const entry = sessions[consoleId]
    if (!entry?.sessionId) return
    const sessionId = entry.sessionId
    try {
      apply(consoleId, await closeSession(sessionId, entry.epoch))
    } catch (e) {
      // close 是收尾动作:会话已经不在了(404/409)与关成功对用户等价。
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
        entry.state = 'closed'
        return
      }
      throw e
    }
  }

  function forget(consoleId: string): void {
    delete sessions[consoleId]
  }

  return {
    routing,
    sessions,
    sessionFor,
    isTakenOver,
    isLost,
    ensureSession,
    reattach,
    observe,
    applySessionBlock,
    applyStaleEpoch,
    markLost,
    close,
    forget,
  }
}
