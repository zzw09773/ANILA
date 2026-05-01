<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">service clients</h1>
        <p class="page-subtitle">
          Router / worker / admin-tool 走 <code>X-CSP-Service-Token</code> 的 s2s identity。每一列 = 一條 long-lived <code>csk-</code>。
        </p>
      </div>
    </header>

    <TermBox v-if="feedback.message" :tone="feedback.type" dismissible @dismiss="feedback.message = ''">
      {{ feedback.message }}
    </TermBox>

    <div class="row-actions" style="margin: 12px 0;">
      <TermButton variant="primary" @click="openCreateModal" label="+ create client" />
      <span class="row-actions__sep">·</span>
      <button class="term-action" @click="fetchClients">refresh</button>
    </div>

    <TermBox>
      <table class="data-table">
        <thead>
          <tr>
            <th>id</th>
            <th>name</th>
            <th>type</th>
            <th>status</th>
            <th>issued</th>
            <th>rotated</th>
            <th>grace</th>
            <th>actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!clients.length">
            <td colspan="8">
              <TermEmpty message="no service clients yet" />
            </td>
          </tr>
          <tr v-for="c in clients" :key="c.id" :class="{ 'is-revoked': !c.is_active }">
            <td class="tnum">{{ c.id }}</td>
            <td>
              <div class="cell-strong">{{ c.client_name }}</div>
              <div v-if="c.description" class="cell-meta">{{ c.description }}</div>
            </td>
            <td>
              <TermBadge>{{ c.client_type }}</TermBadge>
              <TermBadge v-if="c.is_legacy" variant="warn" style="margin-left: 6px;">legacy</TermBadge>
            </td>
            <td>
              <TermBadge :variant="c.is_active ? '' : 'danger'" dot>
                {{ c.is_active ? 'active' : 'revoked' }}
              </TermBadge>
            </td>
            <td class="cell-meta tnum">{{ formatDate(c.issued_at) }}</td>
            <td class="cell-meta tnum">{{ c.rotated_at ? formatDate(c.rotated_at) : '—' }}</td>
            <td class="cell-meta tnum">
              <span v-if="c.has_previous_token">until {{ formatDate(c.previous_expires_at) }}</span>
              <span v-else>—</span>
            </td>
            <td>
              <div class="row-actions" v-if="c.is_active">
                <button class="term-action" :disabled="busyId === c.id" @click="handleRotate(c)">
                  {{ busyId === c.id ? '…' : 'rotate' }}
                </button>
                <span class="row-actions__sep">·</span>
                <button class="term-action term-action--danger" :disabled="busyId === c.id" @click="handleRevoke(c)">
                  {{ busyId === c.id ? '…' : 'revoke' }}
                </button>
              </div>
              <span v-else class="cell-meta">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Plaintext display -->
    <TermModal :visible="!!issuedSecret" title="copy this token now" width="540px" @close="clearIssuedSecret">
      <p class="cell-meta">
        plaintext 只會出現一次。複製後妥善保存（password manager / vault）。
      </p>
      <div class="secret-banner">
        <div class="secret-banner__body">
          <code class="secret-banner__token">{{ issuedSecret?.value }}</code>
        </div>
        <ul v-if="issuedSecret?.meta" class="secret-banner__meta">
          <li v-if="issuedSecret.meta.client_name">client: <code>{{ issuedSecret.meta.client_name }}</code></li>
          <li v-if="issuedSecret.meta.client_type">type: {{ issuedSecret.meta.client_type }}</li>
          <li v-if="issuedSecret.meta.note">{{ issuedSecret.meta.note }}</li>
        </ul>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="copySecret" label="copy" />
        <TermButton variant="primary" @click="clearIssuedSecret" label="done" />
      </template>
    </TermModal>

    <!-- Create modal -->
    <TermModal :visible="showCreateModal" title="create · service client" width="520px" @close="showCreateModal = false">
      <div class="form-grid">
        <TermField label="name" hint="immutable identifier (e.g. router-primary, ingestion-worker)">
          <input v-model="createForm.client_name" class="term-input" placeholder="router-primary" />
        </TermField>
        <TermField label="type">
          <select v-model="createForm.client_type" class="term-select">
            <option value="router">router</option>
            <option value="worker">worker</option>
            <option value="admin_tool">admin_tool</option>
          </select>
        </TermField>
        <TermField label="description (optional)">
          <textarea v-model="createForm.description" rows="2" class="term-textarea" />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showCreateModal = false" label="cancel" />
        <TermButton
          variant="primary"
          :loading="createBusy"
          :disabled="createBusy || !createForm.client_name || !createForm.client_type"
          label="create"
          @click="handleCreate"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import {
  createServiceClient,
  listServiceClients,
  revokeServiceClient,
  rotateServiceClient,
} from '../api/serviceClients'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal } from '../components/cli'

