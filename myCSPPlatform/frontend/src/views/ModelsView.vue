<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">control plane · registry</p>
        <h1 class="page-head__title">models</h1>
        <p class="page-head__sub">
          llm · vlm · embedding · agent — registered endpoints proxied via /v1/*
        </p>
      </div>
      <TermButton v-if="authStore.isAdmin" variant="primary" @click="openCreateModal" label="register model" />
    </header>

    <div class="kpi-row">
      <TermStat label="models · total" :value="modelsStore.models.length" />
      <TermStat label="online" :value="onlineCount" tone="accent" />
      <TermStat label="connecting" :value="connectingCount" />
      <TermStat label="offline" :value="offlineCount" :tone="offlineCount ? 'danger' : 'default'" />
    </div>

    <TermBox :title="`registry · ${modelsStore.models.length}`" hint="health-checked every 60s" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 60px">health</th>
            <th>name</th>
            <th style="width: 100px">type</th>
            <th>endpoint</th>
            <th style="width: 80px">api</th>
            <th style="width: 80px">active</th>
            <th style="width: 110px">router</th>
            <th v-if="authStore.isAdmin" style="width: 26%">ops</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="model in modelsStore.models" :key="model.id">
            <td>
              <span class="health">
                <TermDot :status="healthStatus(model.health_status)" :title="healthLabel(model.health_status)" />
                <span class="health__txt">{{ healthLabel(model.health_status) }}</span>
              </span>
            </td>
            <td>
              <div class="cell-strong">{{ model.display_name }}</div>
              <div class="cell-meta">{{ model.name }}</div>
              <div v-if="model.base_model_name" class="cell-base">↳ base: {{ model.base_model_name }}</div>
            </td>
            <td><TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge></td>
            <td>
              <span
                v-if="model.endpoint_url === ENDPOINT_REDACTED"
                class="cell-meta"
                title="endpoint URL is owner-only (deployment topology)"
              >🔒 owner-only</span>
              <code v-else class="cell-url" :title="model.endpoint_url">{{ model.endpoint_url }}</code>
            </td>
            <td class="cell-meta">{{ model.api_version }}</td>
            <td>
              <TermBadge :variant="model.is_active ? 'ok' : 'danger'" dot>
                {{ model.is_active ? 'on' : 'off' }}
              </TermBadge>
            </td>
            <td>
              <span v-if="model.is_router_primary" class="primary-pill" title="ANILA Router uses this as primary LLM">
                ★ primary
              </span>
              <span v-else class="cell-meta">—</span>
            </td>
            <td v-if="authStore.isAdmin">
              <div class="row-actions">
                <button class="term-action" @click="openEditModal(model)">edit</button>
                <span class="row-actions__sep">·</span>
                <button class="term-action" @click="handleHealthCheck(model.id)">probe</button>
                <span v-if="model.model_type === 'llm' && !model.is_router_primary" class="row-actions__sep">·</span>
                <button
                  v-if="model.model_type === 'llm' && !model.is_router_primary"
                  class="term-action"
                  :disabled="!model.is_active || settingPrimaryId === model.id"
                  @click="handleSetPrimary(model.id)"
                >
                  {{ settingPrimaryId === model.id ? 'pinning…' : 'set-primary' }}
                </button>
                <span v-else-if="model.is_router_primary" class="row-actions__sep">·</span>
                <button
                  v-if="model.is_router_primary"
                  class="term-action"
                  :disabled="settingPrimaryId === model.id"
                  @click="handleUnsetPrimary(model.id)"
                >
                  unpin
                </button>
                <span class="row-actions__sep">·</span>
                <button
                  v-if="model.is_active"
                  class="term-action"
                  @click="handleDeactivate(model.id)"
                >deactivate</button>
                <button
                  v-else
                  class="term-action"
                  @click="handleActivate(model.id)"
                >activate</button>
                <span v-if="authStore.isOwner" class="row-actions__sep">·</span>
                <button
                  v-if="authStore.isOwner"
                  class="term-action term-action--danger"
                  :disabled="purgingId === model.id"
                  :title="'hard-delete this row · irreversible · owner-only'"
                  @click="handlePurge(model)"
                >
                  {{ purgingId === model.id ? 'purging…' : 'purge' }}
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="modelsStore.models.length === 0">
            <td :colspan="authStore.isAdmin ? 8 : 7"><TermEmpty message="no models registered · register one to enable /v1/* proxy" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? 'edit · model' : 'register · model'" width="600px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="model id" hint="immutable · used in api requests · e.g. llama3-70b">
          <input v-model="form.name" :disabled="!!editingId" class="term-input" placeholder="llama3-70b" />
        </TermField>
        <TermField label="display name">
          <input v-model="form.display_name" class="term-input" placeholder="Llama 3 70B Instruct" />
        </TermField>
        <div class="form-row-2">
          <TermField label="type">
            <select v-model="form.model_type" class="term-select">
              <option value="llm">llm</option>
              <option value="vlm">vlm</option>
              <option value="embedding">embedding</option>
              <option value="agent">agent</option>
            </select>
          </TermField>
          <TermField label="api version">
            <select v-model="form.api_version" class="term-select">
              <option value="v1">v1</option>
              <option value="v2">v2</option>
            </select>
          </TermField>
        </div>
        <TermField label="endpoint url" :hint="endpointFieldLocked ? '🔒 owner-only — admins keep the registered URL untouched on update' : ''">
          <input
            v-model="form.endpoint_url"
            class="term-input"
            :disabled="endpointFieldLocked"
            :placeholder="endpointFieldLocked ? '— owner-only —' : 'http://gpu-server:8080'"
          />
        </TermField>
        <TermField label="description" optional>
          <textarea v-model="form.description" rows="2" class="term-textarea" />
        </TermField>
        <TermField label="context window" optional hint="tokens">
          <input v-model.number="form.context_window" type="number" class="term-input" placeholder="128000" />
        </TermField>
        <TermField v-if="form.model_type === 'agent'" label="base model" hint="for usage attribution">
          <select v-model="form.base_model_id" class="term-select">
            <option :value="null">— standalone —</option>
            <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
              {{ m.display_name }} ({{ m.model_type }})
            </option>
          </select>
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="cancel" />
        <TermButton
          variant="primary"
          :disabled="!form.name || !form.display_name || !form.endpoint_url"
          :label="editingId ? 'update' : 'register'"
          @click="handleSubmit"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useModelsStore } from '../stores/models'
import { useAuthStore } from '../stores/auth'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat, TermDot } from '../components/cli'

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

const baseModelOptions = computed(() =>
  modelsStore.models.filter(m =>
    m.model_type !== 'agent' && m.is_active && m.id !== editingId.value
  )
)

const onlineCount = computed(() => modelsStore.models.filter(m => m.health_status === 'online').length)
const connectingCount = computed(() => modelsStore.models.filter(m => m.health_status === 'connecting').length)
const offlineCount = computed(() => modelsStore.models.filter(m => m.health_status === 'offline').length)

onMounted(() => modelsStore.fetchModels())

function healthStatus(s) {
  return ({ online: 'ok', connecting: 'warn', offline: 'danger' })[s] || 'idle'
}
function healthLabel(s) {
  return ({ online: 'online', connecting: 'connecting', offline: 'offline' })[s] || s || 'unknown'
}

// Owner-only sentinel returned by backend when endpoint_url is redacted.
// Keep in sync with myCSPPlatform/backend/app/api/models.py::ENDPOINT_REDACTED.
const ENDPOINT_REDACTED = '<owner-only>'

function openCreateModal() { editingId.value = null; form.value = defaultForm(); showModal.value = true }
function openEditModal(model) {
  editingId.value = model.id
  // Drop the sentinel before populating the form — otherwise saving
  // would PUT the literal "<owner-only>" string back to backend and
  // corrupt the registered endpoint. Non-owner admins see a placeholder
  // hint instead and the field is disabled.
  const endpointUrl = model.endpoint_url === ENDPOINT_REDACTED ? '' : model.endpoint_url
  form.value = {
    name: model.name, display_name: model.display_name,
    model_type: model.model_type, endpoint_url: endpointUrl,
    api_version: model.api_version, description: model.description || '',
    context_window: model.context_window, base_model_id: model.base_model_id || null,
  }
  showModal.value = true
}

const endpointFieldLocked = computed(() =>
  !!editingId.value && !authStore.isOwner,
)

async function handleSubmit() {
  try {
    const payload = { ...form.value }
    if (payload.model_type !== 'agent') payload.base_model_id = null
    // Don't ship endpoint_url back when the field was locked (admin
    // editing a row whose URL they couldn't see). Backend would accept
    // the empty string and overwrite the real endpoint with junk.
    if (endpointFieldLocked.value) delete payload.endpoint_url
    if (editingId.value) {
      const { name, ...updateData } = payload
      await modelsStore.update(editingId.value, updateData)
    } else {
      await modelsStore.create(payload)
    }
    showModal.value = false
  } catch (e) {
    alert(e.response?.data?.detail || 'operation failed')
  }
}

async function handleHealthCheck(id) {
  const result = await modelsStore.checkHealth(id)
  alert(`health probe → ${result.status}\n${result.detail}`)
}

async function handleSetPrimary(id) {
  settingPrimaryId.value = id
  try { await modelsStore.setPrimary(id) }
  catch (e) { alert(e.response?.data?.detail || 'pin failed') }
  finally { settingPrimaryId.value = null }
}
async function handleUnsetPrimary(id) {
  if (!confirm('unpin primary? ANILA Router will have no primary LLM until you pin a new one.')) return
  settingPrimaryId.value = id
  try { await modelsStore.unsetPrimary(id) }
  catch (e) { alert(e.response?.data?.detail || 'unpin failed') }
  finally { settingPrimaryId.value = null }
}
async function handleDeactivate(id) {
  if (confirm('deactivate this model? you can re-activate it later via the activate button on this row.')) {
    await modelsStore.remove(id)
  }
}
async function handleActivate(id) {
  try { await modelsStore.activate(id) }
  catch (e) { alert(e.response?.data?.detail || 'activate failed') }
}
async function handlePurge(model) {
  if (!model || purgingId.value === model.id) return
  if (!window.confirm(`hard-delete '${model.display_name}'? non-reversible. rejected if usage records or other models reference it.`)) return
  purgingId.value = model.id
  try { await modelsStore.purge(model.id) }
  catch (e) { alert(e.response?.data?.detail || 'purge failed') }
  finally { purgingId.value = null }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-3); }
@media (max-width: 800px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-base { color: var(--c-info); font-size: var(--t-2xs); margin-top: 2px; }
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

.health { display: inline-flex; align-items: center; gap: 6px; }
.health__txt { font-size: var(--t-2xs); color: var(--c-fg-3); text-transform: lowercase; }

.primary-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--t-2xs);
  color: var(--c-warn);
  border: var(--border-w) solid var(--c-warn);
  padding: 1px 6px;
  background: var(--c-warn-soft);
  letter-spacing: 0.04em;
}

.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); flex-wrap: wrap; }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
</style>
