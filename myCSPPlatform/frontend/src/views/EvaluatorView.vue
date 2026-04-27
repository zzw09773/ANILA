<template>
  <div class="ev-root">
    <header class="page-header">
      <router-link :to="{ name: 'CollectionDetail', params: { id: collectionId } }" class="back">
        ← 回到 collection
      </router-link>
      <h1>🧪 Chunking Evaluator</h1>
      <p v-if="collection" class="subtitle">
        collection #{{ collection.id }} · {{ collection.name }} ·
        agent #{{ collection.agent_id }}
      </p>
    </header>

    <div class="layout">
      <!-- Wizard pane -->
      <section class="wizard">
        <header class="wizard-head">
          <h2>新建 evaluation run</h2>
          <ol class="step-pills">
            <li :class="{ active: step === 1, done: step > 1 }">1. Sample documents</li>
            <li :class="{ active: step === 2, done: step > 2 }">2. Eval queries</li>
            <li :class="{ active: step === 3, done: step > 3 }">3. Strategies</li>
            <li :class="{ active: step === 4, done: step > 4 }">4. Judge LLM <span class="muted">(optional)</span></li>
            <li :class="{ active: step === 5 }">5. Confirm</li>
          </ol>
        </header>

        <!-- Step 1 -->
        <div v-if="step === 1">
          <p class="hint">挑要評的 documents（必須先 indexed）。建議 5–10 篇 representative。</p>
          <div v-if="loadingDocs" class="banner muted">載入中…</div>
          <ul v-else class="doc-pick">
            <li v-for="d in indexedDocs" :key="d.id">
              <label>
                <input type="checkbox" :value="d.id" v-model="form.sample_document_ids" />
                <span class="filename">{{ d.filename }}</span>
                <span class="muted">{{ d.chunk_count }} chunks · {{ humanBytes(d.bytes) }}</span>
              </label>
            </li>
          </ul>
          <div class="step-actions">
            <button class="primary" :disabled="form.sample_document_ids.length === 0" @click="step = 2">下一步</button>
          </div>
        </div>

        <!-- Step 2 -->
        <div v-if="step === 2">
          <p class="hint">寫 (query, expected document) 對。Hit@k / MRR 用這份 golden set 評分。</p>
          <table class="query-grid">
            <thead><tr><th>Query</th><th>Expected document</th><th></th></tr></thead>
            <tbody>
              <tr v-for="(q, i) in form.queries" :key="i">
                <td><input v-model.trim="q.query" placeholder="例如：第八條的內容是什麼" /></td>
                <td>
                  <select v-model.number="q.expected_doc_id">
                    <option :value="0" disabled>— 選 doc —</option>
                    <option v-for="d in pickedDocs" :key="d.id" :value="d.id">
                      {{ d.filename }} (#{{ d.id }})
                    </option>
                  </select>
                </td>
                <td><button class="ghost small" @click="form.queries.splice(i, 1)">×</button></td>
              </tr>
            </tbody>
          </table>
          <button class="ghost small" @click="addQuery">＋ 新增 query</button>
          <div class="step-actions">
            <button class="ghost" @click="step = 1">上一步</button>
            <button class="primary" :disabled="!validQueries" @click="step = 3">下一步</button>
          </div>
        </div>

        <!-- Step 3 -->
        <div v-if="step === 3">
          <p class="hint">挑要 benchmark 的 strategies（≥ 2 才有比較意義）。</p>
          <ul class="strategy-pick">
            <li v-for="s in availableStrategies" :key="s.name">
              <label>
                <input type="checkbox" :value="s" v-model="pickedStrategies" />
                <span class="strategy-label">{{ s.label }}</span>
                <span class="muted">{{ s.note }}</span>
              </label>
            </li>
          </ul>
          <div class="step-actions">
            <button class="ghost" @click="step = 2">上一步</button>
            <button class="primary" :disabled="pickedStrategies.length < 1" @click="step = 4">下一步</button>
          </div>
        </div>

        <!-- Step 4 — Judge LLM (optional) -->
        <div v-if="step === 4">
          <p class="hint">
            選一個 user-owned LLM credential 來跑 LLM-as-judge。Judge 會對每個
            (query, top-k chunks) 給 1–3 分（1=答非所問，3=直接命中），平均到
            <code>judge_avg</code>。<strong>這是可選的</strong> — 跳過就只跑 Hit@k / MRR。
          </p>
          <div v-if="loadingCredentials" class="banner muted">載入 credentials…</div>
          <div v-else>
            <div v-if="credentials.length === 0" class="banner muted">
              尚未建立任何 LLM credential — 點下方「＋ 新增 credential」加一個（例如自家 OpenAI key），或直接跳過此步。
            </div>
            <label v-else class="field">
              <span>Judge credential</span>
              <div class="cred-row">
                <select v-model.number="form.judge_credential_id" class="cred-select">
                  <option :value="null">— 不用 judge（只跑 Hit@k / MRR）—</option>
                  <option v-for="c in credentials" :key="c.id" :value="c.id">
                    {{ c.name }} · {{ c.model_name }}
                  </option>
                </select>
                <button
                  v-if="form.judge_credential_id"
                  class="ghost small"
                  type="button"
                  :disabled="deletingCredentialId === form.judge_credential_id"
                  @click="onDeleteCredential(form.judge_credential_id)"
                >
                  {{ deletingCredentialId === form.judge_credential_id ? '刪除中…' : '🗑 刪除' }}
                </button>
              </div>
            </label>

            <button
              v-if="!showCredentialForm"
              class="ghost small"
              type="button"
              @click="showCredentialForm = true"
            >
              ＋ 新增 credential
            </button>

            <div v-else class="cred-form">
              <h4>新增 LLM credential</h4>
              <label class="field">
                <span>Name (給自己看的標籤)</span>
                <input v-model.trim="newCredential.name" placeholder="openai-judge" />
              </label>
              <label class="field">
                <span>Endpoint URL</span>
                <input v-model.trim="newCredential.endpoint_url" placeholder="https://api.openai.com/v1" />
              </label>
              <label class="field">
                <span>Model name</span>
                <input v-model.trim="newCredential.model_name" placeholder="gpt-4o-mini" />
              </label>
              <label class="field">
                <span>API key</span>
                <input v-model.trim="newCredential.api_key" type="password" placeholder="sk-..." />
                <span class="muted">送出後 AES-GCM 加密落 DB；之後讀不出 plaintext，要換 key 請刪掉重建。</span>
              </label>
              <div class="cred-form-actions">
                <button class="ghost small" type="button" @click="cancelCredentialForm">取消</button>
                <button
                  class="primary small"
                  type="button"
                  :disabled="!validNewCredential || creatingCredential"
                  @click="onCreateCredential"
                >
                  {{ creatingCredential ? '送出中…' : '✔ 建立並選用' }}
                </button>
              </div>
              <div v-if="credentialError" class="banner error inline">{{ credentialError }}</div>
            </div>
          </div>

          <label v-if="form.judge_credential_id" class="field">
            <span>Top-k chunks per query (judge 看幾個 chunks)</span>
            <input type="number" min="1" max="20" v-model.number="form.judge_top_k" />
          </label>
          <p v-if="form.judge_credential_id" class="hint muted">
            ⚠ Judge 走你的 credential，計費由 LLM provider 直接收，不在 CSP 的 token_usage 裡。
          </p>
          <div class="step-actions">
            <button class="ghost" @click="step = 3">上一步</button>
            <button class="primary" @click="step = 5">下一步</button>
          </div>
        </div>

        <!-- Step 5 — Confirm -->
        <div v-if="step === 5">
          <p class="hint">確認後 enqueue。Run 結束後右側會出現結果。</p>
          <label class="field">
            <span>Run name</span>
            <input v-model.trim="form.name" placeholder="2026-04-25 baseline" />
          </label>
          <dl class="confirm">
            <div><dt>Documents</dt><dd>{{ form.sample_document_ids.length }} 篇</dd></div>
            <div><dt>Queries</dt><dd>{{ form.queries.length }} 條</dd></div>
            <div><dt>Strategies</dt><dd>{{ pickedStrategies.map(s => s.name).join(', ') }}</dd></div>
            <div>
              <dt>Judge LLM</dt>
              <dd>
                <span v-if="!form.judge_credential_id" class="muted">— 不啟用 —</span>
                <span v-else>
                  {{ selectedCredentialLabel }}
                  · top-{{ form.judge_top_k }}
                </span>
              </dd>
            </div>
          </dl>
          <div class="step-actions">
            <button class="ghost" @click="step = 4">上一步</button>
            <button
              class="primary"
              :disabled="!form.name || submitting"
              @click="submit"
            >
              {{ submitting ? '送出中…' : '🚀 啟動 evaluation' }}
            </button>
          </div>
          <div v-if="submitError" class="banner error inline">{{ submitError }}</div>
        </div>
      </section>

      <!-- Results pane -->
      <section class="results">
        <h2>📊 最近 runs</h2>
        <div v-if="runs.length === 0" class="banner muted">尚無 evaluation runs。</div>
        <ul v-else class="run-list">
          <li v-for="r in runs" :key="r.id" :class="{ selected: selectedRun?.id === r.id }">
            <button class="run-row" @click="selectedRun = r">
              <strong>{{ r.name }}</strong>
              <span class="badge" :class="r.status">{{ r.status }}</span>
              <span class="muted">{{ r.strategies_tried.length }} strategies · {{ r.queries.length }} queries</span>
            </button>
          </li>
        </ul>

        <article v-if="selectedRun" class="run-detail">
          <h3>{{ selectedRun.name }}</h3>
          <p v-if="selectedRun.status !== 'succeeded'" class="banner muted">
            status: {{ selectedRun.status }}
            <span v-if="selectedRun.error_message"> — {{ selectedRun.error_message }}</span>
          </p>

          <div v-if="selectedRun.results">
            <p>
              ⏱ {{ selectedRun.results.elapsed_seconds }}s ·
              {{ selectedRun.results.n_docs }} docs ·
              {{ selectedRun.results.n_queries }} queries ·
              <strong>recommended:</strong>
              <code>{{ selectedRun.recommended_strategy || '—' }}</code>
            </p>
            <p v-if="selectedRun.results.judge_load_error" class="banner error inline">
              ⚠ Judge credential 載入失敗（{{ selectedRun.results.judge_load_error }}）— 已跳過 judge 評分。請檢查 credential 是否被刪除、key 是否被輪替。
            </p>
            <table class="metrics">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Hit@1</th>
                  <th>Hit@5</th>
                  <th>MRR</th>
                  <th title="LLM-as-judge 1–3 平均，僅當 judge 啟用">Judge avg</th>
                  <th>Chunks/doc</th>
                  <th>Avg tokens</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(metrics, name) in selectedRun.results.per_strategy"
                  :key="name"
                  :class="{ best: name === selectedRun.recommended_strategy }"
                >
                  <td><code>{{ name }}</code></td>
                  <template v-if="metrics.error">
                    <td colspan="6" class="err">⚠ {{ metrics.error }}</td>
                  </template>
                  <template v-else>
                    <td>{{ formatPct(metrics.hit_at_1) }}</td>
                    <td>{{ formatPct(metrics.hit_at_5) }}</td>
                    <td>{{ metrics.mrr.toFixed(3) }}</td>
                    <td>
                      <span v-if="metrics.judge_avg != null">
                        {{ metrics.judge_avg.toFixed(2) }}
                        <span class="muted">/3 (n={{ metrics.judge_n_scored }})</span>
                      </span>
                      <span v-else class="muted">—</span>
                    </td>
                    <td>{{ metrics.chunks_per_doc }}</td>
                    <td>{{ metrics.avg_chunk_tokens }}</td>
                  </template>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getCollection } from '../api/ingestionCollections'
