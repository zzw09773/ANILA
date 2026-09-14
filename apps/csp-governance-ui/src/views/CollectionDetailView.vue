<template>
  <div class="page">
    <header class="page-head">
      <div>
        <router-link :to="{ name: 'KnowledgeCollections' }" class="back-link">← 知識庫</router-link>
        <h1 class="page-head__title">
          <span v-if="collection">{{ collection.name }}</span>
          <span v-else>載入中…</span>
        </h1>
        <p v-if="collection" class="page-head__sub">
          {{ collection.embedding_model }} · {{ collection.embedding_dim }}-d ·
          策略 <code>{{ collection.chunking_config.strategy }}</code>
          · 圖說 {{ captionIntentLabel }}
        </p>
      </div>
    </header>

    <div v-if="loadError" class="feedback is-err">! {{ loadError }}</div>

    <TermBox
      v-if="collection"
      title="密等"
      pad="md"
      hint="建立時選定；只能往上調，不能自行降級。預設無機密。"
    >
      <div class="cls-row">
        <div class="cls-current">
          <span class="cell-meta">目前密等</span>
          <strong class="cls-current__level">{{ collection.classification_level || '無機密' }}</strong>
        </div>
        <div class="cls-raise" v-if="raisableLevels.length">
          <label class="cell-meta" for="raise-level">升密至</label>
          <select id="raise-level" v-model="raiseTarget" class="term-select cls-raise__select">
            <option v-for="lvl in raisableLevels" :key="lvl" :value="lvl">{{ lvl }}</option>
          </select>
          <TermButton
            variant="primary"
            :disabled="!raiseTarget || raising"
            :loading="raising"
            :label="raising ? '升密中…' : '確認升密'"
            @click="doRaiseClassification"
          />
        </div>
        <span v-else class="cell-meta">已是最高密等「機密」；降級請走降密申請流程。</span>
      </div>
      <div v-if="raiseError" class="feedback is-err" style="margin-top: var(--gap-2);">! {{ raiseError }}</div>
      <div v-if="raiseMsg" class="feedback is-ok" style="margin-top: var(--gap-2);">{{ raiseMsg }}</div>
    </TermBox>

    <TermBox v-if="collection" title="上傳 · 匯入" pad="md" :hint="INGESTION_FILE_ACCEPT_HINT">
      <div class="upload" @drop.prevent="onDrop" @dragover.prevent>
        <input ref="fileInput" type="file" multiple :accept="INGESTION_FILE_ACCEPT" @change="onFilePicked" style="display:none" />
        <input ref="zipInput" type="file" accept=".zip,application/zip" @change="onZipPicked" style="display:none" />
        <TermButton variant="primary" :disabled="uploading" :loading="uploading" :label="uploading ? `上傳中… ${Math.round(progress * 100)}%` : '+ 檔案(可多選)'" @click="$refs.fileInput.click()" />
        <TermButton :disabled="uploading" label="+ zip · 多檔" @click="$refs.zipInput.click()" />
        <label class="upload__toggle">
          <input type="checkbox" v-model="preserveFolderStructure" />
          <span>保留資料夾路徑（zip）</span>
        </label>
        <span class="cell-meta">也可拖放上傳</span>
      </div>
      <div v-if="uploadError" class="feedback is-err" style="margin-top: var(--gap-2);">! {{ uploadError }}</div>
    </TermBox>

    <!-- Zip result modal --------------------------------------------- -->
    <TermModal :visible="!!zipResult" title="zip · 結果" width="640px" @close="zipResult = null">
      <dl v-if="zipResult" class="zip-grid">
        <div><dt>壓縮檔內檔案</dt><dd class="tnum">{{ zipResult.files_in_archive }}</dd></div>
        <div class="is-ok"><dt>已排入佇列</dt><dd class="tnum">{{ zipResult.enqueued }}</dd></div>
        <div class="is-warn"><dt>重複</dt><dd class="tnum">{{ zipResult.duplicates }}</dd></div>
        <div><dt>已略過</dt><dd class="tnum">{{ zipResult.skipped }}</dd></div>
        <div v-if="zipResult.errors" class="is-err"><dt>錯誤</dt><dd class="tnum">{{ zipResult.errors }}</dd></div>
      </dl>
      <details v-if="zipResult" class="zip-detail">
        <summary>各檔結果 · {{ zipResult.results.length }}</summary>
        <ul class="zip-list">
          <li v-for="r in zipResult.results" :key="r.filename" :class="r.status">
            <span class="zip-list__name">{{ r.filename }}</span>
            <TermBadge :variant="zipBadgeVariant(r.status)">{{ r.status }}</TermBadge>
            <span v-if="r.detail" class="cell-meta">{{ r.detail }}</span>
          </li>
        </ul>
      </details>
      <template #footer>
        <TermButton variant="primary" @click="zipResult = null" label="關閉" />
      </template>
    </TermModal>

    <section v-if="collection" class="split">
      <TermBox :title="`文件 · ${documents.length}`" pad="none" flush>
        <div v-if="loadingDocs" class="loading">載入中…</div>
        <TermEmpty v-else-if="documents.length === 0" message="尚無文件 · 請於上方上傳" />
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
            <div class="cell-meta tnum">{{ humanBytes(d.bytes) }} · {{ d.chunk_count }} 個區塊 · sha {{ d.sha256.slice(0, 8) }}…</div>
            <div v-if="d.latest_job_progress_message" class="cell-meta">{{ d.latest_job_progress_message }}</div>
            <div v-if="d.error_message" class="doc__err">! {{ d.error_message }}</div>
            <button
              v-if="d.status === 'failed'"
              class="term-action doc__reembed"
              :disabled="reprocessingId === d.id"
              @click.stop="doReprocess(d)"
            >↻ {{ reprocessingId === d.id ? '重新嵌入中…' : '重新嵌入' }}</button>
          </li>
        </ul>
      </TermBox>

      <TermBox :title="selectedDoc ? `檢視器 · ${selectedDoc.filename}` : '檢視器'" pad="md">
        <div v-if="!selectedDoc">
          <TermEmpty message="選一份文件以檢視區塊" />
        </div>
        <template v-else>
          <div class="insp-bar">
            <a :href="blobUrl(selectedDoc.id)" target="_blank" class="term-action">↓ 下載原始檔</a>
            <span class="row-actions__sep">·</span>
            <label class="filters__toggle">
              <input type="checkbox" v-model="showVectorDebug" />
              <span>顯示向量除錯</span>
            </label>
          </div>

          <div v-if="loadingChunks" class="loading">載入區塊中…</div>
          <TermEmpty v-else-if="chunks.length === 0" :message="`無區塊 · 文件狀態：${selectedDoc.status}`" />
          <ol v-else class="chunks">
            <li v-for="c in chunks" :key="c.id" class="chunk">
              <header class="chunk__head">
                <code class="chunk__key">{{ c.chunk_key }}</code>
                <span class="cell-meta tnum">id {{ c.id }} · {{ c.token_count }} tokens</span>
              </header>
              <pre class="chunk__content">{{ c.content }}</pre>
              <details class="chunk__meta">
                <summary>中繼資料</summary>
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
                <div v-if="vecError[c.id]" class="vec__err">! {{ vecError[c.id] }}</div>
                <button v-if="!vecDebug[c.id] && !vecError[c.id]" class="term-btn term-btn--xs" :disabled="vecLoading[c.id]" @click="loadVectorDebug(c.id)">
                  [ {{ vecLoading[c.id] ? '載入中…' : '載入向量維度 + 範數' }} ]
                </button>
                <div v-else-if="vecDebug[c.id]?.note" class="vec__stats">
                  <span class="cell-meta">{{ vecDebug[c.id].note }}</span>
                </div>
                <div v-else-if="vecDebug[c.id]" class="vec__stats">
                  <span><b>dim</b> {{ vecDebug[c.id].dim }}</span>
                  <span><b>L2 norm</b> {{ vecDebug[c.id].norm == null ? '—' : vecDebug[c.id].norm.toFixed(4) }}</span>
                  <span class="cell-meta">（完整向量保存在伺服器端）</span>
                </div>
              </div>
            </li>
          </ol>
        </template>
      </TermBox>
    </section>

    <!-- Cross-document relations ------------------------------------- -->
    <TermBox
      v-if="collection"
      :title="`relations · ${relations.length}`"
      pad="md"
      hint="文件之間的引用、補充或修正關係"
    >
      <div class="rel-bar">
        <div class="rel-toggle">
          <button :class="['rel-toggle__btn', { 'is-on': relView === 'table' }]" @click="relView = 'table'">表格</button>
          <button :class="['rel-toggle__btn', { 'is-on': relView === 'graph' }]" @click="relView = 'graph'">圖</button>
        </div>
        <TermButton
          :loading="reresolving"
          :disabled="reresolving"
          :label="reresolving ? '對帳中…' : '↻ 重新解析 · 重新掃描 + 重新抽取'"
          @click="doReresolve"
        />
        <span v-if="relMsg" class="cell-meta">{{ relMsg }}</span>
      </div>

      <!-- manual add -->
      <form class="rel-add" @submit.prevent="doCreateRelation">
        <label class="rel-field"><span class="rel-field__lab">來源文件</span><select v-model.number="newRel.src_document_id" required class="rel-input" aria-label="來源文件">
          <option :value="0" disabled>來源文件…</option>
          <option v-for="d in documents" :key="`s${d.id}`" :value="d.id">{{ d.title || d.filename }}</option>
        </select>
        </label><label class="rel-field"><span class="rel-field__lab">關係</span><select v-model="newRel.relation_type" class="rel-input rel-input--type" aria-label="關係">
          <option v-for="t in RELATION_TYPES" :key="t" :value="t">{{ t }}</option>
        </select>
        </label><label class="rel-field"><span class="rel-field__lab">目標文件</span><select v-model.number="newRel.dst_document_id" class="rel-input" aria-label="目標文件">
          <option :value="0">目標文件…（或輸入名稱 →）</option>
          <option v-for="d in documents" :key="`d${d.id}`" :value="d.id">{{ d.title || d.filename }}</option>
        </select>
        </label><input
          v-model.trim="newRel.target_ref"
          class="rel-input"
          aria-label="目標名稱" placeholder="…或輸入目標名稱"
          :disabled="!!newRel.dst_document_id"
        />
        <TermButton type="submit" variant="primary" :disabled="creating || !newRel.src_document_id" label="+ 新增" />
      </form>
      <div v-if="relError" class="feedback is-err" style="margin-top: var(--gap-2);">! {{ relError }}</div>

      <div v-if="loadingRels" class="loading">載入關聯中…</div>
      <TermEmpty v-else-if="relations.length === 0" message="尚無關聯 · 上傳有連結的文件或於上方新增" />
      <RelationGraph v-else-if="relView === 'graph'" :relations="relations" :documents="documents" />
      <table v-else class="rel-table">
        <thead>
          <tr><th>來源</th><th>類型</th><th>目標</th><th>方式</th><th>佐證</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in relations" :key="r.id" :class="{ 'rel--unresolved': !r.resolved }">
            <td class="rel-doc">{{ r.src_title || `#${r.src_document_id}` }}</td>
            <td><TermBadge variant="">{{ r.relation_type }}</TermBadge></td>
            <td class="rel-doc">
              <span v-if="r.resolved">{{ r.dst_title || `#${r.dst_document_id}` }}</span>
              <span v-else class="rel-target">
                {{ r.target_ref }}
                <TermBadge v-if="r.ambiguous" variant="warn">模糊</TermBadge>
                <TermBadge v-else variant="danger">未解析</TermBadge>
              </span>
            </td>
            <td><TermBadge :variant="r.source === 'manual' ? 'ok' : ''">{{ r.source }}</TermBadge></td>
            <td class="rel-ev" :title="r.evidence || ''">{{ r.evidence || '—' }}</td>
            <td>
              <button
                v-if="r.source === 'manual'"
                class="term-btn term-btn--xs"
                :disabled="deletingId === r.id"
                @click="doDeleteRelation(r)"
              >刪除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getCollection, raiseCollectionClassification } from '../api/ingestionCollections'
