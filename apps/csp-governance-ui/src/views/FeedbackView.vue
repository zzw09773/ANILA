<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">使用者回饋</h1>
        <p class="page-head__sub">
          早上掃差評與留言 · 依 agent / 模型 / 時間篩 · 不含對話正文
        </p>
      </div>
      <div class="page-head__chips">
        <TermBadge variant="danger" dot>爛 · {{ summary.down }}</TermBadge>
        <TermBadge variant="ok" dot>讚 · {{ summary.up }}</TermBadge>
        <TermBadge dot>有留言 · {{ summary.with_comment }}</TermBadge>
      </div>
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox title="篩選" pad="sm" hint="預設近 7 天 · 差評優先可改">
      <div class="filters">
        <TermField label="評分">
          <select v-model="filters.rating" @change="fetchData" class="term-select">
            <option value="down">爛</option>
            <option value="up">讚</option>
            <option value="">全部</option>
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
    </TermBox>

    <TermBox :title="`回饋 · ${items.length}`" pad="none" flush hint="正文請走對話讀取路徑(會落稽核)">
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 72px">評分</th>
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
                {{ row.rating === 'down' ? '爛' : '讚' }}
              </TermBadge>
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
            <td colspan="6">
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
import { listFeedback } from '../api/feedback'
import { extractError } from '../api/errors'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty } from '../components/cli'

const items = ref([])
const summary = ref({ total: 0, up: 0, down: 0, with_comment: 0 })
const pageError = ref('')
const filters = ref({
  rating: 'down',
  days: 7,
  agent_name: '',
  model_name: '',
  only_with_comment: '0',
})

async function fetchData() {
  pageError.value = ''
  try {
    const params = {
      days: filters.value.days,
      only_with_comment: filters.value.only_with_comment === '1',
    }
    if (filters.value.rating) params.rating = filters.value.rating
    if (filters.value.agent_name.trim()) params.agent_name = filters.value.agent_name.trim()
    if (filters.value.model_name.trim()) params.model_name = filters.value.model_name.trim()
    const { data } = await listFeedback(params)
    items.value = Array.isArray(data.items) ? data.items : []
    summary.value = data.summary || { total: 0, up: 0, down: 0, with_comment: 0 }
  } catch (e) {
    pageError.value = extractError(e, '載入回饋失敗')
  }
}

function formatDate(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toISOString().replace('T', ' ').slice(0, 19)
  } catch {
    return String(iso)
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
