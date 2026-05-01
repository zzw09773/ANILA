<template>
  <div class="page">
    <header class="page__head">
      <div>
        <p class="page__eyebrow">control plane &nbsp;·&nbsp; overview</p>
        <h1 class="page__title">dashboard<span class="term-caret" aria-hidden="true" /></h1>
      </div>
      <div class="page__head-meta">
        <span class="term-label">window</span>
        <span class="page__head-val">last 24h</span>
        <span class="term-label">refreshed</span>
        <span class="page__head-val tnum">{{ refreshedLabel }}</span>
        <TermButton size="xs" variant="ghost" :loading="loading" @click="refresh" label="refresh" />
      </div>
    </header>

    <!-- KPI strip ------------------------------------------------------- -->
    <section class="kpi-grid">
      <TermStat label="24h · requests" :value="summary?.total_requests || 0" tone="accent" />
      <TermStat label="24h · tokens"   :value="summary?.total_tokens || 0" />
      <TermStat label="active · models" :value="summary?.active_models || 0" hint="health-checked" />
      <TermStat label="active · keys"  :value="summary?.active_api_keys || 0" />
    </section>

    <!-- Chart + side meta ---------------------------------------------- -->
    <section class="dash-grid">
      <TermBox title="usage · throughput · 24h" hint="per model · ts in local tz" pad="md">
        <UsageLineChart :chart-data="chartData" :height="280" />
      </TermBox>

      <TermBox title="quick · ops" pad="md">
        <ul class="ops">
          <li class="ops__row">
            <span class="ops__k">role</span>
            <span class="ops__v">{{ authStore.user?.role || 'user' }}</span>
          </li>
          <li class="ops__row">
            <span class="ops__k">scope</span>
            <span class="ops__v">{{ scopeLabel }}</span>
          </li>
          <li class="ops__row">
            <span class="ops__k">data plane</span>
            <span class="ops__v ops__v--accent">/v1/* &nbsp;·&nbsp; /v2/embeddings</span>
          </li>
          <li class="ops__row">
            <span class="ops__k">control plane</span>
            <span class="ops__v ops__v--accent">/api/*</span>
          </li>
        </ul>
        <hr class="ops__rule" />
        <div class="ops__quick">
          <router-link to="/api-keys" class="ops__link">→ provision api-key</router-link>
          <router-link to="/models" class="ops__link">→ inspect models</router-link>
          <router-link to="/usage" class="ops__link">→ usage analytics</router-link>
          <router-link v-if="authStore.isDeveloper" to="/developer/agents" class="ops__link">→ register agent</router-link>
          <router-link v-if="authStore.isAdmin" to="/audit-logs" class="ops__link">→ audit log</router-link>
        </div>
      </TermBox>
    </section>

    <!-- Sprint 8 X / Phase H — admin observability strip ---------------- -->
    <section v-if="authStore.isAdmin" class="dash-grid">
      <!-- legacy-token cutover progress widget -->
      <TermBox
        title="cutover · legacy service-token"
        :hint="legacyTokenHint"
        :tone="legacyTokenStats?.count_24h ? 'warn' : ''"
        pad="md"
      >
        <div v-if="legacyTokenStats" class="cutover">
          <div class="cutover__stats">
            <TermStat label="24h · hits" :value="legacyTokenStats.count_24h" :tone="legacyTokenStats.count_24h ? 'warn' : 'ok'" />
            <TermStat label="7d · hits"  :value="legacyTokenStats.count_7d" />
            <TermStat label="30d · hits" :value="legacyTokenStats.count_30d" />
          </div>
          <p class="cutover__last">
            <span class="cutover__k">last seen</span>
            <span class="cutover__v tnum">{{ legacyTokenStats.last_seen_at ? formatTs(legacyTokenStats.last_seen_at) : 'never (cutover clean)' }}</span>
          </p>
          <p v-if="legacyTokenStats.count_30d === 0" class="cutover__hint cutover__hint--ok">
            ✓ 30 天內無 fallback 命中 — 可進入 cutover stage 4（從 .env 拿掉 CSP_SERVICE_TOKEN）
          </p>
          <p v-else class="cutover__hint cutover__hint--warn">
            仍有 agent / Router 走 legacy env-var fallback — 請至 audit log 查 ip_address 找出未 cutover 主機。
          </p>
        </div>
        <TermEmpty v-else message="loading…" />
      </TermBox>

      <!-- top-5 agents over the last 30 days -->
      <TermBox title="top · agents · 30d" hint="caller-attributed token spend" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr>
              <th>agent</th>
              <th class="num" style="width: 110px">tokens</th>
              <th class="num" style="width: 110px">requests</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in topAgents" :key="a.agent_id">
              <td>
                <span class="cell-strong">{{ a.agent_name }}</span>
                <span v-if="a.base_model_id" class="cell-meta"> · base #{{ a.base_model_id }}</span>
              </td>
              <td class="num tnum">{{ formatNum(a.total_tokens) }}</td>
              <td class="num tnum">{{ formatNum(a.total_requests) }}</td>
            </tr>
            <tr v-if="topAgents.length === 0">
              <td colspan="3"><TermEmpty message="no caller-attributed agent usage in the last 30 days" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>
    </section>

    <!-- Platform links ------------------------------------------------- -->
    <TermBox title="platform · external tooling" :hint="`${platformLinks.length} bound`" pad="md">
      <div v-if="platformLinks.length" class="links">
        <PlatformCard v-for="link in platformLinks" :key="link.id" :link="link" />
      </div>
      <TermEmpty v-else message="no platform links yet · admins can bind external tools under /admin/platform-links" />
    </TermBox>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useUsageStore } from '../stores/usage'
import { useAuthStore } from '../stores/auth'
import { listPlatformLinks } from '../api/platformLinks'
import client from '../api/client'
import UsageLineChart from '../components/charts/UsageLineChart.vue'
import PlatformCard from '../components/dashboard/PlatformCard.vue'
import TermBox from '../components/cli/TermBox.vue'
import TermStat from '../components/cli/TermStat.vue'
import TermEmpty from '../components/cli/TermEmpty.vue'
import TermButton from '../components/cli/TermButton.vue'

const usageStore = useUsageStore()
const authStore = useAuthStore()
const summary = ref(null)
const chartData = ref(null)
const platformLinks = ref([])
const refreshedAt = ref(null)
const loading = ref(false)

// Sprint 8 X / Phase H — admin-only observability widgets.
//   legacyTokenStats: cutover progress for the legacy CSP_SERVICE_TOKEN
//                     fallback. When sustained at 0 for a release window
//                     ops can drop the env var and remove the fallback
//                     branch in auth_service.verify_service_token.
//   topAgents:        top-5 by 30-day caller-attributed token spend.
const legacyTokenStats = ref(null)
const topAgents = ref([])

const legacyTokenHint = computed(() => {
  if (!legacyTokenStats.value) return ''
  const c = legacyTokenStats.value.count_24h
  return c === 0 ? 'no fallback hits in 24h' : `${c} fallback hit${c === 1 ? '' : 's'} in 24h`
})

function formatNum(n) {
  if (n === null || n === undefined) return '0'
  return Number(n).toLocaleString()
}
function formatTs(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toISOString().replace('T', ' ').slice(0, 19) }
  catch { return iso }
}

const scopeLabel = computed(() => {
  if (authStore.isAdmin) return 'full · governance'
  if (authStore.isDeveloper) return 'agents · collections · self'
  return 'self · keys · usage'
})

const refreshedLabel = computed(() => {
  if (!refreshedAt.value) return '—'
  const d = refreshedAt.value
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})

async function fetchAdminWidgets() {
  if (!authStore.isAdmin) return
  try {
    const [{ data: stats }, { data: agents }] = await Promise.all([
      client.get('/api/usage/legacy-token-stats'),
      client.get('/api/usage/top-agents', { params: { days: 30, limit: 5 } }),
    ])
    legacyTokenStats.value = stats
    topAgents.value = Array.isArray(agents) ? agents : []
  } catch {
    // Quiet failure — same posture as the rest of the dashboard.
  }
}

async function refresh() {
  loading.value = true
  try {
    await Promise.all([
      usageStore.fetchSummary(),
      usageStore.fetchChart({ range: '24h', group_by: 'model' }),
      listPlatformLinks().then(({ data }) => { platformLinks.value = data }),
      fetchAdminWidgets(),
    ])
    summary.value = usageStore.summary
    chartData.value = usageStore.chartData
    refreshedAt.value = new Date()
  } catch (e) {
    // Errors surface via the alert center; keep dashboard quiet on failure.
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--gap-5);
  padding-bottom: var(--gap-8);
}

.page__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--gap-3);
  flex-wrap: wrap;
}
.page__eyebrow {
  font-size: var(--t-2xs);
  letter-spacing: var(--tracking-caps);
  text-transform: uppercase;
  color: var(--c-fg-3);
  margin-bottom: 4px;
}
.page__title {
  font-size: var(--t-2xl);
  font-weight: 600;
  letter-spacing: var(--tracking-tight);
  margin: 0;
  color: var(--c-fg-1);
}
.page__head-meta {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-2);
  font-size: var(--t-xs);
  color: var(--c-fg-3);
}
.page__head-val { color: var(--c-fg-1); }

/* KPI grid -------------------------------------------------------------- */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--gap-3);
}
@media (max-width: 1100px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px)  { .kpi-grid { grid-template-columns: 1fr; } }

