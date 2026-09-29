import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { inactivityNoticeModel } from '../src/utils/inactivityNotice.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

test('試算與實際停用各有一句，零筆不顯示', () => {
  assert.equal(inactivityNoticeModel(null), null)
  assert.equal(inactivityNoticeModel({ inactivity_notice: { phase: 'preview', count: 0, days: 180 } }), null)
  const preview = inactivityNoticeModel({
    inactivity_notice: { phase: 'preview', count: 2, days: 180 },
  })
  assert.equal(preview.text, '試算：有 2 個帳號超過 180 天未登入，下一次每日排程才會停用')
  assert.equal(preview.href, '/users')
  const applied = inactivityNoticeModel({
    inactivity_notice: { phase: 'applied', count: 3, days: 90 },
  })
  assert.equal(applied.text, '最近一次排程已停用 3 個超過 90 天未登入的帳號')
})

test('閒置通知跟警報摘要同一次抓取，掛在管理員與代理管理員看得到的版面', () => {
  const layout = readSource('components/layout/AppLayout.vue')
  const alert = readSource('components/layout/OpenAlertBanner.vue')
  const notice = readSource('components/layout/InactivityNoticeBanner.vue')
  assert.match(layout, /<InactivityNoticeBanner v-if="isSteward" \/>/)
  assert.match(alert, /publishInactivityNotice/)
  assert.match(notice, /data-testid="inactivity-notice-banner"/)
  assert.match(notice, /--c-info/)
  assert.doesNotMatch(notice, /createPoller/)
})