const clients = ref([])
const busyId = ref(null)
const feedback = ref({ type: 'success', message: '' })
const issuedSecret = ref(null)
const showCreateModal = ref(false)
const createForm = ref({ client_name: '', client_type: 'router', description: '' })
const createBusy = ref(false)

function setFeedback(type, message) {
  feedback.value = { type, message }
}

function formatDate(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toISOString().replace('T', ' ').slice(0, 19) }
  catch { return iso }
}

async function fetchClients() {
  try { clients.value = await listServiceClients() }
  catch (e) { setFeedback('error', e.response?.data?.detail || 'failed to load') }
}

onMounted(fetchClients)

function clearIssuedSecret() {
  issuedSecret.value = null
}

async function copySecret() {
  if (!issuedSecret.value?.value) return
  try {
    await navigator.clipboard.writeText(issuedSecret.value.value)
    setFeedback('success', 'copied to clipboard')
  } catch {
    setFeedback('error', 'clipboard write failed — copy manually')
  }
}

function openCreateModal() {
  createForm.value = { client_name: '', client_type: 'router', description: '' }
  showCreateModal.value = true
}

async function handleCreate() {
  createBusy.value = true
  try {
    const data = await createServiceClient({
      client_name: createForm.value.client_name.trim(),
      client_type: createForm.value.client_type,
      description: createForm.value.description.trim() || null,
    })
    issuedSecret.value = {
      value: data.service_token,
      meta: {
        client_name: data.client.client_name,
        client_type: data.client.client_type,
        note: 'first issuance — paste into the client\'s state file or env',
      },
    }
    showCreateModal.value = false
    await fetchClients()
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || 'failed to create')
  } finally {
    createBusy.value = false
  }
}

async function handleRotate(c) {
  if (!confirm(`輪替「${c.client_name}」？舊 token 仍可用 24h。`)) return
  busyId.value = c.id
  try {
    const data = await rotateServiceClient(c.id)
    issuedSecret.value = {
      value: data.service_token,
      meta: {
        client_name: data.client.client_name,
        client_type: data.client.client_type,
        note: 'rotated — previous valid 24h',
      },
    }
    await fetchClients()
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || 'failed to rotate')
  } finally {
    busyId.value = null
  }
}

async function handleRevoke(c) {
  if (!confirm(`立即吊銷「${c.client_name}」？無 grace。`)) return
  busyId.value = c.id
  try {
    await revokeServiceClient(c.id)
    setFeedback('success', `revoked ${c.client_name}`)
    await fetchClients()
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || 'failed to revoke')
  } finally {
    busyId.value = null
  }
}
</script>

<style scoped>
.page { padding: 16px; }
.page-header { margin-bottom: 12px; }
.page-title { font-size: var(--t-lg); margin: 0 0 4px; font-weight: 500; }
.page-subtitle { font-size: var(--t-2xs); color: var(--c-fg-2); margin: 0; }
.page-subtitle code { background: var(--c-bg-1, #000); padding: 1px 4px; }

.data-table { width: 100%; border-collapse: collapse; font-size: var(--t-2xs); }
.data-table th {
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--c-divider);
  font-weight: 500; color: var(--c-fg-2); text-transform: uppercase;
  font-size: var(--t-3xs); letter-spacing: 0.04em;
}
.data-table td { padding: 6px 8px; border-bottom: 1px solid var(--c-divider); }
.data-table tr.is-revoked td { opacity: 0.5; }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-2); font-size: var(--t-3xs); }
.tnum { font-variant-numeric: tabular-nums; }

.row-actions { display: inline-flex; align-items: center; gap: 6px; }
.row-actions__sep { color: var(--c-fg-3); }
.term-action {
  background: none; border: none; color: var(--c-accent); cursor: pointer;
  font-size: var(--t-2xs); padding: 0; font-family: var(--font-mono);
}
.term-action:disabled { opacity: 0.5; cursor: wait; }
.term-action--danger { color: var(--c-danger, #c44); }

.form-grid { display: flex; flex-direction: column; gap: 12px; }

.secret-banner {
  border: 1px solid var(--c-accent);
  background: var(--c-bg-elev-1, rgba(255,255,255,0.04));
  padding: 10px;
}
.secret-banner__body { display: flex; gap: 8px; }
.secret-banner__token {
  flex: 1; font-family: var(--font-mono); font-size: var(--t-sm);
  background: var(--c-bg-1, #000); padding: 6px 8px;
  word-break: break-all; user-select: all;
}
.secret-banner__meta { margin: 8px 0 0; padding-left: 1.2em; color: var(--c-fg-2); font-size: var(--t-3xs); }
.secret-banner__meta code { background: var(--c-bg-1, #000); padding: 1px 4px; }
</style>
