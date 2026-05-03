<template>
  <div class="page">
    <header class="page-head">
      <div>
        <router-link :to="{ name: 'CollectionDetail', params: { id: collectionId } }" class="back-link">← collection</router-link>
        <h1 class="page-head__title">chunking · evaluator</h1>
        <p v-if="collection" class="page-head__sub">
          collection #{{ collection.id }} · {{ collection.name }} · agent #{{ collection.agent_id }}
        </p>
      </div>
    </header>

    <div class="layout">
      <!-- Wizard ---------------------------------------------------- -->
      <TermBox title="new · evaluation run" pad="md">
        <ol class="steps">
          <li :class="{ 'is-active': step === 1, 'is-done': step > 1 }">01 sample documents</li>
          <li :class="{ 'is-active': step === 2, 'is-done': step > 2 }">02 eval queries</li>
          <li :class="{ 'is-active': step === 3, 'is-done': step > 3 }">03 strategies</li>
          <li :class="{ 'is-active': step === 4, 'is-done': step > 4 }">04 judge llm <span class="cell-meta">(optional)</span></li>
          <li :class="{ 'is-active': step === 5 }">05 confirm</li>
        </ol>

        <!-- Step 1 -->
        <div v-if="step === 1" class="step">
          <p class="cell-meta">pick documents (must be indexed). 5–10 representative items recommended.</p>
          <div v-if="loadingDocs" class="loading">loading…</div>
          <ul v-else class="picklist">
            <li v-for="d in indexedDocs" :key="d.id">
              <label>
                <input type="checkbox" :value="d.id" v-model="form.sample_document_ids" />
                <span class="picklist__name">{{ d.filename }}</span>
                <span class="cell-meta">{{ d.chunk_count }} chunks · {{ humanBytes(d.bytes) }}</span>
              </label>
            </li>
          </ul>
          <div class="step-actions">
            <TermButton variant="primary" :disabled="form.sample_document_ids.length === 0" @click="step = 2" label="next →" />
          </div>
        </div>

        <!-- Step 2 -->
        <div v-if="step === 2" class="step">
          <p class="cell-meta">(query, expected document) pairs · used for hit@k / mrr</p>
          <table class="term-table query-table">
            <thead><tr><th>query</th><th style="width: 40%">expected document</th><th style="width: 36px"></th></tr></thead>
            <tbody>
              <tr v-for="(q, i) in form.queries" :key="i">
                <td><input v-model.trim="q.query" class="term-input" placeholder="e.g. what does §8 specify?" /></td>
                <td>
                  <select v-model.number="q.expected_doc_id" class="term-select">
                    <option :value="0" disabled>— pick doc —</option>
                    <option v-for="d in pickedDocs" :key="d.id" :value="d.id">{{ d.filename }} (#{{ d.id }})</option>
                  </select>
                </td>
                <td><button class="term-action term-action--danger" @click="form.queries.splice(i, 1)">×</button></td>
              </tr>
            </tbody>
          </table>
          <TermButton size="xs" @click="addQuery" label="+ query" />
          <div class="step-actions">
            <TermButton variant="ghost" @click="step = 1" label="← back" />
            <TermButton variant="primary" :disabled="!validQueries" @click="step = 3" label="next →" />
          </div>
        </div>

        <!-- Step 3 -->
        <div v-if="step === 3" class="step">
          <p class="cell-meta">choose strategies to benchmark · ≥ 2 for meaningful comparison</p>
          <ul class="strats">
            <li v-for="s in availableStrategies" :key="s.name">
              <label>
                <input type="checkbox" :value="s" v-model="pickedStrategies" />
                <span class="strats__name">{{ s.label }}</span>
                <span class="cell-meta">{{ s.note }}</span>
              </label>
            </li>
          </ul>
          <div class="step-actions">
            <TermButton variant="ghost" @click="step = 2" label="← back" />
            <TermButton variant="primary" :disabled="pickedStrategies.length < 1" @click="step = 4" label="next →" />
          </div>
        </div>

        <!-- Step 4 -->
        <div v-if="step === 4" class="step">
          <p class="cell-meta">
            llm-as-judge scores (query, top-k chunks) on a 1–3 scale, averaged into <code>judge_avg</code>.
            optional · skip = hit@k / mrr only.
          </p>
          <div v-if="loadingCredentials" class="loading">loading credentials…</div>
          <div v-else>
            <div v-if="credentials.length === 0" class="cell-meta" style="margin-bottom: var(--gap-2);">
              no llm credentials registered yet — add one below or skip.
            </div>
            <TermField v-else label="judge credential">
              <div class="cred-row">
                <select v-model.number="form.judge_credential_id" class="term-select">
                  <option :value="null">— skip judge · only hit@k / mrr —</option>
                  <option v-for="c in credentials" :key="c.id" :value="c.id">{{ c.name }} · {{ c.model_name }}</option>
                </select>
                <button v-if="form.judge_credential_id" class="term-action term-action--danger" :disabled="deletingCredentialId === form.judge_credential_id" @click="onDeleteCredential(form.judge_credential_id)">
                  {{ deletingCredentialId === form.judge_credential_id ? 'deleting…' : 'delete' }}
                </button>
              </div>
            </TermField>

            <TermButton v-if="!showCredentialForm" size="xs" @click="showCredentialForm = true" label="+ add credential" />

            <div v-else class="cred-form">
              <p v-if="insecureContext" class="feedback is-err">! page is not https ({{ pageProtocol }}) — api key would travel in plaintext. switch to https first.</p>
              <TermField label="name (your label)">
                <input v-model.trim="newCredential.name" class="term-input" placeholder="openai-judge" />
              </TermField>
              <TermField label="endpoint url">
                <input v-model.trim="newCredential.endpoint_url" class="term-input" placeholder="https://api.openai.com/v1" />
              </TermField>
              <TermField label="model name">
                <input v-model.trim="newCredential.model_name" class="term-input" placeholder="gpt-4o-mini" />
              </TermField>
              <TermField label="api key" hint="aes-gcm encrypted at rest · cannot be re-read · re-create to rotate">
                <input v-model.trim="newCredential.api_key" type="password" class="term-input" placeholder="sk-…" />
              </TermField>
              <div v-if="credentialError" class="feedback is-err">! {{ credentialError }}</div>
              <div class="step-actions">
                <TermButton variant="ghost" size="xs" @click="cancelCredentialForm" label="cancel" />
                <TermButton variant="primary" size="xs" :disabled="!validNewCredential || creatingCredential" :loading="creatingCredential" :label="creatingCredential ? 'creating' : 'create + select'" @click="onCreateCredential" />
              </div>
            </div>
          </div>

          <TermField v-if="form.judge_credential_id" label="top-k chunks per query" hint="how many chunks the judge sees">
            <input v-model.number="form.judge_top_k" type="number" min="1" max="20" class="term-input" />
          </TermField>
          <p v-if="form.judge_credential_id" class="cell-meta" style="margin-top: var(--gap-2);">
            ! judge bills via your provider · not tracked in csp token_usage.
          </p>

          <div class="step-actions">
            <TermButton variant="ghost" @click="step = 3" label="← back" />
            <TermButton variant="primary" @click="step = 5" label="next →" />
          </div>
        </div>

        <!-- Step 5 -->
        <div v-if="step === 5" class="step">
          <p class="cell-meta">confirm and enqueue · results appear on the right when the run completes.</p>
          <TermField label="run name">
            <input v-model.trim="form.name" class="term-input" placeholder="2026-04-25 baseline" />
          </TermField>
          <dl class="confirm">
            <div><dt>documents</dt><dd>{{ form.sample_document_ids.length }}</dd></div>
            <div><dt>queries</dt><dd>{{ form.queries.length }}</dd></div>
            <div><dt>strategies</dt><dd>{{ pickedStrategies.map(s => s.name).join(', ') }}</dd></div>
            <div>
              <dt>judge llm</dt>
              <dd>
                <span v-if="!form.judge_credential_id" class="cell-meta">disabled</span>
                <span v-else>{{ selectedCredentialLabel }} · top-{{ form.judge_top_k }} · <strong>{{ projectedJudgeCalls }}</strong> calls</span>
              </dd>
            </div>
          </dl>
          <p v-if="judgeCallsExceedCap" class="feedback is-err">
            ! projected {{ projectedJudgeCalls }} judge calls exceed the per-run cap of {{ JUDGE_MAX_CALLS_PER_RUN }}.
          </p>
          <div v-if="submitError" class="feedback is-err">! {{ submitError }}</div>
          <div class="step-actions">
            <TermButton variant="ghost" @click="step = 4" label="← back" />
            <TermButton variant="primary" :disabled="!form.name || submitting || judgeCallsExceedCap" :loading="submitting" :label="submitting ? 'submitting' : '↑ start evaluation'" @click="submit" />
          </div>
        </div>
      </TermBox>

      <!-- Results --------------------------------------------------- -->
      <TermBox title="recent · runs" pad="md">
        <TermEmpty v-if="runs.length === 0" message="no evaluation runs yet" />
        <ul v-else class="runs">
          <li v-for="r in runs" :key="r.id" :class="{ 'is-on': selectedRun?.id === r.id }" @click="selectedRun = r">
            <span class="runs__name">{{ r.name }}</span>
            <TermBadge :variant="runVariant(r.status)" dot>{{ r.status }}</TermBadge>
            <span class="cell-meta">{{ r.strategies_tried.length }} strats · {{ r.queries.length }} q</span>
          </li>
        </ul>

        <article v-if="selectedRun" class="rundetail">
          <h3 class="rundetail__title">{{ selectedRun.name }}</h3>
          <p v-if="selectedRun.status !== 'succeeded'" class="cell-meta">
            status: {{ selectedRun.status }}
            <span v-if="selectedRun.error_message"> · {{ selectedRun.error_message }}</span>
          </p>
          <div v-if="selectedRun.results">
            <p class="rundetail__meta">
              {{ selectedRun.results.elapsed_seconds }}s · {{ selectedRun.results.n_docs }} docs · {{ selectedRun.results.n_queries }} q ·
              recommended <code>{{ selectedRun.recommended_strategy || '—' }}</code>
            </p>
            <p v-if="selectedRun.results.judge_load_error" class="feedback is-err">
              ! judge credential failed to load ({{ selectedRun.results.judge_load_error }}) · skipped judge
            </p>
            <table class="term-table metrics">
              <thead>
                <tr>
                  <th>strategy</th>
                  <th class="num">hit@1</th>
                  <th class="num">hit@5</th>
                  <th class="num">mrr</th>
                  <th class="num" title="llm-as-judge avg 1–3">judge</th>
                  <th class="num">chunks/doc</th>
                  <th class="num">avg tokens</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(metrics, name) in selectedRun.results.per_strategy"
                  :key="name"
                  :class="{ 'is-best': name === selectedRun.recommended_strategy }"
                >
                  <td><code>{{ name }}</code></td>
                  <template v-if="metrics.error">
                    <td colspan="6" class="err">! {{ metrics.error }}</td>
                  </template>
                  <template v-else>
                    <td class="num tnum">{{ formatPct(metrics.hit_at_1) }}</td>
                    <td class="num tnum">{{ formatPct(metrics.hit_at_5) }}</td>
                    <td class="num tnum">{{ metrics.mrr.toFixed(3) }}</td>
                    <td class="num tnum">
                      <span v-if="metrics.judge_avg != null">{{ metrics.judge_avg.toFixed(2) }}<span class="cell-meta">/3 (n={{ metrics.judge_n_scored }})</span></span>
                      <span v-else class="cell-meta">—</span>
                    </td>
                    <td class="num tnum">{{ metrics.chunks_per_doc }}</td>
                    <td class="num tnum">{{ metrics.avg_chunk_tokens }}</td>
                  </template>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
      </TermBox>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getCollection } from '../api/ingestionCollections'
