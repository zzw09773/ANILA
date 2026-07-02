<template>
  <footer class="statusbar">
    <span class="statusbar__cell">
      <TermDot :status="apiStatus" />
      <span>api</span>
      <span class="statusbar__val">{{ apiLabel }}</span>
    </span>
    <span class="statusbar__sep">│</span>
    <span class="statusbar__cell">
      <span class="term-label">role</span>
      <span class="statusbar__val">{{ authStore.user?.role || 'guest' }}</span>
    </span>
    <span class="statusbar__sep">│</span>
    <span class="statusbar__cell">
      <span class="term-label">path</span>
      <span class="statusbar__val">{{ route.path }}</span>
    </span>

    <span class="statusbar__spacer" />

    <span class="statusbar__cell statusbar__cell--mute">
      <span>theme:</span>
      <span class="statusbar__val">{{ theme }}</span>
    </span>
    <span class="statusbar__sep">│</span>
    <span class="statusbar__cell statusbar__cell--mute">
      <span>build {{ buildId }}</span>
    </span>
    <span class="statusbar__sep">│</span>
    <span class="statusbar__cell statusbar__cell--mute tnum">
      {{ now }}
    </span>
  </footer>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useTheme } from '../../composables/useTheme'
import TermDot from '../cli/TermDot.vue'
import client from '../../api/client'

const route = useRoute()
const authStore = useAuthStore()
const { theme } = useTheme()

// Live API health probe — pings /health every 30s. Status: ok | warn | danger.
const apiStatus = ref('idle')
const apiLatency = ref(null)
const apiLabel = computed(() => {
  if (apiStatus.value === 'ok') return `online · ${apiLatency.value}ms`
  if (apiStatus.value === 'warn') return 'degraded'
  if (apiStatus.value === 'danger') return 'offline'
  return 'probing'
})

let pollHandle = null
async function probeApi() {
  const started = performance.now()
  try {
    await client.get('/health', { timeout: 4000 })
    const latency = Math.round(performance.now() - started)
    apiLatency.value = latency
    apiStatus.value = latency > 1500 ? 'warn' : 'ok'
  } catch {
    apiStatus.value = 'danger'
    apiLatency.value = null
  }
}

// Wall clock — UTC offset shown to anchor distributed-team review.
const now = ref('')
function refreshClock() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  now.value = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const buildId = computed(() => {
  // Vite injects timestamps via import.meta.env in production; in dev fall back
  // to a stable short hash of today's date so the bar isn't blank.
  const env = import.meta.env
  if (env?.VITE_BUILD_ID) return env.VITE_BUILD_ID
  return env?.MODE === 'development' ? 'dev' : 'snapshot'
})

let clockHandle = null
onMounted(() => {
  refreshClock()
  clockHandle = window.setInterval(refreshClock, 1000)
  probeApi()
  pollHandle = window.setInterval(probeApi, 30_000)
})
onUnmounted(() => {
  if (clockHandle) window.clearInterval(clockHandle)
  if (pollHandle) window.clearInterval(pollHandle)
})
</script>

<style scoped>
.statusbar {
  height: var(--shell-statusbar-h);
  display: flex;
  align-items: center;
  gap: var(--gap-3);
  padding: 0 var(--gap-3);
  background: var(--c-surface-2);
  border-top: var(--border-w) solid var(--c-border);
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  letter-spacing: 0.05em;
  white-space: nowrap;
  overflow-x: auto;
}
.statusbar::-webkit-scrollbar { display: none; }

.statusbar__cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.statusbar__cell--mute { color: var(--c-fg-mute); }
.statusbar__val {
  color: var(--c-fg-1);
  letter-spacing: 0.04em;
}
.statusbar__sep {
  color: var(--c-border-strong);
}
.statusbar__spacer { flex: 1; }
</style>
