<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">分類盤點</h1>
        <p class="page-head__sub">
          列出各資源目前的分類等級。若「不一致」不是 0，代表舊密等標記與現行等級不符，請匯出清單交由權責人處理。
        </p>
      </div>
      <span class="cell-meta" v-if="generatedAt">產生於 {{ formatDate(generatedAt) }}</span>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <div class="toolbar">
      <TermButton @click="fetchInventory" label="重新整理" />
      <TermButton @click="downloadCsv" label="下載 CSV" :disabled="downloading" />
      <span class="cell-meta">{{ resources.length }} 個資源類型</span>
    </div>

    <p v-if="inconsistentTotal" class="feedback is-warn">
      有 {{ inconsistentTotal }} 筆舊密等標記與現行等級不符。請先匯出清單，
      由密等權責人確認資料歸屬後再處理，系統不會自行變更密等。
    </p>

    <TermBox title="盤點" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>資源類型</th>
            <th v-for="level in LEVELS" :key="level" class="num">{{ level }}</th>
            <th class="num">已鎖定</th>
            <th class="num">不一致</th>
            <th class="num">總計</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in resources"
            :key="row.resource_type"
            :class="{ 'is-inconsistent': row.inconsistent > 0 }"
          >
            <td class="cell-strong">{{ row.resource_type }}</td>
            <td v-for="level in LEVELS" :key="level" class="num tnum">
              {{ row.levels?.[level] ?? 0 }}
            </td>
            <td class="num tnum">{{ row.latched }}</td>
            <td class="num tnum">
              <TermBadge v-if="row.inconsistent > 0" variant="danger" dot>{{ row.inconsistent }}</TermBadge>
              <span v-else>{{ row.inconsistent }}</span>
            </td>
            <td class="num tnum cell-strong">{{ row.total }}</td>
          </tr>
          <tr v-if="resources.length === 0">
            <td :colspan="LEVELS.length + 4"><TermEmpty message="尚無盤點資料" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { extractError } from '../api/errors'
import {
  getClassificationInventory,
  downloadClassificationInventoryCsv,
} from '../api/classificationInventory'
import { TermBox, TermButton, TermBadge, TermEmpty } from '../components/cli'
import { formatDate } from '../utils/formatDate'

// 四級順序(對齊後端 ClassificationLevel 契約宣告順序)。
const LEVELS = ['無機密', '營業秘密', '密', '機密']

const resources = ref([])
const generatedAt = ref('')
const pageError = ref('')
const downloading = ref(false)
const inconsistentTotal = computed(() =>
  resources.value.reduce((sum, row) => sum + (row.inconsistent || 0), 0),
)

async function fetchInventory() {
  pageError.value = ''
  try {
    const { data } = await getClassificationInventory()
    resources.value = data.resources || []
    generatedAt.value = data.generated_at || ''
  } catch (e) {
    pageError.value = extractError(e, '載入分類盤點失敗')
  }
}

async function downloadCsv() {
  pageError.value = ''
  downloading.value = true
  try {
    const { data } = await downloadClassificationInventoryCsv()
    const url = URL.createObjectURL(new Blob([data], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'classification-inventory.csv'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    pageError.value = extractError(e, '下載 CSV 失敗')
  } finally {
    downloading.value = false
  }
}

onMounted(fetchInventory)
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); max-width: 64ch; }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.feedback.is-warn { color: var(--c-warn); border-color: var(--c-warn); background: var(--c-warn-soft); }

.toolbar { display: flex; align-items: center; gap: var(--gap-3); flex-wrap: wrap; }

.num { text-align: right; }
.tnum { font-variant-numeric: tabular-nums; }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

/* 不一致列以警示色標示(inconsistent > 0)。 */
.is-inconsistent { background: var(--c-danger-soft); }
.is-inconsistent .cell-strong { color: var(--c-danger); }
</style>
