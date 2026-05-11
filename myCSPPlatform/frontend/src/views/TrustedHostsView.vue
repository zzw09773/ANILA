<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">trusted hosts</h1>
        <p class="page-subtitle">
          SSRF guard allow-list — 列上的 hostname 在 model / agent / credential
          註冊時可繞過 single-label / internal-zone 阻擋。Owner-only,
          每次新增 / 移除都會寫 audit log。
        </p>
      </div>
    </header>

    <TermBox v-if="feedback.message" :tone="feedback.type" dismissible @dismiss="feedback.message = ''">
      {{ feedback.message }}
    </TermBox>

    <div class="row-actions" style="margin: 12px 0;">
      <TermButton
        v-if="authStore.isOwner"
        variant="primary"
        @click="openCreateModal"
        label="+ add host"
      />
      <span v-if="authStore.isOwner" class="row-actions__sep">·</span>
      <button class="term-action" @click="fetchHosts">refresh</button>
    </div>

    <TermBox>
      <table class="data-table">
        <thead>
          <tr>
            <th>id</th>
            <th>host</th>
            <th>note</th>
            <th>added by</th>
            <th>added at</th>
            <th v-if="authStore.isOwner">actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!hosts.length">
            <td :colspan="authStore.isOwner ? 6 : 5">
              <TermEmpty message="no trusted hosts yet — admin will add them via this page" />
            </td>
          </tr>
          <tr v-for="h in hosts" :key="h.id">
            <td class="tnum">{{ h.id }}</td>
            <td>
              <code class="host-cell">{{ h.host }}</code>
            </td>
            <td class="cell-meta">{{ h.note || '—' }}</td>
            <td class="cell-meta">
              <span v-if="h.created_by_username">{{ h.created_by_username }}</span>
              <span v-else>system / env backfill</span>
            </td>
            <td class="cell-meta tnum">{{ formatDate(h.created_at) }}</td>
            <td v-if="authStore.isOwner">
              <button
                class="term-action term-action--danger"
                :disabled="busyId === h.id"
                @click="handleDelete(h)"
              >
                {{ busyId === h.id ? '…' : 'remove' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" title="add trusted host" width="520px" @close="showModal = false">
      <div class="form-grid">
        <TermField
          label="host"
          hint="bare hostname — no scheme / port / path (e.g. `gemma4`, `inference.internal`)"
        >
          <input
            v-model="form.host"
            class="term-input"
            placeholder="gemma4"
            @keyup.enter="handleSubmit"
          />
        </TermField>
        <TermField label="note" hint="optional, free text — 寫清為什麼信任這個 host">
          <textarea
            v-model="form.note"
            rows="3"
            class="term-textarea"
            placeholder="e.g. anila-models-net 內 vLLM, GPU 3"
          />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="cancel" />
        <TermButton
          variant="primary"
          :disabled="!form.host.trim() || submitting"
          :label="submitting ? 'adding…' : 'add'"
          @click="handleSubmit"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  listTrustedHosts,
  createTrustedHost,
  deleteTrustedHost,
} from '../api/trustedHosts'
import { useAuthStore } from '../stores/auth'
import {
  TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal,
} from '../components/cli'

const authStore = useAuthStore()

const hosts = ref([])
const showModal = ref(false)
const submitting = ref(false)
const busyId = ref(null)
const form = reactive({ host: '', note: '' })
const feedback = reactive({ message: '', type: 'info' })

function setFeedback(type, message) {
  feedback.type = type
  feedback.message = message
}

function formatDate(s) {
  if (!s) return '—'
  try {
    return new Date(s).toLocaleString()
  } catch {
    return s
  }
}

async function fetchHosts() {
  try {
    const { data } = await listTrustedHosts()
    hosts.value = data
  } catch (e) {
    setFeedback('danger', e.response?.data?.detail || 'failed to load trusted hosts')
  }
}

function openCreateModal() {
  form.host = ''
  form.note = ''
  showModal.value = true
}

async function handleSubmit() {
  if (!form.host.trim()) return
  submitting.value = true
  try {
    await createTrustedHost({
      host: form.host.trim(),
      note: form.note.trim() || null,
    })
    showModal.value = false
    setFeedback('ok', `added "${form.host.trim()}"`)
    await fetchHosts()
  } catch (e) {
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : (detail?.message || 'add failed')
    setFeedback('danger', msg)
  } finally {
    submitting.value = false
  }
}

async function handleDelete(host) {
  if (!confirm(`移除受信任 host「${host.host}」?`)) return
  busyId.value = host.id
  try {
    await deleteTrustedHost(host.id)
    setFeedback('ok', `removed "${host.host}"`)
    await fetchHosts()
  } catch (e) {
    setFeedback('danger', e.response?.data?.detail || 'remove failed')
  } finally {
    busyId.value = null
  }
}

onMounted(fetchHosts)
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; }
.page-title { font-size: var(--t-xl); font-weight: 500; color: var(--c-fg-1); margin: 0 0 4px; }
.page-subtitle { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; max-width: 60ch; }
.page-subtitle code { font-family: var(--font-mono); font-size: var(--t-2xs); color: var(--c-accent); }

.row-actions { display: flex; align-items: center; gap: var(--gap-2); }
.row-actions__sep { color: var(--c-fg-mute); }

.data-table { width: 100%; border-collapse: collapse; }
.data-table th, .data-table td { padding: 8px 12px; text-align: left; }
.data-table th { color: var(--c-fg-3); font-weight: 400; font-size: var(--t-2xs); text-transform: uppercase; letter-spacing: 0.05em; }
.data-table tr:not(:last-child) td { border-bottom: var(--border-w) solid var(--c-border); }

.host-cell {
  font-family: var(--font-mono);
  font-size: var(--t-xs);
  color: var(--c-fg-1);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 2px 6px;
}

.tnum { font-variant-numeric: tabular-nums; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
</style>
