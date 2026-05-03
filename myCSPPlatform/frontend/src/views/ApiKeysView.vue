<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">control plane · credentials</p>
        <h1 class="page-head__title">api-keys</h1>
        <p class="page-head__sub">
          openai-compatible bearer keys for the data plane <code class="page-head__code">/v1/*</code>
        </p>
      </div>
      <TermButton variant="primary" @click="showCreateModal = true" label="provision key" />
    </header>

    <TermBox title="keys · all" :hint="`${keysStore.keys.length} record(s)`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 18%">name</th>
            <th style="width: 22%">key</th>
            <th>allowed models</th>
            <th style="width: 14%">created</th>
            <th style="width: 14%">last used</th>
            <th style="width: 8%">status</th>
            <th style="width: 14%">ops</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="key in keysStore.keys" :key="key.id">
            <td>
              <div class="cell-strong">{{ key.name }}</div>
              <div class="cell-meta">id #{{ key.id }}</div>
            </td>
            <td>
              <code class="cell-code">{{ key.key_prefix }}…{{ key.key_suffix }}</code>
            </td>
            <td>
              <div v-if="key.allowed_model_names.length" class="chip-row">
                <TermBadge v-for="name in key.allowed_model_names" :key="name" variant="info">{{ name }}</TermBadge>
              </div>
              <span v-else class="cell-meta">—</span>
            </td>
            <td class="cell-meta tnum">{{ formatDate(key.created_at) }}</td>
            <td class="cell-meta tnum">{{ key.last_used_at ? formatDate(key.last_used_at) : 'never' }}</td>
            <td>
              <TermBadge :variant="key.is_active ? 'ok' : 'danger'" dot>
                {{ key.is_active ? 'active' : 'revoked' }}
              </TermBadge>
            </td>
            <td>
              <div class="row-actions">
                <button v-if="key.is_active" class="term-action" @click="confirmRegenerate(key)">regen</button>
                <span v-if="key.is_active" class="row-actions__sep">·</span>
                <button v-if="key.is_active" class="term-action term-action--danger" @click="confirmRevoke(key)">revoke</button>
              </div>
            </td>
          </tr>
          <tr v-if="keysStore.keys.length === 0">
            <td colspan="7"><TermEmpty message="no api keys yet · click [provision key] to create one" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Create modal --------------------------------------------------- -->
    <TermModal :visible="showCreateModal" title="provision · new api-key" width="520px" @close="showCreateModal = false">
      <div class="form-grid">
        <TermField label="name" hint="for your records · not exposed in /v1/*">
          <input v-model="newKey.name" class="term-input" placeholder="e.g. dev-laptop · ci-runner-1" />
        </TermField>
        <TermField label="expires at" hint="leave blank for non-expiring">
          <input v-model="newKey.expires_at" type="datetime-local" class="term-input" />
        </TermField>

        <TermField v-if="authStore.isAdmin" label="allowed models" hint="server enforces · key cannot exceed user allowlist">
          <div class="check-list term-box term-box--inset" style="padding: 8px 12px; max-height: 200px; overflow:auto;">
            <label v-for="model in allModels" :key="model.id" class="check-list__row">
              <input type="checkbox" :value="model.id" v-model="newKey.model_ids" />
              <span>{{ model.display_name }}</span>
              <TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge>
            </label>
            <p v-if="allModels.length === 0" class="cell-meta">no models registered yet</p>
          </div>
        </TermField>

        <TermField v-else label="your allowlist" hint="set by admin · cannot be widened from this dialog">
          <div class="term-box term-box--inset" style="padding: 8px 12px;">
            <div v-if="myAllowedModels.length" class="chip-row">
              <TermBadge v-for="m in myAllowedModels" :key="m.id" variant="info">{{ m.display_name }}</TermBadge>
            </div>
            <p v-else class="cell-meta">no models assigned · contact an admin</p>
          </div>
        </TermField>
      </div>

      <template #footer>
        <TermButton variant="ghost" @click="showCreateModal = false" label="cancel" />
        <TermButton
          variant="primary"
          :disabled="!canCreate"
          :loading="creating"
          :label="creating ? 'provisioning' : 'provision'"
          :title="!canCreate ? createDisabledReason : ''"
          @click="handleCreate"
        />
      </template>
    </TermModal>

    <!-- Reveal modal --------------------------------------------------- -->
    <TermModal :visible="showKeyModal" :title="keyModalTitle" width="600px" :dismissible="hasCopied" @close="closeKeyModal">
      <p class="reveal__warn">! copy this key now — it cannot be retrieved again.</p>
      <pre class="reveal__key">{{ createdFullKey }}</pre>
      <p class="reveal__hint">
        store in your secret manager · use as <code>Authorization: Bearer …</code>
      </p>
      <template #footer>
        <TermButton variant="default" @click="copyKey" :label="copied ? 'copied ✓' : 'copy'" />
        <TermButton variant="primary" :disabled="!hasCopied" :title="!hasCopied ? 'copy first' : ''" @click="closeKeyModal" label="done" />
      </template>
    </TermModal>

    <!-- Confirm dialogs ------------------------------------------------- -->
    <TermConfirm
      :visible="showRevokeConfirm"
      title="revoke · api-key"
      :message="`revoke '${revokeTarget?.name}'? in-flight requests using this key will be rejected immediately.`"
      confirm-text="revoke"
      :danger="true"
      @confirm="handleRevoke"
      @cancel="showRevokeConfirm = false"
    />
    <TermConfirm
      :visible="showRegenerateConfirm"
      title="regenerate · api-key"
      :message="`regenerate '${regenerateTarget?.name}'? the existing key invalidates immediately, a new sk-… is issued with the same name and allowlist.`"
      confirm-text="regenerate"
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
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermConfirm } from '../components/cli'

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
const keyModalTitle = ref('api-key · created')
const copied = ref(false)
const hasCopied = ref(false)

const newKey = ref({ name: '', model_ids: [], expires_at: '' })

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
  if (!(newKey.value.name || '').trim()) return 'name cannot be blank'
  if (authStore.isAdmin && (newKey.value.model_ids || []).length === 0) return 'pick at least one model'
  if (!authStore.isAdmin && myAllowedModels.value.length === 0) return 'no models in allowlist · contact admin'
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
      model_ids: authStore.isAdmin ? newKey.value.model_ids : myAllowedModels.value.map(m => m.id),
      expires_at: newKey.value.expires_at || null,
    }
    const data = await keysStore.create(payload)
    createdFullKey.value = data.full_key
    keyModalTitle.value = 'api-key · created'
    showCreateModal.value = false
    showKeyModal.value = true
    copied.value = false
    hasCopied.value = false
    newKey.value = { name: '', model_ids: [], expires_at: '' }
  } catch (e) {
    alert(e.response?.data?.detail || 'create failed')
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
  if (!hasCopied.value) return
  showKeyModal.value = false
  createdFullKey.value = ''
}

function confirmRevoke(key) { revokeTarget.value = key; showRevokeConfirm.value = true }
async function handleRevoke() {
  if (revokeTarget.value) await keysStore.revoke(revokeTarget.value.id)
  showRevokeConfirm.value = false
  revokeTarget.value = null
}

function confirmRegenerate(key) { regenerateTarget.value = key; showRegenerateConfirm.value = true }
async function handleRegenerate() {
  showRegenerateConfirm.value = false
  if (!regenerateTarget.value) return
  try {
    const data = await keysStore.regenerate(regenerateTarget.value.id)
    createdFullKey.value = data.full_key
    keyModalTitle.value = 'api-key · regenerated'
    showKeyModal.value = true
    copied.value = false
    hasCopied.value = false
  } catch (e) {
    alert(e.response?.data?.detail || 'regen failed')
  } finally {
    regenerateTarget.value = null
  }
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleString('en-GB')
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: var(--gap-3);
  flex-wrap: wrap;
}
.page-head__eyebrow {
  font-size: var(--t-2xs);
  letter-spacing: var(--tracking-caps);
  text-transform: uppercase;
  color: var(--c-fg-3);
}
.page-head__title {
  font-size: var(--t-2xl);
  font-weight: 600;
  letter-spacing: var(--tracking-tight);
  margin: 4px 0 2px;
}
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__code { color: var(--c-accent); background: var(--c-accent-soft); padding: 0 4px; border-radius: var(--r-soft); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-code {
  font-family: var(--font-mono);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
}

.chip-row { display: flex; flex-wrap: wrap; gap: 4px; }
.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.check-list__row {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: 4px 0;
  font-size: var(--t-sm);
  color: var(--c-fg-1);
  cursor: pointer;
}
.check-list__row input { accent-color: var(--c-accent); }

.reveal__warn {
  font-size: var(--t-xs);
  color: var(--c-danger);
  letter-spacing: 0.04em;
  margin-bottom: var(--gap-2);
}
.reveal__key {
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border-strong);
  padding: var(--gap-3);
  font-family: var(--font-mono);
  font-size: var(--t-sm);
  color: var(--c-accent);
  word-break: break-all;
  white-space: pre-wrap;
  user-select: all;
  margin: 0;
}
.reveal__hint {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  margin-top: var(--gap-2);
}
.reveal__hint code {
  font-family: var(--font-mono);
  color: var(--c-fg-2);
}
</style>
