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
    <span v-if="bracketed" aria-hidden="true" class="term-btn__bracket">[</span>
    <span class="term-btn__label">
      <slot>{{ label }}</slot>
      <span v-if="loading" class="term-btn__dots" aria-hidden="true">…</span>
    </span>
    <span v-if="bracketed" aria-hidden="true" class="term-btn__bracket">]</span>
  </button>
</template>

<script setup>
defineProps({
  variant: { type: String, default: 'default' }, // default | primary | danger | ghost
  size: { type: String, default: 'md' },          // md | xs
  type: { type: String, default: 'button' },
  disabled: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  bracketed: { type: Boolean, default: true },
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
