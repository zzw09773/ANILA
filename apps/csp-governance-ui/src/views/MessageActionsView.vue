<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">自訂動作</h1>
        <p class="page-subtitle">
          訊息列自訂按鈕的撰寫主控台（OW-3）。建立＝開發者以上；更新／刪除／綁定替換＝作者本人或管理員以上；稽核匯出＝管理員以上（非擁有者匯出會遮罩位址與 metadata）。
          畫面閘門只是便利，真正邊界在伺服器。零綁定＝無人可見（fail-closed）。動作本體為提示詞模板。
        </p>
      </div>
    </header>

    <div
      v-if="feedback.message"
      class="feedback"
      :class="feedback.type === 'ok' ? 'is-ok' : 'is-err'"
    >
      <span>{{ feedback.type === 'ok' ? '✓' : '!' }} {{ feedback.message }}</span>
      <button type="button" class="term-action" @click="feedback.message = ''">關閉</button>
    </div>

    <div class="row-actions" style="margin: 12px 0;">
      <TermButton variant="primary" @click="openCreateModal" label="+ 新增動作" />
      <span class="row-actions__sep">·</span>
      <template v-if="authStore.isAdmin">
        <TermButton
          variant="ghost"
          :disabled="exporting"
          :label="exporting ? '匯出中…' : '匯出稽核 NDJSON'"
          @click="openExportModal"
        />
        <span class="row-actions__sep">·</span>
      </template>
      <button class="term-action" @click="fetchActions">重新整理</button>
    </div>

    <TermBox>
      <table class="data-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>標籤</th>
            <th>圖示</th>
            <th>狀態</th>
            <th>版本 · checksum</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!actions.length">
            <td colspan="6">
              <TermEmpty message="尚無自訂動作 — 開發者以上可在此建立提示詞模板動作" />
            </td>
          </tr>
          <tr v-for="a in actions" :key="a.id">
            <td>
              <div class="cell-strong">{{ a.name }}</div>
              <div class="cell-meta">#{{ a.id }}</div>
            </td>
            <td>{{ a.label }}</td>
            <td><code class="icon-cell">{{ a.icon }}</code></td>
            <td>
              <TermBadge :variant="a.is_enabled ? 'ok' : ''" dot>
                {{ a.is_enabled ? '啟用' : '停用' }}
              </TermBadge>
            </td>
            <td class="tnum">
              <div>v{{ a.version }}</div>
              <code
                v-if="a.body_sha256"
                class="checksum"
                :title="a.body_sha256"
                @click="revealChecksum(a.body_sha256)"
              >{{ shortChecksum(a.body_sha256) }}</code>
            </td>
            <td>
              <div class="row-actions">
                <template v-if="canMutateAction(a)">
                  <button class="term-action" @click="openEditModal(a)">編輯</button>
                  <span class="row-actions__sep">·</span>
                  <button class="term-action" @click="openBindingsModal(a)">綁定</button>
                  <span class="row-actions__sep">·</span>
                  <button
                    class="term-action term-action--danger"
                    :disabled="busyId === a.id"
                    @click="handleDelete(a)"
                  >
                    {{ busyId === a.id ? '…' : '刪除' }}
                  </button>
                </template>
                <span v-else class="cell-meta">僅檢視</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- ── Editor modal ─────────────────────────────────────────────── -->
    <TermModal
      :visible="showEditor"
      :title="editingId ? `編輯 · ${form.name || '動作'}` : '新增 · 自訂動作'"
      width="720px"
      @close="showEditor = false"
    >
      <div class="form-grid">
        <div class="form-row-2">
          <TermField label="名稱" hint="唯一識別字，供稽核與 agent_name 使用">
            <input v-model="form.name" class="term-input" placeholder="translate_en" maxlength="100" />
          </TermField>
          <TermField label="標籤" hint="按鈕上顯示的文字">
            <input v-model="form.label" class="term-input" placeholder="翻譯成英文" maxlength="120" />
          </TermField>
        </div>

        <TermField label="圖示" hint="清單來自伺服器 GET /api/message-actions/icons">
          <select v-model="form.icon" class="term-select">
            <option disabled value="">— 選擇圖示 —</option>
            <option v-for="ic in icons" :key="ic" :value="ic">{{ ic }}</option>
          </select>
        </TermField>

        <TermField label="提示詞模板（body）" :hint="bodyHint">
          <textarea
            v-model="form.body"
            class="term-textarea term-textarea--code"
            rows="8"
            :placeholder="bodyPlaceholder"
            :maxlength="maxBodyChars"
          />
          <div class="body-count" :class="{ 'is-over': form.body.length > maxBodyChars }">
            {{ form.body.length }} / {{ maxBodyChars }}
          </div>
        </TermField>

        <TermField label="選項（choices）" :hint="`最多 ${MAX_CHOICES} 項；id 須符合 ^[a-z0-9_-]{1,40}$`">
          <div class="choices">
            <div v-for="(c, idx) in form.choices" :key="idx" class="choice-row">
              <div class="form-row-2">
                <TermField label="id">
                  <input v-model="c.id" class="term-input" maxlength="40" placeholder="en" />
                </TermField>
                <TermField label="標籤">
                  <input v-model="c.label" class="term-input" maxlength="60" placeholder="英文" />
                </TermField>
              </div>
              <TermField label="prompt">
                <input v-model="c.prompt" class="term-input" maxlength="4000" placeholder="翻譯成英文" />
              </TermField>
              <div class="form-row-2">
                <label class="form-toggle">
                  <input v-model="c.input" type="checkbox" />
                  <span>需要自由輸入</span>
                </label>
                <TermField v-if="c.input" label="輸入標籤">
                  <input v-model="c.input_label" class="term-input" maxlength="40" placeholder="補充說明" />
                </TermField>
              </div>
              <button type="button" class="term-action term-action--danger" @click="removeChoice(idx)">
                移除此選項
              </button>
            </div>
            <TermButton
              size="xs"
              :disabled="form.choices.length >= MAX_CHOICES"
              :label="form.choices.length >= MAX_CHOICES ? '已達上限（20）' : '+ 新增選項'"
              @click="addChoice"
            />
          </div>
        </TermField>

        <TermField label="備註" optional>
          <textarea v-model="form.notes" class="term-textarea" rows="2" placeholder="用途說明（選填）" />
        </TermField>

        <label class="form-toggle">
          <input v-model="form.is_enabled" type="checkbox" />
          <span>
            <span class="form-toggle__title">啟用</span>
            <span class="form-toggle__hint">停用後自 /visible 消失，invoke 回 404</span>
          </span>
        </label>

        <div v-if="editorError" class="feedback is-err">! {{ editorError }}</div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showEditor = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="!canSubmitEditor || submitting"
          :label="submitting ? '儲存中…' : (editingId ? '儲存' : '建立')"
          @click="handleSubmitEditor"
        />
      </template>
    </TermModal>

    <!-- ── Bindings modal（整組替換）─────────────────────────────────── -->
    <TermModal
      :visible="showBindings"
      :title="`綁定 · ${bindingsTarget?.label || ''}`"
      width="640px"
      @close="showBindings = false"
    >
      <p class="bindings-notice">
        儲存會<strong>整組替換</strong>此動作的全部綁定（PUT whole-set），不是單筆增刪。
        零綁定＝無人可見。部門綁定涵蓋該節點子樹。
      </p>

      <div v-if="bindingsLoading" class="cell-meta" style="margin-bottom: 10px;">
        載入綁定中…
      </div>
      <div v-else-if="bindingsLoadFailed" class="feedback is-err" style="margin-bottom: 10px;">
        ! {{ bindingsError || '載入綁定失敗' }} — 整組替換已停用，請關閉後重試。
      </div>

      <div class="bindings-list">
        <div v-for="(b, idx) in draftBindings" :key="idx" class="binding-row">
          <TermField label="範圍類型">
            <select
              v-model="b.scope_type"
              class="term-select"
              :disabled="!bindingsLoaded"
              @change="onScopeTypeChange(b)"
            >
              <option value="role">角色</option>
              <option value="department">部門（含子樹）</option>
              <option value="user">特定使用者</option>
            </select>
          </TermField>

          <TermField v-if="b.scope_type === 'role'" label="角色">
            <select v-model="b.role" class="term-select" :disabled="!bindingsLoaded">
              <option value="user">一般使用者</option>
              <option value="developer">開發者</option>
              <option value="admin">管理員</option>
              <option value="owner">擁有者</option>
            </select>
          </TermField>

          <TermField v-else-if="b.scope_type === 'department'" label="部門">
            <div class="picker term-box term-box--inset">
              <button
                v-for="d in deptTreeOptions"
                :key="d.id"
                type="button"
                class="picker__row"
                :class="{ 'is-on': b.department_id === d.id }"
                :style="{ paddingLeft: `${12 + d.depth * 14}px` }"
                :disabled="!bindingsLoaded || !d.is_active"
                @click="b.department_id = d.id"
              >
                <span class="cell-strong">{{ d.name }}</span>
                <span v-if="!d.is_active" class="cell-meta">停用（不可選）</span>
              </button>
              <TermEmpty v-if="deptTreeOptions.length === 0" message="尚無部門 — 請先建立" />
            </div>
          </TermField>

          <TermField v-else label="使用者">
            <input
              v-model="userFilter"
              class="term-input"
              placeholder="搜尋使用者名稱 · email"
              style="margin-bottom: 6px;"
              :disabled="!bindingsLoaded"
            />
            <div class="picker term-box term-box--inset">
              <button
                v-for="u in filteredUsers"
                :key="u.id"
                type="button"
                class="picker__row"
                :class="{ 'is-on': b.user_id === u.id }"
                :disabled="!bindingsLoaded"
                @click="b.user_id = u.id"
              >
                <span class="picker__main">
                  <span class="cell-strong">{{ u.username }}</span>
                  <span class="cell-meta">{{ roleLabel(u.role) }}{{ u.department_name ? ` · ${u.department_name}` : '' }}</span>
                </span>
              </button>
              <TermEmpty v-if="filteredUsers.length === 0" message="無符合的使用者" />
            </div>
          </TermField>

          <button
            type="button"
            class="term-action term-action--danger"
            :disabled="!bindingsLoaded"
            @click="draftBindings.splice(idx, 1)"
          >
            移除此綁定
          </button>
        </div>
      </div>

      <div class="row-actions" style="margin: 10px 0;">
        <TermButton
          size="xs"
          label="+ 新增綁定"
          :disabled="!bindingsLoaded"
          @click="addBinding"
        />
      </div>

      <div v-if="bindingsError && bindingsLoaded" class="feedback is-err">! {{ bindingsError }}</div>

      <template #footer>
        <TermButton variant="ghost" @click="showBindings = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="bindingsSubmitting || !bindingsLoaded"
          :label="bindingsSubmitting ? '替換中…' : '整組替換綁定'"
          @click="handleReplaceBindings"
        />
      </template>
    </TermModal>

    <!-- ── Export modal ──────────────────────────────────────────────── -->
    <TermModal
      :visible="showExport"
      title="匯出稽核 NDJSON"
      width="480px"
      @close="showExport = false"
    >
      <div class="form-grid">
        <p class="bindings-notice">
          匯出訊息動作稽核為 NDJSON（含完整 body 快照）。此操作本身會寫入稽核列。
          伺服器依時間升序後套用筆數上限；請縮小區間以免截斷最近活動。
        </p>
        <div class="form-row-2">
          <TermField label="起始（since · UTC）" optional>
            <input v-model="exportForm.since" type="datetime-local" class="term-input" />
          </TermField>
          <TermField label="結束（until · UTC）" optional>
            <input v-model="exportForm.until" type="datetime-local" class="term-input" />
          </TermField>
        </div>
        <TermField label="筆數上限（limit）" hint="1–50000；預設 5000">
          <input
            v-model.number="exportForm.limit"
            type="number"
            class="term-input"
            min="1"
            max="50000"
          />
        </TermField>
        <div v-if="exportError" class="feedback is-err">! {{ exportError }}</div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showExport = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="exporting || !exportLimitValid"
          :label="exporting ? '匯出中…' : '確認匯出'"
          @click="handleExport"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  listMessageActions,
  listActionIcons,
  createMessageAction,
  updateMessageAction,
  deleteMessageAction,
  getActionBindings,
  replaceActionBindings,
  exportMessageActionAudit,
} from '../api/messageActions'
import { listDepartments } from '../api/departments'
import { listUsers } from '../api/users'
import {
  TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal,
} from '../components/cli'
import { useDialog } from '../composables/useDialog'
import { useAuthStore } from '../stores/auth'

