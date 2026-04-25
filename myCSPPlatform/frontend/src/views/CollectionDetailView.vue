<template>
  <div class="cd-root">
    <!-- Header / breadcrumb -->
    <header class="page-header">
      <router-link :to="{ name: 'KnowledgeCollections' }" class="back">← Knowledge Collections</router-link>
      <h1 v-if="collection">📁 {{ collection.name }}</h1>
      <h1 v-else>載入中…</h1>
      <p v-if="collection" class="subtitle">
        agent #{{ collection.agent_id }} ·
        {{ collection.embedding_model }} · {{ collection.embedding_dim }}-d ·
        strategy: <code>{{ collection.chunking_config.strategy }}</code>
      </p>
    </header>

    <div v-if="loadError" class="banner error">{{ loadError }}</div>

    <!-- Upload zone -->
    <section v-if="collection" class="upload-zone" @drop.prevent="onDrop" @dragover.prevent>
      <input
        ref="fileInput"
        type="file"
        accept=".txt,.md,.markdown,.pdf,.docx,.doc,.odt,.rtf,text/plain,text/markdown,application/pdf"
        @change="onFilePicked"
        style="display: none"
      />
      <input
        ref="zipInput"
        type="file"
        accept=".zip,application/zip"
        @change="onZipPicked"
        style="display: none"
      />
      <button class="primary" @click="$refs.fileInput.click()" :disabled="uploading">
        {{ uploading ? `上傳中… ${Math.round(progress * 100)}%` : '＋ 上傳單檔' }}
      </button>
      <button class="ghost" @click="$refs.zipInput.click()" :disabled="uploading">
        📦 上傳 zip（多檔）
      </button>
      <label class="toggle">
        <input type="checkbox" v-model="preserveFolderStructure" />
        <span>保留資料夾路徑（zip）</span>
      </label>
      <p class="hint">或拖檔到這裡。單檔 ≤ 50 MB；zip ≤ 500 MB / 200 files。</p>
      <div v-if="uploadError" class="banner error inline">{{ uploadError }}</div>
    </section>

    <!-- Zip result summary modal -->
    <div v-if="zipResult" class="modal-overlay" @click.self="zipResult = null">
      <div class="modal">
        <h2>Zip 上傳結果</h2>
        <dl class="zip-summary">
          <div><dt>檔案總數</dt><dd>{{ zipResult.files_in_archive }}</dd></div>
          <div class="enq"><dt>已 enqueue</dt><dd>{{ zipResult.enqueued }}</dd></div>
          <div class="dup"><dt>重複（sha256）</dt><dd>{{ zipResult.duplicates }}</dd></div>
          <div class="skip"><dt>跳過</dt><dd>{{ zipResult.skipped }}</dd></div>
          <div class="err" v-if="zipResult.errors"><dt>錯誤</dt><dd>{{ zipResult.errors }}</dd></div>
        </dl>
        <details>
          <summary>逐檔結果（{{ zipResult.results.length }}）</summary>
          <ul class="zip-results">
            <li v-for="r in zipResult.results" :key="r.filename" :class="r.status">
              <span class="name">{{ r.filename }}</span>
              <span class="badge" :class="r.status">{{ r.status }}</span>
              <span v-if="r.detail" class="detail">{{ r.detail }}</span>
            </li>
          </ul>
        </details>
        <div class="modal-actions">
          <button class="primary" @click="zipResult = null">關閉</button>
        </div>
      </div>
    </div>

    <!-- Documents list (left) + chunks panel (right when expanded) -->
    <section v-if="collection" class="split">
      <div class="docs">
        <h2>📄 Documents <span class="muted">({{ documents.length }})</span></h2>
        <div v-if="loadingDocs" class="banner muted">載入中…</div>
        <div v-else-if="documents.length === 0" class="banner muted">尚無 documents — 從上方上傳。</div>
        <ul v-else class="doc-list">
          <li
            v-for="d in documents"
            :key="d.id"
            class="doc-item"
            :class="{ selected: selectedDoc?.id === d.id, [d.status]: true }"
            @click="selectDoc(d)"
          >
            <div class="doc-row1">
              <span class="filename">{{ d.filename }}</span>
              <span class="badge" :class="d.status">{{ d.status }}</span>
            </div>
            <div class="doc-row2 muted">
              {{ humanBytes(d.bytes) }} · {{ d.chunk_count }} chunks · sha {{ d.sha256.slice(0, 8) }}…
            </div>
            <div v-if="d.error_message" class="doc-error">⚠ {{ d.error_message }}</div>
          </li>
        </ul>
      </div>

      <div class="inspector">
        <h2>
          🔍 Inspector
          <span v-if="selectedDoc" class="muted">— {{ selectedDoc.filename }}</span>
        </h2>
        <div v-if="!selectedDoc" class="banner muted">選一個 document 看 chunks。</div>
        <template v-else>
          <div class="inspector-actions">
            <a :href="blobUrl(selectedDoc.id)" target="_blank" class="link">📥 下載原檔</a>
            <label class="toggle">
              <input type="checkbox" v-model="showVectorDebug" />
              <span>Show vector debug</span>
            </label>
          </div>

          <div v-if="loadingChunks" class="banner muted">chunks 載入中…</div>
          <div v-else-if="chunks.length === 0" class="banner muted">
            尚無 chunks（doc status: {{ selectedDoc.status }}）。
          </div>
          <ol v-else class="chunk-list">
            <li v-for="c in chunks" :key="c.id" class="chunk">
              <header>
                <code class="chunk-key">{{ c.chunk_key }}</code>
                <span class="muted">id={{ c.id }} · {{ c.token_count }} tokens</span>
              </header>
              <pre class="chunk-content">{{ c.content }}</pre>

              <details class="meta">
                <summary>metadata</summary>
                <dl class="meta-grid">
                  <template v-for="(v, k) in c.metadata" :key="k">
                    <dt>{{ k }}</dt>
                    <dd>
                      <code v-if="typeof v === 'object'">{{ JSON.stringify(v) }}</code>
                      <span v-else>{{ v }}</span>
                    </dd>
                  </template>
                </dl>
              </details>

              <div v-if="showVectorDebug" class="vec-debug">
                <button
                  v-if="!vecDebug[c.id]"
                  class="ghost small"
                  :disabled="vecLoading[c.id]"
                  @click="loadVectorDebug(c.id)"
                >
                  {{ vecLoading[c.id] ? '載入中…' : 'Load vector dim + norm' }}
                </button>
                <div v-else class="vec-stats">
                  <span><b>dim</b>: {{ vecDebug[c.id].dim }}</span>
                  <span><b>L2 norm</b>: {{ vecDebug[c.id].norm.toFixed(4) }}</span>
                  <span class="muted">(full 4000-d 不傳到瀏覽器)</span>
                </div>
              </div>
            </li>
          </ol>
        </template>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getCollection } from '../api/ingestionCollections'
