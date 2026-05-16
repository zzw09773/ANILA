<template>
  <div class="login">
    <!-- Top status strip — mirrors the in-app statusbar so the design system
         is consistent before login as after. ----------------------------- -->
    <header class="login__topbar">
      <TermLogo :size="14" />
      <span class="login__topbar-rule">│</span>
      <span class="login__topbar-path">control-plane@anila <span class="term-caret" /></span>
      <span class="login__topbar-spacer" />
      <button class="login__theme" type="button" @click="toggleTheme" :title="`switch to ${otherTheme}`">
        {{ theme === 'dark' ? '◐' : '◑' }} {{ theme }}
      </button>
    </header>

    <main class="login__main">
      <section class="login__panel">
        <!-- Boot log ----------------------------------------------------- -->
        <ol class="bootlog" aria-hidden="true">
          <li v-for="(line, i) in bootLines" :key="i" class="bootlog__line" :style="{ animationDelay: `${i * 60}ms` }">
            <span class="bootlog__ts">[{{ line.ts }}]</span>
            <span :class="['bootlog__lvl', `is-${line.lvl}`]">{{ line.lvl }}</span>
            <span class="bootlog__msg">{{ line.msg }}</span>
          </li>
        </ol>

        <TermBox title="auth · local session" pad="lg" hint="local">
          <form class="login__form" @submit.prevent="handleLogin" autocomplete="on">
            <TermField label="username">
              <input
                v-model="username"
                type="text"
                class="term-input"
                placeholder="enter username"
                autocomplete="username"
                autofocus
                required
              />
            </TermField>
            <TermField label="password">
              <input
                v-model="password"
                type="password"
                class="term-input"
                placeholder="••••••••"
                autocomplete="current-password"
                required
              />
            </TermField>

            <div v-if="error" class="login__msg" :class="isPending ? 'is-warn' : 'is-err'">
              <span class="login__msg-glyph">{{ isPending ? '⏳' : '!' }}</span>
              <span>{{ error }}</span>
            </div>

            <div class="login__actions">
              <TermButton type="submit" variant="primary" :loading="loading" :label="loading ? 'auth' : 'sign-in'" />
              <TermButton variant="ghost" @click="openRegisterModal" label="register" />
            </div>

            <p class="login__hint">
              <TermKbd>↵</TermKbd> submit · <TermKbd>Tab</TermKbd> field
            </p>
          </form>
        </TermBox>

        <p class="login__legal">
          ANILA · CSP control plane &nbsp;·&nbsp; on-prem &nbsp;·&nbsp; admin approval required after registration
        </p>
      </section>
    </main>

    <!-- Register modal · admin-approved pending signup -->
    <TermModal :visible="showRegisterModal" title="register · admin-approved" width="480px" @close="closeRegisterModal">
      <div v-if="!regSuccess" class="login__reg">
        <TermField label="username">
          <input v-model="reg.username" class="term-input" placeholder="e.g. j.smith" autocomplete="username" />
        </TermField>
        <TermField label="email" hint="optional">
          <input v-model="reg.email" type="email" class="term-input" placeholder="user@corp.example" autocomplete="email" />
        </TermField>
        <TermField label="password" hint="8+ chars · upper · lower · symbol">
          <input v-model="reg.password" type="password" class="term-input" placeholder="••••••••" autocomplete="new-password" />
          <ul v-if="reg.password" class="login__rules">
            <li :class="reg.password.length >= 8 ? 'is-ok' : 'is-pending'">{{ reg.password.length >= 8 ? '●' : '○' }} 8+ chars</li>
            <li :class="/[A-Z]/.test(reg.password) ? 'is-ok' : 'is-pending'">{{ /[A-Z]/.test(reg.password) ? '●' : '○' }} uppercase</li>
            <li :class="/[a-z]/.test(reg.password) ? 'is-ok' : 'is-pending'">{{ /[a-z]/.test(reg.password) ? '●' : '○' }} lowercase</li>
            <li :class="hasSpecial(reg.password) ? 'is-ok' : 'is-pending'">{{ hasSpecial(reg.password) ? '●' : '○' }} symbol</li>
          </ul>
        </TermField>
        <div v-if="regError" class="login__msg is-err">! {{ regError }}</div>
      </div>
      <div v-else class="login__reg-done">
        <p class="login__msg is-ok">✓ {{ regSuccess }}</p>
        <p class="login__legal">an admin must approve this account before first login.</p>
      </div>

      <template #footer>
        <template v-if="!regSuccess">
          <TermButton variant="ghost" @click="closeRegisterModal" label="cancel" />
          <TermButton variant="primary" :disabled="!canRegister" :loading="registering" :label="registering ? 'submitting' : 'submit'" @click="handleRegister" />
        </template>
        <TermButton v-else variant="primary" @click="closeRegisterModal" label="close" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { register as registerApi } from '../api/auth'
import { useTheme } from '../composables/useTheme'
import TermLogo from '../components/cli/TermLogo.vue'
import TermBox from '../components/cli/TermBox.vue'
import TermButton from '../components/cli/TermButton.vue'
import TermField from '../components/cli/TermField.vue'
import TermKbd from '../components/cli/TermKbd.vue'
import TermModal from '../components/cli/TermModal.vue'

