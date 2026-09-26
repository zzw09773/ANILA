import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const view = readFileSync(resolve(root, 'src/views/ExternalServicesView.vue'), 'utf8')
const router = readFileSync(resolve(root, 'src/router/index.js'), 'utf8')
const models = readFileSync(resolve(root, 'src/views/ModelsView.vue'), 'utf8')

test('外部服務頁說明原生解析器退路，憑證欄是密碼框', () => {
  assert.match(view, /fallback_note/)
  assert.match(view, /內建原生解析器/)
  assert.match(view, /type="password"/)
  assert.doesNotMatch(view, /\{\{\s*item\.credential\s*\}\}/)
  assert.match(router, /external-services/)
})

test('模型頁不再把主語音按鈕當成解碼位址', () => {
  assert.doesNotMatch(models, /設為主語音辨識/)
  assert.match(models, /外部服務/)
})
