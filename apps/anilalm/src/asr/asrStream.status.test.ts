import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  probeAsrAvailable,
  speechStatusAllowsMic,
  watchSpeechStatus,
} from './asrStream'

describe('anilalm speech status', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('只在治理中心回報啟用且健康時顯示麥克風', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: true, healthy: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    expect(await probeAsrAvailable()).toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/external-services/speech/status',
      { credentials: 'same-origin' },
    )
    expect(speechStatusAllowsMic({ enabled: true, healthy: false })).toBe(false)
  })

  it('載入與聚焦才問，沒有每分鐘的定時器', () => {
    vi.useFakeTimers()
    const run = vi.fn()
    const stop = watchSpeechStatus(run)
    expect(run).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new Event('focus'))
    expect(run).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(120_000)
    expect(run).toHaveBeenCalledTimes(2)
    stop()
  })
})
