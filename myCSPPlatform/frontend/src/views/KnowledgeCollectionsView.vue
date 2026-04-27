<template>
  <div class="kc-root">
    <header class="page-header">
      <h1>📚 Knowledge Collections</h1>
      <p class="subtitle">每個 agent 的 ingestion collections — 上傳 / 切 chunk / 看 vector 索引</p>
    </header>

    <!-- Agent picker + actions row -->
    <section class="picker-row">
      <label class="field">
        <span>Agent</span>
        <select v-model.number="selectedAgentId" :disabled="loadingAgents">
          <option :value="null" disabled>{{ loadingAgents ? '載入 agents...' : '請選擇 agent' }}</option>
          <option v-for="a in agents" :key="a.id" :value="a.id">
            {{ a.name }} (#{{ a.id }})
          </option>
        </select>
      </label>

      <label class="field checkbox">
        <input type="checkbox" v-model="includeArchived" />
        <span>顯示 archived</span>
      </label>

      <button
        class="primary"
        :disabled="!selectedAgentId"
        @click="openCreateModal"
      >
        ＋ 新建 collection
      </button>
    </section>

    <!-- State banners -->
    <div v-if="error" class="banner error">{{ error }}</div>
    <div v-else-if="loadingCollections" class="banner muted">載入中…</div>
    <div v-else-if="!selectedAgentId" class="banner muted">先選一個 agent。</div>
    <div v-else-if="collections.length === 0" class="banner muted">
      此 agent 還沒有 collection — 點右上「＋ 新建 collection」建立第一個。
    </div>

    <!-- Collection cards -->
    <section v-if="selectedAgentId && collections.length > 0" class="cards">
      <article v-for="c in collections" :key="c.id" class="card">
        <header class="card-head">
          <h2>📁 {{ c.name }}</h2>
          <span class="badge" :class="c.status">{{ c.status }}</span>
        </header>
        <p v-if="c.description" class="desc">{{ c.description }}</p>
        <dl class="stats">
          <div><dt>Documents</dt><dd>{{ c.document_count.toLocaleString() }}</dd></div>
          <div><dt>Chunks</dt><dd>{{ c.chunk_count.toLocaleString() }}</dd></div>
          <div><dt>Bytes</dt><dd>{{ humanBytes(c.bytes_stored) }}</dd></div>
          <div><dt>Strategy</dt><dd>{{ c.chunking_config.strategy }}</dd></div>
          <div><dt>Embedding</dt><dd>{{ c.embedding_model }} · {{ c.embedding_dim }}-d</dd></div>
        </dl>
        <footer class="card-actions">
          <router-link :to="{ name: 'CollectionDetail', params: { id: c.id } }" class="link">
            🔍 Inspector
          </router-link>
          <router-link :to="{ name: 'Evaluator', params: { id: c.id } }" class="link">
            🧪 Evaluator
          </router-link>
          <button
            v-if="c.status === 'active'"
            class="ghost"
            @click="archiveCollection(c)"
            title="標記為 archived（不再列出）"
          >
            Archive
          </button>
          <button
            v-else
            class="ghost"
            @click="restoreCollection(c)"
          >
            Restore
          </button>
          <button class="danger" @click="confirmDelete(c)">刪除</button>
        </footer>
      </article>
    </section>

    <!-- Create modal -->
    <div v-if="creating" class="modal-overlay" @click.self="creating = false">
      <div class="modal">
        <h2>新建 collection</h2>
        <form @submit.prevent="submitCreate">
          <label class="field">
            <span>名稱 *</span>
            <input v-model.trim="form.name" required maxlength="200" placeholder="legal-regs" />
          </label>
          <label class="field">
            <span>描述</span>
            <textarea v-model.trim="form.description" rows="2" maxlength="2000" />
          </label>
          <label class="field">
            <span>Chunking strategy</span>
            <select v-model="form.strategy">
              <option value="hierarchical">hierarchical（heading 樹 + ancestor context）</option>
              <option value="markdown-aware">markdown-aware（heading + code-fence safe）</option>
              <option value="fixed">fixed（token-budget windowing）</option>
              <option value="pdf-page">pdf-page（PDF page 邊界，PDF only）</option>
              <option value="cjk-sentence">cjk-sentence（中文句法 + token merge）</option>
              <option value="semantic">semantic（embedding distance — 慢但精準）</option>
            </select>
          </label>
          <label class="field">
            <span>{{ tokenLabel }}</span>
            <input type="number" v-model.number="form.maxTokens" min="64" max="8192" />
            <small class="muted">{{ tokenHint }}</small>
          </label>
          <div class="modal-actions">
            <button type="button" class="ghost" @click="creating = false">取消</button>
            <button type="submit" class="primary" :disabled="submitting">
              {{ submitting ? '建立中…' : '建立' }}
            </button>
          </div>
          <div v-if="formError" class="banner error inline">{{ formError }}</div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { listMyAgents } from '../api/agents'
import {
  listCollections,
  createCollection,
  updateCollection,
  deleteCollection,
} from '../api/ingestionCollections'

const agents = ref([])
const loadingAgents = ref(false)
// Persist last-picked agent to localStorage so dev doesn't re-select
// every visit. The architectural redo (collection-as-first-class
// resource, no agent picker) is Sprint 4 territory; this is the
// pragmatic UX patch in the meantime.
const _LAST_AGENT_KEY = 'anila.kc.lastAgentId'
const selectedAgentId = ref(
  Number(localStorage.getItem(_LAST_AGENT_KEY)) || null,
)
watch(selectedAgentId, (id) => {
  if (id) localStorage.setItem(_LAST_AGENT_KEY, String(id))
})
const includeArchived = ref(false)

const collections = ref([])
const loadingCollections = ref(false)
const error = ref('')

const creating = ref(false)
const submitting = ref(false)
const formError = ref('')
const form = ref({ name: '', description: '', strategy: 'hierarchical', maxTokens: 1024 })

// ── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(async () => {
  loadingAgents.value = true
  try {
    const { data } = await listMyAgents()
    agents.value = Array.isArray(data) ? data : data.items || []
    // Auto-select preference order:
    //   1. localStorage last-picked, if it's still in the user's list.
    //   2. Single-agent shortcut (if user only has one).
    //   3. Otherwise, leave null and prompt the user.
    const lastId = selectedAgentId.value
    const matchLast = agents.value.find((a) => a.id === lastId)
    if (matchLast) {
      selectedAgentId.value = matchLast.id
    } else if (agents.value.length === 1) {
      selectedAgentId.value = agents.value[0].id
    } else {
      selectedAgentId.value = null
    }
  } catch (e) {
    error.value = `載入 agents 失敗：${e.response?.data?.detail || e.message}`
  } finally {
    loadingAgents.value = false
  }
})

