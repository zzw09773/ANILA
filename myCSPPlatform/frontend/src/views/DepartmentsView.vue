<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">部門設定</h2>
      <button
        @click="openCreateModal"
        class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 transition"
      >
        新增部門
      </button>
    </div>

    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">名稱</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">說明</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">使用者</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">建立日期</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="department in departments"
            :key="department.id"
            class="border-b last:border-0 hover:bg-gray-50"
          >
            <td class="px-4 py-3 font-medium">{{ department.name }}</td>
            <td class="px-4 py-3 text-gray-500">{{ department.description || '-' }}</td>
            <td class="px-4 py-3 text-gray-500">
              {{ department.active_user_count }} / {{ department.user_count }}
            </td>
            <td class="px-4 py-3">
              <span
                class="text-xs px-2 py-0.5 rounded"
                :class="department.is_active ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-600'"
              >
                {{ department.is_active ? '啟用' : '停用' }}
              </span>
            </td>
            <td class="px-4 py-3 text-gray-500">{{ formatDate(department.created_at) }}</td>
            <td class="px-4 py-3 space-x-2">
              <button
                @click="openEditModal(department)"
                class="text-indigo-600 hover:text-indigo-800 text-xs"
              >
                編輯
              </button>
              <button
                v-if="department.is_active"
                @click="handleDeactivate(department)"
                class="text-red-600 hover:text-red-800 text-xs"
              >
                停用
              </button>
              <button
                v-else
                @click="handleReactivate(department)"
                class="text-emerald-600 hover:text-emerald-800 text-xs"
              >
                啟用
              </button>
            </td>
          </tr>
          <tr v-if="departments.length === 0">
            <td colspan="6" class="px-4 py-8 text-center text-gray-400">尚無部門</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-md w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">{{ editingId ? '編輯部門' : '新增部門' }}</h3>

        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">部門名稱</label>
            <input
              v-model="form.name"
              type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="例如：研發部"
            />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">說明</label>
            <textarea
              v-model="form.description"
              rows="3"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="選填"
            ></textarea>
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button
            @click="showModal = false"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleSubmit"
            :disabled="!form.name.trim() || saving"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ saving ? '儲存中...' : (editingId ? '更新' : '建立') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import {
  listDepartments,
  createDepartment,
  updateDepartment,
  deactivateDepartment,
} from '../api/departments'

const departments = ref([])
const showModal = ref(false)
const editingId = ref(null)
const saving = ref(false)
const form = ref({ name: '', description: '' })

async function fetchDepartments() {
  const { data } = await listDepartments()
  departments.value = data
}

onMounted(fetchDepartments)

function openCreateModal() {
  editingId.value = null
  form.value = { name: '', description: '' }
  showModal.value = true
}

function openEditModal(department) {
  editingId.value = department.id
  form.value = {
    name: department.name,
    description: department.description || '',
  }
  showModal.value = true
}

async function handleSubmit() {
  saving.value = true
  try {
    const payload = {
      name: form.value.name.trim(),
      description: form.value.description.trim() || null,
    }
    if (editingId.value) {
      await updateDepartment(editingId.value, payload)
    } else {
      await createDepartment(payload)
    }
    showModal.value = false
    await fetchDepartments()
  } catch (e) {
    alert(e.response?.data?.detail || '操作失敗')
  } finally {
    saving.value = false
  }
}

async function handleDeactivate(department) {
  if (!confirm(`確定要停用部門「${department.name}」嗎？目前綁定使用者會被解除部門設定。`)) {
    return
  }
  await deactivateDepartment(department.id)
  await fetchDepartments()
}

async function handleReactivate(department) {
  await updateDepartment(department.id, { is_active: true })
  await fetchDepartments()
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleString('zh-TW')
}
</script>