const MAX_CHOICES = 20
/** Client fallback until GET /icons returns max_body_chars. */
const FALLBACK_MAX_BODY_CHARS = 20000
const CHOICE_ID_RE = /^[a-z0-9_-]{1,40}$/
const ROLE_LABELS = {
  user: '一般使用者',
  developer: '開發者',
  admin: '管理員',
  owner: '擁有者',
}

const { confirm, toast } = useDialog()
const authStore = useAuthStore()

/** Matches server ownership rule: author or admin-tier. */
function canMutateAction(action) {
  if (authStore.isAdmin) return true
  return action?.created_by_user_id === authStore.user?.id
}

const actions = ref([])
const icons = ref([])
const maxBodyChars = ref(FALLBACK_MAX_BODY_CHARS)
const departments = ref([])
const users = ref([])
const busyId = ref(null)
const exporting = ref(false)
const feedback = reactive({ message: '', type: 'info' })

const showEditor = ref(false)
const editingId = ref(null)
const submitting = ref(false)
const editorError = ref('')
const form = reactive(emptyForm())

const showBindings = ref(false)
const bindingsTarget = ref(null)
const draftBindings = ref([])
const bindingsSubmitting = ref(false)
const bindingsError = ref('')
const bindingsLoading = ref(false)
const bindingsLoaded = ref(false)
const bindingsLoadFailed = ref(false)
const bindingsBeforeCount = ref(0)
const bindingsLoadSeq = ref(0)
const userFilter = ref('')

