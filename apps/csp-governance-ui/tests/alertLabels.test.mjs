// 警報清單的憑證／磁碟列要顯示繁中分類與嚴重度，用既有的低／中／高／嚴重。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import './helpers/dom.mjs'
import { createApp, h, nextTick } from 'vue'

import { categoryLabel, severityLabel } from '../src/utils/alertLabels.js'

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

test('憑證與磁碟的分類、嚴重度是繁中，未知代碼原樣留著', () => {
  assert.equal(categoryLabel('certificate'), '憑證')
  assert.equal(categoryLabel('disk'), '磁碟')
  assert.equal(severityLabel('low'), '低')
  assert.equal(severityLabel('medium'), '中')
  assert.equal(severityLabel('high'), '高')
  assert.equal(severityLabel('critical'), '嚴重')
  assert.equal(categoryLabel('not-a-category'), 'not-a-category')
  assert.equal(severityLabel(''), '')
})

test('警報列畫出繁中，不畫英文代碼', async () => {
  const rows = [
    { category: 'certificate', severity: 'critical' },
    { category: 'disk', severity: 'high' },
  ]
  const View = {
    setup() {
      return () => h('ul', rows.map((alert) => h('li', [
        h('span', { class: `severity-tag is-${alert.severity}` }, severityLabel(alert.severity)),
        h('span', { class: 'category' }, categoryLabel(alert.category)),
      ])))
    },
  }
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(View)
  app.mount(el)
  await nextTick()
  const text = el.textContent || ''
  assert.match(text, /憑證/)
  assert.match(text, /嚴重/)
  assert.match(text, /磁碟/)
  assert.match(text, /高/)
  assert.equal(text.includes('certificate'), false)
  assert.equal(text.includes('critical'), false)
  assert.equal(text.includes('disk'), false)
  assert.equal(text.includes('high'), false)
  app.unmount()
})

test('警報頁清單用同一套標籤，不再直接印代碼', () => {
  const alerts = stripComments(readSource('views/AlertsView.vue'))
  assert.match(alerts, /categoryLabel\(alert\.category\)/)
  assert.match(alerts, /severityLabel\(alert\.severity\)/)
  assert.match(alerts, /diskSourceLabel/)
  assert.equal(/\{\{\s*alert\.category\s*\}\}/.test(alerts), false)
  assert.equal(/\{\{\s*alert\.severity\s*\}\}/.test(alerts), false)
  assert.equal(/\{\{\s*alert\.source_id\s*\}\}/.test(alerts), false)
})
