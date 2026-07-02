<!--
  Platform-link tile in CLI form: terse tabular row, hover crosshair.
  The icon glyph is rendered with a small ASCII pictogram (text in tabular
  monospace) — no emojis, no SVG icon library, deliberately authentic to
  the rest of the surface.
-->
<template>
  <a
    :href="link.url"
    target="_blank"
    rel="noopener"
    class="plink"
  >
    <span class="plink__glyph" :title="link.icon">
      {{ glyph }}
    </span>
    <span class="plink__body">
      <span class="plink__row">
        <span class="plink__name">{{ link.name }}</span>
        <span class="plink__arrow" aria-hidden="true">↗</span>
      </span>
      <span class="plink__desc">{{ link.description || link.url }}</span>
    </span>
  </a>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  link: { type: Object, required: true },
})

// Map icon keywords from the platform-links registry to short ASCII glyphs.
// These read like reference table entries, not emoji.
const GLYPHS = {
  workflow: '~~',
  git: '##',
  notebook: 'nb',
  chat: '<>',
  monitor: 'mn',
  database: 'db',
  api: '$_',
  docs: 'rd',
  cpu: 'op',
}
const glyph = computed(() => GLYPHS[props.link.icon] || '··')
</script>

<style scoped>
.plink {
  display: grid;
  grid-template-columns: 32px 1fr;
  gap: var(--gap-3);
  align-items: center;
  padding: var(--gap-3);
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  color: var(--c-fg-1);
  text-decoration: none;
  transition: border-color var(--motion-fast), background-color var(--motion-fast), color var(--motion-fast);
}
.plink:hover {
  background: var(--c-surface-2);
  border-color: var(--c-accent);
  text-decoration: none;
  color: var(--c-fg-1);
}

.plink__glyph {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  background: var(--c-bg);
  color: var(--c-accent);
  border: var(--border-w) solid var(--c-border-strong);
  font-size: var(--t-sm);
  font-weight: 600;
  letter-spacing: 0;
  text-transform: lowercase;
}
.plink__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.plink__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--t-sm);
  font-weight: 500;
  color: var(--c-fg-1);
}
.plink:hover .plink__row { color: var(--c-accent); }
.plink__arrow {
  color: var(--c-fg-3);
  font-size: var(--t-sm);
}
.plink:hover .plink__arrow { color: var(--c-accent); }
.plink__desc {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  letter-spacing: 0.02em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