const showExport = ref(false)
const exportError = ref('')
const exportForm = reactive({
  since: '',
  until: '',
  limit: 5000,
})

function emptyForm() {
  return {
    name: '',
    label: '',
    icon: '',
    body: '',
    choices: [],
    notes: '',
    is_enabled: true,
  }
}

function emptyChoice() {
  return { id: '', label: '', prompt: '', input: false, input_label: '' }
}

function emptyBinding() {
  return { scope_type: 'role', role: 'user', department_id: null, user_id: null }
}

function setFeedback(type, message) {
  feedback.type = type
  feedback.message = message
}

function apiDetail(err) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === 'string' ? d : d?.msg || JSON.stringify(d)))
      .join('；')
  }
  if (detail && typeof detail === 'object' && detail.message) return detail.message
  return err?.message || '操作失敗'
}

async function blobApiDetail(err) {
  const data = err?.response?.data
  if (data instanceof Blob) {
    try {
      const text = await data.text()
      const parsed = JSON.parse(text)
      const detail = parsed?.detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) {
        return detail
          .map((d) => (typeof d === 'string' ? d : d?.msg || JSON.stringify(d)))
          .join('；')
      }
      if (detail && typeof detail === 'object' && detail.message) return detail.message
    } catch {
      // fall through to shared extractor
    }
  }
  return apiDetail(err)
}

