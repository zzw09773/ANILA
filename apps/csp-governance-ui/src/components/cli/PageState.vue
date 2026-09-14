<template>
  <div v-if="loading" class="page-state page-state--loading" role="status">
    {{ loadingLabel }}
  </div>
  <div v-else-if="error" class="page-state page-state--error" role="alert">
    <p>{{ error }}</p>
    <div v-if="$slots.retry" class="page-state__action"><slot name="retry" /></div>
  </div>
  <div v-else-if="empty" class="page-state page-state--empty">
    <p class="page-state__title">{{ emptyTitle }}</p>
    <p v-if="emptyHint" class="page-state__hint">{{ emptyHint }}</p>
    <div v-if="$slots.action" class="page-state__action"><slot name="action" /></div>
  </div>
  <slot v-else />
</template>

<script setup>
defineProps({
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  empty: { type: Boolean, default: false },
  loadingLabel: { type: String, default: '載入中…' },
  emptyTitle: { type: String, default: '目前沒有資料' },
  emptyHint: { type: String, default: '' },
})
</script>
