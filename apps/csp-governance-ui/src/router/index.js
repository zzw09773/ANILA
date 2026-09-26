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
        path: 'model-access-groups',
        name: 'ModelAccessGroups',
        component: () => import('../views/ModelAccessGroupsView.vue'),
        meta: { requiresAdmin: true },
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
        // 單位管理員＝人事與用量：可進使用者頁，頁內再收掉 admin-only 入口。
        meta: { requiresPersonnel: true },
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
        path: 'feedback',
        name: 'Feedback',
        component: () => import('../views/FeedbackView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'banners',
        name: 'Banners',
        component: () => import('../views/BannersView.vue'),
        meta: { requiresAdmin: true },
      },
      // 平台設定總覽 —— 96 顆設定四區三態。讀寫同一道 admin 門
      // (後端 router 級 Depends(require_admin))，所以這裡照 /users 的形狀。
      {
        path: 'platform-settings',
        name: 'PlatformSettings',
        component: () => import('../views/SettingsOverviewView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'external-services',
        name: 'ExternalServices',
        component: () => import('../views/ExternalServicesView.vue'),
        meta: { requiresAdmin: true },
      },
      {
        path: 'audit-logs',
        name: 'AuditLogs',
        component: () => import('../views/AuditLogsView.vue'),
        meta: { requiresAdmin: true },
      },
      // 分類盤點頁已下線（擁有者 2026-09-14：治理中心不需要此盤點）。舊網址改回儀表板。
      {
        path: 'classification-inventory',
        redirect: '/',
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
      // Sprint 8 X / Phase E — service_clients (Router / worker / admin tool)
      // service-token management. Admin-only.
      {
        path: 'service-clients',
        name: 'ServiceClients',
        component: () => import('../views/ServiceClientsView.vue'),
        meta: { requiresAdmin: true },
      },
      // Phase 2 模型 stack 解耦 — SSRF guard allow-list (DB-driven 取代 env)。
      // admin-tier 都看得到 (列表可見性 = 透明度),mutation 走 owner-only
      // (RequireOwner) — UI 端只給 owner 看 +/remove 按鈕,後端 enforce。
      {
        path: 'trusted-hosts',
        name: 'TrustedHosts',
        component: () => import('../views/TrustedHostsView.vue'),
        meta: { requiresAdmin: true },
      },
      // OW-3 — message-level custom actions authoring console.
      // Create = developer+; update/delete/bindings = author or admin-tier;
      // export = admin+ (redacted for non-owner). Route stays developer-tier.
      {
        path: 'message-actions',
        name: 'MessageActions',
        component: () => import('../views/MessageActionsView.vue'),
        meta: { requiresDeveloper: true },
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

  // Cookie 流程下，第一次進站只有 cookie，user 物件需先從 /me 取回。
  // 等 store 完成初始 fetchUser() 才能正確判斷 isAuthenticated。
  if (!authStore.initialized) {
    await authStore.fetchUser()
  }

  if (to.meta.requiresAuth !== false && !authStore.isAuthenticated) {
    next('/login')
  } else if (to.meta.requiresPersonnel && !authStore.isAdmin && !authStore.isUnitAdmin) {
    next('/')
  } else if (to.meta.requiresAdmin && !authStore.isAdmin) {
    // ``isAdmin`` is admin-OR-owner (tier check). Don't compare role
    // strings here — owner is admin's superset and must keep access.
    next('/')
  } else if (to.meta.requiresOwner && !authStore.isOwner) {
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
