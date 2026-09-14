<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">使用者回饋</h1>
        <p class="page-head__sub">
          正讚與倒讚都會進這份清單 · 評分 6–10 為好評、1–5 為差評 · 可依評分 / agent / 模型 / 時間篩選 · 不含對話正文
        </p>
      </div>
      <div class="page-head__chips">
        <TermBadge variant="danger" dot>差評 · {{ summary.down }}</TermBadge>
        <TermBadge variant="ok" dot>好評 · {{ summary.up }}</TermBadge>
        <TermBadge dot>有留言 · {{ summary.with_comment }}</TermBadge>
        <TermBadge variant="warn" dot title="時間窗內回覆看起來像拒答（安全拒答或助手表示超出範圍）的則數，跟著 agent／模型篩選但不看評分；只計數，不會擋回覆">疑似拒答 · {{ summary.refusal_suspected ?? 0 }}</TermBadge>
      </div>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox title="篩選" pad="sm" hint="預設近 7 天 · 好評與差評都列出">
      <div class="filters">
        <TermField label="評分">
          <select v-model="filters.rating" @change="fetchData" class="term-select">
            <option value="">全部</option>
            <option value="up">好評</option>
            <option value="down">差評</option>
          </select>
        </TermField>
        <TermField label="天數">
          <select v-model.number="filters.days" @change="fetchData" class="term-select">
            <option :value="1">近 1 天</option>
            <option :value="7">近 7 天</option>
            <option :value="30">近 30 天</option>
            <option :value="90">近 90 天</option>
          </select>
        </TermField>
        <TermField label="Agent">
          <input
            v-model="filters.agent_name"
            @keyup.enter="fetchData"
            class="term-input"
            placeholder="精確名稱"
          />
        </TermField>
        <TermField label="模型">
          <input
            v-model="filters.model_name"
            @keyup.enter="fetchData"
            class="term-input"
            placeholder="精確名稱"
          />
        </TermField>
        <TermField label="留言">
          <select v-model="filters.only_with_comment" @change="fetchData" class="term-select">
            <option value="0">全部</option>
            <option value="1">只看有留言</option>
          </select>
        </TermField>
        <div class="filters__cta">
          <TermButton @click="fetchData" label="查詢" />
        </div>
      </div>
      <div class="filters__export">
        <TermButton
          @click="downloadCsv"
          label="匯出 CSV(目前篩選)"
          :disabled="exporting"
          title="依畫面上目前的篩選條件匯出整個結果(不只這一頁),不含對話正文"
        />
        <span class="filters__note">
          匯出的是「目前這組篩選」的完整結果,不是畫面上這一頁;筆數超過後端上限時會直接說出來,不會給一份被砍短的檔案。
        </span>
      </div>
      <p v-if="exportNote" class="filters__note filters__note--ok">{{ exportNote }}</p>
    </TermBox>

    <TermBox :title="`回饋 · ${items.length}`" pad="none" flush hint="正文請走對話讀取路徑(會落稽核)">
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 72px">評分</th>
            <th style="width: 110px">分數（好評 6–10／差評 1–5）</th>
            <th>留言 / 原因</th>
            <th style="width: 18%">Agent · 模型</th>
            <th style="width: 100px">密等</th>
            <th style="width: 140px">訊息時間</th>
            <th style="width: 120px">對話</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.message_id">
            <td>
              <TermBadge :variant="row.rating === 'down' ? 'danger' : 'ok'" dot>
                {{ row.rating === 'down' ? '差評' : '好評' }}
              </TermBadge>
            </td>
            <td class="cell-meta tnum">
              {{ row.rating_score == null ? '—' : row.rating_score }}
            </td>
            <td>
              <div class="cell-strong">{{ row.comment || '（無文字留言）' }}</div>
              <div v-if="row.reasons?.length" class="cell-meta cell-meta--wrap">
                {{ row.reasons.join(' · ') }}
              </div>
              <div v-if="row.username" class="cell-meta">by {{ row.username }}</div>
            </td>
            <td>
              <div class="cell-strong">{{ row.agent_name || '—' }}</div>
              <div class="cell-meta">{{ row.model_name || '—' }}</div>
            </td>
            <td>
              <TermBadge :variant="levelTone(row.classification_level)" dot>
                {{ row.classification_level }}
              </TermBadge>
            </td>
            <td class="cell-meta tnum">{{ formatDate(row.message_created_at) }}</td>
            <td class="cell-meta tnum">#{{ row.conversation_id }} / msg {{ row.message_id }}</td>
          </tr>
          <tr v-if="items.length === 0">
            <td colspan="7">
              <TermEmpty message="這個篩選區間沒有回饋 — 若剛上線屬正常" />
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listFeedback, exportFeedbackCsv } from '../api/feedback'
import { extractError } from '../api/errors'
import { formatDate } from '../utils/formatDate'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty } from '../components/cli'

