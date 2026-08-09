// 設定總覽頁 —— 四區、三態，而且一句話都不比後端多講。
//
// 這個 app 沒有 vitest／@testing-library（package.json 的 test 是
// `node --test tests/*.test.mjs`），掛載不了元件。所以照 platformEmbedding／
// anilaSearchableToggle 已確立的兩層作法：
//   1. 行為層：分區／分態／字串全抽進 `utils/settingsView.js`，直接 import 斷言。
//   2. 原始碼層護欄：真正會壞的是呼叫端 —— 純函式全綠而 .vue 忘了接（或自己
//      重寫一份判斷），行為測試一個都不會紅。唯讀區的「沒有 save handler」
//      沿用 runtimeConfigReadOnly.test.mjs 的 regex 護欄型式。
//
// 96 列的 fixture **不是手抄的**：(key, class, env, restart_required) 直接對著
// `services/csp/app/services/settings_registry.py` 的原始碼核對（見「契約漂移」
// 那一組）。後端加第 97 顆而前端沒跟上，這裡會紅。
//
// 測試值刻意避開場上每一個預設（帳本第三度前科）：811 起跳的整數、0.77、
// 以及帶識別字的字串，登錄表與 config.py 裡一個都沒有。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  RESTART_HINT,
  SAVED_NOW_HINT,
  SECTION_DEFS,
  UNKNOWN_SECTION_ID,
  bootOverrideBanner,
  canEdit,
  countMismatchWarning,
  draftValue,
  extractDetail,
  formatSettingValue,
  groupIntoSections,
  isSetLabel,
  lockedReasonText,
  overviewState,
  overviewStateMessage,
  replaceRow,
  rowState,
  saveNotice,
  sectionIdFor,
  sourceLabel,
  valueCells,
} from '../src/utils/settingsView.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '../../..')
const REGISTRY_PATH = resolve(REPO_ROOT, 'services/csp/app/services/settings_registry.py')

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

/** 去掉 `//` 與 `<!-- -->` 註解 —— 註解裡提到的字不該讓 guard 假綠。 */
function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

