<template>
  <TermBox
    v-if="isAdmin && (line || canRollback)"
    title="嵌入重建"
    hint="換模型之後，搜尋仍用上一個模型，直到背景重建完成。"
  >
    <p v-if="line" class="cell-meta">{{ line }}</p>
    <TermButton
      v-if="unembeddable > 0"
      size="xs"
      label="重試無法嵌入的資料"
      :loading="busy"
      :disabled="busy"
      @click="retry"
    />
    <TermButton
      v-if="canRollback"
      size="xs"
      label="切回上一個模型"
      :loading="busy"
      :disabled="busy"
      @click="rollback"
    />
  </TermBox>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { TermBox, TermButton } from './cli'
import { getEmbeddingRebuild, retryUnembeddable, rollbackEmbeddingRebuild } from '../api/models'
import { extractError } from '../api/errors'
import { useDialog } from '../composables/useDialog'
import { formatRebuildStatus, rollbackAvailable } from '../utils/platformEmbedding'

defineProps({
  isAdmin: { type: Boolean, default: false },
})

const { toast } = useDialog()
const status = ref(null)
const busy = ref(false)
let timer = null

const line = computed(() => formatRebuildStatus(status.value))
const canRollback = computed(() => rollbackAvailable(status.value))
const unembeddable = computed(() => Number(status.value?.rebuild?.errors || 0))

async function load() {
  try {
    const { data } = await getEmbeddingRebuild()
    status.value = data
  } catch {
    // 進度讀不到不擋模型頁。下一次輪詢再試。
  }
}

async function retry() {
  busy.value = true
  try {
    const { data } = await retryUnembeddable()
    status.value = data
    toast('已把無法嵌入的資料放回重建', { tone: 'ok' })
  } catch (err) {
    toast(extractError(err, '重試無法嵌入的資料失敗'), { tone: 'error' })
  } finally {
    busy.value = false
  }
}

async function rollback() {
  busy.value = true
  try {
    const { data } = await rollbackEmbeddingRebuild()
    status.value = data
    toast('已切回上一個模型', { tone: 'ok' })
  } catch (err) {
    toast(extractError(err, '切回上一個模型失敗'), { tone: 'error' })
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
