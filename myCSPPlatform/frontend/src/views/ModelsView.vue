<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">模型管理</h2>
      <button
        v-if="authStore.isAdmin"
        @click="openCreateModal"
        class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 transition"
      >
        註冊模型
      </button>
    </div>

    <!-- Models Table -->
    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">名稱</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">類型</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">端點</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">API 版本</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">啟用</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">主路由</th>
            <th v-if="authStore.isAdmin" class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="model in modelsStore.models" :key="model.id" class="border-b last:border-0 hover:bg-gray-50">
            <td class="px-4 py-3">
              <span
                class="inline-block w-3 h-3 rounded-full"
                :class="{
                  'bg-green-500': model.health_status === 'online',
                  'bg-yellow-500': model.health_status === 'connecting',
                  'bg-red-500': model.health_status === 'offline',
                }"
                :title="healthLabel(model.health_status)"
              ></span>
            </td>
            <td class="px-4 py-3">
              <div class="font-medium">{{ model.display_name }}</div>
              <div class="text-xs text-gray-400">{{ model.name }}</div>
              <div v-if="model.base_model_name" class="text-xs text-indigo-500 mt-0.5">
                底層: {{ model.base_model_name }}
              </div>
            </td>
            <td class="px-4 py-3">
              <span class="text-xs px-2 py-0.5 rounded" :class="typeColor(model.model_type)">
                {{ model.model_type.toUpperCase() }}
              </span>
            </td>
            <td class="px-4 py-3 font-mono text-xs text-gray-500 max-w-[200px] truncate">
              {{ model.endpoint_url }}
            </td>
            <td class="px-4 py-3 text-gray-500">{{ model.api_version }}</td>
            <td class="px-4 py-3">
              <span :class="model.is_active ? 'text-green-600' : 'text-red-600'" class="text-xs">
                {{ model.is_active ? '啟用' : '停用' }}
              </span>
            </td>
            <td class="px-4 py-3">
              <span
                v-if="model.is_router_primary"
                class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-amber-100 text-amber-800"
                title="ANILA Router 會使用此模型作為主路由 LLM"
              >
                ★ 主路由
              </span>
              <span v-else class="text-xs text-gray-300">—</span>
            </td>
            <td v-if="authStore.isAdmin" class="px-4 py-3 space-x-2 whitespace-nowrap">
              <button @click="openEditModal(model)" class="text-indigo-600 hover:text-indigo-800 text-xs">
                編輯
              </button>
              <button @click="handleHealthCheck(model.id)" class="text-blue-600 hover:text-blue-800 text-xs">
                檢查
              </button>
              <button
                v-if="model.model_type === 'llm' && !model.is_router_primary"
                :disabled="!model.is_active || settingPrimaryId === model.id"
                @click="handleSetPrimary(model.id)"
                class="text-amber-600 hover:text-amber-800 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
                :title="model.is_active ? '設為 ANILA 主路由模型' : '需先啟用模型'"
              >
                {{ settingPrimaryId === model.id ? '設定中…' : '設為主路由' }}
              </button>
              <button
                v-else-if="model.is_router_primary"
                :disabled="settingPrimaryId === model.id"
                @click="handleUnsetPrimary(model.id)"
                class="text-gray-500 hover:text-gray-700 text-xs disabled:opacity-40"
              >
                取消主路由
              </button>
              <button
                v-if="model.is_active"
                @click="handleDeactivate(model.id)"
                class="text-yellow-600 hover:text-yellow-800 text-xs"
              >
                停用
              </button>
              <button
                @click="handlePurge(model)"
                :disabled="purgingId === model.id"
                class="text-red-600 hover:text-red-800 text-xs disabled:opacity-50"
              >
                {{ purgingId === model.id ? '刪除中…' : '刪除' }}
              </button>
            </td>
          </tr>
          <tr v-if="modelsStore.models.length === 0">
            <td :colspan="authStore.isAdmin ? 8 : 7" class="px-4 py-8 text-center text-gray-400">
              尚無已註冊模型
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create/Edit Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">{{ editingId ? '編輯模型' : '註冊新模型' }}</h3>

        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">模型名稱（ID）</label>
            <input v-model="form.name" :disabled="!!editingId" type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none disabled:bg-gray-100"
              placeholder="例如：llama3-70b" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">顯示名稱</label>
            <input v-model="form.display_name" type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="例如：Llama 3 70B Instruct" />
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">模型類型</label>
              <select v-model="form.model_type"
                class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none">
                <option value="llm">LLM</option>
                <option value="vlm">VLM</option>
                <option value="embedding">Embedding</option>
                <option value="agent">Agent</option>
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">API 版本</label>
              <select v-model="form.api_version"
                class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none">
                <option value="v1">v1</option>
                <option value="v2">v2</option>
              </select>
            </div>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">端點 URL</label>
            <input v-model="form.endpoint_url" type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="例如：http://gpu-server:8080" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">說明</label>
            <textarea v-model="form.description" rows="2"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="選填"></textarea>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Context Window</label>
            <input v-model.number="form.context_window" type="number"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="選填，例如 128000" />
          </div>
          <div v-if="form.model_type === 'agent'">
            <label class="block text-sm font-medium text-gray-700 mb-1">底層模型</label>
            <select v-model="form.base_model_id"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none">
              <option :value="null">無（獨立部署）</option>
              <option
                v-for="m in baseModelOptions"
                :key="m.id"
                :value="m.id"
              >{{ m.display_name }} ({{ m.model_type.toUpperCase() }})</option>
            </select>
            <p class="text-xs text-gray-400 mt-1">選擇此 Agent 所使用的底層模型，用於關聯統計</p>
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button @click="showModal = false"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">
            取消
          </button>
          <button @click="handleSubmit" :disabled="!form.name || !form.display_name || !form.endpoint_url"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50">
            {{ editingId ? '更新' : '建立' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useModelsStore } from '../stores/models'
import { useAuthStore } from '../stores/auth'

const modelsStore = useModelsStore()
const authStore = useAuthStore()
const showModal = ref(false)
const editingId = ref(null)
const purgingId = ref(null)
const settingPrimaryId = ref(null)

const defaultForm = () => ({
  name: '', display_name: '', model_type: 'llm', endpoint_url: '',
  api_version: 'v1', description: '', context_window: null, base_model_id: null,
})
const form = ref(defaultForm())

// Non-agent models available as base model options (exclude current editing model)
const baseModelOptions = computed(() =>
  modelsStore.models.filter(m =>
    m.model_type !== 'agent' && m.is_active && m.id !== editingId.value
  )
)

onMounted(() => modelsStore.fetchModels())

function typeColor(type) {
  const colors = {
    llm: 'bg-blue-50 text-blue-700',
    vlm: 'bg-purple-50 text-purple-700',
    embedding: 'bg-green-50 text-green-700',
    agent: 'bg-orange-50 text-orange-700',
  }
  return colors[type] || 'bg-gray-50 text-gray-700'
}

function healthLabel(status) {
  const labels = { online: '運行中', connecting: '連線中', offline: '斷開' }
  return labels[status] || status
}

function openCreateModal() {
  editingId.value = null
  form.value = defaultForm()
  showModal.value = true
}

function openEditModal(model) {
  editingId.value = model.id
  form.value = {
    name: model.name, display_name: model.display_name,
    model_type: model.model_type, endpoint_url: model.endpoint_url,
    api_version: model.api_version, description: model.description || '',
    context_window: model.context_window, base_model_id: model.base_model_id || null,
  }
  showModal.value = true
}

async function handleSubmit() {
  try {
    const payload = { ...form.value }
    // Clear base_model_id if not agent type
    if (payload.model_type !== 'agent') {
      payload.base_model_id = null
    }
    if (editingId.value) {
      const { name, ...updateData } = payload
      await modelsStore.update(editingId.value, updateData)
    } else {
      await modelsStore.create(payload)
    }
    showModal.value = false
  } catch (e) {
    alert(e.response?.data?.detail || '操作失敗')
  }
}

async function handleHealthCheck(id) {
  const result = await modelsStore.checkHealth(id)
  alert(`健康檢查結果: ${result.status}\n${result.detail}`)
}

async function handleSetPrimary(id) {
  settingPrimaryId.value = id
  try {
    await modelsStore.setPrimary(id)
  } catch (e) {
    alert(e.response?.data?.detail || '設定失敗')
  } finally {
    settingPrimaryId.value = null
  }
}

async function handleUnsetPrimary(id) {
  if (!confirm('取消後 ANILA Router 將沒有主路由模型，直到你重新指定一個。確定要取消嗎？')) return
  settingPrimaryId.value = id
  try {
    await modelsStore.unsetPrimary(id)
  } catch (e) {
    alert(e.response?.data?.detail || '取消失敗')
  } finally {
    settingPrimaryId.value = null
  }
}

async function handleDeactivate(id) {
  if (confirm('確定要停用此模型嗎？（停用後可透過編輯再次啟用）')) {
    await modelsStore.remove(id)
  }
}

async function handlePurge(model) {
  if (!model || purgingId.value === model.id) {
    return
  }
  if (!window.confirm(
    `確定要永久刪除模型「${model.display_name}」？\n` +
    `此操作無法復原。若此模型已有用量紀錄或被其他模型引用，將會被拒絕。`
  )) {
    return
  }
  purgingId.value = model.id
  try {
    await modelsStore.purge(model.id)
  } catch (e) {
    alert(e.response?.data?.detail || '刪除模型失敗')
  } finally {
    purgingId.value = null
  }
}
</script>
