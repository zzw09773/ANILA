// 未處理警報要在每一頁上方出現紅橫幅，確認或解決後消失。
// 寄信設定在警報頁，密碼只寫入、不回顯示。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import './helpers/dom.mjs'
import { createApp, h, nextTick, ref } from 'vue'

import {
  OPEN_ALERT_BANNER_POLL_MS,
  openAlertBannerModel,
} from '../src/utils/openAlertBanner.js'
import { mailSettingsForForm, mailSettingsSaveBody } from '../src/utils/alertMailForm.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

test('有未處理警報就顯示筆數與最高嚴重度，並連到警報頁', () => {
  const model = openAlertBannerModel({
    open_count: 2,
    highest_open_severity: 'critical',
    acknowledged_count: 4,
  })
  assert.ok(model)
  assert.equal(model.count, 2)
  assert.equal(model.href, '/alerts')
  assert.match(model.text, /2/)
  assert.match(model.text, /嚴重/)
  assert.match(model.text, /未處理/)
  assert.equal(model.linkLabel, '查看警報')
})

test('確認或解決後沒有未處理警報，橫幅消失', () => {
  assert.equal(
    openAlertBannerModel({
      open_count: 0,
      highest_open_severity: null,
      acknowledged_count: 3,
    }),
    null,
  )
  assert.equal(
    openAlertBannerModel({ open_count: 0, highest_open_severity: 'high' }),
    null,
  )
})

test('橫幅畫面上出現，摘要變成已解決後從頁面拿掉', async () => {
  const summary = ref({ open_count: 2, highest_open_severity: 'high' })
  const View = {
    setup() {
      return () => {
        const model = openAlertBannerModel(summary.value)
        if (!model) return null
        return h('div', { 'data-testid': 'open-alert-banner', class: 'open-alert-banner' }, [
          h('span', model.text),
          h('a', { href: model.href }, model.linkLabel),
        ])
      }
    },
  }
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(View)
  app.mount(el)
  await nextTick()
  const banner = document.querySelector('[data-testid="open-alert-banner"]')
  assert.ok(banner)
  assert.match(banner.textContent, /2/)
  assert.match(banner.textContent, /高/)
  assert.equal(banner.querySelector('a').getAttribute('href'), '/alerts')
  summary.value = { open_count: 0, highest_open_severity: null }
  await nextTick()
  assert.equal(document.querySelector('[data-testid="open-alert-banner"]'), null)
  app.unmount()
})

test('橫幅每 60 秒更新，而且管理員與代理管理員看得到', () => {
  assert.equal(OPEN_ALERT_BANNER_POLL_MS, 60_000)
  const layout = stripComments(readSource('components/layout/AppLayout.vue'))
  const banner = stripComments(readSource('components/layout/OpenAlertBanner.vue'))
  const alerts = stripComments(readSource('views/AlertsView.vue'))
  assert.match(layout, /OpenAlertBanner/)
  assert.match(layout, /v-if="isSteward"/)
  assert.match(banner, /openAlertBannerModel/)
  assert.match(banner, /OPEN_ALERT_BANNER_POLL_MS/)
  assert.match(banner, /createPoller/)
  assert.match(banner, /poller\.stop\(\)/)
  assert.match(banner, /data-testid="open-alert-banner"/)
  assert.match(banner, /查看警報/)
  assert.match(banner, /\/alerts/)
  assert.match(alerts, /refreshOpenAlertBanner\(\)/)
})

test('寄信表單不把密碼帶回畫面，空白密碼表示不改', () => {
  const form = mailSettingsForForm({
    enabled: true,
    smtp_host: 'mail.example.com',
    smtp_port: 587,
    security: 'starttls',
    username: 'alerts',
    password: 'must-not-appear',
    password_envelope: 'enc::v1::secret',
    has_password: true,
    from_address: 'anila@example.com',
    recipients: 'ops@example.com',
    last_error: null,
  })
  assert.equal(form.password, '')
  assert.equal(form.has_password, true)
  assert.equal(JSON.stringify(form).includes('must-not-appear'), false)
  assert.equal(JSON.stringify(form).includes('enc::v1'), false)

  const kept = mailSettingsSaveBody({ ...form, password: '' })
  assert.equal('password' in kept, false)
  const changed = mailSettingsSaveBody({ ...form, password: 'new-secret' })
  assert.equal(changed.password, 'new-secret')
})

test('警報頁有寄信區與寄測試信，密碼欄是 write-only', () => {
  const alerts = stripComments(readSource('views/AlertsView.vue'))
  assert.match(alerts, /警報寄信/)
  assert.match(alerts, /寄測試信/)
  assert.match(alerts, /type="password"/)
  assert.match(alerts, /last_error/)
  assert.match(alerts, /mailSettingsForForm/)
  assert.match(alerts, /mailSettingsSaveBody/)
  assert.match(alerts, /data\.recipients/)
})
