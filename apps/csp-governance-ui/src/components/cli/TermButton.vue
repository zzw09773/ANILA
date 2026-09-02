<!--
  Bracketed CLI button. Variants: default | primary | danger | ghost.
  - Renders `[ Submit ]` framing automatically when `bracketed` is true.
  - Use `xs` for inline action sizes inside table rows.
-->
<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    class="term-btn"
    :class="[
      `term-btn--${variant}`,
      size === 'xs' ? 'term-btn--xs' : '',
    ]"
    @click="$emit('click', $event)"
  >
    <span class="term-btn__label">
      <slot>{{ label }}</slot>
      <span v-if="loading" class="term-btn__dots" aria-hidden="true">…</span>
    </span>
  </button>
</template>

<script setup>
defineProps({
  variant: { type: String, default: 'default' }, // default | primary | danger | ghost
  size: { type: String, default: 'md' },          // md | xs
  type: { type: String, default: 'button' },
  disabled: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  // 2026-09-02 行政風：括號不再渲染；prop 留著讓既有呼叫端不必改。
  bracketed: { type: Boolean, default: false },
  label: { type: String, default: '' },
})
defineEmits(['click'])
</script>

<style scoped>
.term-btn__bracket {
  color: currentColor;
  opacity: 0.55;
  font-weight: 400;
}
.term-btn__label {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
}
.term-btn__dots {
  letter-spacing: 0.1em;
}
</style>
