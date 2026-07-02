<template>
  <div class="usage-chart" :style="{ height: height + 'px' }">
    <div ref="chartRef" class="usage-chart__canvas" />
    <div v-if="isEmpty" class="usage-chart__empty">
      <span>── no usage data ──</span>
      <small>chart updates after the first proxied request lands.</small>
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
let themeObserver = null
let resizeHandler = null

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

function readPalette() {
  const root = document.documentElement
  const css = getComputedStyle(root)
  const v = (name, fb) => (css.getPropertyValue(name).trim() || fb)
  return {
    series: [
      v('--chart-1', '#7fd99b'),
      v('--chart-2', '#6cb6ff'),
      v('--chart-3', '#e0a458'),
      v('--chart-4', '#c79bff'),
      v('--chart-5', '#ec6f7c'),
      v('--chart-6', '#4dd0c0'),
    ],
    fg1: v('--c-fg-1', '#d8dee8'),
    fg2: v('--c-fg-2', '#9aa4b6'),
    fg3: v('--c-fg-3', '#66708a'),
    grid: v('--chart-grid', '#1f2530'),
    axis: v('--chart-axis', '#66708a'),
    surface: v('--c-surface-1', '#11151c'),
    border: v('--c-border', '#1f2530'),
    mono: v('--font-mono', '"JetBrains Mono", monospace'),
  }
}

function renderChart() {
  if (!chartRef.value) return
  if (!props.chartData) return

  if (!chart) {
    chart = echarts.init(chartRef.value)
  }

  const { timestamps, series } = props.chartData
  if (!Array.isArray(timestamps) || !Array.isArray(series) || timestamps.length === 0) {
    chart.clear()
    return
  }

  const p = readPalette()

  const xData = timestamps.map((ts) => {
    const d = new Date(ts * 1000)
    return d.toLocaleString('en-GB', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  })

  const option = {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: p.mono, fontSize: 11, color: p.fg2 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: p.surface,
      borderColor: p.border,
      borderWidth: 1,
      textStyle: { color: p.fg1, fontFamily: p.mono, fontSize: 11 },
      extraCssText: 'border-radius: 0; box-shadow: none;',
      axisPointer: { lineStyle: { color: p.axis, type: 'dashed' } },
    },
    legend: {
      data: series.map((s) => s.name),
      bottom: 0,
      padding: [4, 8],
      textStyle: { fontSize: 11, color: p.fg2, fontFamily: p.mono },
      itemWidth: 16,
      itemHeight: 2,
      icon: 'rect',
    },
    grid: {
      top: 18,
      left: 56,
      right: 16,
      bottom: 56,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: xData,
      axisLine: { lineStyle: { color: p.axis } },
      axisTick: { lineStyle: { color: p.axis } },
      axisLabel: { fontSize: 10, color: p.fg3, rotate: 30, margin: 12, fontFamily: p.mono },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        fontSize: 10,
        color: p.fg3,
        fontFamily: p.mono,
        formatter: (v) => {
          if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
          if (v >= 1_000) return (v / 1_000).toFixed(1) + 'K'
          return v
        },
      },
      splitLine: { lineStyle: { color: p.grid, type: 'dashed' } },
    },
    series: series.map((s, i) => ({
      name: s.name,
      type: 'line',
      data: s.data,
      smooth: false,
      symbol: 'none',
      lineStyle: { width: 1.5, color: p.series[i % p.series.length] },
      areaStyle: {
        opacity: 0.10,
        color: p.series[i % p.series.length],
      },
    })),
    color: p.series,
  }

  chart.setOption(option, true)
}

watch(() => props.chartData, renderChart, { deep: true })

onMounted(() => {
  renderChart()
  resizeHandler = () => chart?.resize()
  window.addEventListener('resize', resizeHandler)

  // Re-render when the document theme attribute changes — we read tokens
  // off CSSOM so the chart palette must refresh on theme swap.
  themeObserver = new MutationObserver(() => renderChart())
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
})

onUnmounted(() => {
  if (resizeHandler) window.removeEventListener('resize', resizeHandler)
  themeObserver?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<style scoped>
.usage-chart { position: relative; width: 100%; }
.usage-chart__canvas { width: 100%; height: 100%; }
.usage-chart__empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--c-fg-3);
  font-size: var(--t-xs);
  pointer-events: none;
}
.usage-chart__empty small { font-size: var(--t-2xs); color: var(--c-fg-mute); }
</style>
