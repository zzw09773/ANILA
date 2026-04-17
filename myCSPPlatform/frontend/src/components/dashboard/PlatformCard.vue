<template>
  <a
    :href="link.url"
    target="_blank"
    rel="noopener"
    class="block bg-white rounded-xl border border-gray-200 p-5 hover:shadow-lg hover:border-indigo-300 transition-all group"
  >
    <div class="flex items-center space-x-4">
      <div class="w-12 h-12 rounded-lg bg-indigo-50 flex items-center justify-center text-2xl group-hover:bg-indigo-100 transition-colors">
        {{ iconMap[link.icon] || '🔗' }}
      </div>
      <div>
        <h3 class="font-semibold text-gray-900 group-hover:text-indigo-600 transition-colors">
          {{ link.name }}
        </h3>
        <p class="text-sm text-gray-500 mt-0.5">{{ link.description }}</p>
      </div>
    </div>
  </a>
</template>

<script setup>
defineProps({
  link: { type: Object, required: true },
})

const iconMap = {
  workflow: '⚡',
  git: '📦',
  notebook: '📓',
  chat: '💬',
  monitor: '📊',
  database: '🗄️',
  api: '🔌',
  docs: '📄',
}
</script>
