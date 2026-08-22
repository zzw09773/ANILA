import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { postLoginDestination } from '../utils/postLoginDestination'

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
        name: 'Home',
        component: () => import('../views/HomeGateView.vue'),
      },
      {
        path: 'keys',
        name: 'ApiKeys',
        component: () => import('../views/ApiKeysView.vue'),
      },
      {
        // Compatibility only. The actual page is /keys so it cannot collide
        // with an unprefixed fixture/API path.
        path: 'api-keys',
        redirect: { name: 'ApiKeys' },
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
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'departments',
        name: 'Departments',
        component: () => import('../views/DepartmentsView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'alerts',
        name: 'Alerts',
        component: () => import('../views/AlertsView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'feedback',
        name: 'Feedback',
        component: () => import('../views/FeedbackView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'banners',
        name: 'Banners',
        component: () => import('../views/BannersView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'platform-settings',
        name: 'PlatformSettings',
        component: () => import('../views/SettingsOverviewView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'audit',
        name: 'AuditLogs',
        component: () => import('../views/AuditLogsView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'audit-logs',
        redirect: { name: 'AuditLogs' },
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'classification-inventory',
        name: 'ClassificationInventory',
        component: () => import('../views/ClassificationInventoryView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'platform-links',
        name: 'PlatformLinks',
        component: () => import('../views/PlatformLinksView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'service-access',
        name: 'ServiceAccess',
        component: () => import('../views/ServiceAccessView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'developer/agents',
        name: 'DeveloperAgents',
        component: () => import('../views/DeveloperAgentsView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'developer/guide',
        name: 'DeveloperGuide',
        component: () => import('../views/DeveloperGuideView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'service-clients',
        name: 'ServiceClients',
        component: () => import('../views/ServiceClientsView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'trusted-hosts',
        name: 'TrustedHosts',
        component: () => import('../views/TrustedHostsView.vue'),
        meta: { requiresAdmin: true, requiredRole: 'admin' },
      },
      {
        path: 'message-actions',
        name: 'MessageActions',
        component: () => import('../views/MessageActionsView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'knowledge-collections',
        name: 'KnowledgeCollections',
        component: () => import('../views/KnowledgeCollectionsView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'knowledge-collections/preview',
        name: 'ChunkingPreview',
        component: () => import('../views/ChunkingPreviewView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'knowledge-collections/:id',
        name: 'CollectionDetail',
        component: () => import('../views/CollectionDetailView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'knowledge-collections/:id/evaluator',
        name: 'Evaluator',
        component: () => import('../views/EvaluatorView.vue'),
        meta: { requiresDeveloper: true, requiredRole: 'developer' },
      },
      {
        path: 'forbidden',
        name: 'Forbidden',
        component: () => import('../views/ForbiddenView.vue'),
      },
      {
        path: ':pathMatch(.*)*',
        name: 'NotFound',
        component: () => import('../views/NotFoundView.vue'),
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  if (!authStore.initialized) {
    await authStore.fetchUser()
  }

  if (to.meta.requiresAuth !== false && !authStore.isAuthenticated) {
    const nextHref = typeof window !== 'undefined'
      ? `${window.location.origin}${to.fullPath}`
      : to.fullPath
    next({ path: '/login', query: { next: nextHref } })
    return
  }

  if (to.path === '/login' && authStore.isAuthenticated) {
    // Hard navigation — dest may be another SPA on the same host.
    window.location.replace(postLoginDestination(authStore.user, to.query.next))
    next(false)
    return
  }

  if (to.meta.requiresAdmin && !authStore.isAdmin) {
    next({
      name: 'Forbidden',
      query: { required: to.meta.requiredRole || 'admin', from: to.fullPath },
      replace: true,
    })
    return
  }
  if (to.meta.requiresOwner && !authStore.isOwner) {
    next({
      name: 'Forbidden',
      query: { required: 'owner', from: to.fullPath },
      replace: true,
    })
    return
  }
  if (to.meta.requiresDeveloper && !authStore.isDeveloper) {
    next({
      name: 'Forbidden',
      query: { required: to.meta.requiredRole || 'developer', from: to.fullPath },
      replace: true,
    })
    return
  }

  next()
})

export default router
