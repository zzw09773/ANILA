import { describe, expect, it } from 'vitest'
import { slidesFromJobSpec } from '../api/studio'
import { displayBullet, resolveSlideStage } from './slideStage'
import { replaceSlideInDeck } from './slideRedo'
import type { SlidePreview } from '../types'

describe('slidesFromJobSpec', () => {
  it('keeps layout payload so preview can match PPTX', () => {
    const slides = slidesFromJobSpec({
      title: '院內簡報',
      slides: [
        {
          title: '封面主張',
          bullets: ['一句副標'],
          layout_kind: 'section_break',
        },
        {
          title: '準確率已過門檻',
          bullets: ['因此可以進下一階段'],
          layout_kind: 'stat_callout',
          stat: {
            value: '92%',
            label: '現場抽檢通過率',
            supporting: '對照上一季 71%，樣本來自三個場次。',
          },
        },
        {
          title: '兩邊的取捨不一樣',
          bullets: ['保險用'],
          layout_kind: 'two_column',
          columns: [
            { heading: '現況', bullets: ['人工核對', '隔日才出'] },
            { heading: '之後', bullets: ['當班可核', '留下軌跡'] },
          ],
        },
      ],
    })
    expect(slides).toHaveLength(3)
    expect(resolveSlideStage(slides[0]!)).toBe('section_break')
    expect(resolveSlideStage(slides[1]!)).toBe('stat_callout')
    expect(slides[1]?.stat?.value).toBe('92%')
    expect(resolveSlideStage(slides[2]!)).toBe('two_column')
    expect(slides[2]?.columns?.[0]?.heading).toBe('現況')
  })

  it('falls back to standard when a fancy layout is missing payload', () => {
    const [slide] = slidesFromJobSpec({
      slides: [
        {
          title: '沒有數字就不要硬畫',
          bullets: ['只留主張'],
          layout_kind: 'stat_callout',
        },
      ],
    })
    expect(resolveSlideStage(slide!)).toBe('standard')
  })
})

describe('resolveSlideStage', () => {
  it('paints quote and icon rows when the payload is there', () => {
    const quote: SlidePreview = {
      title: '原話比摘要有用',
      bullets: [],
      layoutKind: 'quote',
      quote: { text: '先把來源對上再開口', attribution: '審查紀錄' },
    }
    const rows: SlidePreview = {
      title: '三件要先做的事',
      bullets: [],
      layoutKind: 'icon_rows',
      iconRows: [
        { heading: '對來源', description: '左側勾過的檔' },
        { heading: '講一句', description: '不要唸條列' },
      ],
    }
    expect(resolveSlideStage(quote)).toBe('quote')
    expect(resolveSlideStage(rows)).toBe('icon_rows')
  })
})

describe('displayBullet', () => {
  it('strips renderer markers the PPTX peels', () => {
    expect(displayBullet('● 主點')).toBe('主點')
    expect(displayBullet('沒有標記')).toBe('沒有標記')
  })
})

describe('replaceSlideInDeck keeps layout on the rewritten page', () => {
  it('swaps only that index including layoutKind', () => {
    const current: SlidePreview[] = [
      { title: '封面', bullets: ['副標'], layoutKind: 'section_break' },
      { title: '舊主張', bullets: ['舊點'], layoutKind: 'standard' },
      { title: '收束', bullets: ['帶走'], layoutKind: 'standard' },
    ]
    const next = replaceSlideInDeck(
      current,
      [
        {
          title: '新主張',
          bullets: ['一句'],
          layoutKind: 'stat_callout',
          stat: { value: '3', label: '待補件' },
        },
      ],
      1,
    )
    expect(next).toHaveLength(3)
    expect(next[0]).toEqual(current[0])
    expect(next[1]?.layoutKind).toBe('stat_callout')
    expect(next[1]?.stat?.value).toBe('3')
    expect(next[1]?.title).toContain('（重做）')
    expect(next[2]).toEqual(current[2])
  })
})
