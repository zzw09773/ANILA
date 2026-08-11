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
  groupIntoSections,
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
  ['proxy.llm_timeout', 'LLM_TIMEOUT'],
  ['proxy.embedding_timeout', 'EMBEDDING_TIMEOUT'],
  ['auth.access_token_expire_minutes', 'ACCESS_TOKEN_EXPIRE_MINUTES'],
  ['auth.refresh_token_expire_days', 'REFRESH_TOKEN_EXPIRE_DAYS'],
  ['limits.department_max_depth', 'ANILA_DEPARTMENT_MAX_DEPTH'],
  ['limits.action_invoke_per_min', 'ANILA_ACTION_INVOKE_PER_MIN'],
  ['limits.attachment_budget_ratio', 'ANILA_ATTACHMENT_BUDGET_RATIO'],
  ['intl.zh_normalize', 'ANILA_ZH_NORMALIZE'],
  ['intl.query_expansion', 'ANILA_QUERY_EXPANSION'],
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

test('registry and UI contract contain exactly the twelve C settings', () => {
  assert.equal((registrySource.match(/^    _spec\(/gm) ?? []).length, 12)
  assert.equal(SECTION_DEFS.length, 1)
  assert.deepEqual(SECTION_DEFS[0].classes, ['C'])
  assert.equal(SECTION_DEFS[0].editable, true)
  for (const [key, env] of EXPECTED) {
    const keyPattern = key === 'institutional_kb.score_threshold'
      ? /KB_THRESHOLD_KEY/u
      : new RegExp(key.replaceAll('.', '\\.'), 'u')
    assert.match(registrySource, keyPattern)
    if (env) assert.match(registrySource, new RegExp(`"${env}"`, 'u'))
  }
})

test('all rows stay in one editable section and no row is silently dropped', () => {
  const rows = EXPECTED.map(([key], index) => row(key, index))
  const sections = groupIntoSections(rows)
  assert.deepEqual(sections.map((section) => section.id), ['apply-now'])
  assert.deepEqual(sections[0].items.map((item) => item.key), EXPECTED.map(([key]) => key))
  assert.equal(sectionIdFor(rows[0]), 'apply-now')
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