function roleLabel(role) {
  return ROLE_LABELS[role] || role
}

function shortChecksum(sha) {
  if (!sha) return ''
  return sha.length > 12 ? `${sha.slice(0, 12)}…` : sha
}

async function revealChecksum(sha) {
  if (!sha) return
  try {
    await navigator.clipboard.writeText(sha)
    toast('已複製完整 body_sha256')
  } catch {
    await confirm({
      title: '完整 body_sha256',
      message: sha,
      confirmText: '關閉',
      cancelText: '取消',
    })
  }
}

const bodyHint = computed(() => {
  const limitNote = `上限 ${maxBodyChars.value} 字元（以伺服器設定為準）`
  return `可用佔位符 {content}、{choice}、{input}（純字串替換，非 format）；${limitNote}`
})

const bodyPlaceholder = '請將以下內容翻譯成英文：\n\n{content}'

const canSubmitEditor = computed(() => {
  if (!form.name.trim() || !form.label.trim() || !form.icon) return false
  if (!form.body.trim()) return false
  if (form.body.length > maxBodyChars.value) return false
  return true
})

const exportLimitValid = computed(() => {
  const n = Number(exportForm.limit)
  return Number.isInteger(n) && n >= 1 && n <= 50000
})

const deptTreeOptions = computed(() => buildDeptTreeOptions(departments.value))

