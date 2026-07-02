<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">執行設定 · {{ agent?.name || agentId }}</h1>
        <p class="page-head__sub">
          各 agent 的工具權限 · 工作區上限 · 護欄 — 透過 30 秒輪詢即時套用，無需重啟。
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
        <dl class="status-list">
          <div>
            <dt>覆寫</dt>
            <dd>
              <TermBadge :variant="hasOverride ? 'accent' : ''">
                {{ hasOverride ? '管理員設定' : '程式碼預設' }}
              </TermBadge>
            </dd>
          </div>
          <div v-if="lastSavedAt">
            <dt>最後儲存</dt>
            <dd class="cell-meta tnum">{{ lastSavedAt }}</dd>
          </div>
          <div>
            <dt>輪詢頻率</dt>
            <dd class="cell-meta">agent 程序約每 30 秒</dd>
          </div>
        </dl>
        <p class="cell-meta" style="margin-top: 8px;">
          設為 <code>None</code> 會清除覆寫（agent 回到編譯內建預設）。
          設為 <code>{}</code> 代表「明確為空」— 語意不同。
        </p>
      </TermBox>

      <TermBox title="工具權限" pad="md">
        <p class="cell-meta">
          <code>allow_list</code> + <code>deny_list</code> 由工具 router 判斷；
          <code>ask_tools</code> 把該工具的旗標切為 ASK（中斷等使用者核准）；
          <code>deny_tools</code> 直接拒絕。
        </p>
        <div class="grid">
          <TermField label="allow_list（逗號分隔 · '*' = 全部）">
            <input v-model="permsForm.allow_list_csv" class="term-input" placeholder="*" />
          </TermField>
          <TermField label="deny_list">
            <input v-model="permsForm.deny_list_csv" class="term-input" placeholder="exec_bash" />
          </TermField>
          <TermField label="ask_tools">
            <input v-model="permsForm.ask_tools_csv" class="term-input" placeholder="exec_python,apply_patch" />
          </TermField>
          <TermField label="deny_tools">
            <input v-model="permsForm.deny_tools_csv" class="term-input" placeholder="file_write" />
          </TermField>
        </div>
      </TermBox>

      <TermBox title="工作區上限" pad="md">
        <p class="cell-meta">
          上限會疊加在 agent 的編譯內建預設上。欄位留空即沿用預設；否則覆寫。
        </p>
        <div class="grid">
          <TermField label="fs_read">
            <select v-model="wsForm.fs_read" class="term-select">
              <option :value="null">（預設）</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="fs_write">
            <select v-model="wsForm.fs_write" class="term-select">
              <option :value="null">（預設）</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="network">
            <select v-model="wsForm.network" class="term-select">
              <option :value="null">（預設）</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="exec_bash">
            <select v-model="wsForm.exec_bash" class="term-select">
              <option :value="null">（預設）</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="exec_python">
            <select v-model="wsForm.exec_python" class="term-select">
              <option :value="null">（預設）</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="max_exec_seconds">
            <input v-model.number="wsForm.max_exec_seconds" class="term-input" type="number" min="1" placeholder="（預設 30）" />
          </TermField>
          <TermField label="max_workspace_size_mb">
            <input v-model.number="wsForm.max_workspace_size_mb" class="term-input" type="number" min="1" placeholder="（預設 100）" />
          </TermField>
          <TermField label="command_allowlist（逗號分隔）">
            <input v-model="wsForm.command_allowlist_csv" class="term-input" placeholder="ls,cat,grep" />
          </TermField>
        </div>
      </TermBox>

      <TermBox title="護欄" pad="md">
        <p class="cell-meta">
          輸入護欄檢查工具輸入 dict（regex_block reject/redact）。
          輸出護欄檢查工具結果文字（regex_block reject/redact、max_length 截斷）。
          <code>tool='*'</code> 套用到所有已註冊工具；指定名稱可限縮範圍。
        </p>

        <TermSection title="輸入護欄" />
        <table class="guardrail-table">
          <thead>
            <tr>
              <th>kind</th>
              <th>tool</th>
              <th>params</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(g, idx) in guardrailsForm.input" :key="`in-${idx}`">
              <td>
                <select v-model="g.kind" class="term-select">
                  <option value="regex_block">regex_block</option>
                </select>
              </td>
              <td>
                <input v-model="g.tool" class="term-input" placeholder="*" />
              </td>
              <td>
                <input v-model="g.paramsRaw" class="term-input mono" placeholder='{"pattern":"sk-\\w+","mode":"reject"}' />
              </td>
              <td>
                <button class="term-action danger" @click="removeGuard('input', idx)">移除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <button class="term-action" @click="addGuard('input')">+ 輸入護欄</button>

        <TermSection title="輸出護欄" />
        <table class="guardrail-table">
          <thead>
            <tr>
              <th>kind</th>
              <th>tool</th>
              <th>params</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(g, idx) in guardrailsForm.output" :key="`out-${idx}`">
              <td>
                <select v-model="g.kind" class="term-select">
                  <option value="regex_block">regex_block</option>
                  <option value="max_length">max_length</option>
                </select>
              </td>
              <td>
                <input v-model="g.tool" class="term-input" placeholder="*" />
              </td>
              <td>
                <input v-model="g.paramsRaw" class="term-input mono" :placeholder='outputPlaceholder(g.kind)' />
              </td>
              <td>
                <button class="term-action danger" @click="removeGuard('output', idx)">移除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <button class="term-action" @click="addGuard('output')">+ 輸出護欄</button>
      </TermBox>

      <TermBox title="功能" pad="md">
        <p class="hint" style="margin-bottom: 8px;">
          功能 — 在 ANILA 對話介面跟此 agent 對話時提供給使用者。純宣告式設定，
          不執行任何程式碼。新增功能類型(kind)由前端 renderer 決定，未來可擴充。
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
        <div v-if="parseError" class="feedback is-err" style="margin-bottom: 8px;">
          <span>!</span><span>{{ parseError }}</span>
        </div>
        <div class="row-actions">
          <TermButton variant="primary" :disabled="saving" :loading="saving" @click="handleSave"
            :label="saving ? '儲存中…' : '儲存執行設定'" />
          <TermButton @click="handleClear" :disabled="saving" label="清除覆寫（回到預設）" />
          <TermButton @click="handleReload" :disabled="saving" label="從伺服器重新載入" />
        </div>
      </TermBox>

      <TermBox title="原始 JSON 預覽" pad="sm">
        <pre class="json-preview">{{ buildPreview() }}</pre>
      </TermBox>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  getAgent,
  getAgentRuntimeConfig,
  setAgentRuntimeConfig,
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
const saving = ref(false)
const parseError = ref('')
const feedback = ref({ type: 'success', message: '' })
const lastSavedAt = ref('')
const initialConfig = ref(null) // raw object as last fetched

