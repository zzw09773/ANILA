<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">稽核查詢</h1>
        <p class="page-head__sub">
          推論稽核軌跡 · 哪個帳號、從哪個 IP、何時、問了什麼
        </p>
      </div>
      <div class="page-head__actions">
        <TermButton
          @click="exportCsv"
          label="匯出 CSV"
          :disabled="exporting || loading"
          :loading="exporting"
        />
        <span class="cell-meta">共 {{ total }} 筆</span>
      </div>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox title="篩選" pad="sm">
      <form class="filters" @submit.prevent="onSearch">
        <TermField label="帳號">
          <input
            v-model="filters.username"
            class="term-input"
            placeholder="使用者名稱"
            autocomplete="off"
          />
        </TermField>
        <TermField label="來源 IP">
          <input
            v-model="filters.ip"
            class="term-input"
            placeholder="例：10.53.100.15"
            autocomplete="off"
          />
        </TermField>
        <TermField label="動作">
          <select v-model="filters.action" class="term-select">
            <option
              v-for="opt in actionOptions"
              :key="opt.value || 'all'"
              :value="opt.value"
            >
              {{ opt.label }}
            </option>
          </select>
        </TermField>
        <TermField label="關鍵字" hint="比對內容子字串">
          <input
            v-model="filters.q"
            class="term-input"
            placeholder="內容關鍵字"
            autocomplete="off"
          />
        </TermField>
        <TermField label="起">
          <input v-model="filters.from" type="datetime-local" class="term-input" />
        </TermField>
        <TermField label="迄">
          <input v-model="filters.to" type="datetime-local" class="term-input" />
        </TermField>
        <TermField label="每頁筆數">
          <select v-model.number="filters.limit" class="term-select">
            <option :value="25">25</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
            <option :value="500">500</option>
          </select>
        </TermField>
        <div class="filters__cta">
          <TermButton type="submit" label="查詢" :loading="loading" />
        </div>
      </form>
    </TermBox>

    <TermBox title="結果" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 14%">時間</th>
            <th style="width: 10%">帳號</th>
            <th style="width: 12%">IP</th>
            <th style="width: 10%">動作</th>
            <th style="width: 12%">資源</th>
            <th style="width: 80px">狀態</th>
            <th>內容</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td class="cell-meta tnum">{{ formatDate(row.created_at) }}</td>
            <td class="cell-strong">{{ row.actor_username || '—' }}</td>
            <td class="cell-meta tnum">{{ row.ip_address || '—' }}</td>
            <td>{{ actionLabel(row.action) }}</td>
            <td>
              <div>{{ row.resource_type || '—' }}</div>
              <div class="cell-meta">{{ row.resource_id || '—' }}</div>
            </td>
            <td>
              <TermBadge :variant="statusVariant(row.status)" dot>
                {{ statusLabel(row.status) }}
              </TermBadge>
            </td>
            <td>
              <button
                type="button"
                class="detail-toggle"
                :aria-expanded="expandedId === row.id"
                @click="toggleExpand(row.id)"
              >
                <span v-if="expandedId !== row.id" class="detail-preview">
                  {{ truncateDetail(row.detail) }}
                </span>
                <span v-else class="detail-full">
                  <div class="cell-detail">{{ row.detail || '—' }}</div>
                  <pre v-if="formatMetadata(row.metadata_json)" class="meta-block">{{ formatMetadata(row.metadata_json) }}</pre>
                </span>
              </button>
            </td>
          </tr>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="7"><TermEmpty message="無資料" /></td>
          </tr>
          <tr v-if="loading && rows.length === 0">
            <td colspan="7" class="cell-meta loading-row">查詢中…</td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <div class="pager" v-if="total > 0">
      <span class="cell-meta">
        第 {{ pageFrom }}–{{ pageTo }} 筆 · 共 {{ total }} 筆
      </span>
      <div class="pager__btns">
        <TermButton
          size="xs"
          label="上一頁"
          :disabled="loading || filters.offset <= 0"
          @click="goPrev"
        />
        <TermButton
          size="xs"
          label="下一頁"
          :disabled="loading || !hasNext"
          @click="goNext"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  exportInferenceAuditCsv,
  listInferenceAudit,
} from '../api/inferenceAudit'
import {
  INFERENCE_ACTION_FILTER_OPTIONS,
  actionLabel,
  formatMetadata,
  statusLabel,
  statusVariant,
  truncateDetail,
} from '../utils/inferenceAudit'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty } from '../components/cli'

