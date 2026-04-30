<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">admin · access</p>
        <h1 class="page-head__title">service-access</h1>
        <p class="page-head__sub">per-user / per-department grants for platform_links · multi-service-integration §7.5.3</p>
      </div>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>
    <div v-if="loading" class="loading">loading…</div>

    <div v-else class="link-list">
      <article v-for="link in sortedLinks" :key="link.id" class="link-card">
        <button type="button" class="link-card__head" :class="{ 'is-open': expandedId === link.id }" @click="toggleExpand(link.id)">
          <div class="link-card__title">
            <span class="link-card__name">{{ link.name }}</span>
            <TermBadge v-if="!link.is_active" variant="">inactive</TermBadge>
            <TermBadge v-if="link.is_public" variant="ok">public</TermBadge>
            <span class="role-gate" :class="(link.required_roles || []).length ? 'is-set' : 'is-open'">
              <span class="role-gate__k">role-gate</span>
              <span class="role-gate__v">{{ (link.required_roles || []).length === 0 ? 'open' : (link.required_roles || []).join(' · ') }}</span>
            </span>
          </div>
          <div class="link-card__url">{{ link.url }}</div>
          <div class="link-card__count">
            <span class="cell-meta">active grants</span>
            <span class="link-card__count-num tnum">{{ activeGrantsCount(link.id) }}</span>
          </div>
          <span class="link-card__chev">{{ expandedId === link.id ? '−' : '+' }}</span>
        </button>

        <div v-if="expandedId === link.id" class="link-card__body">
          <div class="link-card__bar">
            <TermButton size="xs" variant="primary" @click="openGrantModal(link, 'user')" label="+ grant user" />
            <TermButton size="xs" @click="openGrantModal(link, 'department')" label="+ grant dept" />
            <span class="bar-meta">
              {{ activeGrantsForLink(link.id).length }} active
              <span v-if="!link.is_public && (link.required_roles || []).length === 0" class="bar-meta--warn">
                · private + open gate · pure whitelist mode
              </span>
            </span>
          </div>

          <table v-if="activeGrantsForLink(link.id).length" class="term-table">
            <thead>
              <tr>
                <th style="width: 80px">scope</th>
                <th>target</th>
                <th style="width: 22%">granted</th>
                <th style="width: 90px">ops</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="g in activeGrantsForLink(link.id)" :key="g.id">
                <td><TermBadge :variant="g.user_id != null ? 'info' : 'warn'">{{ g.user_id != null ? 'user' : 'dept' }}</TermBadge></td>
                <td class="cell-strong">{{ targetLabel(g) }}</td>
                <td class="cell-meta tnum">{{ formatDate(g.granted_at) }} · by {{ granterLabel(g) }}</td>
                <td><button class="term-action term-action--danger" @click="handleRevoke(g)">revoke</button></td>
              </tr>
            </tbody>
          </table>
          <TermEmpty v-else :message="link.is_public ? 'no grants · public — anyone passing the role gate sees this link' : 'no grants yet'" />
        </div>
      </article>

      <div v-if="sortedLinks.length === 0" class="term-box" style="padding: var(--gap-6);">
        <TermEmpty message="no platform links · register one in /admin/platform-links first" />
      </div>
    </div>

    <TermModal :visible="showGrantModal" :title="`grant · ${grantModalLink?.name || ''}`" width="520px" @close="closeGrantModal">
      <p class="cell-meta">a target may have at most one active grant per link.</p>
      <div class="seg">
        <button class="seg__opt" :class="{ 'is-on': grantModalType === 'user' }" @click="setGrantModalType('user')">user-level</button>
        <button class="seg__opt" :class="{ 'is-on': grantModalType === 'department' }" @click="setGrantModalType('department')">department-level</button>
      </div>
      <input v-model="grantModalFilter" :placeholder="grantModalType === 'user' ? 'search username · email' : 'search department'" class="term-input" style="margin-top: var(--gap-2);" />
      <div class="picker term-box term-box--inset" style="margin-top: var(--gap-2);">
        <template v-if="grantModalType === 'user'">
          <button v-for="u in filteredUsers" :key="u.id" type="button" class="picker__row" :class="{ 'is-on': grantModalSelectedId === u.id }" @click="grantModalSelectedId = u.id">
            <span class="picker__main">
              <span class="cell-strong">{{ u.username }}</span>
              <span class="cell-meta">{{ u.role }}{{ u.department_name ? ` · ${u.department_name}` : '' }}</span>
            </span>
            <span v-if="u.email" class="cell-meta">{{ u.email }}</span>
          </button>
          <TermEmpty v-if="filteredUsers.length === 0" message="no matching users" />
        </template>
        <template v-else>
          <button v-for="d in filteredDepts" :key="d.id" type="button" class="picker__row" :class="{ 'is-on': grantModalSelectedId === d.id }" @click="grantModalSelectedId = d.id">
            <span class="picker__main"><span class="cell-strong">{{ d.name }}</span></span>
            <span class="cell-meta">{{ d.user_count ?? 0 }} users</span>
          </button>
          <TermEmpty v-if="filteredDepts.length === 0" :message="departments.length === 0 ? 'no departments yet — create one first' : 'no matching departments'" />
        </template>
      </div>
      <div v-if="grantModalError" class="feedback is-err" style="margin-top: var(--gap-2);">! {{ grantModalError }}</div>
      <template #footer>
        <TermButton variant="ghost" @click="closeGrantModal" label="cancel" />
        <TermButton variant="primary" :disabled="grantModalSubmitting || !grantModalSelectedId" :loading="grantModalSubmitting" :label="grantModalSubmitting ? 'granting' : 'grant'" @click="submitGrant" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { listPlatformLinks } from '../api/platformLinks'
