<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">SSO / OIDC</h2>
      <button
        @click="openCreateModal"
        class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 transition"
      >
        新增 Provider
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
            <th class="px-4 py-3 text-left text-gray-600 font-medium">類型</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">預設部門</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="provider in providers" :key="provider.id" class="border-b last:border-0 hover:bg-gray-50">
            <td class="px-4 py-3">
              <div class="font-medium">{{ provider.name }}</div>
              <div class="text-xs text-gray-400">{{ provider.button_text || '未設定按鈕文字' }}</div>
            </td>
            <td class="px-4 py-3 uppercase">{{ provider.provider_type }}</td>
            <td class="px-4 py-3 text-gray-500">{{ provider.default_department_name || '未設定' }}</td>
            <td class="px-4 py-3">
              <span class="text-xs px-2 py-0.5 rounded" :class="provider.is_active ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-600'">
                {{ provider.is_active ? '啟用' : '停用' }}
              </span>
            </td>
            <td class="px-4 py-3 space-x-2">
              <button @click="openEditModal(provider)" class="text-indigo-600 hover:text-indigo-800 text-xs">編輯</button>
              <button
                v-if="provider.is_active"
                @click="handleDeactivate(provider)"
                class="text-red-600 hover:text-red-800 text-xs"
              >
                停用
              </button>
            </td>
          </tr>
          <tr v-if="providers.length === 0">
            <td colspan="5" class="px-4 py-8 text-center text-gray-400">尚無外部認證 Provider</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
        <h3 class="text-lg font-semibold mb-4">{{ editingId ? '編輯 Provider' : '新增 Provider' }}</h3>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">名稱</label>
            <input v-model="form.name" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">類型</label>
            <select v-model="form.provider_type" disabled class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none bg-gray-100">
              <option value="oidc">OIDC</option>
            </select>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">按鈕文字</label>
            <input v-model="form.button_text" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" placeholder="例如：使用公司 SSO 登入" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">預設部門</label>
            <select v-model="form.default_department_id" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500">
              <option :value="null">未設定</option>
              <option v-for="department in departments" :key="department.id" :value="department.id">{{ department.name }}</option>
            </select>
          </div>
          <label class="flex items-center gap-2 text-sm text-gray-700">
            <input v-model="form.is_active" type="checkbox" class="rounded text-indigo-600 focus:ring-indigo-500" />
            啟用 Provider
          </label>
          <label class="flex items-center gap-2 text-sm text-gray-700">
            <input v-model="form.auto_create_users" type="checkbox" class="rounded text-indigo-600 focus:ring-indigo-500" />
            自動建立使用者
          </label>
        </div>

        <div class="mt-6">
          <h4 class="text-sm font-semibold text-gray-800 mb-3">OIDC 設定</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Issuer URL</label>
              <input v-model="form.oidc_issuer_url" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Client ID</label>
              <input v-model="form.oidc_client_id" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Client Secret</label>
              <input
                v-model="form.oidc_client_secret"
                type="password"
                class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
                :placeholder="editingId ? '保持空白即不變更（顯示為 ***）' : '請輸入 Client Secret'"
              />
              <p class="text-xs text-gray-400 mt-1">已加密儲存（AES-GCM）；後端永遠不會回傳明文。</p>
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Scopes</label>
              <input v-model="form.oidc_scopes" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" placeholder="openid profile email" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Authorization Endpoint</label>
              <input v-model="form.oidc_authorization_endpoint" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Token Endpoint</label>
              <input v-model="form.oidc_token_endpoint" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Userinfo Endpoint</label>
              <input v-model="form.oidc_userinfo_endpoint" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Username Claim</label>
              <input v-model="form.oidc_username_claim" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" placeholder="preferred_username" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Email Claim</label>
              <input v-model="form.oidc_email_claim" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" placeholder="email" />
            </div>
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Subject Claim</label>
              <input v-model="form.oidc_subject_claim" type="text" class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500" placeholder="sub" />
            </div>
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button @click="showModal = false" class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">取消</button>
          <button
            @click="handleSubmit"
            :disabled="!form.name.trim()"
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
import { listDepartments } from '../api/departments'
import { createAuthProvider, deactivateAuthProvider, listAuthProviders, updateAuthProvider } from '../api/authProviders'

const SECRET_MASK = '***'

const providers = ref([])
const departments = ref([])
const showModal = ref(false)
const editingId = ref(null)
const pageError = ref('')

const defaultForm = () => ({
  name: '',
  provider_type: 'oidc',
  button_text: '',
  is_active: true,
  auto_create_users: true,
  default_role: 'user',
  default_department_id: null,
  oidc_issuer_url: '',
  oidc_client_id: '',
  oidc_client_secret: '',
  oidc_authorization_endpoint: '',
  oidc_token_endpoint: '',
  oidc_userinfo_endpoint: '',
  oidc_scopes: 'openid profile email',
  oidc_username_claim: 'preferred_username',
  oidc_email_claim: 'email',
  oidc_subject_claim: 'sub',
})

const form = ref(defaultForm())

async function fetchData() {
  pageError.value = ''
  try {
    const [{ data: providersData }, { data: departmentsData }] = await Promise.all([
      listAuthProviders(),
      listDepartments(),
    ])
    providers.value = providersData
    departments.value = departmentsData.filter(item => item.is_active)
  } catch (e) {
    pageError.value = e.response?.data?.detail || '載入 Provider 設定失敗'
  }
}

onMounted(fetchData)

function openCreateModal() {
  editingId.value = null
  form.value = defaultForm()
  showModal.value = true
}

function openEditModal(provider) {
  editingId.value = provider.id
  // Backend 回傳 oidc_client_secret 為 SECRET_MASK 或 null；UI 編輯時保留空字串，
  // 由 handleSubmit 決定送出 SECRET_MASK（不變更）或 plaintext（替換）。
  const merged = { ...defaultForm(), ...provider }
  merged.oidc_client_secret = ''
  form.value = merged
}

async function handleSubmit() {
  const payload = { ...form.value }
  // 編輯模式且使用者沒輸入新 secret → 送 SECRET_MASK 表示「不變更」。
  // 新增模式或使用者輸入新值 → 直接送 plaintext，後端會加密落地。
  if (editingId.value && !payload.oidc_client_secret) {
    payload.oidc_client_secret = SECRET_MASK
  }
  try {
    if (editingId.value) {
      await updateAuthProvider(editingId.value, payload)
    } else {
      await createAuthProvider(payload)
    }
    showModal.value = false
    await fetchData()
  } catch (e) {
    alert(e.response?.data?.detail || '儲存 Provider 失敗')
  }
}

async function handleDeactivate(provider) {
  if (!confirm(`確定要停用 Provider「${provider.name}」嗎？`)) return
  try {
    await deactivateAuthProvider(provider.id)
    await fetchData()
  } catch (e) {
    alert(e.response?.data?.detail || '停用 Provider 失敗')
  }
}
</script>
