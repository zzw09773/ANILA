<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h2 class="text-lg font-semibold">服務存取權限</h2>
        <p class="mt-1 text-sm text-gray-500">
          管理 platform_links 的個別 user / 部門 grant — 對應 multi-service-integration-plan §7.5.3
        </p>
      </div>
    </div>

    <div v-if="pageError" class="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      {{ pageError }}
    </div>

    <div v-if="loading" class="rounded-xl border border-gray-200 bg-white px-4 py-6 text-sm text-gray-500">
      載入中…
    </div>

    <div v-else class="space-y-4">
      <div
        v-for="link in sortedLinks"
        :key="link.id"
        class="bg-white rounded-xl border border-gray-200 overflow-hidden"
      >
        <button
          type="button"
          class="flex w-full items-start justify-between gap-4 px-5 py-4 text-left hover:bg-gray-50 transition"
          @click="toggleExpand(link.id)"
        >
          <div class="min-w-0 space-y-1">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-medium">{{ link.name }}</span>
              <span
                v-if="!link.is_active"
                class="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600"
              >已停用</span>
              <span
                v-if="link.is_public"
                class="text-xs px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200"
              >public</span>
              <span
                class="text-xs px-2 py-0.5 rounded font-mono"
                :class="(link.required_roles || []).length === 0
                  ? 'bg-gray-100 text-gray-500'
                  : 'bg-indigo-50 text-indigo-700 border border-indigo-200'"
              >
                {{ (link.required_roles || []).length === 0 ? 'role gate: 開放' : (link.required_roles || []).join(' / ') }}
              </span>
            </div>
            <div class="font-mono text-xs text-gray-400 break-all">{{ link.url }}</div>
          </div>
          <div class="text-right shrink-0">
            <div class="text-xs text-gray-500">active grants</div>
            <div class="text-xl font-semibold text-gray-800">{{ activeGrantsCount(link.id) }}</div>
          </div>
        </button>

        <div v-if="expandedId === link.id" class="border-t border-gray-200 bg-gray-50">
          <div class="flex items-center gap-2 px-5 py-3 border-b border-gray-200">
            <button
              class="px-3 py-1.5 bg-indigo-600 text-white text-xs rounded hover:bg-indigo-700 transition"
              @click="openGrantModal(link, 'user')"
            >+ 給 user</button>
            <button
              class="px-3 py-1.5 bg-white border border-gray-300 text-gray-700 text-xs rounded hover:bg-gray-100 transition"
              @click="openGrantModal(link, 'department')"
            >+ 給部門</button>
            <span class="ml-auto text-xs text-gray-400">
              {{ activeGrantsForLink(link.id).length }} 筆 active grant
              <span v-if="!link.is_public && (link.required_roles || []).length === 0" class="ml-2 text-amber-700">
                (private + 開放 gate — 純白名單模式)
              </span>
            </span>
          </div>

          <div v-if="activeGrantsForLink(link.id).length === 0" class="px-5 py-6 text-sm text-gray-400">
            尚未發出任何 active grant{{ link.is_public ? '（已 public，user 通過 role gate 即可看到）' : '' }}
          </div>

          <div
            v-for="g in activeGrantsForLink(link.id)"
            :key="g.id"
            class="flex items-center justify-between gap-3 px-5 py-3 border-b border-gray-200 last:border-b-0 text-sm"
          >
            <div class="min-w-0 space-y-0.5">
              <div class="flex items-center gap-2">
                <span
                  class="text-xs px-2 py-0.5 rounded font-mono"
                  :class="g.user_id != null
                    ? 'bg-indigo-50 text-indigo-700'
                    : 'bg-amber-50 text-amber-700'"
                >{{ g.user_id != null ? 'USER' : 'DEPT' }}</span>
                <span class="font-medium">{{ targetLabel(g) }}</span>
              </div>
              <div class="text-xs text-gray-400">
                {{ formatDate(g.granted_at) }} · 授權者 {{ granterLabel(g) }}
              </div>
            </div>
            <button
              class="text-xs text-red-600 hover:text-red-800"
              @click="handleRevoke(g)"
            >Revoke</button>
          </div>
        </div>
      </div>

      <div v-if="sortedLinks.length === 0" class="px-4 py-10 text-center text-gray-400 bg-white rounded-xl border border-gray-200">
        尚無平台連結 — 請先到「平台連結設定」建立。
      </div>
    </div>

    <div v-if="showGrantModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="closeGrantModal"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-lg w-full mx-4">
        <h3 class="text-lg font-semibold">
          授權 {{ grantModalLink?.name }}
        </h3>
        <p class="mt-1 text-xs text-gray-500">同一目標只能有一筆 active grant</p>

        <div class="mt-4 flex gap-2 text-sm">
          <button
            class="px-3 py-1.5 rounded transition"
            :class="grantModalType === 'user'
              ? 'bg-indigo-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
            @click="setGrantModalType('user')"
          >User-level</button>
          <button
            class="px-3 py-1.5 rounded transition"
            :class="grantModalType === 'department'
              ? 'bg-indigo-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
            @click="setGrantModalType('department')"
          >Department-level</button>
        </div>

        <input
          v-model="grantModalFilter"
          :placeholder="grantModalType === 'user' ? '搜尋 username / email' : '搜尋部門名稱'"
          class="mt-3 w-full px-3 py-2 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-indigo-500 text-sm"
        />

        <div class="mt-3 max-h-64 overflow-y-auto border border-gray-200 rounded-lg bg-gray-50">
          <template v-if="grantModalType === 'user'">
            <button
              v-for="u in filteredUsers"
              :key="u.id"
              type="button"
              class="block w-full text-left px-4 py-2 border-b border-gray-200 last:border-b-0 text-sm transition"
              :class="grantModalSelectedId === u.id ? 'bg-indigo-50' : 'hover:bg-white'"
              @click="grantModalSelectedId = u.id"
            >
              <div class="flex justify-between gap-3">
                <span class="font-medium">{{ u.username }}</span>
                <span class="text-xs text-gray-400">
                  {{ u.role }}{{ u.department_name ? ` · ${u.department_name}` : '' }}
                </span>
              </div>
              <div v-if="u.email" class="text-xs text-gray-500 mt-0.5">{{ u.email }}</div>
            </button>
            <div v-if="filteredUsers.length === 0" class="px-4 py-6 text-xs text-gray-400 text-center">
              沒有符合的 user
            </div>
          </template>
          <template v-else>
            <button
              v-for="d in filteredDepts"
              :key="d.id"
              type="button"
              class="block w-full text-left px-4 py-2 border-b border-gray-200 last:border-b-0 text-sm transition"
              :class="grantModalSelectedId === d.id ? 'bg-indigo-50' : 'hover:bg-white'"
              @click="grantModalSelectedId = d.id"
            >
              <div class="flex justify-between gap-3">
                <span class="font-medium">{{ d.name }}</span>
                <span class="text-xs text-gray-400">{{ d.user_count ?? 0 }} 位使用者</span>
              </div>
            </button>
            <div v-if="filteredDepts.length === 0" class="px-4 py-6 text-xs text-gray-400 text-center">
              {{ departments.length === 0 ? '尚未建立任何部門。請先到部門設定。' : '沒有符合的部門' }}
            </div>
          </template>
        </div>

        <div v-if="grantModalError" class="mt-3 text-sm text-red-600">
          {{ grantModalError }}
        </div>

        <div class="flex justify-end gap-3 mt-5">
          <button
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
            @click="closeGrantModal"
          >取消</button>
          <button
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
            :disabled="grantModalSubmitting || !grantModalSelectedId"
            @click="submitGrant"
          >{{ grantModalSubmitting ? '授權中…' : '授權' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { listPlatformLinks } from '../api/platformLinks'
import { listGrants, createGrant, revokeGrant } from '../api/serviceAccessGrants'
import { listUsers } from '../api/users'
import { listDepartments } from '../api/departments'

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
    const [linksRes, grantsRes, usersRes, deptsRes] = await Promise.all([
      listPlatformLinks({ include_inactive: true }),
      listGrants(),
      listUsers(),
      listDepartments(),
    ])
    links.value = linksRes.data || []
    grants.value = grantsRes.data || []
    users.value = usersRes.data || []
    departments.value = deptsRes.data || []
  } catch (e) {
    pageError.value = e.response?.data?.detail || e.message || '載入失敗'
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

const sortedLinks = computed(() => {
  return links.value.slice().sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
})

function activeGrantsForLink(linkId) {
  return grants.value.filter((g) => g.platform_link_id === linkId && !g.revoked_at)
}

function activeGrantsCount(linkId) {
  return activeGrantsForLink(linkId).length
}

function toggleExpand(id) {
  expandedId.value = expandedId.value === id ? null : id
}

const userById = computed(() => {
  const m = new Map()
  for (const u of users.value) m.set(u.id, u)
  return m
})

const deptById = computed(() => {
  const m = new Map()
  for (const d of departments.value) m.set(d.id, d)
  return m
})

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

function formatDate(s) {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })
}

