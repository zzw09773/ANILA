<template>
  <div class="page">
    <header class="page-head">
      <div>
        <router-link :to="{ name: 'KnowledgeCollections' }" class="back-link">← collections</router-link>
        <h1 class="page-head__title">chunking · preview &amp; pick</h1>
        <p class="page-head__sub">
          上傳一份代表性文件 → 並排比較 6 種 strategy 切出來的 chunks → 選最合適的 → 真正建立 collection。
          純 dry-run，不寫 DB、不打 embedding。
        </p>
      </div>
    </header>

    <!-- Step 1: upload ------------------------------------------------- -->
    <TermBox v-if="!result" title="step 1 · upload one document" pad="md" hint="≤ 10 MB · txt / md / pdf / docx / odt / rtf / 圖片">
      <div class="upload" @drop.prevent="onDrop" @dragover.prevent>
        <input ref="fileInput" type="file"
               accept=".txt,.md,.markdown,.pdf,.docx,.doc,.odt,.rtf,.png,.jpg,.jpeg,.webp,.gif,.bmp,text/plain,text/markdown,application/pdf"
               @change="onFilePicked" style="display:none" />
        <TermButton variant="primary" :disabled="loading" :loading="loading"
                    :label="loading ? `running ${runningSec}s…` : '+ choose file'"
                    @click="$refs.fileInput.click()" />
        <span class="cell-meta">drag &amp; drop also works</span>
      </div>
      <div v-if="error" class="feedback is-err" style="margin-top: var(--gap-2);">! {{ error }}</div>
      <p class="cell-meta" style="margin-top: var(--gap-2);">
        為什麼要 preview：六種 chunker 對不同 doc 結構會切出大不相同的結果。
        短的純文字 / 簡單 markdown 各 strategy 容易看起來一樣；長的有 heading 結構或 PDF 的差異就明顯。
      </p>
    </TermBox>

    <!-- Step 2: results compare --------------------------------------- -->
    <template v-if="result">
      <TermBox title="step 2 · compare" pad="md">
        <div class="meta-row">
          <div>
            <span class="cell-strong">{{ result.filename }}</span>
            <span class="cell-meta"> · {{ humanBytes(result.bytes) }}</span>
            <span v-if="result.parse_metadata.format" class="cell-meta"> · format <code>{{ result.parse_metadata.format }}</code></span>
            <span v-if="result.parse_metadata.page_count" class="cell-meta"> · {{ result.parse_metadata.page_count }} pages</span>
          </div>
          <TermButton variant="ghost" label="↻ start over" @click="reset" />
        </div>
        <p v-if="result.skipped_strategies.length" class="cell-meta" style="margin-top: var(--gap-2);">
          skipped (preview 不支援，commit 時可選):
          <code>{{ result.skipped_strategies.join(', ') }}</code>
        </p>
      </TermBox>

      <!-- per-strategy cards in a responsive grid -->
      <section class="grid">
        <TermBox v-for="entry in strategyEntries" :key="entry.name"
                 :title="entry.displayName" :hint="entry.runMessage" pad="md"
                 :tone="entry.error ? 'warn' : ''">
          <div v-if="entry.error" class="feedback is-err">! {{ entry.error }}</div>
          <template v-else-if="!entry.previewable">
            <p class="cell-meta">
              ⓘ 此 strategy 需要 embeddings；preview 階段略過。建立 collection 時會在 commit 階段執行。
            </p>
          </template>
          <template v-else>
            <div class="stats">
              <TermStat label="chunks" :value="entry.stats.chunk_count" tone="accent" />
              <TermStat label="total tokens" :value="entry.stats.total_tokens" />
              <TermStat label="avg tokens" :value="entry.stats.avg_tokens" />
            </div>
            <p v-if="entry.stats.truncated_to" class="cell-meta" style="margin-top: var(--gap-1);">
              ⚠ 顯示前 {{ entry.stats.truncated_to }} 個（實際更多）
            </p>
            <details class="chunks">
              <summary>preview chunks ({{ entry.chunks.length }})</summary>
              <ol class="chunk-list">
                <li v-for="c in entry.chunks.slice(0, expandedCount[entry.name] || 5)" :key="c.chunk_key" class="chunk">
                  <header class="chunk__head">
                    <code>{{ c.chunk_key }}</code>
                    <span class="cell-meta tnum">{{ c.token_count }} tokens</span>
                  </header>
                  <pre class="chunk__content">{{ c.content }}</pre>
                </li>
              </ol>
              <button v-if="entry.chunks.length > (expandedCount[entry.name] || 5)"
                      class="term-action"
                      @click.prevent="expandedCount[entry.name] = (expandedCount[entry.name] || 5) + 10">
                show 10 more ({{ entry.chunks.length - (expandedCount[entry.name] || 5) }} remaining)
              </button>
            </details>
          </template>
          <div class="actions">
            <TermButton variant="primary" :disabled="!entry.canPick"
                        :label="entry.canPick ? '↓ use this strategy' : 'unavailable'"
                        @click="pickStrategy(entry)" />
          </div>
        </TermBox>
      </section>
    </template>

    <!-- Step 3: confirm + create -------------------------------------- -->
    <TermModal :visible="!!chosen" :title="chosen ? `step 3 · create with ${chosen.displayName}` : 'create'" width="520px" @close="cancelChoice">
      <div v-if="chosen" class="form-grid">
        <TermField label="strategy" hint="locked from preview pick">
          <input :value="chosen.name" readonly disabled class="term-input" />
        </TermField>
        <TermField label="name">
          <input v-model.trim="commitForm.name" class="term-input" maxlength="200" placeholder="legal-regs" />
        </TermField>
        <TermField label="description" optional>
          <textarea v-model.trim="commitForm.description" rows="2" class="term-textarea" maxlength="2000" />
        </TermField>
        <TermField :label="tokenLabel" :hint="tokenHint">
          <input v-model.number="commitForm.maxTokens" type="number" class="term-input" min="64" max="8192" />
        </TermField>
        <p class="cell-meta">
          ⓘ 建立完 collection 後再上傳檔案才會真的索引。本次預覽用的檔案 <strong>不會</strong> 自動進入 collection。
        </p>
        <div v-if="commitError" class="feedback is-err">! {{ commitError }}</div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="cancelChoice" label="cancel" />
        <TermButton variant="primary" :loading="committing" :disabled="committing || !commitForm.name"
                    :label="committing ? 'creating' : 'create collection'" @click="commitCreate" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { listStrategies, previewChunking } from '../api/chunkingPreview'
