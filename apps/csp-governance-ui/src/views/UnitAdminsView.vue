<template>
  <section class="page">
    <PageHead
      title="單位管理員"
      subtitle="一位單位管理員看得到該單位與下層的成員、用量與額度。每一列左方是這個單位自己的人數：指派對上限，人資主管另外算，上層繼承的人不計入。"
    />

    <PageState v-if="loading" loading loading-label="載入單位管理員中…" />
    <PageState v-else-if="pageError" :error="pageError" />
    <PageState
      v-else-if="departments.length === 0"
      empty
      empty-title="尚無部門"
      empty-hint="請先到「部門」建立單位，再回來指派管理員。"
    >
      <template #action>
        <router-link class="term-action" to="/departments">前往部門</router-link>
      </template>
    </PageState>

    <template v-else>
      <div class="kpi-row">
        <TermStat
          label="沒有管理員的單位"
          :value="summary.uncovered"
          format="int"
          :tone="summary.uncovered ? 'warn' : 'default'"
        />
        <TermStat label="指派名額已滿" :value="summary.full" format="int" />
        <TermStat
          label="人資帶入的主管"
          :value="summary.hrPeople"
          format="int"
          hint="人數，不計入指派名額"
        />
      </div>

      <TermBox title="尋找" pad="sm">
        <div class="filters">
          <TermField label="單位">
            <input v-model="deptQuery" class="term-input" placeholder="單位名稱" />
          </TermField>
          <TermField label="篩選">
            <label class="setting-switch">
              <input v-model="uncoveredOnly" type="checkbox" />
              只看沒有管理員的單位
            </label>
          </TermField>
          <TermField label="查一個人" hint="看這個人管理哪些單位，以及原因。">
            <UserSearchField placeholder="員工編號" @select="onLookup" />
          </TermField>
        </div>
      </TermBox>

      <TermBox v-if="lookupUser" :title="`查詢 · ${lookupHeading}`" pad="none" flush>
        <template #trailing>
          <TermButton variant="ghost" size="xs" label="清除" @click="clearLookup" />
        </template>
        <p class="inset-note">下面是這個人目前管理的單位。每一筆都含該單位的下層。</p>
        <table v-if="personUnits.length" class="term-table">
          <thead>
            <tr>
              <th>身分</th>
              <th>單位</th>
              <th>原因</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="unit in personUnits" :key="unit.id">
              <td><TermBadge>{{ kindLabel(unit.kind) }}</TermBadge></td>
              <td class="cell-strong">{{ unit.path }}</td>
              <td>{{ unit.why }}</td>
              <td>
                <button type="button" class="term-action" @click="selectDepartment(unit.departmentId)">查看此單位</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-pad">
          <TermEmpty message="這個人目前沒有管理任何單位。若應由人資帶入，請確認人資有職稱、人資資料庫的「主管自動成為單位管理員」是開的，再請這個人刷卡登入。" />
        </div>
      </TermBox>

      <TermBox :title="`單位 · ${visibleRows.length}`" hint="左方數字不含上層繼承" pad="none" flush>
        <table v-if="visibleRows.length" class="term-table admin-table">
          <thead>
            <tr>
              <th>單位</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in visibleRows"
              :key="row.id"
              :class="{ 'is-selected': selectedId === row.id }"
            >
              <td>
                <div class="dept-lead">
                  <div class="dept-counts">
                    <div class="count-block">
                      <span class="count-figure tnum">{{ row.fraction }}</span>
                      <span class="count-caption">指派</span>
                    </div>
                    <div class="count-block">
                      <span class="count-figure tnum">{{ row.hrCount }}</span>
                      <span class="count-caption">人資主管</span>
                    </div>
                  </div>
                  <div class="tree-cell" :style="{ paddingLeft: `${(row.depth - 1) * 18}px` }">
                    <span class="tree-leaf">{{ row.depth > 1 ? '└' : '' }}</span>
                    <span class="cell-strong">{{ row.name }}</span>
                    <TermBadge v-if="!row.covered">沒有管理員</TermBadge>
                    <TermBadge v-if="!row.isActive" variant="danger">已停用</TermBadge>
                    <span v-if="row.inherited.length" class="cell-meta">繼承 {{ row.inherited.length }} 位，不計入左方數字</span>
                  </div>
                </div>
              </td>
              <td>
                <button type="button" class="term-action" @click="selectDepartment(row.id)">
                  {{ selectedId === row.id ? '查看中' : '查看' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-pad">
          <TermEmpty :message="emptyTableMessage" />
        </div>
      </TermBox>

      <TermBox v-if="!selected" title="這個單位的管理員" pad="md">
        <TermEmpty message="選一個單位的「查看」，才能指派或撤銷。沒有管理員的單位，用上方「只看沒有管理員的單位」找出來。" />
      </TermBox>

      <TermBox v-else :title="selected.path || selected.name" :hint="selectedHint" pad="none" flush>
        <div class="detail-block" data-kind="assigned">
          <TermSection :title="`指派 · ${selected.fraction}`" />
          <table class="term-table">
            <thead>
              <tr>
                <th>身分</th>
                <th>姓名</th>
                <th>員工編號</th>
                <th>說明</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in selected.assigned" :key="row.id">
                <td><TermBadge>{{ kindLabel('assigned') }}</TermBadge></td>
                <td class="cell-strong">{{ personLabel(row).primary }}</td>
                <td class="tnum">{{ personLabel(row).employeeNo }}</td>
                <td class="cell-meta">管理員指派。範圍含這個單位與下層。</td>
                <td>
                  <button
                    v-if="canRevoke(row)"
                    type="button"
                    class="term-action term-action--danger"
                    @click="revokeRow(row)"
                  >撤銷指派</button>
                </td>
              </tr>
              <tr v-if="selected.assigned.length === 0">
                <td colspan="5">
                  <TermEmpty message="還沒有指派的人。在下面用員工編號找出來，再按指派。" />
                </td>
              </tr>
            </tbody>
          </table>
          <div class="assign-row">
            <UserSearchField
              placeholder="員工編號，指派到這個單位"
              :disabled="selected.isActive === false"
              @select="onPick"
            />
            <TermButton
              variant="primary"
              label="指派"
              :disabled="!picked || !!blockReason || saving"
              :loading="saving"
              @click="assignToSelected"
            />
          </div>
          <p v-if="picked" class="cell-meta">要指派的員工編號：{{ picked.username }}</p>
          <p v-if="blockReason" class="cell-meta">{{ blockReason }}</p>
          <p v-if="actionError" class="cell-meta cell-meta--danger">{{ actionError }}</p>
          <p v-if="actionNote" class="cell-meta">{{ actionNote }}</p>
        </div>

        <div class="detail-block" data-kind="hr">
          <TermSection :title="`人資帶入的主管 · ${selected.hrCount}`" />
          <p v-if="selected.hr.length" class="cell-meta">{{ HR_MANAGED_NOTE }}</p>
          <table v-if="selected.hr.length" class="term-table">
            <thead>
              <tr>
                <th>身分</th>
                <th>姓名</th>
                <th>員工編號</th>
                <th>職稱</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in selected.hr" :key="row.id">
                <td><TermBadge>{{ kindLabel('hr') }}</TermBadge></td>
                <td class="cell-strong">{{ personLabel(row).primary }}</td>
                <td class="tnum">{{ personLabel(row).employeeNo }}</td>
                <td>{{ titlesText(row) }}</td>
              </tr>
            </tbody>
          </table>
          <TermEmpty
            v-else
            message="目前沒有人資帶入的主管。有職稱的人刷卡登入，而且人資資料庫的「主管自動成為單位管理員」開著，就會列在這裡。"
          />
        </div>

        <div class="detail-block" data-kind="inherited">
          <TermSection :title="`上層帶入 · ${selected.inherited.length}`" />
          <p class="cell-meta">{{ INHERITED_NOTE }}</p>
          <table v-if="selected.inherited.length" class="term-table">
            <thead>
              <tr>
                <th>身分</th>
                <th>姓名</th>
                <th>員工編號</th>
                <th>說明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in selected.inherited" :key="`${row.inheritedFromId}-${row.id}`">
                <td><TermBadge>{{ kindLabel('inherited') }}</TermBadge> <TermBadge>{{ kindLabel(originKind(row)) }}</TermBadge></td>
                <td class="cell-strong">{{ personLabel(row).primary }}</td>
                <td class="tnum">{{ personLabel(row).employeeNo }}</td>
                <td>{{ inheritedWhy(row) }}</td>
              </tr>
            </tbody>
          </table>
          <TermEmpty v-else message="沒有從上層繼承的管理員。這個單位的範圍只算上面兩區的人。" />
        </div>
      </TermBox>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  PageHead,
  PageState,
  TermBadge,
  TermBox,
  TermButton,
  TermEmpty,
  TermField,
  TermSection,
  TermStat,
  UserSearchField,
} from '../components/cli'
import { extractError } from '../api/errors'
import { getDepartmentTree, listDepartments } from '../api/departments'
import { createUnitAdmin, listUnitAdmins, revokeUnitAdmin, unitAdminCounts } from '../api/unitAdmins'
import { useDialog } from '../composables/useDialog'
import {
  HR_MANAGED_NOTE,
  INHERITED_NOTE,
  alreadyDirect,
  assignBlockReason,
  canRevoke,
  departmentAdminRows,
  filterDepartmentRows,
  formatReportableError,
  inheritedWhy,
  kindLabel,
  orderDepartmentRows,
  originKind,
  personLabel,
  summarize,
  titlesText,
  unitsForPerson,
} from '../utils/unitAdmins'

const { confirm } = useDialog()

const loading = ref(true)
const pageError = ref('')
const departments = ref([])
const tree = ref([])
const assignments = ref([])
const counts = ref(null)
const deptQuery = ref('')
const uncoveredOnly = ref(false)
const selectedId = ref(null)
const lookupUser = ref(null)
const picked = ref(null)
const saving = ref(false)
const actionError = ref('')
const actionNote = ref('')

const orderedRows = computed(() =>
  orderDepartmentRows(
    departmentAdminRows(departments.value, assignments.value, counts.value),
    tree.value,
  ),
)
const visibleRows = computed(() =>
  filterDepartmentRows(orderedRows.value, {
    query: deptQuery.value,
    uncoveredOnly: uncoveredOnly.value,
  }),
)
const summary = computed(() => summarize(orderedRows.value, assignments.value))
const selected = computed(() => orderedRows.value.find((row) => row.id === selectedId.value) || null)
const selectedHint = computed(() => {
  if (!selected.value) return ''
  return `指派 ${selected.value.fraction} · 人資主管 ${selected.value.hrCount} · 繼承不計入`
})
const personUnits = computed(() => {
  if (!lookupUser.value) return []
  return unitsForPerson(lookupUser.value.id, departments.value, assignments.value)
})
const lookupHeading = computed(() => {
  if (!lookupUser.value) return ''
  const named = personUnits.value.find((unit) => unit.displayName)
  const no = lookupUser.value.username
  if (named?.displayName && named.displayName !== no) return `${named.displayName}（員工編號 ${no}）`
  return `員工編號 ${no}`
})
const blockReason = computed(() => {
  const row = selected.value
  if (!row) return ''
  return assignBlockReason({
    isActive: row.isActive,
    assignedCount: row.assignedCount,
    limit: row.limit,
    already: picked.value ? alreadyDirect(assignments.value, row.id, picked.value.id) : false,
  })
})
const emptyTableMessage = computed(() => {
  if (uncoveredOnly.value && deptQuery.value.trim()) return '沒有符合的單位。清掉搜尋，或取消「只看沒有管理員的單位」。'
  if (uncoveredOnly.value) return '每個單位都已經有管理員（含上層涵蓋的）。取消篩選可看全部。'
  if (deptQuery.value.trim()) return '沒有符合的單位。改一下名稱再試。'
  return '尚無部門。請先到「部門」建立單位。'
})

function report(err, fallback) {
  return formatReportableError(extractError(err, fallback), err?.response?.status, err?.code)
}

async function load({ quiet = false } = {}) {
  if (!quiet) loading.value = true
  pageError.value = ''
  try {
    const [deptRes, treeRes, listRes, countRes] = await Promise.all([
      listDepartments(),
      getDepartmentTree(),
      listUnitAdmins(),
      unitAdminCounts(),
    ])
    departments.value = deptRes.data || []
    tree.value = treeRes.data || []
    assignments.value = listRes.data || []
    counts.value = countRes.data || null
  } catch (err) {
    pageError.value = report(err, '讀不到單位管理員')
  } finally {
    loading.value = false
  }
}

function selectDepartment(id) {
  selectedId.value = id
}

function onLookup(user) {
  lookupUser.value = user
}

function clearLookup() {
  lookupUser.value = null
}

function onPick(user) {
  picked.value = user
  actionError.value = ''
  actionNote.value = ''
}

async function assignToSelected() {
  if (!selected.value || !picked.value || blockReason.value) return
  saving.value = true
  actionError.value = ''
  actionNote.value = ''
  try {
    await createUnitAdmin({
      user_id: picked.value.id,
      department_id: selected.value.id,
    })
    const no = picked.value.username
    picked.value = null
    actionNote.value = `已把員工編號 ${no} 指派到「${selected.value.name}」。`
    await load({ quiet: true })
  } catch (err) {
    actionError.value = report(err, '指派沒有完成')
  } finally {
    saving.value = false
  }
}

async function revokeRow(row) {
  if (!canRevoke(row) || !selected.value) return
  const who = personLabel(row)
  const name = who.primary !== '（沒有姓名）' ? who.primary : who.employeeNo
  const ok = await confirm({
    title: '撤銷指派',
    message: `撤銷「${name}」在「${selected.value.name}」的單位管理員？下層不再因這一筆而涵蓋。`,
    confirmText: '撤銷',
    danger: true,
  })
  if (!ok) return
  actionError.value = ''
  actionNote.value = ''
  try {
    await revokeUnitAdmin(row.id)
    actionNote.value = `已撤銷「${name}」的指派。`
    await load({ quiet: true })
  } catch (err) {
    actionError.value = report(err, '撤銷沒有完成')
  }
}

watch(selectedId, () => {
  picked.value = null
  actionError.value = ''
  actionNote.value = ''
})

onMounted(() => load())
</script>

<style scoped>
.kpi-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--gap-3); }
.filters { display: grid; grid-template-columns: 2fr 1fr 1.4fr; gap: var(--gap-3); align-items: end; }
.dept-lead { display: flex; align-items: center; gap: var(--gap-4); flex-wrap: wrap; }
.dept-counts { display: flex; gap: var(--gap-4); flex: none; }
.count-block { display: flex; align-items: baseline; gap: 6px; min-width: 6.5rem; }
.count-figure {
  font-size: var(--t-xl);
  font-weight: 600;
  letter-spacing: var(--tracking-tight);
  color: var(--c-fg-1);
  line-height: 1;
}
.count-caption { font-size: var(--t-2xs); color: var(--c-fg-3); }
.tree-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }
.tree-leaf { width: 16px; flex: none; color: var(--c-border-strong); }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-meta--danger { color: var(--c-danger); font-weight: 500; }
.admin-table tbody td { height: auto; }
.admin-table tbody tr.is-selected td { background: var(--c-surface-2); }
.empty-pad { padding: var(--gap-6) var(--gap-3); }
.inset-note { margin: 0; padding: var(--gap-3) var(--gap-3) 0; color: var(--c-fg-3); font-size: var(--t-2xs); }
.detail-block { padding: var(--gap-4); border-top: var(--border-w) solid var(--c-border); }
.detail-block:first-child { border-top: 0; }
.assign-row { display: flex; gap: var(--gap-3); align-items: flex-end; flex-wrap: wrap; margin-top: var(--gap-3); }
.assign-row :deep(.user-search) { flex: 1; min-width: 12rem; }
@media (max-width: 800px) {
  .kpi-row { grid-template-columns: 1fr; }
  .filters { grid-template-columns: 1fr; }
}
</style>
