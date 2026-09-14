<template>
  <div class="page">
    <PageHead title="平台連結" subtitle="登記給同仁使用的服務入口，可設角色門檻與授權名單。">
      <template #actions>
        <TermButton variant="primary" @click="openCreateModal" label="新增服務" />
      </template>
    </PageHead>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox :title="`服務 · ${links.length}`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>網址</th>
            <th style="width: 22%">註冊資訊</th>
            <th style="width: 90px">狀態</th>
            <th style="width: 14%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="link in links" :key="link.id">
            <td>
              <div class="cell-strong">{{ link.name }}</div>
              <div class="cell-meta">{{ link.description || '無描述' }}</div>
            </td>
            <td><code class="cell-url" :title="serviceUrl(link)">{{ serviceUrl(link) }}</code></td>
            <td>
              <div class="reg-tags">
                <TermBadge variant="accent">{{ launchModeLabel(link.launch_mode) }}</TermBadge>
                <TermBadge :variant="configSourceBadge(link).variant" :title="configSourceBadge(link).locked ? '由部署環境播種：多數欄位唯讀' : 'UI 為唯一事實來源'">
                  {{ configSourceBadge(link).label }}
                </TermBadge>
                <TermBadge v-if="link.classification_ceiling" variant="danger" dot>{{ link.classification_ceiling }}</TermBadge>
                <span v-if="(link.service_admin_user_ids || []).length" class="reg-admins" title="服務管理員（委派）">
                  管理員 {{ link.service_admin_user_ids.length }}
                </span>
              </div>
            </td>
            <td>
              <TermBadge :variant="link.is_active ? 'ok' : 'danger'" dot>{{ link.is_active ? '啟用' : '停用' }}</TermBadge>
              <!-- 閘門關著的服務**留在表上**（拿掉整列 = 管理員管不動它），只標出來。 -->
              <TermBadge v-if="isReleaseGateClosedFor(link)" variant="danger" :title="RELEASE_GATE_HINT">
                {{ RELEASE_GATE_BADGE }}
              </TermBadge>
            </td>
            <td>
              <div class="row-actions">
                <button class="term-action" @click="openEditModal(link)">編輯</button>
                <span class="row-actions__sep">·</span>
                <button v-if="link.is_active" class="term-action term-action--danger" @click="handleDeactivate(link)">停用</button>
                <button v-else class="term-action" @click="handleReactivate(link)">啟用</button>
                <span class="row-actions__sep">·</span>
                <button class="term-action term-action--danger" @click="handlePurge(link)" title="完全刪除,不可復原">刪除</button>
              </div>
            </td>
          </tr>
          <tr v-if="links.length === 0">
            <td colspan="5"><TermEmpty message="尚無註冊服務 · 新增一個以在儀表板顯示外部工具入口" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? '編輯 · 服務' : '新增 · 服務'" width="600px" @close="showModal = false">
      <div class="form-grid">
        <div v-if="editingLocked" class="feedback is-lock">
          🔒 此服務由部署環境（環境種子）播種。多數欄位唯讀，僅 {{ stickyEditableFields(editingService).join(' · ') }} 可由管理員覆寫。
        </div>

        <TermField label="名稱">
          <input v-model="form.name" class="term-input" :disabled="locked('name')" />
        </TermField>
        <TermField label="網址">
          <input v-model="form.url" class="term-input" placeholder="https://…" :disabled="locked('entry_url')" />
        </TermField>
        <div class="form-row-2">
          <TermField label="圖示" hint="清單來自伺服器 GET /api/platform-links/icons">
            <select v-model="form.icon" class="term-select" :disabled="locked('icon')">
              <option value="">— 無圖示 —</option>
              <option v-for="ic in iconOptions" :key="ic" :value="ic">{{ ic }}</option>
            </select>
          </TermField>
          <TermField label="排序">
            <input v-model.number="form.sort_order" type="number" class="term-input" :disabled="locked('sort_order')" />
          </TermField>
        </div>
        <TermField label="描述" optional>
          <textarea v-model="form.description" rows="2" class="term-textarea" :disabled="locked('description')" />
        </TermField>

        <TermSection v-if="registryMode" title="啟動與整合" />
        <div v-if="registryMode" class="form-row-2">
          <TermField label="啟動模式" hint="新分頁：另開視窗 · 內嵌 iframe：站內覆蓋層開啟">
            <select v-model="form.launch_mode" class="term-input" :disabled="locked('launch_mode')">
              <option v-for="m in LAUNCH_MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
            </select>
          </TermField>
          <TermField label="分類上限" hint="此服務可承載的最高機敏等級">
            <select v-model="form.classification_ceiling" class="term-input" :disabled="locked('classification_ceiling')">
              <option value="">未設定</option>
              <option v-for="c in CLASSIFICATION_LEVELS" :key="c" :value="c">{{ c }}</option>
            </select>
          </TermField>
        </div>

        <TermSection title="存取控制" />

        <label class="form-toggle">
          <input v-model="form.is_public" type="checkbox" :disabled="locked('is_public')" />
          <span>
            <span class="form-toggle__title">公開</span>
            <span class="form-toggle__hint">任何通過角色閘門的使用者皆可見 · 不需個別授權</span>
          </span>
        </label>

        <TermField label="必要角色" hint="留空 = 開放閘門 · admin 一律通過">
          <div class="role-grid">
            <label
              v-for="role in availableRoles"
              :key="role"
              class="role-chip"
              :class="{ 'is-on': form.required_roles.includes(role) }"
            >
              <input
                type="checkbox"
                :value="role"
                :checked="form.required_roles.includes(role)"
                :disabled="locked('required_roles')"
                @change="toggleRole(role)"
              />
              <span>{{ role }}</span>
            </label>
          </div>
        </TermField>

        <TermField v-if="registryMode" label="服務管理員" hint="委派給指定使用者管理此服務（授權下放，非排他；平台 admin/owner 仍可管理）">
          <div class="admin-chips" v-if="adminChips.length">
            <span v-for="u in adminChips" :key="u.id" class="admin-chip">
              {{ u.username }}
              <button type="button" class="admin-chip__x" :disabled="locked('service_admin_user_ids')" @click="toggleAdmin(u.id)" aria-label="移除">×</button>
            </span>
          </div>
          <input
            v-model="adminFilter"
            class="term-input"
            placeholder="搜尋使用者 · 帳號 / email"
            :disabled="locked('service_admin_user_ids')"
            style="margin-top: var(--gap-2);"
          />
          <div class="picker term-box term-box--inset" style="margin-top: var(--gap-2);">
            <button
              v-for="u in filteredUsers"
              :key="u.id"
              type="button"
              class="picker__row"
              :class="{ 'is-on': isAdminSelected(u.id) }"
              :disabled="locked('service_admin_user_ids')"
              @click="toggleAdmin(u.id)"
            >
              <span class="picker__main">
                <span class="cell-strong">{{ u.username }}</span>
                <span class="cell-meta">{{ u.role }}{{ u.department_name ? ` · ${u.department_name}` : '' }}</span>
              </span>
              <span v-if="isAdminSelected(u.id)" class="picker__check">✓</span>
              <span v-else-if="u.email" class="cell-meta">{{ u.email }}</span>
            </button>
            <TermEmpty v-if="filteredUsers.length === 0" :message="users.length === 0 ? '無使用者資料' : '無符合的使用者'" />
          </div>
        </TermField>

        <template v-if="editingId && registryMode && auditSupported">
          <TermSection title="稽核回呼" />
          <table v-if="auditCallbacks.length" class="term-table">
            <thead>
              <tr>
                <th style="width: 30%">事件</th>
                <th>操作者</th>
                <th style="width: 30%">時間</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(cb, i) in auditCallbacks" :key="cb.id ?? i">
                <td class="cell-strong">{{ cb.event_type || '—' }}</td>
                <td class="cell-meta">{{ cb.actor?.employee_id || cb.employee_id || '—' }}</td>
                <td class="cell-meta tnum">{{ formatDate(cb.timestamp || cb.created_at) }}</td>
              </tr>
            </tbody>
          </table>
          <TermEmpty v-else message="尚無稽核回呼紀錄" />
        </template>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="取消" />
        <TermButton variant="primary" :disabled="!form.name.trim() || !form.url.trim()" :label="editingId ? '更新' : '建立'" @click="handleSubmit" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { extractError } from '../api/errors'
