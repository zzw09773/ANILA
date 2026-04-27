<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
      <div>
        <h2 class="text-lg font-semibold">使用者管理</h2>
        <p class="mt-0.5 text-sm text-gray-500">管理角色、啟用狀態、可用模型與可用 Agent。</p>
      </div>
      <button
        @click="openCreateModal"
        class="rounded-lg bg-indigo-600 px-4 py-2 text-sm text-white hover:bg-indigo-700 transition"
      >
        新增使用者
      </button>
    </div>

    <div
      v-if="feedback.message"
      class="rounded-xl border px-4 py-3 text-sm"
      :class="feedback.type === 'error'
        ? 'border-red-200 bg-red-50 text-red-700'
        : 'border-green-200 bg-green-50 text-green-700'"
    >
      {{ feedback.message }}
    </div>

    <div class="grid gap-3 md:grid-cols-4">
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Total</div>
        <div class="mt-2 text-2xl font-semibold">{{ users.length }}</div>
      </div>
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Pending</div>
        <div class="mt-2 text-2xl font-semibold text-yellow-700">{{ pendingCount }}</div>
      </div>
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Developers</div>
        <div class="mt-2 text-2xl font-semibold text-indigo-700">{{ developerCount }}</div>
      </div>
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Active</div>
        <div class="mt-2 text-2xl font-semibold text-green-700">{{ activeCount }}</div>
      </div>
    </div>

    <div class="grid gap-3 rounded-xl border border-gray-200 bg-white p-4 lg:grid-cols-4">
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">搜尋</span>
        <input
          v-model="filters.query"
          type="text"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="帳號、Email、部門"
        />
      </label>
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">角色</span>
        <select
          v-model="filters.role"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="all">全部</option>
          <option value="user">使用者</option>
          <option value="developer">開發者</option>
          <option value="admin">管理員</option>
        </select>
      </label>
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">狀態</span>
        <select
          v-model="filters.status"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="all">全部</option>
          <option value="pending">待核准</option>
          <option value="active">啟用</option>
          <option value="inactive">停用</option>
        </select>
      </label>
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">排序</span>
        <select
          v-model="filters.sort"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="newest">最新優先</option>
          <option value="oldest">最舊優先</option>
          <option value="name">帳號 A-Z</option>
        </select>
      </label>
    </div>

    <div
      v-if="selectedUserIds.length"
      class="flex flex-col gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900 lg:flex-row lg:items-center lg:justify-between"
    >
      <div>已選取 {{ selectedUserIds.length }} 位使用者。批次操作會逐筆呼叫既有 API。</div>
      <div class="flex flex-wrap gap-2">
        <button
          @click="handleBulkApprove"
          class="rounded-lg border border-green-300 bg-white px-3 py-2 text-xs font-medium text-green-700 hover:bg-green-50"
        >
          批次核准
        </button>
        <button
          @click="handleBulkDeactivate"
          class="rounded-lg border border-red-300 bg-white px-3 py-2 text-xs font-medium text-red-700 hover:bg-red-50"
        >
          批次停用
        </button>
        <button
          @click="selectedUserIds = []"
          class="rounded-lg border border-gray-300 bg-white px-3 py-2 text-xs text-gray-700 hover:bg-gray-50"
        >
          清除選取
        </button>
      </div>
    </div>

    <div class="overflow-hidden rounded-xl border border-gray-200 bg-white">
      <div v-if="loading" class="px-4 py-10 text-center text-sm text-gray-400">載入使用者清單中...</div>
      <div v-else-if="filteredUsers.length === 0" class="px-4 py-10 text-center text-sm text-gray-400">
        {{ users.length === 0 ? '尚無使用者。' : '沒有符合目前篩選條件的使用者。' }}
      </div>
      <div v-else class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="border-b bg-gray-50">
            <tr>
              <th class="px-4 py-3 text-left">
                <input
                  type="checkbox"
                  :checked="allVisibleSelected"
                  @change="toggleSelectAll($event.target.checked)"
                />
              </th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">帳號</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">Email</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">部門</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">角色</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">狀態</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">上次登入</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">建立日期</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="user in filteredUsers"
              :key="user.id"
              class="border-b last:border-0 hover:bg-gray-50"
            >
              <td class="px-4 py-3">
                <input
                  type="checkbox"
                  :checked="selectedUserIds.includes(user.id)"
                  @change="toggleUserSelection(user.id, $event.target.checked)"
                />
              </td>
              <td class="px-4 py-3">
                <div class="font-medium">{{ user.username }}</div>
                <div class="mt-1 text-xs text-gray-400">ID: {{ user.id }}</div>
              </td>
              <td class="px-4 py-3 text-gray-500">{{ user.email || '-' }}</td>
              <td class="px-4 py-3 text-gray-500">{{ user.department_name || '未設定' }}</td>
              <td class="px-4 py-3">
                <span
                  class="rounded px-2 py-0.5 text-xs"
                  :class="rolePillClass(user.role)"
                >
                  {{ roleLabel(user.role) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span
                  class="rounded px-2 py-0.5 text-xs"
                  :class="statusPillClass(user)"
                >
                  {{ statusLabel(user) }}
                </span>
              </td>
              <td class="px-4 py-3 text-gray-500">{{ user.last_login_at ? formatDate(user.last_login_at) : '從未登入' }}</td>
              <td class="px-4 py-3 text-gray-500">{{ formatDate(user.created_at) }}</td>
              <td class="px-4 py-3">
                <div class="flex flex-wrap gap-2 text-xs">
                  <button
                    v-if="!user.is_approved"
                    @click="handleApprove(user)"
                    class="font-medium text-green-600 hover:text-green-800"
                  >
                    核准
                  </button>
                  <button @click="openEditModal(user)" class="text-indigo-600 hover:text-indigo-800">編輯</button>
                  <button @click="openAllowedModelsModal(user)" class="text-teal-600 hover:text-teal-800">可用模型</button>
                  <button @click="openAllowedAgentsModal(user)" class="text-indigo-600 hover:text-indigo-800">可用 Agent</button>
                  <button @click="openResetPasswordModal(user)" class="text-orange-600 hover:text-orange-800">重設密碼</button>
                  <button
                    v-if="!user.local_password_disabled"
                    @click="handleToggleSsoOnly(user, true)"
                    class="text-purple-600 hover:text-purple-800"
                    title="切換為 SSO-only：拒絕本機密碼登入，只能透過 OIDC 進站"
                  >
                    切 SSO-only
                  </button>
                  <button
                    v-else
                    @click="handleToggleSsoOnly(user, false)"
                    class="text-purple-600 hover:text-purple-800"
                    title="允許本機密碼登入"
                  >
                    解除 SSO-only
                  </button>
                  <button
                    v-if="user.is_active && user.is_approved"
                    @click="handleDeactivate(user)"
                    class="text-red-600 hover:text-red-800"
                  >
                    停用
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showModal = false"></div>
      <div class="relative mx-4 w-full max-w-lg rounded-xl bg-white p-6 shadow-xl">
        <h3 class="text-lg font-semibold">{{ editingId ? '編輯使用者' : '新增使用者' }}</h3>
        <p class="mt-1 text-sm text-gray-500">
          角色變更會直接影響控制面可見範圍與 developer console 使用權。
        </p>

        <div class="mt-5 space-y-4">
          <label class="grid gap-1">
            <span class="text-sm font-medium text-gray-700">帳號</span>
            <input
              v-model="form.username"
              :disabled="!!editingId"
              type="text"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100"
              placeholder="帳號"
            />
          </label>
          <label v-if="!editingId" class="grid gap-1">
            <span class="text-sm font-medium text-gray-700">密碼</span>
            <input
              v-model="form.password"
              type="password"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="密碼"
            />
          </label>
          <label class="grid gap-1">
            <span class="text-sm font-medium text-gray-700">Email</span>
            <input
              v-model="form.email"
              type="email"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="選填"
            />
          </label>
          <label class="grid gap-1">
            <span class="text-sm font-medium text-gray-700">角色</span>
            <select
              v-model="form.role"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
            >
              <option value="user">使用者</option>
              <option value="developer">開發者</option>
              <option value="admin">管理員</option>
            </select>
          </label>
          <div
            v-if="form.role !== 'user'"
            class="rounded-xl border border-yellow-200 bg-yellow-50 px-4 py-3 text-sm text-yellow-800"
          >
            {{ form.role === 'developer'
              ? '開發者可進入 Agent console 並下載模板。'
              : '管理員可管理所有使用者、模型、審計與平台設定。' }}
          </div>
          <label class="grid gap-1">
            <span class="text-sm font-medium text-gray-700">部門</span>
            <select
              v-model="form.department_id"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
            >
              <option :value="null">未設定</option>
              <option v-for="department in activeDepartments" :key="department.id" :value="department.id">
                {{ department.name }}
              </option>
            </select>
          </label>
        </div>

        <div class="mt-6 flex justify-end gap-3">
          <button
            @click="showModal = false"
            class="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleSubmit"
            :disabled="!form.username || (!editingId && !form.password)"
            class="rounded-lg bg-indigo-600 px-4 py-2 text-sm text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ editingId ? '更新' : '建立' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showAllowedModelsModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showAllowedModelsModal = false"></div>
      <div class="relative mx-4 w-full max-w-md rounded-xl bg-white p-6 shadow-xl">
        <h3 class="text-lg font-semibold">可用模型 — {{ allowedModelsTarget?.username }}</h3>
        <p class="mt-1 text-xs text-gray-400">勾選後會同步更新現有 API Key 對模型的可見範圍。</p>

        <div class="mt-4 max-h-64 space-y-2 overflow-y-auto rounded-lg border border-gray-200 p-3">
          <label
            v-for="model in allModels"
            :key="model.id"
            class="flex cursor-pointer items-center space-x-2"
          >
            <input type="checkbox" :value="model.id" v-model="selectedModelIds" class="rounded text-teal-600 focus:ring-teal-500" />
            <span class="text-sm">{{ model.display_name }}</span>
            <span class="text-xs text-gray-400">({{ model.model_type }})</span>
          </label>
          <p v-if="allModels.length === 0" class="text-sm text-gray-400">尚無已註冊模型</p>
        </div>

        <div class="mt-6 flex justify-end gap-3">
          <button
            @click="showAllowedModelsModal = false"
            class="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleSaveAllowedModels"
            :disabled="savingModels"
            class="rounded-lg bg-teal-600 px-4 py-2 text-sm text-white hover:bg-teal-700 disabled:opacity-50"
          >
            {{ savingModels ? '儲存中...' : '儲存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showAllowedAgentsModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showAllowedAgentsModal = false"></div>
      <div class="relative mx-4 w-full max-w-md rounded-xl bg-white p-6 shadow-xl">
        <h3 class="text-lg font-semibold">可用 Agent — {{ allowedAgentsTarget?.username }}</h3>
        <p class="mt-1 text-xs text-gray-400">此名單只會顯示已核准 Agent。</p>

        <div class="mt-4 max-h-64 space-y-2 overflow-y-auto rounded-lg border border-gray-200 p-3">
          <label
            v-for="agent in allAgents"
            :key="agent.id"
            class="flex cursor-pointer items-center space-x-2"
          >
            <input type="checkbox" :value="agent.id" v-model="selectedAgentIds" class="rounded text-indigo-600 focus:ring-indigo-500" />
            <span class="text-sm">{{ agent.name }}</span>
            <span class="max-w-xs truncate text-xs text-gray-400">{{ agent.description_for_router }}</span>
          </label>
          <p v-if="allAgents.length === 0" class="text-sm text-gray-400">尚無已核准 Agent</p>
        </div>

        <div class="mt-6 flex justify-end gap-3">
          <button
            @click="showAllowedAgentsModal = false"
            class="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleSaveAllowedAgents"
            :disabled="savingAgents"
            class="rounded-lg bg-indigo-600 px-4 py-2 text-sm text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ savingAgents ? '儲存中...' : '儲存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showResetModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showResetModal = false"></div>
      <div class="relative mx-4 w-full max-w-md rounded-xl bg-white p-6 shadow-xl">
        <h3 class="text-lg font-semibold">重設密碼 — {{ resetTarget?.username }}</h3>
        <label class="mt-4 grid gap-1">
          <span class="text-sm font-medium text-gray-700">新密碼</span>
          <input
            v-model="resetPassword"
            type="password"
            class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-orange-500"
            placeholder="請輸入新密碼"
          />
        </label>

        <div class="mt-6 flex justify-end gap-3">
          <button
            @click="showResetModal = false"
            class="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleResetPassword"
            :disabled="!resetPassword"
            class="rounded-lg bg-orange-600 px-4 py-2 text-sm text-white hover:bg-orange-700 disabled:opacity-50"
          >
            確認重設
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import client from '../api/client'
import { listDepartments } from '../api/departments'
import { listModels } from '../api/models'
import {
  createUser,
  deactivateUser,
  getUserAllowedAgents,
  getUserAllowedModels,
  listUsers,
  resetUserPassword,
  updateUser,
  updateUserAllowedAgents,
  updateUserAllowedModels,
} from '../api/users'

const users = ref([])
const departments = ref([])
const allModels = ref([])
const allAgents = ref([])
const loading = ref(false)
const feedback = ref({ type: 'success', message: '' })
const filters = ref({
  query: '',
  role: 'all',
  status: 'all',
  sort: 'newest',
})
const selectedUserIds = ref([])

const showModal = ref(false)
const editingId = ref(null)
const form = ref({ username: '', password: '', email: '', role: 'user', department_id: null })
const showResetModal = ref(false)
const resetTarget = ref(null)
const resetPassword = ref('')
const showAllowedModelsModal = ref(false)
const allowedModelsTarget = ref(null)
const selectedModelIds = ref([])
const savingModels = ref(false)
const showAllowedAgentsModal = ref(false)
const allowedAgentsTarget = ref(null)
const selectedAgentIds = ref([])
const savingAgents = ref(false)

const activeDepartments = computed(() => departments.value.filter(department => department.is_active))
const pendingCount = computed(() => users.value.filter(user => !user.is_approved).length)
const developerCount = computed(() => users.value.filter(user => user.role === 'developer').length)
const activeCount = computed(() => users.value.filter(user => user.is_active && user.is_approved).length)

const filteredUsers = computed(() => {
  const query = filters.value.query.trim().toLowerCase()
  let next = users.value.filter(user => {
    if (filters.value.role !== 'all' && user.role !== filters.value.role) {
      return false
    }
    if (filters.value.status === 'pending' && user.is_approved) {
      return false
    }
    if (filters.value.status === 'active' && (!user.is_approved || !user.is_active)) {
      return false
    }
    if (filters.value.status === 'inactive' && (!user.is_approved || user.is_active)) {
      return false
    }
    if (!query) {
      return true
    }
    return [
      user.username,
      user.email,
      user.department_name,
      user.role,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(query)
  })

  next = [...next].sort((left, right) => {
    if (filters.value.sort === 'oldest') {
      return new Date(left.created_at) - new Date(right.created_at)
    }
    if (filters.value.sort === 'name') {
      return (left.username || '').localeCompare(right.username || '')
    }
    return new Date(right.created_at) - new Date(left.created_at)
  })

  return next
})

const allVisibleSelected = computed(() =>
  filteredUsers.value.length > 0 && filteredUsers.value.every(user => selectedUserIds.value.includes(user.id))
)

function setFeedback(type, message) {
  feedback.value = { type, message }
}

async function fetchUsers() {
  loading.value = true
  try {
    const { data } = await listUsers()
    users.value = data
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '載入使用者失敗')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await fetchUsers()
  try {
    const { data } = await listDepartments()
    departments.value = data
  } catch {}
  try {
    const { data } = await listModels()
    allModels.value = data
  } catch {}
  try {
    const { data } = await client.get('/api/agents')
    allAgents.value = data.filter(agent => agent.approval_status === 'approved')
  } catch {}
})

function openCreateModal() {
  editingId.value = null
  form.value = { username: '', password: '', email: '', role: 'user', department_id: null }
  showModal.value = true
}

function openEditModal(user) {
  editingId.value = user.id
  form.value = {
    username: user.username,
    email: user.email || '',
    role: user.role,
    department_id: user.department_id ?? null,
  }
  showModal.value = true
}

async function handleSubmit() {
  try {
    if (editingId.value) {
      await updateUser(editingId.value, {
        email: form.value.email || null,
        role: form.value.role,
        department_id: form.value.department_id,
      })
      setFeedback('success', '使用者資料已更新。')
    } else {
      await createUser(form.value)
      setFeedback('success', '使用者已建立。')
    }
    showModal.value = false
    await fetchUsers()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '操作失敗')
  }
}

async function openAllowedModelsModal(user) {
  allowedModelsTarget.value = user
  selectedModelIds.value = []
  try {
    const { data } = await getUserAllowedModels(user.id)
    selectedModelIds.value = data.map(model => model.id)
  } catch {}
  showAllowedModelsModal.value = true
}

async function handleSaveAllowedModels() {
  savingModels.value = true
  try {
    const result = await updateUserAllowedModels(allowedModelsTarget.value.id, selectedModelIds.value)
    showAllowedModelsModal.value = false
    setFeedback('success', result.data?.message || '已更新可用模型。')
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '更新可用模型失敗')
  } finally {
    savingModels.value = false
  }
}

async function openAllowedAgentsModal(user) {
  allowedAgentsTarget.value = user
  selectedAgentIds.value = []
  try {
    const { data } = await getUserAllowedAgents(user.id)
    selectedAgentIds.value = data.map(agent => agent.id)
  } catch {}
  showAllowedAgentsModal.value = true
}

async function handleSaveAllowedAgents() {
  savingAgents.value = true
  try {
    const result = await updateUserAllowedAgents(allowedAgentsTarget.value.id, selectedAgentIds.value)
    showAllowedAgentsModal.value = false
    setFeedback('success', result.data?.message || '已更新可用 Agent。')
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '更新可用 Agent 失敗')
  } finally {
    savingAgents.value = false
  }
}

function openResetPasswordModal(user) {
  resetTarget.value = user
  resetPassword.value = ''
  showResetModal.value = true
}

async function handleResetPassword() {
  try {
    await resetUserPassword(resetTarget.value.id, { new_password: resetPassword.value })
    showResetModal.value = false
    setFeedback('success', `已重設「${resetTarget.value.username}」的密碼。`)
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '重設密碼失敗')
  }
}

