<!--
  ANILA lockup — 院線 mark + Latin wordmark. One lockup only:
  ANILA · 院內 AI 工作平台. No decorative diamond.
-->
<template>
  <a class="anila-lockup" :href="href" :aria-label="lockup">
    <span class="anila-lockup__mark" aria-hidden="true" />
    <span v-if="!compact" class="anila-lockup__word">
      <span class="anila-lockup__brand">{{ brand }}</span>
      <span class="anila-lockup__sep">·</span>
      <span class="anila-lockup__line">{{ line }}</span>
    </span>
  </a>
</template>

<script setup>
import { ANILA_LOCKUP, ANILA_LOCKUP_BRAND, ANILA_LOCKUP_LINE } from '../../../../shared/product.js'
import { shellWorkbenchHref } from '../../utils/appOrigins'

defineProps({
  compact: { type: Boolean, default: false },
  href: { type: String, default: () => shellWorkbenchHref() },
})

const brand = ANILA_LOCKUP_BRAND
const line = ANILA_LOCKUP_LINE
const lockup = ANILA_LOCKUP
</script>
