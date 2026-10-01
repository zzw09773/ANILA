import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import './helpers/dom.mjs'
import { createApp, nextTick } from 'vue'
import { build } from 'vite'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { calls, resetSkillClient, setSkillList } from './helpers/skillClientMock.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const MOCK = fileURLToPath(new URL('./helpers/skillClientMock.mjs', import.meta.url))

const routerSrc = readFileSync(resolve(ROOT, 'src/router/index.js'), 'utf8')
const sidebarSrc = readFileSync(resolve(ROOT, 'src/components/layout/AppSidebar.vue'), 'utf8')
const deputySrc = readFileSync(resolve(ROOT, 'src/router/deputyPages.js'), 'utf8')

let View

async function loadView() {
  if (View) return View
  const require = createRequire(import.meta.url)
  const result = await build({
    configFile: false,
    root: ROOT,
    plugins: [{
      name: 'skill-test-client',
      enforce: 'pre',
      resolveId(id, importer) {
        if ((id === './client' && importer?.includes('/api/')) || /\/api\/client(?:\.js)?$/.test(id)) {
          return { id: 'skill-test-client', external: true }
        }
      },
    }, vue()],
    logLevel: 'error',
    build: {
      write: false,
      minify: false,
      lib: { entry: resolve(ROOT, 'src/views/SkillReviewView.vue'), formats: ['es'] },
      rollupOptions: {
        external: ['vue', 'skill-test-client'],
        output: {
          inlineDynamicImports: true,
          paths: {
            vue: pathToFileURL(require.resolve('vue')).href,
            'skill-test-client': pathToFileURL(MOCK).href,
          },
        },
      },
    },
  })
  const chunk = (Array.isArray(result) ? result[0] : result).output.find((item) => item.type === 'chunk' && item.isEntry)
  View = (await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default
  return View
}

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mount() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(View)
  app.mount(el)
  return { el, app }
}

function button(label) {
  return [...document.querySelectorAll('button')].find((node) => node.textContent.includes(label))
}

test.before(async () => {
  await loadView()
})

test.afterEach(() => {
  document.body.replaceChildren()
  resetSkillClient()
})

test('導覽有 skill 審核，代理管理員的頁面清單沒有這頁', () => {
  assert.match(routerSrc, /skill-review/)
  assert.match(routerSrc, /requiresSkillReview/)
  assert.match(sidebarSrc, /skill 審核/)
  assert.equal(deputySrc.includes('skill-review'), false)
  assert.equal(deputySrc.includes('skill 審核'), false)
})

test('待審列可以核准，退回必須有理由，已發布可以下架', async () => {
  setSkillList(async () => ({
    data: {
      pending: [{
        version_id: 11,
        lineage_id: 4,
        name: '東區週會',
        description: '整理週會',
        body: '三點列出',
        scope: 'unit',
        department_name: '東區',
        owner_username: 'ada',
        status: 'pending',
      }],
      published: [{
        version_id: 12,
        lineage_id: 5,
        name: '全院格式',
        description: '公文格式',
        body: '用院內格式',
        scope: 'campus',
        status: 'published',
      }],
    },
  }))
  const view = mount()
  await settle()
  assert.match(document.body.textContent, /東區週會/)
  assert.match(document.body.textContent, /全院格式/)

  const reject = button('退回')
  assert.equal(reject.disabled, true)
  reject.click()
  await settle()
  assert.equal(calls.some((call) => call.method === 'post'), false)

  document.querySelector('textarea').value = '請補適用範圍'
  document.querySelector('textarea').dispatchEvent(new Event('input'))
  await settle()
  assert.equal(button('退回').disabled, false)
  button('退回').click()
  await settle()
  const rejected = calls.find((call) => call.url === '/api/skills/reviews/11/reject')
  assert.equal(rejected.body.reason, '請補適用範圍')

  button('核准').click()
  await settle()
  assert.equal(calls.some((call) => call.url === '/api/skills/reviews/11/approve'), true)

  button('下架').click()
  await settle()
  assert.equal(calls.some((call) => call.url === '/api/skills/reviews/12/unpublish'), true)
  view.app.unmount()
})