import { listDocuments } from '../api/ingestionDocuments'
import { createEvalRun, listEvalRuns } from '../api/ingestionEvalRuns'
import { createLlmCredential, deleteLlmCredential, listLlmCredentials } from '../api/ingestionLlmCredentials'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty } from '../components/cli'

const route = useRoute()
const collectionId = ref(Number(route.params.id))

const collection = ref(null)
const documents = ref([])
const loadingDocs = ref(false)

const indexedDocs = computed(() => documents.value.filter(d => d.status === 'indexed'))
const pickedDocs = computed(() => indexedDocs.value.filter(d => form.value.sample_document_ids.includes(d.id)))

const step = ref(1)
const form = ref({
  name: `eval-${new Date().toISOString().slice(0, 10)}`,
  sample_document_ids: [],
  queries: [{ query: '', expected_doc_id: 0 }],
  strategies_tried: [],
  judge_credential_id: null,
  judge_top_k: 5,
})
const submitting = ref(false)
const submitError = ref('')

const credentials = ref([])
const loadingCredentials = ref(false)
const selectedCredentialLabel = computed(() => {
  const c = credentials.value.find(x => x.id === form.value.judge_credential_id)
  return c ? `${c.name} · ${c.model_name}` : ''
})

const pageProtocol = typeof window !== 'undefined' ? window.location.protocol : 'unknown:'
const insecureContext = typeof window !== 'undefined' && !window.isSecureContext && window.location.protocol !== 'https:'

