<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">admin · iam</p>
        <h1 class="page-head__title">users</h1>
        <p class="page-head__sub">role · approval · model + agent allowlist · password reset</p>
      </div>
      <TermButton variant="primary" @click="openCreateModal" label="add user" />
    </header>

    <div v-if="feedback.message" class="feedback" :class="feedback.type === 'error' ? 'is-err' : 'is-ok'">
      <span>{{ feedback.type === 'error' ? '!' : '✓' }}</span>
      <span>{{ feedback.message }}</span>
    </div>

    <div class="kpi-row">
      <TermStat label="users · total" :value="users.length" />
      <TermStat label="pending" :value="pendingCount" :tone="pendingCount ? 'warn' : 'default'" />
      <TermStat label="developers" :value="developerCount" />
      <TermStat label="active" :value="activeCount" tone="accent" />
    </div>

    <TermBox title="filter" pad="sm">
      <div class="filters">
        <TermField label="search">
          <input v-model="filters.query" class="term-input" placeholder="username · email · department" />
        </TermField>
        <TermField label="role">
          <select v-model="filters.role" class="term-select">
            <option value="all">all</option>
            <option value="user">user</option>
            <option value="developer">developer</option>
            <option value="admin">admin</option>
          </select>
        </TermField>
        <TermField label="status">
          <select v-model="filters.status" class="term-select">
            <option value="all">all</option>
            <option value="pending">pending</option>
            <option value="active">active</option>
            <option value="inactive">inactive</option>
          </select>
        </TermField>
        <TermField label="sort">
          <select v-model="filters.sort" class="term-select">
            <option value="newest">newest</option>
            <option value="oldest">oldest</option>
            <option value="name">name a→z</option>
          </select>
        </TermField>
      </div>
    </TermBox>

    <div v-if="selectedUserIds.length" class="bulkbar">
      <span><strong>{{ selectedUserIds.length }}</strong> selected · bulk ops dispatch existing endpoints sequentially</span>
      <div class="bulkbar__actions">
        <TermButton size="xs" @click="handleBulkApprove" label="bulk approve" />
        <TermButton size="xs" variant="danger" @click="handleBulkDeactivate" label="bulk deactivate" />
        <TermButton size="xs" variant="ghost" @click="selectedUserIds = []" label="clear" />
      </div>
    </div>

    <TermBox :title="`registry · ${filteredUsers.length}/${users.length}`" pad="none" flush>
      <div v-if="loading" class="loading">loading users…</div>
      <div v-else-if="filteredUsers.length === 0" style="padding: var(--gap-6) var(--gap-3);">
        <TermEmpty :message="users.length === 0 ? 'no users yet' : 'no users match the filter'" />
      </div>
      <table v-else class="term-table">
        <thead>
          <tr>
            <th style="width: 32px"><input type="checkbox" :checked="allVisibleSelected" @change="toggleSelectAll($event.target.checked)" /></th>
            <th>username</th>
            <th>email</th>
            <th style="width: 14%">department</th>
            <th style="width: 100px">role</th>
            <th style="width: 100px">status</th>
            <th style="width: 14%">last login</th>
            <th style="width: 12%">created</th>
            <th>ops</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in filteredUsers" :key="user.id">
            <td><input type="checkbox" :checked="selectedUserIds.includes(user.id)" @change="toggleUserSelection(user.id, $event.target.checked)" /></td>
            <td>
              <div class="cell-strong">{{ user.username }}</div>
              <div class="cell-meta">id #{{ user.id }}</div>
            </td>
            <td class="cell-meta">{{ user.email || '—' }}</td>
            <td class="cell-meta">{{ user.department_name || '—' }}</td>
            <td><TermBadge :variant="roleVariant(user.role)">{{ user.role }}</TermBadge></td>
            <td>
              <TermBadge :variant="statusVariant(user)" dot>{{ statusLabel(user) }}</TermBadge>
            </td>
            <td class="cell-meta tnum">{{ user.last_login_at ? formatDate(user.last_login_at) : 'never' }}</td>
            <td class="cell-meta tnum">{{ formatDate(user.created_at) }}</td>
            <td>
              <div class="row-actions">
                <button v-if="!user.is_approved" class="term-action" @click="handleApprove(user)">approve</button>
                <span v-if="!user.is_approved" class="row-actions__sep">·</span>
                <button class="term-action" @click="openEditModal(user)">edit</button>
                <span class="row-actions__sep">·</span>
                <button class="term-action" @click="openAllowedModelsModal(user)">models</button>
                <span class="row-actions__sep">·</span>
                <button class="term-action" @click="openAllowedAgentsModal(user)">agents</button>
                <span class="row-actions__sep">·</span>
                <button class="term-action" @click="openResetPasswordModal(user)">reset-pw</button>
                <span class="row-actions__sep">·</span>
                <button v-if="!user.local_password_disabled" class="term-action" @click="handleToggleSsoOnly(user, true)" title="reject local password — sso-only">sso-only</button>
                <button v-else class="term-action" @click="handleToggleSsoOnly(user, false)">unlock-pw</button>
                <span v-if="user.is_active && user.is_approved" class="row-actions__sep">·</span>
                <button v-if="user.is_active && user.is_approved" class="term-action term-action--danger" @click="handleDeactivate(user)">deactivate</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- User edit/create modal --------------------------------------- -->
    <TermModal :visible="showModal" :title="editingId ? 'edit · user' : 'create · user'" width="520px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="username">
          <input v-model="form.username" :disabled="!!editingId" class="term-input" />
        </TermField>
        <TermField v-if="!editingId" label="password">
          <input v-model="form.password" type="password" class="term-input" />
        </TermField>
        <TermField label="email" optional>
          <input v-model="form.email" type="email" class="term-input" />
        </TermField>
        <TermField label="role">
          <select v-model="form.role" class="term-select">
            <option value="user">user</option>
            <option value="developer">developer</option>
            <option value="admin">admin</option>
            <option v-if="authStore.isOwner" value="owner">owner</option>
          </select>
        </TermField>
        <div v-if="form.role !== 'user'" class="role-warn">
          <span>!</span>
          <span>{{ roleHelp(form.role) }}</span>
        </div>
        <div v-if="!authStore.isOwner && elevatedRole(form.role)" class="role-warn" style="margin-top: 4px;">
          <span>⛔</span>
          <span>only owner can create / promote admin or owner accounts.</span>
        </div>
        <TermField label="department">
          <select v-model="form.department_id" class="term-select">
            <option :value="null">— none —</option>
            <option v-for="d in activeDepartments" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="cancel" />
        <TermButton variant="primary" :disabled="!form.username || (!editingId && !form.password)" :label="editingId ? 'update' : 'create'" @click="handleSubmit" />
      </template>
    </TermModal>

    <!-- Allowed models modal ----------------------------------------- -->
    <TermModal :visible="showAllowedModelsModal" :title="`allowed-models · ${allowedModelsTarget?.username || ''}`" width="480px" @close="showAllowedModelsModal = false">
      <p class="cell-meta">changes propagate to existing api-keys for this user.</p>
      <div class="check-list term-box term-box--inset" style="padding: 8px 12px; margin-top: 12px; max-height: 300px; overflow:auto;">
        <label v-for="model in allModels" :key="model.id" class="check-list__row">
          <input type="checkbox" :value="model.id" v-model="selectedModelIds" />
          <span>{{ model.display_name }}</span>
          <TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge>
        </label>
        <p v-if="allModels.length === 0" class="cell-meta">no models registered</p>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showAllowedModelsModal = false" label="cancel" />
        <TermButton variant="primary" :disabled="savingModels" :loading="savingModels" :label="savingModels ? 'saving' : 'save'" @click="handleSaveAllowedModels" />
      </template>
    </TermModal>

    <!-- Allowed agents modal ----------------------------------------- -->
    <TermModal :visible="showAllowedAgentsModal" :title="`allowed-agents · ${allowedAgentsTarget?.username || ''}`" width="480px" @close="showAllowedAgentsModal = false">
      <p class="cell-meta">only approved agents are listed.</p>
      <div class="check-list term-box term-box--inset" style="padding: 8px 12px; margin-top: 12px; max-height: 300px; overflow:auto;">
        <label v-for="agent in allAgents" :key="agent.id" class="check-list__row check-list__row--col">
          <span style="display:flex; align-items:center; gap: 8px;">
            <input type="checkbox" :value="agent.id" v-model="selectedAgentIds" />
            <span>{{ agent.name }}</span>
          </span>
          <span class="cell-meta">{{ agent.description_for_router }}</span>
        </label>
        <p v-if="allAgents.length === 0" class="cell-meta">no approved agents</p>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showAllowedAgentsModal = false" label="cancel" />
        <TermButton variant="primary" :disabled="savingAgents" :loading="savingAgents" :label="savingAgents ? 'saving' : 'save'" @click="handleSaveAllowedAgents" />
      </template>
    </TermModal>

    <!-- Reset password modal ----------------------------------------- -->
    <TermModal :visible="showResetModal" :title="`reset-password · ${resetTarget?.username || ''}`" width="420px" @close="showResetModal = false">
      <TermField label="new password">
        <input v-model="resetPassword" type="password" class="term-input" />
      </TermField>
      <template #footer>
        <TermButton variant="ghost" @click="showResetModal = false" label="cancel" />
        <TermButton variant="primary" :disabled="!resetPassword" label="reset" @click="handleResetPassword" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import client from '../api/client'
