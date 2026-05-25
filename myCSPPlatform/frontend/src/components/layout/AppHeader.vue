<template>
  <header class="topbar">
    <div class="topbar__left">
      <TermLogo :size="14" />
      <span class="topbar__rule">│</span>
      <span class="topbar__path">
        <span class="topbar__path-prefix">{{ hostPrompt }}</span>
        <span class="topbar__path-segment">{{ currentSegment }}</span>
        <span class="term-caret" aria-hidden="true" />
      </span>
    </div>

    <div class="topbar__right">
      <span class="topbar__hints">
        <span class="topbar__hint"><TermKbd>?</TermKbd> shortcuts</span>
      </span>
      <button
        class="topbar__theme"
        type="button"
        :aria-label="`switch to ${otherTheme} theme`"
        :title="`switch to ${otherTheme} theme`"
        @click="toggleTheme"
      >
        <span class="topbar__theme-icon">{{ theme === 'dark' ? '◐' : '◑' }}</span>
        <span class="topbar__theme-label">{{ theme }}</span>
      </button>

      <span class="topbar__rule">│</span>

      <span class="topbar__user">
        <span class="topbar__user-name">{{ authStore.user?.username || 'guest' }}</span>
        <span class="topbar__user-role" :class="`is-${authStore.user?.role || 'user'}`">
          @{{ authStore.user?.role || 'guest' }}
        </span>
      </span>

      <button class="topbar__action" type="button" @click="showChangePwModal = true">
        change-pw
      </button>
      <button class="topbar__action topbar__action--danger" type="button" @click="handleLogout">
        logout
      </button>
    </div>
  </header>

  <!-- Change-password modal — terminal style ----------------------------- -->
  <TermModal :visible="showChangePwModal" title="change password" width="440px" @close="closeChangePw">
    <div class="pw-grid">
      <TermField label="current password">
        <input v-model="pw.current" type="password" class="term-input" placeholder="••••••••" autocomplete="current-password" />
      </TermField>

      <TermField label="new password" :hint="pw.new ? '' : 'at least 8 chars · upper · lower · symbol'">
        <input v-model="pw.new" type="password" class="term-input" placeholder="••••••••" autocomplete="new-password" />
        <ul v-if="pw.new" class="pw-rules">
          <li :class="pwRule(pw.new.length >= 8)">{{ pwGlyph(pw.new.length >= 8) }} 8+ characters</li>
          <li :class="pwRule(/[A-Z]/.test(pw.new))">{{ pwGlyph(/[A-Z]/.test(pw.new)) }} uppercase</li>
          <li :class="pwRule(/[a-z]/.test(pw.new))">{{ pwGlyph(/[a-z]/.test(pw.new)) }} lowercase</li>
          <li :class="pwRule(hasSpecial(pw.new))">{{ pwGlyph(hasSpecial(pw.new)) }} symbol</li>
        </ul>
      </TermField>

      <TermField
        label="confirm new password"
        :error="pw.confirm && pw.new !== pw.confirm ? 'mismatch' : ''"
      >
        <input v-model="pw.confirm" type="password" class="term-input" placeholder="••••••••" autocomplete="new-password" />
      </TermField>

      <div v-if="pwError" class="pw-msg pw-msg--err">! {{ pwError }}</div>
      <div v-if="pwSuccess" class="pw-msg pw-msg--ok">✓ {{ pwSuccess }}</div>
    </div>

    <template #footer>
      <TermButton variant="ghost" @click="closeChangePw" label="cancel" />
      <TermButton
        variant="primary"
        :disabled="!canSubmit"
        :loading="saving"
        :label="saving ? 'saving' : 'update'"
        @click="handleChangePassword"
      />
    </template>
  </TermModal>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { changePassword } from '../../api/auth'
import { useTheme } from '../../composables/useTheme'
import TermLogo from '../cli/TermLogo.vue'
import TermKbd from '../cli/TermKbd.vue'
import TermModal from '../cli/TermModal.vue'
import TermField from '../cli/TermField.vue'
import TermButton from '../cli/TermButton.vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const { theme, toggleTheme } = useTheme()

const otherTheme = computed(() => (theme.value === 'dark' ? 'light' : 'dark'))

const hostPrompt = computed(() => {
  const role = authStore.user?.role || 'guest'
  return `csp-${role}@anila:`
})

const segmentMap = {
  '/': '/dashboard',
  '/api-keys': '/api-keys',
  '/models': '/models',
  '/usage': '/usage',
  '/users': '/admin/users',
  '/departments': '/admin/departments',
  '/alerts': '/admin/alerts',
  '/audit-logs': '/admin/audit',
  '/platform-links': '/admin/platform-links',
  '/service-access': '/admin/service-access',
  '/developer/guide': '/dev/guide',
  '/developer/agents': '/dev/agents',
  '/knowledge-collections': '/dev/collections',
}
const currentSegment = computed(() => {
  if (segmentMap[route.path]) return segmentMap[route.path]
  if (route.path.startsWith('/knowledge-collections/')) {
    return route.path.endsWith('/evaluator')
      ? '/dev/collections/evaluator'
      : '/dev/collections/detail'
  }
  return route.path
})

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
    pwSuccess.value = 'password updated — re-auth required'
    setTimeout(() => {
      authStore.logout()
      router.push('/login')
    }, 1500)
  } catch (e) {
    const detail = e.response?.data?.detail
    pwError.value = Array.isArray(detail) ? detail.map(d => d.msg).join('; ') : (detail || 'update failed')
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
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.topbar__path-prefix {
  color: var(--c-fg-3);
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
  letter-spacing: 0.08em;
  text-transform: uppercase;
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
  letter-spacing: 0.05em;
  text-transform: lowercase;
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
  letter-spacing: 0.05em;
  text-transform: lowercase;
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
