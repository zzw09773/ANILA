<!--
  服務健康總覽卡（W3-3⑦）。

  admin-journey D1:22 個治理視圖沒有一個顯示 router / anila-studio /
  ingestion-worker / csp-db / redis / nginx / pptx-renderer 的狀態。這張卡是
  那個缺口的直接解 —— 綠/黃/紅一個總燈 + 逐服務一列 + 最後檢查時間。

  邏輯全在 `src/utils/healthOverview.js`(純函式,有 node --test 測)。這裡
  只負責畫,所以「載入失敗顯示全綠」這種最糟的失效模式在那一層就被擋掉。
-->
<template>
  <TermBox title="服務健康" :hint="hint" pad="md">
    <template #trailing>
      <TermButton size="xs" variant="ghost" :loading="loading" label="重新探測" @click="$emit('refresh')" />
    </template>

    <div v-if="pageError" class="svc-health__err">! {{ pageError }}</div>

    <div class="svc-health__overall" :class="`is-${view.tone}`">
      <TermDot :status="view.tone" :title="view.overallLabel" />
      <span class="svc-health__overall-label">{{ overallHeadline }}</span>
      <span class="svc-health__checked">
        <span class="term-label">最後檢查</span>
        <span class="tnum">{{ view.checkedAtLabel }}</span>
      </span>
    </div>

    <ul v-if="view.services.length" class="svc-health__list">
      <li v-for="svc in view.services" :key="svc.name" class="svc-health__row">
        <TermDot :status="svc.tone" :title="svc.statusLabel" />
        <span class="svc-health__name">{{ svc.label }}</span>
        <span class="svc-health__svc">{{ svc.name }}</span>
        <span class="svc-health__state" :class="`is-${svc.tone}`">{{ svc.statusLabel }}</span>
        <span class="svc-health__reason">{{ svc.reasonLabel }}</span>
        <span class="svc-health__latency tnum">{{ svc.latencyMs === null ? '—' : `${svc.latencyMs} ms` }}</span>
      </li>
    </ul>
    <TermEmpty v-else :message="loading ? '探測中…' : '尚無服務健康資料'" />

    <p v-if="registryHint" class="svc-health__registry">{{ registryHint }}</p>
  </TermBox>
</template>

<script setup>
import { computed } from 'vue'
import { summarizeHealthOverview } from '../../utils/healthOverview'
import TermBox from '../cli/TermBox.vue'
import TermDot from '../cli/TermDot.vue'
import TermEmpty from '../cli/TermEmpty.vue'
import TermButton from '../cli/TermButton.vue'

const props = defineProps({
  overview: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  pageError: { type: String, default: '' },
})

defineEmits(['refresh'])

const view = computed(() => summarizeHealthOverview(props.overview))

const overallHeadline = computed(() => {
  const v = view.value
  if (!v.loaded) return '尚未取得服務狀態'
  const bad = v.counts.unhealthy
  const warn = v.counts.degraded + v.counts.unknown
  if (bad > 0) return `${bad} 個服務異常（共 ${v.total} 個）`
  if (warn > 0) return `${warn} 個服務需確認（共 ${v.total} 個）`
  return `全部 ${v.total} 個服務正常`
})

const hint = computed(() => (view.value.loaded ? `共 ${view.value.total} 個服務` : ''))

const registryHint = computed(() => {
  const { models, agents } = view.value
  if (!models && !agents) return ''
  const parts = []
  if (models) parts.push(`模型 ${models.healthy}/${models.total} 健康`)
  if (agents) parts.push(`Agent ${agents.healthy}/${agents.total} 健康`)
  return parts.join(' · ')
})
</script>

<style scoped>
.svc-health__err {
  font-size: var(--t-xs);
  color: var(--c-danger);
  border: var(--border-w) solid var(--c-danger);
  background: var(--c-danger-soft);
  padding: var(--gap-2) var(--gap-3);
  margin-bottom: var(--gap-2);
}

.svc-health__overall {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-border);
  background: var(--c-bg);
  margin-bottom: var(--gap-3);
  flex-wrap: wrap;
}
.svc-health__overall.is-warn { border-color: var(--c-warn); background: var(--c-warn-soft); }
.svc-health__overall.is-danger { border-color: var(--c-danger); background: var(--c-danger-soft); }

.svc-health__overall-label { color: var(--c-fg-1); font-size: var(--t-sm); font-weight: 500; }
.svc-health__checked {
  margin-left: auto;
  display: inline-flex;
  gap: 6px;
  align-items: baseline;
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
}

.svc-health__list { list-style: none; margin: 0; padding: 0; }
.svc-health__row {
  display: grid;
  grid-template-columns: 10px 1fr 130px 64px 1fr 72px;
  align-items: center;
  gap: var(--gap-2);
  padding: 5px 0;
  border-bottom: var(--border-w) dashed var(--c-border);
  font-size: var(--t-2xs);
}
.svc-health__row:last-child { border-bottom: 0; }
.svc-health__name { color: var(--c-fg-1); }
.svc-health__svc { color: var(--c-fg-3); font-family: var(--font-mono); }
.svc-health__state.is-ok { color: var(--c-success, #5ca663); }
.svc-health__state.is-warn { color: var(--c-warn); }
.svc-health__state.is-danger { color: var(--c-danger); }
.svc-health__state.is-idle { color: var(--c-fg-3); }
.svc-health__reason { color: var(--c-fg-2); }
.svc-health__latency { color: var(--c-fg-3); text-align: right; }

.svc-health__registry {
  margin: var(--gap-2) 0 0;
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
}

@media (max-width: 800px) {
  .svc-health__row { grid-template-columns: 10px 1fr 64px; }
  .svc-health__svc,
  .svc-health__reason,
  .svc-health__latency { display: none; }
}
</style>
