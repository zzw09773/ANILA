import { describe, expect, it } from 'vitest'
import { classifyChatFailure } from './chat'

describe('chat failure classification', () => {
  it('keeps the retry branch when backend wording changes but code stays stable', () => {
    const body = JSON.stringify({
      detail: {
        code: 'context_overflow',
        message: '後端改寫後的上下文說明',
      },
    })
    expect(classifyChatFailure(400, body)).toBe('context_overflow')
  })

  it('does not classify free-form wording as a control signal', () => {
    expect(
      classifyChatFailure(400, 'ContextWindowExceededError: old wording'),
    ).toBe('other')
  })
})
