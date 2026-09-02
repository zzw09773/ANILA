<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">信任主機</h1>
        <p class="page-subtitle">
          允許平台對外連線的主機清單。列在這裡的主機名稱，在登記模型、Agent、憑證時不會被內網位址防護擋下。僅擁有者可以修改，每次新增或移除都會寫入稽核紀錄。
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
        label="+ 新增主機"
      />
      <span v-if="authStore.isOwner" class="row-actions__sep">·</span>
      <button class="term-action" @click="fetchHosts">重新整理</button>
    </div>

    <TermBox>
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>主機</th>
            <th>備註</th>
            <th>新增者</th>
            <th>新增時間</th>
            <th v-if="authStore.isOwner">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!hosts.length">
            <td :colspan="authStore.isOwner ? 6 : 5">
              <TermEmpty message="尚無信任主機 — 管理員可在此頁新增" />
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
              <span v-else>系統 / env 回填</span>
            </td>
            <td class="cell-meta tnum">{{ formatDate(h.created_at) }}</td>
            <td v-if="authStore.isOwner">
              <button
                class="term-action term-action--danger"
                :disabled="busyId === h.id"
                @click="handleDelete(h)"
              >
                {{ busyId === h.id ? '…' : '移除' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" title="新增信任主機" width="520px" @close="showModal = false">
      <div class="form-grid">
        <TermField
          label="主機"
          hint="純 hostname — 不含 scheme / port / path（例：`gemma4`、`inference.internal`）"
        >
          <input
            v-model="form.host"
            class="term-input"
            placeholder="gemma4"
            @keyup.enter="handleSubmit"
          />
        </TermField>
        <TermField label="備註" hint="選填，自由文字 — 寫清為什麼信任這個 host">
          <textarea
            v-model="form.note"
            rows="3"
            class="term-textarea"
            placeholder="e.g. anila-models-net 內 vLLM, GPU 3"
          />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="!form.host.trim() || submitting"
          :label="submitting ? '新增中…' : '新增'"
          @click="handleSubmit"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { extractError } from '../api/errors'
import {
  listTrustedHosts,
  createTrustedHost,
  deleteTrustedHost,
} from '../api/trustedHosts'
import { useAuthStore } from '../stores/auth'
import {
  TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal,
} from '../components/cli'
import { useDialog } from '../composables/useDialog'
import { formatDate } from '../utils/formatDate'

const { confirm } = useDialog()
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

async function fetchHosts() {
  try {
    const { data } = await listTrustedHosts()
    hosts.value = data
  } catch (e) {
    setFeedback('danger', extractError(e, '載入信任主機失敗'))
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
    setFeedback('ok', `已新增「${form.host.trim()}」`)
    await fetchHosts()
  } catch (e) {
    setFeedback('danger', extractError(e, '新增失敗'))
  } finally {
    submitting.value = false
  }
}

async function handleDelete(host) {
  if (!(await confirm({ message: `移除受信任 host「${host.host}」?`, danger: true }))) return
  busyId.value = host.id
  try {
    await deleteTrustedHost(host.id)
    setFeedback('ok', `已移除「${host.host}」`)
    await fetchHosts()
  } catch (e) {
    setFeedback('danger', extractError(e, '移除失敗'))
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
