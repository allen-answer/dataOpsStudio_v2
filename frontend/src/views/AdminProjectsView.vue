<script setup lang="ts">
/**
 * AdminProjectsView —— /admin/projects(PRD §8)。
 *
 * 后端端点(app/api/routes/admin.py):
 *   GET    /admin/projects                       列表(含 member/datasource/job 计数)
 *   POST   /admin/projects                       新建(name/description/owner_user_id/members)
 *   PATCH  /admin/projects/{id}                   编辑(name/description/owner/members)
 *   GET    /admin/projects/{id}/impact            删除前级联影响(datasource_count/job_count)
 *   DELETE /admin/projects/{id}                   删除(返回 impact)
 *
 * owner / members 用 GET /admin/users 的全量用户列表挑选。
 */
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { FolderKanban, Plus, AlertTriangle, Pencil, Trash2 } from 'lucide-vue-next'
import {
  listAdminProjects,
  createAdminProject,
  patchAdminProject,
  getAdminProjectDeleteImpact,
  deleteAdminProject,
  listAdminUsers,
} from '../api/admin'
import {
  ApiError,
  type AdminProjectItem,
  type AdminUserItem,
  type AdminProjectDeleteImpact,
} from '../api/types'
import { useLicense } from '../composables/useLicense'
import EmptyState from '../components/EmptyState.vue'
import LoadingDots from '../components/LoadingDots.vue'
import Modal from '../components/Modal.vue'

const { t } = useI18n()
const qc = useQueryClient()
const { writesBlocked } = useLicense()

const query = useQuery({ queryKey: ['admin-projects'], queryFn: listAdminProjects })
const projects = computed<AdminProjectItem[]>(() => query.data.value ?? [])

// 用户列表 —— owner / members 选择源(仅在 modal 打开时拉)
const usersQuery = useQuery({
  queryKey: ['admin-users'],
  queryFn: listAdminUsers,
})
const allUsers = computed<AdminUserItem[]>(() => usersQuery.data.value ?? [])
function usernameOf(id: string): string {
  return allUsers.value.find((u) => u.id === id)?.username ?? id.slice(0, 8)
}

function invalidate(): Promise<void> {
  return qc.invalidateQueries({ queryKey: ['admin-projects'] })
}

// ─── 新建 / 编辑(共用 modal)──────────────────────────────
type Mode = 'create' | 'edit'
const modalOpen = ref(false)
const mode = ref<Mode>('create')
const editing = ref<AdminProjectItem | null>(null)
const formError = ref<string | null>(null)

const form = reactive({
  name: '',
  description: '',
  owner_user_id: '',
  members: [] as string[],
})

function openCreate(): void {
  mode.value = 'create'
  editing.value = null
  form.name = ''
  form.description = ''
  form.owner_user_id = allUsers.value[0]?.id ?? ''
  form.members = []
  formError.value = null
  modalOpen.value = true
}

function openEdit(p: AdminProjectItem): void {
  mode.value = 'edit'
  editing.value = p
  form.name = p.name
  form.description = p.description ?? ''
  form.owner_user_id = p.owner_user_id
  // 后端列表不返回 members 明细,只有 member_count;编辑时 members 从空起编,
  // 提交后由后端用 owner + members 重建成员表(owner 始终含)。
  form.members = []
  formError.value = null
  modalOpen.value = true
}

// owner 必须包含在 members 选择里(后端会自动并入,但 UI 上 owner 单选独立)
const memberCandidates = computed(() =>
  allUsers.value.filter((u) => u.id !== form.owner_user_id),
)

// owner 改变时,从 members 移除新 owner(避免重复)
watch(
  () => form.owner_user_id,
  (newOwner) => {
    form.members = form.members.filter((id) => id !== newOwner)
  },
)

function toggleMember(id: string): void {
  const i = form.members.indexOf(id)
  if (i >= 0) form.members.splice(i, 1)
  else form.members.push(id)
}

