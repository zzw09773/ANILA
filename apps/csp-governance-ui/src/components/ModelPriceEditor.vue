<template>
  <div class="price-editor" data-testid="model-price-editor">
    <p class="price-editor__title">單價 · 每百萬 token</p>
    <p class="cell-meta">改價只新增一列，不覆寫舊價。成本用呼叫當時生效的價格。思考留空時沿用輸出價。三個都留空代表從這個時點起未計價。</p>
    <div class="price-editor__currency">
      <TermField label="平台貨幣" hint="全平台只用一種，預設 TWD">
        <input v-model="currency" class="term-input" maxlength="3" data-testid="billing-currency" :disabled="currencyLocked || saving" />
      </TermField>
      <TermButton variant="default" type="button" label="更新貨幣" :disabled="saving || currencyLocked" @click="saveCurrency" />
    </div>
    <p v-if="currencyLocked" class="cell-meta">已有單價或金額額度，貨幣已鎖定。</p>
    <div class="price-editor__grid">
      <TermField label="輸入" optional>
        <input v-model="form.input_per_million" class="term-input" inputmode="decimal" placeholder="未計價" />
      </TermField>
      <TermField label="輸出" optional>
        <input v-model="form.output_per_million" class="term-input" inputmode="decimal" placeholder="未計價" />
      </TermField>
      <TermField label="思考" optional hint="留空＝沿用輸出價">
        <input v-model="form.reasoning_per_million" class="term-input" inputmode="decimal" placeholder="沿用輸出價" />
      </TermField>
      <TermField label="生效時間" optional hint="留空＝現在 · 台北時間">
        <input v-model="form.effective_at" type="datetime-local" class="term-input" />
      </TermField>
    </div>
    <p v-if="error" class="cell-meta cell-meta--danger">{{ error }}</p>
    <TermButton variant="primary" type="button" label="新增這筆單價" :disabled="saving" @click="savePrice" />
    <table class="term-table price-editor__history">
      <thead>
        <tr>
          <th>生效</th>
          <th class="num">輸入</th>
          <th class="num">輸出</th>
          <th class="num">思考</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in history" :key="row.id">
          <td class="cell-meta tnum">{{ formatDate(row.effective_at) }}</td>
          <td class="num tnum">{{ row.input_per_million || '未計價' }}</td>
          <td class="num tnum">{{ row.output_per_million || '未計價' }}</td>
          <td class="num tnum">{{ row.reasoning_per_million || '沿用輸出' }}</td>
        </tr>
        <tr v-if="history.length === 0">
          <td colspan="4"><TermEmpty message="尚未設定單價，用量會顯示未計價。" /></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { TermButton, TermEmpty, TermField } from './cli'
import { extractError } from '../api/errors'
import {
  addModelPrice,
  getBillingCurrency,
  listModelPrices,
  setBillingCurrency,
} from '../api/pricing'
import { formatDate } from '../utils/formatDate'

const props = defineProps({
  modelId: { type: Number, required: true },
})

const history = ref([])
const currency = ref('TWD')
const currencyLocked = ref(false)
const error = ref('')
const saving = ref(false)
const form = ref({
  input_per_million: '',
  output_per_million: '',
  reasoning_per_million: '',
  effective_at: '',
})

function blankToNull(value) {
  const text = String(value ?? '').trim()
  return text === '' ? null : text
}

function effectivePayload(value) {
  if (!value) return undefined
  if (value.length === 16) return `${value}:00+08:00`
  return value
}

async function load() {
  error.value = ''
  try {
    const [{ data: prices }, { data: money }] = await Promise.all([
      listModelPrices(props.modelId),
      getBillingCurrency(),
    ])
    history.value = prices
    currency.value = money.currency || 'TWD'
    currencyLocked.value = !!money.locked
  } catch (err) {
    error.value = extractError(err, '讀不到單價')
  }
}

async function saveCurrency() {
  saving.value = true
  error.value = ''
  try {
    const { data } = await setBillingCurrency(currency.value)
    currency.value = data.currency
  } catch (err) {
    error.value = extractError(err, '貨幣沒有更新')
  } finally {
    saving.value = false
  }
}

async function savePrice() {
  saving.value = true
  error.value = ''
  try {
    await addModelPrice(props.modelId, {
      input_per_million: blankToNull(form.value.input_per_million),
      output_per_million: blankToNull(form.value.output_per_million),
      reasoning_per_million: blankToNull(form.value.reasoning_per_million),
      effective_at: effectivePayload(form.value.effective_at),
    })
    form.value = {
      input_per_million: '',
      output_per_million: '',
      reasoning_per_million: '',
      effective_at: '',
    }
    await load()
  } catch (err) {
    error.value = extractError(err, '單價沒有寫入')
  } finally {
    saving.value = false
  }
}

watch(() => props.modelId, load, { immediate: true })
</script>

<style scoped>
.price-editor {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
  margin-top: var(--gap-3);
  padding-top: var(--gap-3);
  border-top: var(--border-w) dashed var(--c-border);
}
.price-editor__title { font-weight: 600; margin: 0; }
.price-editor__currency,
.price-editor__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap-3);
  align-items: end;
}
.price-editor__history { margin-top: var(--gap-2); }
</style>
