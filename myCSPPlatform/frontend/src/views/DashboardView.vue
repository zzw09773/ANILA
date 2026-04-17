<template>
  <div class="space-y-6">
    <!-- Summary Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <UsageSummaryCard label="24h 總請求數" :value="summary?.total_requests || 0" />
      <UsageSummaryCard label="24h 總 Token 數" :value="summary?.total_tokens || 0" />
      <UsageSummaryCard label="活躍模型" :value="summary?.active_models || 0" />
      <UsageSummaryCard label="活躍 API Key" :value="summary?.active_api_keys || 0" />
    </div>

    <!-- Mini Usage Chart -->
    <div class="bg-white rounded-xl border border-gray-200 p-5">
      <h3 class="text-sm font-medium text-gray-700 mb-3">24 小時用量趨勢</h3>
      <UsageLineChart :chart-data="chartData" :height="250" />
    </div>

    <!-- Platform Cards -->
    <div>
      <h3 class="text-sm font-medium text-gray-700 mb-3">平台連結</h3>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <PlatformCard v-for="link in platformLinks" :key="link.id" :link="link" />
      </div>
      <p v-if="platformLinks.length === 0" class="text-gray-400 text-sm">
        尚無平台連結，管理員可於設定中新增
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import UsageSummaryCard from '../components/dashboard/UsageSummaryCard.vue'
import PlatformCard from '../components/dashboard/PlatformCard.vue'
import UsageLineChart from '../components/charts/UsageLineChart.vue'
import { useUsageStore } from '../stores/usage'
import { listPlatformLinks } from '../api/platformLinks'

const usageStore = useUsageStore()
const summary = ref(null)
const chartData = ref(null)
const platformLinks = ref([])

onMounted(async () => {
  try {
    await Promise.all([
      usageStore.fetchSummary(),
      usageStore.fetchChart({ range: '24h', group_by: 'model' }),
      listPlatformLinks().then(({ data }) => { platformLinks.value = data }),
    ])
    summary.value = usageStore.summary
    chartData.value = usageStore.chartData
  } catch (e) {
    console.error('載入儀表板失敗:', e)
  }
})
</script>
