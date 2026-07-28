// W2-12 驗收:ANILALM 的錯誤解讀。
//
// 為什麼這支測試存在
// ------------------
// `src/api/client.ts:129-141` 的 `explainError` 處理 string 與 array,**但不處理
// dict**。CSP 有 4 處會回 `detail={"code","message"}`(circuit breaker 的
// `model_unhealthy`、retrieval 的 typed errors、model 註冊的 `untrusted_host`),
// 命中時 `typeof detail === 'string'` 與 `Array.isArray(detail)` 兩條都不成立,
// 於是 fallthrough 到 `` `${status} ${message}` `` —— 使用者看到
// **`"503 Request failed"`**,後端寫好的整條中文說明完全丟失。
//
// 這在 chat 路徑上最痛:模型被 health probe 標成 unhealthy 是**可自救**的
// (去 admin 模型健康頁看狀態),但 `"503 Request failed"` 什麼都沒說。
//
// 本檔釘住三件事:
// 1. dict detail 的訊息不得丟失(先紅後綠)。
// 2. 統一信封 `error.message` 優先於 legacy `detail`。
// 3. 既有 string / array 行為**逐字不變**(不能為了修 dict 而回歸)。

import { describe, expect, it } from 'vitest'

import { explainError, extractErrorCode, extractRequestId } from './errors'

function axiosError(
  data: unknown,
  { status = 400, message = 'Request failed' } = {},
): Error & { isAxiosError: true; response: { status: number; data: unknown } } {
  const err = new Error(message) as Error & {
    isAxiosError: true
    response: { status: number; data: unknown }
  }
  err.isAxiosError = true
  err.response = { status, data }
  return err
}

describe('explainError', () => {
  it('dict detail 的訊息不得丟失(原本退化成 "503 Request failed")', () => {
    const err = axiosError(
      {
        detail: {
          code: 'model_unhealthy',
          message: '模型 gemma4 已被 health probe 標記 unhealthy，暫時拒絕轉發。',
        },
      },
      { status: 503, message: 'Request failed with status code 503' },
    )
    const message = explainError(err)
    expect(message).toBe(
      '模型 gemma4 已被 health probe 標記 unhealthy，暫時拒絕轉發。',
    )
    expect(message).not.toContain('[object Object]')
    expect(message).not.toBe('503 Request failed with status code 503')
  })

  it('統一信封的 error.message 優先於 legacy detail', () => {
    const err = axiosError(
      {
        error: {
          code: 'MODEL_NOT_REGISTERED',
          message: "模型 'gemma4' 未註冊",
          details: null,
          request_id: 'req-7',
        },
        detail: "模型 'gemma4' 未註冊",
      },
      { status: 404 },
    )
    expect(explainError(err)).toBe("模型 'gemma4' 未註冊")
  })

  it('無 message 鍵的 dict 退回可讀 JSON,不是 [object Object]', () => {
    const err = axiosError({ detail: { code: 'invalid_query', field: 'q' } }, { status: 422 })
    const message = explainError(err)
    expect(message).not.toContain('[object Object]')
    expect(message).toContain('invalid_query')
  })

  // ── 既有行為不得回歸 ────────────────────────────────────────────────────
  it('字串 detail 行為與改動前一致', () => {
    expect(explainError(axiosError({ detail: '知識庫不存在' }, { status: 404 }))).toBe(
      '知識庫不存在',
    )
  })

  it('422 array detail 行為與改動前一致(用 ; 串接 msg)', () => {
    const err = axiosError(
      {
        detail: [
          { loc: ['body', 'name'], msg: 'Field required' },
          { loc: ['body', 'url'], msg: 'Input should be a valid URL' },
        ],
      },
      { status: 422 },
    )
    const message = explainError(err)
    expect(message).toContain('Field required')
    expect(message).toContain('Input should be a valid URL')
    expect(message).not.toContain('[object Object]')
  })

  it('沒有 detail 時退回 status + message', () => {
    expect(explainError(axiosError({}, { status: 502, message: 'Bad Gateway' }))).toBe(
      '502 Bad Gateway',
    )
  })

  it('非 axios 的 Error 回 message', () => {
    expect(explainError(new Error('boom'))).toBe('boom')
  })

  it('完全不是 Error 的東西也不會爆', () => {
    expect(explainError('plain string')).toBe('plain string')
    expect(typeof explainError(undefined)).toBe('string')
  })
})

describe('extractErrorCode / extractRequestId', () => {
  it('讀信封的 code 與 request_id', () => {
    const err = axiosError({
      error: {
        code: 'CLEARANCE_INSUFFICIENT',
        message: 'clearance 不足',
        details: null,
        request_id: 'req-abc',
      },
      detail: 'clearance 不足',
    })
    expect(extractErrorCode(err)).toBe('CLEARANCE_INSUFFICIENT')
    expect(extractRequestId(err)).toBe('req-abc')
  })

  it('legacy dict detail 的 code 也認(過渡期)', () => {
    const err = axiosError({ detail: { code: 'model_unhealthy', message: 'x' } })
    expect(extractErrorCode(err)).toBe('model_unhealthy')
  })

  it('拿不到就回 null,不猜', () => {
    expect(extractErrorCode(axiosError({ detail: '純字串' }))).toBeNull()
    expect(extractRequestId(axiosError({ detail: '純字串' }))).toBeNull()
    expect(extractErrorCode(new Error('x'))).toBeNull()
  })
})
