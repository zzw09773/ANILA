<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-100">
    <div class="bg-white p-8 rounded-xl shadow-lg w-full max-w-md">
      <div class="text-center mb-8">
        <h1 class="text-2xl font-bold text-gray-900">CSP Platform</h1>
        <p class="text-gray-500 mt-2">AI 模型服務管理平台</p>
      </div>

      <!-- Login Form (本機帳號) -->
      <form @submit.prevent="handleLogin" class="space-y-5">
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">帳號</label>
          <input
            v-model="username"
            type="text"
            required
            class="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition"
            placeholder="請輸入帳號"
          />
        </div>

        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">密碼</label>
          <input
            v-model="password"
            type="password"
            required
            class="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition"
            placeholder="請輸入密碼"
          />
        </div>

        <div v-if="error" class="text-sm p-3 rounded-lg" :class="isPending ? 'text-yellow-700 bg-yellow-50 border border-yellow-200' : 'text-red-600 bg-red-50'">
          <span v-if="isPending">⏳ {{ error }}</span>
          <span v-else>{{ error }}</span>
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full bg-indigo-600 text-white py-2.5 rounded-lg font-medium hover:bg-indigo-700 disabled:opacity-50 transition"
        >
          {{ loading ? '登入中...' : '登入' }}
        </button>

        <button
          type="button"
          @click="openRegisterModal"
          class="w-full border border-gray-300 text-gray-700 py-2.5 rounded-lg font-medium hover:bg-gray-50 transition"
        >
          註冊新帳號
        </button>
      </form>

      <!-- 單一登入（OIDC） -->
      <div v-if="oidcProviders.length" class="mt-8 pt-6 border-t border-gray-200">
        <p class="text-sm font-medium text-gray-700 mb-3">單一登入</p>
        <div class="space-y-3">
          <button
            v-for="provider in oidcProviders"
            :key="provider.id"
            type="button"
            @click="handleOidcLogin(provider)"
            :disabled="oidcLoadingId === provider.id"
            class="w-full border border-gray-300 text-gray-700 py-2.5 rounded-lg font-medium hover:bg-gray-50 disabled:opacity-50 transition"
          >
            {{ oidcLoadingId === provider.id ? '跳轉中...' : (provider.button_text || `使用 ${provider.name} 登入`) }}
          </button>
        </div>
      </div>
    </div>

    <!-- Register Modal -->
    <div v-if="showRegisterModal" class="fixed inset-0 z-50 flex items-center justify-center">
      <div class="fixed inset-0 bg-black/50" @click="closeRegisterModal"></div>
      <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-md w-full mx-4">
        <h3 class="text-lg font-semibold mb-4">註冊新帳號</h3>

        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">帳號</label>
            <input
              v-model="reg.username"
              type="text"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="請輸入帳號"
            />
          </div>

          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Email</label>
            <input
              v-model="reg.email"
              type="email"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="請輸入 Email"
            />
          </div>

          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">密碼</label>
            <input
              v-model="reg.password"
              type="password"
              class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
              placeholder="請輸入密碼"
            />
            <p class="text-xs text-gray-400 mt-1">至少 8 字元，須包含大小寫字母及特殊符號</p>
            <ul class="text-xs mt-2 space-y-0.5" v-if="reg.password">
              <li :class="reg.password.length >= 8 ? 'text-green-600' : 'text-gray-400'">
                {{ reg.password.length >= 8 ? '✓' : '○' }} 至少 8 個字元
              </li>
              <li :class="/[A-Z]/.test(reg.password) ? 'text-green-600' : 'text-gray-400'">
                {{ /[A-Z]/.test(reg.password) ? '✓' : '○' }} 至少一個大寫字母
              </li>
              <li :class="/[a-z]/.test(reg.password) ? 'text-green-600' : 'text-gray-400'">
                {{ /[a-z]/.test(reg.password) ? '✓' : '○' }} 至少一個小寫字母
              </li>
              <li :class="hasSpecial(reg.password) ? 'text-green-600' : 'text-gray-400'">
                {{ hasSpecial(reg.password) ? '✓' : '○' }} 至少一個特殊符號
              </li>
            </ul>
          </div>

          <div v-if="regError" class="text-red-600 text-sm bg-red-50 p-3 rounded-lg">{{ regError }}</div>

          <div v-if="regSuccess" class="text-green-700 text-sm bg-green-50 border border-green-200 p-3 rounded-lg">
            {{ regSuccess }}
          </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6">
          <button
            v-if="!regSuccess"
            @click="closeRegisterModal"
            class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            v-if="!regSuccess"
            @click="handleRegister"
            :disabled="!canRegister || registering"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ registering ? '送出中...' : '送出申請' }}
          </button>
          <button
            v-if="regSuccess"
            @click="closeRegisterModal"
            class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700"
          >
            關閉
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { getOidcStartUrl, listPublicAuthProviders, register as registerApi } from '../api/auth'

