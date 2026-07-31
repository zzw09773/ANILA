<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">API 金鑰</h1>
        <p class="page-head__sub">
          資料層 <code class="page-head__code">/v1/*</code> 使用的 OpenAI 相容 bearer 金鑰
        </p>
      </div>
      <TermButton variant="primary" @click="showCreateModal = true" label="建立金鑰" />
    </header>

    <TermBox title="金鑰 · 全部" :hint="`${keysStore.keys.length} 筆`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 18%">名稱</th>
            <th style="width: 22%">金鑰</th>
            <th>允許的模型</th>
            <th style="width: 14%">建立時間</th>
            <th style="width: 14%">最後使用</th>
            <th style="width: 8%">狀態</th>
            <th style="width: 14%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="key in keysStore.keys" :key="key.id">
            <td>
              <div class="cell-strong">{{ key.name }}</div>
              <div class="cell-meta">id #{{ key.id }}</div>
            </td>
            <td>
              <code class="cell-code">{{ key.key_prefix }}…{{ key.key_suffix }}</code>
            </td>
            <td>
              <div v-if="key.allowed_model_names.length" class="chip-row">
                <TermBadge v-for="name in key.allowed_model_names" :key="name" variant="info">{{ name }}</TermBadge>
              </div>
              <span v-else class="cell-meta">—</span>
            </td>
            <td class="cell-meta tnum">{{ formatDate(key.created_at) }}</td>
            <td class="cell-meta tnum">{{ key.last_used_at ? formatDate(key.last_used_at) : '從未' }}</td>
            <td>
              <TermBadge :variant="key.is_active ? 'ok' : 'danger'" dot>
                {{ key.is_active ? '使用中' : '已撤銷' }}
              </TermBadge>
            </td>
            <td>
              <div class="row-actions">
                <button v-if="key.is_active" class="term-action" @click="confirmRegenerate(key)">重新產生</button>
                <span v-if="key.is_active" class="row-actions__sep">·</span>
                <button v-if="key.is_active" class="term-action term-action--danger" @click="confirmRevoke(key)">撤銷</button>
              </div>
            </td>
          </tr>
          <tr v-if="keysStore.keys.length === 0">
            <td colspan="7"><TermEmpty message="尚無 API 金鑰 · 點選「建立金鑰」新增" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Create modal --------------------------------------------------- -->
    <TermModal :visible="showCreateModal" title="建立 · 新 API 金鑰" width="520px" @close="showCreateModal = false">
      <div class="form-grid">
        <TermField label="名稱" hint="供你識別 · 不會出現在 /v1/*">
          <input v-model="newKey.name" class="term-input" placeholder="例：dev-laptop · ci-runner-1" />
        </TermField>
        <TermField label="到期時間" hint="留空表示永不過期">
          <input v-model="newKey.expires_at" type="datetime-local" class="term-input" />
        </TermField>

        <TermField v-if="authStore.isAdmin" label="允許的模型" hint="伺服器強制 · 金鑰不得超出使用者允許清單">
          <div class="check-list term-box term-box--inset" style="padding: 8px 12px; max-height: 200px; overflow:auto;">
            <label v-for="model in allModels" :key="model.id" class="check-list__row">
              <input type="checkbox" :value="model.id" v-model="newKey.model_ids" />
              <span>{{ model.display_name }}</span>
              <TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge>
            </label>
            <!-- 讀不到 ≠ 沒有。文案由 allowListNotice() 依狀態決定。 -->
            <p
              v-if="allModelsNotice"
              class="cell-meta"
              :class="{ 'cell-meta--danger': allModelsStatus === ALLOW_LIST_UNREAD }"
            >{{ allModelsNotice }}</p>
          </div>
        </TermField>

        <TermField v-else label="你的允許清單" hint="由管理員設定 · 無法從此對話方塊放寬">
          <div class="term-box term-box--inset" style="padding: 8px 12px;">
            <div v-if="myAllowedModels.length" class="chip-row">
              <TermBadge v-for="m in myAllowedModels" :key="m.id" variant="info">{{ m.display_name }}</TermBadge>
            </div>
            <!-- 讀不到 ≠ 沒有被指派 —— 兩者以前共用這一行字。 -->
            <p
              v-else
              class="cell-meta"
              :class="{ 'cell-meta--danger': myAllowedStatus === ALLOW_LIST_UNREAD }"
            >{{ myAllowedNotice }}</p>
          </div>
        </TermField>
      </div>

      <template #footer>
        <TermButton variant="ghost" @click="showCreateModal = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="!canCreate"
          :loading="creating"
          :label="creating ? '建立中' : '建立'"
          :title="!canCreate ? createDisabledReason : ''"
          @click="handleCreate"
        />
      </template>
    </TermModal>

    <!-- Reveal modal --------------------------------------------------- -->
    <TermModal :visible="showKeyModal" :title="keyModalTitle" width="600px" :dismissible="hasCopied" @close="closeKeyModal">
      <p class="reveal__warn">! 請立即複製此金鑰 — 無法再次取得。</p>
      <pre class="reveal__key">{{ createdFullKey }}</pre>
      <p class="reveal__hint">
        請存入你的密碼管理器 · 以 <code>Authorization: Bearer …</code> 使用
      </p>
      <template #footer>
        <TermButton variant="default" @click="copyKey" :label="copied ? '已複製 ✓' : '複製'" />
        <TermButton variant="primary" :disabled="!hasCopied" :title="!hasCopied ? '請先複製' : ''" @click="closeKeyModal" label="完成" />
      </template>
    </TermModal>

    <!-- Confirm dialogs ------------------------------------------------- -->
    <TermConfirm
      :visible="showRevokeConfirm"
      title="撤銷 · API 金鑰"
      :message="`撤銷「${revokeTarget?.name}」？使用此金鑰的進行中請求將立即被拒絕。`"
      confirm-text="撤銷"
      :danger="true"
      @confirm="handleRevoke"
      @cancel="showRevokeConfirm = false"
    />
    <TermConfirm
      :visible="showRegenerateConfirm"
      title="重新產生 · API 金鑰"
      :message="`重新產生「${regenerateTarget?.name}」？現有金鑰立即失效，並以相同名稱與允許清單核發新的 sk-…。`"
      confirm-text="重新產生"
      @confirm="handleRegenerate"
      @cancel="showRegenerateConfirm = false"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useApiKeysStore } from '../stores/apiKeys'
import { useAuthStore } from '../stores/auth'
import { listModels } from '../api/models'
import { getMyAllowedModels } from '../api/users'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermConfirm } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import {
  ALLOW_LIST_UNREAD,
  allowListNotice,
  allowListStatus,
  allowListUsable,
  loadAllowList,
} from '../utils/allowList'

const { toast } = useDialog()
const keysStore = useApiKeysStore()
const authStore = useAuthStore()

// ⚠ 這兩份清單以前是 `try { ... } catch {}`,讀取失敗時停在 [],畫面跟
// 「本來就沒有模型」一模一樣 —— 使用者被告知去找管理員,管理員看自己的
// 畫面一切正常(和 UsersView 那個最嚴重的缺陷同一個形狀)。現在讀取結果
// 連同 loadFailed 一起保留,失敗有自己的狀態與自己的文案。
const allModelsResult = ref({ items: [], loadFailed: false, error: null })
const myAllowedResult = ref({ items: [], loadFailed: false, error: null })

const allModels = computed(() => allModelsResult.value.items)
const myAllowedModels = computed(() => myAllowedResult.value.items)

const allModelsStatus = computed(() => allowListStatus(allModelsResult.value))
const myAllowedStatus = computed(() => allowListStatus(myAllowedResult.value))

const allModelsNotice = computed(() =>
  allowListNotice(allModelsStatus.value, {
    empty: '尚未註冊任何模型',
    unread: '讀不到模型清單 · 這不代表沒有模型 · 請重新整理或聯絡管理員',
  })
)
const myAllowedNotice = computed(() =>
  allowListNotice(myAllowedStatus.value, {
    empty: '尚未指派模型 · 請聯絡管理員',
    unread: '讀不到你的允許清單 · 這不代表你沒有模型 · 請重新整理後再試',
  })
)

const showCreateModal = ref(false)
const showKeyModal = ref(false)
const showRevokeConfirm = ref(false)
const showRegenerateConfirm = ref(false)
const revokeTarget = ref(null)
const regenerateTarget = ref(null)
const creating = ref(false)
const createdFullKey = ref('')
const keyModalTitle = ref('API 金鑰 · 已建立')
const copied = ref(false)
const hasCopied = ref(false)

const newKey = ref({ name: '', model_ids: [], expires_at: '' })

const canCreate = computed(() => {
  if (creating.value) return false
  if (!(newKey.value.name || '').trim()) return false
  if (authStore.isAdmin) {
    if ((newKey.value.model_ids || []).length === 0) return false
  } else {
    // 讀不到允許清單時也不能建立 —— 非 admin 的 payload 直接拿
    // myAllowedModels 當 model_ids(見 handleCreate),用一份「不知道對不對」
    // 的清單建金鑰,等於憑空決定這把金鑰能打哪些模型。
    if (!allowListUsable(myAllowedStatus.value)) return false
  }
  return true
})

const createDisabledReason = computed(() => {
  if (!(newKey.value.name || '').trim()) return '名稱不得空白'
  if (authStore.isAdmin && (newKey.value.model_ids || []).length === 0) {
    return allModelsStatus.value === ALLOW_LIST_UNREAD
      ? allModelsNotice.value
      : '請至少選擇一個模型'
  }
  if (!authStore.isAdmin && !allowListUsable(myAllowedStatus.value)) {
    return myAllowedStatus.value === ALLOW_LIST_UNREAD
      ? myAllowedNotice.value
      : '允許清單中沒有模型 · 請聯絡管理員'
  }
  return ''
})

onMounted(async () => {
  await keysStore.fetchKeys()
  allModelsResult.value = await loadAllowList(listModels)
  if (allModelsResult.value.loadFailed) {
    toast(allModelsNotice.value, { tone: 'error' })
  }
  if (!authStore.isAdmin) {
    myAllowedResult.value = await loadAllowList(getMyAllowedModels)
    if (myAllowedResult.value.loadFailed) {
      toast(myAllowedNotice.value, { tone: 'error' })
    }
  }
})

async function handleCreate() {
  if (!canCreate.value) return
  creating.value = true
  try {
    const payload = {
      name: (newKey.value.name || '').trim(),
      model_ids: authStore.isAdmin ? newKey.value.model_ids : myAllowedModels.value.map(m => m.id),
      expires_at: newKey.value.expires_at || null,
    }
    const data = await keysStore.create(payload)
    createdFullKey.value = data.full_key
    keyModalTitle.value = 'API 金鑰 · 已建立'
    showCreateModal.value = false
    showKeyModal.value = true
    copied.value = false
    hasCopied.value = false
    newKey.value = { name: '', model_ids: [], expires_at: '' }
  } catch (e) {
    toast(e.response?.data?.detail || '建立失敗', { tone: 'error' })
  } finally {
    creating.value = false
  }
}

function copyKey() {
  const text = createdFullKey.value
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text)
  } else {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } finally { document.body.removeChild(ta) }
  }
  copied.value = true
  hasCopied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}

function closeKeyModal() {
  if (!hasCopied.value) return
  showKeyModal.value = false
  createdFullKey.value = ''
}

function confirmRevoke(key) { revokeTarget.value = key; showRevokeConfirm.value = true }
async function handleRevoke() {
  if (revokeTarget.value) await keysStore.revoke(revokeTarget.value.id)
  showRevokeConfirm.value = false
  revokeTarget.value = null
}

function confirmRegenerate(key) { regenerateTarget.value = key; showRegenerateConfirm.value = true }
async function handleRegenerate() {
  showRegenerateConfirm.value = false
  if (!regenerateTarget.value) return
  try {
    const data = await keysStore.regenerate(regenerateTarget.value.id)
    createdFullKey.value = data.full_key
    keyModalTitle.value = 'API 金鑰 · 已重新產生'
    showKeyModal.value = true
    copied.value = false
    hasCopied.value = false
  } catch (e) {
    toast(e.response?.data?.detail || '重新產生失敗', { tone: 'error' })
  } finally {
    regenerateTarget.value = null
  }
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleString('en-GB')
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: var(--gap-3);
  flex-wrap: wrap;
}
.page-head__title {
  font-size: var(--t-2xl);
  font-weight: 600;
  letter-spacing: var(--tracking-tight);
  margin: 4px 0 2px;
}
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__code { color: var(--c-accent); background: var(--c-accent-soft); padding: 0 4px; border-radius: var(--r-soft); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
/* 讀取失敗的說明文字要看得出是錯誤,不能和「本來就是空的」長一樣。 */
.cell-meta--danger { color: var(--c-danger); font-weight: 500; }
.cell-code {
  font-family: var(--font-mono);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
}

.chip-row { display: flex; flex-wrap: wrap; gap: 4px; }
.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.check-list__row {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: 4px 0;
  font-size: var(--t-sm);
  color: var(--c-fg-1);
  cursor: pointer;
}
.check-list__row input { accent-color: var(--c-accent); }

.reveal__warn {
  font-size: var(--t-xs);
  color: var(--c-danger);
  letter-spacing: 0.04em;
  margin-bottom: var(--gap-2);
}
.reveal__key {
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border-strong);
  padding: var(--gap-3);
  font-family: var(--font-mono);
  font-size: var(--t-sm);
  color: var(--c-accent);
  word-break: break-all;
  white-space: pre-wrap;
  user-select: all;
  margin: 0;
}
.reveal__hint {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  margin-top: var(--gap-2);
}
.reveal__hint code {
  font-family: var(--font-mono);
  color: var(--c-fg-2);
}
</style>