import {
  listDocuments,
  uploadDocument,
  uploadZip,
  listDocumentChunks,
  documentBlobUrl,
  getChunkEmbeddingDebug,
} from '../api/ingestionDocuments'
import { streamJob } from '../api/ingestionJobs'

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

// ── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(async () => {
  await loadAll()
})

async function loadAll() {
  loadError.value = ''
  try {
    const { data } = await getCollection(collectionId.value)
    collection.value = data
  } catch (e) {
    loadError.value = `載入 collection 失敗：${e.response?.data?.detail || e.message}`
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
    // Re-link selectedDoc to refreshed row (so status changes propagate).
    if (selectedDoc.value) {
      const m = data.find((d) => d.id === selectedDoc.value.id)
      if (m) selectedDoc.value = m
    }
  } finally {
    loadingDocs.value = false
  }
}

// Poll every 3s as long as any doc is mid-pipeline.
function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    const inFlight = documents.value.some(
      (d) => !['indexed', 'failed'].includes(d.status),
    )
    if (!inFlight) return
    try { await loadDocs() } catch { /* ignore transient */ }
    // If we have a selected doc and it just finished, refresh its chunks.
    if (selectedDoc.value && selectedDoc.value.status === 'indexed' && chunks.value.length === 0) {
      await loadChunks(selectedDoc.value.id)
    }
  }, 3000)
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

