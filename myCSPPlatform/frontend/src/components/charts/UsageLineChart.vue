<template>
  <div ref="chartRef" class="w-full" :style="{ height: height + 'px' }"></div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
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

function renderChart() {
  if (!chartRef.value || !props.chartData) return

  if (!chart) {
    chart = echarts.init(chartRef.value)
  }

  const { timestamps, series } = props.chartData

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
      bottom: 0,
      textStyle: { fontSize: 12 },
    },
    grid: {
      top: 20,
      left: 60,
      right: 20,
      bottom: series.length > 1 ? 50 : 30,
    },
    xAxis: {
      type: 'category',
      data: xData,
      axisLabel: { fontSize: 11, rotate: 30 },
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
