// W2-12 驗收:治理後台的錯誤解讀 helper。
//
// 為什麼這支測試存在
// ------------------
// 治理後台有 102 處(22 檔)直接把後端的 `detail` 當字串插進訊息,但 CSP 的 `detail`
// 有三種形狀(字串 / 物件 / 422 array)。後兩種會渲染成 `[object Object]`
// —— 使用者看到那個等於什麼都沒看到,而這是**管理員唯一的入口**。
//
// 另一條是防再犯:`LoginView.vue:419` 原本比對後端中文子字串 `'等待核准'`
// 來決定要不要顯示待核准說明頁。後端改一個字就靜默壞掉,而且沒有任何測試會紅。
// 本檔最後一組測試會**真的把後端訊息改字**,驗證分流仍然正確。
//
// 用 node --test(與既有三支測試同一套,不引入 jsdom / vitest)。helper 刻意
// 寫成零依賴純函式,所以直接 import 就能測。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  AUTH_LOCAL_PASSWORD_DISABLED,
  AUTH_PENDING_APPROVAL,
  classifyLoginError,
  extractError,
  extractErrorCode,
  extractErrorDetails,
  extractRequestId,
} from '../src/api/errors.js'

/** 造一個長得像 axios error 的物件。 */
function axiosError(data, { status = 400, message = 'Request failed' } = {}) {
  const err = new Error(message)
  err.response = { status, data }
  err.isAxiosError = true
  return err
}

/** 本包的核心不變式:輸出永不含 `[object Object]`。 */
function assertNoObjectObject(value) {
  assert.equal(typeof value, 'string')
  assert.ok(
    !value.includes('[object Object]'),
    `訊息含 [object Object]:${value}`,
  )
  assert.ok(value.length > 0, '訊息不得為空')
}

// ── ③ dict detail 不得渲染成 [object Object] ──────────────────────────────

test('dict detail 走信封 error.message,不出現 [object Object]', () => {
  const err = axiosError(
    {
      error: {
        code: 'MODEL_UNHEALTHY',
        message: '模型 gemma4 已被 health probe 標記 unhealthy',
        details: null,
        request_id: null,
      },
      detail: { code: 'model_unhealthy', message: '模型 gemma4 已被 health probe 標記 unhealthy' },
    },
    { status: 503 },
  )
  const message = extractError(err, '轉發失敗')
  assertNoObjectObject(message)
  assert.equal(message, '模型 gemma4 已被 health probe 標記 unhealthy')
})

test('沒有信封、只有 dict detail 的舊後端也不得 [object Object]', () => {
  // 過渡期:部署中的舊 CSP 還沒有 error 欄位。helper 仍必須讀得出訊息。
  const err = axiosError(
    { detail: { code: 'model_unhealthy', message: '模型暫時拒絕轉發' } },
    { status: 503 },
  )
  const message = extractError(err, '轉發失敗')
  assertNoObjectObject(message)
  assert.equal(message, '模型暫時拒絕轉發')
})

test('無 message 鍵的 dict detail 退回可讀 JSON,不是 [object Object]', () => {
  const err = axiosError({ detail: { reason: 'quota', remaining: 0 } }, { status: 429 })
  const message = extractError(err, '失敗')
  assertNoObjectObject(message)
  assert.ok(message.includes('quota'), message)
})

test('422 array detail 壓成逐欄位訊息,不是 [object Object]', () => {
  const err = axiosError(
    {
      error: {
        code: 'VALIDATION_ERROR',
        message: '請求驗證失敗 — password: Field required',
        details: [{ loc: ['body', 'password'], msg: 'Field required', type: 'missing' }],
        request_id: null,
      },
      detail: [{ loc: ['body', 'password'], msg: 'Field required', type: 'missing' }],
    },
    { status: 422 },
  )
  const message = extractError(err, '儲存失敗')
  assertNoObjectObject(message)
  assert.ok(message.includes('password'), message)
})

