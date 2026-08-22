<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">稽核紀錄</h1>
        <p class="page-head__sub">治理軌跡 · 管理員寫入操作 · 可依時間篩選並翻頁</p>
      </div>
      <span class="cell-meta">{{ total }} 筆中的第 {{ rangeStart }}–{{ rangeEnd }} 筆</span>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox title="篩選" pad="sm">
      <div class="filters">
        <TermField label="操作者">
          <input v-model="filters.actor_username" class="term-input" placeholder="使用者名稱" />
        </TermField>
        <TermField label="動作">
          <input v-model="filters.action" class="term-input" placeholder="例：create" />
        </TermField>
        <!-- Sprint 8 X / Phase H quick-filter — service-token cutover monitoring. -->
        <TermField label="快速 · 舊版服務憑證事件" hint="歷史稽核篩選（agent 已改派工 JWT）">
          <select v-model="filters.action" class="term-select" @change="fetchLogs">
            <option value="">— 選擇以篩選 —</option>
            <option value="service_token_legacy_env_used">legacy env-var fallback 命中</option>
            <option value="service_token_bootstrap_issued">bootstrap 已核發（管理員）</option>
            <option value="service_token_bootstrap_consumed">bootstrap 已消耗</option>
            <option value="service_token_issued">憑證已核發</option>
            <option value="service_token_rotated">憑證已輪替</option>
            <option value="service_token_revoked">憑證已撤銷</option>
            <option value="service_token_verified">驗證成功</option>
          </select>
        </TermField>
        <TermField label="資源">
          <input v-model="filters.resource_type" class="term-input" placeholder="例：user" />
        </TermField>
        <TermField label="狀態">
          <select v-model="filters.status" class="term-select">
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failure">失敗</option>
          </select>
        </TermField>
        <TermField label="開始時間">
          <input v-model="filters.since" type="datetime-local" class="term-input" />
        </TermField>
        <TermField label="結束時間">
          <input v-model="filters.until" type="datetime-local" class="term-input" />
        </TermField>
        <TermField label="每頁筆數">
          <select v-model.number="filters.limit" class="term-select">
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
            <option :value="500">500</option>
          </select>
        </TermField>
        <div class="filters__cta">
          <TermButton @click="searchFromStart" label="查詢" />
        </div>
      </div>
    </TermBox>

    <TermBox title="紀錄" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 14%">時間</th>
            <th style="width: 12%">操作者</th>
            <th style="width: 10%">動作</th>
            <th style="width: 14%">資源</th>
            <th style="width: 80px">結果</th>
            <th>明細</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="log in logs" :key="log.id">
            <td class="cell-meta tnum">{{ formatDate(log.created_at) }}</td>
            <td>
              <div class="cell-strong">{{ log.actor_username || '系統' }}</div>
              <div class="cell-meta">{{ log.ip_address || '—' }}</div>
            </td>
            <td><code class="action-code">{{ log.action }}</code></td>
            <td>
              <div>{{ log.resource_type }}</div>
              <div class="cell-meta">{{ log.resource_id || '—' }}</div>
            </td>
            <td><TermBadge :variant="log.status === 'success' ? 'ok' : 'danger'" dot>{{ log.status }}</TermBadge></td>
            <td>
              <div class="cell-detail">{{ log.detail || '—' }}</div>
              <pre v-if="log.metadata" class="meta-block">{{ JSON.stringify(log.metadata, null, 2) }}</pre>
            </td>
          </tr>
          <tr v-if="logs.length === 0">
            <td colspan="6"><TermEmpty message="無符合的稽核紀錄" /></td>
          </tr>
        </tbody>
      </table>
      <div v-if="total > filters.limit" class="pager">
        <TermButton size="xs" :disabled="filters.offset <= 0" label="上一頁" @click="prevPage" />
        <span class="cell-meta">第 {{ pageNumber }} 頁</span>
        <TermButton size="xs" :disabled="!hasNext" label="下一頁" @click="nextPage" />
      </div>
    </TermBox>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { listAuditLogs } from '../api/auditLogs'
import { formatDate } from '../utils/formatDate'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty } from '../components/cli'
import { extractError } from '../api/errors'

const logs = ref([])
const total = ref(0)
const pageError = ref('')
const filters = ref({
  actor_username: '',
  action: '',
  resource_type: '',
  status: '',
  since: '',
  until: '',
  limit: 100,
  offset: 0,
})

const rangeStart = computed(() => (total.value === 0 ? 0 : filters.value.offset + 1))
const rangeEnd = computed(() => Math.min(filters.value.offset + logs.value.length, total.value))
const pageNumber = computed(() => Math.floor(filters.value.offset / filters.value.limit) + 1)
const hasNext = computed(() => filters.value.offset + logs.value.length < total.value)

function taipeiIso(value) {
  if (!value) return undefined
  const withSeconds = value.length === 16 ? `${value}:00` : value
  return new Date(`${withSeconds}+08:00`).toISOString()
}

async function fetchLogs() {
  pageError.value = ''
  try {
    const { data, headers } = await listAuditLogs({
      actor_username: filters.value.actor_username || undefined,
      action: filters.value.action || undefined,
      resource_type: filters.value.resource_type || undefined,
      status: filters.value.status || undefined,
      since: taipeiIso(filters.value.since),
      until: taipeiIso(filters.value.until),
      limit: filters.value.limit,
      offset: filters.value.offset,
    })
    logs.value = data
    const headerTotal = headers?.['x-total-count'] || headers?.['X-Total-Count']
    total.value = headerTotal != null ? Number(headerTotal) : data.length
  } catch (e) {
    pageError.value = extractError(e, '載入稽核紀錄失敗')
  }
}

function searchFromStart() {
  filters.value.offset = 0
  return fetchLogs()
}
function nextPage() {
  filters.value.offset += filters.value.limit
  return fetchLogs()
}
function prevPage() {
  filters.value.offset = Math.max(0, filters.value.offset - filters.value.limit)
  return fetchLogs()
}

onMounted(fetchLogs)
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.filters { display: grid; grid-template-columns: 1fr 1fr 1fr 0.8fr 0.6fr 1fr 1fr 0.6fr auto; gap: var(--gap-3); align-items: end; }
.filters__cta { padding-bottom: 1px; }
@media (max-width: 1400px) { .filters { grid-template-columns: 1fr 1fr 1fr 1fr; } }
@media (max-width: 700px)  { .filters { grid-template-columns: 1fr 1fr; } }
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--gap-3);
  padding: var(--gap-3);
  border-top: var(--border-w) solid var(--c-border);
}

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-detail { color: var(--c-fg-2); font-size: var(--t-xs); white-space: pre-wrap; }
.action-code {
  font-family: var(--font-mono);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
  font-size: var(--t-2xs);
  color: var(--c-accent);
}
.meta-block {
  margin: var(--gap-2) 0 0;
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: var(--gap-2);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  max-height: 160px;
  overflow: auto;
}
</style>
