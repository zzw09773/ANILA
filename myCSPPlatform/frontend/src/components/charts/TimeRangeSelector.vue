<template>
  <div class="flex space-x-2">
    <button
      v-for="option in options"
      :key="option.value"
      @click="$emit('update:modelValue', option.value)"
      class="px-3 py-1.5 text-sm rounded-lg transition-colors"
      :class="modelValue === option.value
        ? 'bg-indigo-600 text-white'
        : 'bg-white border border-gray-300 text-gray-600 hover:bg-gray-50'"
    >
      {{ option.label }}
    </button>
  </div>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: '24h' },
})

defineEmits(['update:modelValue'])

const options = [
  { value: '4h', label: '4 小時' },
  { value: '12h', label: '12 小時' },
  { value: '24h', label: '24 小時' },
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
]
</script>