// ── 96 顆的真實 (key, class, env_name, restart_required, 有無 locked_reason) ──
// 全部是登錄表裡真的存在的 key；每一個 class 都遠超過「至少兩顆真 key」。
const REGISTRY_96 = [
  ['app.name', 'APP_NAME', 'B_EDIT', true, false],
  ['app.version', 'APP_VERSION', 'B_EDIT', true, false],
  ['app.debug', 'DEBUG', 'B_LOCKED', true, true],
  ['app.site_url', 'SITE_URL', 'B_EDIT', true, false],
  ['app.static_dir', 'STATIC_DIR', 'B_LOCKED', true, true],
  ['app.host', 'ANILA_HOST', 'B_LOCKED', true, true],
  ['app.python_unbuffered', 'PYTHONUNBUFFERED', 'B_LOCKED', true, true],
  ['db.url', 'DATABASE_URL', 'A', true, true],
  ['db.migration_url', 'MIGRATION_DATABASE_URL', 'A', true, true],
  ['db.app_role_password', 'CSP_APP_DB_PASSWORD', 'A', true, true],
  ['db.legacy_sqlite_path', 'LEGACY_SQLITE_PATH', 'B_LOCKED', true, true],
  ['auth.secret_key', 'SECRET_KEY', 'A', true, true],
  ['auth.secret_key_fallback', 'CSP_SECRET_KEY', 'A', false, true],
  ['auth.jwt_algorithm', 'ALGORITHM', 'SEC', true, true],
  ['auth.access_token_expire_minutes', 'ACCESS_TOKEN_EXPIRE_MINUTES', 'SEC', true, true],
  ['auth.refresh_token_expire_days', 'REFRESH_TOKEN_EXPIRE_DAYS', 'SEC', true, true],
  ['auth.jwt_private_key_path', 'JWT_PRIVATE_KEY_PATH', 'SEC', true, true],
  ['auth.jwt_public_key_path', 'JWT_PUBLIC_KEY_PATH', 'SEC', true, true],
  ['auth.jwt_kid', 'JWT_KID', 'SEC', true, true],
  ['auth.allow_auto_keygen', 'ALLOW_AUTO_KEYGEN', 'SEC', true, true],
  ['auth.cookie_secure', 'COOKIE_SECURE', 'SEC', true, true],
  ['auth.allow_dev_secret', 'ANILA_ALLOW_DEV_SECRET', 'SEC', true, true],
  ['admin.username', 'ADMIN_USERNAME', 'B_EDIT', true, false],
  ['admin.password', 'ADMIN_PASSWORD', 'A', true, true],
  ['card.enabled', 'ENABLE_CARD_LOGIN', 'SEC', true, true],
  ['card.require_card_only', 'REQUIRE_CARD_LOGIN_ONLY', 'SEC', true, true],
  ['card.initial_owners', 'CARD_INITIAL_OWNERS', 'SEC', true, true],
  ['card.ca_bundle_path', 'CARD_CA_BUNDLE_PATH', 'SEC', true, true],
  ['card.dev_trust_test_ca', 'CARD_DEV_TRUST_TEST_CA', 'SEC', false, true],
  ['card.dev_skip_nonce_binding', 'CARD_DEV_SKIP_NONCE_BINDING', 'SEC', true, true],
  ['network.allowed_origins', 'ALLOWED_ORIGINS', 'SEC', true, true],
  ['network.allowed_hosts', 'ALLOWED_HOSTS', 'SEC', true, true],
  ['network.trusted_hosts', 'ANILA_TRUSTED_HOSTS', 'SEC', true, true],
  ['network.allow_http_model_endpoint', 'ANILA_ALLOW_HTTP_ENDPOINT', 'SEC', false, true],
  ['network.allow_http_agent_endpoint', 'ANILA_ALLOW_HTTP_AGENT_ENDPOINT', 'SEC', false, true],
  ['network.allow_grpc_endpoint', 'ANILA_ALLOW_GRPC_ENDPOINT', 'SEC', false, true],
  ['network.allow_private_endpoint', 'ANILA_ALLOW_PRIVATE_ENDPOINT', 'SEC', false, true],
  ['network.environment', 'ANILA_ENV', 'SEC', false, true],
  ['network.ssl_cert_file', 'SSL_CERT_FILE', 'SEC', true, true],
  ['proxy.llm_timeout', 'LLM_TIMEOUT', 'C', false, false],
  ['proxy.embedding_timeout', 'EMBEDDING_TIMEOUT', 'C', false, false],
  ['proxy.max_retries', 'PROXY_MAX_RETRIES', 'C', false, false],
  ['proxy.retry_base_delay', 'PROXY_RETRY_BASE_DELAY', 'C', false, false],
  ['proxy.model_gateway_api_key', 'MODEL_GATEWAY_API_KEY', 'A', true, true],
  ['health.check_interval', 'HEALTH_CHECK_INTERVAL', 'B_EDIT', true, false],
  ['alerts.check_interval', 'ALERT_CHECK_INTERVAL', 'B_EDIT', true, false],
  ['alerts.smtp_enabled', 'ANILA_ALERT_SMTP_ENABLED', 'B_LOCKED', true, true],
  ['alerts.smtp_host', 'ANILA_ALERT_SMTP_HOST', 'B_LOCKED', true, true],
  ['alerts.smtp_port', 'ANILA_ALERT_SMTP_PORT', 'B_LOCKED', true, true],
  ['alerts.smtp_user', 'ANILA_ALERT_SMTP_USER', 'B_LOCKED', true, true],
  ['alerts.smtp_password', 'ANILA_ALERT_SMTP_PASSWORD', 'A', true, true],
  ['alerts.smtp_from', 'ANILA_ALERT_SMTP_FROM', 'B_LOCKED', true, true],
  ['alerts.smtp_to', 'ANILA_ALERT_SMTP_TO', 'B_LOCKED', true, true],
  ['alerts.smtp_use_tls', 'ANILA_ALERT_SMTP_USE_TLS', 'B_LOCKED', true, true],
  ['usage.batch_size', 'USAGE_BATCH_SIZE', 'B_EDIT', true, false],
  ['usage.flush_interval', 'USAGE_FLUSH_INTERVAL', 'B_EDIT', true, false],
  ['service.csp_service_token', 'CSP_SERVICE_TOKEN', 'A', true, true],
  ['service.internal_platform_api_key', 'INTERNAL_PLATFORM_API_KEY', 'A', true, true],
  ['service.codeserver_password', 'CODESERVER_PASSWORD', 'A', true, true],
  ['seed.models', 'AUTO_REGISTER_MODELS', 'B_EDIT', true, false],
  ['seed.agents', 'AUTO_REGISTER_AGENTS', 'B_EDIT', true, false],
  ['seed.links', 'AUTO_REGISTER_LINKS', 'B_EDIT', true, false],
  ['seed.api_keys', 'AUTO_SEED_API_KEYS', 'A', true, true],
  ['storage.attachment_path', 'ATTACHMENT_STORAGE_PATH', 'B_EDIT', true, false],
  ['storage.ingestion_upload_dir', 'INGESTION_UPLOAD_DIR', 'B_LOCKED', true, true],
  ['queue.redis_url', 'REDIS_URL', 'B_LOCKED', true, true],
  ['queue.token_revocation_redis_timeout', 'TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS', 'B_LOCKED', true, true],
  ['limits.department_max_depth', 'ANILA_DEPARTMENT_MAX_DEPTH', 'C', false, false],
  ['limits.message_max_siblings', 'ANILA_MESSAGE_MAX_SIBLINGS', 'C', false, false],
  ['limits.action_invoke_per_min', 'ANILA_ACTION_INVOKE_PER_MIN', 'C', false, false],
  ['limits.action_max_body_chars', 'ANILA_ACTION_MAX_BODY_CHARS', 'C', false, false],
  ['limits.default_context_window', 'ANILA_DEFAULT_CONTEXT_WINDOW', 'C', false, false],
  ['limits.attachment_budget_ratio', 'ANILA_ATTACHMENT_BUDGET_RATIO', 'C', false, false],
  ['limits.attachment_token_safety', 'ANILA_ATTACHMENT_TOKEN_SAFETY', 'C', false, false],
  ['limits.attachment_max_stored_tokens', 'ANILA_ATTACHMENT_MAX_STORED_TOKENS', 'C', false, false],
  ['intl.zh_normalize', 'ANILA_ZH_NORMALIZE', 'C', false, false],
  ['intl.query_expansion', 'ANILA_QUERY_EXPANSION', 'C', false, false],
  ['intl.zip_filename_encoding', 'ANILA_ZIP_FILENAME_ENC', 'C', false, false],
  ['memory.retrieve_top_k', 'MEMORY_RETRIEVE_TOP_K', 'C', false, false],
  ['memory.retrieve_min_cosine', 'MEMORY_RETRIEVE_MIN_COSINE', 'C', false, false],
  ['memory.max_chunk_chars', 'MEMORY_MAX_CHUNK_CHARS', 'C', false, false],
  ['memory.http_timeout', 'MEMORY_HTTP_TIMEOUT', 'C', false, false],
  ['memory.llm_model', 'MEMORY_LLM_MODEL', 'B_LOCKED', true, true],
  ['agents.template_dir', 'ANILA_TEMPLATE_DIR', 'B_LOCKED', true, true],
  ['ingestion.pdf_ocr_fallback', 'PDF_OCR_FALLBACK', 'B_LOCKED', false, true],
  ['ingestion.vision_url', 'VISION_URL', 'B_LOCKED', false, true],
  ['ingestion.vision_model', 'VISION_MODEL', 'B_LOCKED', false, true],
  ['ingestion.vision_api_key', 'VISION_API_KEY', 'A', false, true],
  ['ingestion.vision_verify_ssl', 'VISION_VERIFY_SSL', 'SEC', false, true],
  ['ingestion.pdf_ocr_vision_prompt', 'PDF_OCR_VISION_PROMPT', 'B_LOCKED', false, true],
  ['ingestion.pdf_ocr_dpi', 'PDF_OCR_DPI', 'B_LOCKED', false, true],
  ['ingestion.pdf_ocr_concurrency', 'PDF_OCR_CONCURRENCY', 'B_LOCKED', false, true],
  ['ingestion.pdf_ocr_max_pages', 'PDF_OCR_MAX_PAGES', 'B_LOCKED', false, true],
  ['ingestion.doc_parser', 'DOC_PARSER', 'B_LOCKED', false, true],
  ['ingestion.docling_ocr_langs', 'DOCLING_OCR_LANGS', 'B_LOCKED', false, true],
  ['institutional_kb.score_threshold', null, 'C', false, false],
]

