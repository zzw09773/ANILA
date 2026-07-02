<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">control plane · analytics</p>
        <h1 class="page-head__title">usage</h1>
        <p class="page-head__sub">throughput · token spend · per model · per user</p>
      </div>
      <div class="page-head__actions">
        <TimeRangeSelector v-model="selectedRange" @update:model-value="refreshUsage" />
        <TermButton size="md" variant="default" @click="handleExport" label="export csv" />
      </div>
    </header>

    <!-- Filter bar ----------------------------------------------------- -->
    <TermBox title="filter" pad="sm" hint="server-evaluated">
      <div class="filters">
        <TermField label="type">
          <select v-model="selectedModelType" @change="onModelTypeChange" class="term-select">
            <option :value="null">all</option>
            <option value="llm">llm</option>
            <option value="vlm">vlm</option>
            <option value="embedding">embedding</option>
            <option value="agent">agent</option>
          </select>
        </TermField>
        <TermField v-if="authStore.isAdmin" label="department">
          <select v-model="selectedDepartment" @change="onDepartmentChange" class="term-select">
            <option :value="null">all</option>
            <option v-for="d in activeDepartments" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
        </TermField>
        <TermField label="model">
          <select v-model="selectedModel" @change="refreshUsage" class="term-select">
            <option :value="null">all</option>
            <option v-for="m in filteredModels" :key="m.id" :value="m.id">{{ m.display_name }}</option>
          </select>
        </TermField>
        <TermField v-if="authStore.isAdmin" label="user">
          <select v-model="selectedUser" @change="refreshUsage" class="term-select">
            <option :value="null">all</option>
            <option v-for="u in filteredUsers" :key="u.id" :value="u.id">{{ u.username }}</option>
          </select>
        </TermField>
        <TermField label="group by">
          <select v-model="groupBy" @change="refreshUsage" class="term-select">
            <option value="total">total</option>
            <option value="model">model</option>
            <option v-if="authStore.isAdmin" value="department">department</option>
            <option v-if="authStore.isAdmin" value="user">user</option>
          </select>
        </TermField>
      </div>
    </TermBox>

    <!-- Summary + chart ----------------------------------------------- -->
    <TermBox :title="`throughput · ${rangeLabel}`" pad="md" hint="lower-bound = first request in window">
      <div class="kpi-row">
        <TermStat :label="`${rangeLabel} · requests`" :value="usageStore.summary?.total_requests || 0" tone="accent" />
        <TermStat :label="`${rangeLabel} · tokens`" :value="usageStore.summary?.total_tokens || 0" />
        <TermStat :label="`${rangeLabel} · active keys`" :value="usageStore.summary?.active_api_keys || 0" />
      </div>
      <div class="chart-wrap">
        <UsageLineChart :chart-data="usageStore.chartData" :height="380" />
      </div>
    </TermBox>

    <!-- Top tables ----------------------------------------------------- -->
    <div class="tops" :class="{ 'tops--admin': authStore.isAdmin }">
      <TermBox title="top · models · 30d" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr><th>model</th><th style="width: 90px">type</th><th class="num" style="width: 110px">tokens</th><th class="num" style="width: 110px">requests</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in usageStore.topModels" :key="m.model_id">
              <td>{{ m.model_name }}</td>
              <td><TermBadge :tone="m.model_type">{{ m.model_type || '?' }}</TermBadge></td>
              <td class="num tnum">{{ formatNum(m.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(m.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topModels.length === 0">
              <td colspan="4"><TermEmpty message="no model usage yet" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox v-if="authStore.isAdmin" title="top · departments · 30d" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr><th>department</th><th class="num" style="width: 110px">tokens</th><th class="num" style="width: 110px">requests</th></tr>
          </thead>
          <tbody>
            <tr v-for="d in usageStore.topDepartments" :key="d.department_id ?? 'unassigned'">
              <td>{{ d.department_name }}</td>
              <td class="num tnum">{{ formatNum(d.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(d.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topDepartments.length === 0">
              <td colspan="3"><TermEmpty message="no department usage yet" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox v-if="authStore.isAdmin" title="top · users · 30d" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr><th>user</th><th class="num" style="width: 110px">tokens</th><th class="num" style="width: 110px">requests</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in usageStore.topUsers" :key="u.user_id">
              <td>{{ u.username }}</td>
              <td class="num tnum">{{ formatNum(u.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(u.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topUsers.length === 0">
              <td colspan="3"><TermEmpty message="no user usage yet" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <!-- Sprint 8 X / Phase G — caller attribution rollups (admin) -->
      <TermBox v-if="authStore.isAdmin" title="top · agents · 30d" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr>
              <th>agent</th>
              <th class="num" style="width: 110px">tokens</th>
              <th class="num" style="width: 110px">requests</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in topAgents" :key="a.agent_id">
              <td>
                <span class="cell-strong">{{ a.agent_name }}</span>
                <span v-if="a.base_model_id" class="cell-meta"> · base #{{ a.base_model_id }}</span>
              </td>
              <td class="num tnum">{{ formatNum(a.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(a.total_requests) }}</td>
            </tr>
            <tr v-if="topAgents.length === 0">
              <td colspan="3"><TermEmpty message="no caller-attributed agent usage yet (pre-Phase-G rows show as unattributed)" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox v-if="authStore.isAdmin" title="by · base model · 30d" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr>
              <th>base model</th>
              <th class="num" style="width: 110px">tokens</th>
              <th class="num" style="width: 110px">requests</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in byBaseModel" :key="m.base_model_id">
              <td>{{ m.base_model_name }}</td>
              <td class="num tnum">{{ formatNum(m.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(m.total_requests) }}</td>
            </tr>
            <tr v-if="byBaseModel.length === 0">
              <td colspan="3"><TermEmpty message="no agent → base-model attribution yet" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useUsageStore } from '../stores/usage'
import { useAuthStore } from '../stores/auth'
import { listDepartments } from '../api/departments'
import { listModels } from '../api/models'
import UsageLineChart from '../components/charts/UsageLineChart.vue'
import TimeRangeSelector from '../components/charts/TimeRangeSelector.vue'
import client from '../api/client'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermStat } from '../components/cli'

const usageStore = useUsageStore()
const authStore = useAuthStore()

// Sprint 8 X / Phase G — Phase G stores its rollups inline rather
// than in usageStore because they're admin-only and don't share
// filter dimensions with the rest of the view.
const topAgents = ref([])
const byBaseModel = ref([])

async function fetchPhaseGRollups() {
  if (!authStore.isAdmin) return
  try {
    const [{ data: a }, { data: b }] = await Promise.all([
      client.get('/api/usage/top-agents', { params: { days: 30, limit: 10 } }),
      client.get('/api/usage/by-base-model', { params: { days: 30 } }),
    ])
    topAgents.value = Array.isArray(a) ? a : []
    byBaseModel.value = Array.isArray(b) ? b : []
  } catch {
    topAgents.value = []
    byBaseModel.value = []
  }
}

const selectedRange = ref('24h')
const selectedModel = ref(null)
const selectedUser = ref(null)
const selectedDepartment = ref(null)
const selectedModelType = ref(null)
const groupBy = ref('total')
const models = ref([])
const users = ref([])
const departments = ref([])

const activeDepartments = computed(() => departments.value.filter(d => d.is_active))
const filteredModels = computed(() => {
  if (!selectedModelType.value) return models.value
  return models.value.filter(m => m.model_type === selectedModelType.value)
})
const filteredUsers = computed(() => {
  if (!selectedDepartment.value) return users.value
  return users.value.filter(u => u.department_id === selectedDepartment.value)
})

const rangeLabel = computed(() => ({
  '4h': '4h', '12h': '12h', '24h': '24h', '7d': '7d', '30d': '30d',
})[selectedRange.value] || selectedRange.value)

function buildUsageParams() {
  return {
    range: selectedRange.value,
    model_id: selectedModel.value || undefined,
    user_id: selectedUser.value || undefined,
    department_id: authStore.isAdmin ? (selectedDepartment.value || undefined) : undefined,
    model_type: selectedModelType.value || undefined,
    group_by: groupBy.value,
  }
}
function buildRankingParams() {
  return {
    model_type: selectedModelType.value || undefined,
    department_id: authStore.isAdmin ? (selectedDepartment.value || undefined) : undefined,
  }
}
async function refreshRankings() {
  const r = buildRankingParams()
  await usageStore.fetchTopModels(10, r)
  if (authStore.isAdmin) {
    await Promise.all([
      usageStore.fetchTopUsers(10, r),
      usageStore.fetchTopDepartments(10, r),
    ])
  }
}
async function refreshUsage() {
  const usageParams = buildUsageParams()
  const summaryParams = {
    range: selectedRange.value,
    model_id: selectedModel.value || undefined,
    user_id: selectedUser.value || undefined,
    model_type: selectedModelType.value || undefined,
    department_id: authStore.isAdmin ? (selectedDepartment.value || undefined) : undefined,
  }
  await Promise.all([
    usageStore.fetchChart(usageParams),
    usageStore.fetchSummary(summaryParams),
    refreshRankings(),
    fetchPhaseGRollups(),
  ])
}

onMounted(async () => {
  try { const { data } = await listModels(); models.value = data } catch {}
  if (authStore.isAdmin) {
    try {
      const [{ data: u }, { data: d }] = await Promise.all([client.get('/api/users'), listDepartments()])
      users.value = u
      departments.value = d
    } catch {}
  }
  await refreshUsage()
})

function onModelTypeChange() {
  if (selectedModel.value && !filteredModels.value.some(m => m.id === selectedModel.value)) {
    selectedModel.value = null
  }
  refreshUsage()
}
function onDepartmentChange() {
  if (selectedUser.value && !filteredUsers.value.some(u => u.id === selectedUser.value)) {
    selectedUser.value = null
  }
  if (groupBy.value === 'department' && selectedDepartment.value) groupBy.value = 'total'
  refreshUsage()
}
function handleExport() {
  usageStore.exportCsv({
    range: selectedRange.value,
    model_id: selectedModel.value || undefined,
    user_id: selectedUser.value || undefined,
    department_id: authStore.isAdmin ? (selectedDepartment.value || undefined) : undefined,
    model_type: selectedModelType.value || undefined,
  })
}
function formatNum(n) {
  if (!n) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toLocaleString()
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__actions { display: flex; align-items: center; gap: var(--gap-2); }

.filters {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--gap-3);
}
@media (max-width: 1100px) { .filters { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 700px)  { .filters { grid-template-columns: repeat(2, 1fr); } }

.kpi-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--gap-3); }
@media (max-width: 700px) { .kpi-row { grid-template-columns: 1fr; } }

.chart-wrap { margin-top: var(--gap-3); padding-top: var(--gap-3); border-top: var(--border-w) dashed var(--c-border); }

.tops { display: grid; grid-template-columns: 1fr; gap: var(--gap-3); }
.tops--admin { grid-template-columns: repeat(3, minmax(0, 1fr)); }
@media (max-width: 1100px) { .tops--admin { grid-template-columns: 1fr; } }
</style>