import {
  listPlatformLinks, createPlatformLink, updatePlatformLink,
  deactivatePlatformLink, purgePlatformLink, listPlatformLinkIcons,
} from '../api/platformLinks'
import {
  listServices, createService, updateService,
  deactivateService, purgeService, listServiceAuditCallbacks,
} from '../api/services'
import { listUsers } from '../api/users'
import {
  LAUNCH_MODES, CLASSIFICATION_LEVELS, launchModeLabel, configSourceBadge,
  stickyEditableFields, isFieldLocked, normalizeService,
} from '../utils/serviceRegistry'
import {
  isReleaseGateClosedFor, RELEASE_GATE_BADGE, RELEASE_GATE_HINT,
} from '../utils/anilalmReleaseGate'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermSection, PageHead } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import { formatDate } from '../utils/formatDate'

const { confirm, toast } = useDialog()
const links = ref([])
const users = ref([])
const showModal = ref(false)
const editingId = ref(null)
const editingService = ref(null)
const registryMode = ref(true)
const pageError = ref('')

// 稽核回呼 feature-check：端點未實作（404）時整段隱藏。
const auditSupported = ref(false)
const auditCallbacks = ref([])

const adminFilter = ref('')

function emptyForm() {
  return {
    name: '', url: '', icon: '', description: '', sort_order: 0,
    is_public: false, required_roles: [],
    launch_mode: 'new_tab', classification_ceiling: '',
    service_admin_user_ids: [],
  }
}
const form = ref(emptyForm())
const icons = ref([])

