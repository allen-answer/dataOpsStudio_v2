/**
 * useJobPoll —— 轮询一个 job 直到终态。
 *
 * 触发(start)后:
 *   1. 立刻 GET /jobs/{id}(避免假性等待 1.5s)
 *   2. 终态:state.status = success/failed/cancelled/timeout,stop
 *      非终态:1.5s 后再拉
 *   3. cancel():POST /jobs/{id}/cancel,后端协作式退出,继续轮询直到看到
 *      cancelled / 终态
 *   4. stop():手动停轮询(组件 unmount 时调)
 *
 * 终态后:state.result_set_id / state.error / state.message 可读。
 *
 * 不依赖 vue-query —— 轮询是状态机,不是缓存;用 reactive ref + setTimeout 链。
 */
import { ref, reactive, onUnmounted } from 'vue'
import { getJob, cancelJob, type JobResponse } from '../api/jobs'
import { ApiError, type JobStatus } from '../api/types'

const TERMINAL: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'success',
  'failed',
  'cancelled',
  'timeout',
])

interface PollState {
  jobId: string | null
  status: JobStatus | null
  resultSetId: string | null
  error: string | null
  message: string | null
  cancelling: boolean
}

export interface UseJobPollOptions {
  /** poll 间隔 ms,默认 1500 */
  intervalMs?: number
}

export function useJobPoll(options: UseJobPollOptions = {}) {
  const intervalMs = options.intervalMs ?? 1500

  const state = reactive<PollState>({
    jobId: null,
    status: null,
    resultSetId: null,
    error: null,
    message: null,
    cancelling: false,
  })

  const polling = ref(false)
  let timerId: ReturnType<typeof setTimeout> | null = null
  let abortGen = 0 // 防止 stale poll 在 reset/start 后还接着写状态

  function applyJob(job: JobResponse): void {
    state.status = job.status
    state.resultSetId = job.result_set_id
    state.error = job.error
    state.message = job.message
  }

  async function pollOnce(myGen: number): Promise<void> {
    if (myGen !== abortGen || !state.jobId) return
    try {
      const job = await getJob(state.jobId)
      if (myGen !== abortGen) return
      applyJob(job)
      if (TERMINAL.has(job.status)) {
        polling.value = false
        return
      }
    } catch (e) {
      if (myGen !== abortGen) return
      // 401 已由 client 处理;其他错误记到 error 但继续轮询(后端可能短暂不可用)
      if (e instanceof ApiError && e.status !== 0) {
        state.error = e.message
      }
    }
    timerId = setTimeout(() => pollOnce(myGen), intervalMs)
  }

  function start(jobId: string): void {
    stop()
    abortGen += 1
    state.jobId = jobId
    state.status = 'pending'
    state.resultSetId = null
    state.error = null
    state.message = null
    state.cancelling = false
    polling.value = true
    void pollOnce(abortGen)
  }

  function stop(): void {
    abortGen += 1
    polling.value = false
    if (timerId !== null) {
      clearTimeout(timerId)
      timerId = null
    }
  }

  function reset(): void {
    stop()
    state.jobId = null
    state.status = null
    state.resultSetId = null
    state.error = null
    state.message = null
    state.cancelling = false
  }

  async function cancel(): Promise<void> {
    if (!state.jobId || state.cancelling) return
    state.cancelling = true
    try {
      await cancelJob(state.jobId)
      // 不直接 setStatus('cancelled') —— 后端协作取消,worker 真停下 / 真改库后,
      // 下一次 poll 才把 status 翻到 cancelled。这里只标记 cancelling=true 给 UI。
    } catch (e) {
      state.cancelling = false
      if (e instanceof ApiError) state.error = e.message
      throw e
    }
  }

  onUnmounted(() => stop())

  return { state, polling, start, stop, reset, cancel }
}