onUnmounted(() => {
  stopPolling()
  if (sseHandle) { sseHandle.close(); sseHandle = null }
})

watch(() => route.params.id, (id) => {
  if (id) {
    collectionId.value = Number(id)
    selectedDoc.value = null
    chunks.value = []
    loadAll()
  }
})

// ── Selection ───────────────────────────────────────────────────────────────

async function selectDoc(d) {
  // Tear down any prior SSE — only one selected doc at a time.
  if (sseHandle) { sseHandle.close(); sseHandle = null }

  selectedDoc.value = d
  chunks.value = []
  vecDebug.value = {}
  if (d.status === 'indexed' || d.chunk_count > 0) {
    await loadChunks(d.id)
  }

  // If the doc isn't terminal, the latest job will keep moving —
  // open SSE for live status updates (faster than the 3s poll).
  if (!TERMINAL.has(d.status)) {
    try {
      const detail = await import('../api/ingestionDocuments').then(m => m.getDocument(d.id))
      const jobId = detail.data.latest_job_id
      if (jobId) {
        sseHandle = streamJob(jobId, async (snap) => {
          if (selectedDoc.value?.id !== d.id) return  // user switched docs
          // Patch the selected doc's render-relevant fields.
          if (snap.status === 'succeeded' || snap.status === 'indexed') {
            selectedDoc.value = { ...selectedDoc.value, status: 'indexed' }
            await loadDocs()
            await loadChunks(d.id)
          } else if (snap.status === 'failed') {
            selectedDoc.value = {
              ...selectedDoc.value,
              status: 'failed',
              error_message: snap.error_message,
            }
            await loadDocs()
          } else {
            // running / parsing / chunking / embedding / indexing →
            // we synthesise a doc-level status from the job for the
            // sidebar progress badge.
            const docStatus = snap.progress_message?.split(' ')[0] || snap.status
            selectedDoc.value = { ...selectedDoc.value, status: docStatus }
          }
        })
      }
    } catch {
      // Fall back to the existing 3s poll if SSE init fails.
    }
  }
}

async function loadChunks(docId) {
  loadingChunks.value = true
  try {
    const { data } = await listDocumentChunks(docId, { limit: 200 })
    chunks.value = data
  } catch (e) {
    chunks.value = []
    loadError.value = `chunks 載入失敗：${e.response?.data?.detail || e.message}`
  } finally {
    loadingChunks.value = false
  }
}

// ── Upload ──────────────────────────────────────────────────────────────────

async function onFilePicked(e) {
  const file = e.target.files?.[0]
  if (file) await doUpload(file)
  e.target.value = ''  // allow re-uploading same name after pipeline
}

async function onZipPicked(e) {
  const file = e.target.files?.[0]
  if (file) await doZipUpload(file)
  e.target.value = ''
}

async function onDrop(e) {
  const file = e.dataTransfer?.files?.[0]
  if (!file) return
  if (file.name.toLowerCase().endsWith('.zip')) {
    await doZipUpload(file)
  } else {
    await doUpload(file)
  }
}

async function doUpload(file) {
  uploading.value = true
  progress.value = 0
  uploadError.value = ''
  try {
    await uploadDocument(collectionId.value, file, (p) => { progress.value = p })
    await loadDocs()
  } catch (e) {
    uploadError.value = e.response?.data?.detail || e.message
  } finally {
    uploading.value = false
    progress.value = 0
  }
}

