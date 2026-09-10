import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  listModels, createModel, updateModel, deleteModel, activateModel, purgeModel,
  setRouterPrimary, unsetRouterPrimary, setImagePrimary as setImagePrimaryApi,
  unsetImagePrimary as unsetImagePrimaryApi,
  setSlidesPrimary as setSlidesPrimaryApi, unsetSlidesPrimary as unsetSlidesPrimaryApi,
  setAsrPrimary as setAsrPrimaryApi, unsetAsrPrimary as unsetAsrPrimaryApi,
  setPlatformEmbedding, unsetPlatformEmbedding,
  testModelConnection, importModelsFromEndpoint,
  activateCreatedFromImport,
} from '../api/models'

export const useModelsStore = defineStore('models', () => {
  const models = ref([])
  const loading = ref(false)

  async function fetchModels() {
    loading.value = true
    try {
      const { data } = await listModels()
      models.value = data
    } finally {
      loading.value = false
    }
  }

  // 回傳寫入後的模型列：thinking_probe 只在 create/update 出現（列表不探測），
  // 呼叫端要靠它判斷「等級沒探到」。
  async function create(payload) {
    const { data } = await createModel(payload)
    await fetchModels()
    return data
  }

  async function update(id, payload) {
    const { data } = await updateModel(id, payload)
    await fetchModels()
    return data
  }

  async function remove(id) {
    await deleteModel(id)
    await fetchModels()
  }

  async function activate(id) {
    await activateModel(id)
    await fetchModels()
  }

  async function purge(id) {
    await purgeModel(id)
    await fetchModels()
  }

  // Slice 6b — 主動探測。回傳 { health_status, latency_ms }，並 refetch
  // 讓列表的五態 badge 反映最新結果。
  async function test(id) {
    const { data } = await testModelConnection(id)
    await fetchModels()
    return data
  }

  async function setPrimary(id) {
    await setRouterPrimary(id)
    await fetchModels()
  }

  async function unsetPrimary(id) {
    await unsetRouterPrimary(id)
    await fetchModels()
  }

  // FLUX 主圖像模型（image-primary）— 完全比照 setPrimary/unsetPrimary 寫法。
  async function setImagePrimary(id) {
    await setImagePrimaryApi(id)
    await fetchModels()
  }

  async function unsetImagePrimary(id) {
    await unsetImagePrimaryApi(id)
    await fetchModels()
  }

  async function setSlidesPrimary(id) {
    await setSlidesPrimaryApi(id)
    await fetchModels()
  }

  async function unsetSlidesPrimary(id) {
    await unsetSlidesPrimaryApi(id)
    await fetchModels()
  }

  async function setAsrPrimary(id) {
    await setAsrPrimaryApi(id)
    await fetchModels()
  }

  async function unsetAsrPrimary(id) {
    await unsetAsrPrimaryApi(id)
    await fetchModels()
  }

  async function setPlatformEmbed(id) {
    const { data } = await setPlatformEmbedding(id)
    await fetchModels()
    return data
  }

  async function unsetPlatformEmbed(id) {
    await unsetPlatformEmbedding(id)
    await fetchModels()
  }

  // P4.6 — 自已註冊端點整批帶入。回傳後端計數結果並刷新列表。
  async function importFromEndpoint(sourceModelId) {
    const { data } = await importModelsFromEndpoint(sourceModelId)
    await fetchModels()
    return data
  }

  // P4.6 — 一次啟用本次帶入新增的停用列。
  async function activateCreated(sourceModelId, names) {
    const { data } = await activateCreatedFromImport(sourceModelId, names)
    await fetchModels()
    return data
  }

  return {
    models, loading, fetchModels, create, update, remove, activate, purge, test,
    setPrimary, unsetPrimary, setImagePrimary, setSlidesPrimary, unsetSlidesPrimary, unsetImagePrimary,
    setAsrPrimary, unsetAsrPrimary,
    setPlatformEmbed, unsetPlatformEmbed,
    importFromEndpoint, activateCreated,
  }
})
