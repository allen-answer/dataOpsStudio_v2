<script setup lang="ts">
/**
 * AdminUsersView —— /admin/users(PRD §7)。
 *
 * 后端端点(app/api/routes/admin.py):
 *   GET    /admin/users                         列表
 *   POST   /admin/users                         新建(username/role/initial_password)
 *   PATCH  /admin/users/{id}                     改角色(只 role)
 *   POST   /admin/users/{id}/reset-password      重置 → 返一次性临时密码
 *   POST   /admin/users/{id}/mfa/disable         admin 关 MFA
 *   DELETE /admin/users/{id}                     删除(409 user_owns_projects)
 *
 * ★ 后端 AdminUserItem 不含 last_seen_at,故"最后活跃"列不做(PRD 列了,后端无字段)。
 * ★ 强制下线(吊销 JWT)无端点,不做。
 */
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  Users,
  Plus,
  AlertTriangle,
  Pencil,
  KeyRound,
  ShieldOff,
  Trash2,
  Copy,
  Check,
} from 'lucide-vue-next'
import {
  listAdminUsers,
  createAdminUser,
  patchAdminUserRole,
  resetAdminUserPassword,
  disableAdminUserMfa,
  deleteAdminUser,
} from '../api/admin'
import { ApiError, type AdminUserItem, type Project } from '../api/types'
import { useLicense } from '../composables/useLicense'
import EmptyState from '../components/EmptyState.vue'
import LoadingDots from '../components/LoadingDots.vue'
import Modal from '../components/Modal.vue'

const { t } = useI18n()
const qc = useQueryClient()
const { writesBlocked } = useLicense()

const ROLES = ['admin', 'editor', 'viewer'] as const

const query = useQuery({ queryKey: ['admin-users'], queryFn: listAdminUsers })
const users = computed<AdminUserItem[]>(() => query.data.value ?? [])

function invalidate(): Promise<void> {
  return qc.invalidateQueries({ queryKey: ['admin-users'] })
}

// ─── 新建 ─────────────────────────────────────────────────
const createOpen = ref(false)
const createForm = reactive({ username: '', role: 'viewer', initial_password: '' })
const createError = ref<string | null>(null)

function openCreate(): void {
  createForm.username = ''
  createForm.role = 'viewer'
  createForm.initial_password = ''
  createError.value = null
  createOpen.value = true
}

const createMutation = useMutation({
  mutationFn: () =>
    createAdminUser({
      username: createForm.username.trim(),
      role: createForm.role,
      initial_password: createForm.initial_password,
    }),
  onSuccess: async () => {
    await invalidate()
    createOpen.value = false
  },
})

