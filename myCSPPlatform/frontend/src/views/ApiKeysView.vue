<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">API Key 管理</h2>
      <button
        @click="showCreateModal = true"
        class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 transition"
      >
        建立 API Key
      </button>
    </div>

    <!-- API Keys Table -->
    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">名稱</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">Key</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">允許模型</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">建立日期</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">最後使用</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="key in keysStore.keys" :key="key.id" class="border-b last:border-0 hover:bg-gray-50">
            <td class="px-4 py-3 font-medium">{{ key.name }}</td>
            <td class="px-4 py-3 font-mono text-xs text-gray-500">
              {{ key.key_prefix }}...{{ key.key_suffix }}
            </td>
            <td class="px-4 py-3">
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="name in key.allowed_model_names"
                  :key="name"
                  class="text-xs px-2 py-0.5 bg-indigo-50 text-indigo-700 rounded"
                >
                  {{ name }}
                </span>
                <span v-if="key.allowed_model_names.length === 0" class="text-xs text-gray-400">
                  無
                </span>
              </div>
            </td>
            <td class="px-4 py-3 text-gray-500">{{ formatDate(key.created_at) }}</td>
            <td class="px-4 py-3 text-gray-500">{{ key.last_used_at ? formatDate(key.last_used_at) : '從未使用' }}</td>
            <td class="px-4 py-3">
              <span
                class="text-xs px-2 py-0.5 rounded"
                :class="key.is_active ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'"
              >
                {{ key.is_active ? '啟用' : '已撤銷' }}
              </span>
            </td>
            <td class="px-4 py-3 flex items-center gap-3">
              <button
                v-if="key.is_active"
                @click="confirmRegenerate(key)"
                class="text-indigo-600 hover:text-indigo-800 text-xs"
              >
                重新核發
              </button>
              <button
                v-if="key.is_active"
                @click="confirmRevoke(key)"
                class="text-red-600 hover:text-red-800 text-xs"
              >
                撤銷
              </button>
            </td>
          </tr>
          <tr v-if="keysStore.keys.length === 0">
            <td colspan="7" class="px-4 py-8 text-center text-gray-400">尚無 API Key</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create Key Modal -->
    <div v-if="showCreateModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showCreateModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">建立新的 API Key</h3>

        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">名稱</label>
            <input
              v-model="newKey.name"
              type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="例如：開發測試用"
            />
          </div>

          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">過期時間（選填）</label>
            <input
              v-model="newKey.expires_at"
              type="datetime-local"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
            />
            <p class="text-xs text-gray-400 mt-1">留空表示不限期</p>
          </div>

          <!-- Admin: choose models -->
          <div v-if="authStore.isAdmin">
            <label class="block text-sm font-medium text-gray-700 mb-2">允許使用的模型</label>
            <div class="space-y-2 max-h-48 overflow-y-auto border border-gray-200 rounded-lg p-3">
              <label
                v-for="model in allModels"
                :key="model.id"
                class="flex items-center space-x-2 cursor-pointer"
              >
                <input
                  type="checkbox"
                  :value="model.id"
                  v-model="newKey.model_ids"
                  class="rounded text-indigo-600 focus:ring-indigo-500"
                />
                <span class="text-sm">{{ model.display_name }}</span>
                <span class="text-xs text-gray-400">({{ model.model_type }})</span>
              </label>
              <p v-if="allModels.length === 0" class="text-sm text-gray-400">尚無已註冊的模型</p>
            </div>
          </div>

          <!-- Non-admin: read-only allowlist -->
          <div v-else>
            <label class="block text-sm font-medium text-gray-700 mb-2">你的可用模型</label>
            <div class="border border-gray-200 rounded-lg p-3 bg-gray-50">
              <div class="flex flex-wrap gap-2" v-if="myAllowedModels.length > 0">
                <span
                  v-for="m in myAllowedModels"
                  :key="m.id"
                  class="text-xs px-2 py-1 bg-indigo-50 text-indigo-700 rounded"
                >
                  {{ m.display_name }}
                </span>
              </div>
              <p v-else class="text-sm text-gray-400">尚未被指派任何可用模型，請聯絡管理員</p>
            </div>
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button
            @click="showCreateModal = false"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleCreate"
            :disabled="!canCreate"
            :title="!canCreate ? createDisabledReason : ''"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {{ creating ? '建立中...' : '建立' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Show Key Modal (one-time display) -->
    <div v-if="showKeyModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold mb-2">{{ keyModalTitle }}</h3>
        <p class="text-sm text-red-600 mb-4">
          請立即複製此 Key，關閉後將無法再次查看！
        </p>

        <div class="bg-gray-100 p-4 rounded-lg font-mono text-sm break-all select-all">
          {{ createdFullKey }}
        </div>

        <div class="flex items-center justify-between mt-6">
          <button
            @click="copyKey"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
          >
            {{ copied ? '已複製！' : '複製 Key' }}
          </button>
          <button
            @click="closeKeyModal"
            :disabled="!hasCopied"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
            :title="!hasCopied ? '請先點擊「複製 Key」' : ''"
          >
            我已複製，關閉
          </button>
        </div>
      </div>
    </div>

    <!-- Revoke Confirm -->
    <ConfirmDialog
      :visible="showRevokeConfirm"
      title="撤銷 API Key"
      :message="`確定要撤銷 API Key「${revokeTarget?.name}」嗎？此操作無法復原。`"
      confirm-text="撤銷"
      :danger="true"
      @confirm="handleRevoke"
      @cancel="showRevokeConfirm = false"
    />

    <!-- Regenerate Confirm -->
    <ConfirmDialog
      :visible="showRegenerateConfirm"
      title="重新核發 API Key"
      :message="`確定要重新核發「${regenerateTarget?.name}」嗎？舊的 Key 將立即失效，系統會產生新的 Key。`"
      confirm-text="重新核發"
      :danger="false"
      @confirm="handleRegenerate"
      @cancel="showRegenerateConfirm = false"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useApiKeysStore } from '../stores/apiKeys'
import { useAuthStore } from '../stores/auth'
import { listModels } from '../api/models'
import { getMyAllowedModels } from '../api/users'
import ConfirmDialog from '../components/common/ConfirmDialog.vue'

const keysStore = useApiKeysStore()
const authStore = useAuthStore()

const allModels = ref([])
const myAllowedModels = ref([])

const showCreateModal = ref(false)
const showKeyModal = ref(false)
const showRevokeConfirm = ref(false)
const showRegenerateConfirm = ref(false)
const revokeTarget = ref(null)
const regenerateTarget = ref(null)
const creating = ref(false)
const createdFullKey = ref('')
const keyModalTitle = ref('API Key 已建立')
const copied = ref(false)
const hasCopied = ref(false)

const newKey = ref({
  name: '',
  model_ids: [],
  expires_at: '',
})

// Creation is gated on the same invariants the backend now enforces:
// trimmed non-empty name AND at least one allowed model. Non-admin users
// implicitly inherit their allowed_models, so the empty-list check only
// applies to admin-path explicit selection.
const canCreate = computed(() => {
  if (creating.value) return false
  if (!(newKey.value.name || '').trim()) return false
  if (authStore.isAdmin) {
    if ((newKey.value.model_ids || []).length === 0) return false
  } else {
    if (myAllowedModels.value.length === 0) return false
  }
  return true
})

const createDisabledReason = computed(() => {
  if (!(newKey.value.name || '').trim()) return '名稱不可為空白'
  if (authStore.isAdmin && (newKey.value.model_ids || []).length === 0) return '至少勾選一個可用模型'
  if (!authStore.isAdmin && myAllowedModels.value.length === 0) return '尚未被指派任何可用模型，請聯絡管理員'
  return ''
})

onMounted(async () => {
  await keysStore.fetchKeys()
  try {
    const { data } = await listModels()
    allModels.value = data
  } catch {}
  if (!authStore.isAdmin) {
    try {
      const { data } = await getMyAllowedModels()
      myAllowedModels.value = data
    } catch {}
  }
})

async function handleCreate() {
  if (!canCreate.value) return
  creating.value = true
  try {
    const payload = {
      name: (newKey.value.name || '').trim(),
      // Non-admin path: server injects the user's allowed_models; send
      // an empty list here and let the backend fill it, matching how the
      // POST /api/keys endpoint already behaves.
      model_ids: authStore.isAdmin ? newKey.value.model_ids : myAllowedModels.value.map(m => m.id),
      expires_at: newKey.value.expires_at || null,
    }
    const data = await keysStore.create(payload)
    createdFullKey.value = data.full_key
    keyModalTitle.value = 'API Key 已建立'
    showCreateModal.value = false
    showKeyModal.value = true
    copied.value = false
    hasCopied.value = false
    newKey.value = { name: '', model_ids: [], expires_at: '' }
  } catch (e) {
    alert(e.response?.data?.detail || '建立失敗')
  } finally {
    creating.value = false
  }
}

function copyKey() {
  const text = createdFullKey.value
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text)
  } else {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } finally { document.body.removeChild(ta) }
  }
  copied.value = true
  hasCopied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}

function closeKeyModal() {
  showKeyModal.value = false
  createdFullKey.value = ''
}

function confirmRevoke(key) {
  revokeTarget.value = key
  showRevokeConfirm.value = true
}

async function handleRevoke() {
  if (revokeTarget.value) {
    await keysStore.revoke(revokeTarget.value.id)
  }
  showRevokeConfirm.value = false
  revokeTarget.value = null
}

function confirmRegenerate(key) {
  regenerateTarget.value = key
  showRegenerateConfirm.value = true
}

async function handleRegenerate() {
  showRegenerateConfirm.value = false
  if (!regenerateTarget.value) return
  try {
    const data = await keysStore.regenerate(regenerateTarget.value.id)
    createdFullKey.value = data.full_key
    keyModalTitle.value = '新的 API Key 已核發'
    showKeyModal.value = true
    copied.value = false
    hasCopied.value = false
  } catch (e) {
    alert(e.response?.data?.detail || '重新核發失敗')
  } finally {
    regenerateTarget.value = null
  }
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleString('zh-TW')
}
</script>
