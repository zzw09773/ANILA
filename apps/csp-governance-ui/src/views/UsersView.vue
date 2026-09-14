<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">使用者</h1>
        <p class="page-head__sub">角色 · 審核 · 模型 + Agent 允許清單 · 密碼重設</p>
      </div>
      <TermButton variant="primary" @click="openCreateModal" label="新增使用者" />
    </header>

    <div v-if="feedback.message" class="feedback" :class="feedback.type === 'error' ? 'is-err' : 'is-ok'">
      <span>{{ feedback.type === 'error' ? '!' : '✓' }}</span>
      <span>{{ feedback.message }}</span>
    </div>

    <div class="kpi-row">
      <TermStat label="使用者 · 總數" :value="users.length" />
      <TermStat label="待審核" :value="pendingCount" :tone="pendingCount ? 'warn' : 'default'" />
      <TermStat label="開發者" :value="developerCount" />
      <TermStat label="使用中" :value="activeCount" tone="accent" />
    </div>

    <TermBox title="篩選" pad="sm">
      <div class="filters">
        <TermField label="搜尋">
          <input v-model="filters.query" class="term-input" placeholder="使用者名稱 · email · 部門" />
        </TermField>
        <TermField label="角色">
          <select v-model="filters.role" class="term-select">
            <option value="all">全部</option>
            <option value="user">一般使用者</option>
            <option value="developer">開發者</option>
            <option value="admin">管理員</option>
            <option value="owner">擁有者</option>
            <option value="system">系統</option>
          </select>
        </TermField>
        <TermField label="狀態">
          <select v-model="filters.status" class="term-select">
            <option value="all">全部</option>
            <option value="pending">待審核</option>
            <option value="active">使用中</option>
            <option value="inactive">已停用</option>
          </select>
        </TermField>
        <TermField label="排序">
          <select v-model="filters.sort" class="term-select">
            <option value="newest">最新</option>
            <option value="oldest">最舊</option>
            <option value="name">名稱 a→z</option>
          </select>
        </TermField>
      </div>
    </TermBox>

    <div v-if="selectedUserIds.length" class="bulkbar">
      <span>已選 <strong>{{ selectedUserIds.length }}</strong> 筆 · 批次操作依序呼叫既有端點</span>
      <div class="bulkbar__actions">
        <TermButton size="xs" @click="handleBulkApprove" label="批次核准" />
        <TermButton size="xs" variant="danger" @click="handleBulkDeactivate" label="批次停用" />
        <TermButton size="xs" variant="ghost" @click="selectedUserIds = []" label="清除" />
      </div>
    </div>

    <TermBox :title="`已註冊 · ${filteredUsers.length}/${users.length}`" pad="none" flush>
      <div v-if="loading" class="loading">載入使用者中…</div>
      <div v-else-if="filteredUsers.length === 0" style="padding: var(--gap-6) var(--gap-3);">
        <TermEmpty :message="users.length === 0 ? '尚無使用者' : '無符合篩選的使用者'" />
      </div>
      <table v-else class="term-table">
        <thead>
          <tr>
            <th style="width: 32px"><input type="checkbox" :checked="allVisibleSelected" @change="toggleSelectAll($event.target.checked)" aria-label="選取本頁全部使用者" /></th>
            <th>使用者名稱</th>
            <th>email</th>
            <th style="width: 14%">部門</th>
            <th style="width: 100px">角色</th>
            <th style="width: 100px">狀態</th>
            <th style="width: 14%">上次登入</th>
            <th style="width: 12%">建立時間</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in filteredUsers" :key="user.id">
            <td><input type="checkbox" :checked="selectedUserIds.includes(user.id)" @change="toggleUserSelection(user.id, $event.target.checked)" :aria-label="'選取 ' + user.username" /></td>
            <td>
              <div class="cell-strong">{{ user.username }}</div>
              <div class="cell-meta">id #{{ user.id }}</div>
            </td>
            <td class="cell-meta">{{ user.email || '—' }}</td>
            <td class="cell-meta">{{ departmentLabel(user) }}</td>
            <td><TermBadge :variant="roleVariant(user.role)">{{ roleLabel(user.role) }}</TermBadge></td>
            <td>
              <TermBadge :variant="statusVariant(user)" dot>{{ statusLabel(user) }}</TermBadge>
            </td>
            <td class="cell-meta tnum">{{ user.last_login_at ? formatDate(user.last_login_at) : '從未' }}</td>
            <td class="cell-meta tnum">{{ formatDate(user.created_at) }}</td>
            <td>
              <RowActions>
                <button v-if="!user.is_approved" class="term-action" @click="handleApprove(user)">核准</button>
                <button class="term-action" @click="openEditModal(user)">編輯</button>
                <button class="term-action" @click="openRouterModelsModal(user)">對話可用模型</button>
                <button v-if="user.is_active && user.is_approved" class="term-action term-action--danger" @click="handleDeactivate(user)">停用</button>
                <button v-if="!user.is_active" class="term-action" @click="handleActivate(user)">啟用</button>
                <template #more>
                  <button class="term-action" @click="openAllowedModelsModal(user)">API 金鑰可用模型</button>
                  <button class="term-action" @click="openAllowedAgentsModal(user)">Agent</button>
                  <button class="term-action" @click="openResetPasswordModal(user)">重設密碼</button>
                  <button v-if="!user.local_password_disabled" class="term-action" @click="handleToggleSsoOnly(user, true)">改為僅限 SSO 登入</button>
                  <button v-else class="term-action" @click="handleToggleSsoOnly(user, false)">解鎖密碼</button>
                  <button class="term-action term-action--danger" @click="openHardDeleteModal(user)">刪除</button>
                </template>
              </RowActions>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Hard delete confirmation modal (branch SSO) ------------------- -->
    <TermModal
      :visible="showHardDeleteModal"
      title="永久刪除 · 確認"
      width="520px"
      @close="closeHardDeleteModal"
    >
      <div v-if="hardDeleteTarget" class="login__reg">
        <div class="login__msg" style="background: var(--c-danger-soft); border-color: var(--c-danger); color: var(--c-danger);">
          <strong>⚠ 此操作無法復原</strong>
        </div>
        <p style="font-size: var(--t-xs); color: var(--c-fg-2); line-height: 1.55;">
          將永久刪除使用者 <strong>{{ hardDeleteTarget.username }}</strong>
          ({{ hardDeleteTarget.role }})。
        </p>
        <ul style="font-size: var(--t-2xs); color: var(--c-fg-3); padding-left: 18px; margin: 0;">
          <li>該使用者的 API keys 跟對話歷史會一起刪除</li>
          <li>Audit log 內 actor 欄位會轉為 NULL，但記錄保留</li>
          <li>若該使用者擁有 agent，刪除會被拒絕，請先處理 agent 擁有權</li>
          <li>如僅需暫時停用，請改用「停用」</li>
        </ul>
        <TermField label="輸入使用者名稱以確認">
          <input
            v-model="hardDeleteConfirm"
            class="term-input"
            :placeholder="hardDeleteTarget.username"
            autocomplete="off"
          />
        </TermField>
        <div v-if="hardDeleteError" class="login__msg is-err">! {{ hardDeleteError }}</div>
      </div>

      <template #footer>
        <TermButton variant="ghost" @click="closeHardDeleteModal" label="取消" />
        <TermButton
          variant="danger"
          :disabled="!hardDeleteConfirmed"
          :loading="hardDeleting"
          :label="hardDeleting ? '刪除中' : '永久刪除'"
          @click="handleHardDelete"
        />
      </template>
    </TermModal>

    <!-- User edit/create modal --------------------------------------- -->
    <TermModal :visible="showModal" :title="editingId ? '編輯 · 使用者' : '建立 · 使用者'" width="520px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="使用者名稱">
          <input v-model="form.username" :disabled="!!editingId" class="term-input" />
        </TermField>
        <TermField v-if="!editingId" label="密碼">
          <input v-model="form.password" type="password" class="term-input" />
        </TermField>
        <TermField label="email" optional>
          <input v-model="form.email" type="email" class="term-input" />
        </TermField>
        <TermField label="角色">
          <select v-model="form.role" class="term-select">
            <option value="user">一般使用者</option>
            <option value="developer">開發者</option>
            <option value="admin">管理員</option>
            <option v-if="authStore.isOwner" value="owner">擁有者</option>
          </select>
        </TermField>
        <div v-if="form.role !== 'user'" class="role-warn">
          <span>!</span>
          <span>{{ roleHelp(form.role) }}</span>
        </div>
        <div v-if="!authStore.isOwner && elevatedRole(form.role)" class="role-warn" style="margin-top: 4px;">
          <span>⛔</span>
          <span>只有擁有者能建立 / 提升 admin 或 owner 帳號。</span>
        </div>
        <TermField label="部門" hint="可以掛在任何一層：直屬院部就選最上層，所底下沒有分組就選所。">
          <select v-model="form.department_id" class="term-select">
            <option :value="null">— 無 —</option>
            <option v-for="d in departmentChoices" :key="d.id" :value="d.id">{{ d.label }}</option>
          </select>
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="取消" />
        <TermButton variant="primary" :disabled="!form.username || (!editingId && !form.password)" :label="editingId ? '更新' : '建立'" @click="handleSubmit" />
      </template>
    </TermModal>

    <!-- Allowed models modal ----------------------------------------- -->
    <TermModal :visible="showRouterModelsModal" :title="`對話可用模型 · ${routerModelsTarget?.username || ''}`" width="520px" @close="showRouterModelsModal = false">
      <p class="cell-meta">這是聊天時能選的模型。要改誰能選，請到模型頁的「可使用對象」。這裡只能看，不能改。</p>
      <p v-if="routerModelsLoading" class="cell-meta">載入中…</p>
      <p v-else-if="!routerModels.length" class="cell-meta">目前沒有可選的對話模型。</p>
      <ul v-else class="router-models">
        <li v-for="m in routerModels" :key="m.id">
          <strong>{{ m.display_name }}</strong>
          <span class="cell-meta">{{ sourceLabel(m.grant_sources) }}</span>
        </li>
      </ul>
      <template #footer>
        <TermButton variant="ghost" @click="showRouterModelsModal = false" label="關閉" />
      </template>
    </TermModal>
    <TermModal :visible="showAllowedModelsModal" :title="`API 金鑰可用模型 · ${allowedModelsTarget?.username || ''}`" width="480px" @close="showAllowedModelsModal = false">
      <p class="cell-meta">這只影響他申請的 API 金鑰能呼叫哪些模型，不會改聊天選單。</p>
      <div class="check-list term-box term-box--inset" style="padding: 8px 12px; margin-top: 12px; max-height: 300px; overflow:auto;">
        <label v-for="model in allModels" :key="model.id" class="check-list__row">
          <input type="checkbox" :value="model.id" v-model="selectedModelIds" />
          <span>{{ model.display_name }}</span>
          <TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge>
        </label>
        <p v-if="allModels.length === 0" class="cell-meta">尚未註冊模型</p>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showAllowedModelsModal = false" label="取消" />
        <TermButton variant="primary" :disabled="savingModels" :loading="savingModels" :label="savingModels ? '儲存中' : '儲存'" @click="handleSaveAllowedModels" />
      </template>
    </TermModal>

    <!-- Allowed agents modal ----------------------------------------- -->
    <TermModal :visible="showAllowedAgentsModal" :title="`允許 Agent · ${allowedAgentsTarget?.username || ''}`" width="480px" @close="showAllowedAgentsModal = false">
      <p class="cell-meta">僅列出已核准的 Agent。</p>
      <div class="check-list term-box term-box--inset" style="padding: 8px 12px; margin-top: 12px; max-height: 300px; overflow:auto;">
        <label v-for="agent in allAgents" :key="agent.id" class="check-list__row check-list__row--col">
          <span style="display:flex; align-items:center; gap: 8px;">
            <input type="checkbox" :value="agent.id" v-model="selectedAgentIds" />
            <span>{{ agent.name }}</span>
          </span>
          <span class="cell-meta">{{ agent.description_for_router }}</span>
        </label>
        <p v-if="allAgents.length === 0" class="cell-meta">無已核准的 Agent</p>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showAllowedAgentsModal = false" label="取消" />
        <TermButton variant="primary" :disabled="savingAgents" :loading="savingAgents" :label="savingAgents ? '儲存中' : '儲存'" @click="handleSaveAllowedAgents" />
      </template>
    </TermModal>

    <!-- Reset password modal ----------------------------------------- -->
    <TermModal :visible="showResetModal" :title="`重設密碼 · ${resetTarget?.username || ''}`" width="420px" @close="showResetModal = false">
      <TermField label="新密碼">
        <input v-model="resetPassword" type="password" class="term-input" />
      </TermField>
      <template #footer>
        <TermButton variant="ghost" @click="showResetModal = false" label="取消" />
        <TermButton variant="primary" :disabled="!resetPassword" label="重設" @click="handleResetPassword" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { roleLabel } from '../utils/roleLabel'
