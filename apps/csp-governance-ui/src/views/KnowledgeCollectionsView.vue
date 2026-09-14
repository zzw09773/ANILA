<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">知識庫</h1>
        <p class="page-head__sub">
          文件、區塊與檢索設定。
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
            <TermBadge :variant="c.status === 'active' ? 'ok' : ''">{{ c.status === 'active' ? '使用中' : (c.status === 'archived' ? '已封存' : c.status) }}</TermBadge>
            <TermBadge v-if="c.anila_searchable" variant="ok">ANILA 可檢索</TermBadge>
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
          <button v-if="c.status === 'active'" class="term-action" @click="archiveCollection(c)">封存</button>
          <button v-else class="term-action" @click="restoreCollection(c)">還原</button>
          <span class="cc__sep">·</span>
          <!-- 標記不可用時停用但不藏起來：藏掉的控制項讓管理員以為這個功能不存在，
               而他其實只差一個降密流程／一句「請管理員代標」。原因用文字寫出來，
               不是只掛 :title —— 滑鼠不停在上面的人永遠看不到 tooltip。 -->
          <button
            class="term-action"
            :disabled="!markStates[c.id].allowed || markingId === c.id"
            :title="markStates[c.id].reason"
            @click="toggleAnilaSearchable(c)"
          >{{ c.anila_searchable ? '取消 ANILA 檢索標記' : '標記為 ANILA 可檢索' }}</button>
          <span class="cc__sep">·</span>
          <button class="term-action term-action--danger" @click="confirmDelete(c)">刪除</button>
          <p v-if="markStates[c.id].reason" class="cc__foot-note cell-meta">{{ markStates[c.id].reason }}</p>
          <p v-if="markErrors[c.id]" class="cc__foot-note feedback is-err">{{ markErrors[c.id] }}</p>
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
        <TermField
          label="圖說"
          hint="上線後純文字庫與簡報／掃描檔的需求不同；模型欄是抽換偵測——哪天換掉 gemma，要立刻看得出這個庫當初選了哪顆。"
        >
          <select v-model="form.captionMode" class="term-select">
            <option value="platform">跟隨平台（現況）</option>
            <option value="on">要做圖說</option>
            <option value="off">不要圖說</option>
          </select>
        </TermField>
        <TermField
          v-if="form.captionMode === 'on'"
          label="圖說模型"
          hint="清單是全部已登錄模型，沒有視覺能力過濾。留空＝跟隨平台 VISION_MODEL。"
        >
          <select v-model="form.caption_model" class="term-select">
            <option value="">跟隨平台 VISION_MODEL</option>
            <option v-for="m in modelOptions" :key="m.name" :value="m.name">
              {{ m.display_name || m.name }}（{{ m.model_type }}）
            </option>
          </select>
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
import { listModels } from '../api/models'
import { extractError } from '../api/errors'
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
// ANILA 檢索標記(institutional-kb Task 7)。DB CHECK
// ``ck_ingestion_collections_anila_searchable_unclassified`` 只允許無機密的庫
// 帶著這個旗標,所以密等一離開無機密,標記在資料庫層就已經不可能了。
const ANILA_MARK_UNCLASSIFIED = '無機密'
const markErrors = ref({})
const markingId = ref(null)
const form = ref({
  name: '', description: '', strategy: 'hierarchical', maxTokens: 256,
  classification_level: '無機密',
  captionMode: 'platform',
  caption_model: '',
})
const modelOptions = ref([])

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
  loadModels()
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
    error.value = `載入知識庫失敗：${extractError(e, e.message)}`
  } finally { loadingCollections.value = false }
}

async function loadModels() {
  try {
    const { data } = await listModels()
    modelOptions.value = Array.isArray(data) ? data : []
  } catch {
    modelOptions.value = []
  }
}

function openCreateModal() {
  formError.value = ''
  // 256 matches HierarchicalChunker's post-Sprint-9-X default leaf budget.
  form.value = {
    name: '', description: '', strategy: 'hierarchical', maxTokens: 256,
    classification_level: '無機密',
    captionMode: 'platform',
    caption_model: '',
  }
  creating.value = true
  if (!modelOptions.value.length) loadModels()
}

async function submitCreate() {
  formError.value = ''
  // DK-1：空白名稱只該得到「請輸入名稱」，不是後端原始 pydantic JSON。
  // 部門對話框（DepartmentsView）早就用 disabled 按鈕處理同一件事，
  // 這裡在建庫這一格補上同款前置——欄位錯不該打到後端。
  if (!form.value.name) {
    formError.value = '請輸入名稱'
    return
  }
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
    const caption_enabled =
      form.value.captionMode === 'on' ? true
        : form.value.captionMode === 'off' ? false
          : null
    await createCollection({
      name: form.value.name,
      description: form.value.description || null,
      chunking_config: { strategy: s, params },
      classification_level: form.value.classification_level || '無機密',
      caption_enabled,
      caption_model: caption_enabled === true
        ? (form.value.caption_model || null)
        : null,
    })
    creating.value = false
    await loadCollections()
  } catch (e) {
    // DK-1 同族清掃：建庫失敗不放裸 detail（array/object 會變 [object Object] 或原始 JSON）。
    formError.value = extractError(e, '建立失敗')
  } finally { submitting.value = false }
}

