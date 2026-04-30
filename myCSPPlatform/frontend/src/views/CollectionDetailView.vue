<template>
  <div class="page">
    <header class="page-head">
      <div>
        <router-link :to="{ name: 'KnowledgeCollections' }" class="back-link">← collections</router-link>
        <h1 class="page-head__title">
          <span v-if="collection">{{ collection.name }}</span>
          <span v-else>loading…</span>
        </h1>
        <p v-if="collection" class="page-head__sub">
          agent #{{ collection.agent_id }} · {{ collection.embedding_model }} · {{ collection.embedding_dim }}-d ·
          strategy <code>{{ collection.chunking_config.strategy }}</code>
        </p>
      </div>
    </header>

    <div v-if="loadError" class="feedback is-err">! {{ loadError }}</div>

    <TermBox v-if="collection" title="upload · ingest" pad="md" hint="text / md / pdf / docx · ≤ 50 MB single · ≤ 500 MB / 200 files zip">
      <div class="upload" @drop.prevent="onDrop" @dragover.prevent>
        <input ref="fileInput" type="file" accept=".txt,.md,.markdown,.pdf,.docx,.doc,.odt,.rtf,text/plain,text/markdown,application/pdf" @change="onFilePicked" style="display:none" />
        <input ref="zipInput" type="file" accept=".zip,application/zip" @change="onZipPicked" style="display:none" />
        <TermButton variant="primary" :disabled="uploading" :loading="uploading" :label="uploading ? `uploading… ${Math.round(progress * 100)}%` : '+ single file'" @click="$refs.fileInput.click()" />
        <TermButton :disabled="uploading" label="+ zip · multi-file" @click="$refs.zipInput.click()" />
        <label class="upload__toggle">
          <input type="checkbox" v-model="preserveFolderStructure" />
          <span>preserve folder paths (zip)</span>
        </label>
        <span class="cell-meta">drag &amp; drop also works</span>
      </div>
      <div v-if="uploadError" class="feedback is-err" style="margin-top: var(--gap-2);">! {{ uploadError }}</div>
    </TermBox>

    <!-- Zip result modal --------------------------------------------- -->
    <TermModal :visible="!!zipResult" title="zip · result" width="640px" @close="zipResult = null">
      <dl v-if="zipResult" class="zip-grid">
        <div><dt>files in archive</dt><dd class="tnum">{{ zipResult.files_in_archive }}</dd></div>
        <div class="is-ok"><dt>enqueued</dt><dd class="tnum">{{ zipResult.enqueued }}</dd></div>
        <div class="is-warn"><dt>duplicates</dt><dd class="tnum">{{ zipResult.duplicates }}</dd></div>
        <div><dt>skipped</dt><dd class="tnum">{{ zipResult.skipped }}</dd></div>
        <div v-if="zipResult.errors" class="is-err"><dt>errors</dt><dd class="tnum">{{ zipResult.errors }}</dd></div>
      </dl>
      <details v-if="zipResult" class="zip-detail">
        <summary>per-file results · {{ zipResult.results.length }}</summary>
        <ul class="zip-list">
          <li v-for="r in zipResult.results" :key="r.filename" :class="r.status">
            <span class="zip-list__name">{{ r.filename }}</span>
            <TermBadge :variant="zipBadgeVariant(r.status)">{{ r.status }}</TermBadge>
            <span v-if="r.detail" class="cell-meta">{{ r.detail }}</span>
          </li>
        </ul>
      </details>
      <template #footer>
        <TermButton variant="primary" @click="zipResult = null" label="close" />
      </template>
    </TermModal>

    <section v-if="collection" class="split">
      <TermBox :title="`documents · ${documents.length}`" pad="none" flush>
        <div v-if="loadingDocs" class="loading">loading…</div>
        <TermEmpty v-else-if="documents.length === 0" message="no documents yet · upload above" />
        <ul v-else class="docs">
          <li
            v-for="d in documents"
            :key="d.id"
            class="doc"
            :class="['is-' + d.status, { 'is-selected': selectedDoc?.id === d.id }]"
            @click="selectDoc(d)"
          >
            <div class="doc__row">
              <span class="doc__name">{{ d.filename }}</span>
              <TermBadge :variant="docVariant(d.status)" dot>{{ d.status }}</TermBadge>
            </div>
            <div class="cell-meta tnum">{{ humanBytes(d.bytes) }} · {{ d.chunk_count }} chunks · sha {{ d.sha256.slice(0, 8) }}…</div>
            <div v-if="d.error_message" class="doc__err">! {{ d.error_message }}</div>
          </li>
        </ul>
      </TermBox>

      <TermBox :title="selectedDoc ? `inspector · ${selectedDoc.filename}` : 'inspector'" pad="md">
        <div v-if="!selectedDoc">
          <TermEmpty message="select a document to inspect chunks" />
        </div>
        <template v-else>
          <div class="insp-bar">
            <a :href="blobUrl(selectedDoc.id)" target="_blank" class="term-action">↓ download original</a>
            <span class="row-actions__sep">·</span>
            <label class="filters__toggle">
              <input type="checkbox" v-model="showVectorDebug" />
              <span>show vector debug</span>
            </label>
          </div>

          <div v-if="loadingChunks" class="loading">loading chunks…</div>
          <TermEmpty v-else-if="chunks.length === 0" :message="`no chunks · doc status: ${selectedDoc.status}`" />
          <ol v-else class="chunks">
            <li v-for="c in chunks" :key="c.id" class="chunk">
              <header class="chunk__head">
                <code class="chunk__key">{{ c.chunk_key }}</code>
                <span class="cell-meta tnum">id {{ c.id }} · {{ c.token_count }} tokens</span>
              </header>
              <pre class="chunk__content">{{ c.content }}</pre>
              <details class="chunk__meta">
                <summary>metadata</summary>
                <dl class="chunk__meta-grid">
                  <template v-for="(v, k) in c.metadata" :key="k">
                    <dt>{{ k }}</dt>
                    <dd>
                      <code v-if="typeof v === 'object'">{{ JSON.stringify(v) }}</code>
                      <span v-else>{{ v }}</span>
                    </dd>
                  </template>
                </dl>
              </details>
              <div v-if="showVectorDebug" class="vec">
                <button v-if="!vecDebug[c.id]" class="term-btn term-btn--xs" :disabled="vecLoading[c.id]" @click="loadVectorDebug(c.id)">
                  [ {{ vecLoading[c.id] ? 'loading…' : 'load vector dim + norm' }} ]
                </button>
                <div v-else class="vec__stats">
                  <span><b>dim</b> {{ vecDebug[c.id].dim }}</span>
                  <span><b>L2 norm</b> {{ vecDebug[c.id].norm.toFixed(4) }}</span>
                  <span class="cell-meta">(full vector kept server-side)</span>
                </div>
              </div>
            </li>
          </ol>
        </template>
      </TermBox>
    </section>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getCollection } from '../api/ingestionCollections'
