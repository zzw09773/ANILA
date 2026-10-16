export const reads = []
export const logouts = []
export const clientAuthCalls = { refresh: 0, logout: 0 }
let refreshBehaviour = async () => {}
export function setRefreshBehaviour(value) { refreshBehaviour = value }
export function resetIdentityMock() {
  reads.length = 0; logouts.length = 0
  clientAuthCalls.refresh = 0; clientAuthCalls.logout = 0
  refreshBehaviour = async () => {}
}
export function getMe() { return new Promise((resolve, reject) => reads.push({ resolve, reject })) }
export const login = async () => ({})
export const refreshTokenApi = async () => ({})
export function logout() { return new Promise((resolve, reject) => logouts.push({ resolve, reject })) }
export const loginWithCard = async () => ({ status: 'ok' })
export const useAuthStore = () => ({
  refreshToken: async () => { clientAuthCalls.refresh += 1; await refreshBehaviour() },
  logout: async () => { clientAuthCalls.logout += 1 },
})
export default { currentRoute: { value: { path: '/' } }, push() {} }
