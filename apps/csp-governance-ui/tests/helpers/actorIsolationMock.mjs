import { reactive, ref, h } from 'vue';
import * as base from './quickBuilderMock.mjs';
import mcp from './quickBuilderMcpMock.mjs';
export * from './quickBuilderMock.mjs';
const holds = new Set(),
  pending = new Map(),
  failures = new Map();
export const observed = [];
export const auth = reactive({
  user: {
    id: 101,
    username: 'A_USER_CANARY',
    role: 'admin'
  },
  logout() {
    observed.push(['logout']);
  }
});
export function resetIsolation() {
  base.reset();
  holds.clear();
  pending.clear();
  failures.clear();
  observed.length = 0;
  auth.user = {
    id: 101,
    username: 'A_USER_CANARY',
    role: 'admin'
  };
}
export function hold(name) {
  holds.add(name);
}
export function deny(name, status) {
  failures.set(name, status);
}
export function release(name) {
  holds.delete(name);
  for (const resolve of pending.get(name) || []) resolve();
  pending.delete(name);
}
async function invoke(name, args, fn) {
  observed.push([name]);
  const response = await fn(...args),
    status = failures.get(name);
  if (holds.has(name)) await new Promise(resolve => pending.set(name, [...(pending.get(name) || []), resolve]));
  if (status) throw {
    response: {
      status,
      data: {
        detail: {
          message: 'A_ERROR_CANARY'
        }
      }
    }
  };
  return response;
}
export const getCatalog = (...a) => invoke('getCatalog', a, base.getCatalog);
export const getAssistant = (...a) => invoke('getAssistant', a, base.getAssistant);
export const listAssistants = (...a) => invoke('listAssistants', a, base.listAssistants);
export const listTemplates = (...a) => invoke('listTemplates', a, base.listTemplates);
export const createAssistant = (...a) => invoke('createAssistant', a, base.createAssistant);
export const saveVersion = (...a) => invoke('saveVersion', a, base.saveVersion);
export const compareVersions = (...a) => invoke('compareVersions', a, base.compareVersions);
export const changePassword = (...a) => invoke('changePassword', a, async () => ({
  data: {}
}));
export const useAuthStore = () => auth;
export const useTheme = () => ({
  theme: ref('light'),
  toggleTheme() {}
});
export const useShellNav = () => ({
  open: ref(false),
  narrow: ref(false),
  compact: ref(false),
  toggle() {},
  close() {}
});
export const useRoute = () => ({
  path: '/quick-builder'
});
export const useRouter = () => ({
  push(path) {
    observed.push(['navigate', path]);
  }
});
export const RouterLink = {
  setup: (_, ctx) => () => h('a', ctx.slots.default?.())
};
export default {
  get: (...a) => invoke(a[0].endsWith('/catalog') ? 'mcpCatalog' : 'mcpGet', a, mcp.get),
  post: (...a) => invoke('mcpPost', a, mcp.post)
};
export const loginWithCard = async () => ({});
export const login = async () => ({});
export const refreshTokenApi = async () => ({});
export const getMe = async () => ({});
export const logout = async () => ({});
