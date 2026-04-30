<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">developer · console</p>
        <h1 class="page-head__title">agents</h1>
        <p class="page-head__sub">
          {{ authStore.isAdmin ? 'review and govern every registered agent' : 'manage your agents · download templates · ship to router' }}
        </p>
      </div>
      <div class="page-head__actions">
        <TermButton @click="handleDownloadTemplate" label="download template" />
        <TermButton variant="primary" @click="openRegisterModal" label="register agent" />
      </div>
    </header>

    <div v-if="feedback.message" class="feedback" :class="feedback.type === 'error' ? 'is-err' : 'is-ok'">
      <span>{{ feedback.type === 'error' ? '!' : '✓' }}</span>
      <span>{{ feedback.message }}</span>
    </div>

    <TermBox title="developer · guide" pad="md">
      <button type="button" class="guide-toggle" @click="showGuide = !showGuide">
        <span>{{ showGuide ? '▾' : '▸' }} fork template → deploy → register → wait for approval</span>
        <span class="cell-meta">{{ showGuide ? 'collapse' : 'expand' }}</span>
      </button>
      <div v-if="showGuide" class="guide">
        <ol class="guide__list">
          <li>
            <span class="guide__step">01</span>
            <div>
              <p>get the template, edit <code>retrieve_context()</code> and <code>SYSTEM_PROMPT</code> for your domain.</p>
              <p>configure <code>.env</code>, <code>docker compose up -d</code>, verify <code>curl http://&lt;host&gt;:24786/health</code> returns <code>{"status":"ok"}</code>.</p>
            </div>
          </li>
          <li>
            <span class="guide__step">02</span>
            <div>
              <p>your agent must implement these endpoints:</p>
              <table class="term-table guide__table">
                <thead><tr><th style="width: 70px">method</th><th>path</th><th>purpose</th></tr></thead>
                <tbody>
                  <tr><td><code>GET</code></td><td><code>/health</code></td><td>discovery + health probe (public)</td></tr>
                  <tr><td><code>GET</code></td><td><code>/v1/models</code></td><td>list available model ids (s2s)</td></tr>
                  <tr><td><code>POST</code></td><td><code>/v1/chat/completions</code></td><td>main inference, openai-compat (s2s)</td></tr>
                </tbody>
              </table>
            </div>
          </li>
          <li>
            <span class="guide__step">03</span>
            <div>
              <p>register on this page · status starts as <TermBadge variant="warn">pending</TermBadge> · admin review unblocks router auto-discovery.</p>
            </div>
          </li>
        </ol>
      </div>
    </TermBox>

    <div class="kpi-row">
      <TermStat label="agents · total" :value="agents.length" />
      <TermStat label="pending" :value="pendingCount" :tone="pendingCount ? 'warn' : 'default'" />
      <TermStat label="approved" :value="approvedCount" tone="accent" />
      <TermStat label="healthy" :value="healthyCount" />
    </div>

    <TermBox title="filter" pad="sm">
      <div class="filters">
        <TermField label="search">
          <input v-model="filters.query" class="term-input" placeholder="name · description · endpoint" />
        </TermField>
        <TermField label="approval">
          <select v-model="filters.approval" class="term-select">
            <option value="all">all</option>
            <option value="pending">pending</option>
            <option value="approved">approved</option>
            <option value="rejected">rejected</option>
          </select>
        </TermField>
        <TermField label="health">
          <select v-model="filters.health" class="term-select">
            <option value="all">all</option>
            <option value="healthy">healthy</option>
            <option value="unhealthy">unhealthy</option>
            <option value="unknown">unknown</option>
          </select>
        </TermField>
        <TermField label="sort">
          <select v-model="filters.sort" class="term-select">
            <option value="newest">newest</option>
            <option value="oldest">oldest</option>
            <option value="name">name a→z</option>
            <option value="approval">pending first</option>
          </select>
        </TermField>
      </div>
    </TermBox>

    <div v-if="authStore.isAdmin && pendingCount > 0" class="banner">
      <span>{{ pendingCount }} agent(s) pending · use the filter to triage the queue.</span>
    </div>

    <TermBox :title="`agents · ${filteredAgents.length}/${agents.length}`" pad="none" flush>
      <div v-if="loading" class="loading">loading agents…</div>
      <div v-else-if="filteredAgents.length === 0" style="padding: var(--gap-6);">
        <TermEmpty :message="agents.length === 0 ? 'no agents yet · download the template to get started' : 'no agents match the filter'" />
      </div>
      <table v-else class="term-table">
        <thead>
          <tr>
            <th>name</th>
            <th>endpoint</th>
            <th>router description</th>
            <th style="width: 100px">health</th>
            <th style="width: 110px">approval</th>
            <th style="width: 100px">enc</th>
            <th style="width: 14%">created</th>
            <th>ops</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="agent in filteredAgents" :key="agent.id">
            <td>
              <div class="cell-strong">{{ agent.name }}</div>
              <div class="cell-meta">id #{{ agent.id }}</div>
            </td>
            <td>
              <code class="cell-url" :title="agent.endpoint_url">{{ agent.endpoint_url }}</code>
            </td>
            <td>
              <div class="cell-desc" :title="agent.description_for_router">{{ agent.description_for_router }}</div>
            </td>
            <td><TermBadge :variant="healthVariant(agent.health_status)" dot>{{ agent.health_status }}</TermBadge></td>
            <td><TermBadge :variant="approvalVariant(agent.approval_status)" dot>{{ agent.approval_status }}</TermBadge></td>
            <td>
              <TermBadge :variant="agent.requires_encryption ? 'danger' : ''">
                {{ agent.requires_encryption ? 'forced' : 'normal' }}
              </TermBadge>
            </td>
            <td class="cell-meta tnum">{{ formatDate(agent.created_at) }}</td>
            <td>
              <div class="row-actions">
                <button class="term-action" @click="openDetailModal(agent)">detail</button>
                <span class="row-actions__sep">·</span>
                <button v-if="canEditAgent(agent)" class="term-action" @click="openEditModal(agent)">edit</button>
                <span v-if="canEditAgent(agent) && authStore.isAdmin" class="row-actions__sep">·</span>
                <button v-if="authStore.isAdmin" class="term-action" :disabled="healthCheckingId === agent.id" @click="handleHealthCheck(agent)">
                  {{ healthCheckingId === agent.id ? 'probe…' : 'probe' }}
                </button>
                <template v-if="authStore.isAdmin && agent.approval_status === 'pending'">
                  <span class="row-actions__sep">·</span>
                  <button class="term-action" @click="handleApprove(agent)">approve</button>
                  <span class="row-actions__sep">·</span>
                  <button class="term-action term-action--danger" @click="openRejectModal(agent)">reject</button>
                </template>
                <template v-if="authStore.isAdmin">
                  <span class="row-actions__sep">·</span>
                  <button class="term-action term-action--danger" :disabled="deletingId === agent.id" @click="handleDeleteAgent(agent)">
                    {{ deletingId === agent.id ? 'delete…' : 'delete' }}
                  </button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Register modal ----------------------------------------------- -->
    <TermModal :visible="showRegisterModal" title="register · agent" width="640px" @close="showRegisterModal = false">
      <div class="form-grid">
        <TermField label="name" hint="immutable identifier · letters, digits, dashes" :error="formErrors.name">
          <input v-model="form.name" class="term-input" placeholder="hr-policy-agent" />
        </TermField>
        <TermField label="endpoint url" :error="formErrors.endpoint_url">
          <input v-model="form.endpoint_url" class="term-input" placeholder="http://host:port" />
        </TermField>
        <TermField label="router description" hint="≥ 24 chars · plain language describing what this agent solves" :error="formErrors.description_for_router">
          <textarea v-model="form.description_for_router" rows="3" class="term-textarea" />
        </TermField>
        <div class="form-row-2">
          <TermField label="api version">
            <input v-model="form.api_version" class="term-input" placeholder="v1" />
          </TermField>
          <TermField label="base model" :error="formErrors.base_model_id" hint="usage attribution target">
            <select v-model.number="form.base_model_id" class="term-select">
              <option :value="null" disabled>— select base —</option>
              <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
                {{ m.display_name }} ({{ m.name }} · {{ m.model_type }})
              </option>
            </select>
          </TermField>
        </div>

        <TermSection title="pre-flight checklist" />
        <ul class="check">
          <li :class="form.name ? 'is-ok' : 'is-pending'">{{ form.name ? '●' : '○' }} agent name set</li>
          <li :class="/^https?:\/\//.test(form.endpoint_url) ? 'is-ok' : 'is-pending'">{{ /^https?:\/\//.test(form.endpoint_url) ? '●' : '○' }} endpoint is http(s) url</li>
          <li :class="form.description_for_router.trim().length >= 24 ? 'is-ok' : 'is-pending'">{{ form.description_for_router.trim().length >= 24 ? '●' : '○' }} description ≥ 24 chars</li>
          <li :class="form.base_model_id ? 'is-ok' : 'is-pending'">{{ form.base_model_id ? '●' : '○' }} base model selected</li>
          <li class="is-pending">○ <code>GET /health</code> + <code>POST /v1/chat/completions</code> implemented (manual check)</li>
        </ul>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showRegisterModal = false" label="cancel" />
        <TermButton variant="primary" :loading="registering" :disabled="registering" :label="registering ? 'submitting' : 'submit'" @click="handleRegister" />
      </template>
    </TermModal>

    <!-- Edit modal ------------------------------------------------- -->
    <TermModal :visible="showEditModal" title="edit · agent" width="640px" @close="closeEditModal">
      <div class="form-grid" v-if="editTarget">
        <TermField label="name" hint="immutable">
          <input :value="editTarget.name" class="term-input" disabled />
        </TermField>
        <TermField label="endpoint url">
          <input v-model="editForm.endpoint_url" class="term-input" />
        </TermField>
        <TermField label="router description" hint="router uses this to dispatch — be precise">
          <textarea v-model="editForm.description_for_router" rows="4" class="term-textarea" />
        </TermField>
        <div class="form-row-2">
          <TermField label="api version">
            <input v-model="editForm.api_version" class="term-input" />
          </TermField>
          <TermField label="base model">
            <select v-model.number="editForm.base_model_id" class="term-select">
              <option :value="null" disabled>— select base —</option>
              <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
                {{ m.display_name }} ({{ m.name }} · {{ m.model_type }})
              </option>
            </select>
          </TermField>
        </div>
        <TermField label="capabilities · json" hint='e.g. {"streaming":true,"vision":false}' :error="editFormError">
          <textarea v-model="editForm.capabilitiesRaw" rows="3" class="term-textarea" style="font-family: var(--font-mono); font-size: var(--t-xs);" />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeEditModal" label="cancel" />
        <TermButton variant="primary" :disabled="editing" :loading="editing" :label="editing ? 'saving' : 'save'" @click="handleUpdateAgent" />
      </template>
    </TermModal>

    <!-- Detail drawer (modal-style) ------------------------------- -->
    <TermModal :visible="showDetailModal" :title="detailAgent ? `detail · ${detailAgent.name}` : 'detail'" width="640px" @close="showDetailModal = false">
      <div v-if="detailAgent" class="detail">
        <TermSection title="overview" />
        <dl class="detail__list">
          <div><dt>endpoint</dt><dd><code>{{ detailAgent.endpoint_url }}</code></dd></div>
          <div><dt>api version</dt><dd>{{ detailAgent.api_version || 'v1' }}</dd></div>
          <div><dt>health</dt><dd>{{ detailAgent.health_status }}</dd></div>
          <div><dt>approval</dt><dd>{{ detailAgent.approval_status }}</dd></div>
          <div><dt>created</dt><dd class="tnum">{{ formatDate(detailAgent.created_at) }}</dd></div>
          <div><dt>owner</dt><dd>{{ ownerDisplay(detailAgent) }}</dd></div>
          <div><dt>base model</dt><dd>{{ detailAgent.base_model_id || '—' }}</dd></div>
          <div>
            <dt>encryption</dt>
            <dd>
              <TermBadge :variant="detailAgent.requires_encryption ? 'danger' : ''" dot>
                {{ detailAgent.requires_encryption ? 'forced' : 'normal' }}
              </TermBadge>
              <button
                v-if="authStore.isAdmin"
                class="term-action"
                style="margin-left: 8px;"
                :disabled="encryptionBusyId === detailAgent.id"
                @click="handleToggleEncryption(detailAgent)"
              >
                {{ encryptionBusyId === detailAgent.id ? 'updating…' : (detailAgent.requires_encryption ? 'disable' : 'enable') }}
              </button>
            </dd>
          </div>
        </dl>

        <TermSection title="router description" />
        <p class="detail__desc">{{ detailAgent.description_for_router || '—' }}</p>

        <TermSection title="capabilities" />
        <pre v-if="hasCapabilities(detailAgent)" class="detail__pre">{{ prettyJson(detailAgent.capabilities) }}</pre>
        <TermEmpty v-else message="no capabilities declared in manifest" />

        <TermSection title="status timeline" />
        <ol class="timeline">
          <li v-for="entry in buildStatusHistory(detailAgent)" :key="entry.label + entry.timestamp">
            <span class="timeline__dot" />
            <div>
              <p class="timeline__label">{{ entry.label }}</p>
              <p class="cell-meta tnum">{{ entry.timestamp }}</p>
              <p v-if="entry.detail" class="timeline__detail">{{ entry.detail }}</p>
            </div>
          </li>
        </ol>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showDetailModal = false" label="close" />
      </template>
    </TermModal>

    <!-- Reject modal ---------------------------------------------- -->
    <TermModal :visible="!!rejectTarget" title="reject · agent" width="440px" @close="closeRejectModal">
      <p class="cell-meta">leave a reason so the developer can iterate.</p>
      <TermField label="reason">
        <textarea v-model="rejectReason" rows="4" class="term-textarea" placeholder="e.g. missing /health endpoint · description too short" />
      </TermField>
      <template #footer>
        <TermButton variant="ghost" @click="closeRejectModal" label="cancel" />
        <TermButton variant="danger" @click="handleReject" label="confirm reject" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import {
  approveAgent, deleteAgent, downloadTemplate, getAgent, listMyAgents,
  registerAgent, rejectAgent, setAgentEncryption, triggerAgentHealthCheck, updateAgent,
} from '../api/agents'
import { listModels } from '../api/models'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat, TermSection } from '../components/cli'

const authStore = useAuthStore()

const agents = ref([])
const loading = ref(false)
const showGuide = ref(false)
const showRegisterModal = ref(false)
const showDetailModal = ref(false)
const detailAgent = ref(null)
const registering = ref(false)
const rejectTarget = ref(null)
const rejectReason = ref('')
const encryptionBusyId = ref(null)
const deletingId = ref(null)
const healthCheckingId = ref(null)
const showEditModal = ref(false)
const editTarget = ref(null)
const editing = ref(false)
const editFormError = ref('')
const editForm = ref({ endpoint_url: '', description_for_router: '', api_version: '', base_model_id: null, capabilitiesRaw: '' })
const feedback = ref({ type: 'success', message: '' })
const filters = ref({ query: '', approval: 'all', health: 'all', sort: 'newest' })
const form = ref({ name: '', endpoint_url: '', description_for_router: '', api_version: 'v1', base_model_id: null })
const formErrors = ref({})
const availableModels = ref([])
const baseModelOptions = computed(() =>
  availableModels.value.filter(m => m.is_active && (m.model_type === 'llm' || m.model_type === 'vlm'))
)

const pendingCount = computed(() => agents.value.filter(a => a.approval_status === 'pending').length)
const approvedCount = computed(() => agents.value.filter(a => a.approval_status === 'approved').length)
const healthyCount = computed(() => agents.value.filter(a => a.health_status === 'healthy' || a.health_status === 'online').length)

function ownerDisplay(agent) {
  if (!agent) return '—'
  if (agent.owner_username) return `${agent.owner_username}${agent.owner_user_id ? ` (#${agent.owner_user_id})` : ''}`
  return agent.owner_user_id ? `#${agent.owner_user_id}` : '—'
}
function hasCapabilities(agent) {
  const c = agent?.capabilities
  if (!c) return false
  if (typeof c !== 'object') return true
  return Object.keys(c).length > 0
}

const filteredAgents = computed(() => {
  const query = filters.value.query.trim().toLowerCase()
  let next = agents.value.filter(a => {
    if (filters.value.approval !== 'all' && a.approval_status !== filters.value.approval) return false
    if (filters.value.health !== 'all' && a.health_status !== filters.value.health) return false
    if (!query) return true
    return [a.name, a.endpoint_url, a.description_for_router, a.id].filter(Boolean).join(' ').toLowerCase().includes(query)
  })
  next = [...next].sort((l, r) => {
    if (filters.value.sort === 'name') return (l.name || '').localeCompare(r.name || '')
    if (filters.value.sort === 'oldest') return new Date(l.created_at) - new Date(r.created_at)
    if (filters.value.sort === 'approval') return (l.approval_status === 'pending' ? -1 : 1) - (r.approval_status === 'pending' ? -1 : 1)
    return new Date(r.created_at) - new Date(l.created_at)
  })
  return next
})

function setFeedback(type, message) {
  feedback.value = { type, message }
  if (message) setTimeout(() => { feedback.value = { type: 'success', message: '' } }, 5000)
}

function resetForm() {
  form.value = { name: '', endpoint_url: '', description_for_router: '', api_version: 'v1', base_model_id: null }
  formErrors.value = {}
}

function validateForm() {
  const errors = {}
  if (!form.value.name.trim()) errors.name = 'agent name required'
  if (!/^https?:\/\//.test(form.value.endpoint_url.trim())) errors.endpoint_url = 'must be http or https url'
  if (form.value.description_for_router.trim().length < 24) errors.description_for_router = 'min 24 chars'
  if (!form.value.base_model_id) errors.base_model_id = 'base model required for usage attribution'
  formErrors.value = errors
  return Object.keys(errors).length === 0
}

async function fetchAgents() {
  loading.value = true
  try { const { data } = await listMyAgents(); agents.value = data }
  catch (e) { setFeedback('error', e.response?.data?.detail || 'failed to load agents') }
  finally { loading.value = false }
}
async function fetchAvailableModels() {
  try { const { data } = await listModels(); availableModels.value = data }
  catch { availableModels.value = [] }
}
onMounted(async () => { await Promise.all([fetchAgents(), fetchAvailableModels()]) })

function openRegisterModal() { resetForm(); showRegisterModal.value = true }

async function openDetailModal(agent) {
  try { const { data } = await getAgent(agent.id); detailAgent.value = data }
  catch { detailAgent.value = agent }
  showDetailModal.value = true
}

function openRejectModal(agent) { rejectTarget.value = agent; rejectReason.value = '' }
function closeRejectModal() { rejectTarget.value = null; rejectReason.value = '' }

async function handleRegister() {
  if (!validateForm()) return
  registering.value = true
  try {
    await registerAgent({
      ...form.value,
      name: form.value.name.trim(),
      endpoint_url: form.value.endpoint_url.trim(),
      description_for_router: form.value.description_for_router.trim(),
    })
    showRegisterModal.value = false
    setFeedback('success', 'agent submitted · pending admin review')
    await fetchAgents()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'register failed') }
  finally { registering.value = false }
}

function canEditAgent(agent) {
  if (!agent) return false
  if (authStore.isAdmin) return true
  return agent.owner_user_id === authStore.user?.id
}
function openEditModal(agent) {
  editTarget.value = agent
  editForm.value = {
    endpoint_url: agent.endpoint_url || '',
    description_for_router: agent.description_for_router || '',
    api_version: agent.api_version || 'v1',
    base_model_id: agent.base_model_id ?? null,
    capabilitiesRaw: agent.capabilities && Object.keys(agent.capabilities).length
      ? JSON.stringify(agent.capabilities, null, 2) : '',
  }
  editFormError.value = ''
  showEditModal.value = true
}
function closeEditModal() { showEditModal.value = false; editTarget.value = null; editFormError.value = '' }

async function handleUpdateAgent() {
  if (!editTarget.value || editing.value) return
  if (!editForm.value.base_model_id) { editFormError.value = 'base model required'; return }
  let capabilities = null
  const raw = (editForm.value.capabilitiesRaw || '').trim()
  if (raw) {
    try {
      capabilities = JSON.parse(raw)
      if (typeof capabilities !== 'object' || Array.isArray(capabilities)) throw new Error('capabilities must be an object')
    } catch (err) { editFormError.value = `capabilities json error: ${err.message}`; return }
  }
  const patch = {
    endpoint_url: editForm.value.endpoint_url.trim() || null,
    description_for_router: (editForm.value.description_for_router || '').trim() || null,
    api_version: (editForm.value.api_version || '').trim() || null,
    base_model_id: editForm.value.base_model_id,
    capabilities,
  }
  editing.value = true
  try {
    const { data } = await updateAgent(editTarget.value.id, patch)
    const idx = agents.value.findIndex(a => a.id === data.id)
    if (idx >= 0) agents.value[idx] = data
    if (detailAgent.value && detailAgent.value.id === data.id) detailAgent.value = data
    setFeedback('success', `updated '${data.name}'`)
    closeEditModal()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'update failed') }
  finally { editing.value = false }
}

async function handleApprove(agent) {
  try { await approveAgent(agent.id); setFeedback('success', `approved '${agent.name}'`); await fetchAgents() }
  catch (e) { setFeedback('error', e.response?.data?.detail || 'approve failed') }
}

async function handleToggleEncryption(agent) {
  if (!agent || encryptionBusyId.value === agent.id) return
  const next = !agent.requires_encryption
  if (next && !window.confirm(`enable forced encryption for '${agent.name}'? all conversations through it lock to encrypted mode — irreversible per conversation.`)) return
  encryptionBusyId.value = agent.id
  try {
    const { data } = await setAgentEncryption(agent.id, next)
    const applied = Boolean(data?.requires_encryption ?? next)
    const idx = agents.value.findIndex(a => a.id === agent.id)
    if (idx !== -1) agents.value[idx] = { ...agents.value[idx], requires_encryption: applied }
    if (detailAgent.value && detailAgent.value.id === agent.id) detailAgent.value = { ...detailAgent.value, requires_encryption: applied }
    setFeedback('success', `encryption ${applied ? 'enabled' : 'disabled'} for '${agent.name}'`)
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'encryption update failed') }
  finally { encryptionBusyId.value = null }
}

