<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between flex-wrap gap-4">
      <h2 class="text-lg font-semibold">審計日誌</h2>
      <div class="text-sm text-gray-500">顯示最近 {{ filters.limit }} 筆</div>
    </div>

    <div v-if="pageError" class="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      {{ pageError }}
    </div>

    <div class="flex items-center gap-3 flex-wrap">
      <input v-model="filters.actor_username" type="text" placeholder="操作者" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
      <input v-model="filters.action" type="text" placeholder="動作，例如 create" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
      <input v-model="filters.resource_type" type="text" placeholder="資源，例如 user" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
      <select v-model="filters.status" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500">
        <option value="">全部狀態</option>
        <option value="success">Success</option>
        <option value="failure">Failure</option>
      </select>
      <select v-model.number="filters.limit" class="px-3 py-2 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500">
        <option :value="50">50</option>
        <option :value="100">100</option>
        <option :value="200">200</option>
      </select>
      <button @click="fetchLogs" class="px-3 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">查詢</button>
    </div>

    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">時間</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作者</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">動作</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">資源</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">結果</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">細節</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="log in logs" :key="log.id" class="border-b last:border-0 hover:bg-gray-50 align-top">
            <td class="px-4 py-3 text-gray-500">{{ formatDate(log.created_at) }}</td>
            <td class="px-4 py-3">
              <div class="font-medium">{{ log.actor_username || 'system' }}</div>
              <div class="text-xs text-gray-400">{{ log.ip_address || '-' }}</div>
            </td>
            <td class="px-4 py-3">{{ log.action }}</td>
            <td class="px-4 py-3">
              <div>{{ log.resource_type }}</div>
              <div class="text-xs text-gray-400">{{ log.resource_id || '-' }}</div>
            </td>
            <td class="px-4 py-3">
              <span class="text-xs px-2 py-0.5 rounded" :class="log.status === 'success' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'">
                {{ log.status }}
              </span>
            </td>
            <td class="px-4 py-3 text-gray-500">
              <div>{{ log.detail || '-' }}</div>
              <pre v-if="log.metadata" class="mt-2 text-xs bg-gray-50 p-2 rounded overflow-x-auto">{{ JSON.stringify(log.metadata, null, 2) }}</pre>
            </td>
          </tr>
          <tr v-if="logs.length === 0">
            <td colspan="6" class="px-4 py-8 text-center text-gray-400">尚無審計資料</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listAuditLogs } from '../api/auditLogs'

const logs = ref([])
const pageError = ref('')
const filters = ref({
  actor_username: '',
  action: '',
  resource_type: '',
  status: '',
  limit: 100,
})

async function fetchLogs() {
  pageError.value = ''
  try {
    const { data } = await listAuditLogs({
      actor_username: filters.value.actor_username || undefined,
      action: filters.value.action || undefined,
      resource_type: filters.value.resource_type || undefined,
      status: filters.value.status || undefined,
      limit: filters.value.limit,
    })
    logs.value = data
  } catch (e) {
    pageError.value = e.response?.data?.detail || '載入審計日誌失敗'
  }
}

onMounted(fetchLogs)

function formatDate(value) {
  return new Date(value).toLocaleString('zh-TW')
}
</script>
