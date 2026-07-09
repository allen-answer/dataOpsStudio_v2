/**
 * useLicense —— 全局 license 状态(D.4 顶部横条 + REPAIR/IN_GRACE 写操作守护)。
 *
 * 数据源:GET /api/license/status(已登录可访问,见 app/api/routes/core.py)。
 * 用 TanStack Query 缓存,60s 刷新一次;app shell 横条 + 各页"写操作 disabled"共用。
 *
 * ★ 后端 middleware(crosscutting.py)在 REPAIR / IN_GRACE 下对非安全方法返 403:
 *   - REPAIR:除 license 更新 / backup / restore / diagnostics 外,全部 POST/PUT/PATCH/DELETE 403
 *   - IN_GRACE:除 license 更新 / backup 外,全部写操作 403
 *   前端据此把写按钮 disabled 并给出文案(/admin/license 例外,始终可用)。
 */
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { getLicenseStatus } from '../api/admin'
import type { LicenseMode, LicenseStatus } from '../api/types'

export function useLicense() {
  const query = useQuery({
    queryKey: ['license-status'],
    queryFn: getLicenseStatus,
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: 0,
  })

  const status = computed<LicenseStatus | null>(() => query.data.value ?? null)
  const mode = computed<LicenseMode | null>(() => status.value?.mode ?? null)

  const isRepair = computed(() => mode.value === 'repair')
  const isInGrace = computed(() => mode.value === 'in_grace')
  const isExpired = computed(() => mode.value === 'expired')
  const isTrial = computed(() => mode.value === 'trial')
  const enforcementEnabled = computed(() => status.value?.license_enforcement_enabled ?? true)

  /** 是否应阻止普通写操作(REPAIR 全锁;IN_GRACE 部分锁)。横条/按钮共用。 */
  const writesBlocked = computed(() => enforcementEnabled.value && (isRepair.value || isInGrace.value))

  /** 横条是否显示(VALID 不显示)。 */
  const showBanner = computed(
    () => enforcementEnabled.value && mode.value != null && mode.value !== 'valid',
  )

  return {
    query,
    status,
    mode,
    isRepair,
    isInGrace,
    isExpired,
    isTrial,
    enforcementEnabled,
    writesBlocked,
    showBanner,
  }
}