import { createCollection } from '../api/ingestionCollections'
import { TermBox, TermButton, TermField, TermModal, TermStat } from '../components/cli'

const router = useRouter()

const fileInput = ref(null)
const loading = ref(false)
const runningSec = ref(0)
const error = ref('')
const result = ref(null)
const strategies = ref([])  // catalogue from /strategies endpoint

// expand "show more" state per strategy.
const expandedCount = reactive({})

const chosen = ref(null)
const commitForm = ref({ name: '', description: '', maxTokens: 1024 })
const commitError = ref('')
const committing = ref(false)

// Build a row per registered strategy by joining the catalogue with
// the per-strategy preview output. Cards render even when a strategy
// returned an error or was skipped — better than hiding rows so the
// user sees the full picture.
const strategyEntries = computed(() => {
  if (!result.value) return []
  return strategies.value.map((s) => {
    const r = result.value.per_strategy[s.name]
    const skipped = result.value.skipped_strategies.includes(s.name)
    return {
      name: s.name,
      displayName: s.display_name,
      previewable: s.previewable,
      requires_embedder: s.requires_embedder,
      default_params: s.default_params || {},
      chunks: r?.chunks || [],
      stats: r?.stats || { chunk_count: 0, total_tokens: 0, avg_tokens: 0 },
      error: r?.error || null,
      runMessage: skipped
        ? 'skipped · needs embeddings'
        : r ? '' : 'not requested',
      // canPick: any strategy that has a preview row OR is requires_embedder
      // (semantic) — for the latter we trust the worker to do it on commit.
      canPick: !!r || s.requires_embedder,
    }
  })
})

const tokenLabel = computed(() => ({
  fixed: 'size (tokens)',
  'pdf-page': 'max page tokens',
  'cjk-sentence': 'target tokens',
  semantic: 'min segment tokens',
})[chosen.value?.name] || 'max leaf tokens')

const tokenHint = computed(() => ({
  fixed: 'token budget per chunk · overlap auto = size/8',
  'pdf-page': 'oversized pages split inside via fixed strategy',
  'cjk-sentence': 'merge sentences until target reached',
  semantic: 'segment cap · boundary by embedding distance',
})[chosen.value?.name] || 'token cap per heading-tree leaf')

onMounted(async () => {
  try {
    const { data } = await listStrategies()
    strategies.value = data
  } catch (e) {
    error.value = `failed to load strategy catalogue: ${e.response?.data?.detail || e.message}`
  }
})

function reset() {
  result.value = null
  error.value = ''
  Object.keys(expandedCount).forEach((k) => delete expandedCount[k])
  if (fileInput.value) fileInput.value.value = ''
}