const iconOptions = computed(() => {
  const list = [...icons.value]
  const current = form.value.icon
  if (current && !list.includes(current)) list.push(current)
  return list
})

const availableRoles = ['admin', 'developer', 'user']

function serviceUrl(link) {
  return link.url || link.entry_url || ''
}

// env_seeded 服務的欄位鎖定：僅在編輯既有 env_seeded 列時生效。
const editingLocked = computed(() =>
  !!editingService.value && configSourceBadgeLocked(editingService.value),
)
function configSourceBadgeLocked(svc) {
  return configSourceBadge(svc).locked
}
function locked(field) {
  if (!editingId.value || !editingService.value) return false
  return isFieldLocked(editingService.value, field)
}

const userById = computed(() => {
  const m = new Map()
  for (const u of users.value) m.set(u.id, u)
  return m
})
const adminChips = computed(() =>
  (form.value.service_admin_user_ids || [])
    .map((id) => userById.value.get(id) || { id, username: `user#${id}`, role: '' }),
)
const filteredUsers = computed(() => {
  const q = adminFilter.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) => u.username.toLowerCase().includes(q) || (u.email || '').toLowerCase().includes(q),
  )
})
function isAdminSelected(id) {
  return (form.value.service_admin_user_ids || []).includes(id)
}
function toggleAdmin(id) {
  const list = form.value.service_admin_user_ids
  const idx = list.indexOf(id)
  if (idx >= 0) list.splice(idx, 1)
  else list.push(id)
}

function toggleRole(role) {
  const idx = form.value.required_roles.indexOf(role)
  if (idx >= 0) form.value.required_roles.splice(idx, 1)
  else form.value.required_roles.push(role)
}