test('舊後端的裸 422 array(無信封)也要壓成可讀字串', () => {
  const err = axiosError(
    {
      detail: [
        { loc: ['body', 'name'], msg: 'Field required', type: 'missing' },
        { loc: ['body', 'endpoint_url'], msg: 'Input should be a valid URL' },
      ],
    },
    { status: 422 },
  )
  const message = extractError(err, '儲存失敗')
  assertNoObjectObject(message)
  assert.ok(message.includes('name'), message)
  assert.ok(message.includes('endpoint_url'), message)
})

test('字串 detail 行為與改動前完全一致', () => {
  const err = axiosError({ detail: '模型名稱重複' }, { status: 409 })
  assert.equal(extractError(err, '建立失敗'), '模型名稱重複')
})

test('沒有 response 的網路錯誤退回 fallback,再退回 err.message', () => {
  const netErr = new Error('Network Error')
  assert.equal(extractError(netErr, '載入失敗'), '載入失敗')
  assert.equal(extractError(netErr), 'Network Error')
})

test('完全空的錯誤也回非空字串(絕不顯示 undefined)', () => {
  assertNoObjectObject(extractError(undefined))
  assertNoObjectObject(extractError(null))
  assertNoObjectObject(extractError({}))
  assertNoObjectObject(extractError(axiosError({})))
})

test('extractErrorCode / details / request_id 讀信封', () => {
  const err = axiosError({
    error: {
      code: 'CLEARANCE_INSUFFICIENT',
      message: 'clearance 不足',
      details: { required: '機密' },
      request_id: 'req-42',
    },
    detail: 'clearance 不足',
  })
  assert.equal(extractErrorCode(err), 'CLEARANCE_INSUFFICIENT')
  assert.deepEqual(extractErrorDetails(err), { required: '機密' })
  assert.equal(extractRequestId(err), 'req-42')
})

test('舊後端(無信封)拿不到 code,回 null 而不是猜', () => {
  const err = axiosError({ detail: '拒絕' })
  assert.equal(extractErrorCode(err), null)
  assert.equal(extractRequestId(err), null)
})

// ── ④ 待核准登入靠 code 分流,改字後仍綠 ──────────────────────────────────

test('待核准登入靠 code 分流', () => {
  const err = axiosError(
    {
      error: {
        code: AUTH_PENDING_APPROVAL,
        message: '等待核准中，請通知 admin',
        details: null,
        request_id: null,
      },
      detail: '等待核准中，請通知 admin',
    },
    { status: 403 },
  )
  const result = classifyLoginError(err)
  assert.equal(result.isPending, true)
  assert.equal(result.code, AUTH_PENDING_APPROVAL)
  assertNoObjectObject(result.message)
})

test('後端把待核准訊息改字,分流仍然正確(這條就是防再犯)', () => {
  // 逐字模擬「後端改文案」:訊息完全不含 '等待核准' 也不含 'pending'。
  // 舊寫法 detail.includes('等待核准') 在這裡會靜默失效。
  for (const message of [
    '帳號審核中，請聯絡系統管理員',
    'Your account is awaiting review',
    '（文案已改寫）',
    '',
  ]) {
    const err = axiosError(
      {
        error: {
          code: AUTH_PENDING_APPROVAL,
          message,
          details: null,
          request_id: null,
        },
        detail: message,
      },
      { status: 403 },
    )
    const result = classifyLoginError(err)
    assert.equal(
      result.isPending,
      true,
      `訊息改成 ${JSON.stringify(message)} 後分流壞掉 —— 這正是 W2-12 要防的再犯`,
    )
    assertNoObjectObject(result.message)
  }
})

test('舊寫法的中文子字串比對確實擋不住改字(反證)', () => {
  // 這條不是在測 production code,是在證明「為什麼要改」。
  // 若哪天有人想把 classifyLoginError 改回子字串比對,先讀這裡。
  const renamed = '帳號審核中，請聯絡系統管理員'
  assert.equal(renamed.includes('等待核准'), false)
  assert.equal(renamed.toLowerCase().includes('pending'), false)
})

