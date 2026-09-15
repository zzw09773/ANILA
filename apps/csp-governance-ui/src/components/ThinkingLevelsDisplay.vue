<!--
  模型思考等級探測結果：null 與 [] 皆灰標「未探測」（後端永不回 []；
  與 anila-shell runtime/thinkingTier.js 同義）；有值則以小 chip 列出，
  none 顯示為「關」，超過 4 個折成 +N。
-->
<template>
  <span class="thinking-levels" :title="fullTitle">
    <TermBadge v-if="view.status === 'unprobed'" variant="muted">未探測</TermBadge>
    <template v-else>
      <span
        v-for="level in view.visible"
        :key="level"
        class="thinking-levels__chip"
      >{{ formatThinkingLevel(level) }}</span>
      <span
        v-if="view.overflow"
        class="thinking-levels__chip thinking-levels__chip--more"
      >+{{ view.overflow }}</span>
    </template>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { TermBadge } from './cli'
import { formatThinkingLevel, thinkingLevelsView } from '../utils/thinkingLevels'

const props = defineProps({
  levels: { default: null },
})

const view = computed(() => thinkingLevelsView(props.levels))
const fullTitle = computed(() => {
  if (view.value.status !== 'probed' || !Array.isArray(props.levels) || !props.levels.length) {
    return ''
  }
  return props.levels.map(formatThinkingLevel).join('、')
})
</script>

<style scoped>
.thinking-levels {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.thinking-levels__chip {
  display: inline-flex;
  align-items: center;
  font-size: var(--t-2xs);
  padding: 1px 6px;
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-soft);
  color: var(--c-fg-2);
  letter-spacing: 0.02em;
  line-height: 1.4;
}
.thinking-levels__chip--more {
  color: var(--c-fg-3);
}
</style>