async function handleHealthCheck(agent) {
  if (!agent || healthCheckingId.value === agent.id) return
  healthCheckingId.value = agent.id
  try {
    const { data } = await triggerAgentHealthCheck(agent.id)
    const idx = agents.value.findIndex(a => a.id === agent.id)
    if (idx >= 0) agents.value[idx] = { ...agents.value[idx], health_status: data.status }
    setFeedback(data.status === 'healthy' ? 'success' : 'error',
      `'${agent.name}' health: ${data.status}${data.detail ? ` — ${data.detail}` : ''}`)
  } catch (e) { setFeedback('error', e.response?.data?.detail || `'${agent.name}' health probe failed`) }
  finally { healthCheckingId.value = null }
}

async function handleDeleteAgent(agent) {
  if (!agent || deletingId.value === agent.id) return
  if (!window.confirm(`delete '${agent.name}'? non-reversible · live references will break.`)) return
  deletingId.value = agent.id
  try {
    await deleteAgent(agent.id)
    agents.value = agents.value.filter(a => a.id !== agent.id)
    if (detailAgent.value && detailAgent.value.id === agent.id) { showDetailModal.value = false; detailAgent.value = null }
    setFeedback('success', `deleted '${agent.name}'`)
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'delete failed') }
  finally { deletingId.value = null }
}

