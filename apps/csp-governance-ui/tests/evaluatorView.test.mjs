import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(
  new URL('../src/views/EvaluatorView.vue', import.meta.url),
  'utf8',
)

test('憑證讀取失敗不得變成尚未註冊', () => {
  assert.match(source, /const credentialLoadError = ref\(''\)/)
  assert.match(source, /listLlmCredentials\(\)\.catch\(\(e\) => \{/)
  assert.match(source, /v-if="credentialLoadError"/)
  assert.match(source, /v-else-if="credentials\.length === 0"/)
  assert.doesNotMatch(
    source,
    /listLlmCredentials\(\)\.catch\(\(\) => \(\{ data: \[\] \}\)\)/,
  )
})

test('憑證讀取失敗顯示繁中句子，後端 detail 只放 title', () => {
  const errorBlock = source.match(
    /<div v-if="credentialLoadError"[\s\S]*?<\/div>/,
  )?.[0]
  assert.ok(errorBlock)
  assert.match(errorBlock, /:title="credentialLoadError"/)
  assert.match(errorBlock, /LLM 憑證讀取失敗，無法確認是否已註冊。/)
  assert.doesNotMatch(errorBlock, /\{\{\s*credentialLoadError\s*\}\}/)
})