test('帳密錯誤不得誤判成待核准', () => {
  const err = axiosError(
    {
      error: {
        code: 'AUTH_INVALID_CREDENTIALS',
        message: '帳號或密碼錯誤',
        details: null,
        request_id: null,
      },
      detail: '帳號或密碼錯誤',
    },
    { status: 401 },
  )
  const result = classifyLoginError(err)
  assert.equal(result.isPending, false)
  assert.equal(result.ssoOnly, false)
  assert.equal(result.message, '帳號或密碼錯誤')
})

test('SSO-only 帳號有獨立分支(不與待核准混用)', () => {
  const err = axiosError(
    {
      error: {
        code: AUTH_LOCAL_PASSWORD_DISABLED,
        message: '此帳號已切換為 SSO 登入；請改用單一登入按鈕。',
        details: null,
        request_id: null,
      },
      detail: '此帳號已切換為 SSO 登入；請改用單一登入按鈕。',
    },
    { status: 403 },
  )
  const result = classifyLoginError(err)
  assert.equal(result.ssoOnly, true)
  assert.equal(result.isPending, false)
})

test('過渡期:舊後端無 code 時仍靠中文子字串認出待核准', () => {
  const err = axiosError({ detail: '等待核准中，請通知 admin' }, { status: 403 })
  const result = classifyLoginError(err)
  assert.equal(result.code, null)
  assert.equal(result.isPending, true)
})

// ── 原始碼層護欄:防止有人手滑改回舊寫法 ──────────────────────────────────
//
// 上面那些是行為測試,但它們測的是 helper。真正壞掉的是**呼叫端**:
// `LoginView.vue` 只要有人把 `classifyLoginError` 換回子字串比對,行為測試
// 全綠而 UX 照樣壞。所以這裡直接讀原始碼釘住呼叫端。

const SRC = fileURLToPath(new URL('../src/', import.meta.url))

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

