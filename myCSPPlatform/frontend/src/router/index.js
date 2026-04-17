import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/',
    component: () => import('../components/layout/AppLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        name: 'Dashboard',
        component: () => import('../views/DashboardView.vue'),
      },
      {
        path: 'api-keys',
        name: 'ApiKeys',
        component: () => import('../views/ApiKeysView.vue'),
      },
      {
        path: 'models',
        name: 'Models',
        component: () => import('../views/ModelsView.vue'),
      },
      {
        path: 'usage',
        name: 'Usage',
        component: () => import('../views/UsageView.vue'),
      },
      {
        path: 'users',
        name: 'Users',
        component: () => import('../views/UsersView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'departments',
        name: 'Departments',
        component: () => import('../views/DepartmentsView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'alerts',
        name: 'Alerts',
        component: () => import('../views/AlertsView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'audit-logs',
        name: 'AuditLogs',
        component: () => import('../views/AuditLogsView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'auth-providers',
        name: 'AuthProviders',
        component: () => import('../views/AuthProvidersView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'platform-links',
        name: 'PlatformLinks',
        component: () => import('../views/PlatformLinksView.vue'),
        meta: { requiresAdmin: true },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  const authStore = useAuthStore()

  if (to.meta.requiresAuth !== false && !authStore.isAuthenticated) {
    next('/login')
  } else if (to.meta.requiresAdmin && authStore.user?.role !== 'admin') {
    next('/')
  } else if (to.path === '/login' && authStore.isAuthenticated) {
    next('/')
  } else {
    next()
  }
})

export default router
