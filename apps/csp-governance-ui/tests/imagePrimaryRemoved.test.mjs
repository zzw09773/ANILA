// 主圖像旗標不再是生圖模型的設定來源。控制項與 API 客戶端都要拿掉。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(HERE, '..', rel), 'utf8')

test('models view no longer offers the legacy image-primary controls', () => {
  const view = read('src/views/ModelsView.vue')
  const api = read('src/api/models.js')
  const store = read('src/stores/models.js')
  for (const src of [view, api, store]) {
    assert.equal(src.includes('set-image-primary'), false)
    assert.equal(src.includes('unset-image-primary'), false)
    assert.equal(src.includes('is_image_primary'), false)
    assert.equal(src.includes('設為主圖像模型'), false)
    assert.equal(src.includes('主圖像'), false)
  }
})