const JUDGE_MAX_CALLS_PER_RUN = 100
const projectedJudgeCalls = computed(() => form.value.queries.length * pickedStrategies.value.length)
const judgeCallsExceedCap = computed(() => Boolean(form.value.judge_credential_id) && projectedJudgeCalls.value > JUDGE_MAX_CALLS_PER_RUN)

const showCredentialForm = ref(false)
const creatingCredential = ref(false)
const deletingCredentialId = ref(null)
const credentialError = ref('')
const newCredential = ref({ name: '', endpoint_url: '', model_name: '', api_key: '' })
const validNewCredential = computed(() =>
  Boolean(newCredential.value.name && newCredential.value.endpoint_url && newCredential.value.model_name && newCredential.value.api_key)
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
  } finally { creatingCredential.value = false }
}
async function onDeleteCredential(id) {
  if (!window.confirm('delete this credential? non-reversible · key is not retrievable.')) return
  deletingCredentialId.value = id
  try {
    await deleteLlmCredential(id)
    credentials.value = credentials.value.filter(c => c.id !== id)
    if (form.value.judge_credential_id === id) form.value.judge_credential_id = null
  } catch (e) { credentialError.value = e.response?.data?.detail || e.message }
  finally { deletingCredentialId.value = null }
}

const availableStrategies = [
  { name: 'hierarchical', label: 'hierarchical', params: { max_leaf_tokens: 1024 }, note: 'heading tree + ancestor context' },
  { name: 'fixed', label: 'fixed', params: { size: 1024, overlap: 128 }, note: 'token-budget windowing' },
  { name: 'markdown-aware', label: 'markdown-aware', params: { max_leaf_tokens: 1024 }, note: 'heading + code-fence safe' },
  { name: 'pdf-page', label: 'pdf-page', params: { max_page_tokens: 4096 }, note: 'pdf only · page boundaries' },
  { name: 'cjk-sentence', label: 'cjk-sentence', params: { target_tokens: 512 }, note: 'cjk syntax + token merge' },
  { name: 'semantic', label: 'semantic', params: { breakpoint_percentile: 80 }, note: 'embedding distance · expensive' },
]
const pickedStrategies = ref([])