import { listDepartments } from '../api/departments'
import { listModels } from '../api/models'
import {
  createUser,
  deactivateUser,
  getUserAllowedAgents,
  getUserAllowedModels,
  listUsers,
  resetUserPassword,
  updateUser,
  updateUserAllowedAgents,
  updateUserAllowedModels,
} from '../api/users'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat } from '../components/cli'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()

const users = ref([])
const departments = ref([])
const allModels = ref([])
const allAgents = ref([])
const loading = ref(false)
const feedback = ref({ type: 'success', message: '' })
const filters = ref({ query: '', role: 'all', status: 'all', sort: 'newest' })
const selectedUserIds = ref([])

const showModal = ref(false)
const editingId = ref(null)
const form = ref({ username: '', password: '', email: '', role: 'user', department_id: null })
const showResetModal = ref(false)
const resetTarget = ref(null)
const resetPassword = ref('')
const showAllowedModelsModal = ref(false)
const allowedModelsTarget = ref(null)
const selectedModelIds = ref([])
const savingModels = ref(false)
const showAllowedAgentsModal = ref(false)
const allowedAgentsTarget = ref(null)
const selectedAgentIds = ref([])
const savingAgents = ref(false)

const activeDepartments = computed(() => departments.value.filter(d => d.is_active))
const pendingCount = computed(() => users.value.filter(u => !u.is_approved).length)
const developerCount = computed(() => users.value.filter(u => u.role === 'developer').length)
const activeCount = computed(() => users.value.filter(u => u.is_active && u.is_approved).length)

