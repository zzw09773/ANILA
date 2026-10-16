import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/dom.mjs'
import { createApp, h, nextTick } from 'vue'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { resolve, dirname } from 'node:path'
import { pending } from './helpers/bannerIdentityMock.mjs'
import { announceIdentity, invalidateIdentity, currentIdentityEpoch } from '../src/api/identityEpoch.js'
import { refreshOpenAlertBanner } from '../src/utils/openAlertBanner.js'
import { subscribeUnreadFeedback, publishUnreadFeedback } from '../src/utils/unreadFeedbackBanner.js'
import { subscribeInactivityNotice } from '../src/utils/inactivityNotice.js'
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(import.meta.url)
let app
let host
let components
const settle = async () => { for (let i = 0; i < 12; i++) { await Promise.resolve(); await nextTick() } }
const summary = n => ({ open_count: n, highest_open_severity: 'high', unread_feedback_count: n, inactivity_notice: { phase: 'preview', count: n, days: 180 } })
function mount() {
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h('div', components.map(c => h(c))) })
  app.component('router-link', { props: ['to'], setup: (p, { slots }) => () => h('a', { href: p.to }, slots.default?.()) })
  app.mount(host)
}
function unmount() { app?.unmount(); host?.remove(); app = null }
function empty() { assert.equal(document.querySelectorAll('[data-testid$="-banner"]').length, 0) }
async function deliver(n) { assert.ok(pending.length); pending.shift().resolve({ data: summary(n) }); await settle() }
function counts(n) {
  assert.equal(document.querySelectorAll('[data-testid$="-banner"]').length, 3)
  assert.match(document.querySelector('[data-testid="unread-feedback-banner"]').textContent, new RegExp(`${n} 筆`))
}
test.before(async () => {
  components = []
  for (const name of ['OpenAlertBanner', 'UnreadFeedbackBanner', 'InactivityNoticeBanner']) {
    const result = await build({ configFile: false, envDir: false, css: { postcss: { plugins: [] } }, root: ROOT, logLevel: 'silent', plugins: [{
      name: 'banner-mocks', enforce: 'pre', resolveId(id) {
        if (id.endsWith('/api/alerts')) return { id: new URL('./helpers/bannerIdentityMock.mjs', import.meta.url).href, external: true }
        for (const util of ['unreadFeedbackBanner', 'inactivityNotice', 'openAlertBanner']) {
          if (id.endsWith(`/utils/${util}`)) return { id: new URL(`../src/utils/${util}.js`, import.meta.url).href, external: true }
        }
      }
    }, vue()], build: { write: false, minify: false, lib: { entry: resolve(ROOT, `src/components/layout/${name}.vue`), formats: ['es'] }, rollupOptions: { external: ['vue'], output: { inlineDynamicImports: true, paths: { vue: pathToFileURL(require.resolve('vue')).href } } } } })
    const chunk = (Array.isArray(result) ? result[0] : result).output.find(c => c.type === 'chunk' && c.isEntry)
    components.push((await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default)
  }
})
test.afterEach(async () => { unmount(); invalidateIdentity(); pending.splice(0); await settle() })
test('first pending summary with unknown actor cannot bind A after direct positive B', async () => {
  mount(); const old = pending.shift(); assert.ok(old)
  announceIdentity('B'); await settle(); empty()
  old.resolve({ data: summary(99) }); await settle(); empty()
  await deliver(2); counts(2)
})
test('direct actor switch clears mounted models/cache and fences A reply while B waits', async () => {
  announceIdentity('A'); mount(); await deliver(7); counts(7)
  refreshOpenAlertBanner(); const old = pending.shift()
  announceIdentity('B'); await settle(); empty()
  let feedback, inactivity
  const off1 = subscribeUnreadFeedback(x => { feedback = x })
  const off2 = subscribeInactivityNotice(x => { inactivity = x })
  assert.equal(feedback, null); assert.equal(inactivity, null)
  old.resolve({ data: summary(99) }); await settle(); empty()
  await deliver(2); counts(2); off1(); off2()
})
test('invalidation clears all summaries synchronously and blocks polling until fresh actor', async () => {
  announceIdentity('A'); mount(); await deliver(8)
  refreshOpenAlertBanner(); const old = pending.shift()
  invalidateIdentity(); await settle(); empty()
  refreshOpenAlertBanner(); assert.equal(pending.length, 0)
  old.resolve({ data: summary(99) }); await settle(); empty()
  announceIdentity('B'); await deliver(1); counts(1)
})
test('pagehide clears caches without broadcasting epoch and remount cannot replay A', async () => {
  announceIdentity('A'); mount(); await deliver(9)
  const epoch = currentIdentityEpoch()
  window.dispatchEvent(new Event('pagehide')); await settle(); empty()
  assert.equal(currentIdentityEpoch(), epoch)
  unmount(); mount(); await settle(); empty(); assert.equal(pending.length, 0)
  window.dispatchEvent(new Event('pageshow')); await settle(); empty()
  announceIdentity('A'); await deliver(3); counts(3)
})
for (const status of [401, 403, 404]) test(`summary ${status} revokes cached models and fences next polls`, async () => {
  announceIdentity('A'); mount(); await deliver(4)
  refreshOpenAlertBanner(); pending.shift().reject({ response: { status } }); await settle(); empty()
  refreshOpenAlertBanner(); assert.equal(pending.length, 0)
  announceIdentity('A'); await deliver(1); counts(1)
})
test('same actor polling retains transient failure, updates success, late unmounted reply ignored', async () => {
  announceIdentity('A'); mount(); await deliver(4)
  announceIdentity('A'); assert.equal(pending.length, 0)
  refreshOpenAlertBanner(); pending.shift().reject({ response: { status: 500 } }); await settle(); counts(4)
  refreshOpenAlertBanner(); await deliver(6); counts(6)
  refreshOpenAlertBanner(); const old = pending.shift(); unmount()
  old.resolve({ data: summary(99) }); await settle()
  let cached; const off = subscribeUnreadFeedback(x => { cached = x }); assert.equal(cached.unread_feedback_count, 6); off()
})
test('cache retains only validated numeric count and discards malformed summary fields', () => {
  announceIdentity('A')
  let cached; const off = subscribeUnreadFeedback(x => { cached = x })
  publishUnreadFeedback({ unread_feedback_count: '7', unrelated: 'A_UNUSED_CANARY' })
  assert.deepEqual(cached, { unread_feedback_count: 7 })
  publishUnreadFeedback({ unread_feedback_count: { hidden: 'A_MALFORMED_CANARY' } })
  assert.equal(cached, null); off()
})