import { computed, onMounted, ref } from 'vue'
import client from '../api/client'
import { listDepartments } from '../api/departments'
import { departmentOptions, departmentPath, indexById } from '../utils/departmentTree'
import { formatDate } from '../utils/formatDate'
import { listModels } from '../api/models'
import { extractError } from '../api/errors'
import {
  createUser,
  deactivateUser,
  getUserAllowedAgents,
  getUserAllowedModels,
  hardDeleteUser,
  listUsers,
  resetUserPassword,
  updateUser,
  updateUserAllowedAgents,
  updateUserAllowedModels,
  getUserRouterModels,
} from '../api/users'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat, RowActions } from '../components/cli'
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
const showRouterModelsModal = ref(false)
const routerModelsTarget = ref(null)
const routerModels = ref([])
const routerModelsLoading = ref(false)
const SOURCE_LABEL = { all: '全院', department: '部門', group: '群組', user: '個人' }
function sourceLabel(sources) {
  const names = [...new Set(sources || [])].map((s) => SOURCE_LABEL[s] || s)
  return names.length ? names.join('、') : '—'
}
async function openRouterModelsModal(user) {
  routerModelsTarget.value = user
  routerModels.value = []
  routerModelsLoading.value = true
  showRouterModelsModal.value = true
  try {
    const { data } = await getUserRouterModels(user.id)
    routerModels.value = data || []
  } catch (e) {
    feedback.value = { type: 'error', message: extractError(e, '讀取對話模型失敗') }
  } finally {
    routerModelsLoading.value = false
  }
}
const showAllowedAgentsModal = ref(false)
const allowedAgentsTarget = ref(null)
const selectedAgentIds = ref([])
const savingAgents = ref(false)

