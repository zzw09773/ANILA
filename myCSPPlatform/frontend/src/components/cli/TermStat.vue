<!--
  KPI / metric tile in CLI form:
   ┌─ LABEL ────────…
   │  9.2K       <- big tabular figure
   │  delta hint
-->
<template>
  <div class="term-stat" :class="`term-stat--${tone}`">
    <div class="term-stat__head">
      <span class="term-stat__label">{{ label }}</span>
      <span v-if="hint" class="term-stat__hint">{{ hint }}</span>
    </div>
    <div class="term-stat__value tnum">
      <slot>{{ formatted }}</slot>
    </div>
    <div v-if="$slots.foot || delta" class="term-stat__foot">
      <slot name="foot">
        <span v-if="delta" :class="['term-stat__delta', delta.startsWith('-') ? 'is-down' : 'is-up']">
          {{ delta }}
        </span>
      </slot>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: [Number, String], default: 0 },
  hint: { type: String, default: '' },
  delta: { type: String, default: '' },
  tone: { type: String, default: 'default' }, // default | accent | warn | danger
  format: { type: String, default: 'compact' }, // compact | int | raw
})

const formatted = computed(() => {
  if (props.format === 'raw') return props.value
  const n = Number(props.value) || 0
  if (props.format === 'int') return n.toLocaleString()
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toLocaleString()
})
</script>

<style scoped>
.term-stat {
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  padding: var(--gap-3) var(--gap-4);
  display: flex;
  flex-direction: column;
  gap: var(--gap-1);
  min-height: 88px;
}
.term-stat__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.term-stat__label {
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
  color: var(--c-fg-3);
}
.term-stat__hint {
  font-size: var(--t-2xs);
  color: var(--c-fg-mute);
}
.term-stat__value {
  font-size: var(--t-3xl);
  font-weight: 600;
  color: var(--c-fg-1);
  letter-spacing: var(--tracking-tight);
  line-height: 1.1;
  margin-top: 2px;
}
.term-stat__foot {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
}
.term-stat__delta.is-up { color: var(--c-ok); }
.term-stat__delta.is-down { color: var(--c-danger); }

.term-stat--accent { border-left: 3px solid var(--c-accent); }
.term-stat--warn   { border-left: 3px solid var(--c-warn); }
.term-stat--danger { border-left: 3px solid var(--c-danger); }
</style>