/** 一列 payload。欄位名與型別照 task-5-report §1 的 SettingItem。 */
function fixtureItem([key, env, cls, restart, hasLock], i) {
  const row = {
    key,
    class: cls,
    description: `${key} 的繁中說明（含值域人話）`,
    env_name: env,
    value_type: 'str',
    editable: cls === 'C' || cls === 'B_EDIT',
    restart_required: restart,
    locked_reason: hasLock ? `鎖定理由 · ${key} · 改法：在 compose 的 csp 服務 environment 設 ${env}` : null,
    default: null,
    effective: null,
    stored: null,
    stored_usable: false,
    pending: null,
    source: 'default',
    is_set: null,
    updated_at: null,
    updated_by: null,
  }
  if (cls === 'A') {
    // A 類：後端就只回 is_set（＋source），值一律 null。
    return { ...row, is_set: i % 3 === 0, source: i % 3 === 0 ? 'env' : 'default' }
  }
  const shaped = {
    ...row,
    default: `預設-${i}`,
    effective: `生效-${811 + i}`,
    stored: i % 4 === 0 ? `存的-${811 + i}` : null,
    stored_usable: i % 4 === 0,
    source: i % 4 === 0 ? 'db' : 'env',
  }
  if (cls === 'B_EDIT' && i % 2 === 0) {
    return { ...shaped, stored: `存的-${907 + i}`, stored_usable: true, pending: `待生效-${907 + i}`, source: 'env' }
  }
  return shaped
}

const ALL_96 = REGISTRY_96.map(fixtureItem)

function overviewFixture(overrides = {}) {
  return {
    total: ALL_96.length,
    boot_override_load_failed: false,
    boot_override_failure_reason: '',
    boot_override_applied_count: 3,
    items: ALL_96,
    ...overrides,
  }
}

/** 一列真的會出現三態的 B_EDIT（stored／effective／pending 三個數字互異）。 */
function threeStateRow(overrides = {}) {
  return {
    key: 'health.check_interval',
    class: 'B_EDIT',
    description: '健康檢查間隔秒數。允許 5–86400。',
    env_name: 'HEALTH_CHECK_INTERVAL',
    value_type: 'int',
    editable: true,
    restart_required: true,
    locked_reason: null,
    default: 811,
    effective: 907,
    stored: '1301',
    stored_usable: true,
    pending: 1301,
    source: 'env',
    is_set: null,
    updated_at: '2026-08-09T02:00:00+00:00',
    updated_by: 'kung',
    ...overrides,
  }
}

// ═══ 契約漂移護欄 ══════════════════════════════════════════════════════════
// fixture 是「96 顆」的替身。替身與本尊脫節的那一天，這裡要紅在前面。

test('fixture 的 96 顆 (key, class) 與後端登錄表原始碼一致', () => {
  const src = readFileSync(REGISTRY_PATH, 'utf8')
  const body = src.slice(src.indexOf('SETTINGS: tuple'))
  const declared = [...body.matchAll(
    /_spec\(\s*(?:"([^"]+)"|(\w+))\s*,\s*(?:"[^"]*"|None)\s*,\s*SettingClass\.(\w+)/g,
  )].map(([, literal, constant, cls]) => [
    // 唯一一顆用常數宣告的 key（env_name 為 None，本來就只住在 DB）。
    literal ?? (constant === 'KB_THRESHOLD_KEY' ? 'institutional_kb.score_threshold' : `<${constant}>`),
    cls,
  ])

  assert.equal(declared.length, 96, '登錄表的條目數變了，fixture 沒跟上')
  assert.deepEqual(
    declared,
    REGISTRY_96.map(([key, , cls]) => [key, cls]),
    '登錄表的 key／class 與本檔 fixture 不一致',
  )
})

test('五個 class 的顆數與後端報告的 20／12／26／25／13 對得起來', () => {
  const count = (cls) => ALL_96.filter((i) => i.class === cls).length
  assert.equal(count('C'), 20)
  assert.equal(count('B_EDIT'), 12)
  assert.equal(count('B_LOCKED'), 26)
  assert.equal(count('SEC'), 25)
  assert.equal(count('A'), 13)
  assert.equal(ALL_96.length, 96)
})

// ═══ 分區 ═════════════════════════════════════════════════════════════════

test('96 顆一顆都不掉：各區列數加總等於 items 長度', () => {
  const sections = groupIntoSections(ALL_96)
  const placed = sections.reduce((n, s) => n + s.items.length, 0)
  assert.equal(placed, 96, '有設定在分區時人間蒸發了')
  const keys = new Set(sections.flatMap((s) => s.items.map((i) => i.key)))
  assert.equal(keys.size, 96, '有 key 被分到兩個區')
})