// 編制到三層之後，光看「企劃組」分不出是哪個所底下的（不同所可以同名），
// 所以選單與列表一律攤開完整祖先路徑，綁在哪一層是看得出來的選擇。
const departmentIndex = computed(() => indexById(departments.value))
const departmentChoices = computed(() => departmentOptions(departments.value))
function departmentLabel(user) {
  if (user.department_id == null) return user.department_name || '—'
  return departmentPath(user.department_id, departmentIndex.value) || user.department_name || '—'
}
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
  catch (e) { setFeedback('error', extractError(e, '載入使用者失敗')) }
  finally { loading.value = false }
}

onMounted(async () => {
  await fetchUsers()
  try { const { data } = await listDepartments(); departments.value = data } catch {}
  try { const { data } = await listModels(); allModels.value = data } catch {}
  try {
    const { data } = await client.get('/api/agents')
    allAgents.value = data.filter(a =>
      a.approval_status === 'approved' || a.approval_status === 'registered'
    )
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
      setFeedback('success', '使用者已更新')
    } else {
      await createUser(form.value)
      setFeedback('success', '使用者已建立')
    }
    showModal.value = false
    await fetchUsers()
  } catch (e) { setFeedback('error', extractError(e, '操作失敗')) }
}

async function openAllowedModelsModal(user) {
  // ⚠ 讀不到現況就**不要開視窗**。這裡曾經是 `try {...} catch {}`,
  // 讀取失敗時 selectedModelIds 停在 [],視窗照常開啟、每個框都沒勾——
  // 跟「這個人本來就沒有權限」在畫面上完全無法分辨。管理員勾一個想加的、按儲存,
  // 後端是**全量取代**(users.py:295 先 delete 再重建),還會連帶把被撤掉的模型
  // 從該使用者所有 API key 上移除(:304)。畫面回報綠色成功。
  // 也就是說:網路抖一下,一次正常的管理操作就靜默撤光一個人的權限。
  allowedModelsTarget.value = user
  selectedModelIds.value = []
  try {
    const { data } = await getUserAllowedModels(user.id)
    selectedModelIds.value = data.map(m => m.id)
  } catch (e) {
    setFeedback('error', '讀不到目前的允許清單,先不開啟編輯視窗以免誤撤權限。請重試。')
    allowedModelsTarget.value = null
    return
  }
  showAllowedModelsModal.value = true
}
async function handleSaveAllowedModels() {
  savingModels.value = true
  try {
    const r = await updateUserAllowedModels(allowedModelsTarget.value.id, selectedModelIds.value)
    showAllowedModelsModal.value = false
    setFeedback('success', r.data?.message || '允許清單已更新')
  } catch (e) {
    setFeedback('error', extractError(e, '更新允許清單失敗'))
  } finally { savingModels.value = false }
}

