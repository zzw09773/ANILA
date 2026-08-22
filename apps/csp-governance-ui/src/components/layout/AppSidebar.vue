<template>
  <aside class="sidenav">
    <nav class="sidenav__nav" aria-label="主要選單">
      <template v-for="group in menuGroups" :key="group.label">
        <div v-if="group.items.length" class="sidenav__group">
          <div class="sidenav__group-label">{{ group.label }}</div>
          <ul class="sidenav__list">
            <li v-for="item in group.items" :key="item.path">
              <router-link
                :to="item.path"
                class="sidenav__item"
                :class="{ 'is-active': isActive(item.path) }"
              >
                <span class="sidenav__rail" aria-hidden="true" />
                <span class="sidenav__label">{{ item.label }}</span>
              </router-link>
            </li>
          </ul>
        </div>
      </template>
    </nav>

    <div class="sidenav__foot">
      <a class="sidenav__workbench" :href="workbenchHref">工作臺</a>
      <div class="sidenav__foot-row">
        <span>{{ authStore.user?.username || '未登入' }}</span>
        <span class="sidenav__foot-val">{{ roleLabel }}</span>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { shellWorkbenchHref } from '../../utils/appOrigins'

const route = useRoute()
const authStore = useAuthStore()
const workbenchHref = shellWorkbenchHref()

const ROLE_LABEL = {
  owner: '擁有者',
  admin: '管理員',
  developer: '開發者',
  user: '使用者',
}
const roleLabel = computed(() => ROLE_LABEL[authStore.user?.role] || '')

const menuGroups = computed(() => {
  if (authStore.isRegularUser) {
    return [{
      label: '工作臺',
      items: [{ path: '/', label: '工作臺' }],
    }]
  }

  const groups = [
    {
      label: '概況',
      items: [
        { path: '/', label: '總覽' },
        { path: '/keys', label: 'API 金鑰' },
        { path: '/models', label: '模型' },
        { path: '/usage', label: '用量' },
      ],
    },
  ]

  if (authStore.isDeveloper) {
    groups.push({
      label: '開發',
      items: [
        { path: '/developer/guide', label: '開發指南' },
        { path: '/developer/agents', label: '助手' },
        { path: '/knowledge-collections', label: '知識庫' },
        { path: '/message-actions', label: '自訂動作' },
      ],
    })
  }

  if (authStore.isAdmin) {
    groups.push({
      label: '身分',
      items: [
        { path: '/users', label: '使用者' },
        { path: '/departments', label: '部門' },
      ],
    })
    groups.push({
      label: '營運',
      items: [
        { path: '/alerts', label: '警報' },
        { path: '/banners', label: '公告橫幅' },
        { path: '/platform-settings', label: '平台設定' },
        { path: '/trusted-hosts', label: '信任主機' },
        { path: '/service-clients', label: '服務客戶端' },
      ],
    })
    groups.push({
      label: '合規',
      items: [
        { path: '/audit', label: '稽核紀錄' },
        { path: '/classification-inventory', label: '分類盤點' },
        { path: '/feedback', label: '使用者回饋' },
      ],
    })
    groups.push({
      label: '服務',
      items: [
        { path: '/platform-links', label: '平台連結' },
        { path: '/service-access', label: '服務存取' },
      ],
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
</script>

<style scoped>
.sidenav {
  background: var(--c-surface-1);
  border-right: var(--border-w) solid var(--c-border);
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100%;
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
  padding: 0 var(--gap-4);
  margin-bottom: var(--gap-2);
  font-size: var(--t-xs);
  font-weight: 600;
  color: var(--c-fg-3);
}

.sidenav__list { list-style: none; padding: 0; margin: 0; }

.sidenav__item {
  position: relative;
  display: flex;
  align-items: center;
  padding: 0 var(--gap-4);
  height: 32px;
  color: var(--c-fg-2);
  font-size: var(--t-sm);
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
  font-weight: 600;
}
.sidenav__rail {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: transparent;
}
.sidenav__item.is-active .sidenav__rail {
  background: var(--c-accent);
}

.sidenav__label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidenav__foot {
  border-top: var(--border-w) solid var(--c-border);
  padding: var(--gap-3) var(--gap-4);
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--c-surface-1);
}
.sidenav__workbench {
  color: var(--c-accent);
  font-size: var(--t-sm);
  font-weight: 600;
  text-decoration: none;
}
.sidenav__workbench:hover { text-decoration: underline; }
.sidenav__foot-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--gap-2);
  font-size: var(--t-xs);
  color: var(--c-fg-2);
}
.sidenav__foot-val { color: var(--c-fg-3); }
</style>