test('四區各自收到自己的 class，一顆都不串門', () => {
  const byId = Object.fromEntries(groupIntoSections(ALL_96).map((s) => [s.id, s]))
  const classesIn = (id) => [...new Set((byId[id]?.items ?? []).map((i) => i.class))].sort()

  assert.deepEqual(classesIn('apply-now'), ['C'])
  assert.deepEqual(classesIn('apply-on-restart'), ['B_EDIT'])
  assert.deepEqual(classesIn('locked'), ['B_LOCKED', 'SEC'])
  assert.deepEqual(classesIn('secrets'), ['A'])

  assert.equal(byId['apply-now'].items.length, 20)
  assert.equal(byId['apply-on-restart'].items.length, 12)
  assert.equal(byId['locked'].items.length, 51)
  assert.equal(byId['secrets'].items.length, 13)
})

test('版面順序＝C → B_EDIT → 唯讀 → A（brief 的四區順序）', () => {
  assert.deepEqual(
    groupIntoSections(ALL_96).map((s) => s.id),
    ['apply-now', 'apply-on-restart', 'locked', 'secrets'],
  )
})

test('區內順序照後端回的順序，不自己排序', () => {
  const backendOrder = ALL_96.filter((i) => i.class === 'C').map((i) => i.key)
  const rendered = groupIntoSections(ALL_96)
    .find((s) => s.id === 'apply-now').items.map((i) => i.key)
  assert.deepEqual(rendered, backendOrder)
  // 若有人加了 sort()，第一顆會從 proxy.llm_timeout 變成 intl.query_expansion。
  assert.notDeepEqual(rendered, [...rendered].sort())
})

test('可編輯／唯讀的區旗標：只有前兩區可以放編輯器', () => {
  const byId = Object.fromEntries(groupIntoSections(ALL_96).map((s) => [s.id, s]))
  assert.equal(byId['apply-now'].editable, true)
  assert.equal(byId['apply-on-restart'].editable, true)
  assert.equal(byId['locked'].editable, false)
  assert.equal(byId['secrets'].editable, false)
  // A 區永遠沒有值欄位——後端根本不回值。
  assert.equal(byId['secrets'].showsValues, false)
  assert.equal(byId['locked'].showsValues, true)
})

test('後端回了這一頁還不認得的 class：進唯讀溢位區，值不顯示，也不會被丟掉', () => {
  const alien = { ...fixtureItem(REGISTRY_96[0], 0), key: 'future.thing', class: 'D', editable: true }
  const sections = groupIntoSections([...ALL_96, alien])
  const overflow = sections.find((s) => s.id === UNKNOWN_SECTION_ID)

  assert.equal(sectionIdFor(alien), UNKNOWN_SECTION_ID)
  assert.ok(overflow, '不認得的 class 被靜默丟掉了')
  assert.deepEqual(overflow.items.map((i) => i.key), ['future.thing'])
  assert.equal(overflow.editable, false, '不認得的類別不可以給編輯器')
  assert.equal(overflow.showsValues, false, '不知道能不能顯示的值就不要顯示')
  assert.equal(sections.reduce((n, s) => n + s.items.length, 0), 97)
})

test('沒有不認得的 class 時不生出空的溢位區', () => {
  assert.equal(groupIntoSections(ALL_96).some((s) => s.id === UNKNOWN_SECTION_ID), false)
})

test('SECTION_DEFS 的 class 名單不重不漏，涵蓋契約上的五個 class', () => {
  const covered = SECTION_DEFS.flatMap((s) => s.classes)
  assert.equal(new Set(covered).size, covered.length, '同一個 class 被兩個區宣告')
  assert.deepEqual([...covered].sort(), ['A', 'B_EDIT', 'B_LOCKED', 'C', 'SEC'])
})

test('清單還沒回來／壞形狀時不炸', () => {
  assert.deepEqual(groupIntoSections(undefined).map((s) => s.items.length), [0, 0, 0, 0])
  assert.deepEqual(groupIntoSections(null).map((s) => s.items.length), [0, 0, 0, 0])
})

// ═══ 三態 ═════════════════════════════════════════════════════════════════

test('三態：現在生效／待生效／存了但讀不回來，彼此分得開', () => {
  const effective = rowState({ ...threeStateRow(), stored: null, stored_usable: false, pending: null })
  const pending = rowState(threeStateRow())
  const unusable = rowState({ ...threeStateRow(), stored: '不是數字', stored_usable: false, pending: null })

  const ids = [effective.id, pending.id, unusable.id]
  assert.deepEqual(ids, ['effective', 'pending', 'unusable'])
  assert.equal(new Set([effective.label, pending.label, unusable.label]).size, 3, '三態共用同一句話')
  assert.equal(new Set([effective.className, pending.className, unusable.className]).size, 3, '三態共用同一個樣式')
})

test('待生效那一態必須說出「重啟」，而生效那一態不可以說', () => {
  const pending = rowState(threeStateRow())
  assert.match(pending.label, /重啟|尚未/)
  assert.doesNotMatch(rowState({ ...threeStateRow(), pending: null, stored: null }).label, /重啟/)
})

test('存了但讀不回來的那一列，不可以被講成待生效', () => {
  const state = rowState({ ...threeStateRow(), stored: '壞掉的值', stored_usable: false, pending: null })
  assert.equal(state.id, 'unusable')
  assert.match(state.label, /不會生效|讀不回|用不了/)
})

test('A 類沒有值，就沒有三態徽章可以掛', () => {
  const secret = ALL_96.find((i) => i.class === 'A')
  assert.equal(rowState(secret), null)
  assert.equal(rowState({ ...secret, class: 'D' }), null, '不認得的類別也不臆測狀態')
})

// ═══ 每列五欄：effective／pending／stored／default／source ═══════════════════