import { listDocuments, uploadDocument, uploadZip, listDocumentChunks, documentBlobUrl, getChunkEmbeddingDebug, reprocessDocument } from '../api/ingestionDocuments'
import { listRelations, createRelation, deleteRelation, reresolveRelations } from '../api/ingestionRelations'
import { streamJob } from '../api/ingestionJobs'
import { extractError } from '../api/errors'
import { TermBox, TermButton, TermBadge, TermEmpty, TermModal } from '../components/cli'
import RelationGraph from '../components/RelationGraph.vue'
import {
  INGESTION_FILE_ACCEPT,
  INGESTION_FILE_ACCEPT_HINT,
} from '../utils/ingestionFileAccept'

const CLASSIFICATION_LEVELS = ['無機密', '營業秘密', '密', '機密']

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
const reprocessingId = ref(null)
const preserveFolderStructure = ref(false)
const zipResult = ref(null)

const raiseTarget = ref('')
const raising = ref(false)
const raiseError = ref('')
const raiseMsg = ref('')

const raisableLevels = computed(() => {
  const current = collection.value?.classification_level || '無機密'
  const idx = CLASSIFICATION_LEVELS.indexOf(current)
  if (idx < 0) return CLASSIFICATION_LEVELS.slice(1)
  return CLASSIFICATION_LEVELS.slice(idx + 1)
})

