<template>
  <div class="page">
    <PageHead
      title="人資資料庫"
      subtitle="卡片登入時向人資查這一位同仁。密碼存進去之後不會再顯示。"
    />

    <TermBox v-if="feedback" :tone="feedbackTone" dismissible @dismiss="feedback = ''">
      {{ feedback }}
    </TermBox>

    <TermBox>
      <p v-if="!authStore.isOwner" class="health">只有擁有者可以變更這些設定。</p>
      <form class="form-grid" @submit.prevent="save">
        <label class="check">
          <input v-model="draft.enabled" type="checkbox" :disabled="!authStore.isOwner" />
          啟用
        </label>
        <TermField label="主機" hint="Oracle 主機。要先加到信任主機。">
          <input v-model="draft.host" class="term-input" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <div v-if="privateEndpoint" class="private-endpoint" data-testid="private-endpoint-action">
          <p>{{ privateEndpoint.message }}</p>
          <TermButton
            v-if="privateEndpoint.canManage"
            type="button"
            variant="primary"
            :disabled="busy"
            :label="privateEndpoint.buttonLabel"
            @click="addTrustedHostAndRetry"
          />
          <router-link v-else :to="privateEndpoint.linkTo">
            {{ privateEndpoint.linkLabel }}
          </router-link>
        </div>
        <TermField label="埠" hint="預設 1521。">
          <input v-model="draft.port" class="term-input" inputmode="numeric" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <TermField label="服務名稱">
          <input v-model="draft.serviceName" class="term-input" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <TermField label="帳號">
          <input v-model="draft.user" class="term-input" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <TermField
          label="密碼"
          :hint="hasPassword ? '已儲存。留白表示不改。想清掉就勾選清除。' : '存進去之後不會再顯示明文。'"
        >
          <input
            v-model="draft.password"
            class="term-input"
            type="password"
            autocomplete="new-password"
            :disabled="!authStore.isOwner"
          />
        </TermField>
        <label v-if="hasPassword" class="check">
          <input v-model="draft.clearPassword" type="checkbox" :disabled="!authStore.isOwner" />
          清除已存密碼
        </label>
        <TermField label="資料表" hint="例如 SCHEMA.TABLE。只接受識別名稱。">
          <input v-model="draft.tableName" class="term-input" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <TermField label="根單位名稱" hint="一級單位都掛在這個最上層單位之下。還沒有最上層單位時，用這個名稱建立。">
          <input v-model="draft.rootUnitName" class="term-input" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <TermField label="單位管理員職稱" hint="完全相符，一行一個。留空表示不依職稱授與。">
          <textarea v-model="draft.unitAdminTitles" class="term-input" rows="4" :disabled="!authStore.isOwner" />
        </TermField>
        <TermField label="降密審批職稱" hint="完全相符，一行一個。留空表示不依職稱授與。">
          <textarea v-model="draft.declassTitles" class="term-input" rows="4" :disabled="!authStore.isOwner" />
        </TermField>
        <p class="health">連線：{{ healthText }}</p>
        <p v-if="placementNote" class="health">{{ placementNote }}</p>
        <div v-if="authStore.isOwner" class="row-actions">
          <TermButton type="submit" variant="primary" :disabled="busy" label="儲存" />
        </div>
      </form>
    </TermBox>

    <TermBox>
      <h2 class="svc-title">測試連線</h2>
      <p class="health">用已儲存的連線設定查一位同仁。這一頁不會把密碼送出去。</p>
      <form class="form-grid" @submit.prevent="runTest">
        <TermField label="員工編號">
          <input v-model="employeeNo" class="term-input" autocomplete="off" :disabled="!authStore.isOwner" />
        </TermField>
        <div v-if="authStore.isOwner" class="row-actions">
          <TermButton type="submit" variant="primary" :disabled="busy" :loading="testing" label="測試連線" />
        </div>
      </form>
      <p v-if="testResult" class="health">{{ testResult }}</p>
    </TermBox>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { extractError, privateEndpointFromError } from '../api/errors'
import { getHrDatabase, testHrDatabase, updateHrDatabase } from '../api/hrDatabase'
import { useAuthStore } from '../stores/auth'
import { PageHead, TermBox, TermButton, TermField } from '../components/cli'

const authStore = useAuthStore()
const feedback = ref('')
const feedbackTone = ref('ok')
const busy = ref(false)
const testing = ref(false)
const hasPassword = ref(false)
const healthText = ref('尚未檢查')
const employeeNo = ref('')
const testResult = ref('')
const privateEndpoint = ref(null)
const placementNote = ref('')
const draft = reactive({
  enabled: false,
  host: '',
  port: '1521',
  serviceName: '',
  user: '',
  password: '',
  clearPassword: false,
  tableName: '',
  rootUnitName: '國家中山科學研究院',
  unitAdminTitles: '',
  declassTitles: '',
})

