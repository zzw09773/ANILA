<template>
  <header class="anila-topbar">
    <TermLogo />
    <AnilaAccountMenu
      @change-password="showChangePwModal = true"
      @logout="handleLogout"
    />
  </header>

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
import { useAuthStore } from '../../stores/auth'
import { changePassword } from '../../api/auth'
import TermLogo from '../cli/TermLogo.vue'
import TermModal from '../cli/TermModal.vue'
import TermField from '../cli/TermField.vue'
import TermButton from '../cli/TermButton.vue'
import AnilaAccountMenu from './AnilaAccountMenu.vue'
import { extractError } from '../../api/errors'
import { loginHref } from '../../utils/appOrigins'

const authStore = useAuthStore()

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