import { listDocuments } from '../api/ingestionDocuments'
import { createEvalRun, getEvalRun, listEvalRuns } from '../api/ingestionEvalRuns'
import {
  createLlmCredential,
  deleteLlmCredential,
  listLlmCredentials,
} from '../api/ingestionLlmCredentials'

const route = useRoute()
const collectionId = ref(Number(route.params.id))

const collection = ref(null)
const documents = ref([])
const loadingDocs = ref(false)

const indexedDocs = computed(() =>
  documents.value.filter((d) => d.status === 'indexed'),
)
const pickedDocs = computed(() =>
  indexedDocs.value.filter((d) => form.value.sample_document_ids.includes(d.id)),
)

const step = ref(1)
const form = ref({
  name: `eval-${new Date().toISOString().slice(0, 10)}`,
  sample_document_ids: [],
  queries: [{ query: '', expected_doc_id: 0 }],
  strategies_tried: [],
  // Sprint 5 X — optional LLM-as-judge.
  judge_credential_id: null,
  judge_top_k: 5,
})
const submitting = ref(false)
const submitError = ref('')

const credentials = ref([])
const loadingCredentials = ref(false)
const selectedCredentialLabel = computed(() => {
  const c = credentials.value.find((x) => x.id === form.value.judge_credential_id)
  return c ? `${c.name} · ${c.model_name}` : ''
})