const filteredUsers = computed(() => {
  const query = filters.value.query.trim().toLowerCase()
  let next = users.value.filter(u => {
    if (filters.value.role !== 'all' && u.role !== filters.value.role) return false
    if (filters.value.status === 'pending' && u.is_approved) return false
    if (filters.value.status === 'active' && (!u.is_approved || !u.is_active)) return false
    if (filters.value.status === 'inactive' && (!u.is_approved || u.is_active)) return false
    if (!query) return true
    return [u.username, u.email, u.department_name, u.role].filter(Boolean).join(' ').toLowerCase().includes(query)
  })
  next = [...next].sort((a, b) => {
    if (filters.value.sort === 'oldest') return new Date(a.created_at) - new Date(b.created_at)
    if (filters.value.sort === 'name') return (a.username || '').localeCompare(b.username || '')
    return new Date(b.created_at) - new Date(a.created_at)
  })
  return next
})

const allVisibleSelected = computed(() =>
  filteredUsers.value.length > 0 && filteredUsers.value.every(u => selectedUserIds.value.includes(u.id))
)

function setFeedback(type, message) {
  feedback.value = { type, message }
  if (message) setTimeout(() => { feedback.value = { type: 'success', message: '' } }, 5000)
}

async function fetchUsers() {
  loading.value = true
  try { const { data } = await listUsers(); users.value = data }
  catch (e) { setFeedback('error', e.response?.data?.detail || 'failed to load users') }
  finally { loading.value = false }
}

