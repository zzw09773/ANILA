<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">admin · audit</p>
        <h1 class="page-head__title">audit-log</h1>
        <p class="page-head__sub">last {{ filters.limit }} entries · governance trail · admin write ops</p>
      </div>
      <span class="cell-meta">{{ logs.length }} record(s)</span>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox title="filter" pad="sm">
      <div class="filters">
        <TermField label="actor">
          <input v-model="filters.actor_username" class="term-input" placeholder="username" />
        </TermField>
        <TermField label="action">
          <input v-model="filters.action" class="term-input" placeholder="e.g. create" />
        </TermField>
        <!-- Sprint 8 X / Phase H quick-filter — service-token cutover monitoring. -->
        <TermField label="quick · service token" hint="audit cutover progress">
          <select v-model="filters.action" class="term-select" @change="fetchLogs">
            <option value="">— pick to filter —</option>
            <option value="service_token_legacy_env_used">legacy env-var fallback hits</option>
            <option value="service_token_bootstrap_issued">bootstrap issued (admin)</option>
            <option value="service_token_bootstrap_consumed">bootstrap consumed</option>
            <option value="service_token_issued">credential issued</option>
            <option value="service_token_rotated">credential rotated</option>
            <option value="service_token_revoked">credential revoked</option>
            <option value="service_token_verified">verify ok</option>
          </select>
        </TermField>
        <TermField label="resource">
          <input v-model="filters.resource_type" class="term-input" placeholder="e.g. user" />
        </TermField>
        <TermField label="status">
          <select v-model="filters.status" class="term-select">
            <option value="">all</option>
            <option value="success">success</option>
            <option value="failure">failure</option>
          </select>
        </TermField>
        <TermField label="limit">
          <select v-model.number="filters.limit" class="term-select">
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
            <option :value="500">500</option>
          </select>
        </TermField>
        <div class="filters__cta">
          <TermButton @click="fetchLogs" label="query" />
        </div>
      </div>
    </TermBox>

    <TermBox title="entries" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 14%">timestamp</th>
            <th style="width: 12%">actor</th>
            <th style="width: 10%">action</th>
            <th style="width: 14%">resource</th>
            <th style="width: 80px">result</th>
            <th>detail</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="log in logs" :key="log.id">
            <td class="cell-meta tnum">{{ formatDate(log.created_at) }}</td>
            <td>
              <div class="cell-strong">{{ log.actor_username || 'system' }}</div>
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
            <td colspan="6"><TermEmpty message="no audit entries match" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listAuditLogs } from '../api/auditLogs'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty } from '../components/cli'

const logs = ref([])
const pageError = ref('')
const filters = ref({ actor_username: '', action: '', resource_type: '', status: '', limit: 100 })

async function fetchLogs() {
  pageError.value = ''
  try {
    const { data } = await listAuditLogs({
      actor_username: filters.value.actor_username || undefined,
      action: filters.value.action || undefined,
      resource_type: filters.value.resource_type || undefined,
      status: filters.value.status || undefined,
      limit: filters.value.limit,
    })
    logs.value = data
  } catch (e) {
    pageError.value = e.response?.data?.detail || 'failed to load audit log'
  }
}
onMounted(fetchLogs)
function formatDate(v) { return new Date(v).toLocaleString('en-GB') }
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.filters { display: grid; grid-template-columns: 1fr 1fr 1fr 0.8fr 0.6fr auto; gap: var(--gap-3); align-items: end; }
.filters__cta { padding-bottom: 1px; }
@media (max-width: 1100px) { .filters { grid-template-columns: 1fr 1fr 1fr; } }
@media (max-width: 700px)  { .filters { grid-template-columns: 1fr 1fr; } }

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
