<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">執行設定 · {{ agent?.name || agentId }}</h1>
        <p class="page-head__sub">
          工具權限 · 工作區上限 · 護欄 — <strong>唯讀檢查</strong>。熱更新未出貨，官方 agent 不會輪詢此設定；此頁無法寫入。
        </p>
      </div>
      <div class="page-head__actions">
        <TermButton @click="goBack" label="← Agent" />
      </div>
    </header>

    <div v-if="feedback.message" class="feedback" :class="feedback.type === 'error' ? 'is-err' : 'is-ok'">
      <span>{{ feedback.type === 'error' ? '!' : '✓' }}</span>
      <span>{{ feedback.message }}</span>
    </div>

    <div v-if="loading" class="cell-meta" style="padding: 12px 0;">載入中…</div>

    <template v-else>
      <TermBox title="狀態" pad="md">
        <div class="retired-banner" data-testid="runtime-config-retired-banner">
          熱更新未出貨 — 此處顯示的是資料庫裡歷史留下的 JSON（若有），
          <strong>不會套用到任何正式 agent</strong>。要改工具權限／護欄請改 agent 程式碼。
          寫入 API 已回 410；本頁已移除儲存控制項。
        </div>
        <dl class="status-list">
          <div>
            <dt>覆寫</dt>
            <dd>
              <TermBadge :variant="hasOverride ? 'accent' : ''">
                {{ hasOverride ? '資料庫有值（未生效）' : '無（程式碼預設）' }}
              </TermBadge>
            </dd>
          </div>
          <div>
            <dt>生效狀態</dt>
            <dd class="cell-meta">未出貨 · 唯讀</dd>
          </div>
        </dl>
      </TermBox>

      <TermBox title="工具權限（唯讀）" pad="md">
        <div class="grid">
          <TermField label="allow_list">
            <input :value="permsView.allow_list_csv" class="term-input" readonly disabled />
          </TermField>
          <TermField label="deny_list">
            <input :value="permsView.deny_list_csv" class="term-input" readonly disabled />
          </TermField>
          <TermField label="ask_tools">
            <input :value="permsView.ask_tools_csv" class="term-input" readonly disabled />
          </TermField>
          <TermField label="deny_tools">
            <input :value="permsView.deny_tools_csv" class="term-input" readonly disabled />
          </TermField>
        </div>
      </TermBox>

      <TermBox title="工作區上限（唯讀）" pad="md">
        <div class="grid">
          <TermField label="fs_read">
            <input :value="fmtBool(wsView.fs_read)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="fs_write">
            <input :value="fmtBool(wsView.fs_write)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="network">
            <input :value="fmtBool(wsView.network)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="exec_bash">
            <input :value="fmtBool(wsView.exec_bash)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="exec_python">
            <input :value="fmtBool(wsView.exec_python)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="max_exec_seconds">
            <input :value="fmtNum(wsView.max_exec_seconds)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="max_workspace_size_mb">
            <input :value="fmtNum(wsView.max_workspace_size_mb)" class="term-input" readonly disabled />
          </TermField>
          <TermField label="command_allowlist">
            <input :value="wsView.command_allowlist_csv" class="term-input" readonly disabled />
          </TermField>
        </div>
      </TermBox>

      <TermBox title="護欄（唯讀）" pad="md">
        <TermSection title="輸入護欄" />
        <p v-if="!guardrailsView.input.length" class="cell-meta">（無）</p>
        <table v-else class="guardrail-table">
          <thead>
            <tr>
              <th>kind</th>
              <th>tool</th>
              <th>params</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(g, idx) in guardrailsView.input" :key="`in-${idx}`">
              <td>{{ g.kind }}</td>
              <td>{{ g.tool }}</td>
              <td class="mono">{{ g.paramsRaw }}</td>
            </tr>
          </tbody>
        </table>

        <TermSection title="輸出護欄" />
        <p v-if="!guardrailsView.output.length" class="cell-meta">（無）</p>
        <table v-else class="guardrail-table">
          <thead>
            <tr>
              <th>kind</th>
              <th>tool</th>
              <th>params</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(g, idx) in guardrailsView.output" :key="`out-${idx}`">
              <td>{{ g.kind }}</td>
              <td>{{ g.tool }}</td>
              <td class="mono">{{ g.paramsRaw }}</td>
            </tr>
          </tbody>
        </table>
      </TermBox>

      <TermBox title="功能" pad="md">
        <p class="hint" style="margin-bottom: 8px;">
          功能 — 在 ANILA 對話介面跟此 agent 對話時提供給使用者。純宣告式設定，
          不執行任何程式碼。此區塊仍可編輯（與上方未出貨的 runtime_config 無關）。
        </p>
        <div v-if="fnError" class="feedback is-err" style="margin-bottom: 8px;">
          <span>!</span><span>{{ fnError }}</span>
        </div>
        <ul v-if="functions.length" class="prompt-list">
          <li v-for="f in functions" :key="f.id" class="prompt-row">
            <div class="prompt-row__main">
              <div class="prompt-row__label">
                <TermBadge :variant="f.kind === 'preset_prompt' ? 'info' : 'warn'">{{ FUNCTION_KIND_LABEL[f.kind] || f.kind }}</TermBadge>
                {{ f.label }}
              </div>
              <div class="prompt-row__text">{{ f.config.text || f.config.template || '' }}</div>
            </div>
            <TermButton :disabled="fnBusy" label="刪除" @click="handleDeleteFunction(f)" />
          </li>
        </ul>
        <p v-else class="hint" style="margin-bottom: 8px;">尚未設定任何功能。</p>

        <div class="prompt-add">
          <TermField label="類型">
            <select v-model="newFn.kind" class="term-input">
              <option value="preset_prompt">預設提示詞 — 點清單填入輸入框</option>
              <option value="prompt_action">回應動作 — 對回覆套模板送出 ({content})</option>
            </select>
          </TermField>
          <TermField label="標籤">
            <input v-model="newFn.label" class="term-input" placeholder="撰寫週報 / 翻譯成英文" maxlength="120" />
          </TermField>
          <TermField :label="newFn.kind === 'preset_prompt' ? '提示詞文字' : '模板（{content} = 回覆內容）'">
            <textarea v-model="newFn.body" class="term-textarea" rows="3"
              :placeholder="newFn.kind === 'preset_prompt' ? '請幫我把以下工作項目整理成一份正式週報：' : '請把以下內容翻譯成英文：\n\n{content}'"></textarea>
          </TermField>
          <TermField v-if="newFn.kind === 'preset_prompt'" label="autosend (選填,直接送出而非填入)">
            <input type="checkbox" v-model="newFn.autosend" />
          </TermField>
          <div class="row-actions">
            <TermButton variant="primary" :disabled="fnBusy || !newFn.label.trim() || !newFn.body.trim()"
              :loading="fnBusy" label="新增功能" @click="handleAddFunction" />
          </div>
        </div>
      </TermBox>

      <TermBox title="操作" pad="md">
        <div class="row-actions">
          <TermButton @click="handleReload" label="從伺服器重新載入" />
        </div>
      </TermBox>

      <TermBox title="原始 JSON 預覽" pad="sm">
        <pre class="json-preview">{{ jsonPreview }}</pre>
      </TermBox>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { extractError } from '../api/errors'
