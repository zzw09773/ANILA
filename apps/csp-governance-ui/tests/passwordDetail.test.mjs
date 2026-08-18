import assert from 'node:assert/strict'
import test from 'node:test'
import { getLoginErrorMessage } from '../src/utils/loginSurface.js'

test('治理中心登入解析器讀取物件型 detail 的中文 message', () => {
  const error = {
    response: {
      status: 403,
      data: {
        detail: {
          code: 'pending_approval',
          message: '等待核准中，請通知 admin',
        },
      },
    },
  }

  assert.equal(getLoginErrorMessage(error), '等待核准中，請通知 admin')
})