watch(raisableLevels, (levels) => {
  raiseTarget.value = levels[0] || ''
}, { immediate: true })

const captionIntentLabel = computed(() => {
  const c = collection.value
  if (!c) return '—'
  if (c.caption_enabled === true) {
    return c.caption_model ? `要 · ${c.caption_model}` : '要 · 跟隨平台模型'
  }
  if (c.caption_enabled === false) return '不要'
  return '跟隨平台'
})

const showVectorDebug = ref(false)
const vecDebug = ref({})
const vecLoading = ref({})
const vecError = ref({})

// ── Cross-document relations ───────────────────────────────────────────────
const RELATION_TYPES = ['based_on', 'amends', 'supersedes', 'cites', 'supplements', 'relates']
const relView = ref('table')
const relations = ref([])
const loadingRels = ref(false)
const relError = ref('')
const relMsg = ref('')
const creating = ref(false)
const reresolving = ref(false)
const deletingId = ref(null)
const newRel = ref({ src_document_id: 0, relation_type: 'cites', dst_document_id: 0, target_ref: '', evidence: '' })

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
    loadError.value = `載入知識庫失敗：${extractError(e, e.message)}`
    return
  }
  await loadDocs()
  await loadRelations()
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
    loadError.value = `載入區塊失敗：${extractError(e, e.message)}`
  } finally { loadingChunks.value = false }
}

