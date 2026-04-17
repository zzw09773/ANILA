<template>
  <header class="bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between">
    <h2 class="text-lg font-semibold text-gray-800">{{ pageTitle }}</h2>
    <div class="flex items-center space-x-4">
      <span class="text-sm text-gray-500">{{ authStore.user?.username }}</span>
      <button
        @click="showChangePwModal = true"
        class="text-sm text-gray-500 hover:text-indigo-600 transition-colors"
      >
        修改密碼
      </button>
      <button
        @click="handleLogout"
        class="text-sm text-gray-500 hover:text-red-600 transition-colors"
      >
        登出
      </button>
    </div>
  </header>

  <!-- Change Password Modal -->
  <div v-if="showChangePwModal" class="fixed inset-0 z-50 flex items-center justify-center">
    <div class="fixed inset-0 bg-black/50" @click="closeChangePw"></div>
    <div class="relative bg-white rounded-xl shadow-xl p-6 max-w-md w-full mx-4">
      <h3 class="text-lg font-semibold mb-4">修改密碼</h3>

      <div class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">目前密碼</label>
          <input
            v-model="pw.current"
            type="password"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
            placeholder="請輸入目前密碼"
          />
        </div>

        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">新密碼</label>
          <input
            v-model="pw.new"
            type="password"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
            placeholder="請輸入新密碼"
          />
          <ul class="text-xs mt-2 space-y-0.5" v-if="pw.new">
            <li :class="pw.new.length >= 8 ? 'text-green-600' : 'text-gray-400'">
              {{ pw.new.length >= 8 ? '✓' : '○' }} 至少 8 個字元
            </li>
            <li :class="/[A-Z]/.test(pw.new) ? 'text-green-600' : 'text-gray-400'">
              {{ /[A-Z]/.test(pw.new) ? '✓' : '○' }} 至少一個大寫字母
            </li>
            <li :class="/[a-z]/.test(pw.new) ? 'text-green-600' : 'text-gray-400'">
              {{ /[a-z]/.test(pw.new) ? '✓' : '○' }} 至少一個小寫字母
            </li>
            <li :class="hasSpecial(pw.new) ? 'text-green-600' : 'text-gray-400'">
              {{ hasSpecial(pw.new) ? '✓' : '○' }} 至少一個特殊符號
            </li>
          </ul>
        </div>

        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">確認新密碼</label>
          <input
            v-model="pw.confirm"
            type="password"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
            placeholder="再次輸入新密碼"
          />
          <p v-if="pw.confirm && pw.new !== pw.confirm" class="text-xs text-red-500 mt-1">密碼不一致</p>
        </div>

        <div v-if="pwError" class="text-red-600 text-sm bg-red-50 p-3 rounded-lg">{{ pwError }}</div>
        <div v-if="pwSuccess" class="text-green-700 text-sm bg-green-50 border border-green-200 p-3 rounded-lg">{{ pwSuccess }}</div>
      </div>

      <div class="flex justify-end space-x-3 mt-6">
        <button @click="closeChangePw"
          class="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50">
          取消
        </button>
        <button
          @click="handleChangePassword"
          :disabled="!canSubmit || saving"
          class="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
        >
          {{ saving ? '儲存中...' : '更新密碼' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { changePassword } from '../../api/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const pageTitles = {
  '/': '儀表板',
  '/api-keys': 'API Key 管理',
  '/models': '模型管理',
  '/usage': '用量分析',
  '/users': '使用者管理',
  '/departments': '部門設定',
  '/alerts': '告警中心',
  '/audit-logs': '審計日誌',
  '/auth-providers': 'SSO / LDAP / OIDC',
  '/platform-links': '平台連結設定',
}

const pageTitle = computed(() => pageTitles[route.path] || 'CSP Platform')

const showChangePwModal = ref(false)
const pw = ref({ current: '', new: '', confirm: '' })
const pwError = ref('')
const pwSuccess = ref('')
const saving = ref(false)

const SPECIAL_CHARS = '!@#$%^&*()_+-=[]{}|;:,.<>?/~`"\'\\'
function hasSpecial(str) {
  return [...str].some(c => SPECIAL_CHARS.includes(c))
}

const canSubmit = computed(() =>
  pw.value.current &&
  pw.value.new.length >= 8 &&
  /[A-Z]/.test(pw.value.new) &&
  /[a-z]/.test(pw.value.new) &&
  hasSpecial(pw.value.new) &&
  pw.value.new === pw.value.confirm
)

function closeChangePw() {
  showChangePwModal.value = false
  pw.value = { current: '', new: '', confirm: '' }
  pwError.value = ''
  pwSuccess.value = ''
}

async function handleChangePassword() {
  pwError.value = ''
  pwSuccess.value = ''
  saving.value = true
  try {
    await changePassword(pw.value.current, pw.value.new)
    pwSuccess.value = '密碼已更新，請重新登入'
    // Token was rotated by backend — force re-login after a short delay
    setTimeout(() => {
      authStore.logout()
      router.push('/login')
    }, 1500)
  } catch (e) {
    const detail = e.response?.data?.detail
    if (Array.isArray(detail)) {
      pwError.value = detail.map(d => d.msg).join('；')
    } else {
      pwError.value = detail || '更新失敗'
    }
  } finally {
    saving.value = false
  }
}

function handleLogout() {
  authStore.logout()
  router.push('/login')
}
</script>
