import client from './client'

// 跟後端 EMERGENCY_CONFIRM_TEXT 同一個句子。送出前對話框必須讓人看到它。
export const JWT_EMERGENCY_CONFIRM = '所有人會被登出，進行中的派工權杖會失效。自行驗證派工權杖的 agent 最多還能接受舊鑰 5 分鐘'

export function emergencyRotateJwtSigningKey() {
  return client.post('/api/auth/jwt-keyring/emergency-rotation', {
    confirm: JWT_EMERGENCY_CONFIRM,
  })
}
