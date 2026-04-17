<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">平台連結設定</h2>
      <button
        @click="openCreateModal"
        class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 transition"
      >
        新增連結
      </button>
    </div>

    <div v-if="pageError" class="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      {{ pageError }}
    </div>

    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">名稱</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">網址</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">排序</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="link in links" :key="link.id" class="border-b last:border-0 hover:bg-gray-50">
            <td class="px-4 py-3">
              <div class="font-medium">{{ link.name }}</div>
              <div class="text-xs text-gray-400">{{ link.description || '無描述' }}</div>
            </td>
            <td class="px-4 py-3 font-mono text-xs text-gray-500">{{ link.url }}</td>
            <td class="px-4 py-3 text-gray-500">{{ link.sort_order }}</td>
            <td class="px-4 py-3">
              <span
                class="text-xs px-2 py-0.5 rounded"
                :class="link.is_active ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-600'"
              >
                {{ link.is_active ? '啟用' : '停用' }}
              </span>
            </td>
            <td class="px-4 py-3 space-x-2">
              <button @click="openEditModal(link)" class="text-indigo-600 hover:text-indigo-800 text-xs">編輯</button>
              <button
                v-if="link.is_active"
                @click="handleDeactivate(link)"
                class="text-red-600 hover:text-red-800 text-xs"
              >
                停用
              </button>
              <button
                v-else
                @click="handleReactivate(link)"
                class="text-emerald-600 hover:text-emerald-800 text-xs"
              >
                啟用
              </button>
            </td>
          </tr>
          <tr v-if="links.length === 0">
            <td colspan="5" class="px-4 py-8 text-center text-gray-400">尚無平台連結</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">{{ editingId ? '編輯平台連結' : '新增平台連結' }}</h3>

        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">名稱</label>
            <input v-model="form.name" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">網址</label>
            <input v-model="form.url" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Icon</label>
            <input v-model="form.icon" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" placeholder="例如：workflow" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">說明</label>
            <textarea v-model="form.description" rows="2" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"></textarea>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">排序</label>
            <input v-model.number="form.sort_order" type="number" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button @click="showModal = false" class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">取消</button>
          <button
            @click="handleSubmit"
            :disabled="!form.name.trim() || !form.url.trim()"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ editingId ? '更新' : '建立' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listPlatformLinks, createPlatformLink, updatePlatformLink, deletePlatformLink } from '../api/platformLinks'

const links = ref([])
const showModal = ref(false)
const editingId = ref(null)
const form = ref({ name: '', url: '', icon: '', description: '', sort_order: 0 })
const pageError = ref('')

async function fetchLinks() {
  pageError.value = ''
  try {
    const { data } = await listPlatformLinks({ include_inactive: true })
    links.value = data
  } catch (e) {
    pageError.value = e.response?.data?.detail || '載入平台連結失敗'
  }
}

onMounted(fetchLinks)

function openCreateModal() {
  editingId.value = null
  form.value = { name: '', url: '', icon: '', description: '', sort_order: 0 }
  showModal.value = true
}

function openEditModal(link) {
  editingId.value = link.id
  form.value = {
    name: link.name,
    url: link.url,
    icon: link.icon || '',
    description: link.description || '',
    sort_order: link.sort_order || 0,
  }
  showModal.value = true
}

async function handleSubmit() {
  const payload = {
    name: form.value.name.trim(),
    url: form.value.url.trim(),
    icon: form.value.icon.trim() || null,
    description: form.value.description.trim() || null,
    sort_order: form.value.sort_order || 0,
  }
  try {
    if (editingId.value) {
      await updatePlatformLink(editingId.value, payload)
    } else {
      await createPlatformLink(payload)
    }
    showModal.value = false
    await fetchLinks()
  } catch (e) {
    alert(e.response?.data?.detail || '操作失敗')
  }
}

async function handleDeactivate(link) {
  if (!confirm(`確定要停用平台連結「${link.name}」嗎？`)) return
  try {
    await deletePlatformLink(link.id)
    await fetchLinks()
  } catch (e) {
    alert(e.response?.data?.detail || '停用平台連結失敗')
  }
}

async function handleReactivate(link) {
  try {
    await updatePlatformLink(link.id, { is_active: true })
    await fetchLinks()
  } catch (e) {
    alert(e.response?.data?.detail || '啟用平台連結失敗')
  }
}
</script>
