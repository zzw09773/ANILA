import { describe, expect, it } from 'vitest'
import {
  REDONE_SUFFIX,
  markRedoneTitle,
  replaceSlideInDeck,
  slidesAfterRegenerate,
} from './slideRedo'

const deck = [
  { title: '封面', bullets: ['舊封面'] },
  { title: '審查摘要', bullets: ['舊摘要'] },
  { title: '結論', bullets: ['舊結論'] },
]

describe('replaceSlideInDeck', () => {
  it('rewrites only the requested page and keeps the rest', () => {
    const next = replaceSlideInDeck(
      deck,
      [
        { title: '不該出現', bullets: ['整份新簡報'] },
        { title: markRedoneTitle('審查摘要'), bullets: ['新論點'] },
      ],
      1,
    )
    expect(next).toHaveLength(3)
    expect(next[0]).toEqual(deck[0])
    expect(next[1]?.title).toBe(`審查摘要${REDONE_SUFFIX}`)
    expect(next[1]?.bullets).toEqual(['新論點'])
    expect(next[2]).toEqual(deck[2])
  })

  it('uses a one-slide payload for the current page', () => {
    const next = replaceSlideInDeck(
      deck,
      [{ title: `審查摘要${REDONE_SUFFIX}`, bullets: ['改寫'] }],
      1,
    )
    expect(next[0]).toEqual(deck[0])
    expect(next[1]?.bullets).toEqual(['改寫'])
    expect(next[2]).toEqual(deck[2])
  })

  it('does not blank the preview when spec is missing', () => {
    expect(replaceSlideInDeck(deck, undefined, 1)).toEqual(deck)
    expect(replaceSlideInDeck(deck, [], 1)).toEqual(deck)
    expect(slidesAfterRegenerate(deck, { spec: null }, 1)).toEqual(deck)
  })
})

describe('markRedoneTitle', () => {
  it('adds （重做） once', () => {
    expect(markRedoneTitle('審查摘要')).toBe(`審查摘要${REDONE_SUFFIX}`)
    expect(markRedoneTitle(`審查摘要${REDONE_SUFFIX}`)).toBe(
      `審查摘要${REDONE_SUFFIX}`,
    )
  })
})