const actionOptions = INFERENCE_ACTION_FILTER_OPTIONS

const rows = ref([])
const total = ref(0)
const pageError = ref('')
const loading = ref(false)
const exporting = ref(false)
const expandedId = ref(null)

const filters = ref({
  username: '',
  ip: '',
  action: '',
  q: '',
  from: '',
  to: '',
  limit: 50,
  offset: 0,
})

const hasNext = computed(() => filters.value.offset + filters.value.limit < total.value)
const pageFrom = computed(() => (total.value === 0 ? 0 : filters.value.offset + 1))
const pageTo = computed(() => Math.min(filters.value.offset + filters.value.limit, total.value))

function formatDate(v) {
  if (!v) return '—'
  return new Date(v).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei' })
}

function toggleExpand(id) {
  expandedId.value = expandedId.value === id ? null : id
}

function apiErrorMessage(e, fallback) {
  const detail = e.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((d) => d.msg || JSON.stringify(d)).join('; ') || fallback
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return fallback
}

async function fetchRows() {
  pageError.value = ''
  loading.value = true
  try {
    const { data } = await listInferenceAudit(filters.value)
    rows.value = data.rows || []
    total.value = data.total ?? 0
  } catch (e) {
    pageError.value = apiErrorMessage(e, '載入稽核查詢失敗')
  } finally {
    loading.value = false
  }
}

function onSearch() {
  filters.value.offset = 0
  expandedId.value = null
  return fetchRows()
}

function goPrev() {
  filters.value.offset = Math.max(0, filters.value.offset - filters.value.limit)
  expandedId.value = null
  return fetchRows()
}

function goNext() {
  if (!hasNext.value) return
  filters.value.offset += filters.value.limit
  expandedId.value = null
  return fetchRows()
}

async function exportCsv() {
  pageError.value = ''
  exporting.value = true
  try {
    const { data } = await exportInferenceAuditCsv(filters.value)
    const url = URL.createObjectURL(new Blob([data], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'inference-audit.csv'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    pageError.value = apiErrorMessage(e, '匯出 CSV 失敗')
  } finally {
    exporting.value = false
  }
}

onMounted(fetchRows)
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__actions { display: flex; align-items: center; gap: var(--gap-3); flex-wrap: wrap; }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.filters {
  display: grid;
  grid-template-columns: 1fr 1fr 0.9fr 1.2fr 1fr 1fr 0.7fr auto;
  gap: var(--gap-3);
  align-items: end;
}
.filters__cta { padding-bottom: 1px; }
@media (max-width: 1200px) { .filters { grid-template-columns: 1fr 1fr 1fr 1fr; } }
@media (max-width: 700px)  { .filters { grid-template-columns: 1fr 1fr; } }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-detail { color: var(--c-fg-2); font-size: var(--t-xs); white-space: pre-wrap; word-break: break-word; }
.tnum { font-variant-numeric: tabular-nums; }
.loading-row { padding: var(--gap-4); text-align: center; }

.detail-toggle {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 0;
  color: inherit;
  cursor: pointer;
  font: inherit;
}
.detail-toggle:hover .detail-preview { color: var(--c-accent); }
.detail-preview {
  display: block;
  color: var(--c-fg-2);
  font-size: var(--t-xs);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 42ch;
}
.meta-block {
  margin: var(--gap-2) 0 0;
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: var(--gap-2);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.pager {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--gap-3);
  flex-wrap: wrap;
}
.pager__btns { display: flex; gap: var(--gap-2); }
</style>