onMounted(async () => {
  await fetchUsers()
  try { const { data } = await listDepartments(); departments.value = data } catch {}
  try { const { data } = await listModels(); allModels.value = data } catch {}
  try {
    const { data } = await client.get('/api/agents')
    allAgents.value = data.filter(a => a.approval_status === 'approved')
  } catch {}
})

function openCreateModal() {
  editingId.value = null
  form.value = { username: '', password: '', email: '', role: 'user', department_id: null }
  showModal.value = true
}
function openEditModal(user) {
  editingId.value = user.id
  form.value = { username: user.username, email: user.email || '', role: user.role, department_id: user.department_id ?? null }
  showModal.value = true
}

async function handleSubmit() {
  try {
    if (editingId.value) {
      await updateUser(editingId.value, { email: form.value.email || null, role: form.value.role, department_id: form.value.department_id })
      setFeedback('success', 'user updated')
    } else {
      await createUser(form.value)
      setFeedback('success', 'user created')
    }
    showModal.value = false
    await fetchUsers()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'operation failed') }
}

async function openAllowedModelsModal(user) {
  allowedModelsTarget.value = user
  selectedModelIds.value = []
  try { const { data } = await getUserAllowedModels(user.id); selectedModelIds.value = data.map(m => m.id) } catch {}
  showAllowedModelsModal.value = true
}
async function handleSaveAllowedModels() {
  savingModels.value = true
  try {
    const r = await updateUserAllowedModels(allowedModelsTarget.value.id, selectedModelIds.value)
    showAllowedModelsModal.value = false
    setFeedback('success', r.data?.message || 'allowlist updated')
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || 'failed to update allowlist')
  } finally { savingModels.value = false }
}

async function openAllowedAgentsModal(user) {
  allowedAgentsTarget.value = user
  selectedAgentIds.value = []
  try { const { data } = await getUserAllowedAgents(user.id); selectedAgentIds.value = data.map(a => a.id) } catch {}
  showAllowedAgentsModal.value = true
}
async function handleSaveAllowedAgents() {
  savingAgents.value = true
  try {
    const r = await updateUserAllowedAgents(allowedAgentsTarget.value.id, selectedAgentIds.value)
    showAllowedAgentsModal.value = false
    setFeedback('success', r.data?.message || 'agent allowlist updated')
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || 'failed to update agents')
  } finally { savingAgents.value = false }
}

function openResetPasswordModal(user) { resetTarget.value = user; resetPassword.value = ''; showResetModal.value = true }
async function handleResetPassword() {
  try {
    await resetUserPassword(resetTarget.value.id, { new_password: resetPassword.value })
    showResetModal.value = false
    setFeedback('success', `password reset for '${resetTarget.value.username}'`)
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || 'reset failed')
  }
}

async function handleApprove(user) {
  try {
    await client.post(`/api/users/${user.id}/approve`)
    setFeedback('success', `approved '${user.username}'`)
    await fetchUsers()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'approve failed') }
}
async function handleDeactivate(user) {
  if (!window.confirm(`deactivate '${user.username}'?`)) return
  try {
    await deactivateUser(user.id)
    setFeedback('success', `deactivated '${user.username}'`)
    await fetchUsers()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'deactivate failed') }
}
async function handleToggleSsoOnly(user, disable) {
  const action = disable ? 'switch to sso-only' : 'unlock local password'
  if (!window.confirm(`${action} for '${user.username}'?`)) return
  try {
    await updateUser(user.id, { local_password_disabled: disable })
    setFeedback('success', disable ? `'${user.username}' is now sso-only` : `local-password unlocked for '${user.username}'`)
    await fetchUsers()
  } catch (e) { setFeedback('error', e.response?.data?.detail || `${action} failed`) }
}