async function submitCreate(): Promise<void> {
  createError.value = null
  if (!createForm.username.trim() || !createForm.initial_password) {
    createError.value = t('common.error_required_fields')
    return
  }
  try {
    await createMutation.mutateAsync()
  } catch (e) {
    createError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

// ─── 改角色 ───────────────────────────────────────────────
const roleModal = ref<AdminUserItem | null>(null)
const roleDraft = ref<string>('viewer')
const roleArmed = ref(false) // 降级 admin 二次确认
const roleError = ref<string | null>(null)

function openRole(u: AdminUserItem): void {
  roleModal.value = u
  roleDraft.value = u.role
  roleArmed.value = false
  roleError.value = null
}

const isAdminDowngrade = computed(
  () => roleModal.value?.role === 'admin' && roleDraft.value !== 'admin',
)

const roleMutation = useMutation({
  mutationFn: () => patchAdminUserRole(roleModal.value!.id, roleDraft.value),
  onSuccess: async () => {
    await invalidate()
    roleModal.value = null
  },
})

async function submitRole(): Promise<void> {
  roleError.value = null
  if (!roleModal.value) return
  if (roleDraft.value === roleModal.value.role) {
    roleModal.value = null
    return
  }
  // 降级 admin → 二次确认
  if (isAdminDowngrade.value && !roleArmed.value) {
    roleArmed.value = true
    return
  }
  try {
    await roleMutation.mutateAsync()
  } catch (e) {
    roleError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

// ─── 重置密码(展示一次性临时密码)──────────────────────────
const resetModal = ref<AdminUserItem | null>(null)
const tempPassword = ref<string | null>(null)
const resetError = ref<string | null>(null)
const copied = ref(false)

function openReset(u: AdminUserItem): void {
  resetModal.value = u
  tempPassword.value = null
  resetError.value = null
  copied.value = false
}

const resetMutation = useMutation({
  mutationFn: () => resetAdminUserPassword(resetModal.value!.id),
  onSuccess: (res) => {
    tempPassword.value = res.temporary_password
  },
})

async function submitReset(): Promise<void> {
  resetError.value = null
  try {
    await resetMutation.mutateAsync()
  } catch (e) {
    resetError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

async function copyTemp(): Promise<void> {
  if (!tempPassword.value) return
  try {
    await navigator.clipboard.writeText(tempPassword.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    /* clipboard 不可用时静默 —— 用户仍可手动选中 */
  }
}

// ─── 关 MFA ───────────────────────────────────────────────
const mfaMutation = useMutation({
  mutationFn: (userId: string) => disableAdminUserMfa(userId),
  onSuccess: invalidate,
})
async function onDisableMfa(u: AdminUserItem): Promise<void> {
  if (!confirm(t('admin.users.confirm_disable_mfa', { name: u.username }))) return
  try {
    await mfaMutation.mutateAsync(u.id)
  } catch {
    /* 失败提示走 list 刷新 / 控制台;此处不阻断 */
  }
}

// ─── 删除(409 列引用清单)─────────────────────────────────
const deleteModal = ref<AdminUserItem | null>(null)
const ownedProjects = ref<Project[]>([])
const deleteError = ref<string | null>(null)

function openDelete(u: AdminUserItem): void {
  deleteModal.value = u
  ownedProjects.value = []
  deleteError.value = null
}

const deleteMutation = useMutation({
  mutationFn: () => deleteAdminUser(deleteModal.value!.id),
  onSuccess: async () => {
    await invalidate()
    deleteModal.value = null
  },
})

async function submitDelete(): Promise<void> {
  deleteError.value = null
  ownedProjects.value = []
  try {
    await deleteMutation.mutateAsync()
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      const body = e.body as { owned_projects?: Project[] } | undefined
      ownedProjects.value = body?.owned_projects ?? []
      deleteError.value = t('admin.users.delete_blocked')
    } else {
      deleteError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
    }
  }
}

// ─── 展示工具 ─────────────────────────────────────────────
function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

const ROLE_STYLE: Record<string, string> = {
  admin:
    'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300 border border-red-200/60 dark:border-red-500/30',
  editor:
    'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 border border-sky-200/60 dark:border-sky-500/30',
  viewer:
    'bg-slate-50 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300 border border-slate-200/60 dark:border-slate-500/30',
}
function roleClass(role: string): string {
  return ROLE_STYLE[role] ?? ROLE_STYLE.viewer
}

function errorMessage(): string {
  const e = query.error.value
  if (e instanceof ApiError) {
    if (e.status === 403) return t('admin.forbidden_hint')
    return e.message || t('common.error_unknown')
  }
  return t('common.error_unknown')
}
</script>

<template>
  <div class="px-6 lg:px-10 py-8 w-full">
    <!-- header -->
    <div class="flex items-end justify-between mb-6">
      <div>
        <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">
          {{ t('admin.users.title') }}
        </h1>
        <div
          v-if="!query.isLoading.value && !query.isError.value"
          class="text-sm chrome-text-muted mt-1"
        >
          {{ t('admin.users.count', { count: users.length }) }}
        </div>
      </div>
      <button
        type="button"
        class="chrome-btn-primary"
        :disabled="writesBlocked"
        :title="writesBlocked ? t('license.writes_blocked') : ''"
        @click="openCreate"
      >
        <Plus class="w-4 h-4" />
        {{ t('admin.users.new') }}
      </button>
    </div>

    <!-- loading -->
    <div
      v-if="query.isLoading.value"
      class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted"
    >
      <LoadingDots />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- error -->
    <div
      v-else-if="query.isError.value"
      class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3"
    >
      <AlertTriangle class="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage() }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">
          {{ t('common.retry') }}
        </button>
      </div>
    </div>

    <!-- empty -->
    <div v-else-if="users.length === 0" class="chrome-bg-panel border chrome-border rounded-card">
      <EmptyState :icon="Users" :title="t('admin.users.empty_title')" :hint="t('admin.users.empty_hint')" />
    </div>

    <!-- table -->
    <div
      v-else
      class="chrome-bg-panel border chrome-border rounded-card overflow-hidden"
      style="box-shadow: var(--shadow-card);"
    >
      <table class="w-full text-data">
        <thead>
          <tr
            class="text-left text-xs chrome-text-muted border-b chrome-border-subtle"
            style="background-color: rgb(var(--bg-panel-elevated) / 0.4);"
          >
            <th class="font-medium py-2 px-3">{{ t('admin.users.col_username') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.users.col_role') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.users.col_mfa') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.users.col_created_at') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('admin.users.col_actions') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="u in users"
            :key="u.id"
            class="border-b chrome-border-subtle last:border-b-0 group transition-colors"
          >
            <td class="py-2 px-3">
              <div class="font-medium chrome-text-heading">{{ u.username }}</div>
              <div class="text-xs font-mono chrome-text-muted mt-0.5">{{ u.id.slice(0, 8) }}</div>
            </td>
            <td class="py-2 px-3">
              <span
                class="inline-flex items-center px-1.5 py-0.5 rounded-input text-xs font-medium"
                :class="roleClass(u.role)"
              >
                {{ u.role }}
              </span>
            </td>
            <td class="py-2 px-3">
              <span
                v-if="u.mfa_enabled"
                class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1"
              >
                <Check class="w-3.5 h-3.5" /> {{ t('admin.users.mfa_on') }}
              </span>
              <span v-else class="text-xs chrome-text-muted">{{ t('admin.users.mfa_off') }}</span>
            </td>
            <td class="py-2 px-3 chrome-text-muted tabular-nums">{{ formatDate(u.created_at) }}</td>
            <td class="py-2 px-3">
              <div class="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button type="button" class="chrome-btn-ghost" :disabled="writesBlocked" :title="t('admin.users.action_role')" @click="openRole(u)">
                  <Pencil class="w-3.5 h-3.5" />
                </button>
                <button type="button" class="chrome-btn-ghost" :disabled="writesBlocked" :title="t('admin.users.action_reset')" @click="openReset(u)">
                  <KeyRound class="w-3.5 h-3.5" />
                </button>
                <button
                  v-if="u.mfa_enabled"
                  type="button"
                  class="chrome-btn-ghost"
                  :disabled="writesBlocked"
                  :title="t('admin.users.action_disable_mfa')"
                  @click="onDisableMfa(u)"
                >
                  <ShieldOff class="w-3.5 h-3.5" />
                </button>
                <button type="button" class="chrome-btn-ghost hover:!text-red-500" :disabled="writesBlocked" :title="t('common.delete')" @click="openDelete(u)">
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新建 modal -->
    <Modal :open="createOpen" :title="t('admin.users.create_title')" @close="createOpen = false">
      <form @submit.prevent="submitCreate" class="space-y-4">
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.users.field_username') }}</label>
          <input v-model="createForm.username" type="text" maxlength="64" class="chrome-input w-full" autocomplete="off" :disabled="createMutation.isPending.value" />
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.users.field_role') }}</label>
          <select v-model="createForm.role" class="chrome-input w-full" :disabled="createMutation.isPending.value">
            <option v-for="r in ROLES" :key="r" :value="r">{{ r }}</option>
          </select>
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.users.field_initial_password') }}</label>
          <input v-model="createForm.initial_password" type="password" class="chrome-input w-full" autocomplete="new-password" :disabled="createMutation.isPending.value" />
        </div>
        <div v-if="createError" class="text-xs text-red-500">{{ createError }}</div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="chrome-btn-secondary" @click="createOpen = false" :disabled="createMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="submit" class="chrome-btn-primary" :disabled="createMutation.isPending.value">
            <template v-if="createMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ t('admin.users.create_submit') }}</span>
          </button>
        </div>
      </form>
    </Modal>

    <!-- 改角色 modal -->
    <Modal :open="roleModal !== null" :title="t('admin.users.role_title')" :subtitle="roleModal?.username" @close="roleModal = null">
      <form @submit.prevent="submitRole" class="space-y-4">
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.users.field_role') }}</label>
          <select v-model="roleDraft" class="chrome-input w-full" :disabled="roleMutation.isPending.value">
            <option v-for="r in ROLES" :key="r" :value="r">{{ r }}</option>
          </select>
        </div>
        <div
          v-if="isAdminDowngrade"
          class="flex items-start gap-2 rounded-input px-3 py-2 text-xs"
          style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
        >
          <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
          <span>{{ roleArmed ? t('admin.users.downgrade_armed') : t('admin.users.downgrade_hint') }}</span>
        </div>
        <div v-if="roleError" class="text-xs text-red-500">{{ roleError }}</div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="chrome-btn-secondary" @click="roleModal = null" :disabled="roleMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="submit" class="chrome-btn-primary" :class="{ 'chrome-btn-danger': isAdminDowngrade && roleArmed }" :disabled="roleMutation.isPending.value">
            <template v-if="roleMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ isAdminDowngrade && roleArmed ? t('admin.users.downgrade_confirm') : t('common.save') }}</span>
          </button>
        </div>
      </form>
    </Modal>

    <!-- 重置密码 modal -->
    <Modal :open="resetModal !== null" :title="t('admin.users.reset_title')" :subtitle="resetModal?.username" @close="resetModal = null">
      <div class="space-y-4">
        <template v-if="tempPassword === null">
          <p class="text-sm chrome-text-normal">{{ t('admin.users.reset_hint') }}</p>
          <div v-if="resetError" class="text-xs text-red-500">{{ resetError }}</div>
          <div class="flex justify-end gap-2 pt-2">
            <button type="button" class="chrome-btn-secondary" @click="resetModal = null" :disabled="resetMutation.isPending.value">{{ t('common.cancel') }}</button>
            <button type="button" class="chrome-btn-primary" @click="submitReset" :disabled="resetMutation.isPending.value">
              <template v-if="resetMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
              <span v-else>{{ t('admin.users.reset_submit') }}</span>
            </button>
          </div>
        </template>
        <template v-else>
          <div class="rounded-input border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2.5 text-xs text-amber-800 dark:text-amber-300">
            {{ t('admin.users.reset_once_warning') }}
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('admin.users.temp_password') }}</label>
            <div class="flex items-center gap-2">
              <code class="flex-1 chrome-input font-mono text-sm break-all select-all">{{ tempPassword }}</code>
              <button type="button" class="chrome-btn-secondary" @click="copyTemp">
                <component :is="copied ? Check : Copy" class="w-4 h-4" />
                {{ copied ? t('common.copied') : t('common.copy') }}
              </button>
            </div>
          </div>
          <div class="flex justify-end pt-2">
            <button type="button" class="chrome-btn-primary" @click="resetModal = null">{{ t('common.done') }}</button>
          </div>
        </template>
      </div>
    </Modal>

    <!-- 删除 modal -->
    <Modal :open="deleteModal !== null" :title="t('admin.users.delete_title')" :subtitle="deleteModal?.username" @close="deleteModal = null">
      <div class="space-y-4">
        <p class="text-sm chrome-text-normal">{{ t('admin.users.delete_confirm') }}</p>
        <div v-if="deleteError" class="text-xs text-red-500">{{ deleteError }}</div>
        <!-- 409 引用清单 -->
        <div
          v-if="ownedProjects.length > 0"
          class="rounded-input border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3 py-2.5"
        >
          <div class="text-xs font-medium text-red-700 dark:text-red-400 mb-1.5">
            {{ t('admin.users.owned_projects', { count: ownedProjects.length }) }}
          </div>
          <ul class="space-y-1">
            <li v-for="p in ownedProjects" :key="p.id" class="text-xs text-red-600 dark:text-red-300 flex items-center gap-2">
              <span class="font-medium">{{ p.name }}</span>
              <span class="font-mono opacity-70">{{ p.id.slice(0, 8) }}</span>
            </li>
          </ul>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="chrome-btn-secondary" @click="deleteModal = null" :disabled="deleteMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="button" class="chrome-btn-primary chrome-btn-danger" @click="submitDelete" :disabled="deleteMutation.isPending.value">
            <template v-if="deleteMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ t('common.delete') }}</span>
          </button>
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.form-label {
  @apply block text-xs uppercase tracking-wider font-medium;
  color: rgb(var(--text-muted));
}
</style>
