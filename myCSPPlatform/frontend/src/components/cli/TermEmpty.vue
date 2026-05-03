<template>
  <div class="term-empty">
    <slot>{{ message }}</slot>
  </div>
</template>

<script setup>
defineProps({ message: { type: String, default: 'no records' } })
</script>
