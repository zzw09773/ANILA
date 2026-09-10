<!--
  ANILA wordmark — static PNG mark (compact) or mark + subtitle (expanded).
  Assets live in public/brand; navy-on-transparent, so masthead/dark surfaces
  put a light plate behind the img (see AppHeader / LoginView).
-->
<template>
  <span class="term-logo" :class="{ 'term-logo--compact': compact }">
    <img
      class="term-logo__mark"
      :src="markSrc"
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
import { ANILA_MARK_PNG } from '../../brandAssets.js'

const props = defineProps({
  compact: { type: Boolean, default: false },
  size: { type: Number, default: 16 },
  subtitle: { type: String, default: 'CSP' },
})

const markSrc = ANILA_MARK_PNG
const imgHeight = computed(() => (
  // anila-logo.png is a 1408² pad; visible mountain ~17%. Use cropped mark.
  props.compact ? Math.max(props.size, 22) : Math.max(32, Math.min(40, props.size + 18))
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