async function handleReject() {
  try {
    await rejectAgent(rejectTarget.value.id, rejectReason.value.trim())
    setFeedback('success', `rejected '${rejectTarget.value.name}'`)
    closeRejectModal()
    await fetchAgents()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'reject failed') }
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
    setFeedback('success', 'template downloaded')
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'download failed') }
}

function approvalVariant(s) {
  return ({ pending: 'warn', approved: 'ok', rejected: 'danger' })[s] || ''
}
function healthVariant(s) {
  if (s === 'healthy' || s === 'online') return 'ok'
  if (s === 'unhealthy' || s === 'offline') return 'danger'
  return ''
}
function formatDate(s) { return s ? new Date(s).toLocaleString('en-GB') : '—' }
function prettyJson(value) { return JSON.stringify(value || {}, null, 2) }
function buildStatusHistory(agent) {
  const history = [{ label: 'agent registered', timestamp: formatDate(agent.created_at), detail: 'endpoint and description bound to registry' }]
  if (agent.approval_status === 'approved') {
    history.push({ label: 'approved', timestamp: formatDate(agent.approved_at), detail: agent.approved_by ? `by user #${agent.approved_by}` : '' })
  }
  if (agent.approval_status === 'rejected') {
    history.push({ label: 'rejected', timestamp: formatDate(agent.updated_at || agent.created_at), detail: 'check health, description, and endpoint then re-submit' })
  }
  history.push({ label: `health · ${agent.health_status || 'unknown'}`, timestamp: formatDate(agent.updated_at || agent.created_at), detail: 'derived from latest probe' })
  return history
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__actions { display: inline-flex; gap: var(--gap-2); }

.feedback { display: flex; gap: var(--gap-2); align-items: center; font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.feedback.is-ok  { color: var(--c-ok);     border-color: var(--c-ok);     background: var(--c-ok-soft); }

.banner {
  font-size: var(--t-xs);
  color: var(--c-warn);
  border: var(--border-w) solid var(--c-warn);
  background: var(--c-warn-soft);
  padding: var(--gap-2) var(--gap-3);
}

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-3); }
@media (max-width: 800px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }

