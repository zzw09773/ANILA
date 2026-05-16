<template>
  <aside class="sidenav">
    <nav class="sidenav__nav" aria-label="primary">
      <template v-for="(group, gIdx) in menuGroups" :key="group.label">
        <div v-if="group.items.length" class="sidenav__group">
          <div class="sidenav__group-label">
            <span class="sidenav__group-glyph">{{ String(gIdx + 1).padStart(2, '0') }}</span>
            <span>{{ group.label }}</span>
            <span class="sidenav__group-count">{{ group.items.length }}</span>
          </div>
          <ul class="sidenav__list">
            <li v-for="(item, iIdx) in group.items" :key="item.path">
              <router-link
                :to="item.path"
                class="sidenav__item"
                :class="{ 'is-active': isActive(item.path) }"
              >
                <span class="sidenav__rail" aria-hidden="true" />
                <span class="sidenav__index">{{ String(iIdx + 1).padStart(2, '0') }}</span>
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
        <span class="term-label">session</span>
        <span class="sidenav__foot-val">
          {{ authStore.user?.username || 'guest' }}
        </span>
      </div>
      <div class="sidenav__foot-row sidenav__foot-row--mute">
        <span class="term-label">scope</span>
        <span class="sidenav__foot-val sidenav__foot-val--mute">{{ scopeLabel }}</span>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'

const route = useRoute()
const authStore = useAuthStore()

const menuGroups = computed(() => {
  const groups = [
    {
      label: 'primary',
      items: [
        { path: '/', label: 'dashboard' },
        { path: '/api-keys', label: 'api-keys' },
        { path: '/models', label: 'models' },
        { path: '/usage', label: 'usage' },
      ],
    },
  ]

  if (authStore.isDeveloper) {
    groups.push({
      label: 'developer',
      items: [
        { path: '/developer/guide', label: 'guide' },
        { path: '/developer/agents', label: 'agents' },
        { path: '/knowledge-collections', label: 'collections' },
      ],
    })
  }

  if (authStore.isAdmin) {
    groups.push({
      label: 'admin',
      items: [
        { path: '/users', label: 'users' },
        { path: '/departments', label: 'departments' },
        { path: '/alerts', label: 'alerts' },
        { path: '/audit-logs', label: 'audit-log' },
        { path: '/platform-links', label: 'platform-links' },
        { path: '/service-access', label: 'service-access' },
        { path: '/service-clients', label: 'service-clients' },
        { path: '/trusted-hosts', label: 'trusted-hosts' },
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

const scopeLabel = computed(() => {
  if (authStore.isAdmin) return 'full · governance'
  if (authStore.isDeveloper) return 'agents · collections'
  return 'self · keys · usage'
})
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
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: 0 var(--gap-4);
  margin-bottom: var(--gap-2);
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
  color: var(--c-fg-3);
}
.sidenav__group-glyph {
  color: var(--c-fg-mute);
  font-weight: 500;
}
.sidenav__group-count {
  margin-left: auto;
  color: var(--c-fg-mute);
  font-size: var(--t-2xs);
  letter-spacing: 0;
}

.sidenav__list { list-style: none; padding: 0; margin: 0; }

.sidenav__item {
  position: relative;
  display: grid;
  grid-template-columns: 16px 1fr auto;
  align-items: center;
  gap: var(--gap-2);
  padding: 0 var(--gap-4);
  height: 26px;
  color: var(--c-fg-2);
  font-size: var(--t-sm);
  text-decoration: none;
  letter-spacing: 0.02em;
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
  width: 2px;
  background: transparent;
}
.sidenav__item.is-active .sidenav__rail {
  background: var(--c-accent);
}

.sidenav__index {
  color: var(--c-fg-mute);
  font-size: var(--t-2xs);
  letter-spacing: 0;
  font-variant-numeric: tabular-nums;
}
.sidenav__item.is-active .sidenav__index {
  color: var(--c-accent);
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