test('每一列都把五個欄位並列出來（pending 與 effective 同時看得到）', () => {
  const cells = valueCells(threeStateRow())
  assert.deepEqual(cells.map((c) => c.field), ['effective', 'pending', 'stored', 'default', 'source'])
})

test('⚠ 殺形：pending 不可以穿 effective 的樣式', () => {
  const cells = valueCells(threeStateRow())
  const eff = cells.find((c) => c.field === 'effective')
  const pend = cells.find((c) => c.field === 'pending')

  assert.notEqual(pend.className, eff.className, '待生效畫成已生效——這是本計畫的殺形')
  assert.notEqual(pend.label, eff.label)
  assert.match(pend.label, /重啟|尚未/)
  assert.equal(pend.text, '1301')
  assert.equal(eff.text, '907')
  assert.notEqual(pend.text, eff.text, 'fixture 自己就分不開，這支測試不算數')
})

test('沒有 pending 的列，那一格不可以借用有 pending 的樣式', () => {
  const withPending = valueCells(threeStateRow()).find((c) => c.field === 'pending')
  const without = valueCells({ ...threeStateRow(), pending: null }).find((c) => c.field === 'pending')
  assert.equal(without.text, '—')
  assert.notEqual(without.className, withPending.className)
})

test('stored 讀不回來時，那一格自己講得出「不會生效」', () => {
  const cells = valueCells({ ...threeStateRow(), stored: '不是數字', stored_usable: false, pending: null })
  const stored = cells.find((c) => c.field === 'stored')
  const usable = valueCells(threeStateRow()).find((c) => c.field === 'stored')
  assert.notEqual(stored.className, usable.className)
  assert.match(stored.label, /不會生效|讀不回|用不了/)
  assert.equal(stored.text, '不是數字', '存了什麼就顯示什麼，不改寫')
})

test('程式預設那一格不可以被講成生效值', () => {
  const cells = valueCells(threeStateRow())
  const dflt = cells.find((c) => c.field === 'default')
  assert.equal(dflt.text, '811')
  assert.notEqual(dflt.className, cells.find((c) => c.field === 'effective').className)
  assert.match(dflt.label, /預設/)
  assert.doesNotMatch(dflt.label, /生效/)
})

test('A 類不產生任何值欄位——後端沒給的東西畫面上生不出來', () => {
  const secret = ALL_96.find((i) => i.class === 'A')
  assert.deepEqual(valueCells(secret), [])
})

// ═══ 顯示字串 ═════════════════════════════════════════════════════════════

test('null 是「沒有」，空字串是「空字串」，0 與 false 是它們自己', () => {
  assert.equal(formatSettingValue(null), '—')
  assert.equal(formatSettingValue(undefined), '—')
  assert.equal(formatSettingValue(''), '（空字串）')
  assert.equal(formatSettingValue(0), '0')
  assert.equal(formatSettingValue(false), 'false')
  assert.equal(formatSettingValue(true), 'true')
  assert.equal(formatSettingValue(0.77), '0.77')
  assert.equal(formatSettingValue('ch_tra,en'), 'ch_tra,en')
})

test('布林顯示成 env 那一層寫得出來的字，不要自己翻成是／否', () => {
  // 五種互不相容的真值判準（== "1"、!= "0"、lower == "true"…）在後端，
  // 前端把 false 翻成「否」等於再發明第六種寫法。
  assert.equal(formatSettingValue(false), 'false')
  assert.notEqual(formatSettingValue(false), '否')
})

test('來源四種都有繁中對照，不認得的原樣顯示不臆測', () => {
  assert.match(sourceLabel('db'), /DB|資料庫/)
  assert.match(sourceLabel('db-boot'), /開機/)
  assert.match(sourceLabel('env'), /env|compose/i)
  assert.match(sourceLabel('default'), /預設/)
  assert.equal(sourceLabel('future-layer'), 'future-layer')
  assert.equal(sourceLabel(null), '—')
  // 四個標籤必須互不相同，否則「db-boot」與「db」在畫面上是同一件事。
  const four = ['db', 'db-boot', 'env', 'default'].map(sourceLabel)
  assert.equal(new Set(four).size, 4)
})

test('locked_reason 原樣全文上畫面 —— 降級七顆的 compose 指引在裡面', () => {
  const real =
    '開機序早於覆蓋載入 —— import 期就被讀走，重啟也套不上（Task 4 C1）'
    + '。改法：在 compose 的 csp 服務 environment 設 DEBUG'
    + '（現在沒有這一行就自己加），改完 up -d 重建容器'
  const out = lockedReasonText({ key: 'app.debug', class: 'B_LOCKED', locked_reason: real })

  assert.equal(out, real, 'locked_reason 被改寫或截斷了')
  assert.equal(out.length, real.length)
  assert.match(out, /DEBUG/, 'compose 鍵指引不見了，管理員沒有自救路徑')
  assert.match(out, /up -d/)
  assert.equal(lockedReasonText({ locked_reason: null }), null)
  assert.equal(lockedReasonText({ locked_reason: '' }), null)
})

test('A 類只講「設了沒有」，後端沒講就說沒講', () => {
  assert.equal(isSetLabel({ class: 'A', is_set: true }), '已設定')
  assert.equal(isSetLabel({ class: 'A', is_set: false }), '未設定')
  assert.equal(isSetLabel({ class: 'A', is_set: null }), '—')
  assert.equal(new Set([isSetLabel({ is_set: true }), isSetLabel({ is_set: false })]).size, 2)
})

