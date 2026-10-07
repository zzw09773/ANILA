import { describe, expect, it } from 'vitest'

import { cspLoginHref } from './loginRedirect'

describe('cspLoginHref', () => {
  it('未登入導向留在 8443', () => {
    const href = cspLoginHref({ pathname: '/anilalm/', search: '', hash: '' })
    const landed = new URL(href, 'https://lab.example:8443/anilalm/')
    expect(href.startsWith('/')).toBe(true)
    expect(landed.origin).toBe('https://lab.example:8443')
    expect(landed.pathname).toBe('/login')
    expect(landed.searchParams.get('next')).toBe('/anilalm/')
  })
})
