<template>
  <footer class="statusbar">
    <span class="statusbar__cell">
      <TermDot :status="apiStatus" />
      <span>{{ apiLabel }}</span>
    </span>
    <span class="statusbar__sep" aria-hidden="true">·</span>
    <span class="statusbar__cell">
      <span>{{ pageLabel }}</span>
    </span>

    <span class="statusbar__spacer" />

    <span class="statusbar__cell statusbar__cell--mute tnum">
      {{ now }}
    </span>
  </footer>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import TermDot from '../cli/TermDot.vue'
import client from '../../api/client'

const route = useRoute()
const authStore = useAuthStore()

const apiStatus = ref('idle')
const apiLatency = ref(null)
const apiLabel = computed(() => {
  if (apiStatus.value === 'ok') return `服務正常 · ${apiLatency.value} 毫秒`
  if (apiStatus.value === 'warn') return '服務較慢'
  if (apiStatus.value === 'danger') return '服務中斷'
  return '連線確認中'
})

const PAGE_LABEL = {
  '/keys': 'API 金鑰',
  '/models': '模型',
  '/usage': '用量',
  '/users': '使用者',
  '/audit': '稽核紀錄',
  '/developer/agents': '助手',
  '/forbidden': '這頁你看不到',
}
const pageLabel = computed(() => {
  if (route.path === '/') return authStore.isRegularUser ? '工作臺' : '總覽'
  return PAGE_LABEL[route.path] || route.meta?.title || route.path.replace(/^\//, '') || '總覽'
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

const now = ref('')
function refreshClock() {
  now.value = new Date().toLocaleTimeString('zh-TW', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZone: 'Asia/Taipei',
  })
}

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
.statusbar__sep {
  color: var(--c-border-strong);
}
.statusbar__spacer { flex: 1; }
</style>
