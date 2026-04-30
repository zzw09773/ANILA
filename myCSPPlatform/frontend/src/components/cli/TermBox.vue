<!--
  Titled terminal box. Renders a thin-bordered surface with a small-caps
  legend label inset in the top edge — `┌─ TITLE ────────────…`.
  Slots: default (body), trailing (right side of the title bar — actions / hints).
-->
<template>
  <section class="term-box" :class="[`term-box--pad-${pad}`, { 'term-box--inset': inset }]">
    <header v-if="title || $slots.title || $slots.trailing" class="term-box__head">
      <div class="term-box__legend">
        <span v-if="title" class="term-box__title">{{ title }}</span>
        <slot v-else name="title" />
        <span v-if="hint" class="term-box__hint">{{ hint }}</span>
      </div>
      <span class="term-box__rule" />
      <div v-if="$slots.trailing" class="term-box__trailing">
        <slot name="trailing" />
      </div>
    </header>
    <div class="term-box__body" :class="{ 'term-box__body--flush': flush }">
      <slot />
    </div>
  </section>
</template>

<script setup>
defineProps({
  title: { type: String, default: '' },
  hint: { type: String, default: '' },
  pad: { type: String, default: 'md' }, // none | sm | md | lg
  flush: { type: Boolean, default: false }, // remove body padding (e.g. for tables)
  inset: { type: Boolean, default: false }, // use --c-bg instead of --c-surface-1
})
</script>

<style scoped>
.term-box {
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  display: flex;
  flex-direction: column;
}
.term-box--inset {
  background: var(--c-bg);
}

.term-box__head {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: var(--gap-2) var(--gap-3);
  border-bottom: var(--border-w) solid var(--c-border);
  background: var(--c-surface-2);
  min-height: 30px;
}
.term-box__legend {
  display: inline-flex;
  align-items: baseline;
  gap: var(--gap-2);
  flex-shrink: 0;
}
.term-box__title {
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
  color: var(--c-fg-1);
  font-weight: 600;
}
.term-box__hint {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  letter-spacing: 0.05em;
}
.term-box__rule {
  flex: 1;
  height: 1px;
  background: var(--c-border);
  align-self: center;
}
.term-box__trailing {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-2);
  flex-shrink: 0;
}

.term-box__body--flush { padding: 0 !important; }
.term-box--pad-none > .term-box__body { padding: 0; }
.term-box--pad-sm   > .term-box__body { padding: var(--gap-2) var(--gap-3); }
.term-box--pad-md   > .term-box__body { padding: var(--gap-4); }
.term-box--pad-lg   > .term-box__body { padding: var(--gap-6); }
</style>
