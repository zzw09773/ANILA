<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">知識庫</h1>
        <p class="page-head__sub">
          第一級 RAG 儲存 · agent 後端以 <code>RAG_COLLECTION_ID=&lt;id&gt;</code> 掛載
        </p>
      </div>
      <!-- Sprint 8 X / chunking-preview Phase 3 — preview-then-pick
           is the canonical create flow. Users upload a representative
           doc, compare every strategy's chunks side-by-side, then
           pick. The old "instant dropdown" form is reachable via the
           wizard's "skip preview" link for power users who already
           know which strategy they want. -->
      <router-link :to="{ name: 'ChunkingPreview' }" custom v-slot="{ navigate }">
        <TermButton variant="primary" label="+ 新增知識庫" @click="navigate" />
      </router-link>
    </header>

    <TermBox title="篩選" pad="sm">
      <div class="filters">
        <label class="filters__toggle">
          <input type="checkbox" v-model="includeArchived" /> 顯示已封存
        </label>
        <label v-if="isAdmin" class="filters__toggle">
          <input type="checkbox" v-model="showAllCollections" /> 顯示他人的知識庫（管理員）
        </label>
      </div>
    </TermBox>

    <div v-if="error" class="feedback is-err">! {{ error }}</div>
    <div v-else-if="loadingCollections" class="loading">載入知識庫中…</div>

    <div v-if="collections.length === 0 && !loadingCollections && !error" class="term-box" style="padding: var(--gap-6);">
      <TermEmpty message="尚無知識庫 · 點選「新增知識庫」建立" />
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
          <div><dt>文件</dt><dd class="tnum">{{ c.document_count.toLocaleString() }}</dd></div>
          <div><dt>區塊</dt><dd class="tnum">{{ c.chunk_count.toLocaleString() }}</dd></div>
          <div><dt>位元組</dt><dd class="tnum">{{ humanBytes(c.bytes_stored) }}</dd></div>
          <div><dt>策略</dt><dd>{{ c.chunking_config.strategy }}</dd></div>
          <div><dt>嵌入</dt><dd>{{ c.embedding_model }} · {{ c.embedding_dim }}-d</dd></div>
          <div><dt>密等</dt><dd>{{ c.classification_level || '無機密' }}</dd></div>
          <div><dt>擁有者</dt><dd>user #{{ c.created_by }}</dd></div>
        </dl>

        <div class="cc__dsn">
          <span class="cell-meta">agent 後端環境變數</span>
          <code>RAG_COLLECTION_ID={{ c.id }}</code>
        </div>

        <footer class="cc__foot">
          <router-link :to="{ name: 'CollectionDetail', params: { id: c.id } }" class="term-action">→ 檢視器</router-link>
          <span class="cc__sep">·</span>
          <router-link :to="{ name: 'Evaluator', params: { id: c.id } }" class="term-action">→ 評測器</router-link>
          <span class="cc__sep">·</span>
          <button v-if="c.status === 'active'" class="term-action" @click="archiveCollection(c)">封存</button>
          <button v-else class="term-action" @click="restoreCollection(c)">還原</button>
          <span class="cc__sep">·</span>
          <button class="term-action term-action--danger" @click="confirmDelete(c)">刪除</button>
        </footer>
      </article>
    </div>

    <TermModal :visible="creating" title="新增 · 知識庫" width="520px" @close="creating = false">
      <div class="form-grid">
        <TermField label="名稱">
          <input v-model.trim="form.name" class="term-input" maxlength="200" placeholder="legal-regs" />
        </TermField>
        <TermField label="描述" optional>
          <textarea v-model.trim="form.description" rows="2" class="term-textarea" maxlength="2000" />
        </TermField>
        <TermField
          label="密等"
          hint="建立時選定；只能往上調，不能自行降級。預設無機密。"
        >
          <select v-model="form.classification_level" class="term-select">
            <option v-for="lvl in CLASSIFICATION_LEVELS" :key="lvl" :value="lvl">{{ lvl }}</option>
          </select>
        </TermField>
        <TermField label="切塊策略">
          <select v-model="form.strategy" class="term-select">
            <option value="hierarchical">hierarchical · 標題樹 + 上階脈絡</option>
            <option value="markdown-aware">markdown-aware · 標題 + code-fence 安全</option>
            <option value="fixed">fixed · token 預算切窗</option>
            <option value="pdf-page">pdf-page · 僅 PDF · 依頁邊界</option>
            <option value="cjk-sentence">cjk-sentence · CJK 語法 + token 合併</option>
            <option value="semantic">semantic · 嵌入距離 · 慢但精準</option>
          </select>
        </TermField>
        <TermField :label="tokenLabel" :hint="tokenHint">
          <input v-model.number="form.maxTokens" type="number" class="term-input" min="64" max="8192" />
        </TermField>
        <div v-if="formError" class="feedback is-err">! {{ formError }}</div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="creating = false" label="取消" />
        <TermButton variant="primary" :loading="submitting" :disabled="submitting" :label="submitting ? '建立中' : '建立'" @click="submitCreate" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { listCollections, createCollection, updateCollection, deleteCollection } from '../api/ingestionCollections'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal } from '../components/cli'