async function onFilePicked(e) { const fs = [...(e.target.files || [])]; if (fs.length) await doUploadMany(fs); e.target.value = '' }
async function onZipPicked(e) { const f = e.target.files?.[0]; if (f) await doZipUpload(f); e.target.value = '' }
async function onDrop(e) {
  const fs = [...(e.dataTransfer?.files || [])]; if (!fs.length) return
  const zips = fs.filter(f => f.name.toLowerCase().endsWith('.zip'))
  const plain = fs.filter(f => !f.name.toLowerCase().endsWith('.zip'))
  if (plain.length) await doUploadMany(plain)
  for (const z of zips) await doZipUpload(z)
}
// 免壓縮多檔上傳:逐檔序列上傳(避免一次塞爆、進度可讀),收集各檔錯誤,最後整批刷新一次。
async function doUploadMany(files) {
  uploading.value = true; uploadError.value = ''
  const errs = []
  try {
    for (const file of files) {
      progress.value = 0
      try { await uploadDocument(collectionId.value, file, p => { progress.value = p }) }
      catch (e) { errs.push(`${file.name}: ${extractError(e, e.message)}`) }
    }
    if (errs.length) uploadError.value = errs.join('\n')
    await loadDocs()
  } finally { uploading.value = false; progress.value = 0 }
}
async function doZipUpload(file) {
  uploading.value = true; progress.value = 0; uploadError.value = ''; zipResult.value = null
  try {
    const { data } = await uploadZip(collectionId.value, file, { preserveFolderStructure: preserveFolderStructure.value }, p => { progress.value = p })
    zipResult.value = data
    await loadDocs()
  } catch (e) { uploadError.value = extractError(e, e.message) }
  finally { uploading.value = false; progress.value = 0 }
}
// 重新嵌入失敗的文件:呼叫後端 re-enqueue,成功後刷新清單(狀態回 pending → processing)。
async function doReprocess(d) {
  if (reprocessingId.value) return
  reprocessingId.value = d.id
  try { await reprocessDocument(d.id); await loadDocs() }
  catch (e) { uploadError.value = extractError(e, e.message) }
  finally { reprocessingId.value = null }
}

