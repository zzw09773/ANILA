// 控制台不再把 vlm 當成一種模型類型。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(HERE, '..', rel), 'utf8')

const VIEWS = [
  'src/views/ModelsView.vue',
  'src/views/UsageView.vue',
  'src/views/DeveloperAgentsView.vue',
]

test('console views do not offer a vlm model type', () => {
  for (const rel of VIEWS) {
    const src = read(rel)
    assert.equal(src.includes('value="vlm"'), false, rel)
    assert.equal(src.includes("value='vlm'"), false, rel)
    assert.equal(src.includes("model_type === 'vlm'"), false, rel)
    assert.equal(src.includes('model_type === "vlm"'), false, rel)
  }
})
