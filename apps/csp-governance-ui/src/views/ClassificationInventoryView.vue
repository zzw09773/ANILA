<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">分類盤點</h1>
        <p class="page-head__sub">
          切換五級分類前的資源盤點快照。「不一致」= 舊 latch 為真但等級仍低於「機密」,
          backfill 完成後應為 0；無舊 boolean 可比對的資源顯示「不適用」。
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

    <TermBox title="盤點" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>資源類型</th>
            <th>說明 / 管理面</th>
            <th v-for="level in LEVELS" :key="level" class="num">{{ level }}</th>
            <th class="num">已閂鎖</th>
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
            <td class="cell-strong">
              {{ row.resource_type }}
              <span v-if="row.ceiling" class="cell-meta">（上限許可）</span>
            </td>
            <td class="cell-desc">
              <div>{{ row.description || '—' }}</div>
              <router-link
                v-if="row.manage_path"
                :to="row.manage_path"
                class="term-action"
              >→ 前往管理面</router-link>
              <span v-else class="cell-meta">系統自動治理，無人工管理面</span>
            </td>
            <td v-for="level in LEVELS" :key="level" class="num tnum">
              {{ row.levels?.[level] ?? 0 }}
            </td>
            <td class="num tnum">{{ row.latched }}</td>
            <td class="num tnum">
              <span v-if="row.inconsistent == null" class="cell-meta">不適用</span>
              <TermBadge v-else-if="row.inconsistent > 0" variant="danger" dot>{{ row.inconsistent }}</TermBadge>
              <span v-else>{{ row.inconsistent }}</span>
            </td>
            <td class="num tnum cell-strong">{{ row.total }}</td>
          </tr>
          <tr v-if="resources.length === 0">
            <td :colspan="LEVELS.length + 5"><TermEmpty message="尚無盤點資料" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import {
  getClassificationInventory,
  downloadClassificationInventoryCsv,
} from '../api/classificationInventory'
import { TermBox, TermButton, TermBadge, TermEmpty } from '../components/cli'

// 五級順序(對齊後端 ClassificationLevel 契約宣告順序)。
const LEVELS = ['無機密', '營業秘密', '機密', '極機密', '絕對機密']

const resources = ref([])
const generatedAt = ref('')
const pageError = ref('')
const downloading = ref(false)

async function fetchInventory() {
  pageError.value = ''
  try {
    const { data } = await getClassificationInventory()
    resources.value = data.resources || []
    generatedAt.value = data.generated_at || ''
  } catch (e) {
    pageError.value = e.response?.data?.detail || '載入分類盤點失敗'
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
    pageError.value = e.response?.data?.detail || '下載 CSV 失敗'
  } finally {
    downloading.value = false
  }
}

function formatDate(v) {
  return new Date(v).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei' })
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

.toolbar { display: flex; align-items: center; gap: var(--gap-3); flex-wrap: wrap; }

.num { text-align: right; }
.tnum { font-variant-numeric: tabular-nums; }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-desc { max-width: 28ch; font-size: var(--t-xs); color: var(--c-fg-2); }
.cell-desc .term-action { display: inline-block; margin-top: 4px; }

/* 不一致列以警示色標示(inconsistent > 0)。 */
.is-inconsistent { background: var(--c-danger-soft); }
.is-inconsistent .cell-strong { color: var(--c-danger); }
</style>
