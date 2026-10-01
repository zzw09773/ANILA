<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">額度</h1>
        <p class="page-head__sub">
          預設不限制。單位額度含該單位的下層。任一條設成擋下而且已到上限，呼叫就會被擋下。
          額度只在呼叫開始前檢查，已經在途的呼叫不會被取消，實際用量最多超出同時在途的呼叫數。
        </p>
      </div>
    </header>

    <p v-if="!canEdit" class="cell-meta" data-testid="quota-readonly">單位管理員只能查看額度，不能修改。</p>
    <p v-if="error" class="cell-meta cell-meta--danger">{{ error }}</p>

    <TermBox v-if="canEdit" title="設定額度" pad="md">
      <div class="quota-form">
        <TermField label="對象">
          <select v-model="form.scope_type" class="term-select" :disabled="editingId !== null">
            <option value="user">使用者</option>
            <option value="unit">單位</option>
            <option value="api_key">API 金鑰</option>
          </select>
        </TermField>
        <TermField v-if="form.scope_type === 'user'" label="使用者">
          <select v-model="form.user_id" class="term-select" :disabled="editingId !== null">
            <option :value="null">請選擇</option>
            <option v-for="user in users" :key="user.id" :value="user.id">{{ user.username }}</option>
          </select>
        </TermField>
        <TermField v-if="form.scope_type === 'unit'" label="單位">
          <select v-model="form.department_id" class="term-select" :disabled="editingId !== null">
            <option :value="null">請選擇</option>
            <option v-for="dept in departments" :key="dept.id" :value="dept.id">{{ dept.name }}</option>
          </select>
        </TermField>
        <TermField v-if="form.scope_type === 'api_key'" label="API 金鑰">
          <select v-model="form.api_key_id" class="term-select" :disabled="editingId !== null">
            <option :value="null">請選擇</option>
            <option v-for="key in apiKeys" :key="key.id" :value="key.id">{{ key.name }}</option>
          </select>
        </TermField>
        <TermField label="期間">
          <select v-model="form.period" class="term-select" :disabled="editingId !== null">
            <option value="daily">每日</option>
            <option value="monthly">每月</option>
          </select>
        </TermField>
        <TermField label="計量">
          <select v-model="form.metric" class="term-select" :disabled="editingId !== null">
            <option value="tokens">token</option>
            <option value="cost" :disabled="!moneyReady">金額</option>
          </select>
        </TermField>
        <p v-if="coverageHint" class="cell-meta">{{ coverageHint }}</p>
        <TermField label="上限" hint="留空＝不限 · 0＝立刻擋下">
          <input v-model="form.limit_value" class="term-input" inputmode="decimal" placeholder="不限" />
        </TermField>
        <TermField label="提醒門檻（%）">
          <input v-model.number="form.warn_percent" class="term-input" type="number" min="1" max="100" />
        </TermField>
        <TermField label="到上限時">
          <select v-model="form.on_limit" class="term-select">
            <option value="warn">提醒</option>
            <option value="block">擋下</option>
          </select>
        </TermField>
      </div>
      <div class="quota-form__actions">
        <TermButton variant="primary" type="button" :label="editingId ? '更新額度' : '建立額度'" @click="save" />
        <TermButton v-if="editingId" variant="ghost" type="button" label="取消編輯" @click="resetForm" />
      </div>
    </TermBox>

    <TermBox title="目前額度" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>對象</th>
            <th>期間</th>
            <th>計量</th>
            <th class="num">上限</th>
            <th class="num">提醒</th>
            <th>到上限</th>
            <th v-if="canEdit">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>
              {{ subjectLabel(row) }}
              <p v-if="row.unpriced_message" class="cell-meta">{{ row.unpriced_message }}</p>
            </td>
            <td>{{ row.period === 'daily' ? '每日' : '每月' }}</td>
            <td>{{ row.metric === 'tokens' ? 'token' : '金額' }}</td>
            <td class="num tnum">{{ row.limit_display == null ? '不限' : row.limit_display }}</td>
            <td class="num tnum">{{ row.warn_percent }}%</td>
            <td>{{ row.on_limit === 'block' ? '擋下' : '提醒' }}</td>
            <td v-if="canEdit">
              <button class="term-action" type="button" @click="beginEdit(row)">編輯</button>
              <span class="row-actions__sep">·</span>
              <button class="term-action term-action--danger" type="button" @click="remove(row)">刪除</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="canEdit ? 7 : 6"><TermEmpty message="尚未設定額度。沒有額度就不會限制呼叫。" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { TermBox, TermButton, TermEmpty, TermField } from '../components/cli'
import { listApiKeys } from '../api/apiKeys'
import { extractError } from '../api/errors'
import { listDepartments } from '../api/departments'
import { createQuota, deleteQuota, listQuotas, quotaPriceCoverage, updateQuota } from '../api/quotas'
import { listUsers } from '../api/users'
import { useAuthStore } from '../stores/auth'
import { moneyToMicros } from '../utils/pricingDisplay'

const authStore = useAuthStore()
const canEdit = computed(() => authStore.isAdmin)
const rows = ref([])
const users = ref([])
const departments = ref([])
const apiKeys = ref([])
const error = ref('')
const moneyReady = ref(false)
const coverageHint = ref('')
const editingId = ref(null)
const originalLimit = ref(null)
const originalLimitText = ref('')
const form = ref(emptyForm())

