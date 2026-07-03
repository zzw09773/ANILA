<template>
  <div class="login">
    <!-- Slim top bar — brand mark + theme toggle only (terminal path chrome
         removed per redesign §3.2). ------------------------------------- -->
    <header class="login__topbar">
      <TermLogo :size="14" />
      <span class="login__topbar-spacer" />
      <button class="login__theme" type="button" @click="toggleTheme" :title="`切換至${otherTheme === 'light' ? '淺色' : '深色'}主題`">
        {{ theme === 'dark' ? '◐' : '◑' }} {{ theme }}
      </button>
    </header>

    <main class="login__main">
      <section class="login__panel">
        <!-- Page hero title ---------------------------------------------- -->
        <header class="login__hero">
          <h1 class="login__title">ANILA 治理中心</h1>
          <p class="login__subtitle">知識治理控制台 · 請使用自然人憑證卡登入</p>
        </header>

        <!-- Card login = primary hero card ------------------------------- -->
        <!-- 卡登流程邏輯逐字保留:handleDetectCard / detectedCard /
             cardComponentOrigin / cardPin / handleCardLogin。只改版型。 -->
        <section class="login__card">
          <h2 class="login__card-title">自然人憑證卡登入</h2>

          <form class="login__form" @submit.prevent="handleCardLogin" autocomplete="off">
            <p v-if="!detectedCard" class="login__card-lead">請插入自然人憑證卡</p>
            <p class="login__card-note">
              並確認本機元件運作中（<span style="font-family: var(--font-mono, monospace);">{{ cardComponentOrigin }}</span>）。
            </p>

            <!-- Step 1: 偵測卡片 — 在輸入 PIN 前讓使用者確認自己的卡片 -->
            <div v-if="!detectedCard" class="login__actions">
              <TermButton
                type="button"
                variant="primary"
                :loading="detectLoading"
                :label="detectLoading ? '偵測中' : '偵測憑證卡'"
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
                  {{ detectedCard.email || '（無 email）' }} · card #{{ detectedCard.cardSN || 'n/a' }}
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
            <TermField v-if="detectedCard" label="PIN">
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
                :label="cardLoading ? '驗證中' : '簽章送出'"
              />
            </div>
          </form>
        </section>

        <!-- Secondary: 帳密 + OIDC 收合在「其他登入方式」下,降低視覺權重 --- -->
        <details class="login__more">
          <summary class="login__more-summary">其他登入方式</summary>

          <div class="login__more-body">
            <!-- 本地帳密登入 -------------------------------------------- -->
            <div class="login__section">
              <h3 class="login__section-title">帳號密碼登入</h3>
              <form class="login__form" @submit.prevent="handleLogin" autocomplete="on">
                <TermField label="帳號">
                  <input
                    v-model="username"
                    type="text"
                    class="term-input"
                    placeholder="輸入帳號"
                    autocomplete="username"
                    required
                  />
                </TermField>
                <TermField label="密碼">
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
                  <TermButton type="submit" variant="primary" :loading="loading" :label="loading ? '驗證中' : '登入'" />
                  <TermButton variant="ghost" @click="openRegisterModal" label="註冊" />
                </div>

                <p class="login__hint">
                  <TermKbd>↵</TermKbd> 送出 · <TermKbd>Tab</TermKbd> 切換欄位 · <TermKbd>1</TermKbd>–<TermKbd>9</TermKbd> 單一登入提供者
                </p>
              </form>
            </div>

            <!-- 單一登入 (OIDC) ----------------------------------------- -->
            <div v-if="oidcProviders.length" class="login__section">
              <h3 class="login__section-title">單一登入（SSO）</h3>
              <ul class="login__sso">
                <li
                  v-for="(provider, i) in oidcProviders"
                  :key="provider.id"
                  class="login__sso-row"
                >
                  <span class="login__sso-key">[{{ i + 1 }}]</span>
                  <span class="login__sso-name">{{ provider.name }}</span>
                  <span class="login__sso-meta">{{ provider.button_text || `${provider.name} 提供者` }}</span>
                  <button
                    type="button"
                    class="login__sso-btn"
                    :disabled="oidcLoadingId === provider.id"
                    @click="handleOidcLogin(provider)"
                  >
                    {{ oidcLoadingId === provider.id ? '導向中…' : '連線 →' }}
                  </button>
                </li>
              </ul>
            </div>
          </div>
        </details>

        <p class="login__legal">
          ANILA · CSP 控制平面 &nbsp;·&nbsp; 地端部署 &nbsp;·&nbsp; 存取需管理員核准
        </p>
      </section>
    </main>

    <!-- Pending registration / approval modal (branch SSO) ----------------- -->
    <TermModal
      :visible="!!pending"
      :title="
        pending && pending.status === 'pending_approval'
          ? '註冊 · 等待核准'
          : '註冊 · 完成資料'
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
              {{ pending.email || '（無 email）' }}
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
          <TermButton variant="ghost" @click="resetPending" label="取消" />
          <TermButton
            variant="primary"
            :disabled="!pendingDeptId || pendingDepartments.length === 0"
            :loading="pendingSubmitting"
            :label="pendingSubmitting ? '送出中' : '送出'"
            @click="handleSubmitRegistration"
          />
        </template>
        <TermButton v-else variant="primary" @click="resetPending" label="關閉" />
      </template>
    </TermModal>

    <!-- Register modal ------------------------------------------------- -->
    <TermModal :visible="showRegisterModal" title="註冊 · 自助" width="480px" @close="closeRegisterModal">
      <div v-if="!regSuccess" class="login__reg">
        <TermField label="帳號">
          <input v-model="reg.username" class="term-input" placeholder="e.g. j.smith" autocomplete="username" />
        </TermField>
        <TermField label="email">
          <input v-model="reg.email" type="email" class="term-input" placeholder="user@corp.example" autocomplete="email" />
        </TermField>
        <TermField label="密碼" hint="8 字元以上 · 大寫 · 小寫 · 符號">
          <input v-model="reg.password" type="password" class="term-input" placeholder="••••••••" autocomplete="new-password" />
          <ul v-if="reg.password" class="login__rules">
            <li :class="reg.password.length >= 8 ? 'is-ok' : 'is-pending'">{{ reg.password.length >= 8 ? '●' : '○' }} 8 字元以上</li>
            <li :class="/[A-Z]/.test(reg.password) ? 'is-ok' : 'is-pending'">{{ /[A-Z]/.test(reg.password) ? '●' : '○' }} 大寫</li>
            <li :class="/[a-z]/.test(reg.password) ? 'is-ok' : 'is-pending'">{{ /[a-z]/.test(reg.password) ? '●' : '○' }} 小寫</li>
            <li :class="hasSpecial(reg.password) ? 'is-ok' : 'is-pending'">{{ hasSpecial(reg.password) ? '●' : '○' }} 符號</li>
          </ul>
        </TermField>
        <div v-if="regError" class="login__msg is-err">! {{ regError }}</div>
      </div>
      <div v-else class="login__reg-done">
        <p class="login__msg is-ok">✓ {{ regSuccess }}</p>
        <p class="login__legal">首次登入前需管理員核准此帳號。</p>
      </div>

      <template #footer>
        <template v-if="!regSuccess">
          <TermButton variant="ghost" @click="closeRegisterModal" label="取消" />
          <TermButton variant="primary" :disabled="!canRegister" :loading="registering" :label="registering ? '送出中' : '送出'" @click="handleRegister" />
        </template>
        <TermButton v-else variant="primary" @click="closeRegisterModal" label="關閉" />
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
// 送來這裡並夾帶 ?next=<原 URL>。登入成功後跳回去；沒帶 next 就進 ANILA
// 工作區 (/anila/app) — 一般使用者的日常入口是任務中心,不是治理 dashboard;
// admin 要進治理中心走 shell 導覽的「治理中心」或直接開 /。
//
// 接受兩種 next 形式：
//   1. 相對路徑（以 / 開頭、不含 //） — 例：/dashboard
//   2. 同 hostname 的 absolute URL — 例：https://172.16.120.35:4443/app/...
//      （4443 port 的 anila-ui 跨 port 跳回時必要）
// 拒絕跨 hostname、javascript:、//evil.com 等 open-redirect 攻擊向量。
const DEFAULT_POST_LOGIN_DEST = '/anila/app'