const router = useRouter()
const authStore = useAuthStore()
const { theme, toggleTheme } = useTheme()
const otherTheme = computed(() => (theme.value === 'dark' ? 'light' : 'dark'))

const username = ref('')
const password = ref('')
const error = ref('')
const isPending = ref(false)
const loading = ref(false)

const showRegisterModal = ref(false)
const registering = ref(false)
const regError = ref('')
const regSuccess = ref('')
const reg = ref({ username: '', email: '', password: '' })

const SPECIAL_CHARS = '!@#$%^&*()_+-=[]{}|;:,.<>?/~`"\'\\'
function hasSpecial(str) { return [...str].some(c => SPECIAL_CHARS.includes(c)) }

const canRegister = computed(() =>
  reg.value.username &&
  reg.value.password.length >= 8 &&
  /[A-Z]/.test(reg.value.password) &&
  /[a-z]/.test(reg.value.password) &&
  hasSpecial(reg.value.password)
)

const bootLines = ref([])
const t0 = Date.now()
function ts(offsetMs) {
  const d = new Date(t0 + offsetMs)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

onMounted(() => {
  bootLines.value = [
    { ts: ts(0),   lvl: 'info', msg: 'ANILA · CSP control plane — booting tty/0' },
    { ts: ts(40),  lvl: 'info', msg: 'local auth ready' },
    { ts: ts(120), lvl: 'ok',   msg: 'awaiting credentials' },
  ]
})

async function handleLogin() {
  error.value = ''
  isPending.value = false
  loading.value = true
  try {
    await authStore.login(username.value, password.value)
    router.push('/')
  } catch (e) {
    const detail = e.response?.data?.detail || 'login failed — check credentials'
    if (detail.includes('尚未開通') || detail.includes('等待核准') || detail.toLowerCase().includes('pending')) {
      isPending.value = true
    }
    error.value = detail
  } finally {
    loading.value = false
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
    const { data } = await registerApi(reg.value.username, reg.value.email || null, reg.value.password)
    regSuccess.value = data.message || 'registered — pending approval'
  } catch (e) {
    const detail = e.response?.data?.detail
    regError.value = Array.isArray(detail) ? detail.map(d => d.msg).join('; ') : (detail || 'register failed')
  } finally {
    registering.value = false
  }
}
</script>

<style scoped>
.login {
  min-height: 100vh;
  background: var(--c-bg);
  display: grid;
  grid-template-rows: var(--shell-topbar-h) 1fr;
}

.login__topbar {
  display: flex;
  align-items: center;
  gap: var(--gap-3);
  padding: 0 var(--gap-4);
  background: var(--c-surface-2);
  border-bottom: var(--border-w) solid var(--c-border);
  font-size: var(--t-xs);
  color: var(--c-fg-2);
}
.login__topbar-rule { color: var(--c-border-strong); }
.login__topbar-path { color: var(--c-fg-3); }
.login__topbar-path .term-caret { vertical-align: -0.05em; }
.login__topbar-spacer { flex: 1; }
.login__theme {
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
}
.login__theme:hover { color: var(--c-accent); border-color: var(--c-accent); }

.login__main {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--gap-6);
}

.login__panel {
  width: 100%;
  max-width: 520px;
  display: flex;
  flex-direction: column;
  gap: var(--gap-4);
}

.bootlog {
  list-style: none;
  margin: 0;
  padding: 0 0 var(--gap-2);
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  border-bottom: var(--border-w) dashed var(--c-border);
}
.bootlog__line {
  display: flex;
  gap: var(--gap-2);
  padding: 1px 0;
  opacity: 0;
  animation: boot-fade 220ms var(--easing) forwards;
}
@keyframes boot-fade {
  from { opacity: 0; transform: translateY(2px); }
  to   { opacity: 1; transform: none; }
}
.bootlog__ts { color: var(--c-fg-mute); font-variant-numeric: tabular-nums; }
.bootlog__lvl {
  text-transform: uppercase;
  letter-spacing: 0.06em;
  width: 32px;
  flex-shrink: 0;
}
.bootlog__lvl.is-info { color: var(--c-info); }
.bootlog__lvl.is-ok   { color: var(--c-ok); }
.bootlog__lvl.is-warn { color: var(--c-warn); }
.bootlog__lvl.is-err  { color: var(--c-danger); }

.login__form {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
}
.login__actions {
  display: flex;
  gap: var(--gap-2);
  margin-top: var(--gap-1);
}
.login__hint {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.login__msg {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  font-size: var(--t-xs);
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid;
}
.login__msg.is-err  { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.login__msg.is-warn { color: var(--c-warn);   border-color: var(--c-warn);   background: var(--c-warn-soft); }
.login__msg.is-ok   { color: var(--c-ok);     border-color: var(--c-ok);     background: var(--c-ok-soft); }
.login__msg-glyph { font-weight: 600; }

.login__legal {
  font-size: var(--t-2xs);
  color: var(--c-fg-mute);
  text-align: center;
  letter-spacing: 0.04em;
}

.login__reg {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
}
.login__rules {
  margin-top: var(--gap-1);
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px var(--gap-3);
  font-size: var(--t-2xs);
}
.login__rules li.is-ok      { color: var(--c-ok); }
.login__rules li.is-pending { color: var(--c-fg-3); }
.login__reg-done {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
}
</style>
