import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: '概览' },
        },
        {
          path: 'databases',
          name: 'databases',
          component: () => import('@/views/DatabasesView.vue'),
          meta: { title: '数据库管理' },
        },
        {
          path: 'sync',
          name: 'sync',
          component: () => import('@/views/SyncView.vue'),
          meta: { title: '同步管理' },
        },
        {
          path: 'backups',
          name: 'backups',
          component: () => import('@/views/BackupsView.vue'),
          meta: { title: '备份管理' },
        },
        {
          path: 'logs',
          name: 'logs',
          component: () => import('@/views/LogsView.vue'),
          meta: { title: '日志管理' },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: { title: '设置' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

let bootstrapped = false

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const ui = useUiStore()

  // 首次进入时校验一次本地令牌，之后沿用内存状态
  if (!bootstrapped) {
    bootstrapped = true
    if (auth.token && !auth.checked) {
      await auth.check()
    }
  }

  // 会话已失效：禁止进入任何受保护页面。
  // 旧实现只检查 auth.isAuthenticated（token 是否存在），
  // 而 401 处理器清掉存储后内存里的 token 仍然存在，
  // 于是用户会停留在失效页面上反复操作而不知道发生了什么。
  if (ui.sessionExpired && !to.meta.public) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (to.meta.public) {
    if (auth.isAuthenticated && auth.checked && !ui.sessionExpired) {
      return { name: 'dashboard' }
    }
    return true
  }

  if (!auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string) || ''
  document.title = title ? `${title} · DBSync` : 'DBSync'
})
