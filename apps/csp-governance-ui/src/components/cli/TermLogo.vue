<!--
  ANILA wordmark — static PNG mark (compact) or mark + subtitle (expanded).
  Assets live in public/brand; navy-on-transparent, so masthead/dark surfaces
  put a light plate behind the img (see AppHeader / LoginView).
-->
<template>
  <span class="term-logo" :class="{ 'term-logo--compact': compact }">
    <img
      class="term-logo__mark"
      :src="compact ? markSrc : logoSrc"
      alt="ANILA"
      :style="{ height: imgHeight + 'px' }"
      draggable="false"
    />
    <span v-if="!compact" class="term-logo__word">
      <span class="term-logo__sub">{{ subtitle }}</span>
    </span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { ANILA_LOGO_PNG, ANILA_MARK_PNG } from '../../brandAssets.js'

const props = defineProps({
  compact: { type: Boolean, default: false },
  size: { type: Number, default: 16 },
  subtitle: { type: String, default: 'CSP' },
})

const markSrc = ANILA_MARK_PNG
const logoSrc = ANILA_LOGO_PNG
const imgHeight = computed(() => (
  props.compact ? Math.max(props.size, 16) : Math.min(36, Math.max(28, props.size * 2))
))
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
  display: block;
  width: auto;
  object-fit: contain;
}
.term-logo__word {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  color: var(--c-fg-1);
  font-family: var(--font-display);
  font-weight: 700;
  font-size: var(--t-lg);
  line-height: 1;
}
.term-logo__sub {
  color: var(--c-fg-2);
  font-size: var(--t-md);
  letter-spacing: 0.04em;
}
</style>