const filteredUsers = computed(() => {
  const q = grantModalFilter.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) =>
      u.username.toLowerCase().includes(q) ||
      (u.email || '').toLowerCase().includes(q),
  )
})

const filteredDepts = computed(() => {
  const q = grantModalFilter.value.trim().toLowerCase()
  if (!q) return departments.value
  return departments.value.filter((d) => d.name.toLowerCase().includes(q))
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

function closeGrantModal() {
  showGrantModal.value = false
  grantModalLink.value = null
  grantModalSelectedId.value = null
  grantModalError.value = ''
}

function setGrantModalType(type) {
  if (grantModalType.value === type) return
  grantModalType.value = type
  grantModalSelectedId.value = null
}

async function submitGrant() {
  if (!grantModalSelectedId.value || !grantModalLink.value) return
  grantModalSubmitting.value = true
  grantModalError.value = ''
  try {
    const payload = {
      platform_link_id: grantModalLink.value.id,
      ...(grantModalType.value === 'user'
        ? { user_id: grantModalSelectedId.value }
        : { department_id: grantModalSelectedId.value }),
    }
    await createGrant(payload)
    closeGrantModal()
    await loadAll()
  } catch (e) {
    grantModalError.value = e.response?.data?.detail || '授權失敗'
  } finally {
    grantModalSubmitting.value = false
  }
}

async function handleRevoke(g) {
  const target = targetLabel(g)
  if (!confirm(`確認 revoke ${target} 的 grant？此動作可由再次 grant 還原。`)) return
  try {
    await revokeGrant(g.id)
    await loadAll()
  } catch (e) {
    alert(e.response?.data?.detail || 'Revoke 失敗')
  }
}
</script>
