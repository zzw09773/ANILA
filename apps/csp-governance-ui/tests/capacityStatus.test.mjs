import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { diskMountRows, diskSourceLabel, summarizeCertificate } from '../src/utils/capacityStatus.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

test('disk rows show label, percent and free GiB, never a path', () => {
  const rows = diskMountRows([
    { label: 'ingestion', used_pct: 42.26, free_gib: 5 },
    { label: 'attachments', used_pct: 10, free_gib: 2.5 },
    { label: '/var/secret/uploads', used_pct: 1, free_gib: 9 },
  ])
  assert.deepEqual(rows, [
    { label: '知識庫', usedLabel: '42.3%', freeLabel: '5.0 GiB' },
    { label: '附件', usedLabel: '10.0%', freeLabel: '2.5 GiB' },
    { label: '未命名', usedLabel: '1.0%', freeLabel: '9.0 GiB' },
  ])
  assert.equal(JSON.stringify(rows).includes('/var/'), false)
})

test('disk source id uses the same Traditional Chinese label as the dashboard tile', () => {
  assert.equal(diskSourceLabel('ingestion'), '知識庫')
  assert.equal(diskSourceLabel('attachments'), '附件')
  assert.equal(diskSourceLabel('root'), '系統')
  assert.equal(diskSourceLabel('/var/secret'), '未命名')
  assert.equal(diskSourceLabel('ingestion'), diskMountRows([{ label: 'ingestion', used_pct: 1, free_gib: 1 }])[0].label)
  assert.equal(diskSourceLabel('attachments'), diskMountRows([{ label: 'attachments', used_pct: 1, free_gib: 1 }])[0].label)
})

test('certificate card shows the expiry date and stays quiet when nginx is down', () => {
  const soon = summarizeCertificate({
    status: 'ok',
    not_after: '2026-10-10T00:00:00Z',
    days_remaining: 11,
    severity: 'high',
  })
  assert.equal(soon.date, '2026-10-10T00:00:00Z')
  assert.equal(soon.tone, 'warn')

  const critical = summarizeCertificate({
    status: 'ok',
    not_after: '2026-10-01T00:00:00Z',
    days_remaining: 2,
    severity: 'critical',
  })
  assert.equal(critical.tone, 'danger')

  const down = summarizeCertificate({
    status: 'unreachable',
    not_after: null,
    days_remaining: null,
    severity: null,
  })
  assert.equal(down.date, null)
  assert.equal(down.dateLabel, '無法讀取')
})

test('dashboard mounts disk and certificate from the admin API', () => {
  const view = readSource('views/DashboardView.vue')
  const api = readSource('api/capacity.js')
  assert.match(view, /磁碟/)
  assert.match(view, /HTTPS 憑證/)
  assert.match(view, /diskMountRows/)
  assert.match(view, /summarizeCertificate/)
  assert.match(view, /formatDate/)
  assert.match(api, /\/api\/admin\/disk-mounts/)
  assert.match(api, /\/api\/admin\/tls-certificate/)
  assert.equal(view.includes('server.key'), false)
})
