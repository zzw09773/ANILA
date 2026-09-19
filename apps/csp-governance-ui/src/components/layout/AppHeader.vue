<template>
  <!-- 刊頭：與主畫面同層中性表面、系統 sans 站名、目前頁面用中文。 -->
  <header class="topbar">
    <a class="skip-link" href="#gov-main">跳到主要內容</a>
    <div class="topbar__left">
      <button
        class="topbar__menu"
        type="button"
        :aria-expanded="open"
        aria-controls="gov-sidenav"
        :aria-label="open ? '關閉選單' : '開啟選單'"
        @click="toggle()"
      >
        <span class="topbar__menu-icon" aria-hidden="true">
          <span /><span /><span />
        </span>
      </button>
      <TermLogo :size="18" :compact="narrow" subtitle="治理中心" />
      <span class="topbar__crumb" aria-current="page">{{ currentPageLabel }}</span>
    </div>

    <div class="topbar__right">
      <span class="topbar__user">
        <span class="topbar__user-name">{{ authStore.user?.username || '訪客' }}</span>
        <span class="topbar__user-role">{{ roleLabel(authStore.user?.role) }}</span>
      </span>
      <button
        class="topbar__theme"
        type="button"
        :aria-label="`切換至${otherTheme === 'light' ? '淺色' : '深色'}主題`"
        :title="`切換至${otherTheme === 'light' ? '淺色' : '深色'}主題`"
        @click="toggleTheme"
      >
        {{ theme === 'dark' ? '淺色' : '深色' }}
      </button>
      <template v-if="!compact">
        <button class="topbar__action" type="button" @click="showChangePwModal = true">
          變更密碼
        </button>
        <button class="topbar__action" type="button" @click="handleLogout">
          登出
        </button>
      </template>
      <div v-else class="topbar__more">
        <button
          class="topbar__action"
          type="button"
          aria-haspopup="menu"
          :aria-expanded="accountOpen"
          aria-label="帳號選單"
          @click="accountOpen = !accountOpen"
        >
          帳號
        </button>
        <div v-if="accountOpen" class="topbar__more-menu" role="menu">
          <button type="button" role="menuitem" @click="openChangePwFromMenu">變更密碼</button>
          <button type="button" role="menuitem" @click="handleLogout">登出</button>
        </div>
      </div>
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
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { roleLabel } from '../../utils/roleLabel'
import { changePassword } from '../../api/auth'
import { useTheme } from '../../composables/useTheme'
import { useShellNav } from '../../composables/useShellNav.js'
import TermLogo from '../cli/TermLogo.vue'
import TermModal from '../cli/TermModal.vue'
import TermField from '../cli/TermField.vue'
import TermButton from '../cli/TermButton.vue'
import { extractError } from '../../api/errors'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const { theme, toggleTheme } = useTheme()
const nav = useShellNav()
const { open, narrow, compact, toggle } = nav
const accountOpen = ref(false)
watch(compact, (c) => { if (!c) accountOpen.value = false })

function openChangePwFromMenu() {
  accountOpen.value = false
  showChangePwModal.value = true
}

const otherTheme = computed(() => (theme.value === 'dark' ? 'light' : 'dark'))

const segmentMap = {
  '/': '/dashboard',
  '/api-keys': '/api-keys',
  '/models': '/models',
  '/usage': '/usage',
  '/users': '/admin/users',
  '/departments': '/admin/departments',
  '/alerts': '/admin/alerts',
  '/feedback': '/admin/feedback',
  '/audit-logs': '/admin/audit',
  '/platform-links': '/admin/platform-links',
  '/service-access': '/admin/service-access',
  '/developer/guide': '/dev/guide',
  '/developer/agents': '/dev/agents',
  '/knowledge-collections': '/dev/collections',
}
const currentSegment = computed(() => {
  if (segmentMap[route.path]) return segmentMap[route.path]
  if (route.path.startsWith('/knowledge-collections/')) return '/dev/collections/detail'
  return route.path
})

// 目前頁面的中文名（跟側欄同一份對照），刊頭顯示這個而不是 URL 片段。
const PAGE_LABELS = {
  '/': '儀表板', '/api-keys': 'API 金鑰', '/models': '模型', '/model-access-groups': '群組', '/usage': '用量',
  '/developer/guide': '開發指南', '/developer/agents': 'Agent', '/knowledge-collections': '知識庫',
  '/message-actions': '自訂動作', '/users': '使用者', '/departments': '部門', '/alerts': '警報',
  '/feedback': '使用者回饋', '/banners': '公告橫幅', '/audit-logs': '稽核紀錄',
  '/platform-links': '平台連結', '/service-access': '服務存取',
  '/service-clients': '服務客戶端', '/trusted-hosts': '信任主機', '/platform-settings': '平台設定',
}
const currentPageLabel = computed(() => {
  const path = route.path
  if (PAGE_LABELS[path]) return PAGE_LABELS[path]
  const hit = Object.keys(PAGE_LABELS).find((k) => k !== '/' && path.startsWith(k))
  return hit ? PAGE_LABELS[hit] : currentSegment.value
})
watch(currentPageLabel, (label) => {
  document.title = `${label} · ANILA 治理中心`
}, { immediate: true })


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
    setTimeout(() => {
      authStore.logout()
      router.push('/login')
    }, 1500)
  } catch (e) {
    pwError.value = extractError(e, '更新失敗')
  } finally {
    saving.value = false
  }
}