const filteredUsers = computed(() => {
  const q = userFilter.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) =>
      u.username.toLowerCase().includes(q)
      || (u.email || '').toLowerCase().includes(q),
  )
})

function buildDeptTreeOptions(depts) {
  const byParent = new Map()
  for (const d of depts) {
    const p = d.parent_id ?? null
    if (!byParent.has(p)) byParent.set(p, [])
    byParent.get(p).push(d)
  }
  const out = []
  const walk = (parentId, depth) => {
    const kids = (byParent.get(parentId) || [])
      .slice()
      .sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh-Hant'))
    for (const d of kids) {
      out.push({ ...d, depth })
      walk(d.id, depth + 1)
    }
  }
  walk(null, 0)
  // Orphans (parent missing from list) — still selectable, flat.
  const seen = new Set(out.map((d) => d.id))
  for (const d of depts) {
    if (!seen.has(d.id)) out.push({ ...d, depth: 0 })
  }
  return out
}

async function fetchActions() {
  try {
    const { data } = await listMessageActions()
    actions.value = Array.isArray(data) ? data : []
  } catch (e) {
    setFeedback('danger', apiDetail(e) || '載入自訂動作失敗')
  }
}

function applyBodyLimitFromPayload(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return
  const limit = data.max_body_chars
  if (typeof limit === 'number' && Number.isFinite(limit) && limit > 0) {
    maxBodyChars.value = Math.floor(limit)
  }
}

async function fetchIcons() {
  try {
    const { data } = await listActionIcons()
    applyBodyLimitFromPayload(data)
    if (Array.isArray(data)) {
      icons.value = data
    } else if (data && typeof data === 'object' && Array.isArray(data.icons)) {
      icons.value = data.icons
    } else {
      icons.value = []
    }
  } catch (e) {
    setFeedback('danger', apiDetail(e) || '載入圖示清單失敗')
  }
}

async function fetchDepartmentsAndUsers() {
  try {
    const [d, u] = await Promise.all([listDepartments(), listUsers()])
    departments.value = d.data || []
    users.value = u.data || []
  } catch {
    // Bindings modal will show empty pickers; list page still usable.
  }
}

function openCreateModal() {
  editingId.value = null
  Object.assign(form, emptyForm())
  if (icons.value.length && !form.icon) form.icon = icons.value[0]
  editorError.value = ''
  showEditor.value = true
}

function openEditModal(action) {
  editingId.value = action.id
  Object.assign(form, {
    name: action.name || '',
    label: action.label || '',
    icon: action.icon || '',
    body: action.body || '',
    choices: Array.isArray(action.choices)
      ? action.choices.map((c) => ({
          id: c.id || '',
          label: c.label || '',
          prompt: c.prompt || '',
          input: !!c.input,
          input_label: c.input_label || '',
        }))
      : [],
    notes: action.notes || '',
    is_enabled: !!action.is_enabled,
  })
  editorError.value = ''
  showEditor.value = true
}

function addChoice() {
  if (form.choices.length >= MAX_CHOICES) return
  form.choices.push(emptyChoice())
}

function removeChoice(idx) {
  form.choices.splice(idx, 1)
}

