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

        <TermBox title="auth · local session" pad="lg" hint="local · ldap · oidc · card">
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
              <TermKbd>↵</TermKbd> submit · <TermKbd>Tab</TermKbd> field · <TermKbd>1</TermKbd>–<TermKbd>9</TermKbd> sso provider
            </p>
          </form>
        </TermBox>

        <TermBox v-if="oidcProviders.length" title="auth · single sign-on" pad="md">
          <ul class="login__sso">
            <li
              v-for="(provider, i) in oidcProviders"
              :key="provider.id"
              class="login__sso-row"
            >
              <span class="login__sso-key">[{{ i + 1 }}]</span>
              <span class="login__sso-name">{{ provider.name }}</span>
              <span class="login__sso-meta">oidc · {{ provider.button_text || `${provider.name} provider` }}</span>
              <button
                type="button"
                class="login__sso-btn"
                :disabled="oidcLoadingId === provider.id"
                @click="handleOidcLogin(provider)"
              >
                {{ oidcLoadingId === provider.id ? 'redirecting…' : 'connect →' }}
              </button>
            </li>
          </ul>
        </TermBox>

        <!-- branch SSO: 中科院憑證卡登入 ------------------------------------ -->
        <TermBox title="auth · pki card" pad="md" hint="ncsist · 中科院憑證卡">
          <form class="login__form" @submit.prevent="handleCardLogin" autocomplete="off">
            <p class="login__hint" style="margin: 0 0 var(--gap-2);">
              請插入憑證卡，並確認本機元件運作中（<span style="font-family: var(--font-mono, monospace);">{{ cardComponentOrigin }}</span>）。
            </p>

            <!-- Step 1: 偵測卡片 — 在輸入 PIN 前讓使用者確認自己的卡片 -->
            <div v-if="!detectedCard" class="login__actions">
              <TermButton
                type="button"
                variant="ghost"
                :loading="detectLoading"
                :label="detectLoading ? 'detecting' : 'detect card'"
                @click="handleDetectCard"
              />
            </div>

            <!-- Step 4: 顯示使用者資訊 (偵測成功後) -->
            <div v-else class="login__msg" style="background: var(--c-surface-2); border-color: var(--c-border); color: var(--c-fg-2);">
              <div style="flex: 1;">
                <div style="margin-bottom: 4px;">
                  <strong>{{ detectedCard.displayName }}</strong>
                  <span style="color: var(--c-fg-3); margin-left: 8px;">
                    員工編號 {{ detectedCard.employeeId }}
                  </span>
                </div>
                <div style="font-size: var(--t-2xs); color: var(--c-fg-3);">
                  {{ detectedCard.email || '(no email)' }} · card #{{ detectedCard.cardSN || 'n/a' }}
                </div>
              </div>
              <button
                type="button"
                style="background: transparent; border: 0; color: var(--c-fg-3); cursor: pointer; font-size: var(--t-2xs);"
                @click="resetDetectedCard"
                title="重新偵測"
              >×</button>
            </div>

            <!-- Step 2-3: PIN → 簽章 (只有偵測成功才開啟) -->
            <TermField v-if="detectedCard" label="pin">
              <input
                v-model="cardPin"
                type="password"
                inputmode="numeric"
                maxlength="6"
                class="term-input"
                placeholder="6 位數字"
                autocomplete="off"
                autofocus
              />
            </TermField>

            <div v-if="cardError" class="login__msg is-err">
              <span class="login__msg-glyph">!</span>
              <span>{{ cardError }}</span>
            </div>

            <!-- Step 5: 提交 -->
            <div v-if="detectedCard" class="login__actions">
              <TermButton
                type="submit"
                variant="primary"
                :loading="cardLoading"
                :disabled="!cardPin"
                :label="cardLoading ? 'verifying' : 'sign &amp; submit'"
              />
            </div>
          </form>
        </TermBox>

        <p class="login__legal">
          ANILA · CSP control plane &nbsp;·&nbsp; on-prem &nbsp;·&nbsp; access requires admin approval
        </p>
      </section>
    </main>

    <!-- Pending registration / approval modal (branch SSO) ----------------- -->
    <TermModal
      :visible="!!pending"
      :title="
        pending && pending.status === 'pending_approval'
          ? 'registration · awaiting approval'
          : 'registration · complete profile'
      "
      width="480px"
      @close="resetPending"
    >
      <div v-if="pending && pending.status === 'pending_registration'" class="login__reg">
        <div class="login__msg" style="background: var(--c-surface-2); border-color: var(--c-border); color: var(--c-fg-2);">
          <div>
            <div style="margin-bottom: 4px;">
              <strong>{{ pending.display_name }}</strong>
              <span style="color: var(--c-fg-3); margin-left: 8px;">
                員工編號 {{ pending.employee_id }}
              </span>
            </div>
            <div style="font-size: var(--t-2xs); color: var(--c-fg-3);">
              {{ pending.email || '(no email)' }}
            </div>
          </div>
        </div>
        <p style="font-size: var(--t-xs); color: var(--c-fg-2); margin: 8px 0;">
          {{ pending.message }}
        </p>
        <TermField label="department · 單位">
          <select v-model="pendingDeptId" class="term-input">
            <option :value="null" disabled>請選擇單位</option>
            <option v-for="dept in pendingDepartments" :key="dept.id" :value="dept.id">
              {{ dept.name }}
            </option>
          </select>
          <p v-if="pendingDepartments.length === 0" style="font-size: var(--t-2xs); color: var(--c-warn); margin-top: 4px;">
            尚無可選單位 — 請通知管理員到 admin 介面建立 departments 後再試。
          </p>
        </TermField>
        <div v-if="pendingError" class="login__msg is-err">! {{ pendingError }}</div>
      </div>
      <div v-else-if="pending && pending.status === 'pending_approval'" class="login__reg-done">
        <p class="login__msg" style="background: var(--c-warn-soft); border-color: var(--c-warn); color: var(--c-warn);">
          ⏳ {{ pending.message }}
        </p>
        <p class="login__legal">
          {{ pending.display_name }} ({{ pending.employee_id }}) — 一旦管理員核准，下次刷卡即可登入。
        </p>
      </div>

      <template #footer>
        <template v-if="pending && pending.status === 'pending_registration'">
          <TermButton variant="ghost" @click="resetPending" label="cancel" />
          <TermButton
            variant="primary"
            :disabled="!pendingDeptId || pendingDepartments.length === 0"
            :loading="pendingSubmitting"
            :label="pendingSubmitting ? 'submitting' : 'submit'"
            @click="handleSubmitRegistration"
          />
        </template>
        <TermButton v-else variant="primary" @click="resetPending" label="close" />
      </template>
    </TermModal>

    <!-- Register modal ------------------------------------------------- -->
    <TermModal :visible="showRegisterModal" title="register · self-service" width="480px" @close="closeRegisterModal">
      <div v-if="!regSuccess" class="login__reg">
        <TermField label="username">
          <input v-model="reg.username" class="term-input" placeholder="e.g. j.smith" autocomplete="username" />
        </TermField>
        <TermField label="email">
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
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import {
  cardCompleteRegistration,
  cardListDepartments,
  getOidcStartUrl,
  listPublicAuthProviders,
  register as registerApi,
} from '../api/auth'
import {
  CARD_COMPONENT_ORIGIN,
  CardComponentNotInstalledError,
  CardNotInsertedError,
  detectCard,
} from '../api/caAuth'
import { useTheme } from '../composables/useTheme'
import TermLogo from '../components/cli/TermLogo.vue'
import TermBox from '../components/cli/TermBox.vue'
import TermButton from '../components/cli/TermButton.vue'
import TermField from '../components/cli/TermField.vue'
import TermKbd from '../components/cli/TermKbd.vue'
import TermModal from '../components/cli/TermModal.vue'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const { theme, toggleTheme } = useTheme()
const otherTheme = computed(() => (theme.value === 'dark' ? 'light' : 'dark'))

