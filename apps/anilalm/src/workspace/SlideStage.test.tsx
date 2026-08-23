import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TOKENS } from '../theme/tokens'
import { SlideStage } from './SlideStage'
import type { SlidePreview } from '../types'

const t = TOKENS.light

function paint(slide: SlidePreview) {
  return render(<SlideStage slide={slide} t={t} />)
}

describe('SlideStage paints the same kinds as PPTX', () => {
  it('section_break is a claim plus one kicker, not a bullet list', () => {
    const { container } = paint({
      title: '這份簡報只回答能不能上線',
      bullets: ['依三份審查紀錄'],
      layoutKind: 'section_break',
    })
    expect(container.querySelector('[data-layout="section_break"]')).toBeTruthy()
    expect(screen.getByText('這份簡報只回答能不能上線')).toBeTruthy()
    expect(screen.getByText('依三份審查紀錄')).toBeTruthy()
    expect(container.querySelectorAll('li')).toHaveLength(0)
  })

  it('stat_callout shows the number, not interchangeable bullets', () => {
    const { container } = paint({
      title: '通過率已經過門檻',
      bullets: ['所以可以進下一階段'],
      layoutKind: 'stat_callout',
      stat: {
        value: '92%',
        label: '現場抽檢通過率',
        supporting: '對照上一季 71%。',
      },
    })
    expect(container.querySelector('[data-layout="stat_callout"]')).toBeTruthy()
    expect(screen.getByText('92%')).toBeTruthy()
    expect(screen.getByText('現場抽檢通過率')).toBeTruthy()
  })

  it('two_column paints both sides', () => {
    const { container } = paint({
      title: '現況跟之後的差在節奏',
      bullets: ['保險'],
      layoutKind: 'two_column',
      columns: [
        { heading: '現況', bullets: ['隔日才出'] },
        { heading: '之後', bullets: ['當班可核'] },
      ],
    })
    expect(container.querySelector('[data-layout="two_column"]')).toBeTruthy()
    expect(screen.getByText('現況')).toBeTruthy()
    expect(screen.getByText('之後')).toBeTruthy()
  })
})