import { listGrants, createGrant, revokeGrant } from '../api/serviceAccessGrants'
import { listUsers } from '../api/users'
import { listDepartments } from '../api/departments'
import { TermBadge, TermButton, TermEmpty, TermModal } from '../components/cli'

const links = ref([])
const grants = ref([])
const users = ref([])
const departments = ref([])
const loading = ref(true)
const pageError = ref('')
const expandedId = ref(null)

const showGrantModal = ref(false)
const grantModalLink = ref(null)
const grantModalType = ref('user')
const grantModalFilter = ref('')
const grantModalSelectedId = ref(null)
const grantModalSubmitting = ref(false)
const grantModalError = ref('')

async function loadAll() {
  loading.value = true
  pageError.value = ''
  try {
    const [l, g, u, d] = await Promise.all([
      listPlatformLinks({ include_inactive: true }),
      listGrants(), listUsers(), listDepartments(),
    ])
    links.value = l.data || []
    grants.value = g.data || []
    users.value = u.data || []
    departments.value = d.data || []
  } catch (e) {
    pageError.value = e.response?.data?.detail || e.message || 'failed to load'
  } finally { loading.value = false }
}
onMounted(loadAll)

const sortedLinks = computed(() => links.value.slice().sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0)))
function activeGrantsForLink(id) { return grants.value.filter(g => g.platform_link_id === id && !g.revoked_at) }
function activeGrantsCount(id) { return activeGrantsForLink(id).length }
function toggleExpand(id) { expandedId.value = expandedId.value === id ? null : id }

const userById = computed(() => { const m = new Map(); for (const u of users.value) m.set(u.id, u); return m })
const deptById = computed(() => { const m = new Map(); for (const d of departments.value) m.set(d.id, d); return m })

function targetLabel(g) {
  if (g.user_id != null) {
    const u = userById.value.get(g.user_id)
    return u ? `${u.username}${u.email ? ` <${u.email}>` : ''}` : `user#${g.user_id}`
  }
  const d = deptById.value.get(g.department_id)
  return d ? d.name : `dept#${g.department_id}`
}
function granterLabel(g) {
  if (!g.granted_by) return 'system'
  const u = userById.value.get(g.granted_by)
  return u ? u.username : `user#${g.granted_by}`
}
function formatDate(s) { return s ? new Date(s).toLocaleString('en-GB') : '—' }

const filteredUsers = computed(() => {
  const q = grantModalFilter.value.trim().toLowerCase()
  return q ? users.value.filter(u => u.username.toLowerCase().includes(q) || (u.email || '').toLowerCase().includes(q)) : users.value
})
const filteredDepts = computed(() => {
  const q = grantModalFilter.value.trim().toLowerCase()
  return q ? departments.value.filter(d => d.name.toLowerCase().includes(q)) : departments.value
})

