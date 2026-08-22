<template>
  <div class="term-empty" role="status">
    <p v-if="title" class="term-empty__title">{{ title }}</p>
    <p class="term-empty__message"><slot>{{ message }}</slot></p>
    <p v-if="next" class="term-empty__next">{{ next }}</p>
    <div v-if="$slots.action" class="term-empty__action">
      <slot name="action" />
    </div>
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, default: '' },
  message: { type: String, default: '還沒有資料' },
  next: { type: String, default: '' },
})
</script>

<style scoped>
.term-empty__title {
  margin: 0 0 var(--gap-1);
  color: var(--c-fg-1);
  font-weight: 600;
}
.term-empty__message { margin: 0; }
.term-empty__next {
  margin: var(--gap-2) 0 0;
  color: var(--c-fg-2);
}
.term-empty__action { margin-top: var(--gap-3); }
</style>
