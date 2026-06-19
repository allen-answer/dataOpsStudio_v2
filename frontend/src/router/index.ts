import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const LoginView = () => import('../views/LoginView.vue')
const AppShellLayout = () => import('../views/AppShellLayout.vue')
const ProjectsView = () => import('../views/ProjectsView.vue')
const DatasourcesView = () => import('../views/DatasourcesView.vue')
const PlaceholderView = () => import('../views/PlaceholderView.vue')
const DesignSystemPreview = () => import('../views/DesignSystemPreview.vue')
const TokensView = () => import('../views/TokensView.vue')
const SqlWorkspaceView = () => import('../views/SqlWorkspaceView.vue')
const CompareView = () => import('../views/CompareView.vue')
const JobsView = () => import('../views/JobsView.vue')
const AccountSecurityView = () => import('../views/AccountSecurityView.vue')
const AdminLayout = () => import('../views/AdminLayout.vue')
const AdminUsersView = () => import('../views/AdminUsersView.vue')
const AdminProjectsView = () => import('../views/AdminProjectsView.vue')
const AdminAuditLogsView = () => import('../views/AdminAuditLogsView.vue')
const AdminLicenseView = () => import('../views/AdminLicenseView.vue')
const AdminAiConfigView = () => import('../views/AdminAiConfigView.vue')

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { public: true },
  },
  {
    path: '/__design__',
    name: 'design-system',
    component: DesignSystemPreview,
    meta: { public: true }, // 内部参考页,不强制登录
  },
  {
    path: '/__tokens__',
    name: 'tokens',
    component: TokensView,
    meta: { public: true }, // Figma Make TokensView 镜像;design reference
  },
  {
    path: '/',
    component: AppShellLayout,
    children: [
      { path: '', name: 'home', redirect: { name: 'projects' } },
      { path: 'projects', name: 'projects', component: ProjectsView },
      {
        path: 'projects/:id/datasources',
        name: 'datasources',
        component: DatasourcesView,
      },
      {
        path: 'projects/:id/sql',
        name: 'sql',
        component: SqlWorkspaceView,
      },
      {
        path: 'projects/:id/compare',
        name: 'compare',
        component: CompareView,
      },
      {
        path: 'projects/:id/jobs',
        name: 'jobs',
        component: JobsView,
      },
      {
        path: 'account/security',
        name: 'account-security',
        component: AccountSecurityView,
      },
    ],
  },
  {
    // admin 后台 —— 独立外壳(深色顶部条 + admin 导航),全部 meta.admin=true。
    // 守卫双层:guards.ts 拦非 admin;AdminLayout 再做 403 兜底呈现。
    path: '/admin',
    component: AdminLayout,
    meta: { admin: true },
    children: [
      { path: '', name: 'admin-home', redirect: { name: 'admin-users' } },
      { path: 'users', name: 'admin-users', component: AdminUsersView },
      { path: 'projects', name: 'admin-projects', component: AdminProjectsView },
      { path: 'license', name: 'admin-license', component: AdminLicenseView },
      { path: 'audit-logs', name: 'admin-audit', component: AdminAuditLogsView },
      { path: 'ai-config', name: 'admin-ai-config', component: AdminAiConfigView },
    ],
  },
  {
    path: '/:catchAll(.*)',
    redirect: { name: 'home' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