watch([selectedAgentId, includeArchived], () => {
  if (selectedAgentId.value) loadCollections()
})

// ── Data ────────────────────────────────────────────────────────────────────

async function loadCollections() {
  if (!selectedAgentId.value) return
  loadingCollections.value = true
  error.value = ''
  try {
    const { data } = await listCollections({
      agent_id: selectedAgentId.value,
      include_archived: includeArchived.value,
    })
    collections.value = data
  } catch (e) {
    error.value = `載入 collections 失敗：${e.response?.data?.detail || e.message}`
  } finally {
    loadingCollections.value = false
  }
}

// ── Create ──────────────────────────────────────────────────────────────────

function openCreateModal() {
  formError.value = ''
  form.value = { name: '', description: '', strategy: 'hierarchical', maxTokens: 1024 }
  creating.value = true
}

async function submitCreate() {
  formError.value = ''
  submitting.value = true
  const params =
    form.value.strategy === 'fixed'
      ? { size: form.value.maxTokens, overlap: Math.floor(form.value.maxTokens / 8) }
      : { max_leaf_tokens: form.value.maxTokens }
  try {
    await createCollection({
      agent_id: selectedAgentId.value,
      name: form.value.name,
      description: form.value.description || null,
      chunking_config: { strategy: form.value.strategy, params },
    })
    creating.value = false
    await loadCollections()
  } catch (e) {
    formError.value = e.response?.data?.detail || e.message
  } finally {
    submitting.value = false
  }
}

// ── Archive / Restore / Delete ──────────────────────────────────────────────

async function archiveCollection(c) {
  try {
    await updateCollection(c.id, { status: 'archived' })
    await loadCollections()
  } catch (e) {
    error.value = `Archive 失敗：${e.response?.data?.detail || e.message}`
  }
}

async function restoreCollection(c) {
  try {
    await updateCollection(c.id, { status: 'active' })
    await loadCollections()
  } catch (e) {
    error.value = `Restore 失敗：${e.response?.data?.detail || e.message}`
  }
}

