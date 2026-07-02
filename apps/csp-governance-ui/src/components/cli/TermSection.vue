<!--
  Inline section header — `── TITLE ─────────────…`. Use inside views to
  group regions without adding nested boxes.
-->
<template>
  <header class="term-section">
    <span class="term-section__title">
      <slot name="title">{{ title }}</slot>
    </span>
    <span class="term-section__rule" />
    <div v-if="$slots.trailing" class="term-section__trailing">
      <slot name="trailing" />
    </div>
  </header>
</template>

<script setup>
defineProps({
  title: { type: String, default: '' },
})
</script>

<style scoped>
.term-section {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: 0 0 var(--gap-2) 0;
  border-bottom: var(--border-w) solid var(--c-border);
  margin-bottom: var(--gap-3);
}
.term-section__title {
  font-size: var(--t-xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
  color: var(--c-fg-2);
  font-weight: 600;
}
.term-section__rule {
  flex: 1;
  height: 1px;
  background: var(--c-border);
}
.term-section__trailing {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-2);
}
</style>