async function handleApprove(user) {
  try {
    await client.post(`/api/users/${user.id}/approve`)
    setFeedback('success', `已核准使用者「${user.username}」。`)
    await fetchUsers()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '核准失敗')
  }
}

async function handleDeactivate(user) {
  if (!window.confirm(`確定要停用使用者「${user.username}」嗎？`)) {
    return
  }
  try {
    await deactivateUser(user.id)
    setFeedback('success', `已停用使用者「${user.username}」。`)
    await fetchUsers()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '停用失敗')
  }
}

// Sprint 6 X / B2：admin 切換「本機密碼登入禁用」flag。flip → True 表示
// 該使用者只能透過 SSO 進站；flip → False 解除限制。
async function handleToggleSsoOnly(user, disable) {
  const action = disable ? '切換為 SSO-only' : '解除 SSO-only 限制'
  if (!window.confirm(`確定要對「${user.username}」${action}嗎？`)) {
    return
  }
  try {
    await updateUser(user.id, { local_password_disabled: disable })
    setFeedback(
      'success',
      disable
        ? `「${user.username}」已切換為 SSO-only，本機密碼登入會被拒絕。`
        : `已恢復「${user.username}」的本機密碼登入。`,
    )
    await fetchUsers()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || `${action}失敗`)
  }
}