function resolveNextDestination() {
  const candidate = route.query.next
  if (typeof candidate !== 'string' || !candidate) return DEFAULT_POST_LOGIN_DEST

  // 嘗試當 absolute URL parse；同 hostname 才接受
  try {
    const url = new URL(candidate)
    if (url.hostname === window.location.hostname && (url.protocol === 'https:' || url.protocol === 'http:')) {
      return url.toString()
    }
    return DEFAULT_POST_LOGIN_DEST
  } catch {
    // 不是 absolute URL — 走相對路徑驗證
    if (!candidate.startsWith('/') || candidate.startsWith('//')) return DEFAULT_POST_LOGIN_DEST
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
    const detail = e.response?.data?.detail || '登入失敗 — 請檢查帳號密碼'
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
    cardError.value = '請先點「偵測卡片」'
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
    cardError.value = e.response?.data?.detail || e.message || '憑證卡登入失敗'
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
    error.value = e.response?.data?.detail || '無法啟動 SSO 流程'
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
    regError.value = Array.isArray(detail) ? detail.map(d => d.msg).join('; ') : (detail || '註冊失敗')
  } finally {
    registering.value = false
  }
}
</script>

<style scoped>
.login {
  min-height: 100dvh;
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
  padding: var(--gap-8) var(--gap-6);
}