// Per-agent functions (2026-06-11, extensible)
const functions = ref([])
const fnError = ref('')
const fnBusy = ref(false)
const newFn = ref({ kind: 'preset_prompt', label: '', body: '', autosend: false })

async function loadFunctions() {
  try {
    const { data } = await listAgentFunctions(agentId.value)
    functions.value = Array.isArray(data) ? data : []
  } catch (e) {
    fnError.value = e?.response?.data?.detail || '載入功能失敗'
  }
}

// 把 kind + body/autosend 組成後端的 config。新增 kind 時在這裡擴充。
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
    fnError.value = e?.response?.data?.detail || '新增失敗'
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
    fnError.value = e?.response?.data?.detail || '刪除失敗'
  } finally {
    fnBusy.value = false
  }
}

const permsForm = ref({
  allow_list_csv: '',
  deny_list_csv: '',
  ask_tools_csv: '',
  deny_tools_csv: '',
})

const wsForm = ref({
  fs_read: null,
  fs_write: null,
  network: null,
  exec_bash: null,
  exec_python: null,
  max_exec_seconds: null,
  max_workspace_size_mb: null,
  command_allowlist_csv: '',
})

const guardrailsForm = ref({
  input: [],
  output: [],
})

const hasOverride = computed(() => initialConfig.value != null)

function csvToArray(csv) {
  if (!csv) return []
  return csv.split(',').map(s => s.trim()).filter(Boolean)
}

function arrayToCsv(arr) {
  return Array.isArray(arr) ? arr.join(',') : ''
}

function outputPlaceholder(kind) {
  if (kind === 'max_length') return '{"max_chars":4096}'
  return '{"pattern":"secret","mode":"redact"}'
}