function validateChoicesClient() {
  if (form.choices.length > MAX_CHOICES) {
    return '選項數量超過上限（20）'
  }
  const seen = new Set()
  for (const c of form.choices) {
    if (!CHOICE_ID_RE.test(c.id || '')) {
      return '選項 id 須符合 ^[a-z0-9_-]{1,40}$'
    }
    if (seen.has(c.id)) return `選項 id 重複：${c.id}`
    seen.add(c.id)
    if (!(c.label || '').trim()) return '選項標籤不可為空'
    if ((c.label || '').length > 60) return '選項標籤過長（上限 60）'
    if ((c.prompt || '').length > 4000) return '選項 prompt 過長（上限 4000）'
    if (c.input_label && c.input_label.length > 40) {
      return '選項輸入標籤過長（上限 40）'
    }
  }
  return null
}

function buildChoicesPayload() {
  return form.choices.map((c) => ({
    id: (c.id || '').trim(),
    label: (c.label || '').trim(),
    prompt: c.prompt || '',
    input: !!c.input,
    input_label: c.input
      ? ((c.input_label || '').trim() || null)
      : null,
  }))
}

async function handleSubmitEditor() {
  editorError.value = ''
  const choiceErr = validateChoicesClient()
  if (choiceErr) {
    editorError.value = choiceErr
    return
  }

  if (form.body.length > maxBodyChars.value) {
    editorError.value = `body 超過上限（${maxBodyChars.value}）`
    return
  }

  const payload = {
    name: form.name.trim(),
    label: form.label.trim(),
    icon: form.icon,
    body: form.body,
    choices: buildChoicesPayload(),
    notes: (form.notes || '').trim() || null,
    is_enabled: !!form.is_enabled,
  }

  if (!payload.body?.trim()) {
    editorError.value = '缺少動作內容（body）'
    return
  }

  submitting.value = true
  try {
    if (editingId.value) {
      await updateMessageAction(editingId.value, payload)
      setFeedback('ok', `已更新「${payload.name}」（版本遞增）`)
    } else {
      await createMessageAction(payload)
      setFeedback('ok', `已建立「${payload.name}」`)
    }
    showEditor.value = false
    await fetchActions()
  } catch (e) {
    editorError.value = apiDetail(e)
  } finally {
    submitting.value = false
  }
}

async function handleDelete(action) {
  const ok = await confirm({
    title: '刪除自訂動作',
    message: `確定刪除「${action.label}」（${action.name}）？綁定會一併級聯刪除，且無法復原。`,
    danger: true,
    requireText: action.name,
  })
  if (!ok) return
  busyId.value = action.id
  try {
    await deleteMessageAction(action.id)
    setFeedback('ok', `已刪除「${action.name}」`)
    await fetchActions()
  } catch (e) {
    setFeedback('danger', apiDetail(e))
  } finally {
    busyId.value = null
  }
}

async function openBindingsModal(action) {
  const seq = ++bindingsLoadSeq.value
  bindingsTarget.value = action
  bindingsError.value = ''
  userFilter.value = ''
  draftBindings.value = []
  bindingsLoaded.value = false
  bindingsLoadFailed.value = false
  bindingsBeforeCount.value = 0
  bindingsLoading.value = true
  showBindings.value = true
  try {
    const { data } = await getActionBindings(action.id)
    if (seq !== bindingsLoadSeq.value) return
    draftBindings.value = (Array.isArray(data) ? data : []).map((b) => ({
      scope_type: b.scope_type,
      role: b.role || 'user',
      department_id: b.department_id ?? null,
      user_id: b.user_id ?? null,
    }))
    bindingsBeforeCount.value = draftBindings.value.length
    bindingsLoaded.value = true
  } catch (e) {
    if (seq !== bindingsLoadSeq.value) return
    bindingsLoadFailed.value = true
    bindingsLoaded.value = false
    bindingsError.value = apiDetail(e)
  } finally {
    if (seq === bindingsLoadSeq.value) {
      bindingsLoading.value = false
    }
  }
}