.login__panel {
  width: 100%;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  gap: var(--gap-6);
}

/* Page hero title --------------------------------------------------------- */
.login__hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--gap-2);
  text-align: center;
}
.login__title {
  font-family: var(--font-sans);
  font-size: var(--t-3xl);
  font-weight: 600;
  color: var(--c-fg-1);
  letter-spacing: var(--tracking-tight);
  margin: 0;
}
.login__subtitle {
  font-size: var(--t-sm);
  color: var(--c-fg-3);
  margin: 0;
}

/* Card login = primary hero card ------------------------------------------ */
.login__card {
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  border-top: var(--border-w-strong) solid var(--c-accent);
  border-radius: var(--r-md);
  padding: var(--gap-6);
  display: flex;
  flex-direction: column;
  gap: var(--gap-4);
}
.login__card-title {
  font-family: var(--font-sans);
  font-size: var(--t-xl);
  font-weight: 600;
  color: var(--c-accent-strong);
  letter-spacing: var(--tracking-tight);
  margin: 0;
}
.login__card-lead {
  font-size: var(--t-md);
  color: var(--c-fg-1);
  margin: 0;
}
.login__card-note {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  margin: 0;
  line-height: var(--lh-base);
}

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
  border-radius: var(--r-soft);
}
.login__msg.is-err  { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.login__msg.is-warn { color: var(--c-warn);   border-color: var(--c-warn);   background: var(--c-warn-soft); }
.login__msg.is-ok   { color: var(--c-ok);     border-color: var(--c-ok);     background: var(--c-ok-soft); }
.login__msg-glyph { font-weight: 600; }

/* Secondary: 「其他登入方式」收合區 --------------------------------------- */
.login__more {
  border: 0;
}
.login__more-summary {
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: var(--gap-3);
  color: var(--c-fg-3);
  font-size: var(--t-sm);
  padding: var(--gap-1) 0;
  user-select: none;
}
.login__more-summary::-webkit-details-marker { display: none; }
.login__more-summary::before,
.login__more-summary::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--c-border);
}
.login__more-summary:hover { color: var(--c-accent); }
.login__more[open] .login__more-summary { margin-bottom: var(--gap-4); }

.login__more-body {
  display: flex;
  flex-direction: column;
  gap: var(--gap-5);
}
.login__section {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
}
.login__section-title {
  font-size: var(--t-2xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
  color: var(--c-fg-3);
  font-weight: 600;
  margin: 0;
}

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
  border-top: var(--border-w) solid var(--c-border);
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
