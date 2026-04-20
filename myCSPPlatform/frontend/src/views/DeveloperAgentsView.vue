<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-lg font-semibold">Agent 管理</h2>
        <p class="text-sm text-gray-500 mt-0.5">
          {{ authStore.isAdmin ? '所有已註冊的 Agent（admin 視角）' : '我的 Agent 列表' }}
        </p>
      </div>
      <div class="flex space-x-2">
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

    <!-- Agents Table -->
    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b">
          <tr>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">名稱</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">Endpoint</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">描述（Router 用）</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">Health</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">狀態</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">建立日期</th>
            <th class="px-4 py-3 text-left text-gray-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="agent in agents" :key="agent.id" class="border-b last:border-0 hover:bg-gray-50">
            <td class="px-4 py-3 font-medium">{{ agent.name }}</td>
            <td class="px-4 py-3 text-gray-500 text-xs font-mono truncate max-w-[160px]">{{ agent.endpoint_url }}</td>
            <td class="px-4 py-3 text-gray-500 text-xs truncate max-w-[200px]">{{ agent.description_for_router }}</td>
            <td class="px-4 py-3">
              <span class="text-xs px-2 py-0.5 rounded"
                :class="{
                  'bg-green-50 text-green-700': agent.health_status === 'healthy',
                  'bg-red-50 text-red-700': agent.health_status === 'unhealthy',
                  'bg-gray-100 text-gray-500': agent.health_status === 'unknown',
                }">
                {{ agent.health_status }}
              </span>
            </td>
            <td class="px-4 py-3">
              <span class="text-xs px-2 py-0.5 rounded"
                :class="{
                  'bg-yellow-50 text-yellow-700': agent.approval_status === 'pending',
                  'bg-green-50 text-green-700': agent.approval_status === 'approved',
                  'bg-red-50 text-red-700': agent.approval_status === 'rejected',
                }">
                {{ { pending: '待審核', approved: '已核准', rejected: '已拒絕' }[agent.approval_status] }}
              </span>
            </td>
            <td class="px-4 py-3 text-gray-500">{{ formatDate(agent.created_at) }}</td>
            <td class="px-4 py-3 space-x-2 whitespace-nowrap">
              <template v-if="authStore.isAdmin && agent.approval_status === 'pending'">
                <button @click="handleApprove(agent)" class="text-green-600 hover:text-green-800 text-xs font-medium">
                  核准
                </button>
                <button @click="handleReject(agent)" class="text-red-600 hover:text-red-800 text-xs font-medium">
                  拒絕
                </button>
              </template>
              <button @click="openDetailModal(agent)" class="text-indigo-600 hover:text-indigo-800 text-xs">
                詳情
              </button>
            </td>
          </tr>
          <tr v-if="agents.length === 0">
            <td colspan="7" class="px-4 py-8 text-center text-gray-400">尚無 Agent — 點擊「註冊 Agent」開始</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Register Modal -->
    <div v-if="showRegisterModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showRegisterModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">註冊新 Agent</h3>

        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Agent 名稱 <span class="text-red-500">*</span></label>
            <input v-model="form.name" type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="例如：hr-policy-agent" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Endpoint URL <span class="text-red-500">*</span></label>
            <input v-model="form.endpoint_url" type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="http://host:port" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Router 描述 <span class="text-red-500">*</span></label>
            <textarea v-model="form.description_for_router" rows="2"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
              placeholder="一段讓 Router LLM 理解此 Agent 能力的自然語言描述" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">API Version</label>
            <input v-model="form.api_version" type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="v1" />
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button @click="showRegisterModal = false"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">
            取消
          </button>
          <button @click="handleRegister"
            :disabled="!form.name || !form.endpoint_url || !form.description_for_router || registering"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50">
            {{ registering ? '提交中...' : '提交審核' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Detail Modal -->
    <div v-if="showDetailModal && detailAgent" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="showDetailModal = false"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">Agent 詳情 — {{ detailAgent.name }}</h3>
        <dl class="space-y-2 text-sm">
          <div class="flex"><dt class="w-32 text-gray-500 shrink-0">Endpoint</dt><dd class="font-mono text-xs break-all">{{ detailAgent.endpoint_url }}</dd></div>
          <div class="flex"><dt class="w-32 text-gray-500 shrink-0">API Version</dt><dd>{{ detailAgent.api_version }}</dd></div>
          <div class="flex"><dt class="w-32 text-gray-500 shrink-0">Health</dt><dd>{{ detailAgent.health_status }}</dd></div>
          <div class="flex"><dt class="w-32 text-gray-500 shrink-0">狀態</dt><dd>{{ detailAgent.approval_status }}</dd></div>
          <div class="flex"><dt class="w-32 text-gray-500 shrink-0">Router 描述</dt><dd>{{ detailAgent.description_for_router }}</dd></div>
          <div v-if="detailAgent.capabilities" class="flex">
            <dt class="w-32 text-gray-500 shrink-0">Capabilities</dt>
            <dd class="font-mono text-xs">{{ JSON.stringify(detailAgent.capabilities) }}</dd>
          </div>
        </dl>
        <div class="flex justify-end mt-6">
          <button @click="showDetailModal = false"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">
            關閉
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { listMyAgents, registerAgent, approveAgent, rejectAgent, downloadTemplate } from '../api/agents'

const authStore = useAuthStore()

const agents = ref([])
const showRegisterModal = ref(false)
const showDetailModal = ref(false)
const detailAgent = ref(null)
const registering = ref(false)
const form = ref({ name: '', endpoint_url: '', description_for_router: '', api_version: 'v1' })

async function fetchAgents() {
  try {
    const { data } = await listMyAgents()
    agents.value = data
  } catch (e) {
    console.error('fetch agents failed', e)
  }
}

onMounted(fetchAgents)

function openRegisterModal() {
  form.value = { name: '', endpoint_url: '', description_for_router: '', api_version: 'v1' }
  showRegisterModal.value = true
}

function openDetailModal(agent) {
  detailAgent.value = agent
  showDetailModal.value = true
}

async function handleRegister() {
  registering.value = true
  try {
    await registerAgent(form.value)
    showRegisterModal.value = false
    await fetchAgents()
    alert('已提交，等待 admin 審核')
  } catch (e) {
    alert(e.response?.data?.detail || '提交失敗')
  } finally {
    registering.value = false
  }
}

async function handleApprove(agent) {
  try {
    await approveAgent(agent.id)
    await fetchAgents()
  } catch (e) {
    alert(e.response?.data?.detail || '核准失敗')
  }
}

async function handleReject(agent) {
  const reason = prompt(`拒絕原因（可留空）：`)
  if (reason === null) return
  try {
    await rejectAgent(agent.id, reason)
    await fetchAgents()
  } catch (e) {
    alert(e.response?.data?.detail || '拒絕失敗')
  }
}

async function handleDownloadTemplate() {
  try {
    const { data } = await downloadTemplate()
    const url = URL.createObjectURL(new Blob([data]))
    const a = document.createElement('a')
    a.href = url
    a.download = 'anila-core-template.zip'
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    alert(e.response?.data?.detail || '下載失敗')
  }
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleString('zh-TW')
}
</script>
