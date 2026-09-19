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
import {
  calls,
  resetFeedbackClient,
  setListImpl,
  setConvImpl,
} from './helpers/feedbackClientMock.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const MOCK = fileURLToPath(new URL('./helpers/feedbackClientMock.mjs', import.meta.url))
const VIEW_SRC = readFileSync(resolve(ROOT, 'src/views/FeedbackView.vue'), 'utf8')

const branchMessages = [
  { id: 1, role: 'user', content: '活躍問句', parent_id: null },
  { id: 2, role: 'assistant', content: '活躍分支答（不該出現）', parent_id: 1 },
  { id: 3, role: 'user', content: '被評分那一問', parent_id: null },
  { id: 4, role: 'assistant', content: '被評分的非活躍答', parent_id: 3 },
  { id: 5, role: 'assistant', content: '同層另一答（不該出現）', parent_id: 3 },
]

function row(overrides = {}) {
  return {
    message_id: 4,
    conversation_id: 1,
    rating: 'down',
    rating_score: 2,
    comment: '離題',
    reasons: ['離題'],
    username: 'u1',
    agent_name: '規章助手',
    model_name: 'local-llm',
    classification_level: '一般',
    message_created_at: '2026-09-01T00:00:00+08:00',
    ...overrides,
  }
}

let View