async function openAllowedAgentsModal(user) {
  // 同 openAllowedModelsModal:讀不到現況就不開視窗。後端同樣是全量取代
  // (users.py:368 先 delete 再重建)。
  allowedAgentsTarget.value = user
  selectedAgentIds.value = []
  try {
    const { data } = await getUserAllowedAgents(user.id)
    selectedAgentIds.value = data.map(a => a.id)
  } catch (e) {
    setFeedback('error', '讀不到目前的 Agent 允許清單,先不開啟編輯視窗以免誤撤權限。請重試。')
    allowedAgentsTarget.value = null
    return
  }
  showAllowedAgentsModal.value = true
}
async function handleSaveAllowedAgents() {
  savingAgents.value = true
  try {
    const r = await updateUserAllowedAgents(allowedAgentsTarget.value.id, selectedAgentIds.value)
    showAllowedAgentsModal.value = false
    setFeedback('success', r.data?.message || 'Agent 允許清單已更新')
  } catch (e) {
    setFeedback('error', extractError(e, '更新 Agent 失敗'))
  } finally { savingAgents.value = false }
}

function openResetPasswordModal(user) { resetTarget.value = user; resetPassword.value = ''; showResetModal.value = true }
async function handleResetPassword() {
  try {
    await resetUserPassword(resetTarget.value.id, { new_password: resetPassword.value })
    showResetModal.value = false
    setFeedback('success', `已重設「${resetTarget.value.username}」的密碼`)
  } catch (e) {
    setFeedback('error', extractError(e, '重設失敗'))
  }
}

