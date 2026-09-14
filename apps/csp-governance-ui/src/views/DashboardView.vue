<template>
  <div class="page">
    <PageHead title="儀表板" subtitle="先處理需要注意的事，再看近 24 小時用量。">
      <template #actions>
        <span class="page-head__meta">近 24 小時 · 更新於 {{ refreshedLabel }}</span>
        <TermButton size="xs" variant="ghost" :loading="loading" @click="refresh" label="重新整理" />
      </template>
    </PageHead>

    <div v-if="loadError" class="feedback is-err">
      ! {{ loadError }}
      <button type="button" class="err-retry" @click="refresh">重試</button>
    </div>

    <!-- P3.3 / P3.4 companion — 服務健康總覽 + 告警摘要（admin only） ---- -->
    <section v-if="authStore.isAdmin" class="dash-grid">
      <ServiceHealthCard
        :overview="healthOverview"
        :loading="healthLoading"
        :page-error="healthError"
        @refresh="fetchHealthOverview"
      />
      <AlertSummaryCard :raw="alertSummary" :page-error="alertError" />
    </section>


    <!-- KPI strip ------------------------------------------------------- -->
    <section class="kpi-grid">
      <TermStat label="24h · 請求數" :value="kpiValue(summary?.total_requests)" :format="kpiFormat" tone="accent" />
      <TermStat label="24h · Token"   :value="kpiValue(summary?.total_tokens)" :format="kpiFormat" />
      <TermStat label="健康模型" :value="kpiValue(summary?.active_models)" :format="kpiFormat" hint="通過健康檢查的模型數" />
      <TermStat label="近 24h 有呼叫的金鑰"  :value="kpiValue(summary?.active_api_keys)" :format="kpiFormat" hint="區間內實際發出請求的金鑰" />
    </section>

    <!-- Chart + side meta ---------------------------------------------- -->
    <section class="dash-grid">
      <TermBox title="用量 · 吞吐 · 24h" hint="依模型 · 當地時區" pad="md">
        <UsageLineChart :chart-data="chartData" :height="280" />
      </TermBox>

      <TermBox title="快速操作" pad="md">
        <ul class="ops">
          <li class="ops__row">
            <span class="ops__k">角色</span>
            <span class="ops__v">{{ roleLabel(authStore.user?.role) }}</span>
          </li>
          <li class="ops__row">
            <span class="ops__k">權限範圍</span>
            <span class="ops__v">{{ scopeLabel }}</span>
          </li>
        </ul>
        <hr class="ops__rule" />
        <div class="ops__quick">
          <router-link to="/api-keys" class="ops__link">→ 建立 API 金鑰</router-link>
          <router-link to="/models" class="ops__link">→ 檢視模型</router-link>
          <router-link to="/usage" class="ops__link">→ 用量分析</router-link>
          <router-link v-if="authStore.isDeveloper" to="/developer/agents" class="ops__link">→ 註冊 Agent</router-link>
          <router-link v-if="authStore.isAdmin" to="/audit-logs" class="ops__link">→ 稽核紀錄</router-link>
          <router-link v-if="authStore.isAdmin" to="/feedback" class="ops__link">→ 使用者回饋</router-link>
        </div>
      </TermBox>
    </section>

    <!-- Sprint 8 X / Phase H — admin observability strip ---------------- -->
    <section v-if="authStore.isAdmin" class="dash-grid">
      <!-- legacy-token cutover progress widget -->
      <TermBox
        title="舊版服務憑證"
        :hint="legacyTokenHint"
        :tone="legacyTokenStats?.count_24h ? 'warn' : ''"
        pad="md"
      >
        <div v-if="legacyTokenStats" class="cutover">
          <div class="cutover__stats">
            <TermStat label="近 24 小時命中" :value="legacyTokenStats.count_24h" :tone="legacyTokenStats.count_24h ? 'warn' : 'ok'" />
            <TermStat label="近 7 天命中"  :value="legacyTokenStats.count_7d" />
            <TermStat label="近 30 天命中" :value="legacyTokenStats.count_30d" />
          </div>
          <p class="cutover__last">
            <span class="cutover__k">最近出現</span>
            <span class="cutover__v tnum">{{ legacyTokenStats.last_seen_at ? formatDate(legacyTokenStats.last_seen_at) : 'never (cutover clean)' }}</span>
          </p>
          <p v-if="legacyTokenStats.count_30d === 0" class="cutover__hint cutover__hint--ok">
            ✓ 30 天內沒有舊版憑證命中，可安排從環境設定移除舊憑證。
          </p>
          <p v-else class="cutover__hint cutover__hint--warn">
            仍有服務使用舊版環境變數憑證。請到稽核紀錄依來源位址找出尚未更換的主機。
          </p>
        </div>
        <div v-else-if="legacyTokenLoading || !legacyTokenTried" class="cutover-state">
          <TermEmpty message="載入中…" />
        </div>
        <div v-else class="cutover-state">
          <p class="feedback is-err">! {{ legacyTokenError || '無法載入舊版 token 統計' }}</p>
          <TermButton size="xs" variant="ghost" :loading="legacyTokenLoading" label="重試" @click="fetchAdminWidgets" />
        </div>
      </TermBox>

      <!-- top-5 agents over the last 30 days -->
      <TermBox title="熱門助手 · 30 天" hint="依呼叫端歸屬的用量" pad="none" flush>
        <table class="term-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th class="num" style="width: 110px">Token</th>
              <th class="num" style="width: 110px">請求數</th>
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
            <tr v-if="topAgents.length === 0 && legacyTokenTried && !legacyTokenLoading && !legacyTokenError">
              <td colspan="3"><TermEmpty message="過去 30 天無歸屬呼叫端的 Agent 用量" /></td>
            </tr>
            <tr v-if="topAgents.length === 0 && legacyTokenTried && !legacyTokenLoading && legacyTokenError">
              <td colspan="3"><TermEmpty message="熱門 Agent 一併載入失敗 · 請重試上方卡片" /></td>
            </tr>
          </tbody>
        </table>
      </TermBox>
    </section>

    <!-- Platform links ------------------------------------------------- -->
    <TermBox title="平台 · 外部工具" :hint="`已綁定 ${platformLinks.length} 項`" pad="md">
      <div v-if="platformLinks.length" class="links">
        <PlatformCard v-for="link in platformLinks" :key="link.id" :link="link" />
      </div>
      <!-- 路徑以 router/index.js 的 `platform-links` 為準;/admin/platform-links
           是麵包屑用的顯示字串,不是可導覽的位址(沒有 route、沒有 catch-all)。 -->
      <TermEmpty v-else message="尚無平台連結 · 管理員可於 /platform-links 綁定外部工具" />
    </TermBox>
  </div>
