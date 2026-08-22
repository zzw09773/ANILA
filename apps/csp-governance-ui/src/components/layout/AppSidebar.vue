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

const menuGroups = computed(() => {
  if (authStore.isRegularUser) {
    return [{
      label: '工作臺',
      items: [{ path: '/', label: '工作臺' }],
    }]
  }

  const groups = [
    {
      label: '人',
      items: [],
    },
    {
      label: '用量',
      items: [
        { path: '/', label: '總覽' },
        { path: '/usage', label: '用量' },
        { path: '/keys', label: 'API 金鑰' },
        { path: '/models', label: '模型' },
        { path: '/alerts', label: '警報' },
      ],
    },
    {
      label: '平台',
      items: [],
    },
  ]

  if (authStore.isAdmin) {
    groups[0].items.push(
      { path: '/users', label: '使用者' },
      { path: '/departments', label: '部門' },
      { path: '/feedback', label: '使用者回饋' },
    )
    groups[2].items.push(
      { path: '/banners', label: '公告橫幅' },
      { path: '/platform-settings', label: '平台設定' },
      { path: '/trusted-hosts', label: '信任主機' },
      { path: '/service-clients', label: '服務客戶端' },
      { path: '/audit', label: '稽核紀錄' },
      { path: '/classification-inventory', label: '分類盤點' },
      { path: '/platform-links', label: '平台連結' },
      { path: '/service-access', label: '服務存取' },
    )
  }

  if (authStore.isDeveloper) {
    groups[0].items.push(
      { path: '/developer/agents', label: '助手' },
    )
    groups[2].items.unshift(
      { path: '/developer/guide', label: '開發指南' },
      { path: '/knowledge-collections', label: '知識庫' },
      { path: '/message-actions', label: '自訂動作' },
    )
  }

  return groups.filter((group) => group.items.length)
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
  background: var(--paper);
  border-right: 1px solid var(--hairline);
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
  border-top: 1px solid var(--hairline);
}

.sidenav__group-label {
  padding: 0 var(--gap-4);
  margin-bottom: var(--gap-2);
  font-size: var(--t-xs);
  font-weight: 600;
  color: var(--mute);
}

.sidenav__list { list-style: none; padding: 0; margin: 0; }

.sidenav__item {
  position: relative;
  display: flex;
  align-items: center;
  padding: 0 var(--gap-4);
  height: 36px;
  color: var(--ink);
  font-size: var(--t-sm);
  text-decoration: none;
}
.sidenav__item:hover {
  color: var(--signal);
  background: var(--accent-soft);
  text-decoration: none;
}
.sidenav__item.is-active {
  color: var(--signal);
  background: var(--accent-soft);
  font-weight: 600;
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
  background: var(--signal);
}

.sidenav__label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidenav__foot {
  border-top: 1px solid var(--hairline);
  padding: var(--gap-3) var(--gap-4);
  background: var(--paper);
}
.sidenav__workbench {
  color: var(--signal);
  font-size: var(--t-sm);
  font-weight: 600;
  text-decoration: none;
}
.sidenav__workbench:hover { text-decoration: underline; }
</style>
