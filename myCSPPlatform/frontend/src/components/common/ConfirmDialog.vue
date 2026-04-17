<template>
  <div v-if="visible" class="fixed inset-0 z-50 flex items-center justify-center">
    <div class="fixed inset-0 bg-black/50" @click="$emit('cancel')"></div>
    <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-md w-full mx-4">
      <h3 class="text-lg font-semibold text-gray-900">{{ title }}</h3>
      <p class="text-gray-600 mt-2">{{ message }}</p>
      <div class="flex justify-end space-x-3 mt-6">
        <button
          @click="$emit('cancel')"
          class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
        >
          取消
        </button>
        <button
          @click="$emit('confirm')"
          class="px-4 py-2 text-sm rounded-lg text-white"
          :class="danger ? 'bg-red-600 hover:bg-red-700' : 'bg-indigo-600 hover:bg-indigo-700'"
        >
          {{ confirmText }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: '確認' },
  message: { type: String, default: '' },
  confirmText: { type: String, default: '確認' },
  danger: { type: Boolean, default: false },
})

defineEmits(['confirm', 'cancel'])
</script>
