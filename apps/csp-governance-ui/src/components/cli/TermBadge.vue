<!--
  Compact tag chip. Variants snap to semantic tokens so views never reach
  for raw colors. Pass either `variant="ok|warn|danger|info|accent"` or
  `tone="llm|vlm|embedding|agent"` for model-type tags.
-->
<template>
  <span class="term-badge" :class="cls">
    <span v-if="dot" class="term-badge__dot" :class="dotCls" />
    <slot>{{ label }}</slot>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  variant: { type: String, default: '' },
  tone: { type: String, default: '' }, // llm | vlm | embedding | agent
  dot: { type: Boolean, default: false },
  label: { type: String, default: '' },
})

const cls = computed(() => {
  const c = []
  if (props.variant) c.push(`term-badge--${props.variant}`)
  if (props.tone) c.push(`term-badge--${props.tone}`)
  return c
})

const dotCls = computed(() => {
  const m = { ok: 'term-dot--ok', warn: 'term-dot--warn', danger: 'term-dot--danger', info: 'term-dot--info', accent: 'term-dot--ok' }
  return ['term-dot', m[props.variant] || 'term-dot--idle']
})
</script>

<style scoped>
.term-badge__dot {
  width: 6px;
  height: 6px;
}
</style>
