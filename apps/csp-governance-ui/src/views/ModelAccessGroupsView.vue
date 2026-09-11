<template>
  <section class="page">
    <header class="page-head">
      <h1>Router 授權群組</h1>
      <p>跨部門專案成員。人員查詢請用現有使用者頁，不要一次下載全院名冊。</p>
    </header>
    <form class="row" @submit.prevent="createGroup">
      <input v-model="name" class="term-input" placeholder="群組名稱" />
      <button class="term-action" type="submit">新增</button>
    </form>
    <ul class="groups">
      <li v-for="g in groups" :key="g.id">
        <strong>{{ g.name }}</strong>
        <span class="cell-meta">{{ g.is_active ? '有效' : '停用' }} · {{ g.member_count }} 人</span>
        <input v-model="memberDraft[g.id]" class="term-input" placeholder="使用者 ID，逗號分隔" />
        <button class="term-action" type="button" @click="saveMembers(g)">更新成員</button>
        <button class="term-action" type="button" @click="removeGroup(g)">刪除</button>
      </li>
    </ul>
    <p v-if="error" class="field-note">{{ error }}</p>
  </section>
</template>
<script setup>
import { onMounted, reactive, ref } from 'vue'
import {
  createModelAccessGroup,
  deleteModelAccessGroup,
  listModelAccessGroups,
  replaceModelAccessGroupMembers,
} from '../api/models'

const groups = ref([])
const name = ref('')
const error = ref('')
const memberDraft = reactive({})

async function refresh() {
  const { data } = await listModelAccessGroups()
  groups.value = data
}
onMounted(refresh)

async function createGroup() {
  error.value = ''
  try {
    await createModelAccessGroup({ name: name.value, is_active: true })
    name.value = ''
    await refresh()
  } catch (e) {
    error.value = e?.response?.data?.detail || '新增失敗'
  }
}
async function saveMembers(group) {
  error.value = ''
  const ids = String(memberDraft[group.id] || '')
    .split(',')
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n > 0)
  try {
    await replaceModelAccessGroupMembers(group.id, ids)
    await refresh()
  } catch (e) {
    error.value = e?.response?.data?.detail || '更新成員失敗'
  }
}
async function removeGroup(group) {
  error.value = ''
  try {
    await deleteModelAccessGroup(group.id)
    await refresh()
  } catch (e) {
    error.value = e?.response?.data?.detail || '刪除失敗'
  }
}
</script>