const runs = ref([])
const selectedRun = ref(null)
let pollTimer = null

const validQueries = computed(() => form.value.queries.length > 0 && form.value.queries.every(q => q.query && q.expected_doc_id > 0))
function addQuery() { form.value.queries.push({ query: '', expected_doc_id: 0 }) }

onMounted(async () => {
  loadingDocs.value = true
  loadingCredentials.value = true
  try {
    const [coll, docs, list, creds] = await Promise.all([
      getCollection(collectionId.value),
      listDocuments(collectionId.value),
      listEvalRuns({ collection_id: collectionId.value }),
      listLlmCredentials().catch(() => ({ data: [] })),
    ])
    collection.value = coll.data
    documents.value = docs.data
    runs.value = list.data
    credentials.value = creds.data
    if (list.data.length > 0) selectedRun.value = list.data[0]
  } finally { loadingDocs.value = false; loadingCredentials.value = false }
  startPolling()
})
function startPolling() {
  pollTimer = setInterval(async () => {
    const inFlight = runs.value.some(r => !['succeeded', 'failed', 'cancelled'].includes(r.status))
    if (!inFlight && selectedRun.value && ['succeeded', 'failed'].includes(selectedRun.value.status)) return
    try {
      const { data } = await listEvalRuns({ collection_id: collectionId.value })
      runs.value = data
      if (selectedRun.value) {
        const refreshed = data.find(r => r.id === selectedRun.value.id)
        if (refreshed) selectedRun.value = refreshed
      }
    } catch {}
  }, 2500)
}
watch(() => route.params.id, (id) => { if (id) collectionId.value = Number(id) })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

async function submit() {
  submitting.value = true
  submitError.value = ''
  try {
    const payload = {
      collection_id: collectionId.value,
      name: form.value.name,
      sample_document_ids: [...form.value.sample_document_ids],
      strategies_tried: pickedStrategies.value.map(s => ({ name: s.name, params: s.params })),
      queries: form.value.queries.map(q => ({ query: q.query, expected_doc_id: q.expected_doc_id })),
      judge_credential_id: form.value.judge_credential_id || null,
      judge_top_k: form.value.judge_top_k,
    }
    const { data } = await createEvalRun(payload)
    runs.value.unshift(data)
    selectedRun.value = data
    step.value = 1
    form.value = {
      name: `eval-${new Date().toISOString().slice(0, 10)}`,
      sample_document_ids: [], queries: [{ query: '', expected_doc_id: 0 }],
      strategies_tried: [], judge_credential_id: null, judge_top_k: 5,
    }
    pickedStrategies.value = []
  } catch (e) { submitError.value = e.response?.data?.detail || e.message }
  finally { submitting.value = false }
}

function humanBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = Number(n), u = 0
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u += 1 }
  return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`
}
function formatPct(v) { return `${(v * 100).toFixed(1)}%` }
function runVariant(s) { return ({ succeeded: 'ok', failed: 'danger' })[s] || 'warn' }
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; flex-direction: column; gap: 4px; }
.back-link { font-size: var(--t-2xs); color: var(--c-fg-3); text-decoration: none; }
.back-link:hover { color: var(--c-accent); text-decoration: none; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 0; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); margin: 0; }

.layout { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
@media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }

.steps {
  list-style: none; padding: 0; margin: 0 0 var(--gap-3);
  display: flex; gap: 4px; flex-wrap: wrap; font-size: var(--t-2xs);
}
.steps li {
  padding: 4px 10px;
  border: var(--border-w) solid var(--c-border);
  color: var(--c-fg-3);
  letter-spacing: 0.04em;
}
.steps li.is-done { color: var(--c-ok); border-color: var(--c-ok); }
.steps li.is-active { color: var(--c-accent-fg); background: var(--c-accent); border-color: var(--c-accent); font-weight: 600; }

.step { display: flex; flex-direction: column; gap: var(--gap-3); }
.step-actions { display: flex; justify-content: flex-end; gap: var(--gap-2); padding-top: var(--gap-2); border-top: var(--border-w) dashed var(--c-border); }

.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

.picklist, .strats { list-style: none; padding: 0; margin: 0; max-height: 280px; overflow-y: auto; border: var(--border-w) solid var(--c-border); }
.picklist li, .strats li { padding: 4px var(--gap-2); border-bottom: var(--border-w) dashed var(--c-border); }
.picklist li:last-child, .strats li:last-child { border-bottom: 0; }
.picklist label, .strats label { display: flex; align-items: center; gap: var(--gap-2); font-size: var(--t-sm); color: var(--c-fg-1); cursor: pointer; }
.picklist label input, .strats label input { accent-color: var(--c-accent); }
.picklist__name, .strats__name { flex: 1; }

.query-table input, .query-table select { width: 100%; }

.cred-row { display: flex; gap: 6px; align-items: center; }
.cred-form {
  margin-top: var(--gap-2); padding: var(--gap-3);
  background: var(--c-surface-2); border: var(--border-w) solid var(--c-border);
  display: flex; flex-direction: column; gap: var(--gap-2);
}

.confirm {
  display: grid; grid-template-columns: 110px 1fr; gap: 4px var(--gap-2); margin: 0;
  font-size: var(--t-sm);
}
.confirm dt { color: var(--c-fg-3); font-size: var(--t-2xs); text-transform: uppercase; letter-spacing: var(--tracking-caps); }
.confirm dd { margin: 0; color: var(--c-fg-1); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.loading { padding: var(--gap-3); color: var(--c-fg-3); font-size: var(--t-sm); text-align: center; }

/* Results */
.runs { list-style: none; padding: 0; margin: 0 0 var(--gap-3); }
.runs li {
  display: flex; gap: var(--gap-2); align-items: center;
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid var(--c-border);
  margin-bottom: 4px;
  cursor: pointer;
  background: var(--c-surface-1);
}
.runs li:hover { background: var(--c-row-hover); }
.runs li.is-on { border-color: var(--c-accent); background: var(--c-accent-soft); }
.runs__name { flex: 1; font-weight: 500; color: var(--c-fg-1); }

.rundetail { padding-top: var(--gap-2); border-top: var(--border-w) solid var(--c-border); }
.rundetail__title { font-size: var(--t-md); margin: 0 0 var(--gap-1); }
.rundetail__meta { font-size: var(--t-xs); color: var(--c-fg-2); margin: 0 0 var(--gap-2); }
.rundetail__meta code { color: var(--c-accent); background: var(--c-accent-soft); padding: 0 4px; }

.metrics tr.is-best td { background: var(--c-accent-soft); color: var(--c-accent-strong); font-weight: 600; }
.metrics .err { color: var(--c-danger); }
</style>