test('⚠ 編輯框裡放的是可送出的字串，不是顯示字串', () => {
  // formatSettingValue 的「—」與「（空字串）」是講給人看的；灌進 input 之後
  // 按下儲存，那三個字就會變成一個真的設定值。
  assert.equal(draftValue({ effective: '', pending: null }), '')
  assert.notEqual(draftValue({ effective: '', pending: null }), formatSettingValue(''))
  assert.equal(draftValue({ effective: null, pending: null }), '')
  assert.notEqual(draftValue({ effective: null, pending: null }), formatSettingValue(null))
  assert.equal(draftValue({ effective: 907, pending: null }), '907')
  assert.equal(draftValue({ effective: false, pending: null }), 'false')
  // 已經存了待生效值的列，框裡放的是那個待生效值（管理員接著改的就是它）。
  assert.equal(draftValue(threeStateRow()), '1301')
})

// ═══ 開機覆蓋 banner ═══════════════════════════════════════════════════════

test('這次開機沒載入覆蓋 → 大字 banner，原因原樣帶出來', () => {
  const banner = bootOverrideBanner(overviewFixture({
    boot_override_load_failed: true,
    boot_override_failure_reason: 'OperationalError',
    boot_override_applied_count: 0,
  }))
  assert.ok(banner, 'load_failed 卻沒有 banner')
  assert.equal(banner.tone, 'danger')
  assert.match(banner.title, /沒有載入|沒有套/)
  assert.equal(banner.reason, 'OperationalError')
})

test('⚠ banner 不看 applied_count 的臉色：套過幾顆都要講', () => {
  // 「快照宣稱套過、行程其實沒套」正是後端 §2 第三種分岔的形狀。
  const banner = bootOverrideBanner(overviewFixture({
    boot_override_load_failed: true,
    boot_override_failure_reason: 'ProgrammingError',
    boot_override_applied_count: 7,
  }))
  assert.ok(banner, 'applied_count 非 0 就把 banner 吞掉了')
  assert.equal(banner.reason, 'ProgrammingError')
})

test('後端沒給原因時說「沒有給原因」，不要自己編一個', () => {
  const banner = bootOverrideBanner(overviewFixture({
    boot_override_load_failed: true,
    boot_override_failure_reason: '',
  }))
  assert.match(banner.reason, /沒有|未提供/)
  assert.doesNotMatch(banner.reason, /資料庫|連線|逾時/, '後端沒說的原因不可以猜')
})

test('⚠ 自查：後端說 96 顆而這一頁只收到 90 顆，要講出來', () => {
  // 少畫六顆的頁面與正常的頁面在畫面上長得一模一樣——每一區都有東西、
  // 沒有錯誤訊息。那正是本包要消滅的「靜默地少講一件事」。
  const short = overviewFixture({ items: ALL_96.slice(0, 90) })
  const warning = countMismatchWarning(short)
  assert.ok(warning, '總數對不起來卻一聲不吭')
  assert.match(warning, /96/)
  assert.match(warning, /90/)

  assert.equal(countMismatchWarning(overviewFixture()), null)
  assert.equal(countMismatchWarning({ total: 0, items: [] }), null)
  assert.equal(countMismatchWarning(null), null)
  assert.equal(countMismatchWarning({ items: ALL_96 }), null, '後端沒給 total 就不要自己算一個出來比')
})

test('開機正常時不掛 banner（也不對壞形狀 payload 炸）', () => {
  assert.equal(bootOverrideBanner(overviewFixture()), null)
  assert.equal(bootOverrideBanner(null), null)
  assert.equal(bootOverrideBanner({}), null)
  // 只有布林 true 才算失敗；字串 'false' 不是。
  assert.equal(bootOverrideBanner({ boot_override_load_failed: 'false' }), null)
})

// ═══ 儲存之後說什麼 ════════════════════════════════════════════════════════

test('B_EDIT 存完要講「重啟後生效」，而且把指令講出來', () => {
  const notice = saveNotice(threeStateRow())
  assert.equal(notice.message, RESTART_HINT)
  assert.equal(RESTART_HINT, '已儲存，重啟後生效：docker compose up -d csp')
  assert.notEqual(notice.tone, 'ok', '重啟才生效的事不可以用成功語氣蓋過去')
})

test('回應沒有 pending 但 restart_required 為真，一樣要講重啟', () => {
  assert.equal(saveNotice({ ...threeStateRow(), pending: null, restart_required: true }).message, RESTART_HINT)
})

test('C 類存完是「下一個請求就生效」，不可以叫人去重啟', () => {
  const notice = saveNotice({
    key: 'proxy.llm_timeout', class: 'C', restart_required: false,
    effective: 811, pending: null, stored: '811', stored_usable: true, source: 'db',
  })
  assert.equal(notice.message, SAVED_NOW_HINT)
  assert.doesNotMatch(notice.message, /重啟|up -d/)
  assert.notEqual(notice.message, RESTART_HINT)
})

// ═══ 顯示值一律來自後端回應 ════════════════════════════════════════════════

test('PUT 之後整列取代，不是把新舊欄位合起來', () => {
  const before = threeStateRow()
  const after = { ...threeStateRow(), effective: 907, pending: 4321, stored: '4321', updated_by: 'owner' }
  const [got] = replaceRow([before], after).filter((i) => i.key === before.key)

  assert.deepEqual(got, after)
  assert.equal(got.pending, 4321)
  assert.equal(got.updated_by, 'owner')
})

test('⚠ 回應少了某個欄位，畫面就不可以留著舊的那個值', () => {
  const before = { ...threeStateRow(), pending: 1301 }
  const after = { key: before.key, class: 'B_EDIT', effective: 907, stored: null, stored_usable: false }
  const [got] = replaceRow([before], after)

  assert.deepEqual(got, after, '整列取代被寫成 merge，舊值會留在畫面上')
  assert.equal('pending' in got, false, '舊的 pending 還黏在那一列上')
})

test('replaceRow 不動其他列，也不會把不認識的 key 硬塞進來', () => {
  const rows = ALL_96.slice(0, 5)
  const same = replaceRow(rows, { key: 'not.in.the.list', class: 'C' })
  assert.deepEqual(same.map((i) => i.key), rows.map((i) => i.key))
  assert.equal(same.length, 5)
})