function toggleUserSelection(userId, checked) {
  if (checked) {
    selectedUserIds.value = Array.from(new Set([...selectedUserIds.value, userId]))
    return
  }
  selectedUserIds.value = selectedUserIds.value.filter(id => id !== userId)
}

function toggleSelectAll(checked) {
  if (checked) {
    selectedUserIds.value = Array.from(new Set([...selectedUserIds.value, ...filteredUsers.value.map(user => user.id)]))
    return
  }
  const visibleIds = new Set(filteredUsers.value.map(user => user.id))
  selectedUserIds.value = selectedUserIds.value.filter(id => !visibleIds.has(id))
}

async function handleBulkApprove() {
  const targets = users.value.filter(user => selectedUserIds.value.includes(user.id) && !user.is_approved)
  if (!targets.length) {
    setFeedback('error', '目前選取中沒有待核准使用者。')
    return
  }
  for (const user of targets) {
    await client.post(`/api/users/${user.id}/approve`)
  }
  selectedUserIds.value = []
  setFeedback('success', `已核准 ${targets.length} 位使用者。`)
  await fetchUsers()
}

async function handleBulkDeactivate() {
  const targets = users.value.filter(user => selectedUserIds.value.includes(user.id) && user.is_active && user.is_approved)
  if (!targets.length) {
    setFeedback('error', '目前選取中沒有可停用的啟用使用者。')
    return
  }
  if (!window.confirm(`確定要停用 ${targets.length} 位使用者嗎？`)) {
    return
  }
  for (const user of targets) {
    await deactivateUser(user.id)
  }
  selectedUserIds.value = []
  setFeedback('success', `已停用 ${targets.length} 位使用者。`)
  await fetchUsers()
}

function roleLabel(role) {
  return { admin: '管理員', developer: '開發者', user: '使用者' }[role] || role
}

function rolePillClass(role) {
  return {
    admin: 'bg-purple-50 text-purple-700',
    developer: 'bg-indigo-50 text-indigo-700',
    user: 'bg-gray-100 text-gray-600',
  }[role] || 'bg-gray-100 text-gray-600'
}

function statusLabel(user) {
  if (!user.is_approved) {
    return '待核准'
  }
  return user.is_active ? '啟用' : '停用'
}

function statusPillClass(user) {
  if (!user.is_approved) {
    return 'bg-yellow-50 text-yellow-700'
  }
  return user.is_active ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
}

function formatDate(dateStr) {
  if (!dateStr) {
    return '未提供'
  }
  return new Date(dateStr).toLocaleString('zh-TW')
}
</script>
