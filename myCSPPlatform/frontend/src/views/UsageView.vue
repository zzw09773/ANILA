<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between flex-wrap gap-4">
      <h2 class="text-lg font-semibold">用量分析</h2>
      <div class="flex items-center space-x-4">
        <TimeRangeSelector v-model="selectedRange" @update:model-value="refreshUsage" />
        <button
          @click="handleExport"
          class="px-3 py-1.5 text-sm border border-gray-300 rounded-lg text-gray-600 hover:bg-gray-50"
        >
          匯出 CSV
        </button>
      </div>
    </div>

    <div class="flex items-center space-x-4 flex-wrap gap-2">
      <div>
        <label class="text-sm text-gray-600 mr-2">類型：</label>
        <select
          v-model="selectedModelType"
          @change="onModelTypeChange"
          class="px-3 py-1.5 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option :value="null">全部</option>
          <option value="llm">LLM</option>
          <option value="vlm">VLM</option>
          <option value="embedding">Embedding</option>
          <option value="agent">Agent</option>
        </select>
      </div>
      <div v-if="authStore.isAdmin">
        <label class="text-sm text-gray-600 mr-2">部門：</label>
        <select
          v-model="selectedDepartment"
          @change="onDepartmentChange"
          class="px-3 py-1.5 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option :value="null">全部</option>
          <option v-for="d in activeDepartments" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </div>
      <div>
        <label class="text-sm text-gray-600 mr-2">模型：</label>
        <select
          v-model="selectedModel"
          @change="refreshUsage"
          class="px-3 py-1.5 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option :value="null">全部</option>
          <option v-for="m in filteredModels" :key="m.id" :value="m.id">{{ m.display_name }}</option>
        </select>
      </div>
      <div v-if="authStore.isAdmin">
        <label class="text-sm text-gray-600 mr-2">使用者：</label>
        <select
          v-model="selectedUser"
          @change="refreshUsage"
          class="px-3 py-1.5 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option :value="null">全部</option>
          <option v-for="u in filteredUsers" :key="u.id" :value="u.id">{{ u.username }}</option>
        </select>
      </div>
      <div>
        <label class="text-sm text-gray-600 mr-2">分組：</label>
        <select
          v-model="groupBy"
          @change="refreshUsage"
          class="px-3 py-1.5 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="total">總計</option>
          <option value="model">依模型</option>
          <option v-if="authStore.isAdmin" value="department">依部門</option>
          <option v-if="authStore.isAdmin" value="user">依使用者</option>
        </select>
      </div>
    </div>

    <div class="bg-white rounded-xl border border-gray-200 p-5">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-5">
        <div class="rounded-xl bg-gray-50 border border-gray-200 p-4">
          <div class="text-sm text-gray-500">{{ rangeLabel }} 總請求</div>
          <div class="text-2xl font-semibold mt-2">{{ formatNum(usageStore.summary?.total_requests || 0) }}</div>
        </div>
        <div class="rounded-xl bg-gray-50 border border-gray-200 p-4">
          <div class="text-sm text-gray-500">{{ rangeLabel }} 總 Tokens</div>
          <div class="text-2xl font-semibold mt-2">{{ formatNum(usageStore.summary?.total_tokens || 0) }}</div>
        </div>
        <div class="rounded-xl bg-gray-50 border border-gray-200 p-4">
          <div class="text-sm text-gray-500">{{ rangeLabel }} 活躍 API Keys</div>
          <div class="text-2xl font-semibold mt-2">{{ formatNum(usageStore.summary?.active_api_keys || 0) }}</div>
        </div>
      </div>

      <UsageLineChart :chart-data="usageStore.chartData" :height="400" />
    </div>

    <div
      class="grid gap-6"
      :class="authStore.isAdmin ? 'grid-cols-1 xl:grid-cols-3' : 'grid-cols-1 lg:grid-cols-2'"
    >
      <div class="bg-white rounded-xl border border-gray-200 p-5">
        <h3 class="text-sm font-medium text-gray-700 mb-3">Top 模型用量（30 天）</h3>
        <table class="w-full text-sm">
          <thead>
            <tr class="text-gray-500 border-b">
              <th class="py-2 text-left">模型</th>
              <th class="py-2 text-left">類型</th>
              <th class="py-2 text-right">Token 數</th>
              <th class="py-2 text-right">請求數</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in usageStore.topModels" :key="m.model_id" class="border-b last:border-0">
              <td class="py-2">{{ m.model_name }}</td>
              <td class="py-2">
                <span class="text-xs px-1.5 py-0.5 rounded" :class="typeTagColor(m.model_type)">
                  {{ (m.model_type || '').toUpperCase() }}
                </span>
              </td>
              <td class="py-2 text-right font-mono">{{ formatNum(m.total_tokens) }}</td>
              <td class="py-2 text-right font-mono">{{ formatNum(m.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topModels.length === 0">
              <td colspan="4" class="py-4 text-center text-gray-400">暫無資料</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="authStore.isAdmin" class="bg-white rounded-xl border border-gray-200 p-5">
        <h3 class="text-sm font-medium text-gray-700 mb-3">Top 部門用量（30 天）</h3>
        <table class="w-full text-sm">
          <thead>
            <tr class="text-gray-500 border-b">
              <th class="py-2 text-left">部門</th>
              <th class="py-2 text-right">Token 數</th>
              <th class="py-2 text-right">請求數</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="department in usageStore.topDepartments"
              :key="department.department_id ?? 'unassigned'"
              class="border-b last:border-0"
            >
              <td class="py-2">{{ department.department_name }}</td>
              <td class="py-2 text-right font-mono">{{ formatNum(department.total_tokens) }}</td>
              <td class="py-2 text-right font-mono">{{ formatNum(department.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topDepartments.length === 0">
              <td colspan="3" class="py-4 text-center text-gray-400">暫無資料</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="authStore.isAdmin" class="bg-white rounded-xl border border-gray-200 p-5">
        <h3 class="text-sm font-medium text-gray-700 mb-3">Top 使用者用量（30 天）</h3>
        <table class="w-full text-sm">
          <thead>
            <tr class="text-gray-500 border-b">
              <th class="py-2 text-left">使用者</th>
              <th class="py-2 text-right">Token 數</th>
              <th class="py-2 text-right">請求數</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in usageStore.topUsers" :key="u.user_id" class="border-b last:border-0">
              <td class="py-2">{{ u.username }}</td>
              <td class="py-2 text-right font-mono">{{ formatNum(u.total_tokens) }}</td>
              <td class="py-2 text-right font-mono">{{ formatNum(u.total_requests) }}</td>
            </tr>
            <tr v-if="usageStore.topUsers.length === 0">
              <td colspan="3" class="py-4 text-center text-gray-400">暫無資料</td>
            </tr>
          </tbody>
        </table>
      </div>
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

const usageStore = useUsageStore()
const authStore = useAuthStore()

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

const rangeLabel = computed(() => {
  const labels = {
    '4h': '4 小時',
    '12h': '12 小時',
    '24h': '24 小時',
    '7d': '7 天',
    '30d': '30 天',
  }
  return labels[selectedRange.value] || selectedRange.value
})

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
  const rankingParams = buildRankingParams()
  await usageStore.fetchTopModels(10, rankingParams)
  if (authStore.isAdmin) {
    await Promise.all([
      usageStore.fetchTopUsers(10, rankingParams),
      usageStore.fetchTopDepartments(10, rankingParams),
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
  ])
}

onMounted(async () => {
  try {
    const { data } = await listModels()
    models.value = data
  } catch {}

  if (authStore.isAdmin) {
    try {
      const [{ data: usersData }, { data: departmentsData }] = await Promise.all([
        client.get('/api/users'),
        listDepartments(),
      ])
      users.value = usersData
      departments.value = departmentsData
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
  if (groupBy.value === 'department' && selectedDepartment.value) {
    groupBy.value = 'total'
  }
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

function typeTagColor(type) {
  const colors = {
    llm: 'bg-blue-50 text-blue-700',
    vlm: 'bg-purple-50 text-purple-700',
    embedding: 'bg-green-50 text-green-700',
    agent: 'bg-orange-50 text-orange-700',
  }
  return colors[type] || 'bg-gray-50 text-gray-700'
}

function formatNum(n) {
  if (!n) return '0'
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return n.toLocaleString()
}
</script>