// Inline "+ 新增 credential" form state — lets the user create a judge
// credential without leaving the wizard. POST returns no plaintext key,
// so the form's api_key field is only used at creation time.
const showCredentialForm = ref(false)
const creatingCredential = ref(false)
const deletingCredentialId = ref(null)
const credentialError = ref('')
const newCredential = ref({
  name: '',
  endpoint_url: '',
  model_name: '',
  api_key: '',
})
const validNewCredential = computed(() =>
  Boolean(
    newCredential.value.name &&
    newCredential.value.endpoint_url &&
    newCredential.value.model_name &&
    newCredential.value.api_key,
  ),
)

function cancelCredentialForm() {
  showCredentialForm.value = false
  credentialError.value = ''
  newCredential.value = { name: '', endpoint_url: '', model_name: '', api_key: '' }
}

async function onCreateCredential() {
  creatingCredential.value = true
  credentialError.value = ''
  try {
    const { data } = await createLlmCredential({ ...newCredential.value })
    credentials.value = [...credentials.value, data]
    form.value.judge_credential_id = data.id
    cancelCredentialForm()
  } catch (e) {
    credentialError.value = e.response?.data?.detail || e.message
  } finally {
    creatingCredential.value = false
  }
}

async function onDeleteCredential(credentialId) {
  if (!window.confirm('刪除此 credential? 無法復原（key 不可解密）。')) return
  deletingCredentialId.value = credentialId
  try {
    await deleteLlmCredential(credentialId)
    credentials.value = credentials.value.filter((c) => c.id !== credentialId)
    if (form.value.judge_credential_id === credentialId) {
      form.value.judge_credential_id = null
    }
  } catch (e) {
    credentialError.value = e.response?.data?.detail || e.message
  } finally {
    deletingCredentialId.value = null
  }
}

