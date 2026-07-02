import { defineStore } from 'pinia'
import { ref } from 'vue'
import { listApiKeys, createApiKey, updateApiKey, revokeApiKey, regenerateApiKey } from '../api/apiKeys'

export const useApiKeysStore = defineStore('apiKeys', () => {
  const keys = ref([])
  const loading = ref(false)

  async function fetchKeys() {
    loading.value = true
    try {
      const { data } = await listApiKeys()
      keys.value = data
    } finally {
      loading.value = false
    }
  }

  async function create(payload) {
    const { data } = await createApiKey(payload)
    await fetchKeys()
    return data // Contains full_key
  }

  async function update(id, payload) {
    await updateApiKey(id, payload)
    await fetchKeys()
  }

  async function revoke(id) {
    await revokeApiKey(id)
    await fetchKeys()
  }

  async function regenerate(id) {
    const { data } = await regenerateApiKey(id)
    await fetchKeys()
    return data
  }

  return { keys, loading, fetchKeys, create, update, revoke, regenerate }
})