function loadFromConfig(cfg) {
  initialConfig.value = cfg
  const perms = (cfg && cfg.tool_permissions) || {}
  permsForm.value = {
    allow_list_csv: arrayToCsv(perms.allow_list),
    deny_list_csv: arrayToCsv(perms.deny_list),
    ask_tools_csv: arrayToCsv(perms.ask_tools),
    deny_tools_csv: arrayToCsv(perms.deny_tools),
  }
  const ws = (cfg && cfg.workspace) || {}
  wsForm.value = {
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
  guardrailsForm.value = {
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

function addGuard(side) {
  const blank = { kind: side === 'input' ? 'regex_block' : 'max_length', tool: '*', paramsRaw: '' }
  guardrailsForm.value[side].push(blank)
}

function removeGuard(side, idx) {
  guardrailsForm.value[side].splice(idx, 1)
}

function buildConfig() {
  parseError.value = ''
  const out = {}
  const allow = csvToArray(permsForm.value.allow_list_csv)
  const deny = csvToArray(permsForm.value.deny_list_csv)
  const ask = csvToArray(permsForm.value.ask_tools_csv)
  const denyTools = csvToArray(permsForm.value.deny_tools_csv)
  if (allow.length || deny.length || ask.length || denyTools.length) {
    out.tool_permissions = {}
    if (allow.length) out.tool_permissions.allow_list = allow
    if (deny.length) out.tool_permissions.deny_list = deny
    if (ask.length) out.tool_permissions.ask_tools = ask
    if (denyTools.length) out.tool_permissions.deny_tools = denyTools
  }

  const ws = {}
  for (const key of ['fs_read', 'fs_write', 'network', 'exec_bash', 'exec_python']) {
    if (wsForm.value[key] !== null && wsForm.value[key] !== undefined) ws[key] = wsForm.value[key]
  }
  for (const key of ['max_exec_seconds', 'max_workspace_size_mb']) {
    const v = wsForm.value[key]
    if (typeof v === 'number' && !Number.isNaN(v)) ws[key] = v
  }
  const cmds = csvToArray(wsForm.value.command_allowlist_csv)
  if (cmds.length) ws.command_allowlist = cmds
  if (Object.keys(ws).length) out.workspace = ws

  const buildGuards = (side) => {
    const list = []
    for (const g of guardrailsForm.value[side]) {
      let params = {}
      if (g.paramsRaw && g.paramsRaw.trim()) {
        try {
          params = JSON.parse(g.paramsRaw)
          if (typeof params !== 'object' || Array.isArray(params)) {
            throw new Error('params must be a JSON object')
          }
        } catch (e) {
          parseError.value = `${side} 護欄 #${list.length + 1} 的 JSON 無效：${e.message}`
          throw e
        }
      }
      list.push({ kind: g.kind, tool: g.tool || '*', ...params })
    }
    return list
  }
  let inputs, outputs
  try {
    inputs = buildGuards('input')
    outputs = buildGuards('output')
  } catch {
    return null
  }
  if (inputs.length || outputs.length) {
    out.guardrails = {}
    if (inputs.length) out.guardrails.input = inputs
    if (outputs.length) out.guardrails.output = outputs
  }
  return out
}

function buildPreview() {
  try {
    const cfg = buildConfig()
    if (cfg === null) return '（無效 — 請修正上方錯誤）'
    if (Object.keys(cfg).length === 0) return '{}  // 空 — 明確「無覆寫」語意'
    return JSON.stringify(cfg, null, 2)
  } catch {
    return '（無效）'
  }
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
    feedback.value = { type: 'error', message: `載入失敗：${e.response?.data?.detail || e.message}` }
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  const cfg = buildConfig()
  if (cfg === null) return
  saving.value = true
  feedback.value = { type: 'success', message: '' }
  try {
    const resp = await setAgentRuntimeConfig(agentId.value, cfg)
    initialConfig.value = resp.data?.runtime_config ?? cfg
    lastSavedAt.value = new Date().toLocaleTimeString()
    feedback.value = { type: 'success', message: '已儲存 · agent 約 30 秒內生效' }
  } catch (e) {
    feedback.value = { type: 'error', message: e.response?.data?.detail || e.message }
  } finally {
    saving.value = false
  }
}

async function handleClear() {
  if (!(await confirm({ message: '清除 runtime_config 覆寫？agent 會回到編譯內建預設。', confirmText: '清除', danger: true }))) return
  saving.value = true
  feedback.value = { type: 'success', message: '' }
  try {
    await setAgentRuntimeConfig(agentId.value, null)
    initialConfig.value = null
    loadFromConfig(null)
    lastSavedAt.value = new Date().toLocaleTimeString()
    feedback.value = { type: 'success', message: '已清除 · agent 約 30 秒內回到預設' }
  } catch (e) {
    feedback.value = { type: 'error', message: e.response?.data?.detail || e.message }
  } finally {
    saving.value = false
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
.term-input.mono {
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
.term-action {
  background: transparent;
  border: 1px solid var(--c-border);
  color: var(--c-fg-1);
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 3px;
  cursor: pointer;
}
.term-action.danger {
  color: var(--c-danger);
}
.term-action:hover {
  background: var(--c-surface-2);
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