// branch SSO: 其他 SPA (anila-ui / ANILALM) 在 unauthenticated 時把使用者
// 送來這裡並夾帶 ?next=<原 URL>。登入成功後跳回去；沒帶 next 就回 dashboard。
//
// 接受兩種 next 形式：
//   1. 相對路徑（以 / 開頭、不含 //） — 例：/dashboard
//   2. 同 hostname 的 absolute URL — 例：https://172.16.120.35:4443/app/...
//      （4443 port 的 anila-ui 跨 port 跳回時必要）
// 拒絕跨 hostname、javascript:、//evil.com 等 open-redirect 攻擊向量。
function resolveNextDestination() {
  const candidate = route.query.next
  if (typeof candidate !== 'string' || !candidate) return '/'

  // 嘗試當 absolute URL parse；同 hostname 才接受
  try {
    const url = new URL(candidate)
    if (url.hostname === window.location.hostname && (url.protocol === 'https:' || url.protocol === 'http:')) {
      return url.toString()
    }
    return '/'
  } catch {
    // 不是 absolute URL — 走相對路徑驗證
    if (!candidate.startsWith('/') || candidate.startsWith('//')) return '/'
    return candidate
  }
}

const username = ref('')
const password = ref('')
const error = ref('')
const isPending = ref(false)
const loading = ref(false)
const oidcLoadingId = ref(null)
const providers = ref([])