/* Chart + side meta ----------------------------------------------------- */
.dash-grid {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: var(--gap-3);
}
@media (max-width: 1100px) { .dash-grid { grid-template-columns: 1fr; } }

.ops {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.ops__row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--gap-2) 0;
  border-bottom: var(--border-w) dashed var(--c-border);
  font-size: var(--t-sm);
}
.ops__row:last-child { border-bottom: 0; }
.ops__k {
  color: var(--c-fg-3);
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
}
.ops__v { color: var(--c-fg-1); }
.ops__v--accent { color: var(--c-accent); }
.ops__rule { border: 0; border-top: var(--border-w) solid var(--c-border); margin: var(--gap-2) 0; }
.ops__quick {
  display: flex;
  flex-direction: column;
}
.ops__link {
  color: var(--c-fg-2);
  text-decoration: none;
  font-size: var(--t-sm);
  padding: 4px 0;
  letter-spacing: 0.02em;
}
.ops__link:hover { color: var(--c-accent); text-decoration: none; }

/* Platform link grid ---------------------------------------------------- */
.links {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--gap-2);
}
@media (max-width: 1100px) { .links { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px)  { .links { grid-template-columns: 1fr; } }

/* Sprint 8 X / Phase H — cutover widget */
.cutover { display: flex; flex-direction: column; gap: 10px; }
.cutover__stats { display: flex; gap: 12px; flex-wrap: wrap; }
.cutover__last { margin: 0; font-size: var(--t-2xs); color: var(--c-fg-2); }
.cutover__k { display: inline-block; min-width: 80px; color: var(--c-fg-3); }
.cutover__v { color: var(--c-fg-1); font-family: var(--font-mono); }
.cutover__hint { margin: 4px 0 0; font-size: var(--t-2xs); }
.cutover__hint--ok { color: var(--c-success, #5ca663); }
.cutover__hint--warn { color: var(--c-warn, #c08a2c); }

.term-table { width: 100%; border-collapse: collapse; font-size: var(--t-2xs); }
.term-table th {
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--c-divider);
  font-weight: 500; color: var(--c-fg-2);
  font-size: var(--t-3xs); text-transform: uppercase; letter-spacing: 0.04em;
}
.term-table td { padding: 6px 8px; border-bottom: 1px solid var(--c-divider); }
.term-table .num { text-align: right; }
.tnum { font-variant-numeric: tabular-nums; }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-2); font-size: var(--t-3xs); }
</style>
