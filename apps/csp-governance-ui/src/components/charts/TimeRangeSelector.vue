<template>
  <div class="trange" role="tablist" aria-label="time range">
    <button
      v-for="option in options"
      :key="option.value"
      role="tab"
      :aria-selected="modelValue === option.value"
      class="trange__opt"
      :class="{ 'is-active': modelValue === option.value }"
      @click="$emit('update:modelValue', option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: '24h' },
})
defineEmits(['update:modelValue'])

const options = [
  { value: '4h',  label: '4h' },
  { value: '12h', label: '12h' },
  { value: '24h', label: '24h' },
  { value: '7d',  label: '7d' },
  { value: '30d', label: '30d' },
]
</script>

<style scoped>
.trange {
  display: inline-flex;
  border: var(--border-w) solid var(--c-border-strong);
  border-radius: var(--r-soft);
  background: var(--c-surface-1);
  overflow: hidden;
}
.trange__opt {
  background: transparent;
  border: 0;
  height: 26px;
  padding: 0 var(--gap-3);
  color: var(--c-fg-2);
  font-family: var(--font-mono);
  font-size: var(--t-xs);
  letter-spacing: 0.04em;
  cursor: pointer;
  border-right: var(--border-w) solid var(--c-border);
}
.trange__opt:last-child { border-right: 0; }
.trange__opt:hover { color: var(--c-fg-1); background: var(--c-surface-2); }
.trange__opt.is-active {
  color: var(--c-accent-fg);
  background: var(--c-accent);
  font-weight: 600;
}
</style>
