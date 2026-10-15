import test from 'node:test'
import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'
import './helpers/dom.mjs'
import { createApp, h, nextTick } from 'vue'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import {
  holdNextCreate,
  holdNextTrustedHost,
  postCount,
  rejectCreateAsHostNotTrusted,
  resetModelFormClient,
} from './helpers/modelFormClientMock.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const MOCK = fileURLToPath(new URL('./helpers/modelFormClientMock.mjs', import.meta.url))

let View
let piniaRuntime
let mounted = []

async function loadView() {
  if (View) return View
  const require = createRequire(import.meta.url)
  const piniaUrl = pathToFileURL(require.resolve('pinia')).href
  const result = await build({
    configFile: false,
    root: ROOT,
    plugins: [{
      name: 'model-form-test-client',
      enforce: 'pre',
      resolveId(id, importer) {
        if ((id === './client' && importer?.includes('/api/')) || /\/api\/client(?:\.js)?$/.test(id)) {
          return { id: 'model-form-client', external: true }
        }
      },
    }, vue()],
    logLevel: 'error',
    build: {
      write: false,
      minify: false,
      lib: { entry: resolve(ROOT, 'src/views/ModelsView.vue'), formats: ['es'] },
      rollupOptions: {
        external: ['vue', 'pinia', 'model-form-client'],
        output: {
          inlineDynamicImports: true,
          paths: {
            vue: pathToFileURL(require.resolve('vue')).href,
            pinia: piniaUrl,
            'model-form-client': pathToFileURL(MOCK).href,
          },
        },
      },
    },
  })
  const chunk = (Array.isArray(result) ? result[0] : result).output.find((item) => item.type === 'chunk' && item.isEntry)
  piniaRuntime = await import(piniaUrl)
  View = (await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default
  return View
}

function buttonByLabel(label) {
  return [...document.querySelectorAll('button')].find((button) => button.textContent.trim() === label)
}

function fill(placeholder, value) {
  const input = document.querySelector(`input[placeholder="${placeholder}"]`)
  assert.ok(input, `missing input ${placeholder}`)
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mountView() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(View)
  const pinia = piniaRuntime.createPinia()
  app.use(pinia)
  app.component('router-link', {
    props: ['to'],
    setup(_, { slots }) {
      return () => h('a', slots.default?.())
    },
  })
  app.mount(el)
  mounted.push(app)
  return { el, app, pinia }
}

async function openCreateForm() {
  buttonByLabel('註冊模型').click()
  await nextTick()
  fill('llama3-70b', 'glm-local')
  fill('Llama 3 70B Instruct', '院內模型')
  fill('http://gemma4:8000/v1', 'http://10.1.2.3:8000/v1')
  await nextTick()
}

test.before(async () => {
  await loadView()
})

test.afterEach(() => {
  for (const app of mounted) app.unmount()
  mounted = []
  document.body.replaceChildren()
  resetModelFormClient()
})

test('對話選單渲染共用 badge、保留可及名稱與可使用對象動作', async () => {
  const { el, pinia } = mountView()
  await settle()
  pinia._s.get('models').models = [{
    id: 71, name: 'test-model', display_name: '測試模型', model_type: 'llm',
    router_enabled: true, is_router_primary: true, is_slides_primary: true,
    is_platform_embedding: true, is_active: true, health_status: 'healthy',
  }]
  await nextTick()
  const group = el.querySelector('[role="group"][aria-label="對話選單標籤"]')
  assert.ok(group)
  assert.deepEqual([...group.querySelectorAll('.term-badge')].map((badge) => badge.textContent.trim()), [
    'Router', '★ 全院預設', '★ 主簡報', '★ 主 embedding',
  ])
  assert.ok(group.querySelector('.term-badge--warn[title="全院 Router 預設基礎模型"]'))
  const audience = buttonByLabel('可使用對象')
  assert.ok(audience)
  audience.click()
  await settle()
  assert.ok(buttonByLabel('更新'))
  assert.ok(document.getElementById('model-grant-editor'))
})

test('Router only 與平台入口不混入破折號，無標籤才顯示破折號', async () => {
  const { el, pinia } = mountView()
  await settle()
  pinia._s.get('models').models = [
    { id: 81, name: 'router-only', router_enabled: true },
    { id: 82, name: 'anila-router', router_enabled: false },
    { id: 83, name: 'plain', router_enabled: false },
  ].map((model) => ({ ...model, display_name: model.name, model_type: 'llm', is_active: true, health_status: 'healthy' }))
  await nextTick()
  const groups = [...el.querySelectorAll('.chat-menu-badges')]
  assert.deepEqual(groups.map((group) => group.textContent.trim()), ['Router', '—'])
  assert.deepEqual(groups.map((group) => group.querySelectorAll('.term-badge').length), [1, 0])
  // 平台入口依既有契約獨立於註冊模型表，不因補標籤而放回 CRUD 列。
  assert.ok(el.querySelector('[data-platform-chat-entry]'))
  assert.equal(el.querySelector('[data-platform-chat-entry] .chat-menu-badges'), null)
})

test('註冊送出進行中時，按鈕停用且再按一次不會再送出', async () => {
  holdNextCreate()
  mountView()
  await settle()
  await openCreateForm()
  const submit = buttonByLabel('註冊')
  assert.equal(submit.disabled, false)
  submit.click()
  submit.click()
  assert.equal(postCount('/api/models'), 1)
  await nextTick()
  assert.equal(buttonByLabel('註冊').disabled, true)
})

test('加入信任主機並重試進行中時，註冊鈕停用且 handleSubmit 直接返回', async () => {
  rejectCreateAsHostNotTrusted()
  holdNextTrustedHost()
  mountView()
  await settle()
  await openCreateForm()
  buttonByLabel('註冊').click()
  await settle()
  const retry = buttonByLabel('加入信任主機並重試')
  assert.ok(retry, 'retry action is shown')
  assert.equal(retry.disabled, false)
  const modelsBefore = postCount('/api/models')
  retry.click()
  buttonByLabel('註冊').click()
  assert.equal(postCount('/api/models'), modelsBefore)
  await nextTick()
  assert.equal(buttonByLabel('註冊').disabled, true)
  assert.equal(buttonByLabel('加入信任主機並重試').disabled, true)
})