const availableStrategies = [
  { name: 'hierarchical', label: 'hierarchical', params: { max_leaf_tokens: 1024 }, note: 'heading 樹 + ancestor context' },
  { name: 'fixed', label: 'fixed', params: { size: 1024, overlap: 128 }, note: 'token-budget windowing' },
  { name: 'markdown-aware', label: 'markdown-aware', params: { max_leaf_tokens: 1024 }, note: 'heading + code-fence safe' },
  { name: 'pdf-page', label: 'pdf-page', params: { max_page_tokens: 4096 }, note: 'PDF page boundaries（PDF only）' },
  { name: 'cjk-sentence', label: 'cjk-sentence', params: { target_tokens: 512 }, note: 'CJK 句法 + token merge' },
  { name: 'semantic', label: 'semantic', params: { breakpoint_percentile: 80 }, note: 'embedding distance（昂貴）' },
]
const pickedStrategies = ref([])

const runs = ref([])
const selectedRun = ref(null)
let pollTimer = null

// ── Wizard validation ──────────────────────────────────────────────────────

const validQueries = computed(() =>
  form.value.queries.length > 0 &&
  form.value.queries.every((q) => q.query && q.expected_doc_id > 0),
)

function addQuery() {
  form.value.queries.push({ query: '', expected_doc_id: 0 })
}

