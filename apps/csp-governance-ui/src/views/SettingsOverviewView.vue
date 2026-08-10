<!--
  平台設定總覽 —— 96 顆設定，四區三態。

  這一頁刻意只有樣板：分區、分態、顯示字串全部出自 `utils/settingsView.js`，
  由 tests/settingsOverview.test.mjs 直接 import 驗（本 app 掛載不了元件，
  寫在這裡的判斷等於沒有測試）。

  兩件不可以在這裡自己做的事：
  1. **不做樂觀更新**。畫面上的值一律是後端回應那一列，PUT 之後整列取代。
  2. **不改寫後端的話**。`detail` 與 `locked_reason` 原樣上畫面 —— 那兩段字裡
     寫著值域說明與「真的要改的話該去哪裡改」，改寫等於把自救路徑刪掉。
-->
<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">平台設定</h1>
        <p class="page-head__sub">
          一頁看完每一顆設定現在到底是什麼值、為什麼是那個值。後端沒說的，這裡不會替它說。
        </p>
      </div>
      <TermButton variant="ghost" :loading="loading" label="重新載入" @click="load" />
    </header>

    <!-- 開機覆蓋沒載入 = 這一頁「重啟後生效」的承諾這次全部落空。放最前面。 -->
    <div v-if="banner" class="boot-override-banner">
      <p class="boot-override-banner__title">{{ banner.title }}</p>
      <p class="boot-override-banner__body">{{ banner.message }}</p>
      <p class="boot-override-banner__reason">原因：{{ banner.reason }}</p>
    </div>

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

    <template v-else>
      <div data-region="editable" class="settings-region">
        <TermBox
          v-for="section in editableSections"
          :key="section.id"
          :title="`${section.title} · ${section.items.length}`"
          :hint="section.hint"
          pad="none"
          flush
        >
          <table class="term-table">
            <thead>
              <tr>
                <th style="width: 26%">設定</th>
                <th>現況</th>
                <th style="width: 30%">改成</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in section.items" :key="item.key">
                <td>
                  <div class="cell-strong">{{ item.key }}</div>
                  <div class="cell-meta">{{ item.description }}</div>
                  <div class="cell-meta">{{ item.env_name || '（只住在 DB，沒有 env 回退層）' }} · {{ item.value_type }}</div>
                </td>
                <td>
                  <span v-if="rowState(item)" class="setting-state" :class="rowState(item).className">
                    {{ rowState(item).label }}
                  </span>
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
                  <p v-else class="cell-meta">後端說這一顆不可編輯。</p>
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

      <!--
        唯讀區：這裡**一個控制項都不能有**。按了沒反應的儲存鈕，正是
        FAKE-CONTROLS 上那三十項的長相。護欄在 settingsOverview.test.mjs，
        它從 data-region="readonly" 一路切到 <script setup>。
      -->
      <div data-region="readonly" class="settings-region">
        <TermBox
          v-for="section in readonlySections"
          :key="section.id"
          :title="`${section.title} · ${section.items.length}`"
          :hint="section.hint"
          pad="none"
          flush
        >
          <table class="term-table">
            <thead>
              <tr>
                <th style="width: 26%">設定</th>
                <th>{{ section.showsValues ? '現況' : '設了沒有' }}</th>
                <th style="width: 34%">為什麼改不動</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in section.items" :key="item.key">
                <td>
                  <div class="cell-strong">{{ item.key }}</div>
                  <div class="cell-meta">{{ item.description }}</div>
                  <div class="cell-meta">{{ item.env_name || '（只住在 DB，沒有 env 回退層）' }} · {{ item.value_type }}</div>
                </td>
                <td v-if="section.showsValues">
                  <span v-if="rowState(item)" class="setting-state" :class="rowState(item).className">
                    {{ rowState(item).label }}
                  </span>
                  <dl class="setting-cells">
                    <div v-for="cell in valueCells(item)" :key="cell.field" class="setting-cell" :class="cell.className">
                      <dt>{{ cell.label }}</dt>
                      <dd>{{ cell.text }}</dd>
                    </div>
                  </dl>
                </td>
                <td v-else>
                  <span class="setting-state setting-state--isset">{{ isSetLabel(item) }}</span>
                  <div class="cell-meta">來源：{{ sourceLabel(item.source) }}</div>
                </td>
                <td>
                  <p v-if="lockedReasonText(item)" class="setting-locked">{{ lockedReasonText(item) }}</p>
                  <p v-else class="cell-meta">後端沒有給鎖定理由。</p>
                </td>
              </tr>
            </tbody>
          </table>
        </TermBox>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getPlatformSettingsOverview, updatePlatformSetting } from '../api/platformSettings'
