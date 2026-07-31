<!--
  告警摘要卡（W3-3④）。

  `/api/alerts/summary` 早就有 `high_count`,只是沒人擺首頁。air-gapped 內網
  沒有 mail relay(有沒有屬組織事實、列決策件),所以首版的「通知」就是:
  首頁這張卡 + 警報頁 30 秒輪詢 + `anila-ops.sh health` 讀同一支 API。

  `high_count > 0` 時整張卡換色並補一行行動指引 —— 平時與有事在視覺上必須
  不同,否則管理者掃過去不會停下來。
-->
<template>
  <TermBox title="告警" :hint="hint" pad="md">
    <div v-if="pageError" class="alert-sum__err">! {{ pageError }}</div>

    <div class="alert-sum__head" :class="[`is-${tone}`, { 'is-urgent': urgent }]">
      <TermDot :status="tone" :title="headline" />
      <span class="alert-sum__headline">{{ headline }}</span>
    </div>

    <div class="alert-sum__stats">
      <TermStat label="高嚴重度" :value="summary.high_count" :tone="urgent ? 'danger' : 'default'" format="int" />
      <TermStat label="待處理" :value="summary.open_count" :tone="summary.open_count ? 'warn' : 'default'" format="int" />
      <TermStat label="已確認" :value="summary.acknowledged_count" format="int" />
      <TermStat label="已解決" :value="summary.resolved_count" format="int" />
    </div>

    <router-link to="/alerts" class="alert-sum__link">→ 前往警報中心</router-link>
  </TermBox>
</template>

<script setup>
import { computed } from 'vue'
import {
  alertSummaryHeadline,
  alertSummaryTone,
  isAlertSummaryUrgent,
  normalizeAlertSummary,
} from '../../utils/alertSummary'
import TermBox from '../cli/TermBox.vue'
import TermDot from '../cli/TermDot.vue'
import TermStat from '../cli/TermStat.vue'

const props = defineProps({
  raw: { type: Object, default: null },
  pageError: { type: String, default: '' },
})

const summary = computed(() => normalizeAlertSummary(props.raw))
const tone = computed(() => alertSummaryTone(props.raw))
const urgent = computed(() => isAlertSummaryUrgent(props.raw))
const headline = computed(() => alertSummaryHeadline(props.raw))
const hint = computed(() => (urgent.value ? '需要立即處理' : ''))
</script>

<style scoped>
.alert-sum__err {
  font-size: var(--t-xs);
  color: var(--c-danger);
  border: var(--border-w) solid var(--c-danger);
  background: var(--c-danger-soft);
  padding: var(--gap-2) var(--gap-3);
  margin-bottom: var(--gap-2);
}

.alert-sum__head {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-border);
  background: var(--c-bg);
  margin-bottom: var(--gap-3);
}
.alert-sum__head.is-warn { border-color: var(--c-warn); background: var(--c-warn-soft); }
.alert-sum__head.is-danger { border-color: var(--c-danger); background: var(--c-danger-soft); }
.alert-sum__head.is-urgent .alert-sum__headline { color: var(--c-danger); font-weight: 600; }
.alert-sum__headline { font-size: var(--t-sm); color: var(--c-fg-1); }

.alert-sum__stats { display: flex; gap: var(--gap-3); flex-wrap: wrap; }

.alert-sum__link {
  display: inline-block;
  margin-top: var(--gap-3);
  font-size: var(--t-xs);
  color: var(--c-fg-2);
  text-decoration: none;
}
.alert-sum__link:hover { color: var(--c-accent); }
</style>
