<template>
  <header class="page-head">
    <div class="page-head__copy">
      <h1 class="page-head__title">{{ title }}</h1>
      <p v-if="subtitle" class="page-head__sub">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.actions" class="page-head__actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup>
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
})
</script>
