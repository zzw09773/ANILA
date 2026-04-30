<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">admin · auth</p>
        <h1 class="page-head__title">sso · oidc</h1>
        <p class="page-head__sub">external identity providers · oidc only · client-secret aes-gcm at rest</p>
      </div>
      <TermButton variant="primary" @click="openCreateModal" label="add provider" />
    </header>

    <div v-if="pageError" class="feedback is-err">! {{ pageError }}</div>

    <TermBox :title="`providers · ${providers.length}`" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th>name</th>
            <th style="width: 100px">type</th>
            <th>default department</th>
            <th style="width: 100px">status</th>
            <th style="width: 18%">ops</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in providers" :key="p.id">
            <td>
              <div class="cell-strong">{{ p.name }}</div>
              <div class="cell-meta">{{ p.button_text || '— no button text —' }}</div>
            </td>
            <td><TermBadge variant="info">{{ p.provider_type }}</TermBadge></td>
            <td class="cell-meta">{{ p.default_department_name || '—' }}</td>
            <td><TermBadge :variant="p.is_active ? 'ok' : 'danger'" dot>{{ p.is_active ? 'active' : 'inactive' }}</TermBadge></td>
            <td>
              <div class="row-actions">
                <button class="term-action" @click="openEditModal(p)">edit</button>
                <span v-if="p.is_active" class="row-actions__sep">·</span>
                <button v-if="p.is_active" class="term-action term-action--danger" @click="handleDeactivate(p)">deactivate</button>
              </div>
            </td>
          </tr>
          <tr v-if="providers.length === 0">
            <td colspan="5"><TermEmpty message="no providers · add one to enable sso login" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? 'edit · provider' : 'add · provider'" width="720px" @close="showModal = false">
      <div class="form-grid">
        <div class="form-row-2">
          <TermField label="name">
            <input v-model="form.name" class="term-input" />
          </TermField>
          <TermField label="type">
            <select v-model="form.provider_type" disabled class="term-select">
              <option value="oidc">oidc</option>
            </select>
          </TermField>
        </div>
        <div class="form-row-2">
          <TermField label="button text" hint="shown on /login">
            <input v-model="form.button_text" class="term-input" placeholder="sign in with corporate sso" />
          </TermField>
          <TermField label="default department">
            <select v-model="form.default_department_id" class="term-select">
              <option :value="null">— none —</option>
              <option v-for="d in departments" :key="d.id" :value="d.id">{{ d.name }}</option>
            </select>
          </TermField>
        </div>
        <div class="form-row-2">
          <label class="form-toggle">
            <input v-model="form.is_active" type="checkbox" />
            <span>active</span>
          </label>
          <label class="form-toggle">
            <input v-model="form.auto_create_users" type="checkbox" />
            <span>auto-create users on first login</span>
          </label>
        </div>

        <TermSection title="oidc · endpoints" />

        <div class="form-row-2">
          <TermField label="issuer url">
            <input v-model="form.oidc_issuer_url" class="term-input" placeholder="https://idp.example.com" />
          </TermField>
          <TermField label="client id">
            <input v-model="form.oidc_client_id" class="term-input" />
          </TermField>
          <TermField label="client secret" :hint="editingId ? 'leave blank to keep existing — encrypted at rest' : 'aes-gcm encrypted before write'">
            <input v-model="form.oidc_client_secret" type="password" class="term-input" :placeholder="editingId ? '*** unchanged ***' : ''" />
          </TermField>
          <TermField label="scopes">
            <input v-model="form.oidc_scopes" class="term-input" placeholder="openid profile email" />
          </TermField>
          <TermField label="authorization endpoint">
            <input v-model="form.oidc_authorization_endpoint" class="term-input" />
          </TermField>
          <TermField label="token endpoint">
            <input v-model="form.oidc_token_endpoint" class="term-input" />
          </TermField>
          <TermField label="userinfo endpoint">
            <input v-model="form.oidc_userinfo_endpoint" class="term-input" />
          </TermField>
          <TermField label="username claim">
            <input v-model="form.oidc_username_claim" class="term-input" placeholder="preferred_username" />
          </TermField>
          <TermField label="email claim">
            <input v-model="form.oidc_email_claim" class="term-input" placeholder="email" />
          </TermField>
          <TermField label="subject claim">
            <input v-model="form.oidc_subject_claim" class="term-input" placeholder="sub" />
          </TermField>
        </div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="cancel" />
        <TermButton variant="primary" :disabled="!form.name.trim()" :label="editingId ? 'update' : 'create'" @click="handleSubmit" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listDepartments } from '../api/departments'
import { createAuthProvider, deactivateAuthProvider, listAuthProviders, updateAuthProvider } from '../api/authProviders'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermSection } from '../components/cli'

const SECRET_MASK = '***'

const providers = ref([])
const departments = ref([])
const showModal = ref(false)
const editingId = ref(null)
const pageError = ref('')

const defaultForm = () => ({
  name: '', provider_type: 'oidc', button_text: '',
  is_active: true, auto_create_users: true, default_role: 'user', default_department_id: null,
  oidc_issuer_url: '', oidc_client_id: '', oidc_client_secret: '',
  oidc_authorization_endpoint: '', oidc_token_endpoint: '', oidc_userinfo_endpoint: '',
  oidc_scopes: 'openid profile email',
  oidc_username_claim: 'preferred_username',
  oidc_email_claim: 'email',
  oidc_subject_claim: 'sub',
})
const form = ref(defaultForm())

async function fetchData() {
  pageError.value = ''
  try {
    const [{ data: p }, { data: d }] = await Promise.all([listAuthProviders(), listDepartments()])
    providers.value = p
    departments.value = d.filter(x => x.is_active)
  } catch (e) {
    pageError.value = e.response?.data?.detail || 'failed to load providers'
  }
}
onMounted(fetchData)

function openCreateModal() { editingId.value = null; form.value = defaultForm(); showModal.value = true }
function openEditModal(p) {
  editingId.value = p.id
  const merged = { ...defaultForm(), ...p }
  merged.oidc_client_secret = ''
  form.value = merged
  showModal.value = true
}

async function handleSubmit() {
  const payload = { ...form.value }
  if (editingId.value && !payload.oidc_client_secret) payload.oidc_client_secret = SECRET_MASK
  try {
    if (editingId.value) await updateAuthProvider(editingId.value, payload)
    else await createAuthProvider(payload)
    showModal.value = false
    await fetchData()
  } catch (e) {
    alert(e.response?.data?.detail || 'save failed')
  }
}
async function handleDeactivate(p) {
  if (!confirm(`deactivate '${p.name}'?`)) return
  try { await deactivateAuthProvider(p.id); await fetchData() }
  catch (e) { alert(e.response?.data?.detail || 'deactivate failed') }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__eyebrow { font-size: var(--t-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-fg-3); }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }

.feedback { font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.row-actions { display: inline-flex; gap: 6px; align-items: center; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
.form-toggle { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-sm); color: var(--c-fg-2); cursor: pointer; }
.form-toggle input { accent-color: var(--c-accent); }
</style>
