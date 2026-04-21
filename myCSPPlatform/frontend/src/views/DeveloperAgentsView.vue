<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
      <div>
        <h2 class="text-lg font-semibold">Agent Console</h2>
        <p class="text-sm text-gray-500 mt-0.5">
          {{ authStore.isAdmin ? '審核與管理所有已註冊 Agent' : '管理你的 Agent 與模板下載流程' }}
        </p>
      </div>
      <div class="flex flex-wrap gap-2">
        <button
          @click="handleDownloadTemplate"
          class="px-4 py-2 text-sm border border-indigo-300 text-indigo-700 rounded-lg hover:bg-indigo-50 transition"
        >
          下載官方模板
        </button>
        <button
          @click="openRegisterModal"
          class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 transition"
        >
          註冊 Agent
        </button>
      </div>
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
        <div class="mt-2 text-2xl font-semibold">{{ agents.length }}</div>
      </div>
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Pending</div>
        <div class="mt-2 text-2xl font-semibold text-yellow-700">{{ pendingCount }}</div>
      </div>
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Approved</div>
        <div class="mt-2 text-2xl font-semibold text-green-700">{{ approvedCount }}</div>
      </div>
      <div class="rounded-xl border border-gray-200 bg-white p-4">
        <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Healthy</div>
        <div class="mt-2 text-2xl font-semibold text-indigo-700">{{ healthyCount }}</div>
      </div>
    </div>

    <div class="grid gap-3 rounded-xl border border-gray-200 bg-white p-4 lg:grid-cols-4">
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">搜尋</span>
        <input
          v-model="filters.query"
          type="text"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="名稱、描述、endpoint"
        />
      </label>
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">審核狀態</span>
        <select
          v-model="filters.approval"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="all">全部</option>
          <option value="pending">待審核</option>
          <option value="approved">已核准</option>
          <option value="rejected">已拒絕</option>
        </select>
      </label>
      <label class="grid gap-1">
        <span class="text-xs font-medium uppercase tracking-[0.16em] text-gray-400">Health</span>
        <select
          v-model="filters.health"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="all">全部</option>
          <option value="healthy">Healthy</option>
          <option value="unhealthy">Unhealthy</option>
          <option value="unknown">Unknown</option>
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
          <option value="name">名稱 A-Z</option>
          <option value="approval">待審核優先</option>
        </select>
      </label>
    </div>

    <div v-if="authStore.isAdmin" class="rounded-xl border border-yellow-200 bg-yellow-50 px-4 py-3 text-sm text-yellow-800">
      目前有 {{ pendingCount }} 個 Agent 待審核。可用篩選切到「待審核」快速處理 approval queue。
    </div>

    <div class="overflow-hidden rounded-xl border border-gray-200 bg-white">
      <div v-if="loading" class="px-4 py-10 text-center text-sm text-gray-400">載入 Agent 清單中...</div>
      <div v-else-if="filteredAgents.length === 0" class="px-4 py-10 text-center text-sm text-gray-400">
        {{ agents.length === 0 ? '尚無 Agent，從模板下載與註冊流程開始。' : '沒有符合目前篩選條件的 Agent。' }}
      </div>
      <div v-else class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="border-b bg-gray-50">
            <tr>
              <th class="px-4 py-3 text-left font-medium text-gray-600">名稱</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">Endpoint</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">Router 描述</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">Health</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">審核狀態</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">加密</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">建立日期</th>
              <th class="px-4 py-3 text-left font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="agent in filteredAgents"
              :key="agent.id"
              class="border-b last:border-0 hover:bg-gray-50"
            >
              <td class="px-4 py-3">
                <div class="font-medium">{{ agent.name }}</div>
                <div class="mt-1 text-xs text-gray-400">ID: {{ agent.id }}</div>
              </td>
              <td class="px-4 py-3 text-xs font-mono text-gray-500">
                <div class="max-w-[220px] truncate">{{ agent.endpoint_url }}</div>
              </td>
              <td class="px-4 py-3 text-xs text-gray-500">
                <div class="max-w-[260px] truncate">{{ agent.description_for_router }}</div>
              </td>
              <td class="px-4 py-3">
                <span
                  class="rounded px-2 py-0.5 text-xs"
                  :class="healthPillClass(agent.health_status)"
                >
                  {{ agent.health_status }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span
                  class="rounded px-2 py-0.5 text-xs"
                  :class="approvalPillClass(agent.approval_status)"
                >
                  {{ approvalLabel(agent.approval_status) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span
                  class="rounded px-2 py-0.5 text-xs"
                  :class="agent.requires_encryption ? 'bg-red-50 text-red-700' : 'bg-gray-50 text-gray-500'"
                >
                  {{ agent.requires_encryption ? '強制加密' : '一般' }}
                </span>
              </td>
              <td class="px-4 py-3 text-gray-500">{{ formatDate(agent.created_at) }}</td>
              <td class="px-4 py-3 whitespace-nowrap">
                <div class="flex flex-wrap gap-2 text-xs">
                  <button @click="openDetailModal(agent)" class="font-medium text-indigo-600 hover:text-indigo-800">
                    詳情
                  </button>
                  <button
                    v-if="authStore.isAdmin && agent.approval_status === 'pending'"
                    @click="handleApprove(agent)"
                    class="font-medium text-green-600 hover:text-green-800"
                  >
                    核准
                  </button>
                  <button
                    v-if="authStore.isAdmin && agent.approval_status === 'pending'"
                    @click="openRejectModal(agent)"
                    class="font-medium text-red-600 hover:text-red-800"
                  >
                    拒絕
                  </button>
                  <button
                    v-if="authStore.isAdmin"
                    @click="handleDeleteAgent(agent)"
                    :disabled="deletingId === agent.id"
                    class="font-medium text-red-600 hover:text-red-800 disabled:opacity-50"
                  >
                    {{ deletingId === agent.id ? '刪除中…' : '刪除' }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="showRegisterModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showRegisterModal = false"></div>
      <div class="relative mx-4 w-full max-w-2xl rounded-xl bg-white p-6 shadow-xl">
        <h3 class="text-lg font-semibold">註冊新 Agent</h3>
        <p class="mt-1 text-sm text-gray-500">提交後會進入 pending 狀態，等待 admin 核准。</p>

        <div class="mt-5 grid gap-4 md:grid-cols-2">
          <label class="grid gap-1 md:col-span-2">
            <span class="text-sm font-medium text-gray-700">Agent 名稱</span>
            <input
              v-model="form.name"
              type="text"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="例如：hr-policy-agent"
            />
            <span v-if="formErrors.name" class="text-xs text-red-600">{{ formErrors.name }}</span>
          </label>
          <label class="grid gap-1 md:col-span-2">
            <span class="text-sm font-medium text-gray-700">Endpoint URL</span>
            <input
              v-model="form.endpoint_url"
              type="text"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="http://host:port"
            />
            <span v-if="formErrors.endpoint_url" class="text-xs text-red-600">{{ formErrors.endpoint_url }}</span>
          </label>
          <label class="grid gap-1 md:col-span-2">
            <span class="text-sm font-medium text-gray-700">Router 描述</span>
            <textarea
              v-model="form.description_for_router"
              rows="3"
              class="w-full resize-none rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="用自然語言說明這個 Agent 能解決什麼問題。"
            />
            <span v-if="formErrors.description_for_router" class="text-xs text-red-600">{{ formErrors.description_for_router }}</span>
          </label>
          <label class="grid gap-1">
            <span class="text-sm font-medium text-gray-700">API Version</span>
            <input
              v-model="form.api_version"
              type="text"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="v1"
            />
          </label>
        </div>

        <div class="mt-6 rounded-xl border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600">
          <div class="font-medium text-gray-800">提交前檢查</div>
          <ul class="mt-2 space-y-1 text-xs">
            <li>{{ form.name ? '✓' : '○' }} 已填 Agent 名稱</li>
            <li>{{ /^https?:\/\//.test(form.endpoint_url) ? '✓' : '○' }} Endpoint 使用 http/https URL</li>
            <li>{{ form.description_for_router.trim().length >= 24 ? '✓' : '○' }} Router 描述至少 24 字元</li>
          </ul>
        </div>

        <div class="mt-6 flex justify-end gap-3">
          <button
            @click="showRegisterModal = false"
            class="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleRegister"
            :disabled="registering"
            class="rounded-lg bg-indigo-600 px-4 py-2 text-sm text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ registering ? '提交中...' : '提交審核' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showDetailModal && detailAgent" class="fixed inset-0 z-50 flex items-center justify-end">
      <div class="fixed inset-0 bg-black/40" @click="showDetailModal = false"></div>
      <div class="relative h-full w-full max-w-xl overflow-y-auto bg-white p-6 shadow-2xl">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h3 class="text-lg font-semibold">Agent 詳情</h3>
            <p class="mt-1 text-sm text-gray-500">{{ detailAgent.name }}</p>
          </div>
          <button @click="showDetailModal = false" class="text-sm text-gray-500 hover:text-gray-700">關閉</button>
        </div>

        <div class="mt-6 space-y-5">
          <div class="rounded-xl border border-gray-200 bg-gray-50 p-4">
            <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Overview</div>
            <dl class="mt-3 space-y-2 text-sm">
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">Endpoint</dt><dd class="font-mono text-xs break-all">{{ detailAgent.endpoint_url }}</dd></div>
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">API Version</dt><dd>{{ detailAgent.api_version || 'v1' }}</dd></div>
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">Health</dt><dd>{{ detailAgent.health_status }}</dd></div>
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">審核狀態</dt><dd>{{ approvalLabel(detailAgent.approval_status) }}</dd></div>
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">建立時間</dt><dd>{{ formatDate(detailAgent.created_at) }}</dd></div>
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">Owner</dt><dd>{{ detailAgent.owner_user_id || '未提供' }}</dd></div>
              <div class="flex gap-3"><dt class="w-28 shrink-0 text-gray-500">Base Model</dt><dd>{{ detailAgent.base_model_id || '未設定' }}</dd></div>
              <div class="flex gap-3">
                <dt class="w-28 shrink-0 text-gray-500">加密模式</dt>
                <dd class="flex flex-wrap items-center gap-2">
                  <span
                    class="rounded px-2 py-0.5 text-xs"
                    :class="detailAgent.requires_encryption ? 'bg-red-50 text-red-700' : 'bg-gray-50 text-gray-500'"
                  >
                    {{ detailAgent.requires_encryption ? '強制加密' : '一般' }}
                  </span>
                  <button
                    v-if="authStore.isAdmin"
                    @click="handleToggleEncryption(detailAgent)"
                    :disabled="encryptionBusyId === detailAgent.id"
                    class="rounded-lg border px-2.5 py-1 text-xs transition disabled:opacity-50"
                    :class="detailAgent.requires_encryption
                      ? 'border-gray-300 text-gray-700 hover:bg-gray-50'
                      : 'border-red-300 text-red-700 hover:bg-red-50'"
                  >
                    {{
                      encryptionBusyId === detailAgent.id
                        ? '更新中…'
                        : (detailAgent.requires_encryption ? '停用加密' : '啟用強制加密')
                    }}
                  </button>
                </dd>
              </div>
            </dl>
            <p v-if="authStore.isAdmin" class="mt-3 text-xs text-gray-400">
              啟用後，任何使用此 Agent 的對話會自動進入加密模式；一旦對話變成加密，整段對話單向上鎖，使用者無法關閉。
            </p>
          </div>

          <div>
            <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Router Description</div>
            <div class="mt-2 rounded-xl border border-gray-200 bg-white p-4 text-sm text-gray-700">
              {{ detailAgent.description_for_router || '未提供' }}
            </div>
          </div>

          <div>
            <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Capabilities</div>
            <pre class="mt-2 overflow-x-auto rounded-xl border border-gray-200 bg-gray-950 p-4 text-xs text-gray-100">{{ prettyJson(detailAgent.capabilities) }}</pre>
          </div>

          <div>
            <div class="text-xs uppercase tracking-[0.16em] text-gray-400">Status Timeline</div>
            <div class="mt-2 space-y-3">
              <div
                v-for="entry in buildStatusHistory(detailAgent)"
                :key="entry.label + entry.timestamp"
                class="rounded-xl border border-gray-200 bg-white p-4"
              >
                <div class="font-medium">{{ entry.label }}</div>
                <div class="mt-1 text-xs text-gray-500">{{ entry.timestamp }}</div>
                <div v-if="entry.detail" class="mt-2 text-sm text-gray-600">{{ entry.detail }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="rejectTarget" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="closeRejectModal"></div>
      <div class="relative mx-4 w-full max-w-md rounded-xl bg-white p-6 shadow-xl">
        <h3 class="text-lg font-semibold">拒絕 Agent</h3>
        <p class="mt-1 text-sm text-gray-500">你可以留下一段拒絕理由，方便開發者修改後再送審。</p>
        <textarea
          v-model="rejectReason"
          rows="4"
          class="mt-4 w-full resize-none rounded-lg border border-gray-300 px-3 py-2 outline-none focus:ring-2 focus:ring-red-500"
          placeholder="例如：缺少健康檢查、Router 描述過短。"
        />
        <div class="mt-6 flex justify-end gap-3">
          <button
            @click="closeRejectModal"
            class="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            @click="handleReject"
            class="rounded-lg bg-red-600 px-4 py-2 text-sm text-white hover:bg-red-700"
          >
            確認拒絕
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { approveAgent, deleteAgent, downloadTemplate, getAgent, listMyAgents, registerAgent, rejectAgent, setAgentEncryption } from '../api/agents'

const authStore = useAuthStore()

const agents = ref([])
const loading = ref(false)
const showRegisterModal = ref(false)
const showDetailModal = ref(false)
const detailAgent = ref(null)
const registering = ref(false)
const rejectTarget = ref(null)
const rejectReason = ref('')
const encryptionBusyId = ref(null)
const deletingId = ref(null)
const feedback = ref({ type: 'success', message: '' })
const filters = ref({
  query: '',
  approval: 'all',
  health: 'all',
  sort: 'newest',
})
const form = ref({
  name: '',
  endpoint_url: '',
  description_for_router: '',
  api_version: 'v1',
})
const formErrors = ref({})

const pendingCount = computed(() => agents.value.filter(agent => agent.approval_status === 'pending').length)
const approvedCount = computed(() => agents.value.filter(agent => agent.approval_status === 'approved').length)
const healthyCount = computed(() => agents.value.filter(agent => agent.health_status === 'healthy').length)

const filteredAgents = computed(() => {
  const query = filters.value.query.trim().toLowerCase()
  let next = agents.value.filter(agent => {
    if (filters.value.approval !== 'all' && agent.approval_status !== filters.value.approval) {
      return false
    }
    if (filters.value.health !== 'all' && agent.health_status !== filters.value.health) {
      return false
    }
    if (!query) {
      return true
    }
    return [
      agent.name,
      agent.endpoint_url,
      agent.description_for_router,
      agent.id,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(query)
  })

  next = [...next].sort((left, right) => {
    if (filters.value.sort === 'name') {
      return (left.name || '').localeCompare(right.name || '')
    }
    if (filters.value.sort === 'oldest') {
      return new Date(left.created_at) - new Date(right.created_at)
    }
    if (filters.value.sort === 'approval') {
      return (left.approval_status === 'pending' ? -1 : 1) - (right.approval_status === 'pending' ? -1 : 1)
    }
    return new Date(right.created_at) - new Date(left.created_at)
  })

  return next
})

function setFeedback(type, message) {
  feedback.value = { type, message }
}

function resetForm() {
  form.value = {
    name: '',
    endpoint_url: '',
    description_for_router: '',
    api_version: 'v1',
  }
  formErrors.value = {}
}

function validateForm() {
  const errors = {}
  if (!form.value.name.trim()) {
    errors.name = '請提供 Agent 名稱'
  }
  if (!/^https?:\/\//.test(form.value.endpoint_url.trim())) {
    errors.endpoint_url = 'Endpoint 必須是 http 或 https URL'
  }
  if (form.value.description_for_router.trim().length < 24) {
    errors.description_for_router = 'Router 描述至少需要 24 個字元'
  }
  formErrors.value = errors
  return Object.keys(errors).length === 0
}

async function fetchAgents() {
  loading.value = true
  try {
    const { data } = await listMyAgents()
    agents.value = data
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '載入 Agent 清單失敗')
  } finally {
    loading.value = false
  }
}

onMounted(fetchAgents)

function openRegisterModal() {
  resetForm()
  showRegisterModal.value = true
}

async function openDetailModal(agent) {
  try {
    const { data } = await getAgent(agent.id)
    detailAgent.value = data
  } catch {
    detailAgent.value = agent
  }
  showDetailModal.value = true
}

function openRejectModal(agent) {
  rejectTarget.value = agent
  rejectReason.value = ''
}

function closeRejectModal() {
  rejectTarget.value = null
  rejectReason.value = ''
}

async function handleRegister() {
  if (!validateForm()) {
    return
  }
  registering.value = true
  try {
    await registerAgent({
      ...form.value,
      name: form.value.name.trim(),
      endpoint_url: form.value.endpoint_url.trim(),
      description_for_router: form.value.description_for_router.trim(),
    })
    showRegisterModal.value = false
    setFeedback('success', 'Agent 已提交審核，狀態為 pending。')
    await fetchAgents()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '提交 Agent 失敗')
  } finally {
    registering.value = false
  }
}

async function handleApprove(agent) {
  try {
    await approveAgent(agent.id)
    setFeedback('success', `已核准 Agent「${agent.name}」`)
    await fetchAgents()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '核准失敗')
  }
}

async function handleToggleEncryption(agent) {
  if (!agent || encryptionBusyId.value === agent.id) {
    return
  }
  const next = !agent.requires_encryption
  if (next && !window.confirm(`啟用後，所有走「${agent.name}」的對話會自動鎖為加密模式，使用者無法關閉。確定啟用？`)) {
    return
  }
  encryptionBusyId.value = agent.id
  try {
    const { data } = await setAgentEncryption(agent.id, next)
    const applied = Boolean(data?.requires_encryption ?? next)
    const list = agents.value
    const idx = list.findIndex((a) => a.id === agent.id)
    if (idx !== -1) {
      list[idx] = { ...list[idx], requires_encryption: applied }
    }
    if (detailAgent.value && detailAgent.value.id === agent.id) {
      detailAgent.value = { ...detailAgent.value, requires_encryption: applied }
    }
    setFeedback('success', `已${applied ? '啟用' : '停用'} Agent「${agent.name}」加密模式`)
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '加密設定更新失敗')
  } finally {
    encryptionBusyId.value = null
  }
}

async function handleDeleteAgent(agent) {
  if (!agent || deletingId.value === agent.id) {
    return
  }
  if (!window.confirm(`確定要刪除 Agent「${agent.name}」？此操作無法復原，對話中的引用會斷線。`)) {
    return
  }
  deletingId.value = agent.id
  try {
    await deleteAgent(agent.id)
    agents.value = agents.value.filter((a) => a.id !== agent.id)
    if (detailAgent.value && detailAgent.value.id === agent.id) {
      showDetailModal.value = false
      detailAgent.value = null
    }
    setFeedback('success', `已刪除 Agent「${agent.name}」`)
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '刪除 Agent 失敗')
  } finally {
    deletingId.value = null
  }
}

async function handleReject() {
  try {
    await rejectAgent(rejectTarget.value.id, rejectReason.value.trim())
    setFeedback('success', `已拒絕 Agent「${rejectTarget.value.name}」`)
    closeRejectModal()
    await fetchAgents()
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '拒絕失敗')
  }
}

async function handleDownloadTemplate() {
  try {
    const { data } = await downloadTemplate()
    const url = URL.createObjectURL(new Blob([data]))
    const link = document.createElement('a')
    link.href = url
    link.download = 'anila-core-template.zip'
    link.click()
    URL.revokeObjectURL(url)
    setFeedback('success', '官方模板已下載。')
  } catch (error) {
    setFeedback('error', error.response?.data?.detail || '模板下載失敗')
  }
}

function approvalLabel(status) {
  return { pending: '待審核', approved: '已核准', rejected: '已拒絕' }[status] || status
}

function approvalPillClass(status) {
  return {
    pending: 'bg-yellow-50 text-yellow-700',
    approved: 'bg-green-50 text-green-700',
    rejected: 'bg-red-50 text-red-700',
  }[status] || 'bg-gray-100 text-gray-500'
}

function healthPillClass(status) {
  return {
    healthy: 'bg-green-50 text-green-700',
    unhealthy: 'bg-red-50 text-red-700',
    unknown: 'bg-gray-100 text-gray-500',
  }[status] || 'bg-gray-100 text-gray-500'
}

function formatDate(dateStr) {
  if (!dateStr) {
    return '未提供'
  }
  return new Date(dateStr).toLocaleString('zh-TW')
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2)
}

function buildStatusHistory(agent) {
  const history = [
    {
      label: 'Agent 已建立',
      timestamp: formatDate(agent.created_at),
      detail: '已完成 endpoint / description 註冊。',
    },
  ]
  if (agent.approval_status === 'approved') {
    history.push({
      label: 'Admin 已核准',
      timestamp: formatDate(agent.approved_at),
      detail: agent.approved_by ? `approved_by: ${agent.approved_by}` : '',
    })
  }
  if (agent.approval_status === 'rejected') {
    history.push({
      label: '審核被拒絕',
      timestamp: formatDate(agent.updated_at || agent.created_at),
      detail: '請檢查健康狀態、Router 描述與 endpoint 可用性後再送審。',
    })
  }
  history.push({
    label: `Health · ${agent.health_status || 'unknown'}`,
    timestamp: formatDate(agent.updated_at || agent.created_at),
    detail: '此時間點依目前 API 回傳推估；若後端之後補 health history，可直接接上。',
  })
  return history
}
</script>
