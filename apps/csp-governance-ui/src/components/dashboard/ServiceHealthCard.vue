<!--
  服務健康總覽卡（W3-3⑦）。

  admin-journey D1:22 個治理視圖沒有一個顯示 router / anila-studio /
  ingestion-worker / csp-db / redis / nginx / pptx-renderer 的狀態。這張卡是
  那個缺口的直接解 —— 綠/黃/紅一個總燈 + 逐服務一列 + 最後檢查時間。

  邏輯全在 `src/utils/healthOverview.js`(純函式,有 node --test 測)。這裡
  只負責畫,所以「載入失敗顯示全綠」這種最糟的失效模式在那一層就被擋掉。

  UX-09:基礎服務、模型、Agent 分成三組摘要。基礎服務全綠不再代表整個平台,
  模型 4/8 要自己一行、附檢視入口,0/0 講「尚未註冊」而不是健康。
  UX-11:正常服務先收成一行摘要,有異常才預設展開明細。
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

    <!-- 基礎服務：健康時只留摘要，有問題才逐列顯示細節(UX-11) -->
    <div v-if="view.loaded" class="svc-health__section-head">
      <span class="svc-health__section-label">基礎服務</span>
      <span class="svc-health__section-count tnum">{{ basicSummary }}</span>
      <button
        v-if="view.services.length"
        type="button"
        class="svc-health__toggle"
        :aria-expanded="showBasicDetails ? 'true' : 'false'"
        @click="toggleBasicDetails"
      >
        {{ showBasicDetails ? '收起明細' : '展開明細' }}
      </button>
    </div>

    <ul v-if="showBasicDetails && view.services.length" class="svc-health__list">
      <li v-for="svc in view.services" :key="svc.name" class="svc-health__row">
        <TermDot :status="svc.tone" :title="svc.statusLabel" />
        <span class="svc-health__name">{{ svc.label }}</span>
        <span class="svc-health__svc">{{ svc.name }}</span>
        <span class="svc-health__state" :class="`is-${svc.tone}`">{{ svc.statusLabel }}</span>
        <span class="svc-health__reason">{{ svc.reasonLabel }}</span>
        <span class="svc-health__latency tnum">{{ svc.latencyMs === null ? '—' : `${svc.latencyMs} ms` }}</span>
      </li>
    </ul>
    <TermEmpty v-else-if="!view.services.length" :message="loading ? '探測中…' : '尚無服務健康資料'" />

    <!-- 模型／Agent：未註冊或未確認不當健康，獨立醒目並連到對應頁(UX-09) -->
    <ul v-if="registryEntries.length" class="svc-health__registry" :title="registryHint">
      <li v-for="entry in registryEntries" :key="entry.key" class="svc-health__registry-row">
        <TermDot :status="entry.tone" :title="entry.text" />
        <span class="svc-health__registry-text" :class="`is-${entry.tone}`">{{ entry.text }}</span>
        <span v-if="entry.detail" class="svc-health__registry-detail">（{{ entry.detail }}）</span>
        <router-link
          v-if="reachable(entry)"
          :to="entry.to"
          class="svc-health__registry-link"
        >→ {{ entry.linkLabel }}</router-link>
        <span v-else class="svc-health__registry-link is-muted">{{ entry.linkLabel }}</span>
      </li>
    </ul>
  </TermBox>
</template>

<script setup>
import { computed, ref } from 'vue'
import {
  registryEntryReachable,
  registryHealthEntries,
  registryHealthLine,
  summarizeHealthOverview,
} from '../../utils/healthOverview'
import { useAuthStore } from '../../stores/auth'
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

const authStore = useAuthStore()
const view = computed(() => summarizeHealthOverview(props.overview))

// 沿用既有 tier：deputy(steward 但非 developer)看得到狀態,但 /models 不在
// deputy 白名單、/developer/agents 又要 developer → 只藏連結不藏文字,
// 不讓使用者點進死路。
function reachable(entry) {
  return registryEntryReachable(entry, {
    isDeveloper: authStore.isDeveloper,
    isDeputy: authStore.isDeputy,
    isAdmin: authStore.isAdmin,
  })
}

// null 表示尚未手動切換：有異常時預設展開、全正常時收合，把空間讓出來(UX-11)。
const detailsOverride = ref(null)
const showBasicDetails = computed(() =>
  detailsOverride.value === null ? view.value.hasProblem : detailsOverride.value,
)
function toggleBasicDetails() {
  detailsOverride.value = !showBasicDetails.value
}

const overallHeadline = computed(() => {
  const v = view.value
  if (!v.loaded) return '尚未取得服務狀態'
  const bad = v.counts.unhealthy
  const warn = v.counts.degraded + v.counts.unknown
  if (bad > 0) return `${bad} 個服務異常（共 ${v.total} 個）`
  if (warn > 0) return `${warn} 個服務需確認（共 ${v.total} 個）`
  return `全部 ${v.total} 個服務正常`
})

const hint = computed(() => (view.value.loaded ? '基礎服務；模型與 Agent 見下方' : ''))

const basicSummary = computed(() => {
  const v = view.value
  if (!v.services.length) return ''
  const bad = v.counts.unhealthy
  const warn = v.counts.degraded + v.counts.unknown
  if (bad === 0 && warn === 0) return `全部 ${v.total} 個正常`
  const parts = []
  if (bad > 0) parts.push(`${bad} 異常`)
  if (warn > 0) parts.push(`${warn} 待確認`)
  return `${parts.join(' · ')}（共 ${v.total} 個）`
})

const registryEntries = computed(() => registryHealthEntries(view.value.models, view.value.agents))
const registryHint = computed(() => registryHealthLine(view.value.models, view.value.agents))
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

.svc-health__section-head {
  display: flex;
  align-items: baseline;
  gap: var(--gap-2);
  padding: 2px 0 var(--gap-1);
}
.svc-health__section-label {
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
  color: var(--c-fg-3);
}
.svc-health__section-count { font-size: var(--t-2xs); color: var(--c-fg-2); }
.svc-health__toggle {
  margin-left: auto;
  background: transparent;
  border: 0;
  color: var(--c-fg-3);
  font: inherit;
  font-size: var(--t-2xs);
  cursor: pointer;
  padding: 2px 0;
}
.svc-health__toggle:hover { color: var(--c-accent); }

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
  list-style: none;
  margin: var(--gap-3) 0 0;
  padding: var(--gap-2) 0 0;
  border-top: var(--border-w) solid var(--c-border);
}
.svc-health__registry-row {
  display: flex;
  align-items: baseline;
  gap: var(--gap-2);
  padding: 3px 0;
  font-size: var(--t-2xs);
}
.svc-health__registry-text { color: var(--c-fg-1); }
.svc-health__registry-text.is-warn { color: var(--c-warn); }
.svc-health__registry-text.is-danger { color: var(--c-danger); }
.svc-health__registry-text.is-idle { color: var(--c-fg-2); }
.svc-health__registry-detail { color: var(--c-fg-3); }
.svc-health__registry-link {
  margin-left: auto;
  color: var(--c-fg-2);
  text-decoration: none;
  font-size: var(--t-2xs);
}
.svc-health__registry-link:hover { color: var(--c-accent); }
.svc-health__registry-link.is-muted { color: var(--c-fg-3); }

@media (max-width: 800px) {
  .svc-health__row { grid-template-columns: 10px 1fr 64px; }
  .svc-health__svc,
  .svc-health__reason,
  .svc-health__latency { display: none; }
}
</style>