import { listDocuments, uploadDocument, uploadZip, listDocumentChunks, documentBlobUrl, getChunkEmbeddingDebug } from '../api/ingestionDocuments'
import { streamJob } from '../api/ingestionJobs'
import { TermBox, TermButton, TermBadge, TermEmpty, TermModal } from '../components/cli'

const route = useRoute()
const collectionId = ref(Number(route.params.id))

const collection = ref(null)
const loadError = ref('')
const documents = ref([])
const loadingDocs = ref(false)
const selectedDoc = ref(null)
const chunks = ref([])
const loadingChunks = ref(false)

const uploading = ref(false)
const progress = ref(0)
const uploadError = ref('')
const preserveFolderStructure = ref(false)
const zipResult = ref(null)

const showVectorDebug = ref(false)
const vecDebug = ref({})
const vecLoading = ref({})

let pollTimer = null
let sseHandle = null
const TERMINAL = new Set(['indexed', 'failed', 'cancelled'])

onMounted(loadAll)

async function loadAll() {
  loadError.value = ''
  try {
    const { data } = await getCollection(collectionId.value)
    collection.value = data
  } catch (e) {
    loadError.value = `failed to load collection: ${e.response?.data?.detail || e.message}`
    return
  }
  await loadDocs()
  startPolling()
}
async function loadDocs() {
  loadingDocs.value = true
  try {
    const { data } = await listDocuments(collectionId.value)
    documents.value = data
    if (selectedDoc.value) {
      const m = data.find(d => d.id === selectedDoc.value.id)
      if (m) selectedDoc.value = m
    }
  } finally { loadingDocs.value = false }
}
function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    const inFlight = documents.value.some(d => !['indexed', 'failed'].includes(d.status))
    if (!inFlight) return
    try { await loadDocs() } catch {}
    if (selectedDoc.value && selectedDoc.value.status === 'indexed' && chunks.value.length === 0) {
      await loadChunks(selectedDoc.value.id)
    }
  }, 3000)
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }
onUnmounted(() => { stopPolling(); if (sseHandle) { sseHandle.close(); sseHandle = null } })
watch(() => route.params.id, (id) => {
  if (id) { collectionId.value = Number(id); selectedDoc.value = null; chunks.value = []; loadAll() }
})