function titlesToText(list) {
  return Array.isArray(list) ? list.join('\n') : ''
}

function titlesFromText(text) {
  return String(text || '')
    .split(/[\n、，,;；]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function applyView(data) {
  draft.enabled = Boolean(data.enabled)
  draft.host = data.host || ''
  draft.port = String(data.port || 1521)
  draft.serviceName = data.service_name || ''
  draft.user = data.user || ''
  draft.password = ''
  draft.clearPassword = false
  draft.tableName = data.table_name || ''
  draft.rootUnitName = data.root_unit_name || '國家中山科學研究院'
  draft.unitAdminTitles = titlesToText(data.unit_admin_titles)
  draft.declassTitles = titlesToText(data.declass_titles)
  placementNote.value = data.placement_note || ''
  hasPassword.value = Boolean(data.has_password)
  const detail = data.health_detail ? `（${data.health_detail}）` : ''
  healthText.value = `${data.health_status || 'unknown'}${detail}`
}

function bodyFor() {
  const body = {
    enabled: draft.enabled,
    host: draft.host,
    port: Number(draft.port) || 1521,
    service_name: draft.serviceName,
    user: draft.user,
    table_name: draft.tableName,
    root_unit_name: draft.rootUnitName,
    unit_admin_titles: titlesFromText(draft.unitAdminTitles),
    declass_titles: titlesFromText(draft.declassTitles),
  }
  if (draft.clearPassword) body.password = ''
  else if (draft.password) body.password = draft.password
  return body
}

async function load() {
  const { data } = await getHrDatabase()
  applyView(data)
}

function rememberPrivateEndpoint(error, body) {
  const action = privateEndpointFromError(error, {
    canManageTrustedHosts: authStore.isOwner,
  })
  privateEndpoint.value = action ? { ...action, retryBody: body } : null
}

async function save() {
  if (!authStore.isOwner) return
  const body = bodyFor()
  busy.value = true
  feedback.value = ''
  try {
    const { data } = await updateHrDatabase(body)
    applyView(data)
    privateEndpoint.value = null
    feedbackTone.value = 'ok'
    feedback.value = '已儲存。密碼不會顯示回來。'
  } catch (error) {
    feedbackTone.value = 'error'
    feedback.value = extractError(error, '儲存失敗')
    rememberPrivateEndpoint(error, body)
  } finally {
    busy.value = false
  }
}

async function addTrustedHostAndRetry() {
  const block = privateEndpoint.value
  if (!block?.canManage || busy.value) return
  busy.value = true
  feedback.value = ''
  try {
    const { createTrustedHost } = await import('../api/trustedHosts')
    await createTrustedHost({ host: block.host, note: block.note })
    const { data } = await updateHrDatabase(block.retryBody)
    applyView(data)
    privateEndpoint.value = null
    feedbackTone.value = 'ok'
    feedback.value = '已儲存。密碼不會顯示回來。'
  } catch (error) {
    feedbackTone.value = 'error'
    feedback.value = extractError(error, '儲存失敗')
    rememberPrivateEndpoint(error, block.retryBody)
  } finally {
    busy.value = false
  }
}

function describeTest(data) {
  if (!data?.ok) return data?.message || '測試連線失敗'
  if (!data.found) return data.message || '查無此人'
  const units = [data.dept1, data.dept2].filter(Boolean).join(' / ') || '（沒有單位）'
  const titles = Array.isArray(data.titles) && data.titles.length ? data.titles.join('、') : '（沒有職稱）'
  return `${data.name || '（沒有姓名）'}；${units}；${titles}`
}

async function runTest() {
  if (!authStore.isOwner) return
  busy.value = true
  testing.value = true
  feedback.value = ''
  testResult.value = ''
  try {
    const { data } = await testHrDatabase(employeeNo.value)
    testResult.value = describeTest(data)
    feedbackTone.value = data?.ok ? 'ok' : 'error'
    feedback.value = testResult.value
    await load()
  } catch (error) {
    feedbackTone.value = 'error'
    feedback.value = extractError(error, '測試連線失敗')
    testResult.value = feedback.value
  } finally {
    testing.value = false
    busy.value = false
  }
}

onMounted(() => {
  load().catch((error) => {
    feedbackTone.value = 'error'
    feedback.value = extractError(error, '讀取失敗')
  })
})
</script>

<style scoped>
.svc-title { margin: 0 0 8px; font-size: 16px; }
.form-grid { display: grid; gap: 12px; max-width: 640px; }
.check { display: flex; gap: 8px; align-items: center; }
.health { margin: 0; }
.private-endpoint { display: grid; gap: 8px; }
</style>