async function handleApprove(user) {
  try {
    await client.post(`/api/users/${user.id}/approve`)
    setFeedback('success', `已核准「${user.username}」`)
    await fetchUsers()
  } catch (e) { setFeedback('error', extractError(e, '核准失敗')) }
}
async function handleDeactivate(user) {
  if (!window.confirm(`停用「${user.username}」？`)) return
  try {
    await deactivateUser(user.id)
    setFeedback('success', `已停用「${user.username}」`)
    await fetchUsers()
  } catch (e) { setFeedback('error', extractError(e, '停用失敗')) }
}

// branch SSO: 重新啟用之前被 deactivate 的使用者。
// Backend 沒有專屬 /activate endpoint — 直接 PUT 把 is_active 設 true 就好。
// 注意：仍維持 is_approved 原狀。若使用者在 pending_approval 狀態下被 deactivate，
// 啟用後仍然不能登入，要再 approve；UI 已用 approve 按鈕區隔兩種狀態。
async function handleActivate(user) {
  if (!window.confirm(`重新啟用「${user.username}」？`)) return
  try {
    await updateUser(user.id, { is_active: true })
    setFeedback('success', `已啟用「${user.username}」`)
    await fetchUsers()
  } catch (e) {
    setFeedback('error', extractError(e, '啟用失敗'))
  }
}

