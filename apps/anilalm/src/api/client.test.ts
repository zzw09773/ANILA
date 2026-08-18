import { AxiosError, type AxiosResponse } from 'axios'
import { describe, expect, it } from 'vitest'
import { explainError } from './client'

describe('ANILALM API error parsing', () => {
  it('returns the message from an object-shaped detail', () => {
    const response = {
      status: 403,
      statusText: 'Forbidden',
      headers: {},
      config: {},
      data: {
        detail: {
          code: 'pending_approval',
          message: '等待核准中，請通知 admin',
        },
      },
    } as AxiosResponse
    const error = new AxiosError(
      'Request failed with status code 403',
      'ERR_BAD_REQUEST',
      undefined,
      undefined,
      response,
    )

    expect(explainError(error)).toBe('等待核准中，請通知 admin')
  })
})
