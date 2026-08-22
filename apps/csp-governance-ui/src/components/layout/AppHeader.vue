<template>
  <header class="topbar">
    <div class="topbar__left">
      <TermLogo :size="16" subtitle="系統管理" />
      <span class="topbar__path">
        <span class="topbar__path-segment">{{ currentSegment }}</span>
      </span>
    </div>

    <div class="topbar__right">
      <button
        class="topbar__theme"
        type="button"
        :aria-label="`切換至${otherTheme === 'light' ? '淺色' : '深色'}主題`"
        :title="`切換至${otherTheme === 'light' ? '淺色' : '深色'}主題`"
        @click="toggleTheme"
      >
        <span class="topbar__theme-icon">{{ theme === 'dark' ? '◐' : '◑' }}</span>
        <span class="topbar__theme-label">{{ theme === 'dark' ? '深色' : '淺色' }}</span>
      </button>

      <span class="topbar__user">
        <span class="topbar__user-name">{{ authStore.user?.username || '未登入' }}</span>
        <span class="topbar__user-role" :class="`is-${authStore.user?.role || 'user'}`">
          {{ roleLabel }}
        </span>
      </span>

      <button class="topbar__action" type="button" @click="showChangePwModal = true">
        變更密碼
      </button>
      <button class="topbar__action topbar__action--danger" type="button" @click="handleLogout">
        登出
      </button>
    </div>
  </header>

  <!-- Change-password modal — terminal style ----------------------------- -->
  <TermModal :visible="showChangePwModal" title="變更密碼" width="440px" @close="closeChangePw">
    <div class="pw-grid">
      <TermField label="目前密碼">
        <input v-model="pw.current" type="password" class="term-input" placeholder="••••••••" autocomplete="current-password" />
      </TermField>

      <TermField label="新密碼" :hint="pw.new ? '' : '至少 8 字元 · 大寫 · 小寫 · 符號'">
        <input v-model="pw.new" type="password" class="term-input" placeholder="••••••••" autocomplete="new-password" />
        <ul v-if="pw.new" class="pw-rules">
          <li :class="pwRule(pw.new.length >= 8)">{{ pwGlyph(pw.new.length >= 8) }} 8 字元以上</li>
          <li :class="pwRule(/[A-Z]/.test(pw.new))">{{ pwGlyph(/[A-Z]/.test(pw.new)) }} 大寫字母</li>
          <li :class="pwRule(/[a-z]/.test(pw.new))">{{ pwGlyph(/[a-z]/.test(pw.new)) }} 小寫字母</li>
          <li :class="pwRule(hasSpecial(pw.new))">{{ pwGlyph(hasSpecial(pw.new)) }} 符號</li>
        </ul>
      </TermField>

      <TermField
        label="確認新密碼"
        :error="pw.confirm && pw.new !== pw.confirm ? '不一致' : ''"
      >
        <input v-model="pw.confirm" type="password" class="term-input" placeholder="••••••••" autocomplete="new-password" />
      </TermField>

      <div v-if="pwError" class="pw-msg pw-msg--err">! {{ pwError }}</div>
      <div v-if="pwSuccess" class="pw-msg pw-msg--ok">✓ {{ pwSuccess }}</div>
    </div>

    <template #footer>
      <TermButton variant="ghost" @click="closeChangePw" label="取消" />
      <TermButton
        variant="primary"
        :disabled="!canSubmit"
        :loading="saving"
        :label="saving ? '儲存中' : '更新'"
        @click="handleChangePassword"
      />
    </template>
  </TermModal>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { changePassword } from '../../api/auth'
import { useTheme } from '../../composables/useTheme'
import TermLogo from '../cli/TermLogo.vue'
import TermModal from '../cli/TermModal.vue'
import TermField from '../cli/TermField.vue'
import TermButton from '../cli/TermButton.vue'
import { extractError } from '../../api/errors'
import { loginHref } from '../../utils/appOrigins'

const route = useRoute()
const authStore = useAuthStore()
const { theme, toggleTheme } = useTheme()

const otherTheme = computed(() => (theme.value === 'dark' ? 'light' : 'dark'))

const segmentMap = {
  '/keys': 'API 金鑰',
  '/models': '模型',
  '/usage': '用量',
  '/users': '使用者',
  '/departments': '部門',
  '/alerts': '警報',
  '/feedback': '使用者回饋',
  '/audit': '稽核紀錄',
  '/audit-logs': '稽核紀錄',
  '/banners': '公告橫幅',
  '/platform-settings': '平台設定',
  '/platform-links': '平台連結',
  '/service-access': '服務存取',
  '/developer/guide': '開發指南',
  '/developer/agents': '助手',
  '/knowledge-collections': '知識庫',
  '/message-actions': '自訂動作',
  '/classification-inventory': '分類盤點',
  '/service-clients': '服務客戶端',
  '/trusted-hosts': '信任主機',
  '/forbidden': '沒有權限',
}
const currentSegment = computed(() => {
  if (route.path === '/') return authStore.isRegularUser ? '工作臺' : '總覽'
  if (segmentMap[route.path]) return segmentMap[route.path]
  if (route.path.startsWith('/knowledge-collections/')) {
    return route.path.endsWith('/evaluator') ? '知識庫評估' : '知識庫內容'
  }
  return route.meta?.title || '系統管理'
})