async function doZipUpload(file) {
  uploading.value = true
  progress.value = 0
  uploadError.value = ''
  zipResult.value = null
  try {
    const { data } = await uploadZip(
      collectionId.value,
      file,
      { preserveFolderStructure: preserveFolderStructure.value },
      (p) => { progress.value = p },
    )
    zipResult.value = data
    await loadDocs()
  } catch (e) {
    uploadError.value = e.response?.data?.detail || e.message
  } finally {
    uploading.value = false
    progress.value = 0
  }
}

// ── Vector debug ────────────────────────────────────────────────────────────

async function loadVectorDebug(chunkId) {
  if (!selectedDoc.value) return
  vecLoading.value = { ...vecLoading.value, [chunkId]: true }
  try {
    const { data } = await getChunkEmbeddingDebug(selectedDoc.value.id, chunkId)
    vecDebug.value = { ...vecDebug.value, [chunkId]: data }
  } catch (e) {
    uploadError.value = `vector debug 失敗：${e.response?.data?.detail || e.message}`
  } finally {
    vecLoading.value = { ...vecLoading.value, [chunkId]: false }
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function blobUrl(id) { return documentBlobUrl(id) }

function humanBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = Number(n), u = 0
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u += 1 }
  return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`
}
</script>

<style scoped>
.cd-root { padding: 1.25rem; max-width: 1400px; }

.page-header { margin-bottom: 1rem; }
.back { color: #2563eb; text-decoration: none; font-size: 0.85rem; }
.back:hover { text-decoration: underline; }
.page-header h1 { margin: 0.25rem 0 0.25rem; font-size: 1.4rem; }
.subtitle { margin: 0 0 1rem; color: #6b7280; font-size: 0.85rem; }
.subtitle code { background: #f3f4f6; padding: 0.1rem 0.3rem; border-radius: 3px; }

.upload-zone {
  display: flex; align-items: center; gap: 1rem;
  padding: 1rem; background: #f9fafb; border: 2px dashed #d1d5db; border-radius: 6px;
  margin-bottom: 1rem;
}
.hint { margin: 0; color: #9ca3af; font-size: 0.85rem; }

.split { display: grid; grid-template-columns: 320px 1fr; gap: 1rem; }
.docs h2, .inspector h2 { margin: 0 0 0.75rem; font-size: 1.05rem; }
.muted { color: #9ca3af; font-size: 0.85rem; font-weight: normal; }

.doc-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.4rem; }
.doc-item {
  background: #fff; border: 1px solid #e5e7eb; border-radius: 4px; padding: 0.6rem 0.8rem;
  cursor: pointer; transition: border-color 0.1s;
}
.doc-item:hover { border-color: #93c5fd; }
.doc-item.selected { border-color: #2563eb; background: #eff6ff; }
.doc-item.failed { border-left: 3px solid #dc2626; }
.doc-item.pending, .doc-item.parsing, .doc-item.chunking, .doc-item.embedding { border-left: 3px solid #f59e0b; }
.doc-item.indexed { border-left: 3px solid #10b981; }
.doc-row1 { display: flex; justify-content: space-between; align-items: center; }
.filename { font-size: 0.9rem; word-break: break-all; }
.doc-row2 { font-size: 0.75rem; margin-top: 0.2rem; }
.doc-error { color: #b91c1c; font-size: 0.8rem; margin-top: 0.3rem; }

.badge {
  font-size: 0.65rem; padding: 0.15rem 0.45rem; border-radius: 999px;
  text-transform: uppercase; flex-shrink: 0;
}
.badge.indexed { background: #d1fae5; color: #065f46; }
.badge.failed { background: #fee2e2; color: #991b1b; }
.badge.pending, .badge.parsing, .badge.chunking, .badge.embedding {
  background: #fef3c7; color: #92400e;
}

.inspector { background: #fff; border: 1px solid #e5e7eb; border-radius: 6px; padding: 1rem; min-height: 300px; }
.inspector-actions { display: flex; gap: 1rem; align-items: center; margin-bottom: 0.75rem; }
.link { color: #2563eb; text-decoration: none; font-size: 0.85rem; }
.link:hover { text-decoration: underline; }
.toggle { display: flex; gap: 0.4rem; align-items: center; font-size: 0.85rem; color: #4b5563; cursor: pointer; }

.chunk-list { list-style: decimal; padding-left: 1.5rem; margin: 0; display: flex; flex-direction: column; gap: 0.85rem; }
.chunk { background: #fafafa; border: 1px solid #e5e7eb; border-radius: 4px; padding: 0.75rem; }
.chunk header { display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; margin-bottom: 0.4rem; flex-wrap: wrap; }
.chunk-key { font-size: 0.8rem; color: #6b7280; }
.chunk-content {
  background: #fff; padding: 0.5rem 0.7rem; border: 1px solid #e5e7eb; border-radius: 3px;
  white-space: pre-wrap; word-break: break-word; font-size: 0.85rem; line-height: 1.5;
  margin: 0; max-height: 250px; overflow-y: auto;
}
.meta { margin-top: 0.5rem; font-size: 0.8rem; }
.meta summary { cursor: pointer; color: #6b7280; }
.meta-grid { display: grid; grid-template-columns: max-content 1fr; gap: 0.2rem 0.75rem; margin-top: 0.4rem; }
.meta-grid dt { color: #9ca3af; }
.meta-grid dd { margin: 0; word-break: break-word; }

.vec-debug { margin-top: 0.5rem; padding: 0.4rem 0.6rem; background: #f3f4f6; border-radius: 3px; }
button.small { font-size: 0.8rem; padding: 0.3rem 0.6rem; }
.vec-stats { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.85rem; }
.vec-stats b { color: #4b5563; font-weight: 600; }

button { padding: 0.5rem 0.9rem; border: 1px solid transparent; border-radius: 4px; cursor: pointer; font-size: 0.9rem; }
button.primary { background: #2563eb; color: #fff; }
button.primary:hover:not(:disabled) { background: #1d4ed8; }
button.primary:disabled { opacity: 0.5; cursor: not-allowed; }
button.ghost { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }

.banner { padding: 0.6rem 0.8rem; border-radius: 4px; margin-bottom: 0.5rem; font-size: 0.85rem; }
.banner.muted { background: #f9fafb; color: #6b7280; }
.banner.error { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.banner.inline { margin-top: 0.5rem; }

/* Zip result modal */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: #fff; padding: 1.5rem; border-radius: 6px; min-width: 480px; max-width: 90vw; max-height: 80vh; overflow-y: auto; }
.modal h2 { margin: 0 0 1rem; font-size: 1.1rem; }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 1rem; }
.zip-summary { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem 1rem; margin-bottom: 1rem; }
.zip-summary > div { display: flex; flex-direction: column; padding: 0.5rem 0.75rem; border-radius: 4px; background: #f9fafb; }
.zip-summary dt { font-size: 0.7rem; color: #9ca3af; text-transform: uppercase; }
.zip-summary dd { margin: 0; font-weight: 600; font-size: 1.1rem; }
.zip-summary .enq { background: #d1fae5; }
.zip-summary .dup { background: #fef3c7; }
.zip-summary .skip { background: #e5e7eb; }
.zip-summary .err { background: #fee2e2; }
.zip-results { list-style: none; padding: 0; margin: 0.5rem 0; max-height: 400px; overflow-y: auto; font-size: 0.85rem; }
.zip-results li { display: grid; grid-template-columns: 1fr auto auto; gap: 0.5rem; padding: 0.4rem 0.5rem; border-bottom: 1px solid #f3f4f6; align-items: center; }
.zip-results li.error, .zip-results li.too_large { background: #fef2f2; }
.zip-results .name { word-break: break-all; }
.zip-results .detail { color: #6b7280; font-size: 0.8rem; }
.zip-results .badge.enqueued { background: #d1fae5; color: #065f46; }
.zip-results .badge.duplicate { background: #fef3c7; color: #92400e; }
.zip-results .badge.skipped, .zip-results .badge.too_large { background: #e5e7eb; color: #374151; }
.zip-results .badge.error { background: #fee2e2; color: #991b1b; }
</style>