function addBinding() {
  if (!bindingsLoaded.value) return
  draftBindings.value.push(emptyBinding())
}

function onScopeTypeChange(b) {
  b.role = b.scope_type === 'role' ? (b.role || 'user') : null
  b.department_id = b.scope_type === 'department' ? b.department_id : null
  b.user_id = b.scope_type === 'user' ? b.user_id : null
}

function serializeBindings() {
  return draftBindings.value.map((b) => {
    if (b.scope_type === 'role') {
      return { scope_type: 'role', role: b.role, department_id: null, user_id: null }
    }
    if (b.scope_type === 'department') {
      return {
        scope_type: 'department',
        role: null,
        department_id: b.department_id,
        user_id: null,
      }
    }
    return {
      scope_type: 'user',
      role: null,
      department_id: null,
      user_id: b.user_id,
    }
  })
}

async function handleReplaceBindings() {
  if (!bindingsTarget.value || !bindingsLoaded.value) return
  const specs = serializeBindings()
  for (const b of specs) {
    if (b.scope_type === 'role' && !b.role) {
      bindingsError.value = 'scope_type=role 時必須只填 role'
      return
    }
    if (b.scope_type === 'department' && b.department_id == null) {
      bindingsError.value = 'scope_type=department 時必須只填 department_id'
      return
    }
    if (b.scope_type === 'user' && b.user_id == null) {
      bindingsError.value = 'scope_type=user 時必須只填 user_id'
      return
    }
  }

  const ok = await confirm({
    title: '整組替換綁定',
    message:
      `即將整組替換「${bindingsTarget.value.name}」的可見範圍：`
      + `目前 ${bindingsBeforeCount.value} 筆 → 替換後 ${specs.length} 筆`
      + '（擴大可見範圍等同擴大按壓權限）。確定？',
    danger: true,
    confirmText: '整組替換',
  })
  if (!ok) return

  bindingsSubmitting.value = true
  bindingsError.value = ''
  try {
    await replaceActionBindings(bindingsTarget.value.id, specs)
    setFeedback('ok', `已整組替換「${bindingsTarget.value.name}」的綁定`)
    showBindings.value = false
  } catch (e) {
    bindingsError.value = apiDetail(e)
  } finally {
    bindingsSubmitting.value = false
  }
}

function openExportModal() {
  exportError.value = ''
  showExport.value = true
}