async function loadView() {
  if (View) return View
  // Compile the browser SFC, not ssrLoadModule (which produces ssrRender).
  // Keep Vue and the mock external so the mounted component shares this test's instances.
  const require = createRequire(import.meta.url)
  const result = await build({
    configFile: false,
    root: ROOT,
    plugins: [{
      name: 'feedback-test-client',
      enforce: 'pre',
      resolveId(id, importer) {
        if ((id === './client' && importer?.includes('/api/')) || /\/api\/client(?:\.js)?$/.test(id)) {
          return { id: 'feedback-test-client', external: true }
        }
      },
    }, vue()],
    logLevel: 'error',
    build: {
      write: false,
      minify: false,
      lib: { entry: resolve(ROOT, 'src/views/FeedbackView.vue'), formats: ['es'] },
      rollupOptions: {
        external: ['vue', 'feedback-test-client'],
        output: { inlineDynamicImports: true, paths: {
          vue: pathToFileURL(require.resolve('vue')).href,
          'feedback-test-client': pathToFileURL(MOCK).href,
        } },
      },
    },
  })
  const chunk = (Array.isArray(result) ? result[0] : result).output.find((item) => item.type === 'chunk' && item.isEntry)
  View = (await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default
  return View
}

async function flush() {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

function mountView() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(View)
  app.mount(el)
  return { el, app }
}

function viewButtons() {
  return [...document.querySelectorAll('button')].filter((b) => b.textContent.includes('查看被評分回覆'))
}

test.before(async () => {
  await loadView()
})


test.afterEach(() => {
  document.body.replaceChildren()
  resetFeedbackClient()
})

test('FeedbackView 用插值顯示正文，沒有 v-html，並走 getConversationAll', () => {
  assert.doesNotMatch(VIEW_SRC, /v-html/)
  assert.match(VIEW_SRC, /getConversationAll/)
  assert.match(VIEW_SRC, /<TermModal/)
  assert.match(VIEW_SRC, /查看被評分回覆/)
})

test('掛載清單時不預抓對話正文；點了才 GET view=all', async () => {
  setListImpl(async () => ({ data: { items: [row()], summary: { total: 1, up: 0, down: 1, with_comment: 1 } } }))
  setConvImpl(async () => ({ data: { messages: branchMessages } }))
  const { app } = mountView()
  await flush()
  assert.equal(calls.filter((c) => String(c.url).includes('/api/conversations/')).length, 0)
  assert.equal(viewButtons().length, 1)
  viewButtons()[0].click()
  await flush()
  const convCalls = calls.filter((c) => String(c.url).includes('/api/conversations/'))
  assert.equal(convCalls.length, 1)
  assert.equal(convCalls[0].url, '/api/conversations/1')
  assert.deepEqual(convCalls[0].params, { view: 'all' })
  assert.match(document.body.textContent, /被評分的非活躍答/)
  assert.match(document.body.textContent, /被評分那一問/)
  assert.doesNotMatch(document.body.textContent, /活躍分支答/)
  assert.doesNotMatch(document.body.textContent, /同層另一答/)
  app.unmount()
})

test('目標缺失時顯示 not-found，不是成功正文', async () => {
  setListImpl(async () => ({ data: { items: [row({ message_id: 99 })], summary: { total: 1, up: 0, down: 1, with_comment: 0 } } }))
  setConvImpl(async () => ({ data: { messages: branchMessages } }))
  const { app } = mountView()
  await flush()
  viewButtons()[0].click()
  await flush()
  assert.ok(document.querySelector('[data-testid="feedback-reply-missing"]'))
  assert.equal(document.querySelector('[data-testid="feedback-reply-rated"]'), null)
  assert.doesNotMatch(document.body.textContent, /被評分的非活躍答/)
  app.unmount()
})

test('拒絕／伺服器錯誤不得裝成成功，可重試', async () => {
  setListImpl(async () => ({ data: { items: [row()], summary: { total: 1, up: 0, down: 1, with_comment: 0 } } }))
  let convHits = 0
  setConvImpl(async () => {
    convHits += 1
    if (convHits === 1) {
      throw Object.assign(new Error('denied'), {
        response: { status: 403, data: { detail: '權限不足' } },
      })
    }
    return { data: { messages: branchMessages } }
  })
  const { app } = mountView()
  await flush()
  viewButtons()[0].click()
  await flush()
  const err = document.querySelector('[data-testid="feedback-reply-error"]')
  assert.ok(err)
  assert.match(err.textContent, /權限不足/)
  assert.equal(document.querySelector('[data-testid="feedback-reply-rated"]'), null)
  document.querySelector('[data-testid="feedback-reply-retry"]').click()
  await flush()
  assert.match(document.body.textContent, /被評分的非活躍答/)
  assert.equal(convHits, 2)
  app.unmount()
})

test('關閉後過期的 GET 不得把正文寫回', async () => {
  let release
  const hold = new Promise((resolve) => { release = resolve })
  setListImpl(async () => ({ data: { items: [row()], summary: { total: 1, up: 0, down: 1, with_comment: 0 } } }))
  setConvImpl(async () => {
    await hold
    return { data: { messages: branchMessages } }
  })
  const { app } = mountView()
  await flush()
  viewButtons()[0].click()
  await flush()
  assert.ok(document.querySelector('[data-testid="feedback-reply-loading"]'))
  document.querySelector('[aria-label="關閉"]')?.click()
  await flush()
  release()
  await flush()
  await hold
  await flush()
  assert.equal(document.querySelector('[data-testid="feedback-reply-rated"]'), null)
  assert.doesNotMatch(document.body.textContent, /被評分的非活躍答/)
  app.unmount()
})

test('改看另一列時，先開的回應不得覆蓋後開的', async () => {
  let releaseA
  const holdA = new Promise((resolve) => { releaseA = resolve })
  setListImpl(async () => ({
    data: {
      items: [row(), row({ message_id: 21, conversation_id: 2, rating: 'up', rating_score: 8 })],
      summary: { total: 2, up: 1, down: 1, with_comment: 1 },
    },
  }))
  setConvImpl(async (url) => {
    if (String(url).includes('/2')) {
      return {
        data: {
          messages: [
            { id: 20, role: 'user', content: '第二問', parent_id: null },
            { id: 21, role: 'assistant', content: '第二答', parent_id: 20 },
          ],
        },
      }
    }
    await holdA
    return { data: { messages: branchMessages } }
  })
  const { app } = mountView()
  await flush()
  const buttons = viewButtons()
  assert.equal(buttons.length, 2)
  buttons[0].click()
  await flush()
  buttons[1].click()
  await flush()
  releaseA()
  await holdA
  await flush()
  assert.match(document.body.textContent, /第二答/)
  assert.match(document.body.textContent, /第二問/)
  assert.doesNotMatch(document.body.textContent, /被評分的非活躍答/)
  app.unmount()
})