import {
  getAgent,
  getAgentRuntimeConfig,
  listAgentFunctions,
  createAgentFunction,
  deleteAgentFunction,
} from '../api/agents'

const FUNCTION_KIND_LABEL = {
  preset_prompt: '預設提示詞',
  prompt_action: '回應動作',
}
import {
  TermBox, TermButton, TermBadge, TermField, TermSection,
} from '../components/cli'
import { useDialog } from '../composables/useDialog'

const { confirm } = useDialog()
const route = useRoute()
const router = useRouter()

const agentId = computed(() => Number(route.params.id))
const agent = ref(null)
const loading = ref(true)
const feedback = ref({ type: 'success', message: '' })
const initialConfig = ref(null)

const functions = ref([])
const fnError = ref('')
const fnBusy = ref(false)
const newFn = ref({ kind: 'preset_prompt', label: '', body: '', autosend: false })

async function loadFunctions() {
  try {
    const { data } = await listAgentFunctions(agentId.value)
    functions.value = Array.isArray(data) ? data : []
  } catch (e) {
    fnError.value = extractError(e, '載入功能失敗')
  }
}

function buildFunctionConfig(fn) {
  if (fn.kind === 'preset_prompt') return { text: fn.body, autosend: !!fn.autosend }
  if (fn.kind === 'prompt_action') return { template: fn.body }
  return {}
}

async function handleAddFunction() {
  const label = newFn.value.label.trim()
  const body = newFn.value.body.trim()
  if (!label || !body) return
  fnBusy.value = true
  fnError.value = ''
  try {
    await createAgentFunction(agentId.value, {
      kind: newFn.value.kind,
      label,
      config: buildFunctionConfig({ ...newFn.value, body }),
      sort_order: functions.value.length,
    })
    newFn.value = { kind: newFn.value.kind, label: '', body: '', autosend: false }
    await loadFunctions()
  } catch (e) {
    fnError.value = extractError(e, '新增失敗')
  } finally {
    fnBusy.value = false
  }
}

async function handleDeleteFunction(f) {
  const ok = await confirm({ title: '刪除功能', message: `確定刪除「${f.label}」？` })
  if (!ok) return
  fnBusy.value = true
  fnError.value = ''
  try {
    await deleteAgentFunction(agentId.value, f.id)
    await loadFunctions()
  } catch (e) {
    fnError.value = extractError(e, '刪除失敗')
  } finally {
    fnBusy.value = false
  }
}

const permsView = ref({
  allow_list_csv: '',
  deny_list_csv: '',
  ask_tools_csv: '',
  deny_tools_csv: '',
})

const wsView = ref({
  fs_read: null,
  fs_write: null,
  network: null,
  exec_bash: null,
  exec_python: null,
  max_exec_seconds: null,
  max_workspace_size_mb: null,
  command_allowlist_csv: '',
})

