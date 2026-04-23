<template>
  <div class="relative w-full" :style="{ height: height + 'px' }">
    <div ref="chartRef" class="w-full h-full"></div>
    <div
      v-if="isEmpty"
      class="absolute inset-0 flex flex-col items-center justify-center text-gray-400 pointer-events-none"
    >
      <div class="text-sm">目前無可用用量資料</div>
      <div class="text-xs mt-1">完成幾筆請求後，此圖會自動更新。</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent,
  CanvasRenderer,
])

const props = defineProps({
  chartData: { type: Object, default: null },
  height: { type: Number, default: 350 },
})

const chartRef = ref(null)
let chart = null

const isEmpty = computed(() => {
  const data = props.chartData
  if (!data) return true
  const timestamps = Array.isArray(data.timestamps) ? data.timestamps : []
  const series = Array.isArray(data.series) ? data.series : []
  if (timestamps.length === 0 || series.length === 0) return true
  return series.every((s) => {
    const points = Array.isArray(s?.data) ? s.data : []
    return points.every((v) => !v)
  })
})

function renderChart() {
  if (!chartRef.value || !props.chartData) return

  if (!chart) {
    chart = echarts.init(chartRef.value)
  }

  const { timestamps, series } = props.chartData
  if (!Array.isArray(timestamps) || !Array.isArray(series) || timestamps.length === 0) {
    chart.clear()
    return
  }

  const xData = timestamps.map((ts) => {
    const d = new Date(ts * 1000)
    return d.toLocaleString('zh-TW', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  })

  const option = {
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(255,255,255,0.95)',
      borderColor: '#e5e7eb',
      textStyle: { color: '#1f2937', fontSize: 12 },
    },
    legend: {
      data: series.map((s) => s.name),
      // Rotated x-axis labels take ~32px; put the legend above that and
      // leave a gap so they never overlap. `bottom: 0` was the bug.
      bottom: 0,
      padding: [4, 8],
      textStyle: { fontSize: 12 },
    },
    grid: {
      top: 20,
      left: 60,
      right: 20,
      // Reserve room for the rotated x-axis labels (~36px at rotate:30°
      // with fontSize:11) plus the legend (~24px tall at fontSize:12).
      // `containLabel: true` makes ECharts auto-include axis labels in
      // the grid box so our `bottom` sits BELOW them, not behind them.
      bottom: 60,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: xData,
      axisLabel: { fontSize: 11, rotate: 30, margin: 12 },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        fontSize: 11,
        formatter: (v) => {
          if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M'
          if (v >= 1000) return (v / 1000).toFixed(1) + 'K'
          return v
        },
      },
    },
    series: series.map((s, i) => ({
      name: s.name,
      type: 'line',
      data: s.data,
      smooth: true,
      lineStyle: { width: 2 },
      areaStyle: { opacity: 0.08 },
    })),
    color: ['#6366f1', '#06b6d4', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6'],
  }

  chart.setOption(option, true)
}

watch(() => props.chartData, renderChart, { deep: true })

onMounted(() => {
  renderChart()
  window.addEventListener('resize', () => chart?.resize())
})

onUnmounted(() => {
  chart?.dispose()
  window.removeEventListener('resize', () => chart?.resize())
})
</script>