function handleLogout() {
  authStore.logout()
  router.push('/login')
}
</script>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--shell-topbar-h);
  padding: 0 var(--gap-5);
  background: var(--c-masthead);
  color: var(--c-masthead-fg);
  border-bottom: var(--border-w) solid var(--c-border);
  font-size: var(--t-sm);
  gap: var(--gap-4);
  min-width: 0;
}
.topbar__left,
.topbar__right {
  display: flex;
  align-items: center;
  gap: var(--gap-4);
  min-width: 0;
}
.topbar__menu {
  display: none;
  flex-shrink: 0;
  width: 32px;
  height: 28px;
  padding: 0;
  background: transparent;
  border: 1px solid color-mix(in oklab, var(--c-masthead-fg) 35%, transparent);
  border-radius: var(--r-md);
  color: var(--c-masthead-fg);
  cursor: pointer;
}
.topbar__menu-icon {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 3px;
  width: 14px;
  margin: 0 auto;
}
.topbar__menu-icon span {
  display: block;
  height: 1.5px;
  background: currentColor;
  border-radius: 1px;
}
.topbar__more { position: relative; flex-shrink: 0; }
.topbar__more-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 50;
  min-width: 8rem;
  padding: 4px;
  background: var(--c-surface-1);
  color: var(--c-fg-1);
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: 0 8px 24px rgba(17, 24, 39, 0.18);
}
.topbar__more-menu button {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 8px 10px;
  border-radius: var(--r-soft);
  color: var(--c-fg-1);
  cursor: pointer;
  white-space: nowrap;
}
.topbar__more-menu button:hover { background: var(--c-surface-2); }
.topbar__left :deep(.term-logo) { color: var(--c-masthead-fg); }
.topbar__left :deep(.term-logo__word),
.topbar__left :deep(.term-logo__sub),
.topbar__left :deep(.term-logo__sep) { color: var(--c-masthead-fg); }
.topbar__left :deep(.term-logo__mark) {
  /* 淺色刊頭用原色海軍標；深色刊頭由 --logo-on-masthead 反相成淺標。 */
  filter: var(--logo-on-masthead);
}
.topbar__crumb {
  padding-left: var(--gap-4);
  border-left: 1px solid color-mix(in oklab, var(--c-masthead-fg) 28%, transparent);
  font-size: var(--t-md);
  color: color-mix(in oklab, var(--c-masthead-fg) 92%, transparent);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.topbar__user {
  display: inline-flex;
  align-items: baseline;
  gap: var(--gap-2);
  color: var(--c-masthead-fg);
  flex-shrink: 0;
  white-space: nowrap;
}
.topbar__user-name { font-weight: 600; }
.topbar__user-role {
  font-size: var(--t-xs);
  padding: 1px 8px;
  border-radius: var(--r-pill);
  background: color-mix(in oklab, var(--c-masthead-fg) 16%, transparent);
  color: var(--c-masthead-fg);
}
.topbar__theme,
.topbar__action {
  background: transparent;
  border: 1px solid color-mix(in oklab, var(--c-masthead-fg) 35%, transparent);
  color: var(--c-masthead-fg);
  height: 28px;
  padding: 0 10px;
  border-radius: var(--r-md);
  font-size: var(--t-xs);
  cursor: pointer;
  flex-shrink: 0;
  white-space: nowrap;
  transition: background-color var(--motion-fast), border-color var(--motion-fast);
}
.topbar__theme:hover,
.topbar__action:hover {
  background: color-mix(in oklab, var(--c-masthead-fg) 14%, transparent);
  border-color: color-mix(in oklab, var(--c-masthead-fg) 60%, transparent);
}

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

@media (max-width: 900px) {
  .topbar { gap: var(--gap-2); padding: 0 var(--gap-3); }
  .topbar__left, .topbar__right { gap: var(--gap-2); }
  .topbar__menu { display: inline-flex; align-items: center; justify-content: center; }
  .topbar__user { display: none; }
  .topbar__crumb { min-width: 0; }
}
</style>
