<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">警報</h1>
        <p class="page-head__sub">系統偵測的異常 · 確認以靜音 · 解決以關閉 · 每 30 秒自動更新</p>
      </div>
      <div class="page-head__chips">
        <TermBadge variant="danger" dot>待處理 · {{ summary.open_count }}</TermBadge>
        <TermBadge variant="warn" dot>已確認 · {{ summary.acknowledged_count }}</TermBadge>
        <TermBadge dot>已解決 · {{ summary.resolved_count }}</TermBadge>
      </div>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox title="篩選" pad="sm">
      <div class="filters">
        <TermField label="狀態">
          <select v-model="filters.status" @change="fetchData" class="term-select">
            <option value="">全部</option>
            <option value="open">待處理</option>
            <option value="acknowledged">已確認</option>
            <option value="resolved">已解決</option>
          </select>
        </TermField>
        <TermField label="嚴重度">
          <select v-model="filters.severity" @change="fetchData" class="term-select">
            <option value="">全部</option>
            <option value="low">低</option>
            <option value="medium">中</option>
            <option value="high">高</option>
            <option value="critical">嚴重</option>
          </select>
        </TermField>
        <TermField label="分類">
          <input v-model="filters.category" @keyup.enter="fetchData" class="term-input" placeholder="例：health" />
        </TermField>
        <div class="filters__cta">
          <TermButton @click="fetchData" label="查詢" />
        </div>
      </div>
    </TermBox>

    <TermBox :title="`警報 · ${alerts.length}`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>警報</th>
            <th style="width: 22%">分類</th>
            <th style="width: 100px">狀態</th>
            <th style="width: 160px">最後出現</th>
            <th style="width: 22%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="alert in alerts" :key="alert.id">
            <td>
              <div class="cell-row">
                <TermDot :status="severityStatus(alert.severity)" :title="alert.severity" />
                <span class="cell-strong">{{ alert.title }}</span>
                <span class="severity-tag" :class="`is-${alert.severity}`">{{ alert.severity }}</span>
              </div>
              <div class="cell-meta cell-meta--wrap">{{ alert.message }}</div>
            </td>
            <td>
              <div class="cell-strong">{{ alert.category }}</div>
              <div class="cell-meta">{{ alert.source_type || '—' }} / {{ alert.source_id || '—' }}</div>
            </td>
            <td><TermBadge :variant="statusVariant(alert.status)" dot>{{ alert.status }}</TermBadge></td>
            <td class="cell-meta tnum">{{ formatDate(alert.last_seen_at) }}</td>
            <td>
              <div class="row-actions">
                <button v-if="alert.status === 'open'" class="term-action" @click="handleAck(alert)">確認</button>
                <span v-if="alert.status === 'open' && alert.status !== 'resolved'" class="row-actions__sep">·</span>
                <button v-if="alert.status !== 'resolved'" class="term-action" @click="handleResolve(alert)">解決</button>
              </div>
            </td>
          </tr>
          <tr v-if="alerts.length === 0">
            <td colspan="5"><TermEmpty message="無符合的警報 · 系統平靜" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { acknowledgeAlert, getAlertSummary, listAlerts, resolveAlert } from '../api/alerts'
import { extractError } from '../api/errors'
import { ALERT_POLL_INTERVAL_MS, createPoller } from '../utils/polling'
import { formatDate } from '../utils/formatDate'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermDot } from '../components/cli'
import { useDialog } from '../composables/useDialog'

const { toast } = useDialog()
const alerts = ref([])
const summary = ref({ open_count: 0, acknowledged_count: 0, resolved_count: 0, high_count: 0 })
const filters = ref({ status: '', severity: '', category: '' })
const pageError = ref('')

async function fetchData() {
  pageError.value = ''
  try {
    const [{ data: a }, { data: s }] = await Promise.all([
      listAlerts({
        status: filters.value.status || undefined,
        severity: filters.value.severity || undefined,
        category: filters.value.category || undefined,
      }),
      getAlertSummary(),
    ])
    alerts.value = a
    summary.value = s
  } catch (e) {
    pageError.value = extractError(e, '載入警報失敗')
  }
}

const poller = createPoller(fetchData, { intervalMs: ALERT_POLL_INTERVAL_MS })
onMounted(() => {
  fetchData()
  poller.start()
})
onUnmounted(() => {
  poller.stop()
})

async function handleAck(alert) {
  try { await acknowledgeAlert(alert.id); await fetchData() }
  catch (e) { toast(extractError(e, '確認失敗'), { tone: 'error' }) }
}
async function handleResolve(alert) {
  try { await resolveAlert(alert.id); await fetchData() }
  catch (e) { toast(extractError(e, '解決失敗'), { tone: 'error' }) }
}

function severityStatus(s) {
  return ({ low: 'info', medium: 'warn', high: 'warn', critical: 'danger' })[s] || 'idle'
}
function statusVariant(s) {
  return ({ open: 'danger', acknowledged: 'warn', resolved: '' })[s] || ''
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__chips { display: inline-flex; gap: 6px; }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.filters { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: var(--gap-3); align-items: end; }
.filters__cta { padding-bottom: 1px; }
@media (max-width: 800px) { .filters { grid-template-columns: 1fr 1fr; } }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-meta--wrap { margin-top: 4px; white-space: pre-wrap; }
.cell-row { display: inline-flex; align-items: center; gap: 6px; }

.severity-tag {
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 0 6px;
  border: var(--border-w) solid;
  border-radius: var(--r-soft);
  margin-left: 4px;
}
.severity-tag.is-low      { color: var(--c-info);   border-color: var(--c-info); }
.severity-tag.is-medium   { color: var(--c-warn);   border-color: var(--c-warn); }
.severity-tag.is-high     { color: var(--c-warn);   border-color: var(--c-warn); background: var(--c-warn-soft); }
.severity-tag.is-critical { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }
</style>
