<template>
  <aside
    id="gov-sidenav"
    class="sidenav"
    :class="{ 'is-open': open }"
    :aria-hidden="narrow && !open"
  >
    <nav class="sidenav__nav" aria-label="主要導覽">
      <template v-for="group in menuGroups" :key="group.label">
        <div v-if="group.items.length" class="sidenav__group">
          <div class="sidenav__group-label">
            <span>{{ group.label }}</span>
          </div>
          <ul class="sidenav__list">
            <li v-for="item in group.items" :key="item.path">
              <router-link
                :to="item.path"
                class="sidenav__item"
                :class="{ 'is-active': isActive(item.path) }"
                @click="nav.close()"
              >
                <span class="sidenav__rail" aria-hidden="true" />
                <span class="sidenav__label">{{ item.label }}</span>
                <span v-if="item.badge" class="sidenav__badge">{{ item.badge }}</span>
              </router-link>
            </li>
          </ul>
        </div>
      </template>
    </nav>

    <div class="sidenav__foot">
      <div class="sidenav__foot-row">
        <span class="term-label">登入身分</span>
        <span class="sidenav__foot-val">
          {{ authStore.user?.username || '訪客' }}
        </span>
      </div>
      <div class="sidenav__foot-row sidenav__foot-row--mute">
        <span class="term-label">權限範圍</span>
        <span class="sidenav__foot-val sidenav__foot-val--mute">{{ scopeLabel }}</span>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useShellNav } from '../../composables/useShellNav.js'

const route = useRoute()
const authStore = useAuthStore()
const nav = useShellNav()
const { open, narrow } = nav

const menuGroups = computed(() => {
  const groups = [
    {
      label: '主要',
      items: [
        { path: '/', label: '儀表板' },
        { path: '/api-keys', label: 'API 金鑰' },
        { path: '/models', label: '模型' },
        { path: '/usage', label: '用量' },
      ],
    },
  ]

  if (authStore.isDeveloper) {
    groups.push({
      label: '開發者',
      items: [
        { path: '/developer/guide', label: '開發指南' },
        { path: '/developer/agents', label: 'Agent' },
        { path: '/knowledge-collections', label: '知識庫' },
        { path: '/message-actions', label: '自訂動作' },
      ],
    })
  }

  if (authStore.isUnitAdmin && !authStore.isAdmin) {
    groups.push({
      label: '人員與單位',
      items: [
        { path: '/users', label: '使用者' },
      ],
    })
  }

  if (authStore.isAdmin) {
    groups.push({
      label: '人員與單位',
      items: [
        { path: '/users', label: '使用者' },
        { path: '/departments', label: '部門' },
        { path: '/model-access-groups', label: '群組' },
      ],
    })
    const adminItems = [
      { path: '/alerts', label: '警報' },
      { path: '/feedback', label: '使用者回饋' },
      { path: '/banners', label: '公告橫幅' },
      { path: '/audit-logs', label: '稽核紀錄' },
      { path: '/platform-links', label: '平台連結' },
      { path: '/service-access', label: '服務存取' },
      { path: '/service-clients', label: '服務客戶端' },
      { path: '/trusted-hosts', label: '信任主機' },
      { path: '/external-services', label: '外部服務' },
      { path: '/platform-settings', label: '平台設定' },
    ]
    groups.push({
      label: '管理',
      items: adminItems,
    })
  }

  return groups
})

function isActive(path) {
  if (path === '/') return route.path === '/'
  if (path === '/knowledge-collections') {
    return route.path.startsWith('/knowledge-collections')
  }
  return route.path === path
}

const scopeLabel = computed(() => {
  if (authStore.isAdmin) return '全部功能'
  if (authStore.isUnitAdmin) return '人事與用量'
  if (authStore.isDeveloper) return 'Agent 與知識庫'
  return '個人金鑰與用量'
})
</script>

<style scoped>
.sidenav {
  background: var(--c-surface-1);
  border-right: var(--border-w) solid var(--c-border);
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: auto;
  overflow: hidden;
}

.sidenav__nav {
  flex: 1;
  overflow-y: auto;
  padding: var(--gap-3) 0 var(--gap-4);
}

.sidenav__group + .sidenav__group {
  margin-top: var(--gap-4);
  padding-top: var(--gap-3);
  border-top: var(--border-w) solid var(--c-border);
}

.sidenav__group-label {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: 0 var(--gap-4);
  margin-bottom: var(--gap-2);
  font-size: var(--t-xs);
  font-weight: 600;
  color: var(--c-fg-3);
}

.sidenav__list { list-style: none; padding: 0; margin: 0; }

.sidenav__item {
  position: relative;
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: var(--gap-2);
  padding: 0 var(--gap-5);
  height: 36px;
  color: var(--c-fg-2);
  font-size: var(--t-base);
  text-decoration: none;
  transition: color var(--motion-fast), background-color var(--motion-fast);
}
.sidenav__item:hover {
  color: var(--c-fg-1);
  background: var(--c-surface-2);
  text-decoration: none;
}
.sidenav__item.is-active {
  color: var(--c-accent-strong);
  background: var(--c-accent-soft);
}
.sidenav__rail {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: transparent;
}
.sidenav__item.is-active .sidenav__rail {
  background: var(--c-accent);
}
.sidenav__item.is-active .sidenav__label {
  font-weight: 600;
}
.sidenav__label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidenav__badge {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  letter-spacing: 0.05em;
}

.sidenav__foot {
  flex-shrink: 0;
  border-top: var(--border-w) solid var(--c-border);
  padding: var(--gap-3) var(--gap-4);
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--c-surface-1);
}
.sidenav__foot-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: var(--t-xs);
}
.sidenav__foot-val {
  color: var(--c-fg-1);
  letter-spacing: 0.02em;
}
.sidenav__foot-val--mute { color: var(--c-fg-3); font-size: var(--t-2xs); }
</style>
