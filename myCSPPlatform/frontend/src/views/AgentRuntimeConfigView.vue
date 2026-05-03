<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">developer · console</p>
        <h1 class="page-head__title">runtime config · {{ agent?.name || agentId }}</h1>
        <p class="page-head__sub">
          per-agent tool permissions · workspace caps · guardrails — live-applied via 30s poll, no restart needed.
        </p>
      </div>
      <div class="page-head__actions">
        <TermButton @click="goBack" label="← agents" />
      </div>
    </header>

    <div v-if="feedback.message" class="feedback" :class="feedback.type === 'error' ? 'is-err' : 'is-ok'">
      <span>{{ feedback.type === 'error' ? '!' : '✓' }}</span>
      <span>{{ feedback.message }}</span>
    </div>

    <div v-if="loading" class="cell-meta" style="padding: 12px 0;">loading…</div>

    <template v-else>
      <TermBox title="status" pad="md">
        <dl class="status-list">
          <div>
            <dt>override</dt>
            <dd>
              <TermBadge :variant="hasOverride ? 'accent' : ''">
                {{ hasOverride ? 'admin-set' : 'code defaults' }}
              </TermBadge>
            </dd>
          </div>
          <div v-if="lastSavedAt">
            <dt>last saved</dt>
            <dd class="cell-meta tnum">{{ lastSavedAt }}</dd>
          </div>
          <div>
            <dt>poll cadence</dt>
            <dd class="cell-meta">≈ 30s on the agent process</dd>
          </div>
        </dl>
        <p class="cell-meta" style="margin-top: 8px;">
          Setting <code>None</code> clears the override (agent reverts to compiled-in defaults).
          Setting <code>{}</code> means "explicit empty" — different semantics.
        </p>
      </TermBox>

      <TermBox title="tool permissions" pad="md">
        <p class="cell-meta">
          <code>allow_list</code> + <code>deny_list</code> are evaluated by the
          tool router; <code>ask_tools</code> flips the per-tool flag to ASK
          (user approval interrupt); <code>deny_tools</code> hard-denies.
        </p>
        <div class="grid">
          <TermField label="allow_list (comma-separated · '*' = all)">
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

      <TermBox title="workspace caps" pad="md">
        <p class="cell-meta">
          Caps overlay on the agent's compiled-in defaults. Leave a field empty
          to keep the default; otherwise it overrides.
        </p>
        <div class="grid">
          <TermField label="fs_read">
            <select v-model="wsForm.fs_read" class="term-select">
              <option :value="null">(default)</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="fs_write">
            <select v-model="wsForm.fs_write" class="term-select">
              <option :value="null">(default)</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="network">
            <select v-model="wsForm.network" class="term-select">
              <option :value="null">(default)</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="exec_bash">
            <select v-model="wsForm.exec_bash" class="term-select">
              <option :value="null">(default)</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="exec_python">
            <select v-model="wsForm.exec_python" class="term-select">
              <option :value="null">(default)</option>
              <option :value="true">true</option>
              <option :value="false">false</option>
            </select>
          </TermField>
          <TermField label="max_exec_seconds">
            <input v-model.number="wsForm.max_exec_seconds" class="term-input" type="number" min="1" placeholder="(default 30)" />
          </TermField>
          <TermField label="max_workspace_size_mb">
            <input v-model.number="wsForm.max_workspace_size_mb" class="term-input" type="number" min="1" placeholder="(default 100)" />
          </TermField>
          <TermField label="command_allowlist (comma-separated)">
            <input v-model="wsForm.command_allowlist_csv" class="term-input" placeholder="ls,cat,grep" />
          </TermField>
        </div>
      </TermBox>

      <TermBox title="guardrails" pad="md">
        <p class="cell-meta">
          Input guardrails inspect tool input dicts (regex_block reject/redact).
          Output guardrails inspect tool result text (regex_block reject/redact,
          max_length truncation). <code>tool='*'</code> applies to every
          registered tool; specify a name to scope it.
        </p>

        <TermSection title="input guardrails" />
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
                <button class="term-action danger" @click="removeGuard('input', idx)">remove</button>
              </td>
            </tr>
          </tbody>
        </table>
        <button class="term-action" @click="addGuard('input')">+ input guardrail</button>

        <TermSection title="output guardrails" />
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
                <button class="term-action danger" @click="removeGuard('output', idx)">remove</button>
              </td>
            </tr>
          </tbody>
        </table>
        <button class="term-action" @click="addGuard('output')">+ output guardrail</button>
      </TermBox>

      <TermBox title="actions" pad="md">
        <div v-if="parseError" class="feedback is-err" style="margin-bottom: 8px;">
          <span>!</span><span>{{ parseError }}</span>
        </div>
        <div class="row-actions">
          <TermButton variant="primary" :disabled="saving" :loading="saving" @click="handleSave"
            :label="saving ? 'saving…' : 'save runtime config'" />
          <TermButton @click="handleClear" :disabled="saving" label="clear override (revert to defaults)" />
          <TermButton @click="handleReload" :disabled="saving" label="reload from server" />
        </div>
      </TermBox>

      <TermBox title="raw JSON preview" pad="sm">
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
} from '../api/agents'
import {
  TermBox, TermButton, TermBadge, TermField, TermSection,
} from '../components/cli'

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
          parseError.value = `invalid JSON in ${side} guardrail #${list.length + 1}: ${e.message}`
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
    if (cfg === null) return '(invalid — fix errors above)'
    if (Object.keys(cfg).length === 0) return '{}  // empty — explicit "no overrides" semantics'
    return JSON.stringify(cfg, null, 2)
  } catch {
    return '(invalid)'
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
    feedback.value = { type: 'error', message: `failed to load: ${e.response?.data?.detail || e.message}` }
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
    feedback.value = { type: 'success', message: 'saved · agent will pick up within ~30s' }
  } catch (e) {
    feedback.value = { type: 'error', message: e.response?.data?.detail || e.message }
  } finally {
    saving.value = false
  }
}