async function selectDoc(d) {
  if (sseHandle) { sseHandle.close(); sseHandle = null }
  selectedDoc.value = d
  chunks.value = []
  vecDebug.value = {}
  if (d.status === 'indexed' || d.chunk_count > 0) await loadChunks(d.id)
  if (!TERMINAL.has(d.status)) {
    try {
      const detail = await import('../api/ingestionDocuments').then(m => m.getDocument(d.id))
      const jobId = detail.data.latest_job_id
      if (jobId) {
        sseHandle = streamJob(jobId, async (snap) => {
          if (selectedDoc.value?.id !== d.id) return
          if (snap.status === 'succeeded' || snap.status === 'indexed') {
            selectedDoc.value = { ...selectedDoc.value, status: 'indexed' }
            await loadDocs(); await loadChunks(d.id)
          } else if (snap.status === 'failed') {
            selectedDoc.value = { ...selectedDoc.value, status: 'failed', error_message: snap.error_message }
            await loadDocs()
          } else {
            const docStatus = snap.progress_message?.split(' ')[0] || snap.status
            selectedDoc.value = { ...selectedDoc.value, status: docStatus }
          }
        })
      }
    } catch {}
  }
}

async function loadChunks(docId) {
  loadingChunks.value = true
  try {
    const { data } = await listDocumentChunks(docId, { limit: 200 })
    chunks.value = data
  } catch (e) {
    chunks.value = []
    loadError.value = `chunk load failed: ${e.response?.data?.detail || e.message}`
  } finally { loadingChunks.value = false }
}

async function onFilePicked(e) { const f = e.target.files?.[0]; if (f) await doUpload(f); e.target.value = '' }
async function onZipPicked(e) { const f = e.target.files?.[0]; if (f) await doZipUpload(f); e.target.value = '' }
async function onDrop(e) {
  const f = e.dataTransfer?.files?.[0]; if (!f) return
  if (f.name.toLowerCase().endsWith('.zip')) await doZipUpload(f)
  else await doUpload(f)
}
async function doUpload(file) {
  uploading.value = true; progress.value = 0; uploadError.value = ''
  try { await uploadDocument(collectionId.value, file, p => { progress.value = p }); await loadDocs() }
  catch (e) { uploadError.value = e.response?.data?.detail || e.message }
  finally { uploading.value = false; progress.value = 0 }
}
async function doZipUpload(file) {
  uploading.value = true; progress.value = 0; uploadError.value = ''; zipResult.value = null
  try {
    const { data } = await uploadZip(collectionId.value, file, { preserveFolderStructure: preserveFolderStructure.value }, p => { progress.value = p })
    zipResult.value = data
    await loadDocs()
  } catch (e) { uploadError.value = e.response?.data?.detail || e.message }
  finally { uploading.value = false; progress.value = 0 }
}

async function loadVectorDebug(chunkId) {
  if (!selectedDoc.value) return
  vecLoading.value = { ...vecLoading.value, [chunkId]: true }
  try {
    const { data } = await getChunkEmbeddingDebug(selectedDoc.value.id, chunkId)
    vecDebug.value = { ...vecDebug.value, [chunkId]: data }
  } catch (e) {
    uploadError.value = `vector debug failed: ${e.response?.data?.detail || e.message}`
  } finally {
    vecLoading.value = { ...vecLoading.value, [chunkId]: false }
  }
}