const saveMutation = useMutation({
  mutationFn: () => {
    if (mode.value === 'create') {
      return createAdminProject({
        name: form.name.trim(),
        description: form.description.trim() || null,
        owner_user_id: form.owner_user_id,
        members: form.members,
      })
    }
    return patchAdminProject(editing.value!.id, {
      name: form.name.trim(),
      description: form.description.trim() || null,
      owner_user_id: form.owner_user_id,
      members: form.members,
    })
  },
  onSuccess: async () => {
    await invalidate()
    modalOpen.value = false
  },
})

async function submit(): Promise<void> {
  formError.value = null
  if (!form.name.trim()) {
    formError.value = t('common.error_required_fields')
    return
  }
  if (!form.owner_user_id) {
    formError.value = t('admin.projects.error_no_owner')
    return
  }
  try {
    await saveMutation.mutateAsync()
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

// ─── 删除(先 impact 再二次确认)────────────────────────────
const deleteModal = ref<AdminProjectItem | null>(null)
const impact = ref<AdminProjectDeleteImpact | null>(null)
const impactLoading = ref(false)
const deleteError = ref<string | null>(null)

async function openDelete(p: AdminProjectItem): Promise<void> {
  deleteModal.value = p
  impact.value = null
  deleteError.value = null
  impactLoading.value = true
  try {
    impact.value = await getAdminProjectDeleteImpact(p.id)
  } catch (e) {
    deleteError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  } finally {
    impactLoading.value = false
  }
}

const deleteMutation = useMutation({
  mutationFn: () => deleteAdminProject(deleteModal.value!.id),
  onSuccess: async () => {
    await invalidate()
    deleteModal.value = null
  },
})

async function submitDelete(): Promise<void> {
  deleteError.value = null
  try {
    await deleteMutation.mutateAsync()
  } catch (e) {
    deleteError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

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
    <div class="flex items-end justify-between mb-6">
      <div>
        <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">
          {{ t('admin.projects.title') }}
        </h1>
        <div v-if="!query.isLoading.value && !query.isError.value" class="text-sm chrome-text-muted mt-1">
          {{ t('admin.projects.count', { count: projects.length }) }}
        </div>
      </div>
      <button type="button" class="chrome-btn-primary" :disabled="writesBlocked" :title="writesBlocked ? t('license.writes_blocked') : ''" @click="openCreate">
        <Plus class="w-4 h-4" />
        {{ t('admin.projects.new') }}
      </button>
    </div>

    <div v-if="query.isLoading.value" class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted">
      <LoadingDots /><span>{{ t('common.loading') }}</span>
    </div>

    <div v-else-if="query.isError.value" class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3">
      <AlertTriangle class="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage() }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">{{ t('common.retry') }}</button>
      </div>
    </div>

    <div v-else-if="projects.length === 0" class="chrome-bg-panel border chrome-border rounded-card">
      <EmptyState :icon="FolderKanban" :title="t('admin.projects.empty_title')" :hint="t('admin.projects.empty_hint')" />
    </div>

    <div v-else class="chrome-bg-panel border chrome-border rounded-card overflow-hidden" style="box-shadow: var(--shadow-card);">
      <table class="w-full text-data">
        <thead>
          <tr class="text-left text-xs chrome-text-muted border-b chrome-border-subtle" style="background-color: rgb(var(--bg-panel-elevated) / 0.4);">
            <th class="font-medium py-2 px-3">{{ t('admin.projects.col_name') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.projects.col_owner') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('admin.projects.col_members') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('admin.projects.col_datasources') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('admin.projects.col_jobs') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.projects.col_created_at') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('admin.projects.col_actions') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in projects" :key="p.id" class="border-b chrome-border-subtle last:border-b-0 group transition-colors">
            <td class="py-2 px-3">
              <div class="font-medium chrome-text-heading">{{ p.name }}</div>
              <div class="text-xs chrome-text-muted mt-0.5">{{ p.description || t('admin.projects.no_description') }}</div>
            </td>
            <td class="py-2 px-3 chrome-text-normal">{{ usernameOf(p.owner_user_id) }}</td>
            <td class="py-2 px-3 text-right tabular-nums chrome-text-normal">{{ p.member_count }}</td>
            <td class="py-2 px-3 text-right tabular-nums chrome-text-normal">{{ p.datasource_count }}</td>
            <td class="py-2 px-3 text-right tabular-nums chrome-text-normal">{{ p.job_count }}</td>
            <td class="py-2 px-3 chrome-text-muted tabular-nums">{{ formatDate(p.created_at) }}</td>
            <td class="py-2 px-3">
              <div class="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button type="button" class="chrome-btn-ghost" :disabled="writesBlocked" :title="t('common.edit')" @click="openEdit(p)">
                  <Pencil class="w-3.5 h-3.5" />
                </button>
                <button type="button" class="chrome-btn-ghost hover:!text-red-500" :disabled="writesBlocked" :title="t('common.delete')" @click="openDelete(p)">
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新建 / 编辑 modal -->
    <Modal
      :open="modalOpen"
      :title="mode === 'create' ? t('admin.projects.create_title') : t('admin.projects.edit_title')"
      @close="modalOpen = false"
    >
      <form @submit.prevent="submit" class="space-y-4">
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.projects.field_name') }}</label>
          <input v-model="form.name" type="text" maxlength="64" class="chrome-input w-full" :disabled="saveMutation.isPending.value" />
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.projects.field_description') }}</label>
          <textarea v-model="form.description" rows="2" class="chrome-input w-full resize-none" :disabled="saveMutation.isPending.value" />
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.projects.field_owner') }}</label>
          <select v-model="form.owner_user_id" class="chrome-input w-full" :disabled="saveMutation.isPending.value">
            <option v-for="u in allUsers" :key="u.id" :value="u.id">{{ u.username }}</option>
          </select>
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('admin.projects.field_members') }}</label>
          <div v-if="mode === 'edit'" class="text-xs chrome-text-muted">
            {{ t('admin.projects.members_edit_hint') }}
          </div>
          <div class="max-h-40 overflow-y-auto border chrome-border rounded-input divide-y chrome-border-subtle">
            <label
              v-for="u in memberCandidates"
              :key="u.id"
              class="flex items-center gap-2 px-3 py-1.5 text-sm chrome-text-normal cursor-pointer hover:chrome-bg-elevated"
            >
              <input type="checkbox" :checked="form.members.includes(u.id)" @change="toggleMember(u.id)" />
              {{ u.username }}
              <span class="text-xs chrome-text-muted ml-auto">{{ u.role }}</span>
            </label>
            <div v-if="memberCandidates.length === 0" class="px-3 py-2 text-xs chrome-text-muted">
              {{ t('admin.projects.no_member_candidates') }}
            </div>
          </div>
        </div>
        <div v-if="formError" class="text-xs text-red-500">{{ formError }}</div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="chrome-btn-secondary" @click="modalOpen = false" :disabled="saveMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="submit" class="chrome-btn-primary" :disabled="saveMutation.isPending.value">
            <template v-if="saveMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ mode === 'create' ? t('admin.projects.create_submit') : t('common.save') }}</span>
          </button>
        </div>
      </form>
    </Modal>

    <!-- 删除 modal(级联影响)-->
    <Modal :open="deleteModal !== null" :title="t('admin.projects.delete_title')" :subtitle="deleteModal?.name" @close="deleteModal = null">
      <div class="space-y-4">
        <p class="text-sm chrome-text-normal">{{ t('admin.projects.delete_confirm') }}</p>
        <div v-if="impactLoading" class="flex items-center gap-2 text-sm chrome-text-muted"><LoadingDots /><span>{{ t('admin.projects.impact_loading') }}</span></div>
        <div
          v-else-if="impact"
          class="rounded-input border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2.5 text-xs text-amber-800 dark:text-amber-300"
        >
          {{ t('admin.projects.impact_summary', { datasources: impact.datasource_count, jobs: impact.job_count }) }}
        </div>
        <div v-if="deleteError" class="text-xs text-red-500">{{ deleteError }}</div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="chrome-btn-secondary" @click="deleteModal = null" :disabled="deleteMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="button" class="chrome-btn-primary chrome-btn-danger" @click="submitDelete" :disabled="deleteMutation.isPending.value || impactLoading">
            <template v-if="deleteMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ t('admin.projects.delete_confirm_button') }}</span>
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