async function handleClear() {
  if (!confirm('Clear runtime_config override? Agent will revert to compiled-in defaults.')) return
  saving.value = true
  feedback.value = { type: 'success', message: '' }
  try {
    await setAgentRuntimeConfig(agentId.value, null)
    initialConfig.value = null
    loadFromConfig(null)
    lastSavedAt.value = new Date().toLocaleTimeString()
    feedback.value = { type: 'success', message: 'cleared · agent reverts to defaults within ~30s' }
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

onMounted(load)
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
  color: var(--cli-fg-muted, #8b94a8);
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
  border-bottom: 1px solid var(--cli-border, #2a2f3a);
}
.guardrail-table th {
  font-weight: 500;
  color: var(--cli-fg-muted, #8b94a8);
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.05em;
}
.term-input.mono {
  font-family: var(--cli-font-mono, monospace);
  font-size: 11px;
}
.row-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.term-action {
  background: transparent;
  border: 1px solid var(--cli-border, #2a2f3a);
  color: var(--cli-fg, #e6edf3);
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 3px;
  cursor: pointer;
}
.term-action.danger {
  color: var(--cli-danger, #f85149);
}
.term-action:hover {
  background: var(--cli-bg-subtle, #2a2f3a);
}
.json-preview {
  margin: 0;
  padding: 8px 10px;
  font-family: var(--cli-font-mono, monospace);
  font-size: 11px;
  background: var(--cli-bg, #0e1116);
  color: var(--cli-fg, #e6edf3);
  border-radius: 3px;
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
  background: rgba(46, 160, 67, 0.1);
  color: var(--cli-success, #2ea043);
}
.feedback.is-err {
  background: rgba(248, 81, 73, 0.1);
  color: var(--cli-danger, #f85149);
}
</style>