import { TermBox, TermButton, TermEmpty } from '../components/cli'
import {
  bootOverrideBanner,
  canEdit,
  countMismatchWarning,
  draftValue,
  extractDetail,
  groupIntoSections,
  isSetLabel,
  lockedReasonText,
  overviewState,
  overviewStateMessage,
  replaceRow,
  rowState,
  saveNotice,
  sourceLabel,
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

const state = computed(() =>
  overviewState({ loaded: loaded.value, error: loadError.value, items: items.value }),
)
const banner = computed(() => bootOverrideBanner(overview.value))
const countWarning = computed(() => countMismatchWarning(overview.value))
const sections = computed(() => groupIntoSections(items.value))
const editableSections = computed(() => sections.value.filter((s) => s.editable))
const readonlySections = computed(() => sections.value.filter((s) => !s.editable))

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
  } catch (e) {
    // 讀不到就說讀不到。退化成一頁空的設定清單，等於告訴管理員「平台沒有設定」。
    loadError.value = extractDetail(e, overviewStateMessage('failed'))
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
    // 送的是使用者原原本本打的那個字串（沒有 trim、沒有轉型）——
    // 每一顆的解析規則在後端，前端先動手就是替它們發明第六種真值判準。
    const { data } = await updatePlatformSetting(item.key, drafts.value[item.key])
    // 整列取代：回應就是事實。merge 會讓回應裡消失的欄位留著舊值。
    items.value = replaceRow(items.value, data)
    drafts.value[item.key] = draftValue(data)
    delete errors.value[item.key]
    notices.value[item.key] = saveNotice(data)
  } catch (e) {
    delete notices.value[item.key]
    errors.value[item.key] = extractDetail(e, '儲存失敗')
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

/* 鎖定理由：**全文**。降級的那幾顆把 compose 自救路徑寫在句尾，
   截斷／省略號等於把管理員唯一的活路剪掉。 */
.setting-locked {
  color: var(--c-fg-2);
  font-size: var(--t-2xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 開機覆蓋 banner：大字、danger 色、站在版面最前面。 */
.boot-override-banner {
  border: var(--border-w) solid var(--c-danger);
  background: var(--c-danger-soft, transparent);
  padding: var(--gap-3) var(--gap-4);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.boot-override-banner__title { font-size: var(--t-lg); font-weight: 600; color: var(--c-danger); }
.boot-override-banner__body { font-size: var(--t-sm); color: var(--c-fg-1); }
.boot-override-banner__reason { font-size: var(--t-xs); color: var(--c-fg-2); overflow-wrap: anywhere; }

.settings-count-warning {
  border-left: 2px solid var(--c-warn);
  padding-left: var(--gap-3);
  font-size: var(--t-sm);
  color: var(--c-warn);
}

.settings-load-error__msg { font-size: var(--t-sm); color: var(--c-fg-1); margin-bottom: 4px; }
.settings-load-error__detail { font-size: var(--t-xs); color: var(--c-danger); margin-bottom: var(--gap-3); overflow-wrap: anywhere; }

/* 三態徽章：三個樣式必須看得出差別（顏色＋邊框，不只顏色）。 */
.setting-state {
  display: inline-block;
  font-size: var(--t-2xs);
  padding: 1px 6px;
  border: var(--border-w) solid var(--c-border-strong);
  border-radius: 2px;
  margin-bottom: 4px;
}
.setting-state--effective { color: var(--c-fg-1); border-color: var(--c-border-strong); }
.setting-state--pending { color: var(--c-warn); border-color: var(--c-warn); border-style: dashed; }
.setting-state--unusable { color: var(--c-danger); border-color: var(--c-danger); }
.setting-state--isset { color: var(--c-fg-3); }

.setting-cells { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 4px 10px; margin: 0; }
.setting-cell dt { font-size: var(--t-2xs); color: var(--c-fg-mute); }
.setting-cell dd { margin: 0; font-size: var(--t-xs); overflow-wrap: anywhere; }

/* 待生效與已生效**不可以長得一樣**（本計畫的殺形）。 */
.setting-cell--effective dd { color: var(--c-fg-1); font-weight: 500; }
.setting-cell--pending dd { color: var(--c-warn); font-weight: 500; border-bottom: 1px dashed var(--c-warn); }
.setting-cell--empty dd { color: var(--c-fg-mute); }
.setting-cell--stored dd { color: var(--c-fg-2); }
.setting-cell--unusable dd { color: var(--c-danger); text-decoration: line-through; }
.setting-cell--default dd { color: var(--c-fg-3); }
.setting-cell--source dd { color: var(--c-fg-3); }

.setting-editor { display: flex; gap: 6px; align-items: center; }
.setting-editor .term-input { flex: 1; min-width: 0; }
.setting-notice { font-size: var(--t-2xs); margin-top: 4px; overflow-wrap: anywhere; }
.setting-notice--warn { color: var(--c-warn); }
.setting-notice--ok { color: var(--c-fg-2); }
.setting-error { font-size: var(--t-2xs); margin-top: 4px; color: var(--c-danger); overflow-wrap: anywhere; }
</style>
