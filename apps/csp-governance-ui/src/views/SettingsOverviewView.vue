<!-- 平台設定總覽：十二顆 C 類設定，儲存後下一個請求生效。 -->
<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">平台設定</h1>
        <p class="page-head__sub">管理仍屬於平台行為的十二顆即時設定。</p>
      </div>
      <TermButton variant="ghost" :loading="loading" label="重新載入" @click="load" />
    </header>

    <p v-if="countWarning" class="settings-count-warning">{{ countWarning }}</p>

    <TermBox v-if="state === 'failed'" title="讀不到設定總覽" class="settings-load-error">
      <p class="settings-load-error__msg">{{ overviewStateMessage('failed') }}</p>
      <p class="settings-load-error__detail">{{ loadError }}</p>
      <TermButton variant="primary" label="重試" @click="load" />
    </TermBox>
    <TermBox v-else-if="state === 'loading'">
      <p class="cell-meta">{{ overviewStateMessage('loading') }}</p>
    </TermBox>
    <TermEmpty v-else-if="state === 'empty'" :message="overviewStateMessage('empty')" />

    <div v-else data-region="editable" class="settings-region">
      <TermBox
        v-for="section in sections"
        :key="section.id"
        :title="`${section.title} · ${section.items.length}`"
        :hint="section.hint"
        pad="none"
        flush
      >
        <table class="term-table">
          <thead>
            <tr>
              <th style="width: 32%">設定</th>
              <th>目前值</th>
              <th style="width: 30%">改成</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in section.items" :key="item.key">
              <td>
                <div class="cell-strong">{{ item.key }}</div>
                <div class="cell-meta">{{ item.description }}</div>
                <div class="cell-meta">{{ item.env_name || '只住在 DB' }} · {{ item.value_type }}</div>
              </td>
              <td>
                <dl class="setting-cells">
                  <div v-for="cell in valueCells(item)" :key="cell.field" class="setting-cell" :class="cell.className">
                    <dt>{{ cell.label }}</dt>
                    <dd>{{ cell.text }}</dd>
                  </div>
                </dl>
              </td>
              <td>
                <div v-if="canEdit(item)" class="setting-editor">
                  <input
                    v-model="drafts[item.key]"
                    class="term-input"
                    :aria-label="`${item.key} 的新值`"
                  />
                  <TermButton
                    variant="primary"
                    :loading="!!saving[item.key]"
                    label="儲存"
                    @click="handleSave(item)"
                  />
                </div>
                <p v-else class="cell-meta">後端未允許編輯此類別。</p>
                <p
                  v-if="notices[item.key]"
                  class="setting-notice"
                  :class="`setting-notice--${notices[item.key].tone}`"
                >{{ notices[item.key].message }}</p>
                <p v-if="errors[item.key]" class="setting-error">{{ errors[item.key] }}</p>
              </td>
            </tr>
          </tbody>
        </table>
      </TermBox>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getPlatformSettingsOverview, updatePlatformSetting } from '../api/platformSettings'
import { TermBox, TermButton, TermEmpty } from '../components/cli'
import {
  canEdit,
  countMismatchWarning,
  draftValue,
  extractDetail,
  groupIntoSections,
  overviewState,
  overviewStateMessage,
  replaceRow,
  saveNotice,
  valueCells,
} from '../utils/settingsView'

const overview = ref(null)
const items = ref([])
const loaded = ref(false)
const loading = ref(false)
const loadError = ref(null)
const drafts = ref({})
const errors = ref({})
const notices = ref({})
const saving = ref({})

const state = computed(() => overviewState({ loaded: loaded.value, error: loadError.value, items: items.value }))
const sections = computed(() => groupIntoSections(items.value))
const countWarning = computed(() => countMismatchWarning(overview.value))

async function load() {
  loading.value = true
  try {
    const { data } = await getPlatformSettingsOverview()
    overview.value = data
    items.value = Array.isArray(data?.items) ? data.items : []
    for (const item of items.value) {
      if (canEdit(item)) drafts.value[item.key] = draftValue(item)
    }
    loadError.value = null
  } catch (error) {
    loadError.value = extractDetail(error, overviewStateMessage('failed'))
    overview.value = null
    items.value = []
  } finally {
    loading.value = false
    loaded.value = true
  }
}

onMounted(load)

async function handleSave(item) {
  saving.value[item.key] = true
  try {
    const { data } = await updatePlatformSetting(item.key, drafts.value[item.key])
    items.value = replaceRow(items.value, data)
    drafts.value[item.key] = draftValue(data)
    delete errors.value[item.key]
    notices.value[item.key] = saveNotice(data)
  } catch (error) {
    delete notices.value[item.key]
    errors.value[item.key] = extractDetail(error, '儲存失敗')
  } finally {
    delete saving.value[item.key]
  }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.settings-region { display: flex; flex-direction: column; gap: var(--gap-4); }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.settings-count-warning { border-left: 2px solid var(--c-warn); padding-left: var(--gap-3); font-size: var(--t-sm); color: var(--c-warn); }
.settings-load-error__msg { font-size: var(--t-sm); color: var(--c-fg-1); margin-bottom: 4px; }
.settings-load-error__detail { font-size: var(--t-xs); color: var(--c-danger); margin-bottom: var(--gap-3); overflow-wrap: anywhere; }
.setting-cells { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 4px 10px; margin: 0; }
.setting-cell dt { font-size: var(--t-2xs); color: var(--c-fg-mute); }
.setting-cell dd { margin: 0; font-size: var(--t-xs); overflow-wrap: anywhere; }
.setting-cell--effective dd { color: var(--c-fg-1); font-weight: 500; }
.setting-cell--stored dd { color: var(--c-fg-2); }
.setting-cell--unusable dd { color: var(--c-danger); text-decoration: line-through; }
.setting-cell--source dd { color: var(--c-fg-3); }
.setting-editor { display: flex; gap: 6px; align-items: center; }
.setting-editor .term-input { flex: 1; min-width: 0; }
.setting-notice { font-size: var(--t-2xs); margin-top: 4px; overflow-wrap: anywhere; }
.setting-notice--ok { color: var(--c-fg-2); }
.setting-error { font-size: var(--t-2xs); margin-top: 4px; color: var(--c-danger); overflow-wrap: anywhere; }
</style>
