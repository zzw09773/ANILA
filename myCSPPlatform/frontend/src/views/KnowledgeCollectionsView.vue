<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">developer · rag</p>
        <h1 class="page-head__title">collections</h1>
        <p class="page-head__sub">
          first-class rag store · agent backend mounts via <code>RAG_COLLECTION_ID=&lt;id&gt;</code>
        </p>
      </div>
      <TermButton variant="primary" @click="openCreateModal" label="new collection" />
    </header>

    <TermBox title="filter" pad="sm">
      <div class="filters">
        <label class="filters__toggle">
          <input type="checkbox" v-model="includeArchived" /> show archived
        </label>
        <label v-if="isAdmin" class="filters__toggle">
          <input type="checkbox" v-model="showAllCollections" /> show others' collections (admin)
        </label>
      </div>
    </TermBox>

    <div v-if="error" class="feedback is-err">! {{ error }}</div>
    <div v-else-if="loadingCollections" class="loading">loading collections…</div>

    <div v-if="collections.length === 0 && !loadingCollections && !error" class="term-box" style="padding: var(--gap-6);">
      <TermEmpty message="no collections yet · click [new collection] to create one" />
    </div>

    <div v-if="collections.length > 0" class="grid">
      <article v-for="c in collections" :key="c.id" class="cc">
        <header class="cc__head">
          <div class="cc__title">
            <span class="cc__name">{{ c.name }}</span>
            <TermBadge :variant="c.status === 'active' ? 'ok' : ''">{{ c.status }}</TermBadge>
          </div>
          <div class="cc__id tnum">id #{{ c.id }}</div>
        </header>
        <p v-if="c.description" class="cc__desc">{{ c.description }}</p>

        <dl class="cc__stats">
          <div><dt>documents</dt><dd class="tnum">{{ c.document_count.toLocaleString() }}</dd></div>
          <div><dt>chunks</dt><dd class="tnum">{{ c.chunk_count.toLocaleString() }}</dd></div>
          <div><dt>bytes</dt><dd class="tnum">{{ humanBytes(c.bytes_stored) }}</dd></div>
          <div><dt>strategy</dt><dd>{{ c.chunking_config.strategy }}</dd></div>
          <div><dt>embedding</dt><dd>{{ c.embedding_model }} · {{ c.embedding_dim }}-d</dd></div>
          <div><dt>owner</dt><dd>user #{{ c.created_by }}</dd></div>
        </dl>

        <div class="cc__dsn">
          <span class="cell-meta">agent backend env</span>
          <code>RAG_COLLECTION_ID={{ c.id }}</code>
        </div>

        <footer class="cc__foot">
          <router-link :to="{ name: 'CollectionDetail', params: { id: c.id } }" class="term-action">→ inspector</router-link>
          <span class="cc__sep">·</span>
          <router-link :to="{ name: 'Evaluator', params: { id: c.id } }" class="term-action">→ evaluator</router-link>
          <span class="cc__sep">·</span>
          <button v-if="c.status === 'active'" class="term-action" @click="archiveCollection(c)">archive</button>
          <button v-else class="term-action" @click="restoreCollection(c)">restore</button>
          <span class="cc__sep">·</span>
          <button class="term-action term-action--danger" @click="confirmDelete(c)">delete</button>
        </footer>
      </article>
    </div>

    <TermModal :visible="creating" title="new · collection" width="520px" @close="creating = false">
      <div class="form-grid">
        <TermField label="name">
          <input v-model.trim="form.name" class="term-input" maxlength="200" placeholder="legal-regs" />
        </TermField>
        <TermField label="description" optional>
          <textarea v-model.trim="form.description" rows="2" class="term-textarea" maxlength="2000" />
        </TermField>
        <TermField label="chunking strategy">
          <select v-model="form.strategy" class="term-select">
            <option value="hierarchical">hierarchical · heading tree + ancestor context</option>
            <option value="markdown-aware">markdown-aware · heading + code-fence safe</option>
            <option value="fixed">fixed · token-budget windowing</option>
            <option value="pdf-page">pdf-page · pdf only · page boundaries</option>
            <option value="cjk-sentence">cjk-sentence · cjk syntax + token merge</option>
            <option value="semantic">semantic · embedding distance · slow but precise</option>
          </select>
        </TermField>
        <TermField :label="tokenLabel" :hint="tokenHint">
          <input v-model.number="form.maxTokens" type="number" class="term-input" min="64" max="8192" />
        </TermField>
        <div v-if="formError" class="feedback is-err">! {{ formError }}</div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="creating = false" label="cancel" />
        <TermButton variant="primary" :loading="submitting" :disabled="submitting" :label="submitting ? 'creating' : 'create'" @click="submitCreate" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useAuthStore } from '../stores/auth'
import { listCollections, createCollection, updateCollection, deleteCollection } from '../api/ingestionCollections'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal } from '../components/cli'

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.isAdmin)

const includeArchived = ref(false)
const showAllCollections = ref(false)
const collections = ref([])
const loadingCollections = ref(false)
const error = ref('')

const creating = ref(false)
const submitting = ref(false)
const formError = ref('')
const form = ref({ name: '', description: '', strategy: 'hierarchical', maxTokens: 1024 })

