<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between flex-wrap gap-4">
      <h2 class="text-lg font-semibold">告警中心</h2>
      <div class="flex items-center gap-3 text-sm">
        <span class="px-3 py-1 rounded-full bg-red-50 text-red-700">Open {{ summary.open_count }}</span>
        <span class="px-3 py-1 rounded-full bg-yellow-50 text-yellow-700">Ack {{ summary.acknowledged_count }}</span>
        <span class="px-3 py-1 rounded-full bg-gray-100 text-gray-700">Resolved {{ summary.resolved_count }}</span>
      </div>
    </div>

    <div v-if="pageError" class="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      {{ pageError }}
    </div>

    <div class="flex items-center gap-3 flex-wrap">
      <select v-model="filters.status" @change="fetchData" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500">
        <option value="">全部狀態</option>
        <option value="open">Open</option>
        <option value="acknowledged">Acknowledged</option>
        <option value="resolved">Resolved</option>
      </select>
      <select v-model="filters.severity" @change="fetchData" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500">
        <option value="">全部等級</option>
        <option value="low">Low</option>
        <option value="medium">Medium</option>
        <option value="high">High</option>
        <option value="critical">Critical</option>
      </select>
      <input
        v-model="filters.category"
        @keyup.enter="fetchData"
        type="text"
        placeholder="分類，例如 health"
        class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
      />
      <button @click="fetchData" class="px-3 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">查詢</button>
    </div>

    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">告警</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">分類</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">最後出現</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="alert in alerts" :key="alert.id" class="border-b last:border-0 hover:bg-gray-50 align-top">
            <td class="px-4 py-3">
              <div class="flex items-center gap-2">
                <span class="inline-block w-2.5 h-2.5 rounded-full" :class="severityColor(alert.severity)"></span>
                <div class="font-medium">{{ alert.title }}</div>
              </div>
              <div class="text-xs text-gray-500 mt-1">{{ alert.message }}</div>
            </td>
            <td class="px-4 py-3 text-gray-500">
              <div>{{ alert.category }}</div>
              <div class="text-xs text-gray-400">{{ alert.source_type || '-' }} / {{ alert.source_id || '-' }}</div>
            </td>
            <td class="px-4 py-3">
              <span class="text-xs px-2 py-0.5 rounded" :class="statusColor(alert.status)">
                {{ alert.status }}
              </span>
            </td>
            <td class="px-4 py-3 text-gray-500">{{ formatDate(alert.last_seen_at) }}</td>
            <td class="px-4 py-3 space-x-2">
              <button
                v-if="alert.status === 'open'"
                @click="handleAck(alert)"
                class="text-yellow-700 hover:text-yellow-900 text-xs"
              >
                Acknowledge
              </button>
              <button
                v-if="alert.status !== 'resolved'"
                @click="handleResolve(alert)"
                class="text-emerald-700 hover:text-emerald-900 text-xs"
              >
                Resolve
              </button>
            </td>
          </tr>
          <tr v-if="alerts.length === 0">
            <td colspan="5" class="px-4 py-8 text-center text-gray-400">目前沒有符合條件的告警</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { acknowledgeAlert, getAlertSummary, listAlerts, resolveAlert } from '../api/alerts'

const alerts = ref([])
const summary = ref({ open_count: 0, acknowledged_count: 0, resolved_count: 0, high_count: 0 })
const filters = ref({ status: '', severity: '', category: '' })
const pageError = ref('')

async function fetchData() {
  const params = {
    status: filters.value.status || undefined,
    severity: filters.value.severity || undefined,
    category: filters.value.category || undefined,
  }
  pageError.value = ''
  try {
    const [{ data: alertData }, { data: summaryData }] = await Promise.all([
      listAlerts(params),
      getAlertSummary(),
    ])
    alerts.value = alertData
    summary.value = summaryData
  } catch (e) {
    pageError.value = e.response?.data?.detail || '載入告警資料失敗'
  }
}

onMounted(fetchData)

async function handleAck(alert) {
  try {
    await acknowledgeAlert(alert.id)
    await fetchData()
  } catch (e) {
    alert(e.response?.data?.detail || 'Acknowledge 失敗')
  }
}

async function handleResolve(alert) {
  try {
    await resolveAlert(alert.id)
    await fetchData()
  } catch (e) {
    alert(e.response?.data?.detail || 'Resolve 失敗')
  }
}

function severityColor(severity) {
  return {
    low: 'bg-sky-400',
    medium: 'bg-yellow-400',
    high: 'bg-orange-500',
    critical: 'bg-red-600',
  }[severity] || 'bg-gray-400'
}

function statusColor(status) {
  return {
    open: 'bg-red-50 text-red-700',
    acknowledged: 'bg-yellow-50 text-yellow-700',
    resolved: 'bg-gray-100 text-gray-700',
  }[status] || 'bg-gray-100 text-gray-700'
}

function formatDate(value) {
  return new Date(value).toLocaleString('zh-TW')
}
</script>
