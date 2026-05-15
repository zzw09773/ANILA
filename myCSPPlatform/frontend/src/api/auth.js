import client from './client'

export const login = (username, password, extra = {}) =>
  client.post('/api/auth/login', { username, password, ...extra })

// cookie 流程：refresh token 從 anila_refresh_token cookie 取，
// 不需 body；保留無參數 signature 以便未來 SDK 可選擇傳入。
export const refreshTokenApi = () =>
  client.post('/api/auth/refresh', {})

export const getMe = () =>
  client.get('/api/auth/me')

export const logout = () =>
  client.post('/api/auth/logout', {})

export const changePassword = (current_password, new_password) =>
  client.put('/api/auth/password', { current_password, new_password })

export const register = (username, email, password) =>
  client.post('/api/auth/register', { username, email, password })

export const listPublicAuthProviders = () =>
  client.get('/api/auth/providers')

export const getOidcStartUrl = (providerId, nextPath = '/') =>
  client.get(`/api/auth/oidc/${providerId}/start`, { params: { next_path: nextPath } })

// branch SSO: 中科院憑證卡登入
export const cardChallenge = () =>
  client.get('/api/auth/card/challenge')

export const cardVerify = (payload) =>
  client.post('/api/auth/card/verify', {
    challenge_token: payload.challenge_token,
    signature: payload.signature,
    card_serial: payload.card_serial ?? null,
  }, {
    // 跨 status code 都不要被預設攔截：202 是 pending、200 是登入成功，
    // 兩種都需要讓 caller 拿到 response.data 自己 branch。
    validateStatus: (s) => s === 200 || s === 202,
  })

// 完成註冊（pending 使用者填單位後送出）
export const cardCompleteRegistration = (payload) =>
  client.post('/api/auth/card/complete-registration', {
    registration_token: payload.registration_token,
    department_id: payload.department_id,
  })

// 公開 endpoint：列 active departments 給「完成註冊」表單用
export const cardListDepartments = () =>
  client.get('/api/auth/card/registration/departments')
