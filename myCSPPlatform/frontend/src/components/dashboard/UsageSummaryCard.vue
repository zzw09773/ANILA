<template>
  <div class="bg-white rounded-xl border border-gray-200 p-5">
    <p class="text-sm text-gray-500">{{ label }}</p>
    <p class="text-2xl font-bold text-gray-900 mt-1">{{ formattedValue }}</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: Number, default: 0 },
})

const formattedValue = computed(() => {
  if (props.value >= 1000000) return (props.value / 1000000).toFixed(1) + 'M'
  if (props.value >= 1000) return (props.value / 1000).toFixed(1) + 'K'
  return props.value.toLocaleString()
})
</script>