async function doRaiseClassification() {
  if (!collection.value || !raiseTarget.value) return
  raising.value = true
  raiseError.value = ''
  raiseMsg.value = ''
  try {
    const { data } = await raiseCollectionClassification(
      collectionId.value,
      raiseTarget.value,
    )
    collection.value = data
    raiseMsg.value = `已升密至「${data.classification_level}」`
    await loadDocs()
  } catch (e) {
    raiseError.value = extractError(e, e.message)
  } finally {
    raising.value = false
  }
}

async function loadRelations() {
  loadingRels.value = true
  relError.value = ''
  try {
    const { data } = await listRelations(collectionId.value)
    relations.value = data
  } catch (e) {
    relError.value = `載入關聯失敗：${extractError(e, e.message)}`
  } finally { loadingRels.value = false }
}

async function doCreateRelation() {
  if (!newRel.value.src_document_id) return
  creating.value = true; relError.value = ''; relMsg.value = ''
  try {
    const payload = {
      src_document_id: newRel.value.src_document_id,
      relation_type: newRel.value.relation_type,
    }
    if (newRel.value.dst_document_id) payload.dst_document_id = newRel.value.dst_document_id
    else if (newRel.value.target_ref) payload.target_ref = newRel.value.target_ref
    else { relError.value = '請選擇目標文件或輸入目標名稱'; creating.value = false; return }
    if (newRel.value.evidence) payload.evidence = newRel.value.evidence
    await createRelation(collectionId.value, payload)
    newRel.value = { src_document_id: 0, relation_type: 'cites', dst_document_id: 0, target_ref: '', evidence: '' }
    await loadRelations()
  } catch (e) {
    relError.value = extractError(e, e.message)
  } finally { creating.value = false }
}

async function doDeleteRelation(r) {
  deletingId.value = r.id; relError.value = ''
  try {
    await deleteRelation(r.id, collectionId.value)
    await loadRelations()
  } catch (e) {
    relError.value = extractError(e, e.message)
  } finally { deletingId.value = null }
}

async function doReresolve() {
  reresolving.value = true; relError.value = ''; relMsg.value = ''
  try {
    const { data } = await reresolveRelations(collectionId.value)
    relMsg.value = `已對帳 · ${data.resolved} 已解析 · ${data.unresolved} 未解析 · ${data.ambiguous} 模糊 · 已排入重新抽取`
    await loadRelations()
  } catch (e) {
    relError.value = extractError(e, e.message)
  } finally { reresolving.value = false }
}

