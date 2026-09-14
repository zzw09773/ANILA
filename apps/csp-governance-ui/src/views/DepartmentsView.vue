<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">部門</h1>
        <p class="page-head__sub">院部 → 研究所／中心 → 組／科；人員可以掛在任何一層，不必掛到最底層</p>
      </div>
      <TermButton variant="primary" @click="openCreateModal(null)" label="新增最上層部門" />
    </header>

    <TermBox :title="`部門 · ${departments.length}`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>描述</th>
            <th style="width: 14%">使用者（直屬／含下層）</th>
            <th style="width: 100px">狀態</th>
            <th style="width: 14%">建立時間</th>
            <th style="width: 18%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in visibleRows" :key="row.id">
            <td>
              <div class="tree-cell" :style="{ paddingLeft: `${(row.depth - 1) * 18}px` }">
                <button
                  v-if="row.hasChildren"
                  class="tree-toggle"
                  :aria-expanded="!collapsed.has(row.id)"
                  :title="collapsed.has(row.id) ? '展開下層' : '收合下層'"
                  @click="toggleCollapse(row.id)"
                >{{ collapsed.has(row.id) ? '+' : '−' }}</button>
                <span v-else class="tree-toggle tree-toggle--leaf">{{ row.depth > 1 ? '└' : '' }}</span>
                <span class="cell-strong">{{ row.name }}</span>
                <TermBadge v-if="row.depth === 1" variant="info">最上層</TermBadge>
              </div>
            </td>
            <td class="cell-meta">{{ row.description || '—' }}</td>
            <td class="tnum">
              <span class="cell-strong">{{ row.active_user_count }}</span>
              <span class="cell-meta"> 直屬／{{ row.user_count }} 含下層</span>
            </td>
            <td><TermBadge :variant="row.is_active ? 'ok' : 'danger'" dot>{{ row.is_active ? '使用中' : '已停用' }}</TermBadge></td>
            <td class="cell-meta tnum">{{ formatDate(row.created_at) }}</td>
            <td>
              <div class="row-actions">
                <button v-if="row.is_active" class="term-action" @click="openCreateModal(row)">新增下層</button>
                <span v-if="row.is_active" class="row-actions__sep">·</span>
                <button class="term-action" @click="openEditModal(row)">編輯</button>
                <span class="row-actions__sep">·</span>
                <button v-if="row.is_active" class="term-action term-action--danger" @click="handleDeactivate(row)">停用</button>
                <button v-else class="term-action" @click="handleReactivate(row)">重新啟用</button>
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
          <input v-model="form.name" class="term-input" placeholder="例：航空研究所" />
        </TermField>
        <TermField label="隸屬於" :hint="parentHint">
          <select v-model="form.parent_id" class="term-select">
            <option :value="null">— 不隸屬任何單位（最上層，如院部）—</option>
            <option v-for="o in parentOptions" :key="o.id" :value="o.id">
              {{ o.label }}
            </option>
          </select>
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
import { ref, computed, onMounted } from 'vue'
import { listDepartments, getDepartmentTree, createDepartment, updateDepartment, deactivateDepartment } from '../api/departments'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import { departmentOptions, flattenTree, indexById, departmentPath } from '../utils/departmentTree'
import { formatDate } from '../utils/formatDate'
import { extractError } from '../api/errors'

const { confirm, toast } = useDialog()
const departments = ref([])
const tree = ref([])
const collapsed = ref(new Set())
const showModal = ref(false)
const editingId = ref(null)
const saving = ref(false)
const form = ref({ name: '', description: '', parent_id: null })

// 層級順序以後端 /tree 為準（它已排序，而且對環狀資料是安全的），
// 每列的明細再從扁平清單補上——/tree 不回描述、建立時間與停用人數。
const orderedRows = computed(() => {
  const byId = indexById(departments.value)
  const rows = flattenTree(tree.value).map((node) => ({
    ...byId.get(node.id),
    depth: node.depth,
    hasChildren: node.hasChildren,
  })).filter((row) => row.id != null)
  // 保險：萬一有部門沒出現在樹裡（環狀資料），仍然要看得到、改得到。
  const seen = new Set(rows.map((r) => r.id))
  for (const d of departments.value) {
    if (!seen.has(d.id)) rows.push({ ...d, depth: 1, hasChildren: false })
  }
  return rows
})

// 收合：某個節點收起來時，它底下（depth 更深、且尚未回到同層）的列都不顯示。
const visibleRows = computed(() => {
  const out = []
  let hideDeeperThan = null
  for (const row of orderedRows.value) {
    if (hideDeeperThan !== null && row.depth > hideDeeperThan) continue
    hideDeeperThan = null
    out.push(row)
    if (collapsed.value.has(row.id)) hideDeeperThan = row.depth
  }
  return out
})

const parentOptions = computed(() =>
  departmentOptions(departments.value, { excludeSubtreeOf: editingId.value }),
)

const parentHint = computed(() =>
  form.value.parent_id === null
    ? '這個部門會成為最上層單位。直屬院部的人員可以直接掛在這一層。'
    : `完整路徑：${departmentPath(form.value.parent_id, indexById(departments.value))} / ${form.value.name.trim() || '（名稱）'}`,
)

function toggleCollapse(id) {
  const next = new Set(collapsed.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  collapsed.value = next
}

async function fetchDepartments() {
  const [flat, nested] = await Promise.all([listDepartments(), getDepartmentTree()])
  departments.value = flat.data
  tree.value = nested.data
}
onMounted(fetchDepartments)

// 帶 parent 進來 = 從某一列按「新增下層」；不帶 = 頁首的「新增部門」，
// 維持一鍵建立最上層單位，不強迫先選母單位。
function openCreateModal(parent = null) {
  editingId.value = null
  form.value = { name: '', description: '', parent_id: parent?.id ?? null }
  showModal.value = true
}
function openEditModal(d) {
  editingId.value = d.id
  form.value = { name: d.name, description: d.description || '', parent_id: d.parent_id ?? null }
  showModal.value = true
}
async function handleSubmit() {
  saving.value = true
  try {
    const payload = {
      name: form.value.name.trim(),
      description: form.value.description.trim() || null,
      parent_id: form.value.parent_id ?? null,
    }
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

/* 樹狀縮排：層級靠 padding-left 表現，收合鈕佔固定寬度好讓名稱對齊 */
.tree-cell { display: flex; align-items: center; gap: 6px; }
.tree-toggle {
  flex: none;
  width: 16px;
  height: 16px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: 1px solid var(--c-border-strong);
  border-radius: 2px;
  color: var(--c-fg-3);
  font-size: var(--t-2xs);
  cursor: pointer;
}
.tree-toggle:hover { color: var(--c-fg-1); border-color: var(--c-fg-3); }
.tree-toggle--leaf { border-color: transparent; cursor: default; color: var(--c-border-strong); }
.tree-toggle--leaf:hover { border-color: transparent; color: var(--c-border-strong); }
</style>