</template>

<script setup>
import { roleLabel } from '../utils/roleLabel'
import { ref, computed, onMounted } from 'vue'
import { useUsageStore } from '../stores/usage'
import { useAuthStore } from '../stores/auth'
import { listPlatformLinks } from '../api/platformLinks'
import { getHealthOverview } from '../api/health'
import { getAlertSummary } from '../api/alerts'
import { extractError } from '../api/errors'
import client from '../api/client'
import { filterPlatformLinksForRelease } from '../utils/anilalmReleaseGate'
import { formatDate } from '../utils/formatDate'
import UsageLineChart from '../components/charts/UsageLineChart.vue'
import PlatformCard from '../components/dashboard/PlatformCard.vue'
import ServiceHealthCard from '../components/dashboard/ServiceHealthCard.vue'
import AlertSummaryCard from '../components/dashboard/AlertSummaryCard.vue'
import TermBox from '../components/cli/TermBox.vue'
import TermStat from '../components/cli/TermStat.vue'
import TermEmpty from '../components/cli/TermEmpty.vue'
import TermButton from '../components/cli/TermButton.vue'
import PageHead from '../components/cli/PageHead.vue'

const usageStore = useUsageStore()
const authStore = useAuthStore()
const summary = ref(null)
const chartData = ref(null)
const platformLinks = ref([])
const refreshedAt = ref(null)
const loading = ref(false)
const loadError = ref('')
const summaryLoaded = ref(false)

// Sprint 8 X / Phase H — admin-only observability widgets.
//   legacyTokenStats: cutover progress for the legacy long-lived service credential
//                     fallback. When sustained at 0 for a release window
//                     ops can drop the env var and remove the fallback
//                     branch in auth_service.verify_service_token.
//   topAgents:        top-5 by 30-day caller-attributed token spend.
const legacyTokenStats = ref(null)
const legacyTokenLoading = ref(false)
const legacyTokenTried = ref(false) // avoids a flash of "failed" before first fetch
const legacyTokenError = ref('')
const topAgents = ref([])

// P3.3 / attic W3-3⑦④ — 服務健康 + 告警摘要。
// 刻意不吃 dashboard 靜默失敗慣例:留白會被讀成「一切正常」。
const healthOverview = ref(null)
const healthLoading = ref(false)
const healthError = ref('')
const alertSummary = ref(null)
const alertError = ref('')

async function fetchHealthOverview() {
  if (!authStore.isAdmin) return
  healthLoading.value = true
  healthError.value = ''
  try {
    const { data } = await getHealthOverview()
    healthOverview.value = data
  } catch (e) {
    healthOverview.value = null
    healthError.value = extractError(e, '載入服務健康總覽失敗')
  } finally {
    healthLoading.value = false
  }
}

