<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">模型</h1>
        <p class="page-head__sub">
          llm · vlm · embedding · agent — 經 /v1/* 代理的已註冊端點
        </p>
      </div>
      <div class="page-head__actions" v-if="authStore.isAdmin || canSetEndpointAddress">
        <TermButton v-if="authStore.isAdmin" variant="ghost" @click="openImportModal" label="整批帶入" />
        <TermButton
          v-if="canSetEndpointAddress"
          variant="primary"
          @click="openCreateModal"
          label="註冊模型"
        />
      </div>
    </header>

    <!-- P4.6b — 擁有者指派可設定端點位址的開發者（嵌在模型頁，非獨立路由） -->
    <TermBox
      v-if="authStore.isOwner"
      title="端點位址設定授權"
      hint="僅擁有者與下列開發者可登錄／變更模型端點位址"
    >
      <div class="author-grant">
        <div class="author-grant__form">
          <TermField label="指派開發者" hint="從開發者帳號中選擇；撤銷立即生效">
            <select v-model="grantUserId" class="term-select">
              <option :value="null">— 請選擇開發者 —</option>
              <option
                v-for="u in grantableDevelopers"
                :key="u.id"
                :value="u.id"
              >
                {{ u.username }}
              </option>
            </select>
          </TermField>
          <TermButton
            variant="primary"
            :disabled="!grantUserId || granting"
            :label="granting ? '指派中…' : '授予'"
            @click="handleGrantAuthor"
          />
        </div>
        <table v-if="endpointAuthors.length" class="term-table author-grant__table">
          <thead>
            <tr>
              <th>開發者</th>
              <th style="width: 40%">授予時間</th>
              <th style="width: 100px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in endpointAuthors" :key="g.id">
              <td class="cell-strong">{{ g.username || `user#${g.user_id}` }}</td>
              <td class="cell-meta">{{ formatGrantedAt(g.granted_at) }}</td>
              <td>
                <button
                  class="term-action term-action--danger"
                  :disabled="revokingId === g.id"
                  @click="handleRevokeAuthor(g)"
                >
                  {{ revokingId === g.id ? '撤銷中…' : '撤銷' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <TermEmpty v-else message="尚未指派任何開發者 · 目前僅擁有者可設定端點位址" />
      </div>
    </TermBox>

    <div class="kpi-row">
      <TermStat label="模型 · 總數" :value="modelsStore.models.length" />
      <TermStat label="健康" :value="healthyCount" tone="accent" />
      <TermStat label="降級" :value="degradedCount" :tone="degradedCount ? 'warn' : 'default'" />
      <TermStat label="異常" :value="unhealthyCount" :tone="unhealthyCount ? 'danger' : 'default'" />
    </div>

    <TermBox :title="`已註冊 · ${modelsStore.models.length}`" hint="每 60 秒健康檢查" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 96px">健康</th>
            <th>名稱</th>
            <th style="width: 100px">類型</th>
            <th style="width: 92px">分類上限</th>
            <th>端點</th>
            <th style="width: 80px">API</th>
            <th style="width: 80px">啟用</th>
            <th style="width: 110px">Router</th>
            <th v-if="authStore.isAdmin || canSetEndpointAddress" style="width: 26%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="model in modelsStore.models" :key="model.id">
            <td>
              <TermBadge :variant="healthVariant(model.health_status)" dot>{{ healthLabel(model.health_status) }}</TermBadge>
              <div v-if="testResults[model.id]?.latencyLabel" class="cell-meta cell-latency">{{ testResults[model.id].latencyLabel }}</div>
            </td>
            <td>
              <div class="cell-strong">
                <span
                  v-if="model.is_internal"
                  class="internal-lock"
                  title="lives on anila-models-net internal docker network — no host port exposure"
                >🔒</span>
                {{ model.display_name }}
              </div>
              <div class="cell-meta">{{ model.name }}</div>
              <div v-if="model.base_model_name" class="cell-base">↳ base: {{ model.base_model_name }}</div>
              <div class="cell-caps">
                <TermBadge v-if="model.protocol" variant="" class="cap-chip">{{ protocolLabel(model.protocol) }}</TermBadge>
                <TermBadge v-for="cap in capabilityChips(model)" :key="cap" variant="info" class="cap-chip">{{ cap }}</TermBadge>
                <span class="cap-key" :class="model.has_api_key ? 'cap-key--set' : 'cap-key--global'">
                  {{ model.has_api_key ? '已設定模型金鑰' : '使用全域金鑰' }}
                </span>
              </div>
            </td>
            <td><TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge></td>
            <td>
              <span v-if="classificationCeilingLabel(model.classification_ceiling) === '無上限'" class="cell-meta">無上限</span>
              <TermBadge v-else variant="accent">{{ classificationCeilingLabel(model.classification_ceiling) }}</TermBadge>
            </td>
            <td>
              <span
                v-if="model.endpoint_url === ENDPOINT_INTERNAL"
                class="cell-meta cell-meta--internal"
                title="endpoint lives on anila-models-net (cross-stack docker DNS) — owner can see the URL"
              >🔒 internal</span>
              <span
                v-else-if="model.endpoint_url === ENDPOINT_REDACTED"
                class="cell-meta"
                title="endpoint URL is owner-only (deployment topology)"
              >🔒 owner-only</span>
              <code v-else class="cell-url" :title="model.endpoint_url">{{ model.endpoint_url }}</code>
            </td>
            <td class="cell-meta">{{ model.api_version }}</td>
            <td>
              <TermBadge :variant="model.is_active ? 'ok' : 'danger'" dot>
                {{ model.is_active ? '開' : '關' }}
              </TermBadge>
            </td>
            <td>
              <span v-if="model.is_router_primary" class="primary-pill" title="ANILA Router uses this as primary LLM">
                ★ 主要
              </span>
              <span v-else class="cell-meta">—</span>
            </td>
            <td v-if="authStore.isAdmin || canSetEndpointAddress">
              <div class="row-actions">
                <button class="term-action" @click="openEditModal(model)">編輯</button>
                <template v-if="authStore.isAdmin">
                  <span class="row-actions__sep">·</span>
                  <button class="term-action" @click="handleHealthCheck(model.id)">探測</button>
                  <span class="row-actions__sep">·</span>
                  <button
                    class="term-action"
                    :disabled="testingId === model.id"
                    title="主動探測此端點連線並回報五態健康與延遲"
                    @click="handleTest(model)"
                  >{{ testingId === model.id ? '測試中…' : '測試連線' }}</button>
                  <span v-if="model.model_type === 'llm' && !model.is_router_primary" class="row-actions__sep">·</span>
                  <button
                    v-if="model.model_type === 'llm' && !model.is_router_primary"
                    class="term-action"
                    :disabled="!model.is_active || settingPrimaryId === model.id"
                    @click="handleSetPrimary(model.id)"
                  >
                    {{ settingPrimaryId === model.id ? '設定中…' : '設為主要' }}
                  </button>
                  <span v-else-if="model.is_router_primary" class="row-actions__sep">·</span>
                  <button
                    v-if="model.is_router_primary"
                    class="term-action"
                    :disabled="settingPrimaryId === model.id"
                    @click="handleUnsetPrimary(model.id)"
                  >
                    取消主要
                  </button>
                  <span class="row-actions__sep">·</span>
                  <button
                    v-if="model.is_active"
                    class="term-action"
                    @click="handleDeactivate(model.id)"
                  >停用</button>
                  <button
                    v-else
                    class="term-action"
                    @click="handleActivate(model.id)"
                  >啟用</button>
                  <span v-if="authStore.isOwner" class="row-actions__sep">·</span>
                  <button
                    v-if="authStore.isOwner"
                    class="term-action term-action--danger"
                    :disabled="purgingId === model.id"
                    :title="'hard-delete this row · irreversible · owner-only'"
                    @click="handlePurge(model)"
                  >
                    {{ purgingId === model.id ? '清除中…' : '清除' }}
                  </button>
                </template>
              </div>
            </td>
          </tr>
          <tr v-if="modelsStore.models.length === 0">
            <td :colspan="(authStore.isAdmin || canSetEndpointAddress) ? 9 : 8"><TermEmpty message="尚未註冊模型 · 註冊後即可啟用 /v1/* 代理" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? '編輯 · 模型' : '註冊 · 模型'" width="600px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="模型 ID" hint="不可變更 · 用於 API 請求 · 例：llama3-70b">
          <input v-model="form.name" :disabled="!!editingId" class="term-input" placeholder="llama3-70b" />
        </TermField>
        <TermField label="顯示名稱">
          <input
            v-model="form.display_name"
            class="term-input"
            placeholder="Llama 3 70B Instruct"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <div class="form-row-2">
          <TermField label="類型">
            <select v-model="form.model_type" class="term-select" :disabled="addressOnlyEditor">
              <option value="llm">llm</option>
              <option value="vlm">vlm</option>
              <option value="embedding">embedding</option>
              <option value="agent">agent</option>
            </select>
          </TermField>
          <TermField label="API 版本">
            <select v-model="form.api_version" class="term-select" :disabled="addressOnlyEditor">
              <option value="v1">v1</option>
              <option value="v2">v2</option>
            </select>
          </TermField>
        </div>
        <TermField
          label="端點 URL"
          :hint="endpointFieldLocked
            ? '🔒 僅擁有者與獲授權開發者可變更端點位址'
            : addressOnlyEditor
              ? '獲授權開發者僅可變更端點位址'
              : '登錄／變更端點位址需擁有者或獲授權開發者身分'"
        >
          <input
            v-model="form.endpoint_url"
            class="term-input"
            :disabled="endpointFieldLocked"
            :placeholder="endpointFieldLocked ? '— 無權設定位址 —' : 'http://gemma4:8000/v1'"
          />
        </TermField>
        <TermField
          label="內部"
          hint="位於 anila-models-net（跨 stack docker DNS）— 不對外開 host port，URL 僅 owner 可見"
        >
          <label class="internal-checkbox">
            <input
              v-model="form.is_internal"
              type="checkbox"
              :disabled="endpointFieldLocked || addressOnlyEditor"
            />
            <span>{{ form.is_internal ? '內部 · 僅平台 stack 內可連' : '外部 · 內網 LAN 或公開端點' }}</span>
          </label>
        </TermField>
        <div class="form-row-2">
          <TermField label="協定 · protocol" hint="端點所講的 wire protocol">
            <select v-model="form.protocol" class="term-select" :disabled="addressOnlyEditor">
              <option v-for="p in PROTOCOL_OPTIONS" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
          </TermField>
          <TermField label="分類上限" hint="可承接的最高分類；留空＝無上限">
            <select
              v-model="form.classification_ceiling"
              class="term-select"
              :disabled="addressOnlyEditor"
            >
              <option :value="null">— 無上限 —</option>
              <option v-for="lvl in CLASSIFICATION_LEVELS" :key="lvl" :value="lvl">{{ lvl }}</option>
            </select>
          </TermField>
        </div>
        <TermField
          label="模型金鑰 · api key"
          optional
          hint="僅寫入,不會回顯;留空=沿用現值或全域金鑰"
        >
          <input
            v-model="form.api_key"
            type="password"
            autocomplete="new-password"
            class="term-input"
            placeholder="Bearer 金鑰(留空＝沿用現值/全域)"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <TermField label="描述" optional>
          <textarea
            v-model="form.description"
            rows="2"
            class="term-textarea"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <TermField label="context window" optional hint="tokens">
          <input
            v-model.number="form.context_window"
            type="number"
            class="term-input"
            placeholder="128000"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <TermField v-if="form.model_type === 'agent'" label="基礎模型" hint="用於用量歸屬">
          <select v-model="form.base_model_id" class="term-select" :disabled="addressOnlyEditor">
            <option :value="null">— 獨立 —</option>
            <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
              {{ m.display_name }} ({{ m.model_type }})
            </option>
          </select>
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="!form.name || !form.display_name || (!endpointFieldLocked && !form.endpoint_url)"
          :label="editingId ? '更新' : '註冊'"
          @click="handleSubmit"
        />
      </template>
    </TermModal>

    <!-- P4.6 — 整批帶入：選已註冊端點 → 拉上游 /v1/models → 回報計數 -->
    <TermModal
      :visible="showImportModal"
      title="整批帶入 · 模型"
      width="560px"
      @close="closeImportModal"
    >
      <div class="form-grid">
        <p class="import-hint">
          從已註冊端點拉取上游 <code>/models</code> 清單並寫入登錄表。
          已存在的名稱不會覆寫管理員設定；格式錯誤的項目會略過並附原因。
          新帶入列只繼承端點層級欄位（含分類上限），context window 與能力旗標維持保守預設，並維持停用待檢視後啟用。
          相同端點會合併為一個選項（位址本身仍僅擁有者可見）。
        </p>
        <TermField
          label="來源端點"
          hint="相同端點位址會合併為一個選項"
        >
          <select v-model="importSourceId" class="term-select">
            <option :value="null">— 請選擇 —</option>
            <option
              v-for="opt in importEndpointOptions"
              :key="opt.sourceModelId"
              :value="opt.sourceModelId"
            >
              {{ opt.label }}
            </option>
          </select>
        </TermField>
        <div v-if="importResult" class="import-result">
          <p class="import-result__summary">
            新增 {{ importResult.created }} ·
            已存在 {{ importResult.already_existed }} ·
            略過 {{ importResult.skipped }}
            <template v-if="importResult.truncated"> · 尚有 {{ importResult.truncated }} 筆未帶入（可再次執行以繼續）</template>
            <template v-if="importResult.missing_from_listing?.length">
              · 上游未列出 {{ importResult.missing_from_listing.length }}
            </template>
          </p>
          <ul v-if="importResult.created_entries?.length" class="import-result__list">
            <li v-for="e in importResult.created_entries" :key="'c-' + e.name">
              新增 · {{ e.name }}
              <span v-if="e.guessed_fields?.length" class="cell-meta">
                — 待確認: {{ e.guessed_fields.join(', ') }}
              </span>
            </li>
          </ul>
          <ul v-else-if="importResult.created_names?.length" class="import-result__list">
            <li v-for="n in importResult.created_names" :key="'c-' + n">新增 · {{ n }}</li>
          </ul>
          <ul v-if="importResult.unchanged?.length" class="import-result__list">
            <li v-for="u in importResult.unchanged" :key="'u-' + u.name">
              未變更 · {{ u.name }}
              <span class="cell-meta"> — {{ u.reason }}</span>
            </li>
          </ul>
          <ul v-if="importResult.skipped_entries?.length" class="import-result__list import-result__list--skip">
            <li v-for="(s, i) in importResult.skipped_entries" :key="'s-' + i">
              略過 · {{ s.name || '（無名稱）' }}
              <span class="cell-meta"> — {{ s.reason }}</span>
            </li>
          </ul>
          <ul v-if="importResult.missing_from_listing?.length" class="import-result__list import-result__list--skip">
            <li v-for="m in importResult.missing_from_listing" :key="'m-' + m.name">
              上游未列出 · {{ m.name }}
              <span class="cell-meta"> — {{ m.reason }}</span>
            </li>
          </ul>
        </div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeImportModal" label="關閉" />
        <TermButton
          v-if="importResult?.created_names?.length"
          variant="ghost"
          :disabled="activatingCreated"
          :label="activatingCreated ? '啟用中…' : `啟用本次新增（${importResult.created_names.length}）`"
          @click="handleActivateCreated"
        />
        <TermButton
          variant="primary"
          :disabled="!importSourceId || importing"
          :label="importing ? '帶入中…' : '開始帶入'"
          @click="handleImport"
        />
      </template>
    </TermModal>

    <!-- Phase 2 — typed 400 confirm modal. Shows when backend rejects a
         register/update because the hostname isn't in trusted_hosts AND
         the failure is fixable (single-label / internal-zone, NOT
         loopback/metadata). Owner can one-click promote + retry. -->
    <TermModal
      :visible="!!untrustedHostPrompt"
      title="hostname 未在受信任清單"
      width="540px"
      @close="cancelTrustPrompt"
    >
      <div v-if="untrustedHostPrompt" class="trust-prompt">
        <p class="trust-prompt__hint">{{ untrustedHostPrompt.hint }}</p>
        <p class="trust-prompt__cta">
          要把 <code>{{ untrustedHostPrompt.host }}</code> 加進 trusted_hosts 並重試嗎?
          <br>
          <span class="cell-meta">會寫一筆 audit log,事後可在 /trusted-hosts 移除。</span>
        </p>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="cancelTrustPrompt" label="取消" />
        <TermButton
          variant="primary"
          :label="`加入 ${untrustedHostPrompt?.host || ''} 並重試`"
          @click="confirmTrustAndRetry"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useModelsStore } from '../stores/models'
import { useAuthStore } from '../stores/auth'
import {
  getMyEndpointAuthorStatus,
  listEndpointAuthors,
  grantEndpointAuthor,
  revokeEndpointAuthor,
} from '../api/models'
import { listUsers } from '../api/users'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import { healthLabel, healthVariant, normalizeHealth } from '../utils/healthStatus'

const { confirm, toast } = useDialog()
const modelsStore = useModelsStore()
const authStore = useAuthStore()
const showModal = ref(false)
const editingId = ref(null)
const purgingId = ref(null)
const settingPrimaryId = ref(null)
// P4.6 — 整批帶入 modal 狀態
const showImportModal = ref(false)
const importSourceId = ref(null)
const importing = ref(false)
const importResult = ref(null)
const activatingCreated = ref(false)
// P4.6b — 端點位址設定授權
const canSetEndpointAddress = ref(false)
const endpointAuthors = ref([])
const developerUsers = ref([])
const grantUserId = ref(null)
const granting = ref(false)
const revokingId = ref(null)
// Slice 6b — 每列一個「測試連線」狀態：testingId 顯示 spinner；
// testResults[id] 快取最近一次探測的延遲標籤（五態 badge 由 refetch 後的
// health_status 反映）。
const testingId = ref(null)
const testResults = ref({})

// doc 04 §2 protocol 列舉。label 為繁中；未知值以原字串回退顯示（防禦 6a）。
const PROTOCOL_OPTIONS = [
  { value: 'openai_compatible', label: 'OpenAI 相容' },
  { value: 'custom_adapter', label: '自訂轉接' },
]
const PROTOCOL_LABELS = Object.fromEntries(PROTOCOL_OPTIONS.map(p => [p.value, p.label]))

// SYSTEM-MAP §8 四級分類（無機密 < 營業秘密 < 密 < 機密）。
const CLASSIFICATION_LEVELS = ['無機密', '營業秘密', '密', '機密']

// supports_* → 能力晶片繁中標籤。缺欄位（6a 未落地）時該晶片不顯示。
const CAPABILITY_LABELS = {
  supports_streaming: '串流',
  supports_json_schema: '結構化輸出',
  supports_tools: '工具呼叫',
}

function protocolLabel(p) {
  if (!p) return '—'
  return PROTOCOL_LABELS[p] || p
}
function classificationCeilingLabel(c) {
  return c || '無上限'
}
function capabilityChips(model) {
  return Object.entries(CAPABILITY_LABELS)
    .filter(([key]) => model[key])
    .map(([, label]) => label)
}

const defaultForm = () => ({
  name: '', display_name: '', model_type: 'llm', endpoint_url: '',
  api_version: 'v1', description: '', context_window: null, base_model_id: null,
  // Default true matches backend ModelCreate schema — new registrations are
  // expected to land on the anila-models-net cross-stack docker network.
  // Admin can untick for an external on-prem LAN endpoint.
  is_internal: true,
  // Slice 6b — model gateway governance。protocol 預設 openai_compatible;
  // classification_ceiling null = 無上限;api_key 為 write-only（留空不覆蓋）。
  protocol: 'openai_compatible', classification_ceiling: null, api_key: '',
})
const form = ref(defaultForm())

const baseModelOptions = computed(() =>
  modelsStore.models.filter(m =>
    m.model_type !== 'agent' && m.is_active && m.id !== editingId.value
  )
)

// Sentinels returned by backend when endpoint_url is redacted from non-owner
// viewers. Keep in sync with services/csp/app/api/models.py.
//   <owner-only>  — generic redaction (external endpoint, owner-only)
//   <internal>    — additional hint: row lives on anila-models-net,
//                    unreachable from outside the platform stack
const ENDPOINT_REDACTED = '<owner-only>'
const ENDPOINT_INTERNAL = '<internal>'

// P4.6b — 管理員以上有 endpoint_group_key（位址仍 owner-only）。
// 分組恢復後相同閘道合併為一個選項；key 為空時才退回 id:（非管理員）。
const importEndpointOptions = computed(() => {
  const seen = new Set()
  const opts = []
  for (const m of modelsStore.models) {
    const isRedacted =
      m.endpoint_url === ENDPOINT_REDACTED || m.endpoint_url === ENDPOINT_INTERNAL
    const key = m.endpoint_group_key || (isRedacted ? `id:${m.id}` : (m.endpoint_url || `id:${m.id}`))
    if (seen.has(key)) continue
    seen.add(key)
    const urlLabel = isRedacted
      ? `${m.display_name}（${m.endpoint_url}）`
      : m.endpoint_url
    opts.push({
      sourceModelId: m.id,
      label: `${urlLabel} · 代表列 ${m.name}`,
    })
  }
  return opts
})

const grantableDevelopers = computed(() => {
  const activeIds = new Set(endpointAuthors.value.map(g => g.user_id))
  return developerUsers.value.filter(u => !activeIds.has(u.id))
})

function formatGrantedAt(value) {
  if (!value) return '—'
  try {
    return new Date(value).toLocaleString('zh-TW')
  } catch {
    return String(value)
  }
}

async function loadEndpointAuthorState() {
  try {
    const { data } = await getMyEndpointAuthorStatus()
    canSetEndpointAddress.value = !!data?.can_set_endpoint_address
  } catch {
    canSetEndpointAddress.value = !!authStore.isOwner
  }
  if (!authStore.isOwner) return
  try {
    const [{ data: grants }, { data: users }] = await Promise.all([
      listEndpointAuthors(),
      listUsers(),
    ])
    endpointAuthors.value = Array.isArray(grants) ? grants : []
    developerUsers.value = (Array.isArray(users) ? users : []).filter(
      u => u.role === 'developer' && u.is_active !== false,
    )
  } catch (e) {
    const detail = e.response?.data?.detail
    toast(typeof detail === 'string' ? detail : '無法載入端點位址授權清單', { tone: 'error' })
  }
}

async function handleGrantAuthor() {
  if (!grantUserId.value || granting.value) return
  granting.value = true
  try {
    await grantEndpointAuthor(grantUserId.value)
    grantUserId.value = null
    toast('已授予端點位址設定權限', { tone: 'success' })
    await loadEndpointAuthorState()
  } catch (e) {
    const detail = e.response?.data?.detail
    toast(typeof detail === 'string' ? detail : '授予失敗', { tone: 'error' })
  } finally {
    granting.value = false
  }
}

async function handleRevokeAuthor(grant) {
  if (!grant || revokingId.value === grant.id) return
  if (!(await confirm({
    message: `撤銷「${grant.username || grant.user_id}」的端點位址設定權限？立即生效。`,
    confirmText: '撤銷',
    danger: true,
  }))) return
  revokingId.value = grant.id
  try {
    await revokeEndpointAuthor(grant.id)
    toast('已撤銷端點位址設定權限', { tone: 'success' })
    await loadEndpointAuthorState()
  } catch (e) {
    const detail = e.response?.data?.detail
    toast(typeof detail === 'string' ? detail : '撤銷失敗', { tone: 'error' })
  } finally {
    revokingId.value = null
  }
}

// KPI 以正規化五態計數，兼容舊值（online/connecting/offline）與新值。
const healthyCount = computed(() => modelsStore.models.filter(m => normalizeHealth(m.health_status) === 'healthy').length)
const degradedCount = computed(() => modelsStore.models.filter(m => normalizeHealth(m.health_status) === 'degraded').length)
const unhealthyCount = computed(() => modelsStore.models.filter(m => normalizeHealth(m.health_status) === 'unhealthy').length)

onMounted(() => {
  modelsStore.fetchModels()
  loadEndpointAuthorState()
})

function openCreateModal() { editingId.value = null; form.value = defaultForm(); showModal.value = true }
function openImportModal() {
  importSourceId.value = null
  importResult.value = null
  showImportModal.value = true
}
function closeImportModal() {
  showImportModal.value = false
  importing.value = false
}
async function handleImport() {
  if (!importSourceId.value || importing.value) return
  const source = modelsStore.models.find(m => m.id === importSourceId.value)
  const label = source
    ? (source.endpoint_url === ENDPOINT_REDACTED || source.endpoint_url === ENDPOINT_INTERNAL
      ? source.display_name
      : source.endpoint_url)
    : String(importSourceId.value)
  if (!(await confirm({
    message: `自「${label}」整批帶入上游模型清單？已存在的名稱會保留本機設定，不會覆寫。`,
    confirmText: '開始帶入',
  }))) return
  importing.value = true
  importResult.value = null
  try {
    const data = await modelsStore.importFromEndpoint(importSourceId.value)
    importResult.value = data
    const trunc = data.truncated
      ? ` · 尚有 ${data.truncated} 筆未帶入，可再次執行以繼續`
      : ''
    toast(
      `整批帶入完成 · 新增 ${data.created} · 已存在 ${data.already_existed} · 略過 ${data.skipped}${trunc}`,
      { tone: (data.skipped || data.truncated) ? 'warn' : 'success' },
    )
  } catch (e) {
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string'
      ? detail
      : (detail?.message || '整批帶入失敗')
    toast(msg, { tone: 'error' })
  } finally {
    importing.value = false
  }
}

async function handleActivateCreated() {
  if (!importSourceId.value || !importResult.value?.created_names?.length || activatingCreated.value) return
  const n = importResult.value.created_names.length
  if (!(await confirm({
    message: `啟用本次新增的 ${n} 個模型？啟用後即可被路由選用；請確認分類上限與能力欄位。`,
    confirmText: '啟用',
  }))) return
  activatingCreated.value = true
  try {
    const data = await modelsStore.activateCreated(
      importSourceId.value,
      importResult.value.created_names,
    )
    toast(
      `已啟用 ${data.activated} 個模型` +
        (data.already_active ? ` · 原本已啟用 ${data.already_active}` : '') +
        (data.wrong_endpoint || data.not_found
          ? ` · 未處理 ${(data.wrong_endpoint || 0) + (data.not_found || 0)}`
          : ''),
      { tone: 'success' },
    )
  } catch (e) {
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string'
      ? detail
      : (detail?.message || '整批啟用失敗')
    toast(msg, { tone: 'error' })
  } finally {
    activatingCreated.value = false
  }
}
function openEditModal(model) {
  editingId.value = model.id
  // Drop the sentinel before populating the form — otherwise saving
  // would PUT the literal "<owner-only>" string back to backend and
  // corrupt the registered endpoint. Non-owner admins see a placeholder
  // hint instead and the field is disabled.
  // Both sentinels (<owner-only> / <internal>) must be stripped before
  // populating the form — otherwise saving would PUT the literal string
  // back. Non-owner admins see a placeholder + disabled field.
  const isRedacted =
    model.endpoint_url === ENDPOINT_REDACTED ||
    model.endpoint_url === ENDPOINT_INTERNAL
  const endpointUrl = isRedacted ? '' : model.endpoint_url
  form.value = {
    name: model.name, display_name: model.display_name,
    model_type: model.model_type, endpoint_url: endpointUrl,
    api_version: model.api_version, description: model.description || '',
    context_window: model.context_window, base_model_id: model.base_model_id || null,
    is_internal: !!model.is_internal,
    // 防禦性：6a 未落地時欄位可能為 undefined，給合理預設。api_key 為 write-only,
    // 永不從後端回顯（後端也不回傳明文金鑰），故一律留空。
    protocol: model.protocol || 'openai_compatible',
    classification_ceiling: model.classification_ceiling ?? null,
    api_key: '',
  }
  showModal.value = true
}

// P4.6b: 新建一律需可設定位址；編輯時無權者不得送出／改寫 endpoint_url。
const endpointFieldLocked = computed(() => !canSetEndpointAddress.value)
// 獲授權開發者僅能改位址；管理員／擁有者維持完整更新表單。
const addressOnlyEditor = computed(
  () => !!editingId.value && canSetEndpointAddress.value && !authStore.isAdmin,
)

// Phase 2 模型 stack 解耦 — SSRF guard 對 single-label / internal-zone
// hostname 回 typed 400 (detail 是 dict 不是 string)。前端在 catch 偵測
// 到 code === "untrusted_host" 時跳 confirm modal,owner 一鍵把該
// hostname 加進 trusted_hosts 後重試 — 不必離開「register model」流程
// 去切到另一個分頁手動操作。
const untrustedHostPrompt = ref(null)   // { host, message, hint, retryPayload, retryMode }

// 由 form 組出送出 payload — register / update / trust-retry 三處共用，
// 避免治理欄位（api_key write-only、base_model_id、locked endpoint）漏處理。
function buildModelPayload() {
  // Designated non-admin authors: address only (matches backend gate).
  if (addressOnlyEditor.value) {
    return { endpoint_url: form.value.endpoint_url }
  }
  const payload = { ...form.value }
  if (payload.model_type !== 'agent') payload.base_model_id = null
  // Don't ship endpoint_url back when the field was locked (admin editing a
  // row whose URL they couldn't see). Backend would accept the empty string
  // and overwrite the real endpoint with junk.
  if (endpointFieldLocked.value) delete payload.endpoint_url
  // api_key 為 write-only：留空 = 沿用現值或全域金鑰,絕不送空字串把既有金鑰清掉。
  if (!payload.api_key) delete payload.api_key
  return payload
}

async function handleSubmit() {
  try {
    const payload = buildModelPayload()
    if (editingId.value) {
      const { name, ...updateData } = payload
      await modelsStore.update(editingId.value, updateData)
    } else {
      await modelsStore.create(payload)
    }
    showModal.value = false
  } catch (e) {
    const detail = e.response?.data?.detail
    // Typed 400 with code "untrusted_host" → show confirm modal so the
    // owner can promote the host to trusted_hosts and retry without
    // leaving this page. Plain-string detail (loopback / metadata /
    // scheme failures) falls through to the existing alert path —
    // those aren't fixable by adding to trust list.
    if (
      detail &&
      typeof detail === 'object' &&
      detail.code === 'untrusted_host' &&
      detail.host &&
      authStore.isOwner
    ) {
      const payload = buildModelPayload()
      untrustedHostPrompt.value = {
        host: detail.host,
        message: detail.message || '',
        hint: detail.hint || '',
        retryPayload: payload,
        retryMode: editingId.value ? 'update' : 'create',
        retryId: editingId.value,
      }
      return
    }
    const msg = typeof detail === 'string'
      ? detail
      : (detail?.message || '操作失敗')
    toast(msg, { tone: 'error' })
  }
}

async function confirmTrustAndRetry() {
  const prompt = untrustedHostPrompt.value
  if (!prompt) return
  try {
    // Lazy import so loading ModelsView for non-owner viewers doesn't
    // even pull the trusted-hosts API client.
    const { createTrustedHost } = await import('../api/trustedHosts')
    await createTrustedHost({
      host: prompt.host,
      note: `auto-added via /models register on ${new Date().toISOString()}`,
    })
    // Retry the original submit. cache TTL is 30s but the service
    // invalidates on mutation, so the retry should see the new host
    // immediately on the same CSP worker. Cross-worker eventual
    // consistency: at worst the admin sees the same error again and
    // can retry once more.
    const { retryPayload, retryMode, retryId } = prompt
    if (retryMode === 'update') {
      const { name, ...updateData } = retryPayload
      await modelsStore.update(retryId, updateData)
    } else {
      await modelsStore.create(retryPayload)
    }
    untrustedHostPrompt.value = null
    showModal.value = false
  } catch (e) {
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : (detail?.message || '重試失敗')
    toast(msg, { tone: 'error' })
  }
}

function cancelTrustPrompt() {
  untrustedHostPrompt.value = null
}

async function handleHealthCheck(id) {
  const result = await modelsStore.checkHealth(id)
  toast(`健康探測 → ${result.status}\n${result.detail}`, { tone: result.status === 'healthy' ? 'success' : 'error' })
}

// Slice 6b — 主動探測連線。POST /test → 五態 + 延遲。防禦性讀取欄位
// （health_status / status、latency_ms / latencyMs），並依五態決定 toast 語氣。
async function handleTest(model) {
  if (testingId.value === model.id) return
  testingId.value = model.id
  try {
    const result = await modelsStore.test(model.id)
    const status = result?.health_status ?? result?.status
    const latency = result?.latency_ms ?? result?.latencyMs ?? null
    testResults.value = {
      ...testResults.value,
      [model.id]: { latencyLabel: latency != null ? `延遲 ${latency} ms` : '' },
    }
    const ok = normalizeHealth(status) === 'healthy'
    const latencyTxt = latency != null ? `（${latency} ms）` : ''
    toast(`測試連線 → ${healthLabel(status)}${latencyTxt}`, { tone: ok ? 'success' : 'error' })
  } catch (e) {
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : (detail?.message || '測試連線失敗')
    toast(msg, { tone: 'error' })
  } finally {
    testingId.value = null
  }
}

async function handleSetPrimary(id) {
  settingPrimaryId.value = id
  try { await modelsStore.setPrimary(id) }
  catch (e) { toast(e.response?.data?.detail || '設定主要失敗', { tone: 'error' }) }
  finally { settingPrimaryId.value = null }
}
async function handleUnsetPrimary(id) {
  if (!(await confirm({ message: '取消主要？在你指定新的主要模型前，ANILA Router 將沒有主要 LLM。', confirmText: '取消主要', danger: true }))) return
  settingPrimaryId.value = id
  try { await modelsStore.unsetPrimary(id) }
  catch (e) { toast(e.response?.data?.detail || '取消主要失敗', { tone: 'error' }) }
  finally { settingPrimaryId.value = null }
}
async function handleDeactivate(id) {
  if (await confirm({ message: '停用此模型？之後可透過該列的「啟用」按鈕重新啟用。', confirmText: '停用', danger: true })) {
    await modelsStore.remove(id)
  }
}
async function handleActivate(id) {
  try { await modelsStore.activate(id) }
  catch (e) { toast(e.response?.data?.detail || '啟用失敗', { tone: 'error' }) }
}
async function handlePurge(model) {
  if (!model || purgingId.value === model.id) return
  if (!(await confirm({ message: `永久刪除「${model.display_name}」？不可復原。若有用量紀錄或其他模型引用則會被拒絕。`, confirmText: '永久刪除', danger: true }))) return
  purgingId.value = model.id
  try { await modelsStore.purge(model.id) }
  catch (e) { toast(e.response?.data?.detail || '清除失敗', { tone: 'error' }) }
  finally { purgingId.value = null }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__actions { display: inline-flex; align-items: center; gap: var(--gap-2); flex-wrap: wrap; }

.author-grant { display: flex; flex-direction: column; gap: var(--gap-3); padding: var(--gap-3); }
.author-grant__form {
  display: flex; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap;
}
.author-grant__form .term-field { flex: 1; min-width: 220px; }
.author-grant__table { margin-top: var(--gap-2); }

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-3); }
@media (max-width: 800px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }

.import-hint { margin: 0; font-size: var(--t-sm); color: var(--c-fg-2); line-height: 1.5; }
.import-hint code {
  font-family: var(--font-mono); font-size: var(--t-2xs);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border); padding: 1px 6px;
}
.import-result { display: flex; flex-direction: column; gap: var(--gap-2); }
.import-result__summary { margin: 0; font-size: var(--t-sm); color: var(--c-fg-1); font-weight: 500; }
.import-result__list {
  margin: 0; padding-left: 1.2em; font-size: var(--t-xs); color: var(--c-fg-2);
  max-height: 180px; overflow: auto;
}
.import-result__list--skip { color: var(--c-warn, #9a6700); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-meta--internal { color: var(--c-ok, #2ea043); }
.cell-base { color: var(--c-info); font-size: var(--t-2xs); margin-top: 2px; }
.internal-lock {
  display: inline-block;
  margin-right: 4px;
  font-size: 0.85em;
  color: var(--c-ok, #2ea043);
  cursor: help;
}
.internal-checkbox {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: var(--t-xs);
  color: var(--c-fg-2);
  cursor: pointer;
  user-select: none;
}
.internal-checkbox input[type="checkbox"] {
  cursor: pointer;
}
.trust-prompt { display: flex; flex-direction: column; gap: var(--gap-3); }
.trust-prompt__hint { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; }
.trust-prompt__cta { color: var(--c-fg-1); font-size: var(--t-sm); margin: 0; }
.trust-prompt__cta code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 6px;
  font-size: var(--t-2xs); color: var(--c-accent);
}
.cell-url {
  display: inline-block;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
}

.cell-latency { margin-top: 3px; }

/* Slice 6b — 治理能力晶片 + 模型金鑰指示（name cell meta）。 */
.cell-caps { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.cap-chip { font-size: var(--t-2xs); }
.cap-key {
  display: inline-flex;
  align-items: center;
  font-size: var(--t-2xs);
  letter-spacing: 0.02em;
  padding: 1px 6px;
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-soft);
}
.cap-key--set { color: var(--c-ok, #2ea043); border-color: var(--c-ok, #2ea043); background: var(--c-ok-soft); }
.cap-key--global { color: var(--c-fg-3); }

.primary-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--t-2xs);
  color: var(--c-warn);
  border: var(--border-w) solid var(--c-warn);
  padding: 1px 6px;
  background: var(--c-warn-soft);
  letter-spacing: 0.04em;
}

.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); flex-wrap: wrap; }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
</style>
