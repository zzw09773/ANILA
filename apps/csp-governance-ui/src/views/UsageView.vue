<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">用量</h1>
        <p class="page-head__sub">吞吐 · Token 用量 · 依模型 · 依使用者</p>
      </div>
      <div class="page-head__actions">
        <TimeRangeSelector v-model="selectedRange" @update:model-value="refreshUsage" />
        <TermButton size="md" variant="default" @click="handleExport" label="匯出 CSV" />
      </div>
    </header>

    <!-- Filter bar ----------------------------------------------------- -->
    <TermBox title="篩選" pad="sm" hint="伺服器端計算">
      <div class="filters">
        <TermField label="類型">
          <select v-model="selectedModelType" @change="onModelTypeChange" class="term-select">
            <option :value="null">全部</option>
            <option value="llm">llm</option>
            <option value="vlm">vlm</option>
            <option value="embedding">embedding</option>
            <option value="agent">agent</option>
          </select>
        </TermField>
        <TermField v-if="authStore.isAdmin" label="部門">
          <select v-model="selectedDepartment" @change="onDepartmentChange" class="term-select">
            <option :value="null">全部</option>
            <option v-for="d in activeDepartments" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
        </TermField>
        <TermField label="模型">
          <select v-model="selectedModel" @change="refreshUsage" class="term-select">
            <option :value="null">全部</option>
            <option v-for="m in filteredModels" :key="m.id" :value="m.id">{{ m.display_name }}</option>
          </select>
        </TermField>
        <TermField v-if="authStore.isAdmin" label="使用者">
          <select v-model="selectedUser" @change="refreshUsage" class="term-select">
            <option :value="null">全部</option>
            <option v-for="u in filteredUsers" :key="u.id" :value="u.id">{{ u.username }}</option>
          </select>
        </TermField>
        <TermField label="分組依據">
          <select v-model="groupBy" @change="refreshUsage" class="term-select">
            <option value="total">總計</option>
            <option value="model">模型</option>
            <option v-if="authStore.isAdmin" value="department">部門</option>
            <option v-if="authStore.isAdmin" value="user">使用者</option>
          </select>
        </TermField>
      </div>
    </TermBox>

    <!-- Summary + chart ----------------------------------------------- -->
    <TermBox :title="`吞吐 · ${rangeLabel}`" pad="md" hint="下界＝視窗內第一筆請求">
      <div class="kpi-row">
        <TermStat :label="`${rangeLabel} · 請求數`" :value="usageStore.summary?.total_requests || 0" tone="accent" />
        <TermStat :label="`${rangeLabel} · Token`" :value="usageStore.summary?.total_tokens || 0" />
        <TermStat :label="`${rangeLabel} · 使用中金鑰`" :value="usageStore.summary?.active_api_keys || 0" />
      </div>
      <div class="chart-wrap">
        <UsageLineChart :chart-data="usageStore.chartData" :height="380" />
      </div>
    </TermBox>

    <!-- Top tables ----------------------------------------------------- -->
    <div class="tops" :class="{ 'tops--admin': authStore.isAdmin }">
      <TermBox :title="`熱門 · 模型 · ${rangeLabel}`" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr><th>模型</th><th style="width: 90px">類型</th><th class="num" style="width: 110px">Token</th><th class="num" style="width: 110px">請求數</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in usageStore.topModels" :key="m.model_id">
              <td>{{ m.model_name }}</td>
              <td><TermBadge :tone="m.model_type">{{ m.model_type || '?' }}</TermBadge></td>
              <td class="num tnum">{{ formatNum(m.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(m.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topModels.length === 0">
              <td colspan="4"><TermEmpty message="尚無模型用量" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox v-if="authStore.isAdmin" :title="`熱門 · 部門 · ${rangeLabel}`" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr><th>部門</th><th class="num" style="width: 110px">Token</th><th class="num" style="width: 110px">請求數</th></tr>
          </thead>
          <tbody>
            <tr v-for="d in usageStore.topDepartments" :key="d.department_id ?? 'unassigned'">
              <td>{{ d.department_name }}</td>
              <td class="num tnum">{{ formatNum(d.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(d.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topDepartments.length === 0">
              <td colspan="3"><TermEmpty message="尚無部門用量" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox v-if="authStore.isAdmin" :title="`熱門 · 使用者 · ${rangeLabel}`" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr><th>使用者</th><th class="num" style="width: 110px">Token</th><th class="num" style="width: 110px">請求數</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in usageStore.topUsers" :key="u.user_id">
              <td>{{ u.username }}</td>
              <td class="num tnum">{{ formatNum(u.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(u.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topUsers.length === 0">
              <td colspan="3"><TermEmpty message="尚無使用者用量" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <!-- Sprint 8 X / Phase G — caller attribution rollups (admin) -->
      <TermBox v-if="authStore.isAdmin" :title="`熱門 · Agent · ${phaseGRangeLabel}`" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th class="num" style="width: 110px">Token</th>
              <th class="num" style="width: 110px">請求數</th>
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
              <td colspan="3"><TermEmpty message="尚無歸屬呼叫端的 Agent 用量（Phase G 前的資料顯示為未歸屬）" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox v-if="authStore.isAdmin" :title="`依 · 基礎模型 · ${phaseGRangeLabel}`" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr>
              <th>基礎模型</th>
              <th class="num" style="width: 110px">Token</th>
              <th class="num" style="width: 110px">請求數</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in byBaseModel" :key="m.base_model_id">
              <td>{{ m.base_model_name }}</td>
              <td class="num tnum">{{ formatNum(m.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(m.total_requests) }}</td>
            </tr>
            <tr v-if="byBaseModel.length === 0">
              <td colspan="3"><TermEmpty message="尚無 Agent → 基礎模型的歸屬資料" /></td>
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

// 視窗 → 天數。Phase G 端點（top-agents／by-base-model）吃 days 不是 range 參數；
// days=1 是 4h/12h/24h 的最小可表示天數（endpoint ge=1）。
function rangeToDays(range) {
  return ({ '4h': 1, '12h': 1, '24h': 1, '7d': 7, '30d': 30 })[range] || 30
}

// G-2：Phase G 兩個面板的**標題窗必須等於資料窗**。那兩支端點吃 days，
// 4h/12h 都會被夾成 24 小時——標題不能繼續寫「4h」，否則標題與資料不等寬。
// 誠實標明最小窗是 24h（後端補 hours 參數是更長的路，見 R2-2 裁定）。
const phaseGRangeLabel = computed(() => ({
  '4h': '24h', '12h': '24h', '24h': '24h', '7d': '7d', '30d': '30d',
})[selectedRange.value] || selectedRange.value)

async function fetchPhaseGRollups() {
  if (!authStore.isAdmin) return
  const days = rangeToDays(selectedRange.value)
  try {
    const [{ data: a }, { data: b }] = await Promise.all([
      client.get('/api/usage/top-agents', { params: { days, limit: 10 } }),
      client.get('/api/usage/by-base-model', { params: { days } }),
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
    // DK-2：排行面板的視窗必須跟 KPI 同一段。後端 get_top_* 原先寫死 30d，
    // 改由 range 參數驅動（get_time_range 同一張 RANGE_CONFIG 對照），
    // 否則「本週用量」會被答成 30 天數字卻不標明。
    range: selectedRange.value,
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