const router = useRouter()
const authStore = useAuthStore()

const username = ref('')
const password = ref('')
const error = ref('')
const isPending = ref(false)
const loading = ref(false)
const oidcLoadingId = ref(null)
const providers = ref([])

const showRegisterModal = ref(false)
const registering = ref(false)
const regError = ref('')
const regSuccess = ref('')
const reg = ref({ username: '', email: '', password: '' })

const SPECIAL_CHARS = '!@#$%^&*()_+-=[]{}|;:,.<>?/~`"\'\\'
function hasSpecial(str) {
  return [...str].some(c => SPECIAL_CHARS.includes(c))
}

const canRegister = computed(() =>
  reg.value.username &&
  reg.value.email &&
  reg.value.password.length >= 8 &&
  /[A-Z]/.test(reg.value.password) &&
  /[a-z]/.test(reg.value.password) &&
  hasSpecial(reg.value.password)
)

// LDAP 已下線；只剩 OIDC 一種外部 provider。
const oidcProviders = computed(() =>
  providers.value.filter(provider => provider.provider_type === 'oidc')
)

async function fetchProviders() {
  try {
    const { data } = await listPublicAuthProviders()
    providers.value = data
  } catch {
    providers.value = []
  }
}

onMounted(fetchProviders)

async function handleLogin() {
  error.value = ''
  isPending.value = false
  loading.value = true
  try {
    await authStore.login(username.value, password.value, { auth_source: 'local' })
    router.push('/')
  } catch (e) {
    const detail = e.response?.data?.detail || '登入失敗，請檢查帳號密碼'
    if (detail.includes('等待核准')) {
      isPending.value = true
    }
    error.value = detail
  } finally {
    loading.value = false
  }
}

async function handleOidcLogin(provider) {
  error.value = ''
  isPending.value = false
  oidcLoadingId.value = provider.id
  try {
    const { data } = await getOidcStartUrl(provider.id, '/')
    window.location.href = data.authorization_url
  } catch (e) {
    const detail = e.response?.data?.detail
    error.value = detail || '無法啟動單一登入流程'
    oidcLoadingId.value = null
  }
}

function openRegisterModal() {
  reg.value = { username: '', email: '', password: '' }
  regError.value = ''
  regSuccess.value = ''
  showRegisterModal.value = true
}

function closeRegisterModal() {
  showRegisterModal.value = false
}

async function handleRegister() {
  regError.value = ''
  registering.value = true
  try {
    const { data } = await registerApi(reg.value.username, reg.value.email, reg.value.password)
    regSuccess.value = data.message || '註冊成功，請等待管理員核准'
  } catch (e) {
    const detail = e.response?.data?.detail
    if (Array.isArray(detail)) {
      regError.value = detail.map(d => d.msg).join('；')
    } else {
      regError.value = detail || '註冊失敗'
    }
  } finally {
    registering.value = false
  }
}
</script>