function onFilePicked(evt) {
  const file = evt.target.files?.[0]
  if (file) runPreview(file)
}

function onDrop(evt) {
  const file = evt.dataTransfer?.files?.[0]
  if (file) runPreview(file)
}

async function runPreview(file) {
  reset()
  loading.value = true
  error.value = ''
  runningSec.value = 0
  const tickerId = setInterval(() => { runningSec.value += 1 }, 1000)
  try {
    const { data } = await previewChunking(file)
    result.value = data
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    clearInterval(tickerId)
    loading.value = false
  }
}

function pickStrategy(entry) {
  chosen.value = entry
  commitForm.value = {
    name: '',
    description: '',
    maxTokens: defaultTokenForStrategy(entry),
  }
  commitError.value = ''
}

function defaultTokenForStrategy(entry) {
  const dp = entry.default_params || {}
  return (
    dp.size ??
    dp.max_leaf_tokens ??
    dp.max_page_tokens ??
    dp.target_tokens ??
    dp.min_segment_tokens ??
    1024
  )
}

function cancelChoice() {
  chosen.value = null
  commitError.value = ''
}

async function commitCreate() {
  if (!chosen.value) return
  commitError.value = ''
  committing.value = true
  // Mirror the param shape the existing KnowledgeCollections create
  // form uses so commit semantics stay identical between the two
  // entry points.
  const s = chosen.value.name
  const v = commitForm.value.maxTokens
  let params
  if (s === 'fixed') params = { size: v, overlap: Math.floor(v / 8) }
  else if (s === 'pdf-page') params = { max_page_tokens: v }
  else if (s === 'cjk-sentence') params = { target_tokens: v, max_tokens: v * 2 }
  else if (s === 'semantic') params = { min_segment_tokens: v, breakpoint_percentile: 80 }
  else params = { max_leaf_tokens: v }

  try {
    const { data } = await createCollection({
      name: commitForm.value.name,
      description: commitForm.value.description || null,
      chunking_config: { strategy: s, params },
    })
    // Drop user back onto the new collection's detail page so they
    // can upload the real corpus there.
    router.push({ name: 'CollectionDetail', params: { id: data.id } })
  } catch (e) {
    commitError.value = e.response?.data?.detail || e.message
  } finally {
    committing.value = false
  }
}

function humanBytes(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0; let x = n
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++ }
  return `${x.toFixed(x >= 10 ? 0 : 1)} ${u[i]}`
}
</script>

<style scoped>
.page { padding: var(--gap-3); display: flex; flex-direction: column; gap: var(--gap-3); }
.page-head { margin-bottom: var(--gap-2); }
.back-link { color: var(--c-fg-2); font-size: var(--t-2xs); text-decoration: none; }
.back-link:hover { color: var(--c-accent); }
.page-head__title { font-size: var(--t-lg); font-weight: 500; margin: 4px 0 6px; }
.page-head__sub { font-size: var(--t-2xs); color: var(--c-fg-2); margin: 0; line-height: 1.6; }

.upload {
  display: flex; align-items: center; gap: var(--gap-2);
  padding: var(--gap-3) var(--gap-2);
  border: 1px dashed var(--c-divider);
  font-size: var(--t-2xs);
}

.feedback.is-err { color: var(--c-danger, #c44); font-size: var(--t-2xs); }
.cell-meta { color: var(--c-fg-2); font-size: var(--t-2xs); }
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.tnum { font-variant-numeric: tabular-nums; }

.meta-row { display: flex; justify-content: space-between; align-items: baseline; }

.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap-3);
}
@media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }

.stats { display: flex; gap: var(--gap-3); flex-wrap: wrap; }

.chunks { margin-top: var(--gap-2); font-size: var(--t-2xs); }
.chunks summary { cursor: pointer; color: var(--c-accent); }
.chunk-list { list-style: none; padding: 0; margin: var(--gap-2) 0 0; display: flex; flex-direction: column; gap: var(--gap-2); }
.chunk { border-left: 2px solid var(--c-divider); padding: 4px 8px; }
.chunk__head { display: flex; justify-content: space-between; margin-bottom: 4px; }
.chunk__content {
  margin: 0; font-family: var(--font-mono); font-size: var(--t-3xs);
  white-space: pre-wrap; word-break: break-word;
  background: var(--c-bg-1, #000); padding: 6px 8px;
  max-height: 180px; overflow-y: auto;
}

.actions { margin-top: var(--gap-2); }
.term-action { background: none; border: none; color: var(--c-accent); cursor: pointer; font-size: var(--t-2xs); padding: 4px 0; font-family: var(--font-mono); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-2); }
</style>
