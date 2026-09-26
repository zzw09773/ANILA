<template>
  <div class="page">
    <PageHead
      title="外部服務"
      subtitle="文件解析與語音辨識的位址。改完不用重編映像，也不用改環境變數。"
    />

    <TermBox v-if="feedback" :tone="feedbackTone" dismissible @dismiss="feedback = ''">
      {{ feedback }}
    </TermBox>

    <TermBox v-for="item in services" :key="item.service_key">
      <h2 class="svc-title">{{ titleOf(item) }}</h2>
      <p class="fallback-note">{{ item.fallback_note }}</p>
      <p v-if="item.service_key === 'document_parser' && item.fallback === 'native'" class="fallback-note">
        目前使用內建原生解析器。
      </p>
      <form class="form-grid" @submit.prevent="save(item)">
        <label class="check">
          <input v-model="drafts[item.service_key].enabled" type="checkbox" />
          啟用
        </label>
        <TermField label="位址" hint="https 或已放行的 http。不要把憑證寫在網址裡。">
          <input
            v-model="drafts[item.service_key].base_url"
            class="term-input"
            autocomplete="off"
            placeholder="https://host:9100"
          />
        </TermField>
        <TermField
          v-if="item.service_key === 'speech'"
          label="協定"
          hint="native 送 X-Token；openai 送 Bearer 到 /v1/audio/transcriptions。"
        >
          <select v-model="drafts[item.service_key].protocol" class="term-input">
            <option value="native">native</option>
            <option value="openai">openai</option>
          </select>
        </TermField>
        <TermField
          v-if="item.service_key === 'speech' && drafts[item.service_key].protocol === 'openai'"
          label="模型名稱"
        >
          <input v-model="drafts[item.service_key].openai_model" class="term-input" />
        </TermField>
        <TermField
          label="憑證"
          :hint="item.has_credential ? '已儲存。留白表示不改。想清掉就勾選清除。' : '選填。存進去之後不會再顯示明文。'"
        >
          <input
            v-model="drafts[item.service_key].credential"
            class="term-input"
            type="password"
            autocomplete="new-password"
          />
        </TermField>
        <label v-if="item.has_credential" class="check">
          <input v-model="drafts[item.service_key].clearCredential" type="checkbox" />
          清除已存憑證
        </label>
        <p class="health">
          健康：{{ item.health_status }}
          <span v-if="item.health_detail"> — {{ item.health_detail }}</span>
          。由平台背景更新，這一頁不會把憑證送去探測。
        </p>
        <div class="row-actions">
          <TermButton type="submit" variant="primary" :disabled="busy" label="儲存" />
          <TermButton
            variant="ghost"
            :disabled="busy"
            label="重新整理"
            @click="reloadStatus"
          />
        </div>
      </form>
    </TermBox>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { extractError } from '../api/errors'
import {
  listExternalServices,
  updateExternalService,
} from '../api/externalServices'
import { PageHead, TermBox, TermButton, TermField } from '../components/cli'

const services = ref([])
const drafts = reactive({})
const feedback = ref('')
const feedbackTone = ref('ok')
const busy = ref(false)

function titleOf(item) {
  return item.service_key === 'document_parser' ? '文件解析（Docling）' : '語音辨識'
}

function remember(list) {
  services.value = list
  for (const item of list) {
    drafts[item.service_key] = {
      enabled: item.enabled,
      base_url: item.base_url || '',
      protocol: item.protocol || 'native',
      openai_model: item.openai_model || 'whisper-1',
      credential: '',
      clearCredential: false,
    }
  }
}

async function load() {
  const { data } = await listExternalServices()
  remember(data.services || [])
}

function bodyFor(item) {
  const draft = drafts[item.service_key]
  const body = {
    enabled: draft.enabled,
    base_url: draft.base_url,
  }
  if (item.service_key === 'speech') {
    body.protocol = draft.protocol
    body.openai_model = draft.openai_model
  }
  if (draft.clearCredential) body.credential = ''
  else if (draft.credential) body.credential = draft.credential
  return body
}

async function save(item) {
  busy.value = true
  feedback.value = ''
  try {
    await updateExternalService(item.service_key, bodyFor(item))
    feedbackTone.value = 'ok'
    feedback.value = '已儲存。憑證不會顯示回來。'
    await load()
  } catch (error) {
    feedbackTone.value = 'error'
    feedback.value = extractError(error, '儲存失敗')
  } finally {
    busy.value = false
  }
}

async function reloadStatus() {
  busy.value = true
  feedback.value = ''
  try {
    await load()
    feedbackTone.value = 'ok'
    feedback.value = '已重新整理。畫面上是最近一次背景探測的結果。'
  } catch (error) {
    feedbackTone.value = 'error'
    feedback.value = extractError(error, '讀取失敗')
  } finally {
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
.fallback-note { margin: 0 0 12px; }
.form-grid { display: grid; gap: 12px; max-width: 640px; }
.check { display: flex; gap: 8px; align-items: center; }
.health { margin: 0; }
</style>
