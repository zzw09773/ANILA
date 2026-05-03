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
      {
        path: 'service-access',
        name: 'ServiceAccess',
        component: () => import('../views/ServiceAccessView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'developer/agents',
        name: 'DeveloperAgents',
        component: () => import('../views/DeveloperAgentsView.vue'),
        meta: { requiresDeveloper: true },
      },
      // v0.1 framework rollout — dedicated dev walkthrough page.
      // Linked from DeveloperAgentsView guide block.
      {
        path: 'developer/guide',
        name: 'DeveloperGuide',
        component: () => import('../views/DeveloperGuideView.vue'),
        meta: { requiresDeveloper: true },
      },
      // Sprint 13 PR C1 — per-agent runtime knobs (tool permissions /
      // workspace caps / guardrails). Owner of the agent OR admin.
      {
        path: 'developer/agents/:id/runtime-config',
        name: 'AgentRuntimeConfig',
        component: () => import('../views/AgentRuntimeConfigView.vue'),
        meta: { requiresDeveloper: true },
      },
      // Sprint 8 X / Phase E — service_clients (Router / worker / admin tool)
      // service-token management. Admin-only.
      {
        path: 'service-clients',
        name: 'ServiceClients',
        component: () => import('../views/ServiceClientsView.vue'),
        meta: { requiresAdmin: true },
      },
      // Phase 2 Sprint 2 / Chunk H — Knowledge Collections inspector.
      // Developer-tier (any user with UserAgentPermission, plus admins).
      {
        path: 'knowledge-collections',
        name: 'KnowledgeCollections',
        component: () => import('../views/KnowledgeCollectionsView.vue'),
        meta: { requiresDeveloper: true },
      },
      // Sprint 8 X / chunking-preview Phase 3 — interactive strategy
      // comparison wizard. Users land here from KnowledgeCollections
      // "+ compare strategies first" CTA.
      {
        path: 'knowledge-collections/preview',
        name: 'ChunkingPreview',
        component: () => import('../views/ChunkingPreviewView.vue'),
        meta: { requiresDeveloper: true },
      },
      {
        path: 'knowledge-collections/:id',
        name: 'CollectionDetail',
        component: () => import('../views/CollectionDetailView.vue'),
        meta: { requiresDeveloper: true },
      },
      {
        // Sprint 3 Chunk N — Chunking Evaluator wizard + results.
        path: 'knowledge-collections/:id/evaluator',
        name: 'Evaluator',
        component: () => import('../views/EvaluatorView.vue'),
        meta: { requiresDeveloper: true },
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

  // Cookie 流程下，第一次進站只有 cookie，user 物件需先從 /me 取回。
  // 等 store 完成初始 fetchUser() 才能正確判斷 isAuthenticated。
  if (!authStore.initialized) {
    await authStore.fetchUser()
  }

  if (to.meta.requiresAuth !== false && !authStore.isAuthenticated) {
    next('/login')
  } else if (to.meta.requiresAdmin && authStore.user?.role !== 'admin') {
    next('/')
  } else if (to.meta.requiresDeveloper && !authStore.isDeveloper) {
    next('/')
  } else if (to.path === '/login' && authStore.isAuthenticated) {
    next('/')
  } else {
    next()
  }
})

export default router