// branch SSO: 中科院憑證卡登入 state
const cardPin = ref('')
const cardError = ref('')
const cardLoading = ref(false)
const cardComponentOrigin = CARD_COMPONENT_ORIGIN
// Step 1 (detect) 的結果。null = 還沒偵測;有值 = 顯示使用者資訊並開啟 PIN 欄。
const detectedCard = ref(null)
const detectLoading = ref(false)

// Pending registration / approval state（首次刷卡未核准走的支線）
const pending = ref(null)            // backend 回的 payload (employee_id, name, email, registration_token, ...)
const pendingDepartments = ref([])   // 從 /card/registration/departments 拿的 active dept list
const pendingDeptId = ref(null)
const pendingSubmitting = ref(false)
const pendingError = ref('')

const showRegisterModal = ref(false)
const registering = ref(false)
const regError = ref('')
const regSuccess = ref('')
const reg = ref({ username: '', email: '', password: '' })

// Stable boot sequence — purely cosmetic but reinforces the terminal frame.
// Timestamps anchored to load to feel real instead of random.
const bootLines = ref([])
const t0 = Date.now()
function ts(offsetMs) {
  const d = new Date(t0 + offsetMs)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const SPECIAL_CHARS = '!@#$%^&*()_+-=[]{}|;:,.<>?/~`"\'\\'
function hasSpecial(str) { return [...str].some(c => SPECIAL_CHARS.includes(c)) }

const canRegister = computed(() =>
  reg.value.username &&
  reg.value.email &&
  reg.value.password.length >= 8 &&
  /[A-Z]/.test(reg.value.password) &&
  /[a-z]/.test(reg.value.password) &&
  hasSpecial(reg.value.password)
)

const oidcProviders = computed(() =>
  providers.value.filter(p => p.provider_type === 'oidc')
)

async function fetchProviders() {
  try {
    const { data } = await listPublicAuthProviders()
    providers.value = data
  } catch {
    providers.value = []
  }
  // Patch the boot log with whatever the providers tell us is wired up.
  bootLines.value = [
    { ts: ts(0),   lvl: 'info', msg: 'ANILA · CSP control plane — booting tty/0' },
    { ts: ts(40),  lvl: 'info', msg: 'loading auth providers...' },
    { ts: ts(120), lvl: 'ok',   msg: `${providers.value.length} provider(s) registered` },
    { ts: ts(180), lvl: 'info', msg: 'awaiting credentials' },
  ]
}

onMounted(() => {
  fetchProviders()
  document.addEventListener('keydown', handleHotkey)
})

function handleHotkey(e) {
  // Number keys trigger SSO providers when nothing else has focus on inputs.
  if (e.target?.tagName === 'INPUT') return
  const num = Number(e.key)
  if (Number.isFinite(num) && num >= 1 && num <= oidcProviders.value.length) {
    handleOidcLogin(oidcProviders.value[num - 1])
  }
}

async function handleLogin() {
  error.value = ''
  isPending.value = false
  loading.value = true
  try {
    await authStore.login(username.value, password.value, { auth_source: 'local' })
    // 統一用 full page navigation：next 可能是 /app, /anilalm/, / 任一個，
    // 而 4443 / 443 port 的 nginx 對 / 的 catch-all 不一定是 myCSPPlatform Vue
    // (4443 port 是 anila-ui)。client-side router.push 只會留在當前 SPA，
    // 反而導致 user 期望「進 ANILA UI」結果停在 myCSPPlatform dashboard。
    // 一律走 browser reload 讓 nginx 重新決定 routing。
    window.location.assign(resolveNextDestination())
  } catch (e) {
    const detail = e.response?.data?.detail || 'login failed — check credentials'
    if (detail.includes('等待核准') || detail.toLowerCase().includes('pending')) {
      isPending.value = true
    }
    error.value = detail
  } finally {
    loading.value = false
  }
}

// Step 1: 偵測卡片 — popup → GetUserCert → 抽 cert claims 顯示。
// 失敗 (沒卡、PIN 不需要、本機元件未啟動) 不污染 cardPin 流程,只清這層。
async function handleDetectCard() {
  cardError.value = ''
  detectLoading.value = true
  try {
    detectedCard.value = await detectCard({ componentOrigin: cardComponentOrigin })
  } catch (e) {
    detectedCard.value = null
    if (e instanceof CardComponentNotInstalledError) {
      cardError.value = e.message
    } else if (e instanceof CardNotInsertedError) {
      cardError.value = '卡片未插入或本機元件無法讀取卡片,請插入卡片後重試。'
    } else {
      cardError.value = e?.message || '偵測卡片失敗'
    }
  } finally {
    detectLoading.value = false
  }
}

function resetDetectedCard() {
  detectedCard.value = null
  cardPin.value = ''
  cardError.value = ''
}

async function handleCardLogin() {
  cardError.value = ''
  if (!detectedCard.value) {
    cardError.value = '請先點「detect card」偵測卡片'
    return
  }
  if (!cardPin.value) {
    cardError.value = '請輸入 PIN 碼'
    return
  }
  cardLoading.value = true
  try {
    const result = await authStore.loginWithCard({ pin: cardPin.value })

    if (result.status === 'ok') {
      // 統一用 full page navigation：next 可能是 /app, /anilalm/, / 任一個，
      // 而 4443 / 443 port 的 nginx 對 / 的 catch-all 不一定是 myCSPPlatform Vue
      // (4443 port 是 anila-ui)。client-side router.push 只會留在當前 SPA，
      // 反而導致 user 期望「進 ANILA UI」結果停在 myCSPPlatform dashboard。
      // 一律走 browser reload 讓 nginx 重新決定 routing。
      window.location.assign(resolveNextDestination())
      return
    }

    // Pending 狀態 — 切換到對應的表單 / 等待頁
    pending.value = result
    if (result.status === 'pending_registration') {
      // 拉 departments list 給 dropdown
      try {
        const { data } = await cardListDepartments()
        pendingDepartments.value = data
      } catch (deptErr) {
        pendingError.value = '無法載入單位清單：' + (deptErr.message || deptErr)
      }
    }
  } catch (e) {
    cardError.value = e.response?.data?.detail || e.message || 'card sign-in failed'
  } finally {
    cardLoading.value = false
  }
}

async function handleSubmitRegistration() {
  pendingError.value = ''
  if (!pendingDeptId.value) {
    pendingError.value = '請選擇單位'
    return
  }
  pendingSubmitting.value = true
  try {
    const { data } = await cardCompleteRegistration({
      registration_token: pending.value.registration_token,
      department_id: Number(pendingDeptId.value),
    })
    // 把 pending 切到 approval-waiting 狀態，UI 切換顯示等待訊息
    pending.value = {
      ...pending.value,
      status: 'pending_approval',
      registration_token: null,
      message: data.message,
    }
  } catch (e) {
    pendingError.value = e.response?.data?.detail || e.message || '註冊失敗'
  } finally {
    pendingSubmitting.value = false
  }
}

function resetPending() {
  pending.value = null
  pendingDepartments.value = []
  pendingDeptId.value = null
  pendingError.value = ''
  cardPin.value = ''
}

async function handleOidcLogin(provider) {
  if (!provider) return
  error.value = ''
  isPending.value = false
  oidcLoadingId.value = provider.id
  try {
    const { data } = await getOidcStartUrl(provider.id, '/')
    window.location.href = data.authorization_url
  } catch (e) {
    error.value = e.response?.data?.detail || 'unable to start sso flow'
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

.login__sso {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.login__sso-row {
  display: grid;
  grid-template-columns: 28px 1fr auto auto;
  align-items: center;
  gap: var(--gap-3);
  padding: var(--gap-2) 0;
  border-top: var(--border-w) dashed var(--c-border);
  font-size: var(--t-sm);
}
.login__sso-row:first-child { border-top: 0; }
.login__sso-key { color: var(--c-fg-3); font-size: var(--t-xs); }
.login__sso-name { color: var(--c-fg-1); font-weight: 500; }
.login__sso-meta { color: var(--c-fg-3); font-size: var(--t-2xs); letter-spacing: 0.04em; }
.login__sso-btn {
  background: transparent;
  color: var(--c-accent);
  border: 0;
  padding: 0;
  font: inherit;
  cursor: pointer;
  font-size: var(--t-xs);
}
.login__sso-btn:hover { text-decoration: underline; }
.login__sso-btn:disabled { color: var(--c-fg-3); cursor: not-allowed; }

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