async function loadVectorDebug(chunkId) {
  if (!selectedDoc.value) return
  vecLoading.value = { ...vecLoading.value, [chunkId]: true }
  vecError.value = { ...vecError.value, [chunkId]: '' }
  try {
    const { data } = await getChunkEmbeddingDebug(selectedDoc.value.id, chunkId)
    vecDebug.value = { ...vecDebug.value, [chunkId]: data }
  } catch (e) {
    // 錯誤只顯示在該 chunk 旁，不進上傳區的 uploadError banner——
    // 向量除錯失敗不代表上傳壞了，放錯位置會讓管理員誤讀成「上傳失敗」。
    vecError.value = { ...vecError.value, [chunkId]: `向量除錯失敗：${extractError(e, '向量除錯失敗')}` }
  } finally {
    vecLoading.value = { ...vecLoading.value, [chunkId]: false }
  }
}

function blobUrl(id) { return documentBlobUrl(id) }
function humanBytes(n) {
  if (n == null || n === '' || Number(n) === 0) return '—'
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
.feedback.is-ok { color: var(--c-ok); border-color: var(--c-ok); background: var(--c-ok-soft); }
.loading { padding: var(--gap-4); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.cls-row {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--gap-3);
}
.cls-current { display: flex; flex-direction: column; gap: 2px; min-width: 120px; }
.cls-current__level {
  font-size: var(--t-xl); font-weight: 600; color: var(--c-fg-1);
  letter-spacing: var(--tracking-tight);
}
.cls-raise { display: flex; align-items: center; gap: var(--gap-2); flex-wrap: wrap; }
.cls-raise__select { min-width: 140px; }

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
.vec__err { font-size: var(--t-xs); color: var(--c-danger); }

/* Relations */
.rel-bar { display: flex; align-items: center; gap: var(--gap-2); flex-wrap: wrap; margin-bottom: var(--gap-3); }
.rel-toggle { display: inline-flex; border: var(--border-w) solid var(--c-border); }
.rel-toggle__btn {
  font: inherit; font-size: var(--t-2xs); color: var(--c-fg-3);
  background: var(--c-bg); border: 0; padding: 3px var(--gap-2); cursor: pointer;
}
.rel-toggle__btn + .rel-toggle__btn { border-left: var(--border-w) solid var(--c-border); }
.rel-toggle__btn.is-on { background: var(--c-accent-soft); color: var(--c-accent); }
.rel-add {
  display: flex; gap: var(--gap-2); flex-wrap: wrap; align-items: center;
  padding-bottom: var(--gap-3); border-bottom: var(--border-w) dashed var(--c-border); margin-bottom: var(--gap-3);
}
.rel-input {
  font-size: var(--t-sm); font-family: var(--font-mono); color: var(--c-fg-1);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border); padding: 4px var(--gap-2);
  min-width: 160px;
}
.rel-input--type { min-width: 110px; }
.rel-input:disabled { opacity: 0.5; }

.rel-table { width: 100%; border-collapse: collapse; font-size: var(--t-sm); }
.rel-table th {
  text-align: left; font-size: var(--t-2xs); text-transform: uppercase; letter-spacing: var(--tracking-caps);
  color: var(--c-fg-3); font-weight: 500; padding: var(--gap-1) var(--gap-2); border-bottom: var(--border-w) solid var(--c-border);
}
.rel-table td { padding: var(--gap-1) var(--gap-2); border-bottom: var(--border-w) dashed var(--c-border); vertical-align: top; }
.rel-table tr.rel--unresolved { background: var(--c-danger-soft); }
.rel-doc { color: var(--c-fg-1); word-break: break-all; max-width: 220px; }
.rel-target { display: inline-flex; align-items: center; gap: 6px; color: var(--c-fg-2); }
.rel-ev {
  color: var(--c-fg-3); font-size: var(--t-2xs); max-width: 260px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
</style>
