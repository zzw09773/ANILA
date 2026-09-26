import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  SECTION_DEFS,
  UNKNOWN_SECTION_ID,
  canEdit,
  countMismatchWarning,
  draftValue,
  formatSettingValue,
  applyWhenLabel,
  groupIntoSections,
  isAtDefault,
  isTextSetting,
  settingUnit,
  textPreview,
  overviewState,
  replaceRow,
  saveNotice,
  sectionIdFor,
  sourceLabel,
  valueCells,
} from '../src/utils/settingsView.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '../../..')
const registrySource = readFileSync(
  resolve(REPO_ROOT, 'services/csp/app/services/settings_registry.py'),
  'utf8',
)
const viewSource = readFileSync(
  resolve(REPO_ROOT, 'apps/csp-governance-ui/src/views/SettingsOverviewView.vue'),
  'utf8',
)

const EXPECTED = [
  ['institutional_kb.score_threshold', null],
  ['memory.retrieve_min_cosine', 'MEMORY_RETRIEVE_MIN_COSINE'],
  ['memory.retrieve_top_k', 'MEMORY_RETRIEVE_TOP_K'],
  ['memory.enabled', 'MEMORY_ENABLED'],
  ['memory.idle_minutes', 'MEMORY_IDLE_MINUTES'],
  ['proxy.llm_timeout', 'LLM_TIMEOUT'],
  ['proxy.embedding_timeout', 'EMBEDDING_TIMEOUT'],
  ['auth.access_token_expire_minutes', 'ACCESS_TOKEN_EXPIRE_MINUTES'],
  ['auth.refresh_token_expire_days', 'REFRESH_TOKEN_EXPIRE_DAYS'],
  ['auth.jwt_rotation_days', 'JWT_ROTATION_DAYS'],
  ['limits.department_max_depth', 'ANILA_DEPARTMENT_MAX_DEPTH'],
  ['limits.action_invoke_per_min', 'ANILA_ACTION_INVOKE_PER_MIN'],
  ['limits.attachment_budget_ratio', 'ANILA_ATTACHMENT_BUDGET_RATIO'],
  ['intl.zh_normalize', 'ANILA_ZH_NORMALIZE'],
  ['intl.query_expansion', 'ANILA_QUERY_EXPANSION'],
  // Router 三份 system prompt（擁有者 2026-08-22 裁定進治理中心，2026-09-02 落地）
  ['router.prompt.system', null],
  ['router.prompt.plain', null],
  ['router.prompt.forced', null],
  // 多輪上限與每次提問的模型呼叫預算（2026-09-26）
  ['limits.router_round_cap', null],
  ['limits.router_model_call_budget', null],
]

function row(key, index = 0, overrides = {}) {
  return {
    key,
    class: 'C',
    description: `${key} 說明`,
    env_name: EXPECTED.find(([candidate]) => candidate === key)?.[1] ?? null,
    value_type: 'int',
    editable: true,
    default: index,
    effective: index + 10,
    stored: String(index + 10),
    stored_usable: true,
    source: 'db',
    updated_at: null,
    updated_by: null,
    ...overrides,
  }
}

