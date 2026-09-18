<template>
  <section class="page">
    <PageHead title="群組" subtitle="跨部門名單，用來指定哪些人能在對話裡選到某模型。不是組織編制。">
      <template #actions>
        <router-link class="term-action" to="/models">前往模型</router-link>
      </template>
    </PageHead>
    <p class="page-state__hint">在模型列的「對話選單」點「可使用對象」，即可把模型開放給這個群組。</p>
    <form class="row" @submit.prevent="createGroup">
      <input v-model="name" class="term-input" placeholder="群組名稱" />
      <TermButton type="submit" variant="primary" label="新增" />
    </form>
    <PageState v-if="!groups.length" empty empty-title="尚未建立群組" empty-hint="在上方輸入名稱後新增。" />
    <ul v-else class="groups">
      <li v-for="g in groups" :key="g.id" class="group-card">
        <header class="group-card__head">
          <strong>{{ g.name }}</strong>
          <span class="cell-meta">{{ g.is_active ? '有效' : '停用' }} · {{ (members[g.id] || []).length }} 人</span>
          <button type="button" class="term-action term-action--danger" @click="removeGroup(g)">刪除</button>
        </header>
        <p v-if="(linkedModels[g.id] || []).length" class="cell-meta">
          已用於：
          <router-link v-for="m in linkedModels[g.id]" :key="m.id" class="term-action" to="/models">{{ m.display_name }}</router-link>
        </p>
        <p v-else class="cell-meta">還沒有模型把可使用對象設成此群組。</p>
        <div class="chips">
          <span v-for="u in members[g.id] || []" :key="u.id" class="chip">
            {{ u.username }}
            <button type="button" class="chip__x" :aria-label="'移出 ' + u.username" @click="removeMember(g, u.id)">×</button>
          </span>
        </div>
        <UserSearchField placeholder="搜尋帳號並加入" @select="u => addMember(g, u)" />
      </li>
    </ul>
    <p v-if="error" class="field-note">{{ error }}</p>
  </section>
</template>
<script setup>
import { onMounted, reactive, ref } from 'vue'
import { PageHead, PageState, TermButton, UserSearchField } from '../components/cli'
import {
  createModelAccessGroup,
  deleteModelAccessGroup,
  listModelAccessGroupMembers,
  listModelAccessGroupModels,
  listModelAccessGroups,
  replaceModelAccessGroupMembers,
} from '../api/models'
import { extractError } from '../api/errors'

const groups = ref([])
const name = ref('')
const error = ref('')
const members = reactive({})
const linkedModels = reactive({})

async function loadGroupExtras(group) {
  const [mem, models] = await Promise.all([
    listModelAccessGroupMembers(group.id),
    listModelAccessGroupModels(group.id),
  ])
  members[group.id] = mem.data || []
  linkedModels[group.id] = models.data || []
}

async function refresh() {
  const { data } = await listModelAccessGroups()
  groups.value = data || []
  await Promise.all((groups.value).map(loadGroupExtras))
}
onMounted(refresh)

async function createGroup() {
  error.value = ''
  try {
    await createModelAccessGroup({ name: name.value, is_active: true })
    name.value = ''
    await refresh()
  } catch (e) {
    error.value = extractError(e, '新增失敗')
  }
}

async function persistMembers(group, next) {
  await replaceModelAccessGroupMembers(group.id, next.map((u) => u.id))
  members[group.id] = next
}

async function addMember(group, user) {
  error.value = ''
  const current = members[group.id] || []
  if (current.some((u) => u.id === user.id)) return
  try {
    await persistMembers(group, [...current, user])
  } catch (e) {
    error.value = extractError(e, '加入失敗')
  }
}

async function removeMember(group, userId) {
  error.value = ''
  const current = (members[group.id] || []).filter((u) => u.id !== userId)
  try {
    await persistMembers(group, current)
  } catch (e) {
    error.value = extractError(e, '移除失敗')
  }
}

async function removeGroup(group) {
  error.value = ''
  try {
    await deleteModelAccessGroup(group.id)
    await refresh()
  } catch (e) {
    error.value = extractError(e, '刪除失敗')
  }
}
</script>
<style scoped>
.row { display: flex; gap: 8px; max-width: 28rem; margin: 12px 0 20px; }
.groups { list-style: none; padding: 0; margin: 0; display: grid; gap: 16px; }
.group-card {
  padding: 12px 14px;
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-md);
}
.group-card__head { display: flex; align-items: center; gap: 10px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: var(--border-w) solid var(--c-border);
  border-radius: 999px;
  font-size: var(--t-sm);
}
.chip__x { border: 0; background: transparent; cursor: pointer; color: var(--c-fg-2); }
</style>
