<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">admin · console</p>
        <h1 class="page-head__title">公告 banner</h1>
        <p class="page-head__sub">
          張貼維護 / 資安 / 規範公告 — 顯示在所有使用者的 ANILA 對話介面頂部。純文字，不執行程式碼。
        </p>
      </div>
    </header>

    <div v-if="error" class="feedback is-err"><span>!</span><span>{{ error }}</span></div>

    <TermBox title="新增公告" pad="md">
      <div class="grid2">
        <TermField label="level">
          <select v-model="form.level" class="term-input">
            <option value="info">info（一般）</option>
            <option value="warning">warning（注意）</option>
            <option value="error">error（重要）</option>
            <option value="success">success（成功）</option>
          </select>
        </TermField>
        <TermField label="啟用">
          <input type="checkbox" v-model="form.is_active" />
        </TermField>
      </div>
      <TermField label="content（公告內容）">
        <textarea v-model="form.content" class="term-textarea" rows="3" maxlength="2000"
          placeholder="系統將於今晚 22:00–23:00 進行維護，期間服務暫停。"></textarea>
      </TermField>
      <div class="row-actions">
        <TermButton variant="primary" :disabled="busy || !form.content.trim()" :loading="busy"
          label="張貼公告" @click="handleCreate" />
      </div>
    </TermBox>

    <TermBox :title="`目前公告 · ${banners.length}`" pad="md">
      <p v-if="banners.length === 0" class="hint">尚無公告。</p>
      <ul v-else class="banner-list">
        <li v-for="b in banners" :key="b.id" class="banner-row" :class="`is-${b.level}`">
          <div class="banner-row__main">
            <div class="banner-row__meta">
              <TermBadge :variant="b.is_active ? 'accent' : ''">{{ b.is_active ? 'active' : 'off' }}</TermBadge>
              <span class="banner-row__level">{{ b.level }}</span>
            </div>
            <div class="banner-row__content">{{ b.content }}</div>
          </div>
          <div class="banner-row__actions">
            <TermButton :disabled="busy" :label="b.is_active ? '停用' : '啟用'" @click="toggleActive(b)" />
            <TermButton :disabled="busy" label="delete" @click="handleDelete(b)" />
          </div>
        </li>
      </ul>
    </TermBox>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listBanners, createBanner, updateBanner, deleteBanner } from '../api/banners'
import { TermBox, TermButton, TermBadge, TermField } from '../components/cli'
import { useDialog } from '../composables/useDialog'

const { confirm } = useDialog()
const banners = ref([])
const error = ref('')
const busy = ref(false)
const form = ref({ level: 'info', content: '', is_active: true })

async function load() {
  try {
    const { data } = await listBanners()
    banners.value = Array.isArray(data) ? data : []
  } catch (e) {
    error.value = e?.response?.data?.detail || '載入公告失敗'
  }
}

async function handleCreate() {
  const content = form.value.content.trim()
  if (!content) return
  busy.value = true
  error.value = ''
  try {
    await createBanner({ level: form.value.level, content, is_active: form.value.is_active, sort_order: banners.value.length })
    form.value = { level: 'info', content: '', is_active: true }
    await load()
  } catch (e) {
    error.value = e?.response?.data?.detail || '張貼失敗'
  } finally {
    busy.value = false
  }
}

async function toggleActive(b) {
  busy.value = true
  try {
    await updateBanner(b.id, { is_active: !b.is_active })
    await load()
  } catch (e) {
    error.value = e?.response?.data?.detail || '更新失敗'
  } finally {
    busy.value = false
  }
}

async function handleDelete(b) {
  const ok = await confirm({ title: '刪除公告', message: '確定刪除這則公告？' })
  if (!ok) return
  busy.value = true
  try {
    await deleteBanner(b.id)
    await load()
  } catch (e) {
    error.value = e?.response?.data?.detail || '刪除失敗'
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.row-actions { display: flex; gap: 8px; margin-top: 8px; }
.hint { color: var(--c-fg-mute); font-size: var(--t-xs, 12px); }
.banner-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.banner-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding: 10px; border: 1px solid var(--c-border); border-left-width: 3px; border-radius: var(--radius, 6px); }
.banner-row.is-warning { border-left-color: var(--c-warn); }
.banner-row.is-error { border-left-color: var(--c-danger); }
.banner-row.is-success { border-left-color: var(--c-success); }
.banner-row.is-info { border-left-color: var(--c-accent); }
.banner-row__meta { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.banner-row__level { font-size: var(--t-2xs, 11px); color: var(--c-fg-mute); font-family: var(--font-mono); }
.banner-row__content { white-space: pre-wrap; word-break: break-word; }
.banner-row__actions { display: flex; gap: 6px; flex-shrink: 0; }
</style>
