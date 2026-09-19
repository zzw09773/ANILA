// 2026-09-02 擁有者在深色主題下看到刊頭是淺藍底＋白字（bg rgb(138,165,209)），字看不見。
// 根因：刊頭直接拿 --c-accent-strong 當底色，而深色主題的 accent 是「調亮」的。
// 修法：刊頭有自己的兩顆 token（底／字），淺色深色各自定義；AppHeader 只吃這兩顆。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const tokens = readFileSync(resolve(ROOT, 'src/assets/styles/tokens.css'), 'utf8')
const header = readFileSync(resolve(ROOT, 'src/components/layout/AppHeader.vue'), 'utf8')

function block(selector) {
  const i = tokens.indexOf(selector)
  assert.ok(i >= 0, `tokens.css has no ${selector} block`)
  return tokens.slice(i, tokens.indexOf('}', i))
}

test('both themes define a masthead background and foreground', () => {
  for (const sel of [':root,\n[data-theme="light"]', '[data-theme="dark"]']) {
    const b = block(sel)
    assert.match(b, /--c-masthead:\s*#[0-9a-f]{6}/i, sel)
    assert.match(b, /--c-masthead-fg:\s*#[0-9a-f]{6}/i, sel)
  }
})

test('masthead text keeps AA contrast in both themes', () => {
  const lum = (hex) => {
    const [r, g, b] = [1, 3, 5].map((i) => {
      const channel = parseInt(hex.slice(i, i + 2), 16) / 255
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  for (const selector of [':root,\n[data-theme="light"]', '[data-theme="dark"]']) {
    const b = block(selector)
    const background = lum(b.match(/--c-masthead:\s*(#[0-9a-f]{6})/i)[1])
    const foreground = lum(b.match(/--c-masthead-fg:\s*(#[0-9a-f]{6})/i)[1])
    const contrast = (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05)
    assert.ok(contrast >= 4.5, `${selector}: masthead contrast ${contrast.toFixed(2)} must be at least 4.5:1`)
  }
})

test('AppHeader paints only with the masthead tokens, never a hard-coded white or the accent', () => {
  const style = header.slice(header.indexOf('<style'))
  assert.match(style, /background:\s*var\(--c-masthead\)/u)
  assert.match(style, /color:\s*var\(--c-masthead-fg\)/u)
  assert.doesNotMatch(style, /background:\s*var\(--c-accent-strong\)/u)
  assert.doesNotMatch(style, /color:\s*#ffffff/iu)
})