.filters { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: var(--gap-3); }
@media (max-width: 800px) { .filters { grid-template-columns: 1fr 1fr; } }

.guide-toggle {
  background: transparent;
  border: 0;
  color: var(--c-fg-1);
  font: inherit;
  cursor: pointer;
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  padding: 0;
}
.guide-toggle:hover { color: var(--c-accent); }
.guide { margin-top: var(--gap-3); }
.guide__list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--gap-3); }
.guide__list li { display: grid; grid-template-columns: 28px 1fr; gap: var(--gap-3); font-size: var(--t-sm); color: var(--c-fg-2); }
.guide__list li p { margin: 0 0 4px; }
.guide__list li code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}
.guide__step {
  font-size: var(--t-xs); color: var(--c-fg-mute);
  font-variant-numeric: tabular-nums;
}
.guide__table { margin-top: 6px; }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-desc {
  font-size: var(--t-xs); color: var(--c-fg-2);
  max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cell-url {
  display: inline-block;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
}

.row-actions { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.loading { padding: var(--gap-6); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }

.check { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 4px; font-size: var(--t-2xs); }
.check li.is-ok { color: var(--c-ok); }
.check li.is-pending { color: var(--c-fg-3); }
.check code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 0 4px;
  color: var(--c-fg-2);
}

/* Detail drawer */
.detail__list {
  display: grid; grid-template-columns: 110px 1fr; gap: 4px var(--gap-3); margin: 0;
  font-size: var(--t-sm);
}
.detail__list dt { color: var(--c-fg-3); font-size: var(--t-2xs); text-transform: uppercase; letter-spacing: var(--tracking-caps); }
.detail__list dd { margin: 0; color: var(--c-fg-1); }
.detail__list code { font-family: var(--font-mono); font-size: var(--t-2xs); color: var(--c-fg-2); }
.detail__desc { color: var(--c-fg-2); white-space: pre-wrap; font-size: var(--t-sm); margin: 0; }
.detail__pre {
  background: var(--c-bg); border: var(--border-w) solid var(--c-border);
  padding: var(--gap-3); margin: 0; font-size: var(--t-2xs); color: var(--c-fg-2);
  max-height: 240px; overflow: auto;
}

.timeline { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--gap-2); }
.timeline li {
  display: grid; grid-template-columns: 12px 1fr; gap: var(--gap-2);
  padding: var(--gap-2) 0; border-bottom: var(--border-w) dashed var(--c-border);
}
.timeline li:last-child { border-bottom: 0; }
.timeline__dot {
  width: 8px; height: 8px; background: var(--c-accent); margin-top: 4px;
}
.timeline__label { color: var(--c-fg-1); font-size: var(--t-sm); margin: 0; font-weight: 500; }
.timeline__detail { color: var(--c-fg-2); font-size: var(--t-2xs); margin: 4px 0 0; }
</style>
