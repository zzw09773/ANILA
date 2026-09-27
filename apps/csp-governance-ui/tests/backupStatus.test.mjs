import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { formatBackupSize, summarizeBackupStatus } from '../src/utils/backupStatus.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

test('formatBackupSize uses binary units', () => {
  assert.equal(formatBackupSize(0), '0 B')
  assert.equal(formatBackupSize(1536), '1.5 KiB')
  assert.equal(formatBackupSize(10 * 1024 * 1024), '10 MiB')
  assert.equal(formatBackupSize(null), '—')
})

test('summarizeBackupStatus shows time, result and size', () => {
  const ok = summarizeBackupStatus({
    last_run_at: '2026-09-27T02:15:00Z',
    last_result: 'success',
    last_size_bytes: 2048,
    last_success_at: '2026-09-27T02:15:00Z',
    stale: false,
    reason: 'ok',
  })
  assert.equal(ok.resultLabel, '成功')
  assert.equal(ok.sizeLabel, '2.0 KiB')
  assert.equal(ok.time, '2026-09-27T02:15:00Z')
  assert.equal(ok.tone, 'ok')

  const failed = summarizeBackupStatus({
    last_run_at: '2026-09-27T04:00:00Z',
    last_result: 'failure',
    last_size_bytes: 0,
    last_success_at: '2026-09-27T02:15:00Z',
    stale: false,
    reason: 'failed',
  })
  assert.equal(failed.resultLabel, '失敗')
  assert.equal(failed.sizeLabel, '—')
  assert.equal(failed.tone, 'warn')

  const stale = summarizeBackupStatus({
    last_run_at: '2026-09-25T02:15:00Z',
    last_result: 'success',
    last_size_bytes: 4096,
    last_success_at: '2026-09-25T02:15:00Z',
    stale: true,
    reason: 'stale',
  })
  assert.equal(stale.resultLabel, '過期')
  assert.equal(stale.sizeLabel, '4.0 KiB')

  const missing = summarizeBackupStatus({
    last_run_at: null,
    last_result: 'none',
    last_size_bytes: null,
    last_success_at: null,
    stale: false,
    reason: 'missing',
  })
  assert.equal(missing.resultLabel, '尚無備份')
  assert.equal(missing.time, null)
})

test('dashboard mounts 最後一次備份 from the backup status endpoint', () => {
  const view = readSource('views/DashboardView.vue')
  const api = readSource('api/backup.js')
  assert.match(view, /最後一次備份/)
  assert.match(view, /getBackupStatus/)
  assert.match(view, /summarizeBackupStatus/)
  assert.match(api, /\/api\/admin\/backup-status/)
})
