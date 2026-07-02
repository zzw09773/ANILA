import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getUsageSummary,
  getUsageChart,
  getTopModels,
  getTopUsers,
  getTopDepartments,
  exportUsageCsv,
} from '../api/usage'

export const useUsageStore = defineStore('usage', () => {
  const summary = ref(null)
  const chartData = ref(null)
  const topModels = ref([])
  const topUsers = ref([])
  const topDepartments = ref([])
  const loading = ref(false)

  async function fetchSummary(params = {}) {
    const { data } = await getUsageSummary(params)
    summary.value = data
  }

  async function fetchChart(params) {
    loading.value = true
    try {
      const { data } = await getUsageChart(params)
      chartData.value = data
    } finally {
      loading.value = false
    }
  }

  async function fetchTopModels(limit = 10, params = {}) {
    const { data } = await getTopModels({ limit, ...params })
    topModels.value = data
  }

  async function fetchTopUsers(limit = 10, params = {}) {
    const { data } = await getTopUsers({ limit, ...params })
    topUsers.value = data
  }

  async function fetchTopDepartments(limit = 10, params = {}) {
    const { data } = await getTopDepartments({ limit, ...params })
    topDepartments.value = data
  }

  async function exportCsv(params) {
    const { data } = await exportUsageCsv(params)
    const blob = new Blob([data], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `usage_${params.range || '24h'}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }

  return {
    summary, chartData, topModels, topUsers, topDepartments, loading,
    fetchSummary, fetchChart, fetchTopModels, fetchTopUsers, fetchTopDepartments, exportCsv,
  }
})