test('可不可以編輯，問後端那個欄位，不是自己看 class 猜', () => {
  // 後端哪天把某一顆 C 降級成唯讀而 class 還沒動，畫面必須立刻停手。
  assert.equal(canEdit({ class: 'C', editable: true }), true)
  assert.equal(canEdit({ class: 'C', editable: false }), false)
  assert.equal(canEdit({ class: 'B_LOCKED', editable: true }), true)
  assert.equal(canEdit({ class: 'A' }), false)
  assert.equal(canEdit(null), false)
})

// ═══ 錯誤與初載狀態 ════════════════════════════════════════════════════════

test('後端的 detail 原樣上畫面（值域說明與 locked_reason 在裡面）', () => {
  const detail = '值域外：alerts.check_interval 允許 15–86400，收到 7。'
  assert.equal(extractDetail({ response: { data: { detail } } }), detail)
})

test('detail 不是字串時也不吞掉，原樣帶出來', () => {
  const detail = [{ loc: ['body', 'value'], msg: 'none is not an allowed value' }]
  const out = extractDetail({ response: { data: { detail } } })
  assert.match(out, /none is not an allowed value/)
})

test('後端沒給 detail 時退回 e.message，不要自己編一句', () => {
  assert.equal(extractDetail(new Error('Network Error')), 'Network Error')
  assert.equal(extractDetail({}, '讀不到設定總覽'), '讀不到設定總覽')
})

test('初載失敗 ≠ 空清單（DepartmentsView 缺的就是這個 UI）', () => {
  assert.equal(overviewState({ loaded: false, error: null, items: [] }), 'loading')
  assert.equal(overviewState({ loaded: true, error: null, items: [] }), 'empty')
  assert.equal(overviewState({ loaded: true, error: null, items: ALL_96 }), 'ready')
  assert.equal(overviewState({ loaded: true, error: '炸了', items: ALL_96 }), 'failed')
  assert.equal(overviewState({ loaded: false, error: '炸了', items: [] }), 'failed')

  assert.notEqual(overviewStateMessage('failed'), overviewStateMessage('empty'))
  assert.match(overviewStateMessage('failed'), /讀不到|失敗/)
  assert.doesNotMatch(overviewStateMessage('failed'), /^尚無|沒有任何設定$/)
})

// ═══ 原始碼層護欄：呼叫端真的照著做 ════════════════════════════════════════

test('api/platformSettings.js 打的是契約上那兩支端點，body 是 {value}', () => {
  const src = stripComments(readSource('api/platformSettings.js'))
  assert.match(src, /client\.get\('\/api\/platform-settings\/overview'\)/)
  assert.match(src, /client\.put\(`\/api\/platform-settings\/\$\{[^}]+\}`,\s*\{\s*value\s*\}\)/)
})

test('送出的字串不可以被前端先 trim —— 有一顆的真值判準沒有 strip', () => {
  // CARD_DEV_SKIP_NONCE_BINDING 的判準沒有 strip，CARD_DEV_TRUST_TEST_CA 有。
  // 前端擅自 trim 等於替後端發明第六種真值規則。
  const api = stripComments(readSource('api/platformSettings.js'))
  const view = stripComments(readSource('views/SettingsOverviewView.vue'))
  assert.doesNotMatch(api, /\.trim\(\)/)
  assert.doesNotMatch(view, /draft[^\n]*\.trim\(\)/)
})

test('SettingsOverviewView 的分區／分態全部走 utils，不自己再寫一份', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  assert.match(src, /from '\.\.\/utils\/settingsView'/)
  for (const fn of ['groupIntoSections', 'valueCells', 'rowState', 'bootOverrideBanner', 'saveNotice', 'canEdit']) {
    assert.match(src, new RegExp(fn), `${fn} 沒有被畫面接走`)
  }
  // 自己再寫一份 class 判斷，就是兩份規則各自漂移的起點。
  assert.doesNotMatch(src, /===\s*'B_EDIT'/)
  assert.doesNotMatch(src, /===\s*'B_LOCKED'/)
  assert.doesNotMatch(src, /===\s*'SEC'/)
})

test('每一格的樣式由 utils 給，畫面不得硬寫死同一個 class', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  assert.match(src, /:class="cell\.className"/, '格子沒有吃 utils 給的樣式，pending 可以被畫成 effective')
  assert.doesNotMatch(src, /class="[^"]*setting-cell--effective/)
  assert.doesNotMatch(src, /class="[^"]*setting-cell--pending/)
})

test('唯讀區在版面最後，而且裡面一個控制項都沒有', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  const editableAt = src.indexOf('data-region="editable"')
  const readonlyAt = src.indexOf('data-region="readonly"')
  assert.ok(editableAt >= 0, '找不到可編輯區')
  assert.ok(readonlyAt > editableAt, '唯讀區不在可編輯區之後，下面的切片就切錯了')

  const readonly = src.slice(readonlyAt, src.indexOf('<script setup>'))
  assert.ok(readonly.length > 200, '唯讀區切出來是空的，這支護欄形同虛設')
  for (const forbidden of [/<input/, /<textarea/, /<select/, /v-model/, /handleSave/, /TermButton/, /@click/]) {
    assert.doesNotMatch(readonly, forbidden, `唯讀區出現了控制項：${forbidden}`)
  }
})