// branch SSO: permanent (hard) delete with typed-confirmation modal — irreversible
const showHardDeleteModal = ref(false)
const hardDeleteTarget = ref(null)
const hardDeleteConfirm = ref('')
const hardDeleting = ref(false)
const hardDeleteError = ref('')

const hardDeleteConfirmed = computed(
  () => !!hardDeleteTarget.value
    && hardDeleteConfirm.value.trim() === hardDeleteTarget.value.username,
)

function openHardDeleteModal(user) {
  hardDeleteTarget.value = user
  hardDeleteConfirm.value = ''
  hardDeleteError.value = ''
  showHardDeleteModal.value = true
}

function closeHardDeleteModal() {
  showHardDeleteModal.value = false
  hardDeleteTarget.value = null
  hardDeleteConfirm.value = ''
  hardDeleteError.value = ''
}

async function handleHardDelete() {
  if (!hardDeleteConfirmed.value) return
  hardDeleting.value = true
  hardDeleteError.value = ''
  try {
    const { data } = await hardDeleteUser(hardDeleteTarget.value.id)
    setFeedback(
      'success',
      `已永久刪除「${hardDeleteTarget.value.username}」` +
        (data?.snapshot?.api_keys ? `（+ ${data.snapshot.api_keys} 組 API 金鑰）` : ''),
    )
    closeHardDeleteModal()
    await fetchUsers()
  } catch (e) {
    hardDeleteError.value = extractError(e, '永久刪除失敗')
  } finally {
    hardDeleting.value = false
  }
}
async function handleToggleSsoOnly(user, disable) {
  const action = disable ? '切換為僅 SSO' : '解鎖本地密碼'
  if (!window.confirm(`對「${user.username}」${action}？`)) return
  try {
    await updateUser(user.id, { local_password_disabled: disable })
    setFeedback('success', disable ? `「${user.username}」現在僅限 SSO` : `已為「${user.username}」解鎖本地密碼`)
    await fetchUsers()
  } catch (e) { setFeedback('error', extractError(e, `${action}失敗`)) }
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
  if (!targets.length) { setFeedback('error', '所選中沒有待審核的使用者'); return }
  for (const u of targets) await client.post(`/api/users/${u.id}/approve`)
  selectedUserIds.value = []
  setFeedback('success', `已核准 ${targets.length} 位使用者`)
  await fetchUsers()
}
async function handleBulkDeactivate() {
  const targets = users.value.filter(u => selectedUserIds.value.includes(u.id) && u.is_active && u.is_approved)
  if (!targets.length) { setFeedback('error', '所選中沒有使用中的使用者'); return }
  if (!window.confirm(`停用 ${targets.length} 位使用者？`)) return
  for (const u of targets) await deactivateUser(u.id)
  selectedUserIds.value = []
  setFeedback('success', `已停用 ${targets.length} 位使用者`)
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
  if (role === 'owner') return 'owner 是平台營運者 — 獨占掌控認證提供者、hard-purge、原始稽核欄位，以及 admin/owner 角色管理。'
  if (role === 'admin') return 'admin 可管理使用者、模型、稽核、計費與用量。無法建立/降級 admin 或變更平台層級設定。'
  if (role === 'developer') return 'developer 可註冊 Agent 並下載樣板。'
  return ''
}
function statusVariant(u) {
  if (!u.is_approved) return 'warn'
  return u.is_active ? 'ok' : 'danger'
}
function statusLabel(u) {
  if (!u.is_approved) return '待審核'
  return u.is_active ? '使用中' : '已停用'
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
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