function openGrantModal(link, type) {
  grantModalLink.value = link
  grantModalType.value = type
  grantModalFilter.value = ''
  grantModalSelectedId.value = null
  grantModalSubmitting.value = false
  grantModalError.value = ''
  showGrantModal.value = true
}
function closeGrantModal() { showGrantModal.value = false; grantModalLink.value = null; grantModalSelectedId.value = null; grantModalError.value = '' }
function setGrantModalType(t) { if (grantModalType.value === t) return; grantModalType.value = t; grantModalSelectedId.value = null }

async function submitGrant() {
  if (!grantModalSelectedId.value || !grantModalLink.value) return
  grantModalSubmitting.value = true
  grantModalError.value = ''
  try {
    const payload = {
      platform_link_id: grantModalLink.value.id,
      ...(grantModalType.value === 'user' ? { user_id: grantModalSelectedId.value } : { department_id: grantModalSelectedId.value }),
    }
    await createGrant(payload)
    closeGrantModal()
    await loadAll()
  } catch (e) {
    grantModalError.value = e.response?.data?.detail || 'grant failed'
  } finally { grantModalSubmitting.value = false }
}
async function handleRevoke(g) {
  if (!confirm(`revoke grant for ${targetLabel(g)}?`)) return
  try { await revokeGrant(g.id); await loadAll() }
  catch (e) { alert(e.response?.data?.detail || 'revoke failed') }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.loading { padding: var(--gap-6); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.link-list { display: flex; flex-direction: column; gap: var(--gap-3); }
.link-card {
  border: var(--border-w) solid var(--c-border);
  background: var(--c-surface-1);
}
.link-card__head {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: var(--gap-3);
  align-items: center;
  padding: var(--gap-3) var(--gap-4);
  background: transparent;
  border: 0;
  width: 100%;
  text-align: left;
  cursor: pointer;
  color: var(--c-fg-1);
}
.link-card__head.is-open { border-bottom: var(--border-w) solid var(--c-border); }
.link-card__head:hover { background: var(--c-row-hover); }

.link-card__title { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; grid-column: 1 / 2; }
.link-card__name { font-weight: 500; }
.link-card__url {
  grid-column: 1 / 2;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  word-break: break-all;
  margin-top: 4px;
}
.link-card__count { display: flex; flex-direction: column; align-items: flex-end; }
.link-card__count-num { font-size: var(--t-xl); color: var(--c-fg-1); font-weight: 600; }
.link-card__chev { color: var(--c-fg-3); font-size: var(--t-base); }

.role-gate {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--t-2xs);
  padding: 0 6px;
  height: 18px;
  border: var(--border-w) solid var(--c-border-strong);
  font-family: var(--font-mono);
}
.role-gate__k { color: var(--c-fg-3); }
.role-gate__v { color: var(--c-fg-1); }
.role-gate.is-set { border-color: var(--c-info); color: var(--c-info); }
.role-gate.is-set .role-gate__k,
.role-gate.is-set .role-gate__v { color: var(--c-info); }

.link-card__body { background: var(--c-bg); }
.link-card__bar {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: var(--gap-2) var(--gap-4);
  border-bottom: var(--border-w) solid var(--c-border);
}
.bar-meta { margin-left: auto; font-size: var(--t-2xs); color: var(--c-fg-3); }
.bar-meta--warn { color: var(--c-warn); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

.seg {
  display: inline-flex;
  border: var(--border-w) solid var(--c-border-strong);
  border-radius: var(--r-soft);
  overflow: hidden;
  margin-top: var(--gap-2);
}
.seg__opt {
  background: transparent;
  border: 0;
  padding: 4px 12px;
  color: var(--c-fg-2);
  font: inherit;
  cursor: pointer;
}
.seg__opt + .seg__opt { border-left: var(--border-w) solid var(--c-border); }
.seg__opt.is-on { background: var(--c-accent); color: var(--c-accent-fg); font-weight: 600; }

.picker { max-height: 280px; overflow-y: auto; padding: 0; }
.picker__row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: var(--gap-2) var(--gap-3);
  background: transparent;
  border: 0;
  border-bottom: var(--border-w) solid var(--c-border);
  text-align: left;
  font: inherit;
  color: var(--c-fg-1);
  cursor: pointer;
}
.picker__row:last-child { border-bottom: 0; }
.picker__row:hover { background: var(--c-row-hover); }
.picker__row.is-on { background: var(--c-accent-soft); }
.picker__main { display: flex; flex-direction: column; gap: 2px; }
</style>
