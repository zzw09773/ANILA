// 快速起步骨架與進階實作範例是兩個下載。
// 預設入口直接下載通用包，並依序說明：下載、lab 開發、port forwarding 後註冊、回填 agent id。
// 檔名只來自 Content-Disposition；503 等失敗不得說成已下載。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'
import './helpers/dom.mjs'
import * as vueRuntime from 'vue'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRouter, createWebHistory } from 'vue-router'
import { calls, resetAgentDownloadClient, setGetImpl } from './helpers/agentDownloadClientMock.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const MOCK = fileURLToPath(new URL('./helpers/agentDownloadClientMock.mjs', import.meta.url))
const require = createRequire(import.meta.url)
const PINIA_URL = pathToFileURL(require.resolve('pinia')).href

const VIEW_SRC = readFileSync(resolve(ROOT, 'src/views/DeveloperAgentsView.vue'), 'utf8')
const GUARD_SRC = readFileSync(resolve(ROOT, 'src/components/agents/AgentGuardPanel.vue'), 'utf8')
const API_SRC = readFileSync(resolve(ROOT, 'src/api/agents.js'), 'utf8')

const QUICK_NAME = 'anila-agent-quickstart-0.1.0-py313-linux-x86_64.zip'
const ADVANCED_NAME = 'anila-agent-advanced-example-0.1.0.zip'

let viewMod
const piniaRuntime = await import(PINIA_URL)

function blobError(status, detail) {
  const body = new Blob([JSON.stringify({ detail })], { type: 'application/json' })
  return Object.assign(new Error(`HTTP ${status}`), {
    response: { status, data: body },
  })
}

function disposition(filename) {
  return { 'content-disposition': `attachment; filename="${filename}"` }
}

function okZip(filename) {
  return async () => ({
    data: new Blob(['PK'], { type: 'application/zip' }),
    headers: disposition(filename),
  })
}

async function compile(entry) {
  const result = await build({
    configFile: false,
    root: ROOT,
    plugins: [{
      name: 'agent-download-test-client',
      enforce: 'pre',
      resolveId(id, importer) {
        if ((id === './client' && importer?.includes('/api/')) || /\/api\/client(?:\.js)?$/.test(id)) {
          return { id: 'agent-download-test-client', external: true }
        }
      },
    }, vue()],
    logLevel: 'error',
    build: {
      write: false,
      minify: false,
      lib: { entry, formats: ['es'] },
      rollupOptions: {
        external: (id) => id === 'vue' || id === 'pinia' || id === 'agent-download-test-client' || id.startsWith('\0'),
        output: {
          inlineDynamicImports: true,
          paths: {
            vue: pathToFileURL(require.resolve('vue')).href,
            pinia: PINIA_URL,
            'agent-download-test-client': pathToFileURL(MOCK).href,
          },
        },
      },
    },
  })
  const chunk = (Array.isArray(result) ? result[0] : result).output.find((item) => item.type === 'chunk' && item.isEntry)
  return import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)
}

async function flush() {
  for (let i = 0; i < 8; i += 1) {
    await Promise.resolve()
    await vueRuntime.nextTick()
  }
}

function mount(mod) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const pinia = piniaRuntime.createPinia()
  const router = createRouter({
    history: createWebHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  })
  const app = vueRuntime.createApp(mod.default)
  app.use(pinia)
  app.use(router)
  app.mount(el)
  return { el, app }
}

function buttonsNamed(text) {
  return [...document.querySelectorAll('button')].filter((button) => button.textContent.includes(text))
}

function click(text) {
  const button = buttonsNamed(text)[0]
  assert.ok(button, `找不到按鈕：${text}`)
  button.click()
}

const savedNames = []

function downloads() {
  return savedNames
}

test.before(async () => {
  viewMod = await compile(resolve(ROOT, 'src/views/DeveloperAgentsView.vue'))
})

test.beforeEach(() => {
  resetAgentDownloadClient()
  setGetImpl('/api/agents', async () => ({
    data: [{
      id: 42,
      name: 'hr-policy-helper',
      endpoint_url: 'https://agent.example/v1',
      approval_status: 'pending',
      health_status: 'unknown',
      created_at: '2026-09-23T00:00:00Z',
    }],
  }))
  setGetImpl('/api/models', async () => ({ data: [] }))
  document.body.replaceChildren()
  savedNames.length = 0
  URL.createObjectURL = () => 'blob:agent-download'
  URL.revokeObjectURL = () => {}
  HTMLAnchorElement.prototype.click = function recordDownload() {
    if (this.download) savedNames.push(this.download)
  }
})

test.afterEach(() => {
  document.body.replaceChildren()
})

