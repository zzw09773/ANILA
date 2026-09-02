<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">服務客戶端</h1>
        <p class="page-subtitle">
          平台內部服務（對話路由、文件匯入工作者、管理工具）彼此驗證用的身分。每一列是一組長效憑證，與 agent 派工權杖無關。
        </p>
      </div>
    </header>

    <TermBox v-if="feedback.message" :tone="feedback.type" dismissible @dismiss="feedback.message = ''">
      {{ feedback.message }}
    </TermBox>

    <div class="row-actions" style="margin: 12px 0;">
      <TermButton variant="primary" @click="openCreateModal" label="+ 建立客戶端" />
      <span class="row-actions__sep">·</span>
      <button class="term-action" @click="fetchClients">重新整理</button>
    </div>

    <TermBox>
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>名稱</th>
            <th>類型</th>
            <th>狀態</th>
            <th>核發時間</th>
            <th>輪替時間</th>
            <th>寬限期</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!clients.length">
            <td colspan="8">
              <TermEmpty message="尚無服務客戶端" />
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
              <TermBadge v-if="c.is_legacy" variant="warn" style="margin-left: 6px;">舊版</TermBadge>
            </td>
            <td>
              <TermBadge :variant="c.is_active ? '' : 'danger'" dot>
                {{ c.is_active ? '使用中' : '已吊銷' }}
              </TermBadge>
            </td>
            <td class="cell-meta tnum">{{ formatDate(c.issued_at) }}</td>
            <td class="cell-meta tnum">{{ c.rotated_at ? formatDate(c.rotated_at) : '—' }}</td>
            <td class="cell-meta tnum">
              <span v-if="c.has_previous_token">至 {{ formatDate(c.previous_expires_at) }}</span>
              <span v-else>—</span>
            </td>
            <td>
              <div class="row-actions" v-if="c.is_active">
                <button class="term-action" :disabled="busyId === c.id" @click="handleRotate(c)">
                  {{ busyId === c.id ? '…' : '輪替' }}
                </button>
                <span class="row-actions__sep">·</span>
                <button class="term-action term-action--danger" :disabled="busyId === c.id" @click="handleRevoke(c)">
                  {{ busyId === c.id ? '…' : '吊銷' }}
                </button>
              </div>
              <span v-else class="cell-meta">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Plaintext display -->
    <TermModal :visible="!!issuedSecret" title="立即複製此 token" width="540px" @close="clearIssuedSecret">
      <p class="cell-meta">
        plaintext 只會出現一次。複製後妥善保存（password manager / vault）。
      </p>
      <div class="secret-banner">
        <div class="secret-banner__body">
          <code class="secret-banner__token">{{ issuedSecret?.value }}</code>
        </div>
        <ul v-if="issuedSecret?.meta" class="secret-banner__meta">
          <li v-if="issuedSecret.meta.client_name">客戶端：<code>{{ issuedSecret.meta.client_name }}</code></li>
          <li v-if="issuedSecret.meta.client_type">類型：{{ issuedSecret.meta.client_type }}</li>
          <li v-if="issuedSecret.meta.note">{{ issuedSecret.meta.note }}</li>
        </ul>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="copySecret" label="複製" />
        <TermButton variant="primary" @click="clearIssuedSecret" label="完成" />
      </template>
    </TermModal>

    <!-- Create modal -->
    <TermModal :visible="showCreateModal" title="建立 · 服務客戶端" width="520px" @close="showCreateModal = false">
      <div class="form-grid">
        <TermField label="名稱" hint="不可變更的識別碼（例：router-primary、ingestion-worker）">
          <input v-model="createForm.client_name" class="term-input" placeholder="router-primary" />
        </TermField>
        <TermField label="類型">
          <select v-model="createForm.client_type" class="term-select">
            <option value="router">router</option>
            <option value="worker">worker</option>
            <option value="admin_tool">admin_tool</option>
          </select>
        </TermField>
        <TermField label="描述（選填）">
          <textarea v-model="createForm.description" rows="2" class="term-textarea" />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showCreateModal = false" label="取消" />
        <TermButton
          variant="primary"
          :loading="createBusy"
          :disabled="createBusy || !createForm.client_name || !createForm.client_type"
          label="建立"
          @click="handleCreate"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { extractError } from '../api/errors'
import {
  createServiceClient,
  listServiceClients,
  revokeServiceClient,
  rotateServiceClient,
} from '../api/serviceClients'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import { formatDate } from '../utils/formatDate'

const { confirm } = useDialog()
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

async function fetchClients() {
  try { clients.value = await listServiceClients() }
  catch (e) { setFeedback('error', extractError(e, '載入失敗')) }
}

onMounted(fetchClients)

function clearIssuedSecret() {
  issuedSecret.value = null
}

async function copySecret() {
  if (!issuedSecret.value?.value) return
  try {
    await navigator.clipboard.writeText(issuedSecret.value.value)
    setFeedback('success', '已複製到剪貼簿')
  } catch {
    setFeedback('error', '寫入剪貼簿失敗 — 請手動複製')
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
        note: '首次核發 — 貼進客戶端的 state 檔或 env',
      },
    }
    showCreateModal.value = false
    await fetchClients()
  } catch (e) {
    setFeedback('error', extractError(e, '建立失敗'))
  } finally {
    createBusy.value = false
  }
}

async function handleRotate(c) {
  if (!(await confirm({ message: `輪替「${c.client_name}」？舊 token 仍可用 24h。`, confirmText: '輪替' }))) return
  busyId.value = c.id
  try {
    const data = await rotateServiceClient(c.id)
    issuedSecret.value = {
      value: data.service_token,
      meta: {
        client_name: data.client.client_name,
        client_type: data.client.client_type,
        note: '已輪替 — 前一組 24 小時內有效',
      },
    }
    await fetchClients()
  } catch (e) {
    setFeedback('error', extractError(e, '輪替失敗'))
  } finally {
    busyId.value = null
  }
}

async function handleRevoke(c) {
  if (!(await confirm({ message: `立即吊銷「${c.client_name}」？無 grace。`, confirmText: '吊銷', danger: true }))) return
  busyId.value = c.id
  try {
    await revokeServiceClient(c.id)
    setFeedback('success', `已吊銷 ${c.client_name}`)
    await fetchClients()
  } catch (e) {
    setFeedback('error', extractError(e, '吊銷失敗'))
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
.page-subtitle code { background: var(--c-surface-2); padding: 1px 4px; }

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
  background: var(--c-surface-2);
  padding: 10px;
}
.secret-banner__body { display: flex; gap: 8px; }
.secret-banner__token {
  flex: 1; font-family: var(--font-mono); font-size: var(--t-sm);
  background: var(--c-surface-1); color: var(--c-fg-1);
  border: var(--border-w) solid var(--c-border-strong);
  padding: 6px 8px;
  word-break: break-all; user-select: all;
}
.secret-banner__meta { margin: 8px 0 0; padding-left: 1.2em; color: var(--c-fg-2); font-size: var(--t-3xs); }
.secret-banner__meta code {
  background: var(--c-surface-1); color: var(--c-fg-1);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 4px;
}
</style>
