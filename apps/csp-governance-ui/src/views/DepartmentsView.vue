<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">部門</h1>
        <p class="page-head__sub">用於用量歸屬與存取範圍的群組</p>
      </div>
      <TermButton variant="primary" @click="openCreateModal" label="新增部門" />
    </header>

    <TermBox :title="`部門 · ${departments.length}`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>描述</th>
            <th style="width: 14%">使用者</th>
            <th style="width: 100px">狀態</th>
            <th style="width: 14%">建立時間</th>
            <th style="width: 18%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in departments" :key="d.id">
            <td class="cell-strong">{{ d.name }}</td>
            <td class="cell-meta">{{ d.description || '—' }}</td>
            <td class="tnum">
              <span class="cell-strong">{{ d.active_user_count }}</span>
              <span class="cell-meta"> / {{ d.user_count }}</span>
            </td>
            <td><TermBadge :variant="d.is_active ? 'ok' : 'danger'" dot>{{ d.is_active ? '使用中' : '已停用' }}</TermBadge></td>
            <td class="cell-meta tnum">{{ formatDate(d.created_at) }}</td>
            <td>
              <div class="row-actions">
                <button class="term-action" @click="openEditModal(d)">編輯</button>
                <span class="row-actions__sep">·</span>
                <button v-if="d.is_active" class="term-action term-action--danger" @click="handleDeactivate(d)">停用</button>
                <button v-else class="term-action" @click="handleReactivate(d)">重新啟用</button>
              </div>
            </td>
          </tr>
          <tr v-if="departments.length === 0">
            <td colspan="6"><TermEmpty message="尚無部門" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? '編輯 · 部門' : '新增 · 部門'" width="440px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="名稱">
          <input v-model="form.name" class="term-input" placeholder="例：研發" />
        </TermField>
        <TermField label="描述" optional>
          <textarea v-model="form.description" rows="3" class="term-textarea" />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="取消" />
        <TermButton variant="primary" :disabled="!form.name.trim() || saving" :loading="saving" :label="saving ? '儲存中' : (editingId ? '更新' : '建立')" @click="handleSubmit" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { extractError } from '../api/errors'
import { listDepartments, createDepartment, updateDepartment, deactivateDepartment } from '../api/departments'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal } from '../components/cli'
import { useDialog } from '../composables/useDialog'

const { confirm, toast } = useDialog()
const departments = ref([])
const showModal = ref(false)
const editingId = ref(null)
const saving = ref(false)
const form = ref({ name: '', description: '' })

async function fetchDepartments() {
  const { data } = await listDepartments()
  departments.value = data
}
onMounted(fetchDepartments)

function openCreateModal() { editingId.value = null; form.value = { name: '', description: '' }; showModal.value = true }
function openEditModal(d) {
  editingId.value = d.id
  form.value = { name: d.name, description: d.description || '' }
  showModal.value = true
}
async function handleSubmit() {
  saving.value = true
  try {
    const payload = { name: form.value.name.trim(), description: form.value.description.trim() || null }
    if (editingId.value) await updateDepartment(editingId.value, payload)
    else await createDepartment(payload)
    showModal.value = false
    await fetchDepartments()
  } catch (e) {
    toast(extractError(e, '操作失敗'), { tone: 'error' })
  } finally { saving.value = false }
}
async function handleDeactivate(d) {
  if (!(await confirm({ message: `停用「${d.name}」？已綁定的使用者會被解除關聯。`, danger: true }))) return
  await deactivateDepartment(d.id)
  await fetchDepartments()
}
async function handleReactivate(d) {
  await updateDepartment(d.id, { is_active: true })
  await fetchDepartments()
}
function formatDate(s) { return new Date(s).toLocaleString('en-GB') }
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }
.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
</style>
