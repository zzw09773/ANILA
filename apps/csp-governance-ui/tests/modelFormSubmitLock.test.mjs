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
  app.use(piniaRuntime.createPinia())
  app.component('router-link', {
    props: ['to'],
    setup(_, { slots }) {
      return () => h('a', slots.default?.())
    },
  })
  app.mount(el)
  mounted.push(app)
  return { el, app }
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
