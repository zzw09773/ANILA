import { AxiosError, type AxiosResponse } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { bindAuthAdapter, explainError, fetchWithSession } from './client'

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

describe('fetchWithSession CSRF', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    document.cookie = 'anila_csrf=; Max-Age=0'
  })

  it('sends the current CSRF cookie on a Studio mutation and refreshes it after 401', async () => {
    const seen: Array<string | null> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: string, init?: RequestInit) => {
        seen.push(new Headers(init?.headers).get('X-CSRF-Token'))
        if (seen.length === 1) return new Response('', { status: 401 })
        return new Response('ok', { status: 200 })
      }),
    )
    document.cookie = 'anila_csrf=old-token'
    bindAuthAdapter({
      refresh: async () => {
        document.cookie = 'anila_csrf=new-token'
        return true
      },
      logout: () => undefined,
    })

    const res = await fetchWithSession('/api/studio/slides/jobs', {
      method: 'POST',
      body: '{}',
    })

    expect(res.status).toBe(200)
    expect(seen).toEqual(['old-token', 'new-token'])
  })
})
