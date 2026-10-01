import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  normalizeServiceUrl,
  probeResultText,
  readAsrHealth,
  serviceUrlsMatch,
  SPEECH_GATEWAY_APPLIED,
  SPEECH_GATEWAY_DISABLED,
  SPEECH_GATEWAY_PENDING,
  SPEECH_GATEWAY_POLL_DEADLINE_MS,
  SPEECH_GATEWAY_POLL_MS,
  SPEECH_GATEWAY_UNCONFIRMED,
  SPEECH_GATEWAY_UNCONFIGURED,
  speechGatewayMessage,
  speechSaveFollowUp,
  watchSpeechGateway,
} from '../src/utils/externalServiceStatus.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const view = readFileSync(resolve(root, 'src/views/ExternalServicesView.vue'), 'utf8')
const router = readFileSync(resolve(root, 'src/router/index.js'), 'utf8')
const models = readFileSync(resolve(root, 'src/views/ModelsView.vue'), 'utf8')

test('外部服務頁說明原生解析器退路，憑證欄是密碼框', () => {
  assert.match(view, /fallback_note/)
  assert.match(view, /內建原生解析器/)
  assert.match(view, /type="password"/)
  assert.doesNotMatch(view, /\{\{\s*item\.credential\s*\}\}/)
  assert.match(router, /external-services/)
})

test('模型頁不再把主語音按鈕當成解碼位址', () => {
  assert.doesNotMatch(models, /設為主語音辨識/)
  assert.match(models, /外部服務/)
})

test('探測把回傳的狀態寫回畫面，存檔後改問閘道實際位址', () => {
  const status = readFileSync(resolve(root, 'src/utils/externalServiceStatus.js'), 'utf8')
  assert.match(view, /label="探測"/)
  assert.match(view, /probeExternalService/)
  assert.match(view, /applyService/)
  assert.match(view, /probeResultText/)
  assert.match(view, /watchSpeechGateway/)
  assert.match(view, /readAsrHealth/)
  assert.match(view, /speechSaveFollowUp/)
  assert.match(view, /readAsrHealth\(client,\s*options\)/)
  assert.match(view, /onBeforeUnmount/)
  assert.match(status, /\/asr\/health/)
  assert.match(status, /語音閘道已改用新位址/)
  assert.match(status, /語音閘道仍在使用舊位址，通常 1～2 分鐘內會更新/)
  assert.match(status, /語音閘道 5 分鐘內未改用新位址，請檢查語音閘道日誌/)
  assert.doesNotMatch(view, /語音閘道約/)
  assert.doesNotMatch(view, /gateway_apply_within_seconds/)
  assert.doesNotMatch(status, /語音閘道約/)
  assert.doesNotMatch(status, /gateway_apply_within_seconds/)
})

test('探測結果用中文寫健康或失敗', () => {
  assert.equal(probeResultText({ health_status: 'healthy' }), '健康')
  assert.equal(
    probeResultText({ health_status: 'unhealthy', health_detail: 'HTTP 503' }),
    '失敗 — HTTP 503',
  )
  assert.equal(probeResultText({ health_status: 'unhealthy' }), '失敗')
  assert.equal(probeResultText({ health_status: 'disabled' }), '未啟用')
  assert.equal(probeResultText({ health_status: 'unknown' }), '未知')
})

test('閘道位址比對去掉憑證，503 的 decode_url 仍要讀得到', async () => {
  assert.equal(SPEECH_GATEWAY_POLL_MS, 10_000)
  assert.equal(SPEECH_GATEWAY_POLL_DEADLINE_MS, 5 * 60 * 1000)
  const saved = 'https://user:s3cret@ASR.Example.test:9000/'
  const reported = 'https://asr.example.test:9000'
  assert.equal(serviceUrlsMatch(saved, reported), true)
  assert.equal(normalizeServiceUrl(saved).includes('s3cret'), false)
  assert.equal(normalizeServiceUrl(saved).includes('user'), false)
  assert.equal(serviceUrlsMatch(saved, 'https://old.example.test:9000'), false)
  assert.equal(serviceUrlsMatch('', ''), true)
  assert.equal(speechGatewayMessage(true, 0), SPEECH_GATEWAY_APPLIED)
  assert.equal(speechGatewayMessage(false, 10_000), SPEECH_GATEWAY_PENDING)
  assert.equal(speechGatewayMessage(false, SPEECH_GATEWAY_POLL_DEADLINE_MS), SPEECH_GATEWAY_UNCONFIRMED)
  assert.equal(speechGatewayMessage(true, SPEECH_GATEWAY_POLL_DEADLINE_MS), SPEECH_GATEWAY_APPLIED)

  const calls = []
  const http = {
    async get(url, config) {
      calls.push({ url, config })
      assert.equal(config.validateStatus(503), true)
      return {
        status: 503,
        data: { decode_url: 'https://user:s3cret@asr.example.test:9000', decode_credential_source: 'csp_registry' },
      }
    },
  }
  const body = await readAsrHealth(http)
  assert.equal(calls[0].url, '/asr/health')
  assert.equal(serviceUrlsMatch('https://asr.example.test:9000', body.decode_url), true)
  assert.equal(normalizeServiceUrl(body.decode_url).includes('s3cret'), false)
})