function toExportIso(localValue) {
  if (!localValue) return undefined
  // AuditLog.created_at is stored as naive UTC. datetime-local is a local
  // wall-clock value with no zone — convert to UTC before send so month-start
  // boundaries do not silently omit the first hours for UTC+N operators.
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(localValue)
  if (!m) return undefined
  const d = new Date(
    Number(m[1]),
    Number(m[2]) - 1,
    Number(m[3]),
    Number(m[4]),
    Number(m[5]),
    Number(m[6] || 0),
  )
  if (Number.isNaN(d.getTime())) return undefined
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
    + `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
  )
}

async function handleExport() {
  if (!exportLimitValid.value) {
    exportError.value = 'limit 須為 1–50000 的整數'
    return
  }
  exportError.value = ''
  exporting.value = true
  const requestedLimit = Number(exportForm.limit)
  try {
    const params = { limit: requestedLimit }
    const since = toExportIso(exportForm.since)
    const until = toExportIso(exportForm.until)
    if (since) params.since = since
    if (until) params.until = until

    const { data } = await exportMessageActionAudit(params)
    const text = data instanceof Blob ? await data.text() : String(data ?? '')
    const rowCount = text.split('\n').filter((line) => line.trim()).length
    const blob = new Blob([text], { type: 'application/x-ndjson' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'message-action-audit.ndjson'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)

    let msg = `已下載稽核匯出（${rowCount} 列）`
    if (rowCount === requestedLimit) {
      msg += ` — ⚠ 結果已達上限 ${requestedLimit} 列，可能遭截斷；請縮小 since／until 區間後再匯出。`
    }
    setFeedback(rowCount === requestedLimit ? 'danger' : 'ok', msg)
    showExport.value = false
  } catch (e) {
    const detail = await blobApiDetail(e)
    exportError.value = detail || '匯出失敗'
    setFeedback('danger', detail || '匯出失敗')
  } finally {
    exporting.value = false
  }
}

onMounted(async () => {
  await Promise.all([fetchActions(), fetchIcons(), fetchDepartmentsAndUsers()])
})
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; }
.page-title { font-size: var(--t-xl); font-weight: 500; color: var(--c-fg-1); margin: 0 0 4px; }
.page-subtitle { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; max-width: 72ch; }
.page-subtitle code { font-family: var(--font-mono); font-size: var(--t-2xs); color: var(--c-accent); }

.row-actions { display: flex; align-items: center; gap: var(--gap-2); flex-wrap: wrap; }
.row-actions__sep { color: var(--c-fg-mute); }

.data-table { width: 100%; border-collapse: collapse; }
.data-table th, .data-table td { padding: 8px 12px; text-align: left; vertical-align: top; }
.data-table th {
  color: var(--c-fg-3); font-weight: 400; font-size: var(--t-2xs);
  text-transform: uppercase; letter-spacing: 0.05em;
}
.data-table tr:not(:last-child) td { border-bottom: var(--border-w) solid var(--c-border); }

.icon-cell {
  font-family: var(--font-mono); font-size: var(--t-xs); color: var(--c-fg-1);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border); padding: 2px 6px;
}
.checksum {
  display: inline-block; margin-top: 2px; cursor: pointer;
  font-family: var(--font-mono); font-size: var(--t-2xs); color: var(--c-fg-2);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border); padding: 1px 5px;
}
.tnum { font-variant-numeric: tabular-nums; }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }

.term-textarea--code { font-family: var(--font-mono); font-size: var(--t-xs); }
.body-count {
  margin-top: 4px; text-align: right; font-size: var(--t-2xs); color: var(--c-fg-3);
  font-variant-numeric: tabular-nums;
}
.body-count.is-over { color: var(--c-danger); }

.choices { display: flex; flex-direction: column; gap: var(--gap-3); }
.choice-row {
  border: var(--border-w) solid var(--c-border); padding: 10px 12px;
  display: flex; flex-direction: column; gap: var(--gap-2);
}

.form-toggle {
  display: flex; align-items: flex-start; gap: 8px; font-size: var(--t-xs); color: var(--c-fg-1);
}
.form-toggle__title { font-weight: 500; display: block; }
.form-toggle__hint { display: block; color: var(--c-fg-3); font-size: var(--t-2xs); }

.feedback {
  display: flex; align-items: center; justify-content: space-between; gap: var(--gap-3);
  font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid;
}
.feedback.is-err {
  color: var(--c-danger); border-color: var(--c-danger);
  background: var(--c-danger-soft, rgba(180, 40, 40, 0.08));
}
.feedback.is-ok {
  color: var(--c-ok); border-color: var(--c-ok);
  background: var(--c-ok-soft, rgba(40, 140, 80, 0.08));
}

.bindings-notice {
  font-size: var(--t-xs); color: var(--c-fg-2); margin: 0 0 12px; line-height: 1.5;
}
.bindings-list { display: flex; flex-direction: column; gap: var(--gap-3); }
.binding-row {
  border: var(--border-w) solid var(--c-border); padding: 10px 12px;
  display: flex; flex-direction: column; gap: var(--gap-2);
}

.picker {
  max-height: 200px; overflow: auto; padding: 4px 0;
}
.picker__row {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  width: 100%; text-align: left; background: transparent; border: 0;
  padding: 6px 12px; cursor: pointer; color: var(--c-fg-1); font-size: var(--t-xs);
}
.picker__row:hover:not(:disabled) { background: var(--c-row-hover, var(--c-surface-2)); }
.picker__row.is-on { background: var(--c-accent-soft); color: var(--c-accent-strong); }
.picker__row:disabled { opacity: 0.5; cursor: not-allowed; }
.picker__main { display: flex; flex-direction: column; gap: 2px; }
</style>