const items = ref([])
const summary = ref({ total: 0, up: 0, down: 0, with_comment: 0 })
const pageError = ref('')
const exporting = ref(false)
const exportNote = ref('')
const filters = ref({
  rating: '',
  days: 7,
  agent_name: '',
  model_name: '',
  only_with_comment: '0',
})

// 查詢與匯出共用同一組參數 —— 匯出「畫面上的東西」不能靠兩份各自維護的
// 組裝邏輯,那遲早會漂移成「匯出的跟看到的不同」。
function buildParams() {
  const params = {
    days: filters.value.days,
    only_with_comment: filters.value.only_with_comment === '1',
  }
  if (filters.value.rating) params.rating = filters.value.rating
  if (filters.value.agent_name.trim()) params.agent_name = filters.value.agent_name.trim()
  if (filters.value.model_name.trim()) params.model_name = filters.value.model_name.trim()
  return params
}

async function fetchData() {
  pageError.value = ''
  try {
    const params = buildParams()
    const { data } = await listFeedback(params)
    items.value = Array.isArray(data.items) ? data.items : []
    summary.value = data.summary || { total: 0, up: 0, down: 0, with_comment: 0 }
  } catch (e) {
    pageError.value = extractError(e, '載入回饋失敗')
  }
}

// 匯出走 responseType: 'blob',錯誤 body 也會是 Blob。不解開的話,後端那句
// 「超過上限、沒有匯出」會被顯示成通用失敗,維運者根本不知道要縮篩選。
async function blobError(err, fallback) {
  const data = err?.response?.data
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text())
      return extractError({ ...err, response: { ...err.response, data: parsed } }, fallback)
    } catch {
      return fallback
    }
  }
  return extractError(err, fallback)
}

async function downloadCsv() {
  pageError.value = ''
  exportNote.value = ''
  exporting.value = true
  try {
    const { data, headers } = await exportFeedbackCsv(buildParams())
    const blob = data instanceof Blob ? data : new Blob([data], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `feedback-${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    const rows = headers?.['x-feedback-export-rows']
    exportNote.value = rows ? `已匯出 ${rows} 列(目前篩選的完整結果)` : '已匯出目前篩選的完整結果'
  } catch (e) {
    pageError.value = await blobError(e, '匯出 CSV 失敗')
  } finally {
    exporting.value = false
  }
}

function levelTone(level) {
  if (level === '機密' || level === '密') return 'danger'
  if (level === '營業秘密') return 'warn'
  return ''
}

onMounted(fetchData)
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); }
.page-head {
  display: flex; justify-content: space-between; align-items: flex-end;
  gap: var(--gap-3); flex-wrap: wrap;
}
.page-head__title { margin: 0; font-size: var(--t-2xl); font-weight: 600; color: var(--c-fg-1); }
.page-head__sub { margin: 4px 0 0; font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__chips { display: inline-flex; gap: var(--gap-2); flex-wrap: wrap; }
.filters {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--gap-2);
  align-items: end;
}
@media (max-width: 1100px) { .filters { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 700px) { .filters { grid-template-columns: 1fr; } }
.filters__cta { display: flex; align-items: end; }
.filters__export {
  display: flex; align-items: center; gap: var(--gap-2);
  flex-wrap: wrap; margin-top: var(--gap-3);
}
.filters__note { margin: var(--gap-2) 0 0; font-size: var(--t-3xs); color: var(--c-fg-3); }
.filters__export .filters__note { margin: 0; }
.filters__note--ok { color: var(--c-fg-2); }
.feedback.is-err {
  font-size: var(--t-xs); color: var(--c-danger);
  border: var(--border-w) solid var(--c-danger); background: var(--c-danger-soft);
  padding: var(--gap-2) var(--gap-3);
}
.term-table { width: 100%; border-collapse: collapse; font-size: var(--t-2xs); }
.term-table th {
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--c-divider);
  font-weight: 500; color: var(--c-fg-2);
  font-size: var(--t-3xs); text-transform: uppercase; letter-spacing: 0.04em;
}
.term-table td { padding: 8px; border-bottom: 1px solid var(--c-divider); vertical-align: top; }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-2); font-size: var(--t-3xs); margin-top: 2px; }
.cell-meta--wrap { white-space: normal; }
.tnum { font-variant-numeric: tabular-nums; }
</style>