function toggleUserSelection(id, on) {
  if (on) selectedUserIds.value = Array.from(new Set([...selectedUserIds.value, id]))
  else selectedUserIds.value = selectedUserIds.value.filter(x => x !== id)
}
function toggleSelectAll(on) {
  if (on) {
    selectedUserIds.value = Array.from(new Set([...selectedUserIds.value, ...filteredUsers.value.map(u => u.id)]))
  } else {
    const visible = new Set(filteredUsers.value.map(u => u.id))
    selectedUserIds.value = selectedUserIds.value.filter(id => !visible.has(id))
  }
}

async function handleBulkApprove() {
  const targets = users.value.filter(u => selectedUserIds.value.includes(u.id) && !u.is_approved)
  if (!targets.length) { setFeedback('error', 'no pending users in selection'); return }
  for (const u of targets) await client.post(`/api/users/${u.id}/approve`)
  selectedUserIds.value = []
  setFeedback('success', `approved ${targets.length} users`)
  await fetchUsers()
}
async function handleBulkDeactivate() {
  const targets = users.value.filter(u => selectedUserIds.value.includes(u.id) && u.is_active && u.is_approved)
  if (!targets.length) { setFeedback('error', 'no active users in selection'); return }
  if (!window.confirm(`deactivate ${targets.length} users?`)) return
  for (const u of targets) await deactivateUser(u.id)
  selectedUserIds.value = []
  setFeedback('success', `deactivated ${targets.length} users`)
  await fetchUsers()
}

function roleVariant(role) {
  if (role === 'owner') return 'danger'
  if (role === 'admin') return 'warn'
  if (role === 'developer') return 'info'
  return ''
}
function elevatedRole(role) {
  return role === 'admin' || role === 'owner'
}
function roleHelp(role) {
  if (role === 'owner') return 'owner is the platform operator — exclusive control over auth providers, hard-purge, raw audit fields, and admin/owner role management.'
  if (role === 'admin') return 'admin can manage users, models, audits, billing, and usage. cannot create/demote admins or alter platform-level config.'
  if (role === 'developer') return 'developer can register agents and download templates.'
  return ''
}
function statusVariant(u) {
  if (!u.is_approved) return 'warn'
  return u.is_active ? 'ok' : 'danger'
}
function statusLabel(u) {
  if (!u.is_approved) return 'pending'
  return u.is_active ? 'active' : 'inactive'
}
function formatDate(dateStr) {
  if (!dateStr) return '—'
  return new Date(dateStr).toLocaleString('en-GB')
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.feedback {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  font-size: var(--t-xs);
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid;
}
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.feedback.is-ok  { color: var(--c-ok);     border-color: var(--c-ok);     background: var(--c-ok-soft); }

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-3); }
@media (max-width: 800px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }

.filters { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: var(--gap-3); }
@media (max-width: 800px) { .filters { grid-template-columns: 1fr 1fr; } }

.bulkbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap-3);
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-accent);
  background: var(--c-accent-soft);
  color: var(--c-accent);
  font-size: var(--t-xs);
}
.bulkbar__actions { display: inline-flex; gap: 6px; }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

.row-actions { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.loading { padding: var(--gap-6); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }

.role-warn {
  display: flex;
  gap: var(--gap-2);
  align-items: flex-start;
  padding: var(--gap-2) var(--gap-3);
  background: var(--c-warn-soft);
  color: var(--c-warn);
  border: var(--border-w) solid var(--c-warn);
  font-size: var(--t-xs);
}

.check-list__row {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: 4px 0;
  font-size: var(--t-sm);
  color: var(--c-fg-1);
  cursor: pointer;
}
.check-list__row--col {
  flex-direction: column;
  align-items: flex-start;
}
.check-list__row input { accent-color: var(--c-accent); }
</style>