async function confirmDelete(c) {
  // Double confirmation because DELETE CASCADE drops every chunk + doc.
  if (!confirm(
    `刪除 collection「${c.name}」會 CASCADE 刪除其下 ${c.document_count} 個 documents 與 ${c.chunk_count} 個 chunks。\n\n確定刪除？`,
  )) return
  try {
    await deleteCollection(c.id)
    await loadCollections()
  } catch (e) {
    error.value = `刪除失敗：${e.response?.data?.detail || e.message}`
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function humanBytes(n) {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = Number(n)
  let u = 0
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024
    u += 1
  }
  return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`
}
</script>

<style scoped>
.kc-root { padding: 1.25rem; max-width: 1200px; }

.page-header h1 { margin: 0 0 0.25rem; font-size: 1.5rem; }
.subtitle { margin: 0 0 1rem; color: #6b7280; font-size: 0.9rem; }

.picker-row {
  display: flex; gap: 1.25rem; align-items: flex-end;
  margin-bottom: 1rem;
  padding: 0.75rem 1rem; background: #f9fafb; border-radius: 6px;
  flex-wrap: wrap;
}
.field { display: flex; flex-direction: column; gap: 0.25rem; }
.field > span { font-size: 0.85rem; color: #4b5563; }
.field.checkbox {
  /* Same baseline as the other fields so the checkbox row aligns
     with the bottom of the Agent <select> rather than the bottom of
     its tiny "Agent" label. */
  flex-direction: row; align-items: center; gap: 0.5rem;
  padding-bottom: 0.5rem;  /* matches the select's vertical padding */
  white-space: nowrap;
}
.field.checkbox > span { font-size: 0.9rem; }
.field.checkbox input[type="checkbox"] {
  width: 1rem; height: 1rem; min-width: 0; padding: 0; margin: 0;
}
.field select, .field input:not([type="checkbox"]), .field textarea {
  padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 4px;
  min-width: 220px; font-size: 0.95rem;
}

button { padding: 0.5rem 0.9rem; border: 1px solid transparent; border-radius: 4px; cursor: pointer; font-size: 0.9rem; }
button.primary { background: #2563eb; color: #fff; }
button.primary:hover:not(:disabled) { background: #1d4ed8; }
button.primary:disabled { opacity: 0.5; cursor: not-allowed; }
button.ghost { background: #f3f4f6; color: #374151; border-color: #d1d5db; }
button.ghost:hover { background: #e5e7eb; }
button.danger { background: #fff; color: #b91c1c; border-color: #fca5a5; }
button.danger:hover { background: #fef2f2; }

.banner { padding: 0.75rem 1rem; border-radius: 4px; margin-bottom: 1rem; font-size: 0.9rem; }
.banner.muted { background: #f9fafb; color: #6b7280; }
.banner.error { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.banner.inline { margin-top: 0.75rem; }

.cards { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 6px; padding: 1rem; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-bottom: 0.5rem; }
.card-head h2 { margin: 0; font-size: 1.05rem; }
.desc { font-size: 0.85rem; color: #6b7280; margin: 0 0 0.75rem; line-height: 1.4; }
.badge { font-size: 0.7rem; padding: 0.2rem 0.5rem; border-radius: 999px; text-transform: uppercase; }
.badge.active { background: #d1fae5; color: #065f46; }
.badge.archived { background: #e5e7eb; color: #374151; }
.stats { display: grid; gap: 0.4rem 1rem; grid-template-columns: 1fr 1fr; margin: 0.5rem 0 0.75rem; }
.stats > div { display: flex; flex-direction: column; }
.stats dt { font-size: 0.7rem; color: #9ca3af; text-transform: uppercase; }
.stats dd { margin: 0; font-size: 0.9rem; color: #111827; }
.card-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.link { color: #2563eb; text-decoration: none; padding: 0.5rem 0.9rem; border-radius: 4px; border: 1px solid #bfdbfe; background: #eff6ff; font-size: 0.9rem; }
.link:hover { background: #dbeafe; }

.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: #fff; padding: 1.5rem; border-radius: 6px; min-width: 420px; max-width: 90vw; }
.modal h2 { margin: 0 0 1rem; font-size: 1.1rem; }
.modal form { display: flex; flex-direction: column; gap: 0.85rem; }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 0.5rem; }
</style>