test('registry and UI contract contain exactly the twenty C settings', () => {
  assert.equal((registrySource.match(/^    _spec\(/gm) ?? []).length, 20)
  assert.equal(SECTION_DEFS.length, 3)
  assert.ok(SECTION_DEFS.every((s) => s.classes.includes('C')))
  assert.ok(SECTION_DEFS.every((s) => s.editable === true))
  for (const [key, env] of EXPECTED) {
    // keys that the registry spells as a constant, not a literal
    const CONSTANT_KEYS = {
      'institutional_kb.score_threshold': /KB_THRESHOLD_KEY/u,
      'router.prompt.system': /_router_prompts\.KEY_SYSTEM/u,
      'router.prompt.plain': /_router_prompts\.KEY_PLAIN/u,
      'router.prompt.forced': /_router_prompts\.KEY_FORCED/u,
      'limits.router_round_cap': /_router_prompts\.KEY_ROUND_CAP/u,
      'limits.router_model_call_budget': /_router_prompts\.KEY_CALL_BUDGET/u,
    }
    const keyPattern = CONSTANT_KEYS[key] ?? new RegExp(key.replaceAll('.', '\\.'), 'u')
    assert.match(registrySource, keyPattern)
    if (env) assert.match(registrySource, new RegExp(`"${env}"`, 'u'))
  }
})

test('all rows stay in one editable section and no row is silently dropped', () => {
  const rows = EXPECTED.map(([key], index) => row(key, index))
  const sections = groupIntoSections(rows)
  assert.deepEqual(sections.map((section) => section.id), ['models', 'account', 'conversation'])
  const shown = sections.flatMap((section) => section.items.map((item) => item.key))
  assert.deepEqual(shown.sort(), EXPECTED.map(([key]) => key).sort())
  assert.equal(sectionIdFor(rows[0]), 'models')
  assert.equal(canEdit(rows[0]), true)
})

test('unknown classes are conservative and visible in an overflow section', () => {
  const unknown = row('future.key', 0, { class: 'future', editable: true })
  const sections = groupIntoSections([unknown])
  assert.equal(sections.at(-1).id, UNKNOWN_SECTION_ID)
  assert.equal(canEdit(unknown), false)
  assert.equal(sections.at(-1).editable, false)
})

test('value rendering shows effective, stored, and source without inventing restart state', () => {
  const item = row('proxy.llm_timeout', 1)
  assert.deepEqual(valueCells(item).map((cell) => cell.field), ['effective', 'stored', 'source'])
  assert.equal(formatSettingValue(''), '（空字串）')
  assert.equal(sourceLabel('env'), 'env／compose')
  assert.equal(draftValue(item), '11')
  assert.deepEqual(saveNotice(item), { tone: 'ok', message: '已儲存，下一個請求就生效' })
})

test('overview state and count mismatch expose incomplete backend payloads', () => {
  assert.equal(overviewState({ loaded: false, error: null, items: [] }), 'loading')
  assert.equal(overviewState({ loaded: true, error: new Error('x'), items: [] }), 'failed')
  assert.equal(overviewState({ loaded: true, error: null, items: [] }), 'empty')
  assert.equal(countMismatchWarning({ total: 12, items: [row('proxy.llm_timeout')] }), '後端說有 12 顆設定，這一頁只收到 1 顆 —— 下面不是全部。')
})

test('save replaces the complete row returned by the backend', () => {
  const oldRow = row('proxy.llm_timeout', 1)
  const newRow = row('proxy.llm_timeout', 99, { effective: 300, stored: '300' })
  assert.deepEqual(replaceRow([oldRow], newRow), [newRow])
})

test('Vue view has no obsolete regions or deferred-application vocabulary', () => {
  for (const token of ['boot_override', 'pending', 'restart', 'locked_reason', 'readonly', 'B_EDIT', 'B_LOCKED']) {
    assert.equal(viewSource.toLowerCase().includes(token.toLowerCase()), false, `stale UI token: ${token}`)
  }
})

test('jwt rotation days lands in the account section and is not a token lifetime', () => {
  const item = row('auth.jwt_rotation_days')
  assert.equal(sectionIdFor(item), 'account')
  assert.equal(applyWhenLabel(item), '下一次金鑰排程檢查就生效')
  assert.equal(settingUnit(item), '天')
  assert.equal(applyWhenLabel(row('auth.access_token_expire_minutes')), '下次簽發權杖時生效')
})

test('emergency jwt rotation explains logout and in-flight dispatch failure', () => {
  const phrase = '所有人會被登出，進行中的派工權杖會失效'
  const api = readFileSync(resolve(HERE, '../src/api/jwtKeyring.js'), 'utf8')
  assert.ok(viewSource.includes(phrase))
  assert.ok(api.includes(phrase))
  assert.ok(api.includes('/api/auth/jwt-keyring/emergency-rotation'))
  assert.ok(viewSource.includes('緊急輪替簽章金鑰'))
})

test('text settings (router prompts) get a preview cell, a textarea and a reset-to-default', () => {
  const long = 'A'.repeat(200) + '\n' + 'B'.repeat(10)
  const item = row('router.prompt.plain', 0, { value_type: 'text', default: long, effective: long, stored: null, source: 'default' })
  assert.equal(isTextSetting(item), true)
  assert.equal(isTextSetting(row('proxy.llm_timeout')), false)
  const effective = valueCells(item).find((cell) => cell.field === 'effective')
  assert.ok(effective.text.endsWith('（共 211 字）'), effective.text)
  assert.ok(effective.text.length < 200, 'preview must be truncated, not the whole prompt')
  assert.equal(textPreview(null), '—')
  assert.equal(isAtDefault(item), true)
  assert.equal(isAtDefault({ ...item, effective: 'edited' }), false)
  // the view wires the pieces: textarea for text settings, one reset button, the 30-second promise
  assert.match(viewSource, /<textarea[\s\S]*v-model="drafts\[item\.key\]"/u)
  assert.match(viewSource, /重設為出貨預設/u)
  assert.match(readFileSync(resolve(HERE, '../src/utils/settingsView.js'), 'utf8'), /30 秒內/u)
  assert.match(viewSource, /handleResetToDefault/u)
})