test('存檔後每 10 秒問一次，對上就停，五分鐘還沒對上就改口', async () => {
  const seen = []
  const timers = []
  let now = 0
  let reported = 'https://old.example.test:9000'
  const stop = watchSpeechGateway({
    savedBaseUrl: 'https://user:s3cret@new.example.test:9000/',
    fetchHealth: async () => ({ decode_url: reported }),
    onStatus: (text) => seen.push(text),
    now: () => now,
    schedule: (fn, ms) => {
      timers.push({ fn, ms })
      return timers.length
    },
    cancel: () => {},
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(seen.at(-1), SPEECH_GATEWAY_PENDING)
  assert.equal(timers.length, 1)
  assert.equal(timers[0].ms, 10_000)

  now = 10_000
  reported = 'https://new.example.test:9000'
  timers[0].fn()
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(seen.at(-1), SPEECH_GATEWAY_APPLIED)
  assert.equal(timers.length, 1)
  stop()

  const pending = []
  const later = []
  let clock = 0
  watchSpeechGateway({
    savedBaseUrl: 'https://new.example.test:9000',
    fetchHealth: async () => ({ decode_url: 'https://old.example.test:9000' }),
    onStatus: (text) => pending.push(text),
    now: () => clock,
    schedule: (fn, ms) => {
      later.push({ fn, ms })
      return later.length
    },
    cancel: () => {},
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(pending.at(-1), SPEECH_GATEWAY_PENDING)
  clock = 5 * 60 * 1000
  later[0].fn()
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(pending.at(-1), SPEECH_GATEWAY_UNCONFIRMED)
  assert.equal(later.length, 1)
})

test('停用與未設定要各自回報，空位址不能算已改用', async () => {
  assert.deepEqual(
    speechSaveFollowUp({ enabled: false, configured: false, base_url: 'https://asr.example.test:9000' }),
    { text: SPEECH_GATEWAY_DISABLED, watch: false },
  )
  assert.deepEqual(
    speechSaveFollowUp({ enabled: true, configured: false, base_url: '' }),
    { text: SPEECH_GATEWAY_UNCONFIGURED, watch: false },
  )
  assert.deepEqual(
    speechSaveFollowUp({ enabled: true, configured: true, base_url: 'https://asr.example.test:9000/' }),
    { text: SPEECH_GATEWAY_PENDING, watch: true },
  )

  const seen = []
  let fetches = 0
  watchSpeechGateway({
    savedBaseUrl: 'https://asr.example.test:9000',
    enabled: false,
    configured: false,
    fetchHealth: async () => {
      fetches += 1
      return { decode_url: '' }
    },
    onStatus: (text) => seen.push(text),
    schedule: () => 1,
    cancel: () => {},
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(fetches, 0)
  assert.equal(seen.at(-1), SPEECH_GATEWAY_DISABLED)
  assert.notEqual(seen.at(-1), SPEECH_GATEWAY_UNCONFIRMED)
  assert.notEqual(seen.at(-1), SPEECH_GATEWAY_APPLIED)

  const emptySeen = []
  watchSpeechGateway({
    savedBaseUrl: '',
    enabled: true,
    configured: false,
    fetchHealth: async () => {
      fetches += 1
      return { decode_url: '' }
    },
    onStatus: (text) => emptySeen.push(text),
    schedule: () => 1,
    cancel: () => {},
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(fetches, 0)
  assert.equal(emptySeen.at(-1), SPEECH_GATEWAY_UNCONFIGURED)
  assert.notEqual(emptySeen.at(-1), SPEECH_GATEWAY_APPLIED)
})

test('health 請求的 timeout 不超過剩餘時間，停止時會中止', async () => {
  const calls = []
  const timers = []
  let now = 0
  const deadlineMs = 5_000
  let pending
  const stop = watchSpeechGateway({
    savedBaseUrl: 'https://new.example.test/v1',
    enabled: true,
    configured: true,
    deadlineMs,
    now: () => now,
    fetchHealth: (options) => {
      calls.push(options)
      assert.ok(options?.signal)
      assert.equal(typeof options.timeout, 'number')
      assert.ok(options.timeout > 0)
      assert.ok(options.timeout <= deadlineMs - (now - 0))
      pending = options
      if (calls.length === 1) {
        return new Promise((resolve, reject) => {
          options.signal.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), { code: 'ERR_CANCELED' }))
          })
        })
      }
      return Promise.resolve({ decode_url: 'https://old.example.test/v1' })
    },
    onStatus: () => {},
    schedule: (fn) => {
      timers.push(fn)
      return timers.length
    },
    cancel: () => {},
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(calls.length, 1)
  assert.equal(calls[0].timeout, deadlineMs)
  stop()
  assert.equal(pending.signal.aborted, true)

  const followUp = []
  let clock = 0
  watchSpeechGateway({
    savedBaseUrl: 'https://new.example.test/v1',
    enabled: true,
    configured: true,
    deadlineMs,
    now: () => clock,
    fetchHealth: async (options) => {
      followUp.push(options.timeout)
      return { decode_url: 'https://old.example.test/v1' }
    },
    onStatus: () => {},
    schedule: (fn) => {
      timers.push(fn)
      return timers.length
    },
    cancel: () => {},
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(followUp[0], deadlineMs)
  clock = 1_000
  timers.at(-1)()
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(followUp[1], deadlineMs - 1_000)

  const controller = new AbortController()
  const seen = []
  const http = {
    async get(url, config) {
      seen.push({ url, config })
      return { status: 200, data: { decode_url: 'https://asr.example.test:9000' } }
    },
  }
  await readAsrHealth(http, { signal: controller.signal, timeout: 1500 })
  assert.equal(seen[0].url, '/asr/health')
  assert.equal(seen[0].config.signal, controller.signal)
  assert.equal(seen[0].config.timeout, 1500)
  assert.equal(seen[0].config.validateStatus(503), true)
})
