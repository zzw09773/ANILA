// 未讀使用者回饋在每一頁上方出現藍色通知，紅橫幅仍在它上面。
// 筆數跟警報橫幅同一支摘要請求、同一個 60 秒輪詢；0 筆就不要畫。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import './helpers/dom.mjs'
import { createApp, h, nextTick, ref } from 'vue'

import { unreadFeedbackBannerModel } from '../src/utils/unreadFeedbackBanner.js'

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

test('有未讀回饋就寫出筆數，並連到使用者回饋頁', () => {
  const model = unreadFeedbackBannerModel({
    open_count: 0,
    unread_feedback_count: 3,
  })
  assert.ok(model)
  assert.equal(model.count, 3)
  assert.equal(model.href, '/feedback')
  assert.equal(model.text, '有 3 筆新的使用者回饋')
})

test('沒有未讀回饋時不顯示通知', () => {
  assert.equal(unreadFeedbackBannerModel({ unread_feedback_count: 0, open_count: 4 }), null)
  assert.equal(unreadFeedbackBannerModel({ open_count: 2 }), null)
  assert.equal(unreadFeedbackBannerModel(null), null)
})

test('紅橫幅與藍通知可以同時出現，紅色在上', async () => {
  const summary = ref({
    open_count: 2,
    highest_open_severity: 'high',
    unread_feedback_count: 4,
  })
  const { openAlertBannerModel } = await import('../src/utils/openAlertBanner.js')
  const View = {
    setup() {
      return () => {
        const alert = openAlertBannerModel(summary.value)
        const feedback = unreadFeedbackBannerModel(summary.value)
        return h('div', [
          alert
            ? h('div', { 'data-testid': 'open-alert-banner', class: 'open-alert-banner' }, alert.text)
            : null,
          feedback
            ? h('a', {
              'data-testid': 'unread-feedback-banner',
              class: 'unread-feedback-banner',
              href: feedback.href,
            }, feedback.text)
            : null,
        ])
      }
    },
  }
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(View)
  app.mount(el)
  await nextTick()
  const alert = document.querySelector('[data-testid="open-alert-banner"]')
  const feedback = document.querySelector('[data-testid="unread-feedback-banner"]')
  assert.ok(alert)
  assert.ok(feedback)
  assert.equal(feedback.getAttribute('href'), '/feedback')
  assert.equal(feedback.textContent, '有 4 筆新的使用者回饋')
  assert.ok(alert.compareDocumentPosition(feedback) & Node.DOCUMENT_POSITION_FOLLOWING)

  summary.value = { open_count: 2, highest_open_severity: 'high', unread_feedback_count: 0 }
  await nextTick()
  assert.ok(document.querySelector('[data-testid="open-alert-banner"]'))
  assert.equal(document.querySelector('[data-testid="unread-feedback-banner"]'), null)
  app.unmount()
})

test('藍色通知掛在每一頁、只給管理員，而且跟警報橫幅共用同一次抓取', () => {
  const layout = stripComments(readSource('components/layout/AppLayout.vue'))
  const alert = stripComments(readSource('components/layout/OpenAlertBanner.vue'))
  const notice = stripComments(readSource('components/layout/UnreadFeedbackBanner.vue'))
  const alertAt = layout.indexOf('<OpenAlertBanner')
  const noticeAt = layout.indexOf('<UnreadFeedbackBanner')
  assert.ok(alertAt >= 0)
  assert.ok(noticeAt > alertAt)
  assert.match(layout.slice(alertAt, alertAt + 80), /v-if="isAdmin"/)
  assert.match(layout.slice(noticeAt, noticeAt + 90), /v-if="isAdmin"/)

  assert.match(alert, /getAlertSummary/)
  assert.match(alert, /OPEN_ALERT_BANNER_POLL_MS/)
  assert.match(alert, /createPoller/)
  assert.match(alert, /publishUnreadFeedback/)
  assert.equal((alert.match(/client\.get|getUnread|\/api\/admin\/feedback/g) || []).length, 0)

  assert.match(notice, /unreadFeedbackBannerModel/)
  assert.match(notice, /data-testid="unread-feedback-banner"/)
  assert.match(notice, /\/feedback/)
  assert.doesNotMatch(notice, /createPoller/)
  assert.doesNotMatch(notice, /setInterval/)
  assert.doesNotMatch(notice, /#9d1c1c/)
  assert.match(notice, /--c-info/)
})