function emptyForm() {
  return {
    scope_type: 'user',
    user_id: null,
    department_id: null,
    api_key_id: null,
    period: 'daily',
    metric: 'tokens',
    limit_value: '',
    warn_percent: 80,
    on_limit: 'warn',
  }
}

function subjectLabel(row) {
  if (row.scope_type === 'unit') return `單位「${row.department_name || row.department_id}」`
  if (row.scope_type === 'api_key') return `API 金鑰「${row.api_key_name || row.api_key_id}」`
  return `使用者 ${row.username || row.user_id}`
}

function resetForm() {
  editingId.value = null
  originalLimit.value = null
  originalLimitText.value = ''
  form.value = emptyForm()
}

async function refreshCoverage() {
  if (editingId.value) {
    moneyReady.value = true
    coverageHint.value = ''
    return
  }
  const scope = form.value.scope_type
  const subjectId = scope === 'user'
    ? form.value.user_id
    : scope === 'unit'
      ? form.value.department_id
      : form.value.api_key_id
  if (!subjectId) {
    moneyReady.value = false
    coverageHint.value = ''
    if (form.value.metric === 'cost') form.value.metric = 'tokens'
    return
  }
  try {
    const { data } = await quotaPriceCoverage({
      scope_type: scope,
      user_id: scope === 'user' ? subjectId : undefined,
      department_id: scope === 'unit' ? subjectId : undefined,
      api_key_id: scope === 'api_key' ? subjectId : undefined,
    })
    moneyReady.value = !!data.ready
    coverageHint.value = data.ready ? '' : '這個對象還有模型未完整計價，不能用金額額度。'
    if (!data.ready && form.value.metric === 'cost') form.value.metric = 'tokens'
  } catch {
    moneyReady.value = false
    coverageHint.value = '這個對象還有模型未完整計價，不能用金額額度。'
    if (form.value.metric === 'cost') form.value.metric = 'tokens'
  }
}

watch(
  () => [
    form.value.scope_type,
    form.value.user_id,
    form.value.department_id,
    form.value.api_key_id,
    editingId.value,
  ],
  () => { refreshCoverage() },
)

async function load() {
  const { data } = await listQuotas()
  rows.value = data
}

function limitPayload() {
  const raw = String(form.value.limit_value ?? '').trim()
  if (raw === '') return { limit_value: null, clear_limit: true }
  if (editingId.value && raw === originalLimitText.value) {
    return { limit_value: originalLimit.value, clear_limit: originalLimit.value == null }
  }
  if (form.value.metric === 'cost') {
    return { limit_value: moneyToMicros(raw), clear_limit: false }
  }
  if (!/^\d+$/.test(raw)) throw new Error('上限須為非負整數')
  return { limit_value: Number(raw), clear_limit: false }
}

async function save() {
  error.value = ''
  try {
    const limit = limitPayload()
    if (editingId.value) {
      await updateQuota(editingId.value, {
        limit_value: limit.limit_value,
        clear_limit: limit.clear_limit,
        warn_percent: Number(form.value.warn_percent),
        on_limit: form.value.on_limit,
      })
    } else {
      await createQuota({
        scope_type: form.value.scope_type,
        user_id: form.value.scope_type === 'user' ? form.value.user_id : null,
        department_id: form.value.scope_type === 'unit' ? form.value.department_id : null,
        api_key_id: form.value.scope_type === 'api_key' ? form.value.api_key_id : null,
        period: form.value.period,
        metric: form.value.metric,
        limit_value: limit.limit_value,
        warn_percent: Number(form.value.warn_percent),
        on_limit: form.value.on_limit,
      })
    }
    resetForm()
    await load()
  } catch (err) {
    error.value = err instanceof Error && !err.response ? err.message : extractError(err, '額度沒有寫入')
  }
}

function beginEdit(row) {
  editingId.value = row.id
  originalLimit.value = row.limit_value
  originalLimitText.value = row.limit_display == null ? '' : String(row.limit_display)
  form.value = {
    scope_type: row.scope_type,
    user_id: row.user_id,
    department_id: row.department_id,
    api_key_id: row.api_key_id,
    period: row.period,
    metric: row.metric,
    limit_value: row.limit_display ?? '',
    warn_percent: row.warn_percent,
    on_limit: row.on_limit,
  }
}

async function remove(row) {
  error.value = ''
  try {
    await deleteQuota(row.id)
    if (editingId.value === row.id) resetForm()
    await load()
  } catch (err) {
    error.value = extractError(err, '額度沒有刪除')
  }
}

onMounted(async () => {
  try {
    await load()
    if (!canEdit.value) return
    const [{ data: userRows }, { data: deptRows }, { data: keyRows }] = await Promise.all([
      listUsers(),
      listDepartments(),
      listApiKeys(),
    ])
    users.value = userRows
    departments.value = deptRows
    apiKeys.value = keyRows
  } catch (err) {
    error.value = extractError(err, '讀不到額度')
  }
})
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.quota-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--gap-3);
}
.quota-form__actions { display: flex; gap: var(--gap-2); margin-top: var(--gap-3); }
@media (max-width: 800px) { .quota-form { grid-template-columns: 1fr; } }
</style>
