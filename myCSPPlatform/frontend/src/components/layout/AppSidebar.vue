<template>
  <aside class="min-h-screen w-64 border-r border-gray-800 bg-gray-950 text-white flex flex-col">
    <div class="border-b border-gray-800 p-6">
      <div class="text-xs uppercase tracking-[0.16em] text-gray-500">Control Plane</div>
      <h1 class="mt-2 text-xl font-bold">CSP Platform</h1>
      <p class="mt-1 text-sm text-gray-400">AI 模型、Agent、權限與審計治理</p>
    </div>

    <nav class="flex-1 space-y-1 p-4">
      <router-link
        v-for="item in menuItems"
        :key="item.path"
        :to="item.path"
        class="flex items-center px-4 py-3 rounded-lg text-sm transition-colors"
        :class="$route.path === item.path
          ? 'bg-indigo-600 text-white'
          : 'text-gray-300 hover:bg-gray-800 hover:text-white'"
      >
        <span class="mr-3 text-lg" v-html="item.icon"></span>
        {{ item.label }}
      </router-link>
    </nav>

    <div class="border-t border-gray-800 p-4">
      <div class="text-xs uppercase tracking-[0.16em] text-gray-500">Current Session</div>
      <div class="mt-2 text-sm text-gray-300">
        {{ authStore.user?.username }}
        <span class="ml-1 rounded px-2 py-0.5 text-xs"
            :class="{
              'bg-purple-600': authStore.user?.role === 'admin',
              'bg-indigo-500': authStore.user?.role === 'developer',
              'bg-gray-700': authStore.user?.role === 'user',
            }">
          {{ authStore.user?.role === 'admin' ? '管理員' : authStore.user?.role === 'developer' ? '開發者' : '使用者' }}
        </span>
      </div>
      <div class="mt-2 text-xs text-gray-500">
        {{ authStore.isAdmin ? '完整治理權限' : authStore.isDeveloper ? '可管理 Agent 與模板' : '僅可存取個人資源' }}
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()

const menuItems = computed(() => {
  const items = [
    { path: '/', label: '儀表板', icon: '&#9633;' },
    { path: '/api-keys', label: 'API Key 管理', icon: '&#128273;' },
    { path: '/models', label: '模型管理', icon: '&#9881;' },
    { path: '/usage', label: '用量分析', icon: '&#128200;' },
  ]
  if (authStore.isDeveloper) {
    items.push({ path: '/developer/agents', label: 'Agent 管理', icon: '&#129302;' })
    items.push({ path: '/knowledge-collections', label: 'Knowledge Collections', icon: '&#128218;' })
  }
  if (authStore.isAdmin) {
    items.push({ path: '/users', label: '使用者管理', icon: '&#128101;' })
    items.push({ path: '/departments', label: '部門設定', icon: '&#127970;' })
    items.push({ path: '/alerts', label: '告警中心', icon: '&#9888;' })
    items.push({ path: '/audit-logs', label: '審計日誌', icon: '&#128221;' })
    items.push({ path: '/auth-providers', label: 'SSO / LDAP / OIDC', icon: '&#128274;' })
    items.push({ path: '/platform-links', label: '平台連結設定', icon: '&#128279;' })
    items.push({ path: '/service-access', label: '服務存取權限', icon: '&#128737;' })
  }
  return items
})
</script>