async function fetchLinks() {
  pageError.value = ''
  try {
    // 優先走 registered_services；7a 尚未上線時退回 legacy platform_links。
    let data
    try {
      const res = await listServices({ include_inactive: true })
      data = res.data
      registryMode.value = true
    } catch (e) {
      if (e.response?.status === 404) {
        const res = await listPlatformLinks({ include_inactive: true })
        data = res.data
        registryMode.value = false
      } else {
        throw e
      }
    }
    // ANILA LM release gate：這裡是**管理面**，關著的服務照列、標「未開放」。
    // 後端已經把它從使用者面清單濾掉了（app/services/anilalm_release_gate.py），
    // 這一頁用 include_inactive=true 取的正是管理清單，不再重複前端過濾——
    // 前端過濾會連編輯／停用／刪除按鈕一起拿走。
    links.value = (Array.isArray(data) ? data : (data?.services || data?.data || []))
      .map(normalizeService)
  } catch (e) {
    pageError.value = extractError(e, '載入服務清單失敗')
  }
}

async function fetchUsers() {
  try {
    const { data } = await listUsers()
    users.value = Array.isArray(data) ? data : []
  } catch {
    users.value = [] // best-effort：picker 無資料時顯示空狀態，不阻斷主流程。
  }
}

async function fetchIcons() {
  try {
    const { data } = await listPlatformLinkIcons()
    if (Array.isArray(data)) {
      icons.value = data
    } else if (data && typeof data === 'object' && Array.isArray(data.icons)) {
      icons.value = data.icons
    } else {
      icons.value = []
    }
  } catch {
    icons.value = []
  }
}

onMounted(() => {
  fetchLinks()
  fetchUsers()
  fetchIcons()
})

function openCreateModal() {
  editingId.value = null
  editingService.value = null
  auditSupported.value = false
  auditCallbacks.value = []
  adminFilter.value = ''
  form.value = emptyForm()
  showModal.value = true
}

function openEditModal(link) {
  editingId.value = link.id
  editingService.value = link
  adminFilter.value = ''
  form.value = {
    name: link.name,
    url: serviceUrl(link),
    icon: link.icon || '',
    description: link.description || '',
    sort_order: link.sort_order || 0,
    is_public: !!link.is_public,
    required_roles: Array.isArray(link.required_roles) ? [...link.required_roles] : [],
    launch_mode: link.launch_mode === 'iframe' ? 'iframe' : 'new_tab',
    classification_ceiling: link.classification_ceiling || '',
    service_admin_user_ids: Array.isArray(link.service_admin_user_ids) ? [...link.service_admin_user_ids] : [],
  }
  showModal.value = true
  if (registryMode.value) loadAuditCallbacks(link.id)
}

async function loadAuditCallbacks(id) {
  auditSupported.value = false
  auditCallbacks.value = []
  try {
    const { data } = await listServiceAuditCallbacks(id, { limit: 10 })
    auditCallbacks.value = Array.isArray(data) ? data : (data?.items || data?.callbacks || [])
    auditSupported.value = true
  } catch {
    // 404（端點未實作）或其他錯誤 → 整段隱藏，不干擾服務編輯。
    auditSupported.value = false
  }
}

function buildPayload() {
  // registry 契約用 entry_url；legacy platform_links 相容面用 url。
  // 混用會讓 Pydantic 默默丟掉網址（更新）或 422（建立）。
  const address = form.value.url.trim()
  const base = {
    name: form.value.name.trim(),
    icon: form.value.icon.trim() || null,
    description: form.value.description.trim() || null,
    sort_order: form.value.sort_order || 0,
    is_public: !!form.value.is_public,
    required_roles: form.value.required_roles,
  }
  if (!registryMode.value) return { ...base, url: address }
  return {
    ...base,
    entry_url: address,
    launch_mode: form.value.launch_mode,
    classification_ceiling: form.value.classification_ceiling || null,
    service_admin_user_ids: form.value.service_admin_user_ids,
  }
}