test('唯讀區照樣把 locked_reason 全文渲染出來，而且沒有被 CSS 截斷', () => {
  const src = readSource('views/SettingsOverviewView.vue')
  const stripped = stripComments(src)
  const readonly = stripped.slice(stripped.indexOf('data-region="readonly"'), stripped.indexOf('<script setup>'))
  assert.match(readonly, /lockedReasonText\(item\)/, 'locked_reason 沒有渲染，降級七顆就沒有自救路徑')

  const style = src.slice(src.indexOf('<style'))
  const lockedBlock = style.slice(style.indexOf('.setting-locked'), style.indexOf('.setting-locked') + 400)
  assert.doesNotMatch(lockedBlock, /text-overflow:\s*ellipsis/, 'locked_reason 被 CSS 截斷')
  assert.doesNotMatch(lockedBlock, /line-clamp/)
  assert.doesNotMatch(lockedBlock, /white-space:\s*nowrap/)
})

test('編輯器只掛在 canEdit 為真的列上', () => {
  const stripped = stripComments(readSource('views/SettingsOverviewView.vue'))
  const editable = stripped.slice(
    stripped.indexOf('data-region="editable"'),
    stripped.indexOf('data-region="readonly"'),
  )
  assert.match(editable, /v-if="canEdit\(item\)"/, '編輯器沒有綁在後端的 editable 欄位上')
  assert.match(editable, /v-model="drafts\[item\.key\]"/)
})

test('⚠ 不做樂觀更新：畫面上的值不得由前端指派', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  for (const forbidden of [
    /item\.effective\s*=/, /item\.pending\s*=/, /item\.stored\s*=/, /item\.source\s*=/,
    /row\.effective\s*=/, /row\.pending\s*=/,
  ]) {
    assert.doesNotMatch(src, forbidden, `有人在前端自己改顯示值：${forbidden}`)
  }
  // 存完之後，那一列必須被回應整列取代。
  assert.match(src, /replaceRow\(/)
})

test('存完之後那一列的 detail 錯誤要消失（先失敗再成功不可以留舊訊息）', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  const handler = src.slice(src.indexOf('async function handleSave'), src.indexOf('</script>'))
  assert.ok(handler.length > 0, '找不到 handleSave')
  assert.match(handler, /delete errors\.value\[|errors\.value\[[^\]]+\]\s*=\s*(null|'')/, '成功後沒有清掉舊的錯誤訊息')
  assert.match(handler, /extractDetail\(/, '錯誤訊息沒有走 extractDetail，detail 可能被改寫')
})

test('banner 站在版面最前面（優先位置）', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  const bannerAt = src.indexOf('boot-override-banner')
  assert.ok(bannerAt >= 0, '找不到開機覆蓋 banner')
  // 位置對、卻沒有接上資料的 banner 是一塊永遠不會亮的招牌。
  assert.match(src, /<div v-if="banner" class="boot-override-banner">/, 'banner 的顯示條件不是 banner 本身')
  assert.match(src, /const banner = computed\(\(\) => bootOverrideBanner\(/, 'banner 不是從 bootOverrideBanner 來的')
  assert.match(src, /\{\{ banner\.reason \}\}/, '原因沒有渲染出來')
  assert.match(src, /\{\{ banner\.title \}\}/)
  assert.ok(bannerAt < src.indexOf('data-region="editable"'), 'banner 排在設定區後面，捲下去才看得到')
  assert.ok(bannerAt < src.indexOf('data-region="readonly"'))

  // 「少收到幾顆」的警告同樣要在設定區之前，否則它就在第 96 列下面。
  const mismatchAt = src.indexOf('countMismatchWarning')
  assert.ok(mismatchAt >= 0, '畫面沒有接總數不符的警告')
  assert.ok(src.indexOf('settings-count-warning') < src.indexOf('data-region="editable"'))
})

test('初載失敗有專屬 UI，而且與空清單分得開', () => {
  const src = stripComments(readSource('views/SettingsOverviewView.vue'))
  assert.match(src, /loadError/, '沒有初載失敗狀態（DepartmentsView 缺的就是這個）')
  assert.match(src, /settings-load-error/, '初載失敗沒有專屬區塊')
  assert.match(src, /重試|重新載入/, '失敗了沒有自救出口')
  assert.match(src, /overviewStateMessage\(/)
})

test('新增檔案不得引入裸 data.detail 插值（沿用 W2-12 的 ratchet 紀律）', () => {
  // ratchet 針對的是**呼叫端**的就地插值：每多一處，就多一個可以自己改寫
  // 後端訊息的地方。呼叫端一律走 extractDetail。
  for (const relative of ['views/SettingsOverviewView.vue', 'api/platformSettings.js']) {
    const source = stripComments(readSource(relative))
    const hits = source.match(/response\??\.data\??\.detail/g) || []
    assert.equal(hits.length, 0, `${relative} 有裸 data.detail 插值`)
  }
})

test('detail 只有一個出入口 —— extractDetail 那一行', () => {
  const source = stripComments(readSource('utils/settingsView.js'))
  const hits = source.match(/response\??\.data\??\.detail/g) || []
  assert.equal(hits.length, 1, '後端訊息的取出點不只一個，遲早會有一份自己改寫')
  const fn = source.slice(source.indexOf('export function extractDetail'))
  assert.match(fn.slice(0, 400), /response\?\.data\?\.detail/)
})

test('router 用 /users 那個 requiresAdmin 形狀註冊 /platform-settings', () => {
  const src = stripComments(readSource('router/index.js'))
  assert.match(src, /path: 'platform-settings'/)
  assert.match(src, /SettingsOverviewView\.vue/)
  const block = src.slice(src.indexOf("path: 'platform-settings'"))
  assert.match(block.slice(0, 260), /meta: \{ requiresAdmin: true \}/)
})

test('側欄的管理群組掛得上這一頁', () => {
  const src = stripComments(readSource('components/layout/AppSidebar.vue'))
  const start = src.indexOf('const adminItems')
  assert.ok(start >= 0, '找不到 adminItems')
  const adminBlock = src.slice(start, src.indexOf('groups.push', start))
  assert.match(adminBlock, /path: '\/platform-settings'/)
  assert.match(adminBlock, /label: '平台設定'/)
})