const guardrailsView = ref({
  input: [],
  output: [],
})

const hasOverride = computed(() => initialConfig.value != null)

const jsonPreview = computed(() => {
  if (initialConfig.value == null) return 'null  // 無覆寫'
  return JSON.stringify(initialConfig.value, null, 2)
})

function arrayToCsv(arr) {
  return Array.isArray(arr) ? arr.join(',') : ''
}

function fmtBool(v) {
  if (typeof v === 'boolean') return String(v)
  return '（預設）'
}

function fmtNum(v) {
  if (typeof v === 'number' && !Number.isNaN(v)) return String(v)
  return '（預設）'
}

function loadFromConfig(cfg) {
  initialConfig.value = cfg
  const perms = (cfg && cfg.tool_permissions) || {}
  permsView.value = {
    allow_list_csv: arrayToCsv(perms.allow_list),
    deny_list_csv: arrayToCsv(perms.deny_list),
    ask_tools_csv: arrayToCsv(perms.ask_tools),
    deny_tools_csv: arrayToCsv(perms.deny_tools),
  }
  const ws = (cfg && cfg.workspace) || {}
  wsView.value = {
    fs_read: typeof ws.fs_read === 'boolean' ? ws.fs_read : null,
    fs_write: typeof ws.fs_write === 'boolean' ? ws.fs_write : null,
    network: typeof ws.network === 'boolean' ? ws.network : null,
    exec_bash: typeof ws.exec_bash === 'boolean' ? ws.exec_bash : null,
    exec_python: typeof ws.exec_python === 'boolean' ? ws.exec_python : null,
    max_exec_seconds: typeof ws.max_exec_seconds === 'number' ? ws.max_exec_seconds : null,
    max_workspace_size_mb: typeof ws.max_workspace_size_mb === 'number' ? ws.max_workspace_size_mb : null,
    command_allowlist_csv: arrayToCsv(ws.command_allowlist),
  }
  const guards = (cfg && cfg.guardrails) || {}
  const toForm = (entry) => ({
    kind: entry.kind || 'regex_block',
    tool: entry.tool || '*',
    paramsRaw: JSON.stringify(stripMeta(entry)),
  })
  guardrailsView.value = {
    input: Array.isArray(guards.input) ? guards.input.map(toForm) : [],
    output: Array.isArray(guards.output) ? guards.output.map(toForm) : [],
  }
}

function stripMeta(entry) {
  const out = { ...entry }
  delete out.kind
  delete out.tool
  return out
}

async function load() {
  loading.value = true
  try {
    const [agentRow, cfgResp] = await Promise.all([
      getAgent(agentId.value),
      getAgentRuntimeConfig(agentId.value),
    ])
    agent.value = agentRow.data
    loadFromConfig(cfgResp.data?.runtime_config ?? null)
  } catch (e) {
    feedback.value = { type: 'error', message: `載入失敗：${extractError(e, e.message)}` }
  } finally {
    loading.value = false
  }
}

async function handleReload() {
  await load()
}

function goBack() {
  router.push({ name: 'DeveloperAgents' })
}

onMounted(async () => {
  await load()
  await loadFunctions()
})
</script>

<style scoped>
.retired-banner {
  padding: 10px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--c-warn, #b08900);
  background: var(--c-warn-soft, rgba(176, 137, 0, 0.12));
  color: var(--c-fg-1);
  font-size: 13px;
  line-height: 1.45;
  border-radius: var(--r-soft, 3px);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin-top: 8px;
}
.status-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin: 0;
}
.status-list dt {
  font-size: 11px;
  color: var(--c-fg-3);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.status-list dd {
  margin: 4px 0 0;
  font-size: 13px;
}
.guardrail-table {
  width: 100%;
  border-collapse: collapse;
  margin: 6px 0 12px;
  font-size: 12px;
}
.guardrail-table th, .guardrail-table td {
  padding: 4px 6px;
  text-align: left;
  border-bottom: 1px solid var(--c-border);
}
.guardrail-table th {
  font-weight: 500;
  color: var(--c-fg-3);
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.05em;
}
.mono {
  font-family: var(--font-mono);
  font-size: 11px;
}
.row-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.prompt-list {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.prompt-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.prompt-row__label {
  font-weight: 600;
  margin-bottom: 2px;
}
.prompt-row__text {
  font-size: var(--t-xs, 12px);
  color: var(--c-fg-mute);
  white-space: pre-wrap;
  word-break: break-word;
}
.prompt-add {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.json-preview {
  margin: 0;
  padding: 8px 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--c-surface-2);
  color: var(--c-fg-1);
  border-radius: var(--r-soft);
  white-space: pre-wrap;
  max-height: 300px;
  overflow: auto;
}
.feedback {
  padding: 8px 10px;
  margin: 8px 0;
  border-radius: 3px;
  font-size: 12px;
  display: flex;
  gap: 6px;
  align-items: center;
}
.feedback.is-ok {
  background: var(--c-ok-soft);
  color: var(--c-ok);
}
.feedback.is-err {
  background: var(--c-danger-soft);
  color: var(--c-danger);
}
</style>