async function handleSubmit() {
  const payload = buildPayload()
  try {
    if (registryMode.value) {
      if (editingId.value) await updateService(editingId.value, payload)
      else await createService(payload)
    } else {
      if (editingId.value) await updatePlatformLink(editingId.value, payload)
      else await createPlatformLink(payload)
    }
    showModal.value = false
    await fetchLinks()
  } catch (e) {
    toast(extractError(e, '儲存失敗'), { tone: 'error' })
  }
}

async function handleDeactivate(link) {
  if (!(await confirm({ message: `停用「${link.name}」?`, confirmText: '停用', danger: true }))) return
  try {
    await (registryMode.value ? deactivateService(link.id) : deactivatePlatformLink(link.id))
    await fetchLinks()
  } catch (e) {
    toast(extractError(e, '停用失敗'), { tone: 'error' })
  }
}

async function handleReactivate(link) {
  try {
    await (registryMode.value
      ? updateService(link.id, { is_active: true })
      : updatePlatformLink(link.id, { is_active: true }))
    await fetchLinks()
  } catch (e) {
    toast(extractError(e, '啟用失敗'), { tone: 'error' })
  }
}

async function handlePurge(link) {
  // Typed-confirm: 必須輸入完整服務名稱才能刪除,避免誤點。
  if (!(await confirm({
    title: '完全刪除服務',
    message: `完全刪除服務「${link.name}」?此動作不可復原。`,
    requireText: link.name,
    confirmText: '刪除',
    danger: true,
  }))) return
  try {
    await (registryMode.value ? purgeService(link.id) : purgePlatformLink(link.id))
    await fetchLinks()
  } catch (e) {
    toast(extractError(e, '刪除失敗'), { tone: 'error' })
  }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.compat-note { color: var(--c-warn); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.feedback.is-lock { color: var(--c-warn); border-color: var(--c-warn); background: var(--c-warn-soft, transparent); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-url {
  display: inline-block;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
}
.reg-tags { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.reg-admins {
  font-size: var(--t-2xs);
  font-family: var(--font-mono);
  color: var(--c-fg-3);
  border: var(--border-w) solid var(--c-border-strong);
  padding: 0 6px;
  height: 18px;
  display: inline-flex;
  align-items: center;
}
.row-actions { display: inline-flex; gap: 6px; align-items: center; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
.form-toggle {
  display: flex;
  gap: var(--gap-3);
  align-items: flex-start;
  cursor: pointer;
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-border);
  background: var(--c-bg);
  font-size: var(--t-sm);
}
.form-toggle input { margin-top: 2px; accent-color: var(--c-accent); }
.form-toggle__title { display: block; color: var(--c-fg-1); font-weight: 500; }
.form-toggle__hint { display: block; color: var(--c-fg-3); font-size: var(--t-2xs); margin-top: 2px; }

.role-grid { display: flex; flex-wrap: wrap; gap: var(--gap-2); }
.role-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: var(--border-w) solid var(--c-border-strong);
  background: var(--c-bg);
  color: var(--c-fg-2);
  cursor: pointer;
  font-size: var(--t-sm);
}
.role-chip.is-on {
  border-color: var(--c-accent);
  color: var(--c-accent);
  background: var(--c-accent-soft);
}
.role-chip input { accent-color: var(--c-accent); }

.admin-chips { display: flex; flex-wrap: wrap; gap: var(--gap-2); }
.admin-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px 2px 8px;
  border: var(--border-w) solid var(--c-accent);
  color: var(--c-accent);
  background: var(--c-accent-soft);
  font-size: var(--t-2xs);
  font-family: var(--font-mono);
}
.admin-chip__x {
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  font-size: var(--t-sm);
  line-height: 1;
  padding: 0 2px;
}
.picker { max-height: 220px; overflow-y: auto; padding: 0; }
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
.picker__row:disabled { opacity: 0.5; cursor: not-allowed; }
.picker__main { display: flex; flex-direction: column; gap: 2px; }
.picker__check { color: var(--c-accent); font-weight: 600; }

.term-input:disabled,
.term-textarea:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