function blobUrl(id) { return documentBlobUrl(id) }
function humanBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = Number(n), u = 0
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u += 1 }
  return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`
}
function docVariant(s) {
  return ({ indexed: 'ok', failed: 'danger' })[s] || 'warn'
}
function zipBadgeVariant(s) {
  return ({ enqueued: 'ok', duplicate: 'warn', error: 'danger', too_large: '', skipped: '' })[s] || ''
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; flex-direction: column; gap: 4px; }
.back-link { font-size: var(--t-2xs); color: var(--c-fg-3); text-decoration: none; }
.back-link:hover { color: var(--c-accent); text-decoration: none; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 0; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); margin: 0; }
.page-head__sub code { color: var(--c-accent); background: var(--c-accent-soft); padding: 0 4px; }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.loading { padding: var(--gap-4); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.upload {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--gap-2);
}
.upload__toggle, .filters__toggle { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-sm); color: var(--c-fg-2); cursor: pointer; }
.upload__toggle input, .filters__toggle input { accent-color: var(--c-accent); }

/* Zip result */
.zip-grid {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--gap-2); margin: 0 0 var(--gap-3);
}
.zip-grid > div {
  display: flex; flex-direction: column; gap: 2px;
  padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid var(--c-border); background: var(--c-bg);
}
.zip-grid dt { font-size: var(--t-2xs); color: var(--c-fg-3); text-transform: uppercase; letter-spacing: var(--tracking-caps); }
.zip-grid dd { margin: 0; font-size: var(--t-md); color: var(--c-fg-1); font-weight: 600; }
.zip-grid .is-ok { border-color: var(--c-ok); }
.zip-grid .is-warn { border-color: var(--c-warn); }
.zip-grid .is-err { border-color: var(--c-danger); }

.zip-detail { font-size: var(--t-xs); color: var(--c-fg-2); }
.zip-detail summary { cursor: pointer; padding: 4px 0; }
.zip-list { list-style: none; padding: 0; margin: var(--gap-2) 0; max-height: 320px; overflow-y: auto; }
.zip-list li {
  display: grid; grid-template-columns: 1fr auto auto; gap: var(--gap-2); align-items: center;
  padding: 4px var(--gap-2); border-bottom: var(--border-w) dashed var(--c-border);
}
.zip-list__name { word-break: break-all; }

/* Doc list + inspector split */
.split { display: grid; grid-template-columns: 320px 1fr; gap: var(--gap-3); }
@media (max-width: 1000px) { .split { grid-template-columns: 1fr; } }

.docs { list-style: none; padding: 0; margin: 0; max-height: 600px; overflow-y: auto; }
.doc {
  padding: var(--gap-2) var(--gap-3);
  border-bottom: var(--border-w) solid var(--c-border);
  cursor: pointer;
  border-left: 2px solid transparent;
}
.doc:hover { background: var(--c-row-hover); }
.doc.is-selected { background: var(--c-accent-soft); border-left-color: var(--c-accent); }
.doc.is-failed { border-left-color: var(--c-danger); }
.doc.is-indexed { border-left-color: var(--c-ok); }
.doc.is-pending, .doc.is-parsing, .doc.is-chunking, .doc.is-embedding { border-left-color: var(--c-warn); }
.doc__row { display: flex; justify-content: space-between; align-items: center; gap: var(--gap-2); }
.doc__name { color: var(--c-fg-1); font-size: var(--t-sm); word-break: break-all; }
.doc__err { color: var(--c-danger); font-size: var(--t-2xs); margin-top: 4px; }

.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.row-actions__sep { color: var(--c-border-strong); }

.insp-bar {
  display: flex; align-items: center; gap: var(--gap-2); flex-wrap: wrap;
  padding-bottom: var(--gap-2); border-bottom: var(--border-w) dashed var(--c-border); margin-bottom: var(--gap-3);
}

.chunks { list-style: none; padding: 0; margin: 0; counter-reset: chunk; display: flex; flex-direction: column; gap: var(--gap-3); }
.chunk { background: var(--c-bg); border: var(--border-w) solid var(--c-border); padding: var(--gap-2) var(--gap-3); counter-increment: chunk; }
.chunk__head { display: flex; justify-content: space-between; align-items: center; gap: var(--gap-2); margin-bottom: 6px; }
.chunk__key { font-family: var(--font-mono); font-size: var(--t-2xs); color: var(--c-accent); }
.chunk__content {
  background: var(--c-surface-1); padding: var(--gap-2); border: var(--border-w) solid var(--c-border);
  white-space: pre-wrap; word-break: break-word; font-size: var(--t-sm); color: var(--c-fg-1);
  margin: 0; max-height: 220px; overflow-y: auto;
}
.chunk__meta { margin-top: 6px; font-size: var(--t-2xs); }
.chunk__meta summary { cursor: pointer; color: var(--c-fg-3); }
.chunk__meta-grid { display: grid; grid-template-columns: max-content 1fr; gap: 2px var(--gap-2); margin-top: 4px; }
.chunk__meta-grid dt { color: var(--c-fg-3); }
.chunk__meta-grid dd { margin: 0; color: var(--c-fg-2); word-break: break-word; }

.vec {
  margin-top: var(--gap-2);
  padding: var(--gap-2);
  background: var(--c-surface-2);
  border: var(--border-w) dashed var(--c-border);
}
.vec__stats { display: flex; gap: var(--gap-3); flex-wrap: wrap; font-size: var(--t-xs); color: var(--c-fg-2); }
.vec__stats b { color: var(--c-fg-3); font-weight: 500; margin-right: 4px; }
</style>