/** 去掉 `//` 行註解 —— 註解裡引用舊寫法(說明為什麼改)不該讓 guard 誤紅。 */
function stripLineComments(source) {
  return source
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

test('LoginView 不得再比對後端中文訊息子字串', () => {
  const source = stripLineComments(readSource('views/LoginView.vue'))
  assert.ok(
    !source.includes("includes('等待核准')"),
    'LoginView 又回到比對後端中文子字串 —— 後端改一個字就會靜默壞掉,' +
      '請改用 classifyLoginError(err).isPending',
  )
  assert.ok(
    source.includes('classifyLoginError'),
    'LoginView 應透過 classifyLoginError 走 code 分流',
  )
})

test('已收斂的檔案不得殘留裸 data.detail 插值', () => {
  // 這份清單就是 W2-12 實際收斂的範圍。**不是**全部 22 檔 —— 殘量在 PR
  // 描述裡有明寫數字,不得宣稱已收斂。清單只准增加。
  const converted = [
    'views/LoginView.vue',
    'views/DeveloperAgentsView.vue',
    'views/UsersView.vue',
    'views/CollectionDetailView.vue',
    'views/ModelsView.vue',
    'views/KnowledgeCollectionsView.vue',
    'views/ApiKeysView.vue',
  ]
  for (const relative of converted) {
    const source = stripLineComments(readSource(relative))
    const hits = source.match(/response\??\.data\??\.detail/g) || []
    assert.equal(
      hits.length,
      0,
      `${relative} 仍有 ${hits.length} 處裸 data.detail 插值,請改用 extractError(e, fallback)`,
    )
    assert.ok(
      source.includes('extractError') || source.includes('classifyLoginError'),
      `${relative} 應 import 共用 helper`,
    )
  }
})

test('未收斂的殘量只准降不准升(ratchet)', () => {
  // 歷程:改動前 102 處 / 22 檔 → W2-12 本體收斂 7 檔 60 處(殘量 42)→
  // W2-12 收尾把剩下 14 檔 40 處掃完,**殘量 2 處 / 1 檔**。
  //
  // 剩下那 2 處在 `views/ClassificationInventoryView.vue`,刻意沒動:W2-11
  // (分類正確性的輸入端)正在改同一支檔案加抽查報表,同時改會撞 merge。
  // W2-11 落地後把上限降到 0。
  //
  // 這條 ratchet 的作用是:新程式碼不准再寫裸 `data.detail` 插值。上限只准降 ——
  // 而上限不會自己降,清完的人要順手鎖緊(這正是 C5 ledger 的同一條紀律)。
  //
  // `src/api/errors.js` 自己要讀 legacy `detail` 當過渡期 fallback,那是 helper
  // 的職責,不算殘量。
  const RESIDUAL_CEILING = 2
  const files = walkSources(SRC).filter((f) => !f.endsWith('/api/errors.js'))
  let residual = 0
  const perFile = []
  for (const file of files) {
    const hits = (readFileSync(file, 'utf8').match(/response\??\.data\??\.detail/g) || [])
      .length
    if (hits) {
      residual += hits
      perFile.push(`${file.slice(SRC.length)}: ${hits}`)
    }
  }
  assert.ok(
    residual <= RESIDUAL_CEILING,
    `裸 data.detail 插值從 ${RESIDUAL_CEILING} 升到 ${residual} —— ` +
      `請改用 extractError(e, fallback)。\n  ${perFile.join('\n  ')}`,
  )
})

function walkSources(dir) {
  const out = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = `${dir}${entry.name}${entry.isDirectory() ? '/' : ''}`
    if (entry.isDirectory()) out.push(...walkSources(full))
    else if (/\.(vue|js)$/.test(entry.name)) out.push(full)
  }
  return out
}

// ── W3-3⑤:追蹤碼附在句尾 ─────────────────────────────────────────────────────
//
// `extractRequestId` 先前定義了卻從沒被呼叫,所以 governance 的錯誤訊息從來不帶
// 追蹤碼。而 admin 正是最需要它的人 —— 他回報問題時念得出這個字串,支援端就
// grep 得到同一個請求的 access log。

test('extractError 在有 request_id 時把追蹤碼附在句尾', () => {
  const err = {
    response: {
      data: {
        error: { code: 'UPSTREAM_TIMEOUT', message: '上游逾時', request_id: 'abc123' },
        detail: '上游逾時',
      },
    },
  }
  assert.equal(extractError(err, '失敗'), '上游逾時（追蹤碼 abc123）')
})

test('沒有 request_id 時訊息維持原樣(不留空括號)', () => {
  const err = { response: { data: { error: { code: 'X', message: '上游逾時' } } } }
  assert.equal(extractError(err, '失敗'), '上游逾時')
})

test('走 legacy detail 或 fallback 時同樣會附追蹤碼', () => {
  const legacy = {
    response: { data: { detail: '舊式訊息', error: { request_id: 'r1' } } },
  }
  assert.equal(extractError(legacy, '失敗'), '舊式訊息（追蹤碼 r1）')

  const onlyId = { response: { data: { error: { request_id: 'r2' } } } }
  assert.equal(extractError(onlyId, '載入失敗'), '載入失敗（追蹤碼 r2）')
})

test('措辭與 shell 對齊(全形括號 + 「追蹤碼」)', () => {
  // 使用者會在同一個平台的不同介面看到它;兩種寫法會讓人以為是兩種東西。
  const err = { response: { data: { error: { message: 'x', request_id: 'q' } } } }
  const out = extractError(err, 'f')
  assert.ok(out.includes('（追蹤碼 '), out)
  assert.ok(out.endsWith('）'), out)
})