async function archiveCollection(c) {
  try { await updateCollection(c.id, { status: 'archived' }); await loadCollections() }
  catch (e) { error.value = `封存失敗：${extractError(e, e.message)}` }
}
async function restoreCollection(c) {
  try { await updateCollection(c.id, { status: 'active' }); await loadCollections() }
  catch (e) { error.value = `還原失敗：${extractError(e, e.message)}` }
}
const markStates = computed(() => {
  const map = {}
  for (const c of collections.value) map[c.id] = anilaMarkState(c, isAdmin.value)
  return map
})

/**
 * 這個庫的 ANILA 檢索標記能不能按,不能按的話原因是什麼。
 *
 * 順序照後端 `_guard_anila_searchable`(collections.py:143)與 PATCH 的 admin 閘
 * (collections.py:676):
 *   1. admin 閘在最前面,**兩個方向**都擋(標記的影響範圍是全院的聊天檢索)。
 *   2. 密等只擋「開啟」;關閉永遠放行 —— 後端每一則拒絕訊息都叫人「先取消標記」,
 *      把關閉也擋起來等於指了一條走不通的路。
 * 只做這兩道:嵌入空間一致與庫內文件密等要跨列查,前端手上的清單是篩過的
 * (只有 origin=csp、可能只有自己的),自己算會跟後端漂開。那兩道交給後端拒絕,
 * 訊息原樣呈現。
 *
 * 純函式、無 Vue 相依:tests/anilaSearchableToggle.test.mjs 會把這段原始碼抽出來
 * 直接評估,所以裡面不用樣板字串與正規表示式字面量(抽取器不處理那兩種狀態)。
 */
function anilaMarkState(c, isAdminUser) {
  const marked = Boolean(c && c.anila_searchable)
  if (!isAdminUser) {
    return {
      marked,
      allowed: false,
      reason: '只有管理員可以設定 ANILA 檢索標記——這等同於把整個庫開放給全院的聊天檢索。請把這個庫的網址交給管理員代為標記。',
    }
  }
  if (marked) return { marked, allowed: true, reason: '' }
  const level = (c && c.classification_level) || ANILA_MARK_UNCLASSIFIED
  if (level !== ANILA_MARK_UNCLASSIFIED) {
    return {
      marked,
      allowed: false,
      reason: '此庫密等是「' + level + '」，只有「' + ANILA_MARK_UNCLASSIFIED
        + '」的庫可以標記為 ANILA 可檢索（資料庫層也擋著）。若這批資料實際上不需要密等，'
        + '請先走降密申請流程降到「' + ANILA_MARK_UNCLASSIFIED + '」，再回來標記。',
    }
  }
  return { marked, allowed: true, reason: '' }
}

async function toggleAnilaSearchable(c) {
  if (!anilaMarkState(c, isAdmin.value).allowed) return
  markingId.value = c.id
  markErrors.value[c.id] = ''
  try {
    // 不做樂觀更新:畫面上那個值等下一輪 LIST 回來的,不是這裡先寫上去的。
    // 「顯示值≠生效值」是本專案盤點過的靜默成功形狀。
    await updateCollection(c.id, { anila_searchable: !c.anila_searchable })
    await loadCollections()
  } catch (e) {
    // 後端的拒絕訊息裡寫著一條走得通的路(降密流程／另建一個庫／請管理員代標),
    // 原樣呈現,不要改寫成「操作失敗」。
    markErrors.value[c.id] = '標記失敗：' + extractError(e, e.message)
  } finally {
    markingId.value = null
  }
}

async function confirmDelete(c) {
  if (!(await confirm({ message: `刪除「${c.name}」？CASCADE 會移除 ${c.document_count} 份文件與 ${c.chunk_count} 個區塊。`, danger: true }))) return
  try { await deleteCollection(c.id); await loadCollections() }
  catch (e) { error.value = `刪除失敗：${extractError(e, e.message)}` }
}

function humanBytes(n) {
  if (n == null || n === '' || Number(n) === 0) return '—'
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
/* 停用原因／後端拒絕訊息各自佔滿一行（cc__foot 是 wrap 的 flex）。 */
.cc__foot-note { flex-basis: 100%; margin: 2px 0 0; line-height: 1.5; }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
</style>