const ROLE_LABEL = {
  owner: '擁有者',
  admin: '管理員',
  developer: '開發者',
  user: '使用者',
}
const roleLabel = computed(() => ROLE_LABEL[authStore.user?.role] || '')

const showChangePwModal = ref(false)
const pw = ref({ current: '', new: '', confirm: '' })
const pwError = ref('')
const pwSuccess = ref('')
const saving = ref(false)

const SPECIAL_CHARS = '!@#$%^&*()_+-=[]{}|;:,.<>?/~`"\'\\'
function hasSpecial(str) { return [...str].some(c => SPECIAL_CHARS.includes(c)) }
function pwRule(ok) { return ok ? 'is-ok' : 'is-pending' }
function pwGlyph(ok) { return ok ? '●' : '○' }

const canSubmit = computed(() =>
  !saving.value &&
  !!pw.value.current &&
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
    pwSuccess.value = '密碼已更新 — 需重新登入'
    setTimeout(async () => {
      await authStore.logout()
      window.location.replace(loginHref())
    }, 1500)
  } catch (e) {
    pwError.value = extractError(e, '更新失敗')
  } finally {
    saving.value = false
  }
}

async function handleLogout() {
  await authStore.logout()
  window.location.replace(loginHref())
}
</script>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--shell-topbar-h);
  padding: 0 var(--gap-3);
  background: var(--c-surface-2);
  border-bottom: 0;
  font-size: var(--t-xs);
  color: var(--c-fg-2);
  gap: var(--gap-3);
}

.topbar__left,
.topbar__right {
  display: flex;
  align-items: center;
  gap: var(--gap-3);
  min-width: 0;
}

.topbar__rule {
  color: var(--c-border-strong);
  font-size: var(--t-base);
  user-select: none;
}

.topbar__path {
  display: inline-flex;
  align-items: center;
  font-family: var(--font-sans);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.topbar__path-segment {
  color: var(--c-accent);
  margin-left: 2px;
  font-weight: 500;
}

.topbar__hints {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-3);
  margin-right: var(--gap-2);
}
.topbar__hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--c-fg-3);
  font-size: var(--t-2xs);
  letter-spacing: 0.04em;
  white-space: nowrap;
}

.topbar__theme {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: var(--border-w) solid var(--c-border);
  color: var(--c-fg-2);
  height: 22px;
  padding: 0 8px;
  border-radius: var(--r-soft);
  font-size: var(--t-2xs);
  letter-spacing: 0.02em;
  text-transform: none;
  cursor: pointer;
  transition: color var(--motion-fast), border-color var(--motion-fast), background-color var(--motion-fast);
}
.topbar__theme:hover {
  color: var(--c-accent);
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}
.topbar__theme-icon { font-size: var(--t-sm); }

.topbar__user {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-size: var(--t-xs);
}
.topbar__user-name { color: var(--c-fg-1); font-weight: 500; }
.topbar__user-role {
  color: var(--c-fg-3);
  font-size: var(--t-2xs);
  letter-spacing: 0;
  text-transform: none;
}
.topbar__user-role.is-owner     { color: var(--c-danger); font-weight: 600; }
.topbar__user-role.is-admin     { color: var(--c-warn); }
.topbar__user-role.is-developer { color: var(--c-info); }
.topbar__user-role.is-user      { color: var(--c-fg-3); }

.topbar__action {
  background: transparent;
  border: 0;
  color: var(--c-fg-3);
  font-family: inherit;
  font-size: var(--t-xs);
  cursor: pointer;
  padding: 0;
  letter-spacing: 0;
  transition: color var(--motion-fast);
}
.topbar__action:hover { color: var(--c-accent); }
.topbar__action--danger:hover { color: var(--c-danger); }

/* Password modal styles ----------------------------------------------- */
.pw-grid {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
}
.pw-rules {
  margin-top: var(--gap-1);
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px var(--gap-3);
  font-size: var(--t-2xs);
}
.pw-rules li.is-ok      { color: var(--c-ok); }
.pw-rules li.is-pending { color: var(--c-fg-3); }
.pw-msg {
  font-size: var(--t-xs);
  border: var(--border-w) solid;
  padding: var(--gap-2) var(--gap-3);
}
.pw-msg--err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.pw-msg--ok  { color: var(--c-ok);     border-color: var(--c-ok);     background: var(--c-ok-soft); }
</style>
