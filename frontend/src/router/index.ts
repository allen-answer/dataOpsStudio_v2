import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const LoginView = () => import('../views/LoginView.vue')
const AppShellLayout = () => import('../views/AppShellLayout.vue')
const ProjectsView = () => import('../views/ProjectsView.vue')
const PlaceholderView = () => import('../views/PlaceholderView.vue')
const DesignSystemPreview = () => import('../views/DesignSystemPreview.vue')

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
    path: '/',
    component: AppShellLayout,
    children: [
      { path: '', name: 'home', redirect: { name: 'projects' } },
      { path: 'projects', name: 'projects', component: ProjectsView },
      {
        path: 'projects/:id/datasources',
        name: 'datasources',
        component: PlaceholderView,
        props: { section: 'datasources' },
      },
      {
        path: 'projects/:id/sql',
        name: 'sql',
        component: PlaceholderView,
        props: { section: 'sql' },
      },
      {
        path: 'projects/:id/jobs',
        name: 'jobs',
        component: PlaceholderView,
        props: { section: 'jobs' },
      },
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