const tokenLabel = computed(() => ({
  fixed: 'size (tokens)',
  'pdf-page': 'max page tokens',
  'cjk-sentence': 'target tokens',
  semantic: 'min segment tokens',
})[form.value.strategy] || 'max leaf tokens')

const tokenHint = computed(() => ({
  fixed: 'token budget per chunk · overlap auto = size/8',
  'pdf-page': 'oversized pages split inside via fixed strategy',
  'cjk-sentence': 'merge sentences until target reached',
  semantic: 'segment cap · boundary by embedding distance',
})[form.value.strategy] || 'token cap per heading-tree leaf')

onMounted(loadCollections)
watch([includeArchived, showAllCollections], loadCollections)

async function loadCollections() {
  loadingCollections.value = true
  error.value = ''
  try {
    const params = { include_archived: includeArchived.value }
    if (showAllCollections.value && isAdmin.value) params.owned_only = false
    const { data } = await listCollections(params)
    collections.value = data
  } catch (e) {
    error.value = `failed to load collections: ${e.response?.data?.detail || e.message}`
  } finally { loadingCollections.value = false }
}

function openCreateModal() {
  formError.value = ''
  form.value = { name: '', description: '', strategy: 'hierarchical', maxTokens: 1024 }
  creating.value = true
}

async function submitCreate() {
  formError.value = ''
  submitting.value = true
  const s = form.value.strategy
  let params
  if (s === 'fixed') params = { size: form.value.maxTokens, overlap: Math.floor(form.value.maxTokens / 8) }
  else if (s === 'pdf-page') params = { max_page_tokens: form.value.maxTokens }
  else if (s === 'cjk-sentence') params = { target_tokens: form.value.maxTokens, max_tokens: form.value.maxTokens * 2 }
  else if (s === 'semantic') params = { min_segment_tokens: form.value.maxTokens, breakpoint_percentile: 80 }
  else params = { max_leaf_tokens: form.value.maxTokens }
  try {
    await createCollection({
      name: form.value.name,
      description: form.value.description || null,
      chunking_config: { strategy: s, params },
    })
    creating.value = false
    await loadCollections()
  } catch (e) {
    formError.value = e.response?.data?.detail || e.message
  } finally { submitting.value = false }
}

async function archiveCollection(c) {
  try { await updateCollection(c.id, { status: 'archived' }); await loadCollections() }
  catch (e) { error.value = `archive failed: ${e.response?.data?.detail || e.message}` }
}
async function restoreCollection(c) {
  try { await updateCollection(c.id, { status: 'active' }); await loadCollections() }
  catch (e) { error.value = `restore failed: ${e.response?.data?.detail || e.message}` }
}
async function confirmDelete(c) {
  if (!confirm(`delete '${c.name}'? CASCADE removes ${c.document_count} docs and ${c.chunk_count} chunks.`)) return
  try { await deleteCollection(c.id); await loadCollections() }
  catch (e) { error.value = `delete failed: ${e.response?.data?.detail || e.message}` }
}

function humanBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = Number(n), u = 0
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u += 1 }
  return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__sub code { color: var(--c-accent); background: var(--c-accent-soft); padding: 0 4px; }

.filters { display: flex; gap: var(--gap-4); flex-wrap: wrap; }
.filters__toggle { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-sm); color: var(--c-fg-2); cursor: pointer; }
.filters__toggle input { accent-color: var(--c-accent); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.loading { padding: var(--gap-6); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.grid {
  display: grid;
  gap: var(--gap-3);
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
}

.cc {
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  display: flex;
  flex-direction: column;
}
.cc__head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--gap-3) var(--gap-4);
  border-bottom: var(--border-w) solid var(--c-border);
  background: var(--c-surface-2);
}
.cc__title { display: flex; align-items: center; gap: 8px; }
.cc__name { color: var(--c-fg-1); font-weight: 600; font-size: var(--t-md); }
.cc__id { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cc__desc {
  margin: 0;
  padding: var(--gap-2) var(--gap-4);
  color: var(--c-fg-2);
  font-size: var(--t-sm);
  border-bottom: var(--border-w) dashed var(--c-border);
}
.cc__stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px var(--gap-3);
  margin: 0;
  padding: var(--gap-3) var(--gap-4);
}
.cc__stats > div { display: flex; flex-direction: column; }
.cc__stats dt {
  font-size: var(--t-2xs); color: var(--c-fg-3);
  text-transform: uppercase; letter-spacing: var(--tracking-caps);
}
.cc__stats dd { margin: 0; color: var(--c-fg-1); font-size: var(--t-sm); }

.cc__dsn {
  margin: 0 var(--gap-4) var(--gap-3);
  padding: var(--gap-2) var(--gap-3);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--t-2xs);
}
.cc__dsn code { font-family: var(--font-mono); color: var(--c-accent); }

.cc__foot {
  padding: var(--gap-2) var(--gap-4);
  border-top: var(--border-w) solid var(--c-border);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: var(--t-xs);
  background: var(--c-surface-2);
}
.cc__sep { color: var(--c-border-strong); }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
</style>