import { useDialog } from '../composables/useDialog'

const { confirm } = useDialog()
const authStore = useAuthStore()
const route = useRoute()
const isAdmin = computed(() => authStore.isAdmin)

const includeArchived = ref(false)
const showAllCollections = ref(false)
const collections = ref([])
const loadingCollections = ref(false)
const error = ref('')

const creating = ref(false)
const submitting = ref(false)
const formError = ref('')
// maxTokens default 256 matches the post-Sprint-9-X HierarchicalChunker
// leaf budget. Power users can crank it for legacy section-sized
// chunking, but small leaves give vector recall the headroom the
// parent-child design assumes.
const CLASSIFICATION_LEVELS = ['無機密', '營業秘密', '密', '機密']
const form = ref({
  name: '', description: '', strategy: 'hierarchical', maxTokens: 256,
  classification_level: '無機密',
})

const tokenLabel = computed(() => ({
  fixed: '大小（tokens）',
  'pdf-page': '每頁最大 tokens',
  'cjk-sentence': '目標 tokens',
  semantic: '最小片段 tokens',
})[form.value.strategy] || '葉節點最大 tokens')

const tokenHint = computed(() => ({
  fixed: '每區塊 token 預算 · overlap 自動 = size/8',
  'pdf-page': '超大頁面內部改用 fixed 策略切分',
  'cjk-sentence': '合併句子直到達到目標',
  semantic: '片段上限 · 以嵌入距離決定邊界',
})[form.value.strategy] || '每個標題樹葉節點的 token 上限')

onMounted(() => {
  loadCollections()
  // Sprint 8 X / chunking-preview Phase 3 — escape hatch from the
  // wizard. Wizard step 1 has a "skip preview · quick create" link
  // pointing at /knowledge-collections?quick=1; landing here with
  // that query param auto-opens the legacy create modal so power
  // users get to the strategy dropdown in 2 clicks total.
  if (route.query.quick) {
    openCreateModal()
  }
})
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
    error.value = `載入知識庫失敗：${e.response?.data?.detail || e.message}`
  } finally { loadingCollections.value = false }
}

function openCreateModal() {
  formError.value = ''
  // 256 matches HierarchicalChunker's post-Sprint-9-X default leaf budget.
  form.value = {
    name: '', description: '', strategy: 'hierarchical', maxTokens: 256,
    classification_level: '無機密',
  }
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
  // hierarchical (Sprint 9 X / parent-child RAG):
  //   * max_leaf_tokens — embedded-leaf budget
  //   * max_parent_tokens — heading-section context (4× leaf)
  //   * overlap_tokens — fallback overlap on oversized paragraphs
  else params = {
    max_leaf_tokens: form.value.maxTokens,
    max_parent_tokens: form.value.maxTokens * 4,
    overlap_tokens: Math.max(16, Math.floor(form.value.maxTokens / 16)),
  }
  try {
    await createCollection({
      name: form.value.name,
      description: form.value.description || null,
      chunking_config: { strategy: s, params },
      classification_level: form.value.classification_level || '無機密',
    })
    creating.value = false
    await loadCollections()
  } catch (e) {
    formError.value = e.response?.data?.detail || e.message
  } finally { submitting.value = false }
}

async function archiveCollection(c) {
  try { await updateCollection(c.id, { status: 'archived' }); await loadCollections() }
  catch (e) { error.value = `封存失敗：${e.response?.data?.detail || e.message}` }
}
async function restoreCollection(c) {
  try { await updateCollection(c.id, { status: 'active' }); await loadCollections() }
  catch (e) { error.value = `還原失敗：${e.response?.data?.detail || e.message}` }
}
async function confirmDelete(c) {
  if (!(await confirm({ message: `刪除「${c.name}」？CASCADE 會移除 ${c.document_count} 份文件與 ${c.chunk_count} 個區塊。`, danger: true }))) return
  try { await deleteCollection(c.id); await loadCollections() }
  catch (e) { error.value = `刪除失敗：${e.response?.data?.detail || e.message}` }
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
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__sub code { color: var(--c-accent); background: var(--c-accent-soft); padding: 0 4px; }

.cta-group { display: flex; align-items: center; gap: var(--gap-3); }
.term-link { color: var(--c-accent); font-size: var(--t-2xs); text-decoration: none; font-family: var(--font-mono); }
.term-link:hover { text-decoration: underline; }

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