test('agents.js 把兩個下載分開，快速骨架才接受 agent_id', () => {
  assert.match(API_SRC, /export (async )?function downloadQuickstart|export const downloadQuickstart/)
  assert.match(API_SRC, /['"]\/api\/agents\/template\/download['"]/)
  assert.match(API_SRC, /['"]\/api\/agents\/examples\/advanced\/download['"]/)
  assert.match(API_SRC, /agent_id/)
  assert.doesNotMatch(
    API_SRC.slice(API_SRC.indexOf('/api/agents/examples/advanced/download')),
    /agent_id/,
  )
})

test('指南把通用包名稱當範例，並寫明註冊必填', () => {
  assert.doesNotMatch(VIEW_SRC, /不改預填名稱/)
  assert.match(VIEW_SRC, /範例名稱/)
  assert.match(VIEW_SRC, /至少 24 字/)
  assert.match(VIEW_SRC, /基礎模型/)
  assert.doesNotMatch(GUARD_SRC, /註冊只需名稱與 endpoint/)
  assert.match(GUARD_SRC, /至少 24 字/)
  assert.match(GUARD_SRC, /基礎模型/)
})

test('預設快速入口說明下載、lab 開發、註冊、回填 agent id', async () => {
  const { app } = mount(viewMod)
  await flush()
  const text = document.body.textContent
  assert.match(text, /下載/)
  assert.match(text, /MLSteam/)
  assert.match(text, /port forwarding/)
  assert.match(text, /註冊/)
  assert.match(text, /deployment\.env/)
  assert.doesNotMatch(VIEW_SRC, /頁首的快速起步尚未綁定/)
  assert.doesNotMatch(VIEW_SRC, /請先註冊助手，再從列表下載/)
  assert.doesNotMatch(VIEW_SRC, /anila-agent is CLI\/library, not a service/)
  assert.doesNotMatch(VIEW_SRC, /wrap in FastAPI/)
  assert.doesNotMatch(GUARD_SRC, /樣板套件尚未落地/)
  assert.match(GUARD_SRC, /先下載/)
  app.unmount()
})

test('頁首兩個下載清楚區分，一般優先打未綁定的快速骨架', async () => {
  setGetImpl('/api/agents/template/download', okZip(QUICK_NAME))
  setGetImpl('/api/agents/examples/advanced/download', okZip(ADVANCED_NAME))
  const { app } = mount(viewMod)
  await flush()
  const labels = [...document.querySelectorAll('.page-head button, .page-head__actions button')]
    .map((button) => button.textContent.trim())
  const quickAt = labels.findIndex((label) => /快速/.test(label))
  const advancedAt = labels.findIndex((label) => /進階/.test(label))
  assert.ok(quickAt >= 0 && advancedAt >= 0)
  assert.ok(quickAt < advancedAt)

  click('快速')
  await flush()
  const quick = calls.find((call) => call.url === '/api/agents/template/download')
  assert.equal(quick.config?.params, undefined)
  assert.deepEqual(downloads(), [QUICK_NAME])
  assert.match(document.body.textContent, /已下載/)
  app.unmount()
})

test('列表行內下載把數字 agent_id 傳給快速骨架', async () => {
  setGetImpl('/api/agents/template/download', okZip(QUICK_NAME))
  const { app } = mount(viewMod)
  await flush()
  const row = [...document.querySelectorAll('tr')].find((tr) => tr.textContent.includes('hr-policy-helper'))
  assert.ok(row)
  const button = [...row.querySelectorAll('button')].find((item) => /快速|專屬|骨架/.test(item.textContent))
  assert.ok(button)
  button.click()
  await flush()
  const quick = calls.find((call) => call.url === '/api/agents/template/download')
  assert.equal(quick.config.params.agent_id, 42)
  assert.equal(downloads().at(-1), QUICK_NAME)
  app.unmount()
})

test('進階範例走另一個端點，不用 agent_id，也不用快速骨架的檔名', async () => {
  setGetImpl('/api/agents/examples/advanced/download', okZip(ADVANCED_NAME))
  const { app } = mount(viewMod)
  await flush()
  click('進階')
  await flush()
  const advanced = calls.find((call) => call.url === '/api/agents/examples/advanced/download')
  assert.ok(advanced)
  assert.equal(advanced.config?.params?.agent_id, undefined)
  assert.equal(downloads().at(-1), ADVANCED_NAME)
  assert.notEqual(downloads().at(-1), 'anila-agent.zip')
  app.unmount()
})

test('503 如實顯示後端原因，不說已下載', async () => {
  setGetImpl('/api/agents/template/download', async () => {
    throw blobError(503, '快速起步骨架在此部署環境中無法取得')
  })
  const { app } = mount(viewMod)
  await flush()
  click('快速')
  await flush()
  const text = document.body.textContent
  assert.match(text, /無法取得/)
  assert.doesNotMatch(text, /已下載/)
  assert.equal(downloads().length, 0)
  app.unmount()
})

test('filename* 與沒有檔名都不可退回舊的 anila-agent.zip', async () => {
  setGetImpl('/api/agents/template/download', async () => ({
    data: new Blob(['PK']),
    headers: {
      'content-disposition': "attachment; filename*=UTF-8''anila-agent-quickstart-9.zip",
    },
  }))
  const { app } = mount(viewMod)
  await flush()
  click('快速')
  await flush()
  assert.equal(downloads().at(-1), 'anila-agent-quickstart-9.zip')

  setGetImpl('/api/agents/examples/advanced/download', async () => ({
    data: new Blob(['PK']),
    headers: {},
  }))
  click('進階')
  await flush()
  assert.equal(downloads().length, 1)
  assert.match(document.body.textContent, /檔名/)
  assert.doesNotMatch(document.body.textContent, /anila-agent\.zip/)
  app.unmount()
})

test('三級制第一級改指快速骨架，不再聲稱舊範例沒有 FastAPI', () => {
  assert.match(GUARD_SRC, /快速起步/)
  assert.match(GUARD_SRC, /進階實作範例/)
  assert.doesNotMatch(GUARD_SRC, /沒有 FastAPI|無 FastAPI|尚未內建 FastAPI/)
  assert.doesNotMatch(GUARD_SRC, /anila-agent is CLI/)
})