// ── Lifecycle ──────────────────────────────────────────────────────────────

onMounted(async () => {
  loadingDocs.value = true
  loadingCredentials.value = true
  try {
    const [coll, docs, list, creds] = await Promise.all([
      getCollection(collectionId.value),
      listDocuments(collectionId.value),
      listEvalRuns({ collection_id: collectionId.value }),
      listLlmCredentials().catch(() => ({ data: [] })), // soft-fail; user just won't see judge picker
    ])
    collection.value = coll.data
    documents.value = docs.data
    runs.value = list.data
    credentials.value = creds.data
    if (list.data.length > 0) selectedRun.value = list.data[0]
  } finally {
    loadingDocs.value = false
    loadingCredentials.value = false
  }
  startPolling()
})

function startPolling() {
  pollTimer = setInterval(async () => {
    const inFlight = runs.value.some(
      (r) => !['succeeded', 'failed', 'cancelled'].includes(r.status),
    )
    if (!inFlight && selectedRun.value && ['succeeded', 'failed'].includes(selectedRun.value.status)) {
      return
    }
    try {
      const { data } = await listEvalRuns({ collection_id: collectionId.value })
      runs.value = data
      if (selectedRun.value) {
        const refreshed = data.find((r) => r.id === selectedRun.value.id)
        if (refreshed) selectedRun.value = refreshed
      }
    } catch { /* transient */ }
  }, 2500)
}

watch(() => route.params.id, (id) => {
  if (id) {
    collectionId.value = Number(id)
  }
})

import { onUnmounted } from 'vue'
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

// ── Submit ─────────────────────────────────────────────────────────────────