async function fetchAlertSummary() {
  if (!authStore.isAdmin) return
  alertError.value = ''
  try {
    const { data } = await getAlertSummary()
    alertSummary.value = data
  } catch (e) {
    alertSummary.value = null
    alertError.value = extractError(e, '載入告警摘要失敗')
  }
}

const legacyTokenHint = computed(() => {
  if (legacyTokenLoading.value || !legacyTokenTried.value) return '載入中'
  if (legacyTokenError.value) return '載入失敗'
  if (!legacyTokenStats.value) return ''
  const c = legacyTokenStats.value.count_24h
  return c === 0 ? '24 小時內無舊 token 回退' : `24 小時內 ${c} 次舊 token 回退`
})

/** Avoid TermStat's Number(x)||0 turning "—" into a fake zero. */
const kpiFormat = computed(() => (summaryLoaded.value ? 'compact' : 'raw'))

function formatNum(n) {
  if (n === null || n === undefined) return '0'
  return Number(n).toLocaleString()
}

/** Failed / not-yet-loaded must not look like a quiet day of zeros. */
function kpiValue(n) {
  if (!summaryLoaded.value) return '—'
  if (n === null || n === undefined) return 0
  return n
}

const scopeLabel = computed(() => {
  if (authStore.isAdmin) return '全部功能'
  if (authStore.isDeveloper) return 'Agent 與知識庫'
  return '個人金鑰與用量'
})

const refreshedLabel = computed(() => {
  if (!refreshedAt.value) return '—'
  const d = refreshedAt.value
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})

async function fetchAdminWidgets() {
  if (!authStore.isAdmin) return
  legacyTokenLoading.value = true
  legacyTokenError.value = ''
  try {
    const [{ data: stats }, { data: agents }] = await Promise.all([
      client.get('/api/usage/legacy-token-stats'),
      client.get('/api/usage/top-agents', { params: { days: 30, limit: 5 } }),
    ])
    legacyTokenStats.value = stats
    topAgents.value = Array.isArray(agents) ? agents : []
  } catch (e) {
    legacyTokenStats.value = null
    topAgents.value = []
    legacyTokenError.value = `舊版 token 統計載入失敗：${extractError(e, '未知錯誤')}`
  } finally {
    legacyTokenLoading.value = false
    legacyTokenTried.value = true
  }
}

async function refresh() {
  loading.value = true
  loadError.value = ''
  try {
    // 每個資料來源各自沉澱,一個壞掉不能讓其他的看起來像「真的是 0」。
    // 這是 2026-07-31 修掉的缺陷:抓取失敗時 KPI 落到 `|| 0`,長得跟安靜的一天
    // 一模一樣;而當時的註解宣稱「錯誤會由 alert center 呈現」——那句話是錯的,
    // 攔截器只處理 401 refresh。
    const admin = fetchAdminWidgets()
    // 健康總覽與告警摘要(P3.3)也獨立沉澱:健康探測掛掉不該讓用量看起來是 0。
    const health = fetchHealthOverview().catch(() => {})
    const alerts = fetchAlertSummary().catch(() => {})

    let usageOk = false
    try {
      await Promise.all([
        usageStore.fetchSummary(),
        usageStore.fetchChart({ range: '24h', group_by: 'model' }),
      ])
      summary.value = usageStore.summary
      chartData.value = usageStore.chartData
      summaryLoaded.value = true
      usageOk = true
    } catch (e) {
      summaryLoaded.value = false
      chartData.value = null
      loadError.value = `儀表板用量載入失敗：${extractError(e)}`
    }

    try {
      const { data } = await listPlatformLinks()
      // ANILA LM release gate：關閉時儀表板不顯示該入口（見 anilalmReleaseGate.js）。
      platformLinks.value = filterPlatformLinksForRelease(Array.isArray(data) ? data : [])
    } catch (e) {
      platformLinks.value = []
      if (!loadError.value) {
        loadError.value = `平台連結載入失敗：${extractError(e)}`
      }
    }

    await Promise.all([admin, health, alerts])
    if (usageOk) refreshedAt.value = new Date()
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

.feedback {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  flex-wrap: wrap;
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--radius-sm, 4px);
  font-size: var(--t-sm);
}
.feedback.is-err {
  color: var(--c-danger);
  border-color: var(--c-danger);
  background: var(--c-danger-soft);
}
.err-retry {
  margin-left: auto;
  background: transparent;
  border: var(--border-w) solid currentColor;
  color: inherit;
  font: inherit;
  font-size: var(--t-xs);
  padding: 2px 8px;
  cursor: pointer;
  border-radius: var(--radius-sm, 4px);
}
.err-retry:hover { opacity: 0.85; }

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
.cutover-state {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}
.cutover-state .feedback { width: 100%; margin: 0; }

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
