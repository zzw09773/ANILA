import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  listModels, createModel, updateModel, deleteModel, activateModel, purgeModel, triggerHealthCheck,
  setRouterPrimary, unsetRouterPrimary, testModelConnection,
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

  async function create(payload) {
    await createModel(payload)
    await fetchModels()
  }

  async function update(id, payload) {
    await updateModel(id, payload)
    await fetchModels()
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

  async function checkHealth(id) {
    const { data } = await triggerHealthCheck(id)
    await fetchModels()
    return data
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

  return {
    models, loading, fetchModels, create, update, remove, activate, purge, checkHealth, test,
    setPrimary, unsetPrimary,
  }
})
