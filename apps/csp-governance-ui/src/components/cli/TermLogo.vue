<!--
  ANILA wordmark — terminal-cursor square + plain monospace wordmark.
  Single SVG glyph (no external asset) so it inherits theme colors and stays
  crisp at every size. Pass `compact` for sidebar collapsed state.
-->
<template>
  <span class="term-logo" :class="{ 'term-logo--compact': compact }">
    <svg
      class="term-logo__mark"
      :width="size"
      :height="size"
      viewBox="0 0 16 16"
      role="img"
      aria-label="ANILA"
    >
      <!-- Outer frame -->
      <rect x="0.5" y="0.5" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1" />
      <!-- Inset solid block (cursor) -->
      <rect x="3.5" y="3.5" width="9" height="9" fill="currentColor" />
      <!-- Notch — gives it character beyond a generic square -->
      <rect x="9.5" y="3.5" width="3" height="3" fill="var(--c-bg)" />
    </svg>
    <span v-if="!compact" class="term-logo__word">
      <span class="term-logo__brand">ANILA</span>
      <span class="term-logo__sep">·</span>
      <span class="term-logo__sub">{{ subtitle }}</span>
    </span>
  </span>
</template>

<script setup>
defineProps({
  compact: { type: Boolean, default: false },
  size: { type: Number, default: 16 },
  subtitle: { type: String, default: 'CSP' },
})
</script>

<style scoped>
.term-logo {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-2);
  color: var(--c-accent);
}
.term-logo__mark {
  flex-shrink: 0;
}
.term-logo__word {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  color: var(--c-fg-1);
  font-weight: 600;
  letter-spacing: 0.06em;
  font-size: var(--t-md);
  line-height: 1;
}
.term-logo__brand {
  letter-spacing: 0.14em;
}
.term-logo__sep {
  color: var(--c-fg-3);
}
.term-logo__sub {
  color: var(--c-fg-2);
  font-size: var(--t-xs);
  letter-spacing: 0.18em;
  text-transform: uppercase;
}
</style>