async function submit() {
  submitting.value = true
  submitError.value = ''
  try {
    const payload = {
      collection_id: collectionId.value,
      name: form.value.name,
      sample_document_ids: [...form.value.sample_document_ids],
      strategies_tried: pickedStrategies.value.map((s) => ({
        name: s.name, params: s.params,
      })),
      queries: form.value.queries.map((q) => ({
        query: q.query, expected_doc_id: q.expected_doc_id,
      })),
      judge_credential_id: form.value.judge_credential_id || null,
      judge_top_k: form.value.judge_top_k,
    }
    const { data } = await createEvalRun(payload)
    runs.value.unshift(data)
    selectedRun.value = data
    // Reset wizard for the next run.
    step.value = 1
    form.value = {
      name: `eval-${new Date().toISOString().slice(0, 10)}`,
      sample_document_ids: [],
      queries: [{ query: '', expected_doc_id: 0 }],
      strategies_tried: [],
      judge_credential_id: null,
      judge_top_k: 5,
    }
    pickedStrategies.value = []
  } catch (e) {
    submitError.value = e.response?.data?.detail || e.message
  } finally {
    submitting.value = false
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

function humanBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = Number(n), u = 0
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u += 1 }
  return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`
}

function formatPct(v) {
  return `${(v * 100).toFixed(1)}%`
}
</script>

<style scoped>
.ev-root { padding: 1.25rem; max-width: 1400px; }
.page-header { margin-bottom: 1rem; }
.back { color: #2563eb; text-decoration: none; font-size: 0.85rem; }
.back:hover { text-decoration: underline; }
.page-header h1 { margin: 0.25rem 0; font-size: 1.4rem; }
.subtitle { margin: 0; color: #6b7280; font-size: 0.85rem; }

.layout { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
.wizard, .results { background: #fff; border: 1px solid #e5e7eb; border-radius: 6px; padding: 1.25rem; }
.wizard-head h2, .results h2 { margin: 0 0 0.75rem; font-size: 1.1rem; }
.step-pills { list-style: none; padding: 0; margin: 0 0 1rem; display: flex; gap: 0.4rem; flex-wrap: wrap; }
.step-pills li { font-size: 0.75rem; padding: 0.25rem 0.6rem; border-radius: 999px; background: #f3f4f6; color: #6b7280; }
.step-pills li.active { background: #2563eb; color: #fff; }
.step-pills li.done { background: #d1fae5; color: #065f46; }

.hint { color: #6b7280; font-size: 0.85rem; margin-bottom: 0.75rem; }
.field { display: flex; flex-direction: column; gap: 0.25rem; margin-bottom: 0.75rem; }
.field span { font-size: 0.85rem; color: #4b5563; }
.field input, .field select { padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 4px; }

.doc-pick, .strategy-pick { list-style: none; padding: 0; margin: 0 0 1rem; max-height: 300px; overflow-y: auto; }
.doc-pick li, .strategy-pick li { padding: 0.4rem 0.5rem; border-bottom: 1px solid #f3f4f6; }
.doc-pick label, .strategy-pick label { display: flex; gap: 0.6rem; align-items: center; cursor: pointer; }
.filename, .strategy-label { font-size: 0.9rem; flex-grow: 1; }
.muted { color: #9ca3af; font-size: 0.8rem; }

.query-grid { width: 100%; border-collapse: collapse; margin-bottom: 0.75rem; }
.query-grid th, .query-grid td { padding: 0.4rem 0.3rem; border-bottom: 1px solid #f3f4f6; font-size: 0.85rem; text-align: left; }
.query-grid input, .query-grid select { width: 100%; padding: 0.3rem 0.5rem; border: 1px solid #d1d5db; border-radius: 3px; }

.confirm { display: grid; grid-template-columns: max-content 1fr; gap: 0.4rem 1rem; }
.confirm dt { color: #6b7280; }
.confirm dd { margin: 0; }

.step-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 1rem; }
button { padding: 0.5rem 0.9rem; border: 1px solid transparent; border-radius: 4px; cursor: pointer; font-size: 0.9rem; }
button.primary { background: #2563eb; color: #fff; }
button.primary:hover:not(:disabled) { background: #1d4ed8; }
button.primary:disabled { opacity: 0.5; cursor: not-allowed; }
button.ghost { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }
button.small { font-size: 0.8rem; padding: 0.3rem 0.6rem; }

.run-list { list-style: none; padding: 0; margin: 0 0 1rem; }
.run-list li { margin-bottom: 0.4rem; }
.run-row { display: flex; gap: 0.5rem; align-items: center; width: 100%; padding: 0.6rem 0.8rem; border: 1px solid #e5e7eb; border-radius: 4px; background: #fff; text-align: left; cursor: pointer; }
.run-row:hover { border-color: #93c5fd; }
.run-list li.selected .run-row { border-color: #2563eb; background: #eff6ff; }

.badge { font-size: 0.65rem; padding: 0.15rem 0.45rem; border-radius: 999px; text-transform: uppercase; }
.badge.queued, .badge.running { background: #fef3c7; color: #92400e; }
.badge.succeeded { background: #d1fae5; color: #065f46; }
.badge.failed { background: #fee2e2; color: #991b1b; }

.run-detail { margin-top: 1rem; }
.run-detail h3 { margin: 0 0 0.5rem; font-size: 1rem; }

.metrics { width: 100%; border-collapse: collapse; margin-top: 0.75rem; font-size: 0.85rem; }
.metrics th, .metrics td { padding: 0.4rem 0.5rem; border-bottom: 1px solid #f3f4f6; text-align: right; }
.metrics th:first-child, .metrics td:first-child { text-align: left; }
.metrics tr.best { background: #ecfdf5; font-weight: 600; }
.metrics .err { color: #b91c1c; text-align: left; }

.banner { padding: 0.6rem 0.8rem; border-radius: 4px; margin-bottom: 0.5rem; font-size: 0.85rem; }
.banner.muted { background: #f9fafb; color: #6b7280; }
.banner.error { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.banner.inline { margin-top: 0.5rem; }

.cred-row { display: flex; gap: 0.5rem; align-items: center; }
.cred-select { flex: 1; padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 4px; }

.cred-form { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; padding: 0.9rem; margin-top: 0.75rem; }
.cred-form h4 { margin: 0 0 0.6rem; font-size: 0.95rem; }
.cred-form-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 0.4rem; }
</style>
